"""ShopRite adapter via Red Pepper Digital + Claude vision.

Flow
----
1. Resolve the current weekly catalogue NID for the store via Red Pepper
   Digital geo-location and catalogue APIs (ZIP -> store_id -> catalogue NID).
2. Concurrently fetch:
   - product regions  (/rpms_catalogue/{nid}/page/0/14/regions) for image URLs
   - page image URLs  (/catalogue/{nid}/page-images/json) for vision input
3. Send page images to Claude vision to extract (name, price, unit, promo).
4. Fuzzy-match vision results to regions to attach image URLs.
5. Return CircularItems with prices.  If vision is unavailable (no API key,
   all pages fail) returns [].  No placeholder fallback.
"""

import asyncio
import math
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..categorizer import categorize
from ..models import CircularItem, Store
from .base import CircularAdapter
from . import vision_extractor

_BRANDS = re.compile(r"\bshop\s*rite\b|\bshoprite\b", re.I)

_RP_BASE = "https://app.redpepper.digital"
_RP_CLIENT = "4573"
_RP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": f"{_RP_BASE}/a10/publications/home/{_RP_CLIENT}",
}

_REPL = "~FFFD~"   # placeholder; actual U+FFFD matched below
_REPL_RE = re.compile("�+(?=s\\b)")   # possessive
_ANY_REPL_RE = re.compile("�+")
_CURLY = {"'": "'", "’": "'", "“": '"', "”": '"', "′": "'"}
_NOISE_RE = re.compile(r"[^a-z0-9 ]")


def _clean(s: str) -> str:
    s = _REPL_RE.sub("'", s)
    s = _ANY_REPL_RE.sub("", s)
    for src, dst in _CURLY.items():
        s = s.replace(src, dst)
    return re.sub(r"\s+", " ", s).strip()


def _words(s: str) -> list[str]:
    return _NOISE_RE.sub(" ", _clean(s).lower()).split()


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


