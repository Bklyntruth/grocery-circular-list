"""Unit tests for the read-only /api/pantry endpoints (run:
python -m backend.pantry_api_test from the repo root).
Tests the pure-data helpers so no real HTTP or Google Sheets round trip
is needed."""
import unittest

from backend.app.pantry_view import low_text as _pantry_low_text
from backend.app.pantry_view import deals_text as _pantry_deals_text


def p(pid, name, minq):
    return {"ProductID": pid, "name": name, "min_quantity": minq}


def inv(pid, qty):
    return {"ProductID": pid, "Current Quantity": qty}


def deal(store, valid_to):
    return {"store_name": store, "valid_to": valid_to}


class LowStock(unittest.TestCase):
    def test_lists_items_at_or_below_their_restock_level(self):
        products = [p("1", "Peanut Butter", 2), p("2", "Oats", 3), p("3", "Rice", 5),
                    p("4", "No Threshold Item", 0)]
        inventory = [inv("1", 0), inv("2", 3), inv("3", 10)]
        out = _pantry_low_text(products, inventory)
        self.assertIn("2 items at or below the restock level:", out)
        self.assertIn("Oats (3 of 3)", out)
        self.assertIn("Peanut Butter (0 of 2)", out)
        self.assertNotIn("Rice", out)
        self.assertNotIn("No Threshold Item", out)

    def test_no_restock_level_or_no_inventory_row_counts_as_zero_on_hand(self):
        products = [p("1", "Flour", 2)]
        self.assertIn("Flour (0 of 2)", _pantry_low_text(products, []))

    def test_singular_plural(self):
        self.assertIn("1 item at or below", _pantry_low_text([p("1", "Milk", 1)], [inv("1", 0)]))

    def test_empty_when_everything_is_above(self):
        out = _pantry_low_text([p("1", "Oats", 2)], [inv("1", 10)])
        self.assertEqual(out, "Nothing is at its restock level right now.")

    def test_sorted_alphabetically(self):
        products = [p("1", "Zebra Bars", 1), p("2", "Apples", 1), p("3", "milk", 1)]
        out = _pantry_low_text(products, [])
        a = out.index("Apples")
        m = out.index("milk")
        z = out.index("Zebra Bars")
        self.assertTrue(a < m < z, "should be case-insensitive sorted")

    def test_bad_numbers_are_ignored_quietly(self):
        products = [p("1", "Oats", "not a number"), p("2", "Flour", 2)]
        inventory = [inv("1", "junk"), inv("2", 1)]
        out = _pantry_low_text(products, inventory)
        self.assertIn("Flour", out)
        self.assertNotIn("Oats", out)

    def test_long_list_is_truncated_with_a_count(self):
        products = [p(str(i), f"Item {i:03d}", 1) for i in range(60)]
        out = _pantry_low_text(products, [], limit=10)
        self.assertIn("60 items at or below", out)
        self.assertIn("... and 50 more", out)
        self.assertEqual(out.count("\n"), 11, "header + 10 items + the '... more' line")


class LiveDeals(unittest.TestCase):
    def test_counts_live_deals_per_store(self):
        today = "2026-11-18"
        deals = [deal("ShopRite", "2026-11-22"), deal("ShopRite", "2026-11-22"),
                 deal("C-Town", "2026-11-22"), deal("Key Food", "2026-11-18"),
                 deal("Key Food", "2026-11-17")]  # expired (one day ago)
        out = _pantry_deals_text(deals, today)
        self.assertIn("4 deals live across 3 stores:", out)
        self.assertIn("2  ShopRite", out)
        self.assertIn("1  C-Town", out)
        self.assertIn("1  Key Food", out)

    def test_deal_with_no_end_date_counts_as_live(self):
        out = _pantry_deals_text([deal("Foodway", "")], "2026-11-18")
        self.assertIn("1 deal live across 1 store", out)

    def test_nothing_live(self):
        out = _pantry_deals_text([deal("ShopRite", "2026-10-01")], "2026-11-18")
        self.assertEqual(out, "No live circular deals right now — try a rescan.")

    def test_a_deal_without_a_store_is_dropped(self):
        self.assertEqual(_pantry_deals_text([deal("", "2026-12-01")], "2026-11-18"),
                         "No live circular deals right now — try a rescan.")


if __name__ == "__main__":
    unittest.main(verbosity=1)
