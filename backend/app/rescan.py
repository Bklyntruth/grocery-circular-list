"""Rescan "doorbell" for devices that can't reach the server directly.

Pantry Portal on an iPad (no Tailscale) calls POST /api/rescan through a
Tailscale Funnel path that exposes only this endpoint. The server re-reads
every circular near the configured ZIP, then writes the deals straight into
the family's pantry Google Sheet — the same CircularDeals / StoreCircular
rows the app writes — so every device picks them up on its next Sync.

Config lives in DATA_DIR/rescan.json (bind-mounted, survives rebuilds):
  {
    "key": "<long random secret, also saved in the app>",
    "sheets_url": "https://script.google.com/macros/s/.../exec",
    "workspace_id": "8417_avenue_j_0001",
    "zip": "11236",
    "public_base": "https://byerway.<tailnet>.ts.net"   # for /img links
  }
"""
import asyncio
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_PATH = DATA_DIR / "rescan.json"

_state: dict = {"running": False, "started_at": None, "finished_at": None, "result": None, "error": None}


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def key_ok(given: Optional[str]) -> bool:
    expected = load_config().get("key") or ""
    return bool(expected) and bool(given) and hmac.compare_digest(given, expected)


def status() -> dict:
    return dict(_state)


def start(fetch_all: Callable[[str], Awaitable[list]], zip_code: Optional[str] = None) -> bool:
    """Kick off a background rescan. Returns False if one is already running."""
    if _state["running"]:
        return False
    _state.update(running=True, started_at=_now(), finished_at=None, result=None, error=None)
    asyncio.get_running_loop().create_task(_run(fetch_all, zip_code))
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Same matching as Pantry Portal's pickByerwayCircularForStore(): exact name,
# then substring either way ("C-Town" ↔ "C-Town Supermarket Flatlands").
def _pick(circulars: list, store_name: str):
    target = (store_name or "").lower().strip()
    if not target:
        return None
    for c in circulars:
        if (c.store_name or "").lower().strip() == target:
            return c
    for c in circulars:
        n = (c.store_name or "").lower().strip()
        if n and (n in target or target in n):
            return c
    return None


# Same filter/shape as the app's byerwayItemsToDeals() + syncCircularDealsToSheets().
def _deals(items, store_name: str, workspace_id: str, public_base: str) -> list[dict]:
    out = []
    for it in items:
        name = (it.name or "").strip()
        price = (it.price or "").strip()
        if not name or not price:
            continue
        img = it.image_url or ""
        if img.startswith("/api/img/"):
            # Funnel maps /img → /api/img; without a public base the link only
            # works on the tailnet, which is still better than nothing.
            img = (public_base.rstrip("/") + "/img/" + img[len("/api/img/"):]) if public_base else img
        out.append({
            "store_name": store_name,
            "workspace_id": workspace_id,
            "product_name": name,
            "price": price,
            "unit": (it.unit or "").strip(),
            "image_url": img,
            "promo": (it.category or "").strip(),
            "valid_from": (it.valid_from or "").strip(),
            "valid_to": (it.valid_to or "").strip(),
        })
    return out


async def _post(client: httpx.AsyncClient, url: str, body: dict) -> None:
    r = await client.post(url, content=json.dumps(body), headers={"Content-Type": "application/json"})
    r.raise_for_status()


async def _run(fetch_all, zip_override: Optional[str]) -> None:
    t0 = time.time()
    try:
        cfg = load_config()
        sheets_url = cfg.get("sheets_url") or ""
        ws = cfg.get("workspace_id") or ""
        zip_code = zip_override or cfg.get("zip") or ""
        if not sheets_url or not zip_code:
            raise RuntimeError("rescan.json needs sheets_url and zip")

        circulars = await fetch_all(zip_code)

        # Apps Script answers POSTs with a redirect to the result page.
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            r = await client.get(sheets_url, params={"action": "read", "sheet": "StoreCircular"})
            r.raise_for_status()
            rows = r.json()
            if not isinstance(rows, list) or not rows:
                raise RuntimeError("could not read the StoreCircular sheet")
            headers = [str(h) for h in rows[0]]

            updated = []
            for raw in rows[1:]:
                row = dict(zip(headers, raw))
                name = str(row.get("store_name") or "").strip()
                row_ws = str(row.get("workspace_id") or "")
                if not name or (ws and row_ws and row_ws != ws):
                    continue
                found = _pick(circulars, name)
                if not found or not found.items:
                    continue
                deals = _deals(found.items, name, ws, cfg.get("public_base") or "")
                if not deals:
                    continue
                await _post(client, sheets_url, {"action": "replace_store_deals", "store_name": name,
                                                 "workspace_id": ws, "deals": deals})
                # Send the whole row back: the sheet's upsert blanks any column left out.
                row["last_fetched"] = _now()
                await _post(client, sheets_url, {"action": "upsert_store_circular",
                                                 "circular": {k: ("" if v is None else v) for k, v in row.items()}})
                updated.append({"store": name, "deals": len(deals)})

        _state["result"] = {
            "zip": zip_code,
            "circulars": len(circulars),
            "stores_updated": updated,
            "seconds": round(time.time() - t0, 1),
        }
    except Exception as e:  # reported through /api/rescan/status
        _state["error"] = str(e)[:300]
    finally:
        _state["running"] = False
        _state["finished_at"] = _now()
