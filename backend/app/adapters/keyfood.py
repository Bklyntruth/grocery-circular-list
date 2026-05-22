"""Key Food stores — weekly deals via Swiftly onsale API (Firebase anonymous auth)."""

import math
import re
from typing import Optional

import httpx

from ..categorizer import categorize
from ..models import CircularItem, Store
from .base import CircularAdapter

_FIREBASE_API_KEY = "AIzaSyAKyUMx4KZfetVZU3T2vn1yPwoRvze_evg"
_CHAIN_ID = "611d0b2f-44cd-471e-b8d8-1d6ead4225aa"
_BASE = "https://prod.swiftlyapi.net"
_KF_ORIGIN = "https://www.keyfood.com"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_BRANDS = re.compile(r"\bkey\s*food\b", re.I)

_store_id_cache: dict[str, str] = {}

# Map Swiftly category slugs to human-readable grocery sections
_CAT_MAP = {
    "produce": "Produce",
    "tomatoes": "Produce",
    "fruit": "Produce",
    "vegetables": "Produce",
    "meat": "Meat & Seafood",
    "seafood": "Meat & Seafood",
    "poultry": "Meat & Seafood",
    "dairy": "Dairy",
    "butter": "Dairy",
    "cheese": "Dairy",
    "eggs": "Dairy",
    "frozen": "Frozen",
    "beverages": "Beverages",
    "soft-drinks": "Beverages",
    "coffee": "Beverages",
    "coffee-and-tea": "Beverages",
    "water": "Beverages",
    "juice": "Beverages",
    "beer": "Beverages",
    "wine": "Beverages",
    "breakfast-and-cereal": "Breakfast & Cereal",
    "cereal": "Breakfast & Cereal",
    "snacks": "Snacks",
    "salty-snacks": "Snacks",
    "candy": "Snacks",
    "cookies-and-crackers": "Snacks",
    "bread": "Bread & Bakery",
    "bakery": "Bread & Bakery",
    "household-supplies": "Household",
    "storage-containers": "Household",
    "paper-products": "Household",
    "cleaning-supplies": "Household",
    "condiments-and-dressings": "Condiments",
    "condiments": "Condiments",
    "oils-for-condiments-and-dressings": "Condiments",
    "sauces": "Condiments",
    "pasta": "Pantry",
    "rice": "Pantry",
    "canned-goods": "Pantry",
    "soups": "Pantry",
    "personal-care": "Health & Beauty",
    "health-and-beauty": "Health & Beauty",
    "vitamins": "Health & Beauty",
    "baby": "Baby",
    "pet": "Pet",
    "deli": "Deli",
    "prepared-foods": "Deli",
}

_DATE_RE = re.compile(r"Valid\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})", re.I)


def _parse_price(promo_text: Optional[str]) -> Optional[str]:
    """Extract clean sale price string from Swiftly promoArea.promoText."""
    if not promo_text:
        return None
    text = promo_text.split("\n")[0].strip()
    return text or None


