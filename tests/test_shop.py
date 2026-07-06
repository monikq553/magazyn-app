import io
import zipfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from docx import Document
from werkzeug.datastructures import MultiDict

import app as warehouse_app


def sample_order():
    return (
        42,
        "SK/TEST/42",
        date(2026, 7, 6),
        "Klient Testowy",
        "ul. Testowa 1, Poznań",
        "123456789",
        "klient@example.com",
        25.0,
        "Przelew",
        "Oczekuje na płatność",
        "Towar zarezerwowany",
        "",
        "",
        "Dostawa testowa",
        "1234567890",
    )


def sample_items():
    return [
        (1, 1, "Deska dębowa", 2.0, 100.0, 123.0, 23.0),
        (2, 2, "Olej do drewna", 3.0, 20.0, 24.6, 23.0),
    ]


class ShopModuleTests(unittest.TestCase):
    def test_order_creation_with_multiple_products_commits_without_document_generation(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            (42,),
            (1, "Deska dębowa", 10.0, "Drewno", 100.0, 23.0),
            (0.0,),
            (2, "Olej do drewna", 20.0, "Farby", 20.0, 23.0),
            (0.0,),
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        form = MultiDict(
            [
                ("order_number", "SK/TEST/42"),
                ("date", "2026-07-06"),
                ("customer_name", "Klient Testowy"),
                ("delivery_address", "ul. Testowa 1"),
                ("email", "klient@example.com"),
                ("shipping_cost", "25"),
                ("payment_method", "Przelew"),
                ("payment_status", "Oczekuje na płatność"),
                ("product_id", "1"),
                ("qty", "2"),
                ("product_id", "2"),
                ("qty", "3"),
            ]
        )

        with warehouse_app.app.test_request_context(
            "/sklep/orders", method="POST", data=form
        ):
            warehouse_app.session["user"] = "admin@example.com"
            warehouse_app.session["role"] = "admin"
            with patch.object(warehouse_app, "db", return_value=connection):
                response = warehouse_app.shop_create_order()

        self.assertEqual(response.status_code, 302)
        self.assertIn("/sklep/orders/42?created=1", response.location)
        connection.commit.assert_called_once()
        item_inserts = [
            call for call in cursor.execute.call_args_list
            if "INSERT INTO shop_order_items" in call.args[0]
        ]
        self.assertEqual(len(item_inserts), 2)
        self.assertFalse(
            any(
                "INSERT INTO shop_sales_documents" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    def test_incomplete_order_returns_specific_form_message(self):
        form = MultiDict(
            [
                ("customer_name", "Klient"),
                ("delivery_address", "Adres"),
                ("product_id", "1"),
                ("qty", ""),
            ]
        )
        with warehouse_app.app.test_request_context(
            "/sklep/orders", method="POST", data=form
        ):
            warehouse_app.session["user"] = "admin@example.com"
            warehouse_app.session["role"] = "admin"
            with patch.object(
                warehouse_app,
                "render_shop_dashboard",
                side_effect=lambda message, status, data: (message, status),
            ):
                response = warehouse_app.shop_create_order()

        self.assertEqual(response[1], 400)
        self.assertIn("Pozycja 1: podaj ilość", response[0])

    def test_pdf_and_docx_contain_logo_customer_products_and_totals(self):
        payload = warehouse_app.create_shop_document_payload(
            sample_order(), sample_items()
        )
        docx_bytes = warehouse_app.simple_docx_bytes(payload)
        pdf_bytes = warehouse_app.shop_pdf_bytes(payload)

        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
            media = [
                name for name in archive.namelist()
                if name.startswith("word/media/")
            ]
            self.assertTrue(media)
            self.assertGreater(len(archive.read(media[0])), 100)

        document = Document(io.BytesIO(docx_bytes))
        text = "\n".join(
            [paragraph.text for paragraph in document.paragraphs]
            + [
                cell.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
            ]
        )
        self.assertIn("Klient Testowy", text)
        self.assertIn("Deska dębowa", text)
        self.assertIn("RAZEM BRUTTO", text)
        self.assertIn("344.80 zł", text)

        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertIn(b"/Subtype /Image", pdf_bytes)
        self.assertGreater(len(pdf_bytes), 5000)

    def test_generated_document_is_upserted_and_saved_at_order(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [sample_order(), (77, "DS/SK/TEST/42")]
        cursor.fetchall.return_value = sample_items()

        document, payload = warehouse_app.generate_shop_sales_document(
            cursor, 42, "admin@example.com"
        )

        self.assertEqual(document[0], 77)
        self.assertEqual(payload["buyer"], "Klient Testowy")
        upserts = [
            call for call in cursor.execute.call_args_list
            if "INSERT INTO shop_sales_documents" in call.args[0]
        ]
        self.assertEqual(len(upserts), 1)
        self.assertIn("ON CONFLICT (order_id) DO UPDATE", upserts[0].args[0])
        params = upserts[0].args[1]
        self.assertGreater(len(params[3].adapted), 5000)
        self.assertGreater(len(params[4].adapted), 5000)

    def test_generate_document_action_commits_and_redirects_to_order(self):
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value = cursor
        payload = warehouse_app.create_shop_document_payload(
            sample_order(), sample_items()
        )
        with warehouse_app.app.test_request_context(
            "/sklep/orders/42/document/generate", method="POST"
        ):
            warehouse_app.session["user"] = "admin@example.com"
            warehouse_app.session["role"] = "admin"
            with (
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(
                    warehouse_app,
                    "generate_shop_sales_document",
                    return_value=((77, "DS/SK/TEST/42"), payload),
                ),
            ):
                response = warehouse_app.shop_generate_document(42)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/sklep/orders/42?document=generated", response.location)
        connection.commit.assert_called_once()
        self.assertTrue(
            any(
                "INSERT INTO shop_notifications" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    def test_pdf_and_docx_download_endpoints(self):
        for fmt, content, mimetype in (
            ("pdf", b"%PDF-test", "application/pdf"),
            (
                "docx",
                b"PK-test",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ):
            with self.subTest(fmt=fmt):
                cursor = MagicMock()
                cursor.fetchone.return_value = ("DS/SK/TEST/42", content)
                connection = MagicMock()
                connection.cursor.return_value = cursor
                with warehouse_app.app.test_request_context(
                    f"/sklep/documents/77/{fmt}"
                ):
                    warehouse_app.session["user"] = "admin@example.com"
                    warehouse_app.session["role"] = "admin"
                    with patch.object(
                        warehouse_app, "db", return_value=connection
                    ):
                        response = warehouse_app.shop_download_document(77, fmt)
                self.assertEqual(response.get_data(), content)
                self.assertEqual(response.mimetype, mimetype)
                self.assertIn(f".{fmt}", response.headers["Content-Disposition"])

    def test_migration_adds_columns_missing_from_legacy_shop_database(self):
        source = Path(warehouse_app.__file__).read_text(encoding="utf-8")
        migration_section = source[source.index("CREATE TABLE IF NOT EXISTS shop_orders("):]
        for column in ("payment_status", "tracking_number", "notes"):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", migration_section)
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_sales_documents_order",
            migration_section,
        )


if __name__ == "__main__":
    unittest.main()
