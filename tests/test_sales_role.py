import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as warehouse_app
from werkzeug.datastructures import MultiDict


class SalesRoleTests(unittest.TestCase):
    def request_context(self, path="/"):
        return warehouse_app.app.test_request_context(path)

    def test_sales_role_is_available_with_read_only_inventory_and_shop(self):
        self.assertIn("sales", warehouse_app.ROLES)
        self.assertEqual(warehouse_app.ROLE_LABELS["sales"], "Handlowiec")
        permissions = warehouse_app.ROLE_PERMISSIONS["sales"]
        self.assertTrue({"dashboard", "inventory", "shop"} <= permissions)
        self.assertNotIn("inventory_manage", permissions)
        self.assertFalse(
            permissions & {"receive", "issue", "users", "backups", "accounting"}
        )

    def test_firebase_login_preserves_sales_role(self):
        client = warehouse_app.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["_csrf_token"] = "csrf-sales"
        cursor = MagicMock()
        cursor.fetchone.return_value = ("sales", "active", False, False)
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with (
            patch.object(warehouse_app, "FIREBASE_ADMIN_READY", True),
            patch.object(warehouse_app, "ensure_db_initialized"),
            patch.object(warehouse_app, "db", return_value=connection),
            patch.object(
                warehouse_app.auth,
                "verify_id_token",
                return_value={"uid": "sales-uid", "email": "sales@example.com"},
            ),
            patch.object(
                warehouse_app.auth,
                "get_user",
                return_value=SimpleNamespace(disabled=False, display_name="Anna"),
            ),
            patch.object(
                warehouse_app.auth,
                "create_session_cookie",
                return_value="sales-cookie",
            ),
        ):
            response = client.post(
                "/auth/session",
                json={"idToken": "token"},
                headers={"X-CSRF-Token": "csrf-sales"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["role"], "sales")

    def test_salesperson_can_view_inventory_but_cannot_edit_product_or_package(self):
        with self.request_context():
            warehouse_app.session["user"] = "sales@example.com"
            warehouse_app.session["role"] = "sales"
            self.assertTrue(warehouse_app.current_user_can("inventory"))
            self.assertFalse(warehouse_app.current_user_can("inventory_manage"))
            product_response = warehouse_app.edit_product(1)
            package_response = warehouse_app.edit_package(1)
        self.assertEqual(product_response[1], 403)
        self.assertEqual(package_response[1], 403)

    def test_salesperson_cannot_receive_issue_or_access_admin_functions(self):
        with self.request_context():
            warehouse_app.session["role"] = "sales"
            for permission in ("receive", "issue", "users", "backups"):
                with self.subTest(permission=permission):
                    self.assertFalse(warehouse_app.current_user_can(permission))

    def test_salesperson_can_create_and_view_shop_orders(self):
        with self.request_context():
            warehouse_app.session["role"] = "sales"
            self.assertTrue(warehouse_app.can_shop("create"))
            self.assertTrue(warehouse_app.can_shop("view"))
            self.assertTrue(warehouse_app.can_shop("sales"))
            self.assertFalse(warehouse_app.can_shop("warehouse"))
            self.assertFalse(warehouse_app.can_shop("accounting"))

    def test_salesperson_created_order_is_automatically_assigned_to_them(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            (42,),
            (1, "Deska", 10.0, "Drewno", 100.0, 23.0),
            (0.0,),
            (0.0,),
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        form = MultiDict(
            [
                ("order_number", "SK/SALES/42"),
                ("date", "2026-07-06"),
                ("customer_name", "Klient"),
                ("delivery_address", "Adres"),
                ("shipping_cost", "0"),
                ("payment_status", "Oczekuje na płatność"),
                ("product_id", "1"),
                ("qty", "2"),
            ]
        )
        with warehouse_app.app.test_request_context(
            "/sklep/orders", method="POST", data=form
        ):
            warehouse_app.session["user"] = "sales@example.com"
            warehouse_app.session["role"] = "sales"
            with (
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(warehouse_app, "update_shop_stage"),
                patch.object(
                    warehouse_app, "assign_order_salesperson"
                ) as assign_salesperson,
            ):
                response = warehouse_app.shop_create_order()
        self.assertEqual(response.status_code, 302)
        assign_salesperson.assert_called_once_with(
            cursor, 42, "sales@example.com", "sales@example.com"
        )

    def test_salesperson_cannot_edit_someone_elses_order(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        connection = MagicMock()
        connection.cursor.return_value = cursor
        form = MultiDict(
            [
                ("customer_name", "Klient"),
                ("delivery_address", "Adres"),
                ("product_id", "1"),
                ("qty", "1"),
            ]
        )
        with warehouse_app.app.test_request_context(
            "/sklep/orders/42/edit", method="POST", data=form
        ):
            warehouse_app.session["user"] = "sales@example.com"
            warehouse_app.session["role"] = "sales"
            with patch.object(warehouse_app, "db", return_value=connection):
                response = warehouse_app.shop_edit_order(42)
        self.assertEqual(response[1], 403)
        connection.commit.assert_not_called()

    def test_salesperson_is_limited_to_assigned_orders(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(1,), None]
        with self.request_context():
            warehouse_app.session["role"] = "sales"
            warehouse_app.session["user"] = "sales@example.com"
            self.assertTrue(warehouse_app.sales_can_access_order(cursor, 10))
            self.assertFalse(warehouse_app.sales_can_access_order(cursor, 11))
        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(all("salesperson_email" in query for query in queries))

    def test_assigning_salesperson_records_actor_and_history(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ("old@example.com",),
            ("Anna Handlowa", "anna@example.com"),
        ]
        with self.request_context():
            warehouse_app.session["user"] = "admin@example.com"
            changed = warehouse_app.assign_order_salesperson(
                cursor, 42, "anna@example.com", "admin@example.com"
            )
        self.assertTrue(changed)
        history_insert = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO shop_order_salesperson_history" in call.args[0]
        )
        self.assertEqual(
            history_insert.args[1],
            (42, "old@example.com", "anna@example.com", "admin@example.com"),
        )

    def test_targeted_notification_uses_assigned_salesperson(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("sales@example.com",)
        warehouse_app.notify_order_salesperson(
            cursor, 42, "invoice_issued", "Faktura wystawiona."
        )
        insert = cursor.execute.call_args_list[-1]
        self.assertIn("recipient_email", insert.args[0])
        self.assertEqual(
            insert.args[1],
            (42, "invoice_issued", "Faktura wystawiona.", "sales@example.com"),
        )

    def test_sales_report_is_scoped_to_logged_in_salesperson(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("Anna Handlowa", 3, 1200.0, 1, 2, 0)
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with warehouse_app.app.test_request_context("/handlowiec/raport"):
            warehouse_app.session["user"] = "anna@example.com"
            warehouse_app.session["role"] = "sales"
            with (
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(
                    warehouse_app,
                    "render_template",
                    side_effect=lambda _name, **kwargs: kwargs,
                ),
            ):
                response = warehouse_app.salesperson_report()
        self.assertEqual(response["rows"][0][1], 3)
        self.assertIn("salesperson_email", cursor.execute.call_args.args[0])
        self.assertEqual(cursor.execute.call_args.args[1], ("anna@example.com",))

    def test_user_form_and_navigation_expose_sales_role(self):
        users_template = (
            Path(warehouse_app.__file__).parent
            / "templates"
            / "users.html"
        ).read_text(encoding="utf-8")
        base_template = (
            Path(warehouse_app.__file__).parent
            / "templates"
            / "base.html"
        ).read_text(encoding="utf-8")
        self.assertIn('value="sales"', users_template)
        self.assertIn("/handlowiec/raport", base_template)


if __name__ == "__main__":
    unittest.main()
