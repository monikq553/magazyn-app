import io
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

import app as warehouse_app
from general_import import (
    duplicate_identity,
    normalize_row,
    parse_workbook,
    validate_row,
)


def workbook_bytes(sheets):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets:
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


class PreviewCursor:
    def __init__(self, run, rows):
        self.run = run
        self.rows = rows
        self.result = None

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        if "from general_imports where id=" in normalized:
            self.result = self.run
        elif "from general_import_rows" in normalized:
            self.result = self.rows
        else:
            raise AssertionError(f"Nieobsłużone SQL testu podglądu: {normalized}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


class PreviewConnection:
    def __init__(self, run, rows):
        self.cursor_instance = PreviewCursor(run, rows)

    def cursor(self):
        return self.cursor_instance

    def close(self):
        pass


class GeneralExcelImportTests(unittest.TestCase):
    def test_invalid_xlsx_is_rejected_before_staging(self):
        with self.assertRaisesRegex(ValueError, "prawidłowym skoroszytem"):
            parse_workbook(b"to nie jest plik Excel")

    def test_import_file_with_products(self):
        parsed = parse_workbook(
            workbook_bytes(
                [
                    (
                        "Produkty",
                        [
                            ["Nazwa produktu", "Ilość", "Jednostka", "Magazyn"],
                            ["Deska dębowa", 12.5, "m3", "Drewno"],
                        ],
                    )
                ]
            )
        )

        self.assertEqual(parsed[0]["entity_type"], "product")
        self.assertEqual(parsed[0]["rows"][0]["normalized_data"]["name"], "Deska dębowa")
        self.assertEqual(parsed[0]["rows"][0]["normalized_data"]["qty"], 12.5)

    def test_import_file_with_packages(self):
        parsed = parse_workbook(
            workbook_bytes(
                [
                    (
                        "Paczki",
                        [
                            ["Numer paczki", "Produkt", "Ilość", "Jednostka", "Magazyn"],
                            ["PACZKA-001", "Deska dębowa", 4, "m3", "Drewno"],
                        ],
                    )
                ]
            )
        )

        row = parsed[0]["rows"][0]["normalized_data"]
        self.assertEqual(parsed[0]["entity_type"], "package")
        self.assertEqual(row["package_number"], "PACZKA-001")
        self.assertEqual(row["product_name"], "Deska dębowa")

    def test_contractors_sheet_is_recognized(self):
        parsed = parse_workbook(
            workbook_bytes(
                [
                    (
                        "Kontrahenci",
                        [
                            ["Kontrahent", "NIP", "E-mail", "Telefon", "Adres"],
                            ["Klient A", "1234567890", "a@example.com", "123", "Poznań"],
                        ],
                    )
                ]
            )
        )

        row = parsed[0]["rows"][0]["normalized_data"]
        self.assertEqual(parsed[0]["entity_type"], "contractor")
        self.assertEqual(row["contractor"], "Klient A")
        self.assertEqual(row["nip"], "1234567890")

    def test_import_issued_products_and_ignore_information_sheets(self):
        parsed = parse_workbook(
            workbook_bytes(
                [
                    (
                        "Wydane produkty",
                        [
                            [
                                "Produkt", "Ilość", "Jednostka", "Magazyn",
                                "Numer paczki", "Kontrahent", "Data", "Numer dokumentu",
                            ],
                            [
                                "Deska dębowa", 2, "m3", "Drewno", "PACZKA-001",
                                "Klient A", "2026-07-06", "WZ-15",
                            ],
                        ],
                    ),
                    ("Podsumowanie", [["Liczba pozycji", 1]]),
                    ("Instrukcja", [["Nie zmieniaj nagłówków"]]),
                ]
            )
        )

        issued = parsed[0]["rows"][0]["normalized_data"]
        self.assertEqual(parsed[0]["entity_type"], "issue")
        self.assertEqual(issued["movement_type"], "WZ")
        self.assertEqual(issued["doc_number"], "WZ-15")
        self.assertEqual(parsed[1]["entity_type"], "ignored")
        self.assertEqual(parsed[1]["rows"], [])
        self.assertEqual(parsed[2]["entity_type"], "ignored")

    def test_manual_column_mapping_and_edit_before_save(self):
        source = {
            "Opis własny": "Deska po korekcie",
            "Stan własny": "7,5",
            "J.M.": "m3",
            "Skład docelowy": "Drewno",
        }
        mapped = normalize_row(
            "product",
            source,
            {
                "name": "Opis własny",
                "qty": "Stan własny",
                "unit": "J.M.",
                "warehouse": "Skład docelowy",
            },
        )
        mapped["name"] = "Deska poprawiona w podglądzie"
        errors = validate_row(
            "product", mapped, warehouse_app.UNITS, warehouse_app.WAREHOUSES
        )

        self.assertEqual(errors, [])
        self.assertEqual(mapped["name"], "Deska poprawiona w podglądzie")
        self.assertEqual(mapped["qty"], 7.5)

    def test_validation_rejects_negative_quantity_bad_unit_and_bad_date(self):
        data = {
            "product_name": "Deska",
            "qty": "-1",
            "unit": "metr",
            "warehouse": "Drewno",
            "contractor": "Klient",
            "date": "31-31-2026",
            "doc_number": "WZ-1",
            "movement_type": "WZ",
        }
        errors = validate_row(
            "issue", data, warehouse_app.UNITS, warehouse_app.WAREHOUSES
        )

        self.assertTrue(any("Ilość" in error for error in errors))
        self.assertIn("Nieprawidłowa jednostka.", errors)
        self.assertIn("Data jest nieprawidłowa.", errors)

    def test_duplicate_detection_keys_cover_products_packages_and_documents(self):
        self.assertEqual(
            duplicate_identity("product", {"name": "DESKA", "warehouse": "Drewno"}),
            duplicate_identity("product", {"name": "deska", "warehouse": "drewno"}),
        )
        self.assertEqual(
            duplicate_identity(
                "package", {"package_number": "PACZKA-1", "warehouse": "Drewno"}
            ),
            ("paczka 1", "drewno"),
        )
        self.assertEqual(
            duplicate_identity("document", {"doc_number": "WZ/15"}),
            "wz 15",
        )

    def test_preview_is_editable_and_confirmation_is_separate(self):
        run = (
            7,
            "import.xlsx",
            "admin@example.com",
            "draft",
            [
                {
                    "name": "Produkty",
                    "entity_type": "product",
                    "columns": ["Nazwa produktu", "Ilość", "Jednostka", "Magazyn"],
                    "mapping": {},
                    "row_count": 1,
                }
            ],
            {},
            [],
            datetime(2026, 7, 6, 10, 0),
            None,
        )
        rows = [
            (
                11,
                "Produkty",
                2,
                "product",
                {"Nazwa produktu": "Deska"},
                {
                    "name": "Deska",
                    "qty": 5,
                    "unit": "m3",
                    "warehouse": "Drewno",
                    "price_netto": 0,
                    "vat": 23,
                },
                None,
                "new",
                True,
                [],
            )
        ]
        connection = PreviewConnection(run, rows)
        with warehouse_app.app.test_request_context("/import-ogolny/7"):
            warehouse_app.session["user"] = "admin@example.com"
            warehouse_app.session["role"] = "admin"
            with patch.object(warehouse_app, "db", return_value=connection):
                response = warehouse_app.general_import_preview(7)

        html = response.encode("utf-8") if isinstance(response, str) else response.get_data()
        self.assertIn(b"Deska", html)
        self.assertIn(b"/import-ogolny/7/prepare", html)
        self.assertIn(b"/import-ogolny/7/confirm", html)
        self.assertIn(b'name="row_11_qty"', html)

    def test_confirmed_product_row_generates_database_insert(self):
        cursor = MagicMock()
        row = (11, "Produkty", 2, "product", {}, {}, None, "new", True, [])
        outcome = warehouse_app.apply_import_product(
            cursor,
            row,
            {
                "name": "Deska",
                "qty": 5,
                "unit": "m3",
                "warehouse": "Drewno",
                "price_netto": 100,
                "vat": 23,
            },
        )

        self.assertEqual(outcome, "added")
        sql, params = cursor.execute.call_args.args
        self.assertIn("INSERT INTO products", sql)
        self.assertEqual(params[:4], ("Deska", 5.0, "m3", "Drewno"))

    def test_shop_rows_with_same_order_share_one_order_record(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (41,)
        cache = {}
        data = {
            "order_number": "SKLEP-100",
            "date": "2026-07-06",
            "contractor": "Klient",
            "product_name": "Deska",
            "qty": 1,
            "price_netto": 100,
            "vat": 23,
            "warehouse": "Drewno",
        }
        first_row = (21, "Sklep", 2, "shop_order", {}, data, None, "new", True, [])
        second_row = (22, "Sklep", 3, "shop_order", {}, data, None, "new", True, [])

        with warehouse_app.app.test_request_context("/import-ogolny/8/confirm"):
            warehouse_app.session["user"] = "admin@example.com"
            with (
                patch.object(
                    warehouse_app, "get_or_create_import_product", return_value=(5, 10)
                ),
                patch.object(warehouse_app, "ensure_shop_accounting_row"),
            ):
                warehouse_app.apply_import_shop_order(cursor, first_row, data, 8, cache)
                warehouse_app.apply_import_shop_order(cursor, second_row, data, 8, cache)

        order_inserts = [
            call for call in cursor.execute.call_args_list
            if "INSERT INTO shop_orders(" in call.args[0]
        ]
        item_inserts = [
            call for call in cursor.execute.call_args_list
            if "INSERT INTO shop_order_items(" in call.args[0]
        ]
        self.assertEqual(len(order_inserts), 1)
        self.assertEqual(len(item_inserts), 2)

    def test_delegated_import_permissions_are_limited_by_role(self):
        with warehouse_app.app.test_request_context("/import-ogolny"):
            warehouse_app.session.update(
                user="warehouse@example.com",
                role="warehouse",
                can_import_warehouse=True,
                can_import_accounting=True,
            )
            self.assertEqual(warehouse_app.general_import_groups(), {"warehouse"})
            warehouse_app.session.update(
                role="accounting",
                can_import_warehouse=True,
                can_import_accounting=True,
            )
            self.assertEqual(warehouse_app.general_import_groups(), {"accounting"})
            warehouse_app.session["role"] = "admin"
            self.assertEqual(
                warehouse_app.general_import_groups(), {"warehouse", "accounting"}
            )

    def test_product_can_be_edited_after_import(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [("Drewno",), (2.0, 1), None]
        connection = MagicMock()
        connection.cursor.return_value = cursor

        with warehouse_app.app.test_request_context(
            "/products/5/edit",
            method="POST",
            data={
                "name": "Deska poprawiona",
                "qty": "8",
                "unit": "m3",
                "warehouse": "Drewno",
                "price_netto": "120",
                "vat": "23",
            },
        ):
            warehouse_app.session["user"] = "admin@example.com"
            warehouse_app.session["role"] = "admin"
            with patch.object(warehouse_app, "db", return_value=connection):
                response = warehouse_app.edit_product(5)

        self.assertEqual(response.status_code, 302)
        connection.commit.assert_called_once()
        self.assertTrue(
            any(
                "UPDATE products SET name=" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    def test_import_history_schema_records_actor_file_counts_and_errors(self):
        source = Path(warehouse_app.__file__).read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS general_imports", source)
        self.assertIn("filename TEXT NOT NULL", source)
        self.assertIn("imported_by TEXT NOT NULL", source)
        self.assertIn("summary JSONB", source)
        self.assertIn("errors JSONB", source)
        self.assertIn("completed_at TIMESTAMPTZ", source)


if __name__ == "__main__":
    unittest.main()
