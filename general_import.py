import io
import math
import re
import unicodedata
import zipfile
from datetime import date, datetime

import pandas as pd


IGNORED_SHEETS = {"podsumowanie", "instrukcja", "summary", "instructions"}

ENTITY_LABELS = {
    "product": "Produkty",
    "package": "Paczki",
    "issue": "Wydane produkty",
    "contractor": "Kontrahenci",
    "receipt": "Przyjęcia",
    "document": "Dokumenty",
    "shop_order": "Sklep internetowy",
    "accounting": "Księgowość",
}

ENTITY_GROUPS = {
    "product": "warehouse",
    "package": "warehouse",
    "issue": "warehouse",
    "contractor": "warehouse",
    "receipt": "warehouse",
    "document": "warehouse",
    "shop_order": "accounting",
    "accounting": "accounting",
}

ENTITY_FIELDS = {
    "product": ("name", "qty", "unit", "warehouse", "price_netto", "vat"),
    "package": ("package_number", "product_name", "qty", "unit", "warehouse"),
    "issue": (
        "product_name", "qty", "unit", "warehouse", "package_number",
        "contractor", "date", "doc_number", "movement_type",
    ),
    "contractor": ("contractor", "nip", "email", "phone", "address"),
    "receipt": (
        "product_name", "qty", "unit", "warehouse", "package_number",
        "contractor", "date", "doc_number",
    ),
    "document": (
        "doc_number", "date", "contractor", "movement_type", "warehouse",
        "product_name", "qty", "unit", "package_number",
    ),
    "shop_order": (
        "order_number", "date", "contractor", "address", "phone", "email",
        "shipping_cost", "payment_method", "payment_status", "status",
        "doc_number", "tracking_number", "nip", "product_name", "qty",
        "price_netto", "vat", "warehouse",
    ),
    "accounting": (
        "order_number", "payment_method", "paid", "amount_paid", "amount_due",
        "invoice_number", "receipt_number", "proforma_number", "date",
    ),
}

FIELD_LABELS = {
    "name": "Nazwa produktu",
    "product_name": "Nazwa produktu",
    "qty": "Ilość",
    "unit": "Jednostka",
    "warehouse": "Magazyn",
    "package_number": "Numer paczki",
    "contractor": "Kontrahent",
    "date": "Data",
    "doc_number": "Numer dokumentu",
    "movement_type": "Typ dokumentu",
    "price_netto": "Cena netto",
    "vat": "VAT",
    "nip": "NIP",
    "email": "E-mail",
    "phone": "Telefon",
    "address": "Adres",
    "order_number": "Numer zamówienia",
    "shipping_cost": "Koszt wysyłki",
    "payment_method": "Sposób płatności",
    "payment_status": "Status płatności",
    "status": "Status",
    "tracking_number": "Numer przesyłki",
    "paid": "Zapłacono",
    "amount_paid": "Kwota zapłacona",
    "amount_due": "Kwota należna",
    "invoice_number": "Numer faktury",
    "receipt_number": "Numer paragonu",
    "proforma_number": "Numer proformy",
}

ALIASES = {
    "name": {"name", "nazwa", "produkt", "nazwa produktu", "towar", "asortyment"},
    "product_name": {
        "product name", "nazwa produktu", "produkt", "towar", "nazwa", "asortyment",
    },
    "qty": {"qty", "quantity", "ilosc", "ilość", "stan", "wydano", "przyjeto", "przyjęto"},
    "unit": {"unit", "jednostka", "jm", "j m", "jedn"},
    "warehouse": {"warehouse", "magazyn", "lokalizacja", "sklad", "skład"},
    "package_number": {
        "package number", "package_number", "numer paczki", "nr paczki",
        "paczka", "numer pakietu",
    },
    "contractor": {
        "contractor", "kontrahent", "klient", "dostawca", "odbiorca",
        "customer", "customer name",
    },
    "date": {"date", "data", "data dokumentu", "data zamowienia", "data zamówienia"},
    "doc_number": {
        "doc number", "doc_number", "numer dokumentu", "nr dokumentu",
        "dokument", "faktura paragon",
    },
    "movement_type": {"movement type", "typ dokumentu", "typ", "rodzaj", "operacja"},
    "price_netto": {"price netto", "price_netto", "cena netto", "netto", "cena"},
    "vat": {"vat", "stawka vat", "vat procent", "vat %"},
    "nip": {"nip", "tax id", "tax_id"},
    "email": {"email", "e mail", "e-mail"},
    "phone": {"phone", "telefon", "tel"},
    "address": {"address", "adres", "adres dostawy"},
    "order_number": {
        "order number", "order_number", "numer zamowienia", "numer zamówienia",
        "nr zamowienia", "nr zamówienia", "zamowienie", "zamówienie",
    },
    "shipping_cost": {"shipping cost", "koszt wysylki", "koszt wysyłki", "wysylka", "wysyłka"},
    "payment_method": {"payment method", "sposob platnosci", "sposób płatności", "platnosc", "płatność"},
    "payment_status": {"payment status", "status platnosci", "status płatności"},
    "status": {"status", "status zamowienia", "status zamówienia"},
    "tracking_number": {
        "tracking number", "numer przesylki", "numer przesyłki",
        "nr listu", "list przewozowy",
    },
    "paid": {"paid", "zaplacono", "zapłacono", "oplacone", "opłacone"},
    "amount_paid": {"amount paid", "kwota zaplacona", "kwota zapłacona"},
    "amount_due": {"amount due", "kwota nalezna", "kwota należna", "do zaplaty", "do zapłaty"},
    "invoice_number": {"invoice number", "numer faktury", "nr faktury", "faktura"},
    "receipt_number": {"receipt number", "numer paragonu", "nr paragonu", "paragon"},
    "proforma_number": {"proforma number", "numer proformy", "nr proformy", "proforma"},
}

