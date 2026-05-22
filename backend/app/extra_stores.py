"""Manually curated stores that OpenStreetMap doesn't have (or has poorly tagged).

Each entry mirrors the OSM `Store` shape. The locator merges these into Overpass
results, de-duplicating by location.
"""

from .models import Store

EXTRA_STORES: list[Store] = [
    Store(
        id="manual:foodway-georgetowne",
        name="Foodway",
        brand="Foodway",
        lat=40.6251127,
        lon=-73.9177145,
        address="2149 Ralph Avenue, Georgetowne Shopping Center, Brooklyn, NY 11234",
    ),
    Store(
        id="manual:keyfood-1804-ralph",
        name="Key Food",
        brand="Key Food",
        lat=40.633112,
        lon=-73.919042,
        address="1804 Ralph Avenue, Brooklyn, NY 11234",
    ),
    Store(
        id="manual:ctown-7924-flatlands",
        name="C-Town",
        brand="C-Town",
        lat=40.6352621,
        lon=-73.9133618,
        address="7924 Flatlands Avenue, Brooklyn, NY 11236",
    ),
    Store(
        id="manual:ctown-7812-flatlands",
        name="C-Town",
        brand="C-Town",
        lat=40.6346041,
        lon=-73.9145108,
        address="7812 Flatlands Avenue, Brooklyn, NY 11236",
    ),
    Store(
        id="manual:shoprite-590-gateway",
        name="ShopRite",
        brand="ShopRite",
        lat=40.6549049,
        lon=-73.8688324,
        address="590 Gateway Drive, Brooklyn, NY 11239",
        ref="289",  # Red Pepper Digital field_store_id for ShopRite of Gateway Plaza
    ),
]
