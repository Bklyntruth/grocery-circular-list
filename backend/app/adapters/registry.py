from typing import Optional

from ..models import Store
from .base import CircularAdapter

_adapters: list[CircularAdapter] = []


def register(adapter: CircularAdapter) -> None:
    _adapters.append(adapter)


def find_for(store: Store) -> Optional[CircularAdapter]:
    for a in _adapters:
        if a.matches(store):
            return a
    return None


def all_adapters() -> list[CircularAdapter]:
    return list(_adapters)