SHEET_ENTITIES = {
    "produkty": "product",
    "products": "product",
    "paczki": "package",
    "packages": "package",
    "wydane produkty": "issue",
    "wydania": "issue",
    "issued products": "issue",
    "kontrahenci": "contractor",
    "contractors": "contractor",
    "przyjecia": "receipt",
    "przyjęcia": "receipt",
    "receipts": "receipt",
    "dokumenty": "document",
    "documents": "document",
    "sklep internetowy": "shop_order",
    "sklep": "shop_order",
    "shop": "shop_order",
    "ksiegowosc": "accounting",
    "księgowość": "accounting",
    "accounting": "accounting",
}


def normalized_text(value):
    if value is None:
        value = ""
    else:
        try:
            if bool(pd.isna(value)):
                value = ""
        except (TypeError, ValueError):
            pass
    text = str(value).strip().casefold()
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return re.sub(r"[_\-/]+", " ", re.sub(r"\s+", " ", text)).strip()


NORMALIZED_ALIASES = {
    field: {normalized_text(alias) for alias in aliases}
    for field, aliases in ALIASES.items()
}


def json_value(value):
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def guess_mapping(columns, entity_type):
    mapping = {}
    normalized_columns = {str(column): normalized_text(column) for column in columns}
    for field in ENTITY_FIELDS.get(entity_type, ()):
        aliases = NORMALIZED_ALIASES.get(field, set())
        match = next(
            (
                original for original, normalized in normalized_columns.items()
                if normalized in aliases
            ),
            None,
        )
        if match is not None:
            mapping[field] = match
    return mapping


def infer_entity(sheet_name, columns):
    normalized_sheet = normalized_text(sheet_name)
    if normalized_sheet in IGNORED_SHEETS:
        return "ignored"
    if normalized_sheet in SHEET_ENTITIES:
        return SHEET_ENTITIES[normalized_sheet]
    scores = {}
    for entity_type in ENTITY_FIELDS:
        mapping = guess_mapping(columns, entity_type)
        scores[entity_type] = len(mapping)
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else "unknown"


def normalize_row(entity_type, source_data, mapping):
    data = {}
    for field in ENTITY_FIELDS.get(entity_type, ()):
        source_column = mapping.get(field)
        data[field] = json_value(source_data.get(source_column, "")) if source_column else ""
    if entity_type == "issue" and not str(data.get("movement_type") or "").strip():
        data["movement_type"] = "WZ"
    if entity_type == "receipt":
        data["movement_type"] = "PZ"
    if entity_type == "document":
        movement = str(data.get("movement_type") or "").strip().upper()
        data["movement_type"] = movement or "WZ"
    return data


def parse_workbook(file_bytes, max_rows=5000, max_sheets=30, max_columns=100):
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            expanded_size = sum(item.file_size for item in archive.infolist())
            if expanded_size > 100 * 1024 * 1024:
                raise ValueError("Rozpakowany plik Excel jest zbyt duży.")
    except zipfile.BadZipFile as exc:
        raise ValueError("Plik nie jest prawidłowym skoroszytem .xlsx.") from exc
    excel = pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl")
    if len(excel.sheet_names) > max_sheets:
        raise ValueError(f"Plik może zawierać maksymalnie {max_sheets} arkuszy.")
    sheets = []
    total_rows = 0
    for sheet_name in excel.sheet_names:
        dataframe = pd.read_excel(
            excel,
            sheet_name=sheet_name,
            dtype=object,
            nrows=max_rows - total_rows + 1,
        )
        dataframe = dataframe.dropna(how="all")
        columns = [str(column).strip() for column in dataframe.columns]
        if len(columns) > max_columns:
            raise ValueError(
                f"Arkusz {sheet_name} może zawierać maksymalnie {max_columns} kolumn."
            )
        entity_type = infer_entity(sheet_name, columns)
        mapping = guess_mapping(columns, entity_type) if entity_type in ENTITY_FIELDS else {}
        rows = []
        if entity_type != "ignored":
            for row_index, raw_row in dataframe.iterrows():
                source_data = {
                    str(column).strip(): json_value(raw_row.get(column))
                    for column in dataframe.columns
                }
                if not any(value not in ("", None) for value in source_data.values()):
                    continue
                total_rows += 1
                if total_rows > max_rows:
                    raise ValueError(f"Plik może zawierać maksymalnie {max_rows} wierszy danych.")
                rows.append({
                    "row_number": int(row_index) + 2,
                    "source_data": source_data,
                    "normalized_data": (
                        normalize_row(entity_type, source_data, mapping)
                        if entity_type in ENTITY_FIELDS else {}
                    ),
                })
        sheets.append({
            "name": sheet_name,
            "entity_type": entity_type,
            "columns": columns,
            "mapping": mapping,
            "rows": rows,
        })
    return sheets