def _parse_dates(validity_text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse valid_from and valid_to from 'Valid MM/DD/YY - MM/DD/YY'."""
    if not validity_text:
        return None, None
    m = _DATE_RE.search(validity_text)
    if not m:
        return None, None
    def to_iso(d: str) -> str:
        mo, dy, yr = d.split("/")
        return f"20{yr}-{mo}-{dy}"
    return to_iso(m.group(1)), to_iso(m.group(2))


def _get_section(categories: list[str]) -> Optional[str]:
    """Return the first matching human-readable section from category slugs."""
    for cat in categories:
        slug = cat.removeprefix("Product/")
        if slug in _CAT_MAP:
            return _CAT_MAP[slug]
    return None


class KeyFoodAdapter(CircularAdapter):
    name = "keyfood"

    def matches(self, store: Store) -> bool:
        target = " ".join(filter(None, [store.brand, store.name])).lower()
        return bool(_BRANDS.search(target))

    async def fetch(self, store: Store, *, postal_code: str) -> list[CircularItem]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Tier 1: Flipp — full weekly circular(s), no auth required.
            # Try user search zip AND the store's own zip — on overlap days this
            # returns items from both the expiring and the new circular.
            store_zip = self._zip_from_address(store.address)
            zips_to_try = list(dict.fromkeys(filter(None, [postal_code, store_zip])))
            items = await self._fetch_via_flipp(client, zips_to_try)
            if items:
                return items
            # Tier 2: Swiftly onsale (typically only a handful of universal deals)
            token = await self._get_firebase_token(client)
            swiftly_store_id = await self._find_store_id(client, token, store.lat, store.lon)
            items = await self._fetch_onsale(client, token, swiftly_store_id)
        return items

    @staticmethod
    def _zip_from_address(address: Optional[str]) -> Optional[str]:
        if not address:
            return None
        m = re.search(r"\b(\d{5})\b", address)
        return m.group(1) if m else None

    async def _fetch_via_flipp(self, client: httpx.AsyncClient, zips: list[str]) -> list[CircularItem]:
        from .flipp import FLIPP_API
        try:
            # Collect all Key Food flyers across every zip (dedup by flyer id)
            seen_flyer_ids: set = set()
            kf_flyers: list[dict] = []
            for zip_code in zips:
                r = await client.get(
                    f"{FLIPP_API}/flyers",
                    params={"locale": "en-us", "postal_code": zip_code},
                    timeout=15,
                )
                if not r.is_success:
                    continue
                data = r.json()
                all_flyers = data.get("flyers", []) if isinstance(data, dict) else []
                for f in all_flyers:
                    if re.search(r"key\s*food", f.get("merchant") or "", re.I):
                        fid = f.get("id")
                        if fid and fid not in seen_flyer_ids:
                            seen_flyer_ids.add(fid)
                            kf_flyers.append(f)

            if not kf_flyers:
                return []

            # Fetch items from ALL matching flyers (covers overlap days where
            # the expiring and the new circular are both active simultaneously)
            items: list[CircularItem] = []
            seen_names: set = set()
            for flyer in kf_flyers:
                flyer_id = flyer.get("id")
                if not flyer_id:
                    continue
                r2 = await client.get(f"{FLIPP_API}/flyers/{flyer_id}", timeout=15)
                if not r2.is_success:
                    continue
                raw_items = (r2.json() or {}).get("items", [])
                for it in raw_items:
                    name = (it.get("name") or "").strip()
                    if not name:
                        continue
                    raw_price = it.get("price")
                    price = f"${raw_price}" if raw_price not in (None, "", 0) else None
                    sale_text = next(
                        (a.get("text", "").strip() for a in (it.get("text_areas") or []) if isinstance(a, dict) and a.get("text")),
                        None,
                    )
                    display_price = price or sale_text
                    if not display_price:
                        continue
                    # Deduplicate identical items that appear in both circulars
                    dedup_key = f"{name}|{display_price}"
                    if dedup_key in seen_names:
                        continue
                    seen_names.add(dedup_key)
                    items.append(CircularItem(
                        name=name,
                        price=display_price,
                        category=categorize(name),
                        valid_from=str(it.get("valid_from") or flyer.get("valid_from") or "") or None,
                        valid_to=str(it.get("valid_to") or flyer.get("valid_to") or "") or None,
                        image_url=it.get("cutout_image_url"),
                        source_url=f"{_KF_ORIGIN}/flyers",
                    ))
            return items
        except Exception:
            return []

    async def _find_store_id(
        self, client: httpx.AsyncClient, token: str, lat: float, lon: float
    ) -> str:
        cache_key = f"{lat:.4f},{lon:.4f}"
        if cache_key in _store_id_cache:
            return _store_id_cache[cache_key]

        hdrs = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": _KF_ORIGIN,
            "Referer": f"{_KF_ORIGIN}/",
            "x-swiftly-chain-id": _CHAIN_ID,
            "User-Agent": _UA,
        }
        try:
            r = await client.get(
                f"{_BASE}/store/api/v2/chains/{_CHAIN_ID}/stores",
                params={"lat": lat, "lon": lon, "radius": 10000, "limit": 10},
                headers=hdrs,
                timeout=15,
            )
            if r.is_success:
                stores = r.json()
                if stores:
                    # Pick the geographically closest store
                    def dist(s: dict) -> float:
                        slat = s.get("lat") or s.get("latitude") or 0
                        slon = s.get("lon") or s.get("longitude") or 0
                        return math.hypot(slat - lat, slon - lon)
                    best = min(stores, key=dist)
                    sid = str(best.get("storeId") or best.get("id") or "2118")
                    _store_id_cache[cache_key] = sid
                    return sid
        except Exception:
            pass

        return "2118"

    async def _get_firebase_token(self, client: httpx.AsyncClient) -> str:
        r = await client.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={_FIREBASE_API_KEY}",
            json={"returnSecureToken": True},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{_KF_ORIGIN}/",
                "Origin": _KF_ORIGIN,
                "User-Agent": _UA,
            },
        )
        r.raise_for_status()
        return r.json()["idToken"]

    async def _fetch_onsale(
        self, client: httpx.AsyncClient, token: str, store_id: str
    ) -> list[CircularItem]:
        hdrs = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": _KF_ORIGIN,
            "Referer": f"{_KF_ORIGIN}/flyers",
            "x-swiftly-chain-id": _CHAIN_ID,
            "x-swiftly-store-id": store_id,
            "User-Agent": _UA,
        }
        r = await client.get(
            f"{_BASE}/product/api/v2/stores/{store_id}/products/onsale",
            params={"limit": 500},
            headers=hdrs,
            timeout=20,
        )
        r.raise_for_status()
        raw_items = r.json()
        # API may wrap results in a paginated envelope
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("items") or raw_items.get("products") or raw_items.get("data") or []

        items: list[CircularItem] = []
        for product in raw_items:
            name = product.get("name", "").strip()
            if not name:
                continue

            brand = product.get("brand", "")
            full_name = f"{brand} {name}".strip() if brand and brand not in name else name

            price_data = (product.get("price") or {}).get("ok") or {}
            promo_area = price_data.get("promoArea") or {}
            sale_price = _parse_price(promo_area.get("promoText"))
            valid_from, valid_to = _parse_dates(promo_area.get("validityText"))

            image_url = None
            primary = product.get("primaryImage") or {}
            if primary.get("url"):
                image_url = primary["url"]

            section = _get_section(product.get("categories") or [])

            items.append(
                CircularItem(
                    name=full_name,
                    price=sale_price,
                    category=section or categorize(full_name),
                    valid_from=valid_from,
                    valid_to=valid_to,
                    image_url=image_url,
                    source_url=f"{_KF_ORIGIN}/flyers",
                )
            )

        return items
