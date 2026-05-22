from abc import ABC, abstractmethod
from typing import Optional

from ..models import CircularItem, Store


class CircularAdapter(ABC):
    name: str = ""

    @abstractmethod
    def matches(self, store: Store) -> bool:
        ...

    @abstractmethod
    async def fetch(self, store: Store, *, postal_code: str) -> list[CircularItem]:
        ...

    async def page_image_urls(self, store: Store, *, postal_code: str) -> list[str]:
        """Return CDN/web URLs for each page of the store's current circular.

        Implement this in adapters whose circular is image-based so the
        vision-fallback path in main.py can extract prices when the adapter's
        normal fetch() returns no prices.  Default returns [] (opt-out).
        """
        return []
