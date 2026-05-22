from ..models import CircularItem, Store
from .base import CircularAdapter


class WholeFoodsAdapter(CircularAdapter):
    name = "wholefoods"

    def matches(self, store: Store) -> bool:
        target = " ".join(filter(None, [store.brand, store.name])).lower()
        return "whole foods" in target

    async def fetch(self, store: Store, *, postal_code: str) -> list[CircularItem]:
        return []
