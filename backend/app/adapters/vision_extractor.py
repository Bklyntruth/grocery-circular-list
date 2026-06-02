"""Gemini Flash vision-based grocery deal extractor.

Uses Gemini 1.5 Flash (free tier, no credit card) to extract structured
deal data from circular page images.

Get a free key at https://aistudio.google.com — no credit card needed.
Free tier: 1,500 requests/day, 15 requests/minute.

Set the key before starting the server:
    $env:GEMINI_API_KEY = "AIza..."
    python -m uvicorn backend.app.main:app ...

Architecture note — WHY per-page, not batched
-----------------------------------------------
Sending multiple pages in one Gemini request causes two failure modes:
  1. Gemini summarises instead of itemising when overwhelmed with images.
  2. Long responses get truncated mid-JSON, silently dropping items.
Each page is processed independently so every item on every page is
captured at full accuracy.  A semaphore caps concurrent requests at 5,
staying safely under the free-tier 15 req/min limit.
DO NOT revert to a batched/all-at-once approach.
"""

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

log = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"
_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# One page at a time — focused, precise, no truncation risk.
_PROMPT = (
    "You are reading ONE page of a grocery store weekly circular.\n"
    "Extract EVERY sale deal on this page that has a visible price.\n"
    "Return a JSON array where each element has exactly these fields:\n"
    '  "name"  : product name only — a real grocery product (string, keep concise, 2-6 words)\n'
    '  "price" : sale price exactly as shown, e.g. "$2.99", "2/$5", "BOGO" (string or null)\n'
    '  "unit"  : size/quantity only, e.g. "12 oz", "per lb" — omit fine print (string or null)\n'
    "Rules:\n"
    "- name must be an actual food or household product, never a date, store name, or slogan\n"
    "- SKIP: date ranges (e.g. 'Valid thru', 'Thru Saturday', any line containing a month/year)\n"
    "- SKIP: store brand banners, circular headers, legal fine print, page numbers\n"
    "- SKIP: anything whose 'name' would be a unit, size, or quantity alone (e.g. '12 oz', 'per lb')\n"
    "- List every individual product separately — do NOT group or summarise\n"
    "Return ONLY the JSON array. No markdown, no explanation, no extra text."
)

# Max concurrent Gemini requests. Free tier is 15 req/min; 5 concurrent
# balances speed and rate-limit headroom for a 13-page circular.
_SEMAPHORE = asyncio.Semaphore(5)

_cache: dict[str, list[dict]] = {}
_cache_ts: dict[str, float] = {}   # NID → unix timestamp of last fill
_CACHE_TTL = 6 * 3600              # 6 hours — re-extract after circular updates

# Patterns that indicate a garbled / non-product entry
import re as _re
_DATE_RE = _re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b"
    r"|\bthru\b|\bvalid\b|\beffective\b|\bsaturday\b|\bsunday\b"
    r"|\bmonday\b|\btuesday\b|\bwednesday\b|\bthursday\b|\bfriday\b"
    r"|\b202[0-9]\b",
    _re.I,
)
_UNIT_ONLY_RE = _re.compile(
    r"^[\d\s./\-]*(oz|fl oz|lb|lbs|pk|ct|ml|g|kg|btl|can|pkg|qt|gal|pint|doz|count)\.?\s*$",
    _re.I,
)


def _recover_partial_json(text: str) -> list[dict]:
    """Extract complete JSON objects from a truncated array string."""
    items = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict) and obj.get("name"):
                        items.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None
    return items


def _is_valid_deal(d: dict) -> bool:
    name = (d.get("name") or "").strip()
    if not name or len(name) < 3:
        return False
    if _DATE_RE.search(name):
        return False
    if _UNIT_ONLY_RE.match(name):
        return False
    # Price must look like money; skip entries that only have None/empty price
    price = (d.get("price") or "").strip()
    if not price:
        return False
    return True


