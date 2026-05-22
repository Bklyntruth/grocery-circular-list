import re
from typing import Optional
from pydantic import BaseModel, field_validator

_PRICE_NUM_RE = re.compile(r"(\d+\.\d*|\d+)")


def _normalize_price(price: Optional[str]) -> Optional[str]:
    """Ensure every dollar amount in a price string has exactly 2 decimal places."""
    if not price:
        return price

    def fix(m: re.Match) -> str:
        val = m.group(1)
        if "." in val:
            integer, frac = val.split(".", 1)
            frac = (frac + "00")[:2]
            return f"{integer}.{frac}"
        return val  # integers like "2/$5" — leave as-is

    return _PRICE_NUM_RE.sub(fix, price)


class Store(BaseModel):
    id: str
    name: str
    brand: Optional[str] = None
    lat: float
    lon: float
    address: Optional[str] = None
    distance_m: Optional[float] = None
    adapter: Optional[str] = None
    ref: Optional[str] = None  # adapter-specific store code (e.g. Red Pepper field_store_id)


class CircularItem(BaseModel):
    name: str
    price: Optional[str] = None
    unit: Optional[str] = None
    category: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    image_url: Optional[str] = None
    source_url: Optional[str] = None

    @field_validator("price", mode="before")
    @classmethod
    def normalize_price(cls, v):
        return _normalize_price(v)


class Circular(BaseModel):
    store_id: str
    store_name: str
    fetched_at: str
    items: list[CircularItem]
