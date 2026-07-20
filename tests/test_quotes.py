import unittest
from decimal import Decimal
from unittest.mock import patch

import app as warehouse_app


class CommercialQuoteTests(unittest.TestCase):
    def test_calculation_combines_percentage_and_amount_discount(self):
        result = warehouse_app.calculate_quote_item("2", "100", "10", "5", "23")
        self.assertEqual(result["net_value"], Decimal("175.00"))
        self.assertEqual(result["vat_value"], Decimal("40.25"))
        self.assertEqual(result["gross_value"], Decimal("215.25"))

    def test_discount_cannot_exceed_item_value(self):
        with self.assertRaisesRegex(ValueError, "Rabat"):
            warehouse_app.calculate_quote_item("1", "10", "0", "11", "23")

    def test_non_sales_role_is_denied_by_backend(self):
        with warehouse_app.app.test_request_context("/oferty"):
            warehouse_app.session["user"] = "warehouse@example.com"
            warehouse_app.session["role"] = "warehouse"
            response = warehouse_app.quotes_page()
        self.assertEqual(response[1], 403)

    def test_pdf_supports_polish_text_and_multiple_rows(self):
        quote = (
            1,"OF/2026/07/001",warehouse_app.datetime(2026,7,20).date(),
            warehouse_app.datetime(2026,8,3).date(),"7 dni","robocza",
            "Żółć Sp. z o.o.","ul. Zażółć 1, Kraków","1234567890","500600700",
            "klient@example.com",None,"Przelew 7 dni","Dostawa z wniesieniem",
            "anna@example.com","Anna Żak","Handlowiec","500100200",
            Decimal("100.00"),Decimal("23.00"),Decimal("123.00"),
        )
        item = (1,1,1,None,"towar","Długa nazwa produktu z polskimi znakami: ąęłńóśźż",1,Decimal("2"),"szt.",Decimal("50"),Decimal("0"),Decimal("0"),Decimal("23"),Decimal("100"),Decimal("23"),Decimal("123"))
        # Match the real SELECT * column order.
        item = (1,1,1,None,"towar",item[5],Decimal("2"),"szt.",Decimal("50"),Decimal("0"),Decimal("0"),Decimal("23"),Decimal("100"),Decimal("23"),Decimal("123"))
        pdf = warehouse_app.commercial_quote_pdf(quote, [item] * 35)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 5000)


if __name__ == "__main__":
    unittest.main()