def parse_number(value, field_label, allow_zero=True):
    if value in ("", None):
        return None
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"{field_label}: nieprawidłowa liczba.")
    if not math.isfinite(number) or number < 0 or (not allow_zero and number <= 0):
        comparator = "większa od zera" if not allow_zero else "nieujemna"
        raise ValueError(f"{field_label} musi być {comparator}.")
    return number


def parse_date(value):
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    if not text:
        return ""
    for date_format in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError("Data jest nieprawidłowa.")


def parse_bool(value):
    return normalized_text(value) in {"1", "true", "tak", "yes", "x", "zaplacono", "oplacone"}


def validate_row(entity_type, data, units, warehouses):
    errors = []
    required = {
        "product": ("name", "qty", "unit", "warehouse"),
        "package": ("package_number", "product_name", "qty", "warehouse"),
        "issue": ("product_name", "qty", "warehouse", "contractor", "date"),
        "contractor": ("contractor",),
        "receipt": ("product_name", "qty", "warehouse", "contractor", "date"),
        "document": ("doc_number", "date", "contractor", "movement_type"),
        "shop_order": ("order_number", "date", "contractor"),
        "accounting": ("order_number",),
    }.get(entity_type, ())
    for field in required:
        if not str(data.get(field, "") or "").strip():
            errors.append(f"{FIELD_LABELS.get(field, field)}: pole wymagane.")

    if data.get("unit") and str(data["unit"]).strip() not in units:
        errors.append("Nieprawidłowa jednostka.")
    if data.get("warehouse") and str(data["warehouse"]).strip() not in warehouses:
        errors.append("Nieprawidłowy magazyn.")
    if len(str(data.get("package_number") or "")) > 100:
        errors.append("Numer paczki może mieć maksymalnie 100 znaków.")
    if len(str(data.get("doc_number") or "")) > 100:
        errors.append("Numer dokumentu może mieć maksymalnie 100 znaków.")

    try:
        if "qty" in data and data.get("qty") not in ("", None):
            allow_zero = entity_type == "product"
            data["qty"] = parse_number(data["qty"], "Ilość", allow_zero=allow_zero)
        for field in ("price_netto", "vat", "shipping_cost", "amount_paid", "amount_due"):
            if field in data and data.get(field) not in ("", None):
                data[field] = parse_number(data[field], FIELD_LABELS[field], allow_zero=True)
        if data.get("vat") not in ("", None) and float(data["vat"]) > 100:
            errors.append("VAT nie może przekraczać 100%.")
    except ValueError as exc:
        errors.append(str(exc))
    if "date" in data and data.get("date"):
        try:
            data["date"] = parse_date(data["date"])
        except ValueError as exc:
            errors.append(str(exc))
    if data.get("movement_type"):
        movement = str(data["movement_type"]).strip().upper()
        if movement not in {"PZ", "WZ", "RW"}:
            errors.append("Typ dokumentu musi mieć wartość PZ, WZ albo RW.")
        data["movement_type"] = movement
    if "paid" in data:
        data["paid"] = parse_bool(data["paid"])
    return errors


def duplicate_identity(entity_type, data):
    if entity_type == "product":
        return (
            normalized_text(data.get("name")),
            normalized_text(data.get("warehouse")),
        )
    if entity_type == "package":
        return (
            normalized_text(data.get("package_number")),
            normalized_text(data.get("warehouse")),
        )
    if entity_type in {"issue", "receipt", "document"}:
        return normalized_text(data.get("doc_number"))
    if entity_type == "contractor":
        return (
            normalized_text(data.get("contractor")),
            normalized_text(data.get("nip")),
        )
    if entity_type in {"shop_order", "accounting"}:
        return normalized_text(data.get("order_number"))
    return None
