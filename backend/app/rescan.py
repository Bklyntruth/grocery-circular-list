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
    "public_base": "https://byerway.<tailnet>.ts.net",  # for /img links
    "schedule": ["thu 22:00", "sat 22:00"]              # optional, see below
  }

It also runs by itself on a schedule (schedule_loop, started with the
server), and each run clears stores whose deals have all ended, so phones
stop downloading last month's ads on every sync.
"""
import asyncio
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_PATH = DATA_DIR / "rescan.json"

log = logging.getLogger("rescan")

# Most nearby stores start a new ad on Friday (C-Town, Key Food, Foodway) or
# Sunday (ShopRite), so by default the rescan runs the night before each:
# Thursday and Saturday at 10 pm. "schedule" in rescan.json overrides it
# ("off" turns it off). A run missed while the server was down still happens
# within a day of its time.
SCHEDULE_DEFAULT = ["thu 22:00", "sat 22:00"]
SCHEDULE_STATE = DATA_DIR / "rescan_schedule.json"
TZ = ZoneInfo(os.environ.get("PUSH_TZ", "America/New_York"))
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SLOT = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2}):(\d{2})$")
STALE_DAYS = 14     # an ad with no end date counts as over two weeks after its last fetch

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

            # Stores whose deals have all ended and that weren't refreshed just
            # now: clear them (an empty replace), so every phone's next sync
            # stops downloading expired ads.
            r = await client.get(sheets_url, params={"action": "read", "sheet": "CircularDeals"})
            r.raise_for_status()
            now = datetime.now(TZ)
            fresh = {u["store"].lower() for u in updated}
            fetched = {str(dict(zip(headers, raw)).get("store_name") or "").strip().lower():
                       dict(zip(headers, raw)).get("last_fetched") for raw in rows[1:]}
            cleared = []
            for name in expired_stores(r.json(), ws, now.date().isoformat(), fresh, fetched,
                                       (now - timedelta(days=STALE_DAYS)).date().isoformat()):
                await _post(client, sheets_url, {"action": "replace_store_deals", "store_name": name,
                                                 "workspace_id": ws, "deals": []})
                cleared.append(name)

        _state["result"] = {
            "zip": zip_code,
            "circulars": len(circulars),
            "stores_updated": updated,
            "stores_cleared": cleared,
            "seconds": round(time.time() - t0, 1),
        }
    except Exception as e:  # reported through /api/rescan/status
        _state["error"] = str(e)[:300]
    finally:
        _state["running"] = False
        _state["finished_at"] = _now()


def expired_stores(rows, workspace_id: str, today: str, skip: set,
                   fetched: Optional[dict] = None, stale_before: Optional[str] = None) -> list[str]:
    """Stores in CircularDeals (a header row + rows) whose every deal has a
    valid_to before `today` (YYYY-MM-DD). Some stores' ads carry no end date;
    those count as ended once the store was last fetched before
    `stale_before` (`fetched`: lower-case name -> last_fetched). Stores in
    `skip` (lower-case, just refreshed) are left alone."""
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    hdr = [str(h) for h in rows[0]]
    if "store_name" not in hdr or "valid_to" not in hdr:
        return []
    sn, vt = hdr.index("store_name"), hdr.index("valid_to")
    wi = hdr.index("workspace_id") if "workspace_id" in hdr else None
    latest: dict[str, Optional[str]] = {}
    names: dict[str, str] = {}
    for row in rows[1:]:
        name = str(row[sn] if sn < len(row) else "").strip()
        if not name:
            continue
        row_ws = str(row[wi] if wi is not None and wi < len(row) else "")
        if workspace_id and row_ws and row_ws != workspace_id:
            continue
        key = name.lower()
        names.setdefault(key, name)
        end = str(row[vt] if vt < len(row) else "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", end):
            latest[key] = None          # no end date: never call this store expired
        elif key not in latest:
            latest[key] = end
        elif latest[key] is not None:
            latest[key] = max(latest[key], end)
    def ended(k, end):
        if end is not None:
            return end < today
        got = str((fetched or {}).get(k) or "")[:10]
        return bool(stale_before) and bool(re.match(r"^\d{4}-\d{2}-\d{2}$", got)) and got < stale_before
    return [names[k] for k, end in latest.items() if k not in skip and ended(k, end)]


def due_slot(now: datetime, slots, done) -> Optional[str]:
    """The scheduled run to start now, as its start time, or None.

    A slot is due from its time until a day later, unless it already ran
    (`done` holds the start times of runs already made)."""
    for s in slots or []:
        m = SLOT.match(str(s).strip().lower())
        if not m:
            continue
        start = (now - timedelta(days=(now.weekday() - DAYS.index(m[1])) % 7)).replace(
            hour=int(m[2]), minute=int(m[3]), second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=7)
        key = start.isoformat()
        if now - start < timedelta(hours=24) and key not in done:
            return key
    return None


def _done() -> list:
    try:
        return json.loads(SCHEDULE_STATE.read_text(encoding="utf-8")).get("done", [])
    except (OSError, ValueError, AttributeError):
        return []


async def schedule_loop(fetch_all: Callable[[str], Awaitable[list]], every_s: int = 300) -> None:
    """Started with the server: every few minutes, start any rescan that's due."""
    while True:
        try:
            cfg = load_config()
            slots = cfg.get("schedule", SCHEDULE_DEFAULT)
            if cfg.get("sheets_url") and cfg.get("zip") and slots != "off":
                done = _done()
                slot = due_slot(datetime.now(TZ), slots, done)
                if slot and start(fetch_all):
                    SCHEDULE_STATE.write_text(json.dumps({"done": (done + [slot])[-20:]}), encoding="utf-8")
                    log.info("rescan: scheduled run for %s started", slot)
        except Exception as e:  # never let a bad config stop the loop
            log.warning("rescan schedule: %s", e)
        await asyncio.sleep(every_s)
