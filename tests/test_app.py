import copy
import os
import unittest
from unittest.mock import patch

import app as warehouse_app


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


if __name__ == "__main__":
    unittest.main()
