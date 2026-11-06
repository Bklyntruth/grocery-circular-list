"""Tests for vision_extractor's Gemini model fallback (run: python -m backend.vision_test from the repo root).

No network: OCR, the image download and Gemini are all faked, and the
back-off sleeps are skipped.
"""
import asyncio
import json
import unittest
from unittest import mock

from backend.app.adapters import vision_extractor as ve

DEALS = [{"name": "Whole Milk", "price": "$3.99", "unit": "1 gal"}]
OCR = [{"name": "OCR guess", "price": "$1.00", "unit": None}]   # fewer than 5, so Gemini is asked


class Reply:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.headers, self.content = {"content-type": "image/jpeg"}, b"img"

    def json(self):
        return self._body


def gemini_ok():
    return Reply(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(DEALS)}]}}]})


class FakeHttp:
    """Answers Gemini by model name through `plan(model) -> Reply`."""
    def __init__(self, plan):
        self.plan, self.asked = plan, []

    async def get(self, url, timeout=None):
        return Reply(200)

    async def post(self, url, params=None, json=None, timeout=None):
        model = url.rsplit("/", 1)[1].split(":")[0]
        self.asked.append(model)
        return self.plan(model)


async def _no_sleep(_seconds):
    return None


def page(plan):
    http = FakeHttp(plan)
    with mock.patch.object(ve, "_extract_page_ocr", mock.AsyncMock(return_value=list(OCR))), \
         mock.patch.object(ve.asyncio, "sleep", _no_sleep):
        deals = asyncio.run(ve._extract_one_page("https://example/p1.jpg", http, "KEY"))
    return deals, http.asked


class ModelFallback(unittest.TestCase):
    def setUp(self):
        ve._model_blocked_until.clear()

    def test_a_model_the_key_cant_use_falls_through_and_is_skipped_next_time(self):
        plan = lambda m: Reply(404, text="no longer available to new users") if m == "gemini-2.5-flash" else gemini_ok()
        deals, asked = page(plan)
        self.assertEqual(deals, DEALS)
        self.assertEqual(asked, ["gemini-2.5-flash", "gemini-3.1-flash-lite"])
        _, asked = page(plan)                       # next page: 2.5 Flash isn't asked again
        self.assertEqual(asked, ["gemini-3.1-flash-lite"])

    def test_daily_limit_is_per_model(self):
        plan = lambda m: Reply(429, text="GenerateRequestsPerDayPerProjectPerModel") if m == "gemini-2.5-flash" else gemini_ok()
        deals, asked = page(plan)
        self.assertEqual(deals, DEALS)
        self.assertEqual(asked, ["gemini-2.5-flash", "gemini-3.1-flash-lite"])

    def test_every_model_out_for_the_day_keeps_ocr_and_stops_asking(self):
        plan = lambda m: Reply(429, text="PerDay quota exceeded")
        deals, asked = page(plan)
        self.assertEqual(deals, OCR)
        self.assertEqual(asked, ve._MODELS)
        deals, asked = page(plan)                   # later pages don't even try
        self.assertEqual((deals, asked), (OCR, []))

    def test_a_refused_key_stops_at_once(self):
        deals, asked = page(lambda m: Reply(403, text="API key not valid"))
        self.assertEqual((deals, asked), (OCR, ["gemini-2.5-flash"]))
        self.assertEqual(ve._usable_models(), ve._MODELS, "a bad key isn't the model's fault")

    def test_a_busy_model_gets_one_retry_then_the_next_model(self):
        plan = lambda m: Reply(503, text="overloaded") if m == "gemini-2.5-flash" else gemini_ok()
        deals, asked = page(plan)
        self.assertEqual(deals, DEALS)
        self.assertEqual(asked, ["gemini-2.5-flash", "gemini-2.5-flash", "gemini-3.1-flash-lite"])

    def test_per_minute_limit_backs_off_on_the_same_model(self):
        replies = iter([Reply(429, text="PerMinute"), Reply(429, text="PerMinute"), gemini_ok()])
        deals, asked = page(lambda m: next(replies))
        self.assertEqual(deals, DEALS)
        self.assertEqual(asked, ["gemini-2.5-flash"] * 3)


if __name__ == "__main__":
    unittest.main()