class ShopRiteAdapter(CircularAdapter):
    name = "shoprite"

    def matches(self, store: Store) -> bool:
        target = " ".join(filter(None, [store.brand, store.name]))
        return bool(_BRANDS.search(target))

    # ------------------------------------------------------------------
    async def fetch(self, store: Store, *, postal_code: str) -> list[CircularItem]:
        async with httpx.AsyncClient(
            headers=_RP_HEADERS, timeout=20, follow_redirects=True
        ) as client:
            _store_id, active_catalogues = await self._find_store_catalogues(client, store)
            if not active_catalogues:
                return []

            # Fetch all active circulars concurrently (handles overlap days where
            # two weekly NIDs are valid at the same time)
            async def _fetch_one(nid: str, cat_meta: dict) -> list[CircularItem]:
                regions, page_urls = await asyncio.gather(
                    self._fetch_regions(client, nid),
                    self._fetch_page_image_urls(client, nid),
                )
                deals = await vision_extractor.extract_deals(page_urls, cache_key=nid)
                return self._to_items(deals, regions, cat_meta)

            results = await asyncio.gather(*[_fetch_one(nid, meta) for nid, meta in active_catalogues])

            # Merge and deduplicate by name
            seen: set[str] = set()
            merged: list[CircularItem] = []
            for batch in results:
                for item in batch:
                    key = item.name.lower()
                    if key not in seen:
                        seen.add(key)
                        merged.append(item)
            return merged

    async def page_image_urls(self, store: Store, *, postal_code: str) -> list[str]:
        """CDN URLs for each page of the current circular (used by vision fallback)."""
        async with httpx.AsyncClient(
            headers=_RP_HEADERS, timeout=20, follow_redirects=True
        ) as client:
            _store_id, active_catalogues = await self._find_store_catalogues(client, store)
            if not active_catalogues:
                return []
            nid, _meta = active_catalogues[0]
            return await self._fetch_page_image_urls(client, nid)

    # ------------------------------------------------------------------
    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6_371_000
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    async def _find_store_catalogues(
        self, client: httpx.AsyncClient, store: Store
    ) -> tuple[Optional[str], list[tuple[str, dict]]]:
        """Return (store_id, [(nid, cat_meta), ...]) for all currently active weekly circulars."""
        cats_r, geo_r = await asyncio.gather(
            client.get(f"{_RP_BASE}/client/{_RP_CLIENT}/catalogues/json?_format=json"),
            client.get(f"{_RP_BASE}/client/{_RP_CLIENT}/catalogue/geo_location/json?_format=json"),
        )
        if cats_r.status_code != 200 or geo_r.status_code != 200:
            return None, []

        cats_by_nid = {c["nid_1"]: c for c in cats_r.json()}
        geo = geo_r.json()

        # Prefer a direct store code when available (store.ref = Red Pepper field_store_id).
        if store.ref:
            store_id = store.ref
        else:
            # Fall back to finding the closest entry by lat/lon.
            best, best_entry = float("inf"), None
            for entry in geo:
                try:
                    d = self._haversine(
                        store.lat, store.lon,
                        float(entry["field_latitude"]), float(entry["field_longitude"]),
                    )
                except (KeyError, ValueError, TypeError):
                    continue
                if d < best:
                    best, best_entry = d, entry
            if best_entry is None or best > 5_000:
                return None, []
            store_id = best_entry["field_store_id"]

        nids = {
            s["field_version"]
            for s in geo
            if s.get("field_store_id") == store_id and s.get("field_version")
        }

        now = datetime.now(timezone.utc)
        weekly: list[tuple[str, dict]] = []
        for nid in nids:
            cat = cats_by_nid.get(nid)
            if not cat:
                continue
            title = cat.get("title", "").lower()
            if "week of" not in title and "weekly" not in title:
                continue
            try:
                start = datetime.fromisoformat(cat["start"]).replace(tzinfo=timezone.utc)
                finish = datetime.fromisoformat(cat["finish"]).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if start <= now <= finish:
                weekly.append((nid, cat))

        weekly.sort(key=lambda x: x[1]["start"], reverse=True)
        return store_id, weekly

    async def _fetch_regions(self, client: httpx.AsyncClient, nid: str) -> list[dict]:
        r = await client.get(f"{_RP_BASE}/rpms_catalogue/{nid}/page/0/30/regions")
        if r.status_code != 200:
            return []
        return [reg for page in r.json().values() for reg in page]

    async def _fetch_page_image_urls(
        self, client: httpx.AsyncClient, nid: str
    ) -> list[str]:
        r = await client.get(
            f"{_RP_BASE}/catalogue/{nid}/page-images/json?_format=json"
        )
        if r.status_code != 200:
            return []
        return [p["image"] for p in r.json() if p.get("image")]

    # ------------------------------------------------------------------
    def _to_items(
        self,
        deals: list[dict],
        regions: list[dict],
        cat_meta: Optional[dict],
    ) -> list[CircularItem]:
        valid_from = cat_meta.get("start") if cat_meta else None
        valid_to = cat_meta.get("finish") if cat_meta else None

        # Build region lookup for image URL matching.
        region_index: list[tuple[list[str], Optional[str]]] = []
        for reg in regions:
            title = _clean(reg.get("field_product_title") or "")
            if not title:
                continue
            img_list = reg.get("field_product_image_url") or []
            img = img_list[0] if isinstance(img_list, list) and img_list else None
            region_index.append((_words(title), img))

        def _image_for(name: str) -> Optional[str]:
            v = _words(name)
            best, best_img = 0.0, None
            for r_words, img in region_index:
                s = _jaccard(v, r_words)
                if s > best:
                    best, best_img = s, img
            return best_img if best >= 0.25 else None

        items: list[CircularItem] = []
        seen: set[str] = set()

        if deals:
            for deal in deals:
                name = _clean(deal.get("name") or "")
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())

                price = (deal.get("price") or "").strip() or None
                unit = _clean(deal.get("unit") or "") or None

                items.append(CircularItem(
                    name=name,
                    price=price,
                    unit=unit,
                    category=categorize(name),
                    valid_from=valid_from,
                    valid_to=valid_to,
                    image_url=_image_for(name),
                ))
        else:
            # Vision unavailable — return product names from the regions structured data.
            for reg in regions:
                name = _clean(reg.get("field_product_title") or "")
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                desc = _clean(reg.get("field_product_description") or "") or None
                img_list = reg.get("field_product_image_url") or []
                img = img_list[0] if isinstance(img_list, list) and img_list else None
                items.append(CircularItem(
                    name=name,
                    price=None,
                    unit=desc,
                    category=categorize(name),
                    valid_from=valid_from,
                    valid_to=valid_to,
                    image_url=img,
                ))

        return items
