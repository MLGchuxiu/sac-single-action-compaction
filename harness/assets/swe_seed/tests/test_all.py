"""Eight regression tests, one per seeded defect."""

import unittest

from inventory.cli import summarize
from inventory.orders import can_ship, total
from inventory.pricing import apply_tax, discount_rate, tier_for
from inventory.reports import top_skus
from inventory.stock import Warehouse


class PricingTests(unittest.TestCase):
    def test_tier_threshold_is_inclusive(self):
        self.assertEqual(tier_for(1000), "silver")
        self.assertEqual(tier_for(5000), "gold")

    def test_discount_at_threshold(self):
        self.assertAlmostEqual(discount_rate(1000), 0.05)
        self.assertAlmostEqual(discount_rate(5000), 0.10)

    def test_apply_tax_rounds_to_cents(self):
        self.assertEqual(apply_tax(19.99, 0.075), 21.49)


class StockTests(unittest.TestCase):
    def test_available_counts_reservations_once(self):
        warehouse = Warehouse()
        warehouse.add("A", 10)
        warehouse.reserve("A", 4)
        self.assertEqual(warehouse.available("A"), 6)

    def test_restock_threshold_is_inclusive(self):
        warehouse = Warehouse()
        warehouse.add("B", 10)
        self.assertTrue(warehouse.needs_restock("B", 10))
        warehouse.add("B", 1)
        self.assertFalse(warehouse.needs_restock("B", 10))


class OrderTests(unittest.TestCase):
    def test_total_includes_shipping(self):
        lines = [{"unit_price": 100.0, "quantity": 1}]
        self.assertAlmostEqual(total(lines, 0.0), 107.5)

    def test_can_ship_needs_both_conditions(self):
        self.assertTrue(can_ship({"paid": True, "all_in_stock": True}))
        self.assertFalse(can_ship({"paid": True, "all_in_stock": False}))
        self.assertFalse(can_ship({"paid": False, "all_in_stock": True}))


class ReportTests(unittest.TestCase):
    def test_top_skus_is_descending(self):
        sales = {"a": 3, "b": 9, "c": 5, "d": 1}
        self.assertEqual(top_skus(sales, 2), ["b", "c"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
