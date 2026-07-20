import unittest
from decimal import Decimal
from pathlib import Path

import app as warehouse_app


class CalculatorTests(unittest.TestCase):
    def calculate(self, direction, **values):
        payload = {"direction": direction, "precision": "none", **values}
        return warehouse_app.calculate_conversion(payload)

    def test_dimension_parser_accepts_all_required_separators_and_orders(self):
        for text in ("140 × 22 × 3000 mm", "140x22x3000", "140/22/3000"):
            parsed = warehouse_app.parse_calculator_dimensions(text)
            self.assertEqual(parsed["width_mm"], Decimal("140"))
            self.assertEqual(parsed["thickness_mm"], Decimal("22"))
            self.assertEqual(parsed["length_value"], Decimal("3000"))
        reversed_values = warehouse_app.parse_calculator_dimensions("22x140x3000", "thickness_width_length")
        self.assertEqual(reversed_values["width_mm"], Decimal("140"))
        self.assertEqual(reversed_values["thickness_mm"], Decimal("22"))

    def test_examples_for_area_and_volume(self):
        area = self.calculate("mb_m2", linear_meters="100", width_mm="140")
        volume = self.calculate("mb_m3", linear_meters="100", width_mm="140", thickness_mm="22")
        self.assertEqual(area["result_exact"], "14")
        self.assertEqual(volume["result_exact"], "0.308")

    def test_all_twelve_directions(self):
        common = dict(width_mm="100", thickness_mm="20", length_value="2", length_unit="m")
        cases = {
            "mb_m2": (dict(linear_meters="10", **common), "1"),
            "m2_mb": (dict(square_meters="1", **common), "10"),
            "mb_m3": (dict(linear_meters="10", **common), "0.02"),
            "m3_mb": (dict(cubic_meters="0.02", **common), "10"),
            "m2_m3": (dict(square_meters="1", **common), "0.02"),
            "m3_m2": (dict(cubic_meters="0.02", **common), "1"),
            "pcs_mb": (dict(pieces="5", **common), "10"),
            "mb_pcs": (dict(linear_meters="10", **common), "5"),
            "pcs_m2": (dict(pieces="5", **common), "1"),
            "m2_pcs": (dict(square_meters="1", **common), "5"),
            "pcs_m3": (dict(pieces="5", **common), "0.02"),
            "m3_pcs": (dict(cubic_meters="0.02", **common), "5"),
        }
        for direction, (inputs, expected) in cases.items():
            with self.subTest(direction=direction):
                self.assertEqual(self.calculate(direction, **inputs)["result_exact"], expected)

    def test_piece_result_contains_floor_and_order_quantity(self):
        result = self.calculate("mb_pcs", linear_meters="100", length_value="5.43", length_unit="m")
        self.assertEqual(result["pieces_floor"], 18)
        self.assertEqual(result["pieces_ceil"], 19)

    def test_zero_negative_and_missing_values_are_rejected(self):
        for width in ("0", "-1", ""):
            with self.subTest(width=width), self.assertRaises(ValueError):
                self.calculate("mb_m2", linear_meters="10", width_mm=width)

    def test_navigation_and_responsive_panel_are_present(self):
        base = Path("templates/base.html").read_text(encoding="utf-8")
        panel = Path("templates/calculator.html").read_text(encoding="utf-8")
        self.assertIn('/przelicznik', base)
        self.assertIn('PRZELICZNIK', panel)
        self.assertIn('@media(max-width:760px)', panel)


if __name__ == "__main__":
    unittest.main()
