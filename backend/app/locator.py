import math
from typing import Optional

import httpx

from .extra_stores import EXTRA_STORES
from .models import Store

NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "grocery-circular-list/0.1 (self-hosted)"

_geocode_cache: dict[str, tuple[float, float]] = {}


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


async def find_stores_near(lat: float, lon: float, radius_m: int = 3200) -> list[Store]:
    query = f"""
    [out:json][timeout:25];
    (
      node["shop"~"supermarket|grocery|convenience"](around:{radius_m},{lat},{lon});
      way["shop"~"supermarket|grocery|convenience"](around:{radius_m},{lat},{lon});
    );
    out center tags;
    """
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
        r = await c.post(OVERPASS, data={"data": query})
        r.raise_for_status()
        data = r.json()

    stores: list[Store] = []
    for el in data.get("elements", []):
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

    for extra in EXTRA_STORES:
        d = _haversine_m(lat, lon, extra.lat, extra.lon)
        if d > radius_m:
            continue
        if any(_haversine_m(extra.lat, extra.lon, s.lat, s.lon) < 50 for s in stores):
            continue  # OSM already has this store
        stores.append(extra.model_copy(update={"distance_m": d}))

    stores.sort(key=lambda s: s.distance_m or float("inf"))
    return stores
