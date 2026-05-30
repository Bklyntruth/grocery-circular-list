import asyncio
import math
from typing import Optional

import httpx

from .extra_stores import EXTRA_STORES
from .models import Store

NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "grocery-circular-list/0.1 (self-hosted)"

_RP_BASE = "https://app.redpepper.digital"
_RP_CLIENT = "4573"
_RP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

_geocode_cache: dict[str, tuple[float, float]] = {}
_shoprite_geo_cache: list[dict] | None = None


async def geocode(query: str) -> Optional[tuple[float, float]]:
    if query in _geocode_cache:
        return _geocode_cache[query]
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=15) as c:
        r = await c.get(
            f"{NOMINATIM}/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        result = float(data[0]["lat"]), float(data[0]["lon"])
        _geocode_cache[query] = result
        return result


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def _get_shoprite_geo() -> list[dict]:
    """Fetch and cache the RedPepper geo list (all ShopRite store locations)."""
    global _shoprite_geo_cache
    if _shoprite_geo_cache is not None:
        return _shoprite_geo_cache
    try:
        async with httpx.AsyncClient(headers=_RP_HEADERS, timeout=15) as c:
            r = await c.get(
                f"{_RP_BASE}/client/{_RP_CLIENT}/catalogue/geo_location/json?_format=json"
            )
            if r.status_code == 200:
                _shoprite_geo_cache = r.json()
                return _shoprite_geo_cache
    except Exception:
        pass
    return []


async def _shoprite_stores_near(lat: float, lon: float, radius_m: int) -> list[Store]:
    """Return ShopRite stores within radius_m from the RedPepper geo API.

    Each store gets store.ref set to the RedPepper field_store_id so the
    ShopRite adapter can skip the haversine fallback and do a direct lookup.
    """
    geo = await _get_shoprite_geo()
    seen_store_ids: set[str] = set()
    stores: list[Store] = []
    for entry in geo:
        store_id = entry.get("field_store_id", "")
        if not store_id or store_id in seen_store_ids:
            continue
        try:
            slat = float(entry["field_latitude"])
            slon = float(entry["field_longitude"])
        except (KeyError, ValueError, TypeError):
            continue
        d = _haversine_m(lat, lon, slat, slon)
        if d > radius_m:
            continue
        seen_store_ids.add(store_id)
        name = entry.get("field_store_name") or "ShopRite"
        addr_parts = [
            entry.get("field_contact_address"),
            entry.get("field_city"),
            entry.get("field_state"),
            entry.get("field_zipcode"),
        ]
        address = ", ".join(p for p in addr_parts if p) or None
        stores.append(Store(
            id=f"rp:{store_id}",
            name=name,
            brand="ShopRite",
            lat=slat,
            lon=slon,
            address=address,
            distance_m=d,
            ref=store_id,
        ))
    return stores


async def find_stores_near(lat: float, lon: float, radius_m: int = 3200) -> list[Store]:
    query = f"""
    [out:json][timeout:25];
    (
      node["shop"~"supermarket|grocery|convenience"](around:{radius_m},{lat},{lon});
      way["shop"~"supermarket|grocery|convenience"](around:{radius_m},{lat},{lon});
    );
    out center tags;
    """

    osm_task = asyncio.create_task(_overpass_fetch(query))
    shoprite_task = asyncio.create_task(_shoprite_stores_near(lat, lon, radius_m))
    osm_data, shoprite_stores = await asyncio.gather(osm_task, shoprite_task, return_exceptions=True)

    stores: list[Store] = []

    if not isinstance(osm_data, Exception):
        for el in osm_data.get("elements", []):
            tags = el.get("tags", {})
            if el["type"] == "node":
                slat, slon = el["lat"], el["lon"]
            else:
                center = el.get("center", {})
                slat, slon = center.get("lat"), center.get("lon")
                if slat is None:
                    continue
            name = tags.get("name") or tags.get("brand")
            if not name:
                continue
            addr_parts = [
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
                tags.get("addr:city"),
                tags.get("addr:state"),
                tags.get("addr:postcode"),
            ]
            address = " ".join(p for p in addr_parts if p) or None
            stores.append(Store(
                id=f"{el['type'][0]}{el['id']}",
                name=name,
                brand=tags.get("brand"),
                lat=slat,
                lon=slon,
                address=address,
                distance_m=_haversine_m(lat, lon, slat, slon),
            ))

    # Merge EXTRA_STORES
    for extra in EXTRA_STORES:
        d = _haversine_m(lat, lon, extra.lat, extra.lon)
        if d > radius_m:
            continue
        if any(_haversine_m(extra.lat, extra.lon, s.lat, s.lon) < 50 for s in stores):
            continue
        stores.append(extra.model_copy(update={"distance_m": d}))

    # Merge ShopRite stores from RedPepper.
    # If an OSM store is already present at the same location, replace it with
    # a copy that has ref set (so the adapter gets a direct store_id lookup).
    # Only add as a new entry when OSM has no match at all.
    if not isinstance(shoprite_stores, Exception):
        _sr_re = __import__("re").compile(r"shop\s*rite", __import__("re").I)
        for sr in shoprite_stores:
            # Try to find an existing ShopRite entry at the same location first.
            idx = next(
                (
                    i for i, s in enumerate(stores)
                    if _sr_re.search(s.name or "") and _haversine_m(sr.lat, sr.lon, s.lat, s.lon) < 300
                ),
                None,
            )
            if idx is not None:
                if not stores[idx].ref:
                    stores[idx] = stores[idx].model_copy(update={"ref": sr.ref})
            else:
                stores.append(sr)

    stores.sort(key=lambda s: s.distance_m or float("inf"))
    return stores


async def _overpass_fetch(query: str) -> dict:
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
        r = await c.post(OVERPASS, data={"data": query})
        r.raise_for_status()
        return r.json()
