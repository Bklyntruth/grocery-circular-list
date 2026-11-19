"""Tests for rescan.py's schedule and clean-up (run: python -m backend.rescan_test from the repo root)."""
import os
import tempfile
import unittest
from datetime import datetime

os.environ["DATA_DIR"] = tempfile.mkdtemp()

from backend.app import rescan  # noqa: E402

TZ = rescan.TZ


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


class Schedule(unittest.TestCase):
    SLOTS = ["thu 22:00", "sat 22:00"]

    def test_due_from_its_time_until_a_day_later(self):
        # 2026-11-05 is a Thursday
        self.assertIsNone(rescan.due_slot(at(2026, 11, 5, 21, 59), self.SLOTS, []))
        thu = rescan.due_slot(at(2026, 11, 5, 22, 3), self.SLOTS, [])
        self.assertTrue(thu.startswith("2026-11-05T22:00"))
        self.assertEqual(rescan.due_slot(at(2026, 11, 6, 21, 0), self.SLOTS, []), thu)   # server was down: still runs
        self.assertIsNone(rescan.due_slot(at(2026, 11, 6, 22, 1), self.SLOTS, []))         # a day late: skip it

    def test_runs_once_per_slot(self):
        thu = rescan.due_slot(at(2026, 11, 5, 22, 3), self.SLOTS, [])
        self.assertIsNone(rescan.due_slot(at(2026, 11, 5, 22, 8), self.SLOTS, [thu]))
        sat = rescan.due_slot(at(2026, 11, 7, 22, 0), self.SLOTS, [thu])
        self.assertTrue(sat.startswith("2026-11-07T22:00"))

    def test_bad_or_empty_schedules_do_nothing(self):
        self.assertIsNone(rescan.due_slot(at(2026, 11, 5, 22, 3), ["thursday 10pm", "", None], []))
        self.assertIsNone(rescan.due_slot(at(2026, 11, 5, 22, 3), [], []))


class Cleanup(unittest.TestCase):
    HDR = ["store_name", "workspace_id", "product_name", "price", "unit", "image_url", "promo", "valid_from", "valid_to"]

    def rows(self, *deals):
        return [self.HDR] + [[s, ws, "x", "$1", "", "", "", "", to] for s, ws, to in deals]

    def test_only_stores_whose_every_deal_has_ended(self):
        r = self.rows(("ShopRite", "ws1", "2026-10-15"), ("ShopRite", "ws1", "2026-10-18"),
                      ("C-Town", "ws1", "2026-10-23"), ("C-Town", "ws1", "2026-11-12"),
                      ("ALDI", "ws1", "2026-09-22"), ("ALDI", "ws1", ""),
                      ("Key Food", "ws1", "2026-10-15T04:00:00.000Z"))
        self.assertEqual(rescan.expired_stores(r, "ws1", "2026-11-04", set()), ["ShopRite", "Key Food"])

    def test_ads_with_no_end_date_end_two_weeks_after_their_last_fetch(self):
        r = self.rows(("Foodway", "ws1", ""), ("Food Town", "ws1", ""), ("Mystery", "ws1", ""))
        fetched = {"foodway": "2026-10-09T16:20:58", "food town": "2026-11-05T01:25:50"}
        self.assertEqual(rescan.expired_stores(r, "ws1", "2026-11-04", set(), fetched, "2026-10-21"), ["Foodway"])
        self.assertEqual(rescan.expired_stores(r, "ws1", "2026-11-04", set()), [], "without fetch dates, nothing is guessed")

    def test_just_refreshed_and_other_pantries_are_left_alone(self):
        r = self.rows(("ShopRite", "ws1", "2026-10-15"), ("Foodway", "ws2", "2026-10-15"), ("Foodway", "", "2026-10-15"))
        self.assertEqual(rescan.expired_stores(r, "ws1", "2026-11-04", {"shoprite"}), ["Foodway"])
        self.assertEqual(rescan.expired_stores([self.HDR], "ws1", "2026-11-04", set()), [])
        self.assertEqual(rescan.expired_stores({"error": "x"}, "ws1", "2026-11-04", set()), [])


class OnFinishHook(unittest.IsolatedAsyncioTestCase):
    """The hook rescan.py calls after each _run(). Lets push.py send a
    lock-screen alert when a scheduled rescan fails."""

    def setUp(self):
        self._prev_cb = rescan._on_finish
        self._prev_state = dict(rescan._state)
        rescan._state.update(running=False, started_at=None, finished_at=None, result=None, error=None)
        rescan._state.pop("scheduled_slot", None)
        self.calls = []
        rescan.set_on_finish(lambda state, slot: self.calls.append((state, slot)))

    def tearDown(self):
        rescan.set_on_finish(self._prev_cb)
        rescan._state.clear()
        rescan._state.update(self._prev_state)

    async def test_fires_after_a_run_with_the_state_and_slot(self):
        # A failing fetch — the body of _run raises, the finally still fires.
        async def boom(_zip):
            raise RuntimeError("Gemini 503")
        rescan._state["scheduled_slot"] = "2026-11-19T22:00:00-05:00"
        # The real _run needs a config; without one the body raises early.
        await rescan._run(boom, None)
        self.assertEqual(len(self.calls), 1, "hook was not called")
        state, slot = self.calls[0]
        self.assertTrue(state.get("error"), "failing run should set an error")
        self.assertEqual(slot, "2026-11-19T22:00:00-05:00", "the scheduled slot should pass through")
        self.assertNotIn("scheduled_slot", rescan._state, "the slot should be cleared after a run")

    async def test_manual_runs_pass_slot_none(self):
        async def ok(_zip):
            return []
        # No slot set: a manual run like /api/rescan.
        await rescan._run(ok, None)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0][1], "manual runs carry slot=None")

    async def test_a_broken_hook_does_not_wedge_the_rescan(self):
        rescan.set_on_finish(lambda state, slot: (_ for _ in ()).throw(RuntimeError("bad hook")))
        async def ok(_zip):
            return []
        await rescan._run(ok, None)   # must not raise


if __name__ == "__main__":
    unittest.main()
