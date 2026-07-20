import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as warehouse_app


class TrashTests(unittest.TestCase):
    def test_binary_decimal_and_date_values_are_serializable(self):
        encoded = warehouse_app.trash_json_value(b"photo")
        self.assertEqual(warehouse_app.trash_restore_value(encoded).adapted, b"photo")
        decimal = warehouse_app.trash_json_value(Decimal("12.34"))
        self.assertEqual(warehouse_app.trash_restore_value(decimal), Decimal("12.34"))

    def test_permanent_delete_requires_exact_second_confirmation(self):
        with warehouse_app.app.test_request_context(
            "/admin/trash/permanent-delete", method="POST", data={"trash_id": "1", "confirm_text": "tak"}
        ):
            warehouse_app.session.update(user="admin@example.com", role="admin")
            response = warehouse_app.admin_trash_permanent_delete()
        self.assertEqual(response[1], 400)

    def test_non_admin_cannot_move_record_to_trash(self):
        with warehouse_app.app.test_request_context("/admin/trash/product/1", method="POST"):
            warehouse_app.session.update(user="sales@example.com", role="sales")
            response = warehouse_app.admin_trash_single("product", 1)
        self.assertEqual(response[1], 403)

    def test_current_user_cannot_be_moved_to_trash(self):
        cursor = MagicMock()
        cursor.description = [MagicMock(name="id"), MagicMock(name="email")]
        cursor.description[0].name = "id"
        cursor.description[1].name = "email"
        cursor.fetchone.return_value = (1, "admin@example.com")
        with warehouse_app.app.test_request_context("/"):
            warehouse_app.session.update(user="admin@example.com", role="admin")
            with patch.object(warehouse_app, "trash_primary_key", return_value="id"), patch.object(
                warehouse_app, "trash_referencing_keys", return_value=[]
            ):
                with self.assertRaisesRegex(ValueError, "własnego konta"):
                    warehouse_app.move_to_trash(cursor, "user", 1)

    def test_admin_navigation_exposes_tools_and_trash(self):
        source = Path("templates/base.html").read_text(encoding="utf-8")
        self.assertIn("/admin/tools/test-data", source)
        self.assertIn("Narzędzia i Kosz", source)


if __name__ == "__main__":
    unittest.main()
