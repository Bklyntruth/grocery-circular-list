"""Gemini Flash vision-based grocery deal extractor.

Uses Gemini 1.5 Flash (free tier, no credit card) to extract structured
deal data from circular page images.

Get a free key at https://aistudio.google.com — no credit card needed.
Free tier: 1,500 requests/day, 15 requests/minute.

Set the key before starting the server:
    $env:GEMINI_API_KEY = "AIza..."
    python -m uvicorn backend.app.main:app ...
"""

import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

log = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"
_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_PROMPT = (
    "You are reading grocery store weekly circular page images (multiple pages shown).\n"
    "Extract every sale deal that has a visible price.\n"
    "Return a JSON array where each element has exactly these fields:\n"
    '  "name"  : product name (string, keep concise)\n'
    '  "price" : sale price exactly as shown, e.g. "$2.99", "2/$5", "BOGO" (string or null)\n'
    '  "unit"  : size/quantity only, e.g. "12 oz", "per lb" — omit fine print (string or null)\n'
    "Skip headers, banners, and anything without a price.\n"
    "Return ONLY the JSON array. No markdown, no explanation, no extra text."
)

_cache: dict[str, list[dict]] = {}


async def extract_deals(
    image_urls: list[str],
    *,
    cache_key: Optional[str] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Download circular page images and extract deals via Gemini Flash vision.

    Returns [] if GEMINI_API_KEY is not set or all pages fail.
    Results are cached in memory for the server session lifetime.
    """
    if cache_key and cache_key in _cache:
        log.debug("vision cache hit %s (%d deals)", cache_key, len(_cache[cache_key]))
        return _cache[cache_key]

    load_dotenv(_ENV_FILE, override=True)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY not set — skipping vision price extraction")
        return []

    own_client = http_client is None
    http = http_client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        deals = await _extract_all(image_urls, http, api_key)
    finally:
        if own_client:
            await http.aclose()

    if cache_key and deals:
        _cache[cache_key] = deals
    log.info("vision extracted %d deals from %d pages (key=%s)",
             len(deals), len(image_urls), cache_key)
    return deals


def clear_cache(cache_key: Optional[str] = None) -> None:
    if cache_key:
        _cache.pop(cache_key, None)
    else:
        _cache.clear()


async def _extract_all(urls: list[str], http: httpx.AsyncClient, api_key: str) -> list[dict]:
    # Download up to 8 pages and send them in ONE Gemini request (= 1 API call total).
    parts: list[dict] = []
    for url in urls[:8]:
        try:
            r = await http.get(url, timeout=30)
            if r.status_code != 200:
                continue
            ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if ct not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                ct = "image/jpeg"
            parts.append({"inline_data": {"mime_type": ct, "data": base64.standard_b64encode(r.content).decode()}})
        except Exception as exc:
            log.warning("image download error %s: %s", url, exc)

    if not parts:
        return []

    parts.append({"text": _PROMPT})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 65536},
    }

    try:
        resp = await http.post(
            f"{_API_BASE}/{_MODEL}:generateContent",
            params={"key": api_key},
            json=payload,
            timeout=120,
        )
        if resp.status_code == 429:
            log.warning("Gemini rate limit — quota exhausted")
            return []
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Gemini API error: %s", exc)
        return []

    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as exc:
        log.warning("Gemini unexpected response: %s", exc)
        return []

    if "```" in text:
        parts_text = text.split("```")
        text = parts_text[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [d for d in result if isinstance(d, dict) and d.get("name")]
        return []
    except json.JSONDecodeError:
        log.warning("vision: could not parse JSON: %s", text[:300])
        return []