async def extract_deals(
    image_urls: list[str],
    *,
    cache_key: Optional[str] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Download circular page images and extract deals via Gemini Flash vision.

    Each page is sent as its own Gemini request so every item is captured
    individually with its price and image context.

    Returns [] if GEMINI_API_KEY is not set or all pages fail.
    Results are cached in memory for the server session lifetime.
    """
    if cache_key and cache_key in _cache:
        import time
        age = time.time() - _cache_ts.get(cache_key, 0)
        if age < _CACHE_TTL:
            log.debug("vision cache hit %s (%d deals, age %.0fs)", cache_key, len(_cache[cache_key]), age)
            return _cache[cache_key]
        # Stale cache — re-extract
        log.info("vision cache expired for %s (age %.0fs) — re-extracting", cache_key, age)
        _cache.pop(cache_key, None)
        _cache_ts.pop(cache_key, None)

    load_dotenv(_ENV_FILE, override=True)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY not set — skipping vision price extraction")
        return []

    own_client = http_client is None
    http = http_client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        deals = await _extract_all_pages(image_urls, http, api_key)
    finally:
        if own_client:
            await http.aclose()

    if cache_key and deals:
        import time
        _cache[cache_key] = deals
        _cache_ts[cache_key] = time.time()
    log.info("vision extracted %d deals from %d pages (key=%s)",
             len(deals), len(image_urls), cache_key)
    return deals


def clear_cache(cache_key: Optional[str] = None) -> None:
    if cache_key:
        _cache.pop(cache_key, None)
        _cache_ts.pop(cache_key, None)
    else:
        _cache.clear()
        _cache_ts.clear()


async def _extract_all_pages(
    urls: list[str], http: httpx.AsyncClient, api_key: str
) -> list[dict]:
    """Process every page independently in parallel (max 5 concurrent)."""
    tasks = [_extract_one_page(url, http, api_key) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    deals: list[dict] = []
    seen: set[str] = set()
    for r in results:
        if isinstance(r, Exception):
            log.warning("page extraction error: %s", r)
            continue
        for d in r:
            if not _is_valid_deal(d):
                log.debug("vision filter dropped: %s", d.get("name"))
                continue
            key = (d.get("name", "") + "|" + (d.get("price") or "")).lower()
            if key not in seen:
                seen.add(key)
                deals.append(d)
    return deals


_OCR_KEY = "helloworld"  # OCR.space free public demo key — 500 pages/month
_PRICE_RE = re.compile(
    r'(?:(?P<multi>\d+)\s*/?\s*(?:\$|for\s*\$)\s*(?P<mprice>\d+(?:\.\d{2})?)'
    r'|\$\s*(?P<price>\d+(?:\.\d{2})?))',
    re.I
)
_JUNK_LINE = re.compile(
    r'^\s*(?:\d{1,2}|\*+|save\b|limit\b|valid\b|offer|wou?\s*buy|thru\b|sunday|monday|'
    r'tuesday|wednesday|thursday|friday|saturday|jan|feb|mar|apr|may|jun|jul|aug|sep|'
    r'oct|nov|dec|shop.?rite|celebrating|america|birthday|stock|buy\s*\d|\s*)[\s\W]*$',
    re.I
)


async def _extract_page_ocr(url: str, http: httpx.AsyncClient) -> list[dict]:
    """OCR.space fallback when Gemini quota is exhausted. Free, 500 pages/month."""
    try:
        img_r = await http.get(url, timeout=30)
        if img_r.status_code != 200:
            return []
        b64 = base64.standard_b64encode(img_r.content).decode()
        ct = img_r.headers.get("content-type", "image/jpeg").split(";")[0].strip()

        resp = await http.post(
            "https://api.ocr.space/parse/image",
            data={
                "apikey": _OCR_KEY,
                "base64Image": f"data:{ct};base64,{b64}",
                "language": "eng",
                "isTable": "false",
                "scale": "true",
                "OCREngine": "2",
            },
            timeout=60,
        )
        result = resp.json()
        raw_text = result.get("ParsedResults", [{}])[0].get("ParsedText", "")
    except Exception as exc:
        log.warning("OCR.space error %s: %s", url, exc)
        return []

    # Parse deals from the OCR text
    lines = raw_text.splitlines()
    deals: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        price_match = _PRICE_RE.search(line)
        if price_match:
            # Price found on this line — name is on preceding non-junk lines
            name_parts = []
            for j in range(max(0, i - 3), i):
                candidate = lines[j].strip()
                if candidate and not _JUNK_LINE.match(candidate) and not _PRICE_RE.search(candidate):
                    name_parts.append(candidate)
            name = " ".join(name_parts[-2:]).strip() if name_parts else ""
            if not name and i + 1 < len(lines):
                name = lines[i + 1].strip()
            if price_match.group("multi"):
                price_str = f"{price_match.group('multi')}/${price_match.group('mprice')}"
            else:
                price_str = f"${price_match.group('price')}"
            if name and len(name) > 3 and not _DATE_RE.search(name):
                deals.append({"name": name, "price": price_str, "unit": None})
        i += 1

    return [d for d in deals if _is_valid_deal(d)]


async def _extract_one_page(
    url: str, http: httpx.AsyncClient, api_key: str
) -> list[dict]:
    """Extract deals from one circular page.

    PRIMARY:  OCR.space (free, 500 pages/month, no daily quota)
    FALLBACK: Gemini Flash (richer structured output, used when OCR returns
              fewer than 5 items — e.g. dense image-heavy pages)

    OCR.space first means no daily Gemini quota jams in normal use.
    Gemini steps in silently when OCR misses items on complex pages.
    """
    # ── PRIMARY: OCR.space ───────────────────────────────────────────────
    ocr_deals = await _extract_page_ocr(url, http)
    if len(ocr_deals) >= 5:
        log.debug("OCR.space got %d deals from %s", len(ocr_deals), url)
        return ocr_deals

    # OCR returned too few — try Gemini for better structured extraction
    if not api_key:
        return ocr_deals  # no key configured, return what we have

    log.debug("OCR only got %d deals, trying Gemini for %s", len(ocr_deals), url)

    # ── Download image for Gemini ────────────────────────────────────────
    try:
        r = await http.get(url, timeout=30)
        if r.status_code != 200:
            return ocr_deals
        ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if ct not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            ct = "image/jpeg"
        image_b64 = base64.standard_b64encode(r.content).decode()
    except Exception as exc:
        log.warning("image download for Gemini %s: %s", url, exc)
        return ocr_deals

    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": ct, "data": image_b64}},
                {"text": _PROMPT},
            ]
        }],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 16384},
    }

    # ── FALLBACK: Gemini ─────────────────────────────────────────────────
    async with _SEMAPHORE:
        resp = None
        for attempt in range(2):
            try:
                resp = await http.post(
                    f"{_API_BASE}/{_MODEL}:generateContent",
                    params={"key": api_key},
                    json=payload,
                    timeout=90,
                )
                if resp.status_code == 429:
                    if attempt == 0:
                        log.info("Gemini rate limit — retrying in 15s for %s", url)
                        await asyncio.sleep(15)
                        continue
                    log.info("Gemini quota exhausted — using OCR result for %s", url)
                    return ocr_deals
                resp.raise_for_status()
                break
            except Exception as exc:
                log.warning("Gemini error for %s (attempt %d): %s", url, attempt + 1, exc)
                if attempt == 0:
                    await asyncio.sleep(5)
                    continue
                return ocr_deals
        if resp is None:
            return ocr_deals

    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as exc:
        log.warning("Gemini unexpected response for %s: %s", url, exc)
        return []

    # Strip markdown code fences if present
    if "```" in text:
        segments = text.split("```")
        text = segments[1] if len(segments) > 1 else segments[0]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [d for d in result if isinstance(d, dict) and d.get("name")]
        return []
    except json.JSONDecodeError:
        # Truncated response — recover complete objects before the cut-off point
        recovered = _recover_partial_json(text)
        if recovered:
            log.info("vision: recovered %d items from truncated JSON on %s", len(recovered), url)
            return recovered
        log.warning("vision: unrecoverable JSON from page %s: %s", url, text[:200])
        return []
