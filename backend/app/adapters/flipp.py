import re

import httpx

from ..categorizer import categorize
from ..models import CircularItem, Store
from .base import CircularAdapter

FLIPP_BRANDS = re.compile(
    r"(?:"
    r"\baldi\b"
    r"|\bbravo\b"
    r"|\bc.?town\b"

    r"|\bfoodtown\b|\bfood\s+town\b"
    r"|\bfood\s+bazaar\b"
    r"|\bfood\s+universe\b"
    r"|\bfood\s+dynasty\b"
    r"|\bfood\s+emporium\b"
    r"|\bmet\s*fresh\b|\bmet\s*foods?\b"
    r"|\bassociated\b"
    r"|\bpioneer\b"
    r"|\bgristedes\b"
    r"|\bfairway\b"
    r"|\bfine\s+fare\b"
    r"|\bmorton\s+williams\b"
    r"|\bwestern\s+beef\b"
    r"|\blidl\b"
    r"|\bwegmans?\b"
    r"|\bstop\s*[&+n]?\s*shop\b|\bstop\s+and\s+shop\b"
    r"|\btrade\s+fair\b"
    r"|\bh\s*mart\b"
    r"|\bseasons\s+kosher\b"
    r"|\bjunior\s+fresh\b"
    r"|\bthree\s+guys\b"
    r"|\bsuperfresh\b|\bsuper\s+fresh\b"
    r")",
    re.I,
)

FLIPP_API = "https://backflipp.wishabi.com/flipp"
UA = "grocery-circular-list/0.1 (self-hosted)"

_ZIP_RE = re.compile(r"\b(\d{5})\b")

def _zip_from_address(address: str | None) -> str | None:
    if not address:
        return None
    m = _ZIP_RE.search(address)
    return m.group(1) if m else None

# Strip common chain-name suffixes before substring matching so OSM "Met Fresh"
# can match Flipp "Met Foods" only when they actually share a brand stem.
_NOISE = re.compile(
    r"\b(supermarket|supermarkets|marketplace|market|of\s+\w+(\s+\w+)*)\b",
    re.I,
)


def _normalize(s: str) -> str:
    s = _NOISE.sub("", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


def _names_match(a: str, b: str) -> bool:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


class FlippAdapter(CircularAdapter):
    name = "flipp"

    def matches(self, store: Store) -> bool:
        target = " ".join(filter(None, [store.brand, store.name])).lower()
        return bool(FLIPP_BRANDS.search(target))

    async def fetch(self, store: Store, *, postal_code: str) -> list[CircularItem]:
        store_label = store.brand or store.name
        # Store's own zip first (most accurate match), user search zip as fallback
        store_zip = _zip_from_address(store.address)
        zips = list(dict.fromkeys(filter(None, [store_zip, postal_code])))
        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20) as client:
            seen_ids: set = set()
            all_flyers: list[dict] = []
            for zip_code in zips:
                for f in await self._list_flyers(client, zip_code):
                    if _names_match(store_label, f.get("merchant", "")) and f.get("id") not in seen_ids:
                        seen_ids.add(f.get("id"))
                        all_flyers.append(f)
            items: list[CircularItem] = []
            for flyer in all_flyers:
                items.extend(await self._fetch_flyer_items(client, flyer))
        return items

    async def _list_flyers(self, client: httpx.AsyncClient, postal_code: str) -> list[dict]:
        r = await client.get(
            f"{FLIPP_API}/flyers",
            params={"locale": "en-us", "postal_code": postal_code},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("flyers", []) if isinstance(data, dict) else []

    async def _fetch_flyer_items(self, client: httpx.AsyncClient, flyer: dict) -> list[CircularItem]:
        flyer_id = flyer.get("id")
        if not flyer_id:
            return []
        valid_from = flyer.get("valid_from")
        valid_to = flyer.get("valid_to")
        r = await client.get(f"{FLIPP_API}/flyers/{flyer_id}")
        if r.status_code != 200:
            return []
        raw_items = (r.json() or {}).get("items", [])
        out: list[CircularItem] = []
        for it in raw_items:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            raw_price = it.get("price")
            price = f"${raw_price}" if raw_price not in (None, "", 0) else None
            sale_text = None
            for area in it.get("text_areas") or []:
                if isinstance(area, dict) and (txt := (area.get("text") or "").strip()):
                    sale_text = txt
                    break
            display_price = price or sale_text
            if not display_price:
                # Skip navigational "Shop X" cards and other priceless filler.
                continue
            out.append(CircularItem(
                name=name,
                price=display_price,
                category=categorize(name),
                valid_from=str(it.get("valid_from") or valid_from or "") or None,
                valid_to=str(it.get("valid_to") or valid_to or "") or None,
                image_url=it.get("cutout_image_url"),
            ))
        return out
