import copy
import gzip
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as warehouse_app
from werkzeug.datastructures import MultiDict
from backup_service import (
    BACKUP_FORMAT,
    BACKUP_TABLES,
    decrypt_backup,
    encrypt_backup,
    parse_backup,
)
from cryptography.fernet import Fernet


class FakeStore:
    def __init__(self):
        self.products = {
            1: {
                "name": "Deska",
                "qty": 0.0,
                "unit": "m3",
                "warehouse": "Drewno",
                "price": 100.0,
                "vat": 23.0,
            }
        }
        self.packages = {}
        self.docs = {}
        self.items = []
        self.next_doc = 1
        self.next_package = 1


class FakeConnection:
    def __init__(self, store):
        self.store = store
        self.snapshot = copy.deepcopy(store.__dict__)
        self.cursor_instance = FakeCursor(store)

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.snapshot = copy.deepcopy(self.store.__dict__)

    def rollback(self):
        self.store.__dict__.clear()
        self.store.__dict__.update(copy.deepcopy(self.snapshot))

    def close(self):
        pass


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.result = None
        self.rowcount = 0

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        self.result = None
        self.rowcount = 0
        if normalized.startswith("select pg_advisory_xact_lock"):
            self.result = (None,)
        elif normalized.startswith("select * from products order by"):
            self.result = [
                (
                    product_id,
                    product["name"],
                    product["qty"],
                    product["unit"],
                    product["warehouse"],
                    product["price"],
                    product["vat"],
                )
                for product_id, product in self.store.products.items()
            ]
        elif normalized.startswith("select name, unit, price_netto, vat from products"):
            product = self.store.products.get(params[0])
            if product:
                self.result = (
                    product["name"], product["unit"], product["price"], product["vat"]
                )
        elif normalized.startswith("select id from products where warehouse="):
            warehouse, name = params
            for product_id, product in self.store.products.items():
                if product["warehouse"] == warehouse and product["name"].lower() == name.lower():
                    self.result = (product_id,)
                    break
        elif normalized.startswith("select 1 from packages"):
            warehouse, number = params
            self.result = next(
                (
                    (1,)
                    for package in self.store.packages.values()
                    if package["warehouse"] == warehouse
                    and package["number"].lower() == number.lower()
                    and package["status"] == "active"
                ),
                None,
            )
        elif normalized.startswith("insert into issue_docs"):
            doc_id = self.store.next_doc
            self.store.next_doc += 1
            movement_type = "PZ" if "'pz'" in normalized else params[-1]
            self.store.docs[doc_id] = {"type": movement_type}
            self.result = (doc_id,)
            self.rowcount = 1
        elif normalized.startswith("update issue_docs set doc_number"):
            self.store.docs[params[1]]["number"] = params[0]
            self.rowcount = 1
        elif normalized.startswith("update products set qty=qty+"):
            if len(params) == 4:
                qty, unit, price, product_id = params
                self.store.products[product_id]["unit"] = unit
            else:
                qty, price, product_id = params
            self.store.products[product_id]["qty"] += qty
            self.store.products[product_id]["price"] = price
            self.rowcount = 1
        elif normalized.startswith("insert into packages"):
            package_id = self.store.next_package
            self.store.next_package += 1
            product_id, number, qty, warehouse, initial_qty = params
            self.store.packages[package_id] = {
                "product_id": product_id,
                "number": number,
                "qty": qty,
                "warehouse": warehouse,
                "initial_qty": initial_qty,
                "status": "active",
            }
            self.result = (package_id,)
            self.rowcount = 1
        elif normalized.startswith("insert into issue_items"):
            self.store.items.append(params)
            self.rowcount = 1
        elif normalized.startswith("insert into action_logs"):
            self.rowcount = 1
        elif normalized.startswith("select id from products where id="):
            product_id, warehouse = params
            product = self.store.products.get(product_id)
            if product and product["warehouse"] == warehouse:
                self.result = (product_id,)
        elif normalized.startswith("select number, qty from packages"):
            package_id, product_id, warehouse = params
            package = self.store.packages.get(package_id)
            if (
                package
                and package["product_id"] == product_id
                and package["warehouse"] == warehouse
                and package["status"] == "active"
            ):
                self.result = (package["number"], package["qty"])
        elif normalized.startswith("update packages set qty=qty-"):
            qty, _, _, package_id, minimum = params
            package = self.store.packages[package_id]
            if package["qty"] >= minimum:
                package["qty"] -= qty
                package["status"] = "issued" if package["qty"] <= 0 else "active"
                self.rowcount = 1
        elif normalized.startswith("update products set qty=qty-"):
            qty, product_id, warehouse, minimum = params
            product = self.store.products[product_id]
            if product["warehouse"] == warehouse and product["qty"] >= minimum:
                product["qty"] -= qty
                self.rowcount = 1
        else:
            raise AssertionError(f"Unhandled SQL in test: {normalized}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return list(self.result or [])


class InventoryFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def post(self, path, data):
        with warehouse_app.app.test_request_context(path, method="POST", data=data):
            with patch.object(warehouse_app, "db", side_effect=lambda: FakeConnection(self.store)):
                return (
                    warehouse_app.create_receipt()
                    if path == "/receive_doc"
                    else warehouse_app.create_issue()
                )

    def test_full_package_flow_and_overissue_guard(self):
        receipt = self.post(
            "/receive_doc",
            {
                "date": "2026-07-04",
                "kontrahent": "Dostawca",
                "product_id": "1",
                "warehouse": "Drewno",
                "package_number": "PACZKA-001",
                "qty": "10",
                "price_netto": "100",
                "price_brutto": "123",
            },
        )
        self.assertEqual(receipt.status_code, 302)
        self.assertEqual(self.store.products[1]["qty"], 10)
        self.assertEqual(self.store.packages[1]["qty"], 10)

        partial = self.post(
            "/issue_doc",
            {
                "date": "2026-07-04",
                "kontrahent": "Plac",
                "product_id": "1",
                "warehouse": "Drewno",
                "package_id": "1",
                "qty": "4",
                "price_netto": "100",
                "price_brutto": "123",
            },
        )
        self.assertEqual(partial.status_code, 302)
        self.assertEqual(self.store.products[1]["qty"], 6)
        self.assertEqual(self.store.packages[1]["qty"], 6)

        too_much = self.post(
            "/issue_doc",
            {
                "date": "2026-07-04",
                "kontrahent": "Plac",
                "product_id": "1",
                "warehouse": "Drewno",
                "package_id": "1",
                "qty": "7",
            },
        )
        self.assertEqual(too_much[1], 400)
        self.assertEqual(self.store.products[1]["qty"], 6)
        self.assertEqual(self.store.packages[1]["qty"], 6)

        final = self.post(
            "/issue_doc",
            {
                "date": "2026-07-04",
                "kontrahent": "Plac",
                "product_id": "1",
                "warehouse": "Drewno",
                "package_id": "1",
                "qty": "6",
            },
        )
        self.assertEqual(final.status_code, 302)
        self.assertEqual(self.store.products[1]["qty"], 0)
        self.assertEqual(self.store.packages[1]["qty"], 0)
        self.assertEqual(self.store.packages[1]["status"], "issued")

    def test_receipt_single_item_with_package_number(self):
        response = self.post(
            "/receive_doc",
            {
                "date": "2026-07-06",
                "kontrahent": "Dostawca",
                "product_name": "Deska",
                "product_id": "1",
                "warehouse": "Drewno",
                "unit": "m3",
                "has_package_number": "1",
                "package_number": "PZ-PAK-1",
                "qty": "3",
                "price_netto": "100",
                "price_brutto": "123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.products[1]["qty"], 3)
        self.assertEqual(self.store.products[1]["unit"], "m3")
        self.assertEqual(self.store.products[1]["price"], 100)
        self.assertEqual(self.store.packages[1]["number"], "PZ-PAK-1")

    def test_receipt_single_item_without_package_number(self):
        response = self.post(
            "/receive_doc",
            {
                "date": "2026-07-06",
                "kontrahent": "Dostawca",
                "product_name": "Deska",
                "product_id": "1",
                "warehouse": "Drewno",
                "unit": "m3",
                "has_package_number": "0",
                "package_number": "",
                "qty": "2",
                "price_netto": "100",
                "price_brutto": "123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.products[1]["qty"], 2)
        self.assertEqual(self.store.packages, {})
        self.assertIsNone(self.store.items[0][5])

    def test_receipt_multiple_items(self):
        response = self.post(
            "/receive_doc",
            MultiDict(
                [
                    ("date", "2026-07-06"),
                    ("kontrahent", "Dostawca"),
                    ("product_name", "Deska"),
                    ("product_name", "Deska"),
                    ("product_id", "1"),
                    ("product_id", "1"),
                    ("warehouse", "Drewno"),
                    ("warehouse", "Drewno"),
                    ("unit", "m3"),
                    ("unit", "m3"),
                    ("has_package_number", "1"),
                    ("has_package_number", "0"),
                    ("package_number", "PZ-MULTI-1"),
                    ("package_number", ""),
                    ("qty", "4"),
                    ("qty", "6"),
                    ("price_netto", "100"),
                    ("price_netto", "100"),
                    ("price_brutto", "123"),
                    ("price_brutto", "123"),
                ]
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.products[1]["qty"], 10)
        self.assertEqual(len(self.store.items), 2)
        self.assertEqual(len(self.store.packages), 1)

    def test_receipt_without_items_returns_form_with_message(self):
        result = self.post(
            "/receive_doc",
            {
                "date": "2026-07-06",
                "kontrahent": "Dostawca",
            },
        )

        response = warehouse_app.app.make_response(result)
        self.assertEqual(response.status_code, 400)
        body = response.get_data(as_text=True)
        self.assertIn("Dodaj co najmniej jedną kompletną pozycję dokumentu.", body)
        self.assertIn('id="receiptForm"', body)
        self.assertIn("novalidate", body)
        self.assertIn('id="saveReceipt"', body)
        self.assertIn('aria-disabled="true"', body)
        self.assertNotIn('id="saveReceipt" type="submit" disabled', body)
        self.assertIn('id="itemsSummary"', body)
        self.assertIn("function receiptValidationErrors()", body)
        self.assertIn("Nie można zapisać dokumentu:", body)
        for field_name in (
            "product_name", "product_id", "warehouse", "unit", "has_package_number",
            "package_number", "qty", "price_netto", "price_brutto",
        ):
            self.assertIn(f'name="{field_name}"', body)
        self.assertIn("function updateSaveState()", body)

    def test_receipt_checked_package_without_number_returns_form_with_message(self):
        result = self.post(
            "/receive_doc",
            {
                "date": "2026-07-06",
                "kontrahent": "Dostawca",
                "product_name": "Deska",
                "product_id": "1",
                "warehouse": "Drewno",
                "unit": "m3",
                "has_package_number": "1",
                "package_number": "",
                "qty": "3",
                "price_netto": "100",
                "price_brutto": "123",
            },
        )

        response = warehouse_app.app.make_response(result)
        self.assertEqual(response.status_code, 400)
        body = response.get_data(as_text=True)
        self.assertIn("wpisz numer paczki", body)
        self.assertIn('value="Deska"', body)
        self.assertIn("checked", body)

    def test_non_finite_and_negative_quantities_are_rejected(self):
        for value in ("0", "-1", "nan", "inf", ""):
            with self.assertRaises(ValueError):
                warehouse_app.parse_positive_number(value)

    def test_rw_issue_is_recorded_as_outgoing_document(self):
        self.post(
            "/receive_doc",
            {
                "date": "2026-07-04",
                "kontrahent": "Dostawca",
                "product_id": "1",
                "warehouse": "Drewno",
                "package_number": "RW-TEST",
                "qty": "2",
            },
        )
        result = self.post(
            "/issue_doc",
            {
                "date": "2026-07-04",
                "kontrahent": "Produkcja",
                "movement_type": "RW",
                "product_id": "1",
                "warehouse": "Drewno",
                "package_id": "1",
                "qty": "1",
            },
        )
        self.assertEqual(result.status_code, 302)
        self.assertEqual(self.store.docs[2]["type"], "RW")
        self.assertEqual(self.store.products[1]["qty"], 1)

    def test_unknown_warehouse_is_rejected(self):
        with warehouse_app.app.test_request_context(
            "/receive_doc",
            method="POST",
            data={
                "product_id": "1",
                "warehouse": "Nieistniejący",
                "qty": "1",
            },
        ):
            with self.assertRaises(ValueError):
                warehouse_app.collect_document_items()

    def test_private_api_requires_firebase_session(self):
        client = warehouse_app.app.test_client()
        response = client.get("/api/packages/lookup?number=TEST")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Wymagane logowanie.")

    def test_login_page_is_public_without_firebase_session(self):
        client = warehouse_app.app.test_client()
        response = client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Logowanie", response.get_data(as_text=True))
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_production_http_is_redirected_to_https(self):
        client = warehouse_app.app.test_client()
        with patch.object(warehouse_app, "IS_PRODUCTION", True):
            response = client.get("/health", base_url="http://pmagazyn.pl")
        self.assertEqual(response.status_code, 308)
        self.assertTrue(response.headers["Location"].startswith("https://"))

    def test_firebase_web_config_has_no_repository_defaults(self):
        if not os.environ.get("FIREBASE_API_KEY"):
            self.assertEqual(warehouse_app.FIREBASE_CONFIG["apiKey"], "")
        if not os.environ.get("FIREBASE_PROJECT_ID"):
            self.assertEqual(warehouse_app.FIREBASE_CONFIG["projectId"], "")

    def test_encrypted_backup_round_trip_and_validation(self):
        tables = {
            name: {"columns": ["id"], "rows": []}
            for name in ("users", "products", "packages", "issue_docs", "issue_items")
        }
        raw = json.dumps(
            {"format": BACKUP_FORMAT, "created_at": "2026-07-04T00:00:00Z", "tables": tables}
        ).encode("utf-8")
        compressed = gzip.compress(raw)
        key = Fernet.generate_key().decode("ascii")
        encrypted = encrypt_backup(compressed, key)
        self.assertNotIn(b"products", encrypted)
        decrypted = decrypt_backup(encrypted, key)
        parsed = parse_backup(decrypted)
        self.assertEqual(parsed["format"], BACKUP_FORMAT)
        with self.assertRaises(ValueError):
            decrypt_backup(encrypted, Fernet.generate_key().decode("ascii"))

    def test_firebase_login_sets_verified_cookie_without_storing_password(self):
        client = warehouse_app.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["_csrf_token"] = "csrf-test"

        cursor = MagicMock()
        cursor.fetchone.return_value = ("employee", "active")
        connection = MagicMock()
        connection.cursor.return_value = cursor
        firebase_user = SimpleNamespace(disabled=False, display_name="Jan Testowy")

        with (
            patch.object(warehouse_app, "FIREBASE_ADMIN_READY", True),
            patch.object(warehouse_app, "ensure_db_initialized"),
            patch.object(warehouse_app, "db", return_value=connection),
            patch.object(
                warehouse_app.auth,
                "verify_id_token",
                return_value={"uid": "firebase-uid", "email": "jan@example.com"},
            ),
            patch.object(warehouse_app.auth, "get_user", return_value=firebase_user),
            patch.object(
                warehouse_app.auth,
                "create_session_cookie",
                return_value="verified-firebase-cookie",
            ),
        ):
            response = client.post(
                "/auth/session",
                json={"idToken": "firebase-id-token"},
                headers={"X-CSRF-Token": "csrf-test"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("firebase_session=verified-firebase-cookie", response.headers["Set-Cookie"])
        executed_sql = " ".join(
            call.args[0] for call in cursor.execute.call_args_list if call.args
        ).lower()
        self.assertNotIn(" password=", executed_sql)
        self.assertNotIn(" password,", executed_sql)
        self.assertNotIn("insert into users(password", executed_sql)

    def test_render_external_database_url_prefers_internal_network(self):
        external = (
            "postgresql://user:secret@"
            "dpg-example-a.frankfurt-postgres.render.com:5432/magazyn?sslmode=require"
        )
        fake_pool = object()
        warehouse_app.DB_POOL = None
        with patch.dict(os.environ, {"DATABASE_URL": external, "RENDER": "true"}, clear=False):
            with patch.object(
                warehouse_app,
                "ThreadedConnectionPool",
                return_value=fake_pool,
            ) as pool_factory:
                warehouse_app.init_db_pool()
        self.assertIs(warehouse_app.DB_POOL, fake_pool)
        internal_dsn = pool_factory.call_args.kwargs["dsn"]
        self.assertIn("@dpg-example-a:5432/magazyn", internal_dsn)
        self.assertNotIn("sslmode", internal_dsn)
        warehouse_app.DB_POOL = None

    def test_shop_document_payload_uses_correct_quantity_prices_and_shipping(self):
        order = (
            1, "SK/1", "2026-07-04", "Klient", "Adres", "123", "a@example.com",
            15.0, "Przelew", "Opłacone", "Nowe zamówienie", "FV/1", "", "Uwagi", "NIP",
        )
        items = [(1, 1, "Deska", 2.0, 100.0, 123.0, 23.0)]
        payload = warehouse_app.create_shop_document_payload(order, items)
        self.assertEqual(payload["items"][0]["name"], "Deska")
        self.assertEqual(payload["items"][0]["qty"], 2.0)
        self.assertEqual(payload["total_net"], 200.0)
        self.assertEqual(payload["total_gross"], 261.0)
        self.assertEqual(payload["shipping"], 15.0)

    def test_backup_includes_all_relational_module_tables(self):
        expected = {
            "shop_order_history",
            "shop_order_stage_history",
            "shop_notifications",
            "shop_sales_documents",
            "shop_accounting",
            "issue_doc_photos",
            "issue_doc_history",
            "issue_imports",
            "issue_import_rows",
            "issue_import_effects",
            "app_settings",
        }
        self.assertTrue(expected.issubset(BACKUP_TABLES))

    def test_migration_defines_only_one_canonical_shop_schema(self):
        source = Path(warehouse_app.__file__).read_text(encoding="utf-8").lower()
        self.assertEqual(source.count("create table if not exists shop_orders("), 1)
        self.assertIn("add column if not exists order_number", source)
        self.assertIn("uq_shop_orders_order_number", source)

    def test_main_templates_render_with_empty_database(self):
        class EmptyCursor:
            def __init__(self):
                self.result = None

            def execute(self, sql, params=()):
                normalized = " ".join(str(sql).lower().split())
                if normalized.startswith("select count(*), coalesce(sum(i.qty*i.price_brutto)"):
                    self.result = [(0, 0, 0, 0, 0)]
                elif "select coalesce(sum(a.amount_due),0)" in normalized:
                    self.result = [(0, 0, 0, 0, 0, 0, 0, 0)]
                elif normalized.startswith("select count(*), coalesce(sum(qty)"):
                    self.result = [(0, 0)]
                else:
                    self.result = []

            def fetchall(self):
                return list(self.result or [])

            def fetchone(self):
                return (self.result or [None])[0]

        class EmptyConnection:
            def __init__(self):
                self.cursor_instance = EmptyCursor()

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        cases = [
            ("/", warehouse_app.home),
            ("/dashboard", warehouse_app.dashboard_page),
            ("/magazyn/Wszystko", lambda: warehouse_app.magazyn("Wszystko")),
            ("/przyjecie", warehouse_app.przyjecie),
            ("/wydanie", warehouse_app.wydanie),
            ("/historia", warehouse_app.historia),
            ("/users", warehouse_app.users),
            ("/sklep", warehouse_app.shop_orders),
            ("/ksiegowosc", warehouse_app.accounting_dashboard),
        ]
        for path, view in cases:
            with self.subTest(path=path):
                with warehouse_app.app.test_request_context(path):
                    warehouse_app.session["user"] = "admin@example.com"
                    warehouse_app.session["role"] = "admin"
                    with patch.object(
                        warehouse_app,
                        "db",
                        side_effect=lambda: EmptyConnection(),
                    ):
                        response = warehouse_app.app.make_response(view())
                self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
