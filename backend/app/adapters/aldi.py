from ..models import CircularItem, Store
from .base import CircularAdapter


class AldiAdapter(CircularAdapter):
    name = "aldi"

    def matches(self, store: Store) -> bool:
        target = " ".join(filter(None, [store.brand, store.name])).lower()
        return "aldi" in target

    async def fetch(self, store: Store, *, postal_code: str) -> list[CircularItem]:
        return []
