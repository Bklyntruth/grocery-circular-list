"""Server-side grocery lists stored as a JSON file (supports multiple named lists)."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_FILE = Path(__file__).resolve().parents[2] / "grocery_lists.json"
_OLD_FILE = Path(__file__).resolve().parents[2] / "grocery_list.json"


def _load() -> dict:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Migrate from old single-list format
    if _OLD_FILE.exists():
        try:
            old_items = json.loads(_OLD_FILE.read_text(encoding="utf-8"))
            if isinstance(old_items, list):
                lid = str(uuid.uuid4())
                data = {"lists": {lid: {"id": lid, "name": "My List", "items": old_items}}}
                _save(data)
                return data
        except Exception:
            pass

    lid = str(uuid.uuid4())
    data = {"lists": {lid: {"id": lid, "name": "My List", "items": []}}}
    _save(data)
    return data


def _save(data: dict) -> None:
    _FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---- list management ----

def get_lists() -> list[dict]:
    data = _load()
    return [
        {"id": lst["id"], "name": lst["name"], "item_count": len(lst.get("items", []))}
        for lst in data.get("lists", {}).values()
    ]


def get_lists_full() -> list[dict]:
    """Returns all lists including their items — for full cross-app sync."""
    data = _load()
    return [
        {
            "id": lst["id"],
            "name": lst["name"],
            "created": lst.get("created", ""),
            "items": lst.get("items", []),
        }
        for lst in data.get("lists", {}).values()
    ]


def create_list(name: str, list_id: Optional[str] = None) -> dict:
    data = _load()
    lid = list_id or str(uuid.uuid4())
    if lid in data.get("lists", {}):
        # Already exists — return existing
        lst = data["lists"][lid]
        return {"id": lid, "name": lst["name"], "item_count": len(lst.get("items", []))}
    created = datetime.now(timezone.utc).isoformat()
    data.setdefault("lists", {})[lid] = {"id": lid, "name": name, "items": [], "created": created}
    _save(data)
    return {"id": lid, "name": name, "item_count": 0}


def rename_list(list_id: str, name: str) -> Optional[dict]:
    data = _load()
    lst = data.get("lists", {}).get(list_id)
    if not lst:
        return None
    lst["name"] = name
    _save(data)
    return {"id": list_id, "name": name, "item_count": len(lst.get("items", []))}


def delete_list(list_id: str) -> bool:
    data = _load()
    if list_id not in data.get("lists", {}):
        return False
    del data["lists"][list_id]
    _save(data)
    return True


# ---- item management ----

def get_items(list_id: str) -> Optional[list[dict]]:
    data = _load()
    lst = data.get("lists", {}).get(list_id)
    return lst["items"] if lst is not None else None


def add_item(list_id: str, item_data: dict) -> Optional[dict]:
    data = _load()
    lst = data.get("lists", {}).get(list_id)
    if lst is None:
        return None
    entry = {
        "id": str(uuid.uuid4()),
        "added_at": datetime.now(timezone.utc).isoformat(),
        "checked": False,
        **item_data,
    }
    lst["items"].append(entry)
    _save(data)
    return entry


def toggle_item(list_id: str, item_id: str, checked: bool) -> Optional[dict]:
    data = _load()
    lst = data.get("lists", {}).get(list_id)
    if not lst:
        return None
    for it in lst["items"]:
        if it["id"] == item_id:
            it["checked"] = checked
            _save(data)
            return it
    return None


def remove_item(list_id: str, item_id: str) -> bool:
    data = _load()
    lst = data.get("lists", {}).get(list_id)
    if not lst:
        return False
    before = len(lst["items"])
    lst["items"] = [it for it in lst["items"] if it["id"] != item_id]
    if len(lst["items"]) < before:
        _save(data)
        return True
    return False


def replace_items(list_id: str, items: list) -> Optional[list]:
    """Bulk-replace all items in a list (used for full sync from Pantry Portal)."""
    data = _load()
    lst = data.get("lists", {}).get(list_id)
    if lst is None:
        return None
    lst["items"] = items
    _save(data)
    return items


def clear_items(list_id: str) -> bool:
    data = _load()
    lst = data.get("lists", {}).get(list_id)
    if not lst:
        return False
    lst["items"] = []
    _save(data)
    return True
