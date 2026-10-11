"""Tests for push.py (run: python -m backend.push_test from the repo root)."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP

import httpx  # noqa: E402

from backend.app import push, rescan  # noqa: E402

TZ = push.TZ
HUB = "https://script.google.com/macros/s/HUB1/exec"
PANTRY = "https://script.google.com/macros/s/PANTRY/exec"


def phone(n=1, host="fcm.googleapis.com"):
    return {"endpoint": f"https://{host}/fcm/send/phone{n}", "keys": {"p256dh": "BP" + "a" * 80, "auth": "x" * 22}}


def at(day: int, hh: int, mm: int = 0) -> datetime:      # 2026-10-12 is a Monday
    return datetime(2026, 10, 12 + day, hh, mm, tzinfo=TZ)


def reset():
    for f in ("push.db", "push.json", "rescan.json"):
        p = Path(TMP) / f
        if p.exists():
            p.unlink()


class Keys(unittest.TestCase):
    def setUp(self):
        reset()

    def test_keys_are_made_once_and_kept(self):
        k1 = push.load_keys()
        self.assertEqual(len(push.public_key()), 87)              # 65-byte P-256 point, base64url
        self.assertEqual(push.load_keys()["private_key"], k1["private_key"])


class Subscriptions(unittest.TestCase):
    def setUp(self):
        reset()

    def test_only_real_push_services_and_valid_hub_links(self):
        self.assertEqual(push.subscribe("power", phone(), HUB)["ok"], True)
        self.assertEqual(push.subscribe("pantry", phone(2, "web.push.apple.com"))["ok"], True)
        with self.assertRaises(ValueError):
            push.subscribe("power", phone(3, "evil.example.com"), HUB)
        with self.assertRaises(ValueError):
            push.subscribe("power", phone(4), "https://evil.example.com/exec")
        with self.assertRaises(ValueError):
            push.subscribe("other", phone(5))
        self.assertTrue(push.is_subscribed(phone()["endpoint"]))
        push.unsubscribe(phone()["endpoint"])
        self.assertFalse(push.is_subscribed(phone()["endpoint"]))

    def test_gone_phones_are_forgotten(self):
        push.subscribe("pantry", phone())
        push.send(phone()["endpoint"], phone(), {"title": "x"}, lambda s, p: 410)
        self.assertFalse(push.is_subscribed(phone()["endpoint"]))

    def test_test_send(self):
        push.subscribe("power", phone(), HUB)
        got = []
        self.assertTrue(push.test_send(phone()["endpoint"], lambda s, p: got.append(p) or 201))
        self.assertEqual(got[0]["title"], "Power Tracker")


SCHED = {"weighIn": {"days": {"mon": "07:00", "sat": "09:00"}}, "lunch": {"days": {"mon": "14:00"}},
         "workoutGap": {"time": "18:00", "days": 2}}


def hub_data(weighed=False, lunch=False, last_workout_days=None, now=None):
    now = now or at(0, 12)
    d = {"settings": [{"key": "pushReminders", "value": json.dumps(SCHED)}], "weighins": [], "foods": [], "sessions": [], "sets": []}
    if weighed:
        d["weighins"].append({"date": now.date().isoformat(), "weight": 250})
    if lunch:
        d["foods"].append({"meal": "lunch", "date": now.astimezone().isoformat()})
    if last_workout_days is not None:
        d["sessions"].append({"session_id": "s1", "date": (now - timedelta(days=last_workout_days)).isoformat()})
        d["sets"].append({"session_id": "s1", "done": True})
    return d


class PowerTracker(unittest.TestCase):
    def kinds(self, now, **kw):
        return [n["kind"] for n in push.power_due(now, hub_data(now=now, **kw))]

    def test_weigh_in_at_the_day_time_unless_done(self):
        self.assertEqual(self.kinds(at(0, 6, 59), last_workout_days=0), [])
        self.assertEqual(self.kinds(at(0, 7, 5), last_workout_days=0), ["weighIn"])
        self.assertEqual(self.kinds(at(0, 7, 5), weighed=True, last_workout_days=0), [])
        self.assertEqual(self.kinds(at(0, 10, 30), last_workout_days=0), [])          # past the 3-hour window
        self.assertEqual(self.kinds(at(1, 7, 5), last_workout_days=0), [])           # Tuesday: no weigh-in set
        self.assertEqual(self.kinds(at(5, 9, 1), last_workout_days=0), ["weighIn"])  # Saturday 9:00

    def test_lunch_unless_logged(self):
        self.assertEqual(self.kinds(at(0, 14, 10), last_workout_days=0), ["lunch"])
        self.assertEqual(self.kinds(at(0, 14, 10), lunch=True, last_workout_days=0), [])

    def test_workout_nudge_after_n_days(self):
        self.assertEqual(self.kinds(at(0, 18, 5), last_workout_days=1), [])
        self.assertEqual(self.kinds(at(0, 18, 5), last_workout_days=2), ["workoutGap"])
        self.assertEqual(self.kinds(at(0, 18, 5)), ["workoutGap"])                    # never worked out


def pantry_sheet(shipments=(), low=()):
    S = [["OrderID", "Vendor", "Order Date", "Status", "workspace_id"]] + [[o, "Melaleuca", "2026-10-10", "Pending", "ws1"] for o in shipments]
    P = [["ProductID", "workspace_id", "name", "min_quantity"], ["p1", "ws1", "Tub Tea", 2], ["p2", "ws1", "Renew", 1], ["p3", "other", "Theirs", 5]]
    I = [["ProductID", "workspace_id", "Current Quantity"], ["p1", "ws1", 1 if "p1" in low else 6],
         ["p2", "ws1", 0 if "p2" in low else 4], ["p3", "other", 0]]
    return {"Shipment": S, "Products": P, "Inventory": I}


class Loop(unittest.TestCase):
    """check_once against a fake Google: hub + pantry sheets, fake phones."""

    def setUp(self):
        reset()
        (Path(TMP) / "rescan.json").write_text(json.dumps({"sheets_url": PANTRY, "workspace_id": "ws1"}))
        self.hub = hub_data(last_workout_days=0)
        self.pantry = pantry_sheet(shipments=["A1"])
        self.sent = []

    def client(self):
        def handler(req):
            sheet = req.url.params["sheet"]
            if str(req.url).startswith(HUB):
                key = {"Settings": "settings", "Weighins": "weighins", "Foods": "foods", "Sessions": "sessions", "Sets": "sets"}[sheet]
                return httpx.Response(200, json={"ok": True, "rows": self.hub[key]})
            return httpx.Response(200, json=self.pantry[sheet])
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def run_at(self, now):
        async def go():
            async with self.client() as c:
                return await push.check_once(c, lambda s, p: self.sent.append((s["endpoint"], p)) or 201, now)
        return asyncio.run(go())

    def test_power_reminders_go_to_every_phone_once_a_day(self):
        push.subscribe("power", phone(1), HUB)
        push.subscribe("power", phone(2, "web.push.apple.com"), HUB)
        self.run_at(at(0, 7, 2))
        self.run_at(at(0, 7, 7))                      # next check: already sent today
        self.assertEqual([p["tag"] for _, p in self.sent], ["weighIn", "weighIn"])
        self.assertEqual(self.sent[0][1]["url"], push.APP_URLS["power"])
        self.sent.clear()
        self.run_at(at(5, 9, 3))                      # Saturday's own reminder
        self.assertEqual(len(self.sent), 2)

    def test_pantry_first_check_is_quiet_then_new_things_alert(self):
        push.subscribe("pantry", phone(3))
        self.run_at(at(0, 9))                         # baseline: A1 already there, nothing low
        self.assertEqual(self.sent, [])
        self.pantry = pantry_sheet(shipments=["A1", "B2"], low=["p1"])
        self.run_at(at(0, 9, 5))
        tags = sorted(p["tag"] for _, p in self.sent)
        self.assertEqual(tags, ["low", "shipment"])
        ship = next(p for _, p in self.sent if p["tag"] == "shipment")
        self.assertIn("#B2", ship["body"])
        self.assertTrue(ship["url"].endswith("/shipments"))
        low = next(p for _, p in self.sent if p["tag"] == "low")
        self.assertEqual(low["body"], "Tub Tea is at restock level.")
        self.sent.clear()
        self.run_at(at(0, 9, 10))                     # nothing new
        self.assertEqual(self.sent, [])
        self.pantry = pantry_sheet(shipments=["A1", "B2"], low=[])   # restocked…
        self.run_at(at(0, 9, 15))
        self.pantry = pantry_sheet(shipments=["A1", "B2"], low=["p1", "p2"])   # …and low again, with another
        self.run_at(at(0, 9, 20))
        self.assertEqual([p["body"] for _, p in self.sent], ["Tub Tea, Renew are at restock level."])

    def test_pantry_waits_for_daytime(self):
        push.subscribe("pantry", phone(3))
        self.run_at(at(0, 9))
        self.pantry = pantry_sheet(shipments=["A1", "C3"])
        self.run_at(at(0, 23))
        self.assertEqual(self.sent, [])
        self.run_at(at(1, 8, 1))
        self.assertEqual([p["tag"] for _, p in self.sent], ["shipment"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
