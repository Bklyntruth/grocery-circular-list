"""Unit tests for the read-only /api/pantry endpoints (run:
python -m backend.pantry_api_test from the repo root).
Tests the pure-data helpers so no real HTTP or Google Sheets round trip
is needed."""
import unittest

from backend.app.pantry_view import low_text as _pantry_low_text
from backend.app.pantry_view import deals_text as _pantry_deals_text
from backend.app.pantry_view import find_text as _pantry_find_text
from backend.app.pantry_view import _price_cents


def p(pid, name, minq):
    return {"ProductID": pid, "name": name, "min_quantity": minq}


def inv(pid, qty):
    return {"ProductID": pid, "Current Quantity": qty}


def deal(store, valid_to, name="", price=""):
    return {"store_name": store, "valid_to": valid_to, "product_name": name, "price": price}


class LowStock(unittest.TestCase):
    def test_lists_items_at_or_below_their_restock_level(self):
        products = [p("1", "Peanut Butter", 2), p("2", "Oats", 3), p("3", "Rice", 5),
                    p("4", "No Threshold Item", 0)]
        inventory = [inv("1", 0), inv("2", 3), inv("3", 10), inv("4", 0)]
        out = _pantry_low_text(products, inventory)
        self.assertIn("2 items at or below the restock level:", out)
        self.assertIn("Oats (3 of 3)", out)
        self.assertIn("Peanut Butter (0 of 2)", out)
        self.assertNotIn("Rice", out)
        self.assertNotIn("No Threshold Item", out)

    def test_catalog_entries_with_no_inventory_row_are_ignored(self):
        """The household's real pantry: 440 catalog entries, 30 stocked.
        Only the 30 with an actual inventory row should be considered."""
        products = [p(str(i), f"Item {i:03d}", 1) for i in range(440)]
        # Only pid "5" and "6" have a stock row; both are empty.
        inventory = [inv("5", 0), inv("6", 0)]
        out = _pantry_low_text(products, inventory)
        self.assertIn("2 items at or below", out)
        self.assertNotIn("Item 000", out, "pid 0 is not in Inventory, so not reported")

    def test_singular_plural(self):
        self.assertIn("1 item at or below", _pantry_low_text([p("1", "Milk", 1)], [inv("1", 0)]))

    def test_empty_when_everything_is_above(self):
        out = _pantry_low_text([p("1", "Oats", 2)], [inv("1", 10)])
        self.assertEqual(out, "Nothing is at its restock level right now.")

    def test_sorted_alphabetically(self):
        products = [p("1", "Zebra Bars", 1), p("2", "Apples", 1), p("3", "milk", 1)]
        out = _pantry_low_text(products, [inv("1", 0), inv("2", 0), inv("3", 0)])
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
        inventory = [inv(str(i), 0) for i in range(60)]
        out = _pantry_low_text(products, inventory, limit=10)
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


class PriceParsing(unittest.TestCase):
    def test_reads_common_shapes(self):
        self.assertEqual(_price_cents("$2.99"), 299)
        self.assertEqual(_price_cents("2.99"), 299)
        self.assertEqual(_price_cents("2/$5"), 250)
        self.assertEqual(_price_cents("3 for $5"), 167)
        self.assertEqual(_price_cents("10/$10"), 100)

    def test_junk_sorts_last(self):
        self.assertEqual(_price_cents(""), 10 ** 9)
        self.assertEqual(_price_cents("call for price"), 10 ** 9)
        self.assertEqual(_price_cents(None), 10 ** 9)


class FindDeals(unittest.TestCase):
    TODAY = "2026-11-18"
    DEALS = [
        deal("ShopRite", "2026-11-22", "Bumble Bee Tuna Chunk Light 5oz", "$1.29"),
        deal("C-Town",   "2026-11-22", "Chicken of the Sea Tuna 5oz",     "4/$5"),
        deal("Key Food", "2026-11-22", "StarKist Tuna 5oz",                "$1.49"),
        deal("Foodway",  "2026-11-22", "Cheetos Crunchy 8.5oz",            "$3.99"),
        deal("ShopRite", "2026-11-22", "Coca-Cola 12pk",                   "2/$10"),
        deal("C-Town",   "2026-11-22", "Coca-Cola 12pk cans",              "$4.99"),
        deal("Key Food", "2026-11-17", "Pepsi 12pk",                       "$5.99"),  # expired
    ]

    def test_finds_matching_deals_cheapest_first(self):
        out = _pantry_find_text(self.DEALS, self.TODAY, "tuna")
        self.assertIn("Best price for tuna: 4/$5 at C-Town", out)
        lines = [l for l in out.splitlines() if l.startswith("  ")]
        self.assertEqual(len(lines), 3, "three live tuna deals")
        # First listed row is the cheapest.
        self.assertIn("C-Town", lines[0])
        self.assertIn("ShopRite", lines[1])
        self.assertIn("Key Food", lines[2])

    def test_parses_multi_for_prices(self):
        out = _pantry_find_text(self.DEALS, self.TODAY, "coca-cola")
        # 2/$10 = $5.00; $4.99 < $5.00, so C-Town wins.
        self.assertIn("Best price for coca-cola: $4.99 at C-Town", out)

    def test_expired_deals_dropped(self):
        out = _pantry_find_text(self.DEALS, self.TODAY, "pepsi")
        self.assertIn("No live deals found", out)

    def test_short_or_empty_query_asks_for_more(self):
        self.assertIn("Say what to search for", _pantry_find_text(self.DEALS, self.TODAY, ""))
        self.assertIn("Say what to search for", _pantry_find_text(self.DEALS, self.TODAY, "a b"))

    def test_nothing_found_says_so(self):
        out = _pantry_find_text(self.DEALS, self.TODAY, "kumquat")
        self.assertIn("No live deals found for 'kumquat'", out)

    def test_long_result_is_truncated(self):
        many = [deal("Store%d" % i, "2026-12-01", "Tuna %d" % i, "$%d.00" % (i + 1))
                for i in range(20)]
        out = _pantry_find_text(many, self.TODAY, "tuna", limit=5)
        self.assertIn("... and 15 more", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
