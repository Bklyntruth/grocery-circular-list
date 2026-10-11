"""Lock-screen notifications (Web Push) for Power Tracker and Pantry Portal.

Phones subscribe from the apps (POST /api/push/subscribe, exposed publicly
through the Tailscale Funnel path /push). Every few minutes this server reads
the Google Sheets the apps already use and sends what's due:

  Power Tracker (per person, read from their Hub script):
    - weigh-in reminder at the day's time, unless already weighed in today
    - log-lunch reminder at the day's time, unless lunch is logged
    - no-workout nudge once a day when the last workout is N+ days old
    (schedule saved by the app in the Hub's Settings as `pushReminders`)

  Pantry Portal (the family pantry sheet from rescan.json):
    - a new shipment arrived from the email scanner (Pending)
    - pantry items dropping to their restock level

Files in DATA_DIR (bind-mounted, survives rebuilds):
  push.json  VAPID keys, generated on first start (public key is served to
             the apps; the private key never leaves this machine)
  push.db    subscriptions + what was already sent (so nothing repeats)
"""
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from . import rescan

log = logging.getLogger("push")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_PATH = DATA_DIR / "push.json"
DB_PATH = DATA_DIR / "push.db"
TZ = ZoneInfo(os.environ.get("PUSH_TZ", "America/New_York"))
CHECK_EVERY_S = 5 * 60
REMINDER_WINDOW = timedelta(hours=3)      # a reminder still goes out up to 3 h late (server restart), never later
PANTRY_HOURS = (8, 21)                    # pantry alerts wait for daytime
MAX_SUBSCRIPTIONS = 60
APPS = {"power", "pantry"}
APP_URLS = {"power": "https://portal-hub-pearl.vercel.app/", "pantry": "https://pantry-portal-c7rp.vercel.app/"}
PUSH_HOSTS = ("fcm.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com",
              ".notify.windows.com", "push.services.mozilla.com")
SCRIPT_URL_RE = re.compile(r"^https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec$")
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]   # datetime.weekday() order


# ── Keys ──────────────────────────────────────────────────────────────
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load_keys() -> dict:
    """VAPID key pair, created once and kept in push.json."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if cfg.get("private_key") and cfg.get("public_key"):
            return cfg
    except (OSError, json.JSONDecodeError):
        pass
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    priv = _b64url(key.private_numbers().private_value.to_bytes(32, "big"))
    pub = _b64url(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    cfg = {"private_key": priv, "public_key": pub, "contact": "mailto:tbyers08@gmail.com"}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return cfg


def public_key() -> str:
    return load_keys()["public_key"]


# ── Storage ───────────────────────────────────────────────────────────
@contextmanager
def db():
    """A connection that commits (or rolls back) and always closes."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""create table if not exists subs (
            endpoint text primary key, app text not null, sub_json text not null,
            hub_url text, label text, created_at text, fails integer default 0)""")
        con.execute("create table if not exists sent (key text primary key, at text)")
        con.execute("create table if not exists seen (kind text, item text, at text, primary key (kind, item))")
        with con:
            yield con
    finally:
        con.close()


def valid_subscription(sub: dict) -> bool:
    try:
        endpoint = sub["endpoint"]
        host = urlparse(endpoint).hostname or ""
        return (endpoint.startswith("https://") and any(host == h or host.endswith(h) for h in PUSH_HOSTS)
                and bool(sub["keys"]["p256dh"]) and bool(sub["keys"]["auth"]))
    except (KeyError, TypeError):
        return False


def subscribe(app: str, sub: dict, hub_url: Optional[str] = None, label: str = "") -> dict:
    if app not in APPS:
        raise ValueError("unknown app")
    if not valid_subscription(sub):
        raise ValueError("not a browser push subscription")
    if app == "power" and not (hub_url and SCRIPT_URL_RE.match(hub_url)):
        raise ValueError("Power Tracker needs its Hub script link")
    with db() as con:
        exists = con.execute("select 1 from subs where endpoint=?", (sub["endpoint"],)).fetchone()
        if not exists and con.execute("select count(*) from subs").fetchone()[0] >= MAX_SUBSCRIPTIONS:
            raise ValueError("too many devices registered")
        con.execute("""insert into subs (endpoint, app, sub_json, hub_url, label, created_at, fails) values (?,?,?,?,?,?,0)
            on conflict(endpoint) do update set app=excluded.app, sub_json=excluded.sub_json, hub_url=excluded.hub_url,
            label=excluded.label, fails=0""",
                    (sub["endpoint"], app, json.dumps(sub), hub_url if app == "power" else None, label[:60], _now().isoformat()))
    return {"ok": True}


def unsubscribe(endpoint: str) -> dict:
    with db() as con:
        con.execute("delete from subs where endpoint=?", (endpoint,))
    return {"ok": True}


def is_subscribed(endpoint: str) -> bool:
    with db() as con:
        return con.execute("select 1 from subs where endpoint=?", (endpoint,)).fetchone() is not None


def _subs(app: str) -> list[dict]:
    with db() as con:
        rows = con.execute("select endpoint, sub_json, hub_url from subs where app=?", (app,)).fetchall()
    return [{"endpoint": e, "sub": json.loads(s), "hub_url": h} for e, s, h in rows]


def _once(key: str) -> bool:
    """True the first time a key is seen (and records it)."""
    with db() as con:
        try:
            con.execute("insert into sent (key, at) values (?, ?)", (key, _now().isoformat()))
            return True
        except sqlite3.IntegrityError:
            return False


def _now() -> datetime:
    return datetime.now(TZ)


# ── Sending ───────────────────────────────────────────────────────────
Sender = Callable[[dict, dict], int]   # (subscription, payload) → HTTP status


def webpush_send(sub: dict, payload: dict) -> int:
    from pywebpush import WebPushException, webpush
    keys = load_keys()
    try:
        r = webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=keys["private_key"],
                    vapid_claims={"sub": keys["contact"]}, ttl=12 * 3600)
        return getattr(r, "status_code", 201)
    except WebPushException as e:
        status = getattr(e.response, "status_code", 500) if e.response is not None else 500
        detail = (e.response.text[:200] if e.response is not None else str(e))
        log.warning("push: send failed (%s) to %s: %s", status, urlparse(sub.get("endpoint", "")).hostname, detail)
        return status
    except Exception as e:      # network trouble, bad keys…
        log.warning("push: send error to %s: %s", urlparse(sub.get("endpoint", "")).hostname, e)
        return 500


def send(endpoint: str, sub: dict, payload: dict, sender: Sender) -> bool:
    status = sender(sub, payload)
    if status >= 500 or status == 429:      # push service hiccup: one more try
        time.sleep(2)
        status = sender(sub, payload)
    with db() as con:
        if status in (404, 410):          # the phone unsubscribed or the app was removed
            con.execute("delete from subs where endpoint=?", (endpoint,))
        elif status >= 400:
            con.execute("update subs set fails=fails+1 where endpoint=?", (endpoint,))
            con.execute("delete from subs where endpoint=? and fails>=20", (endpoint,))
        else:
            con.execute("update subs set fails=0 where endpoint=?", (endpoint,))
    return status < 400


def test_send(endpoint: str, sender: Sender = webpush_send) -> bool:
    with db() as con:
        row = con.execute("select app, sub_json from subs where endpoint=?", (endpoint,)).fetchone()
    if not row:
        return False
    app, sub = row[0], json.loads(row[1])
    title = "Power Tracker" if app == "power" else "Pantry Portal"
    return send(endpoint, sub, {"title": title, "body": "Notifications are on. You'll see reminders here, even on the lock screen.",
                                "url": APP_URLS[app], "tag": "test"}, sender)


# ── What's due: Power Tracker ─────────────────────────────────────────
def _rows(table) -> list[dict]:
    """Hub `read` gives {ok, rows:[{...}]}; the pantry gives a 2-D array."""
    if isinstance(table, dict):
        return table.get("rows") or []
    if isinstance(table, list) and table and isinstance(table[0], list):
        hdr = [str(h) for h in table[0]]
        return [dict(zip(hdr, r)) for r in table[1:]]
    return []


def _local(iso) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(TZ) if d.tzinfo else d.replace(tzinfo=TZ)


def _at(now: datetime, hhmm: str) -> Optional[datetime]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(hhmm or ""))
    return now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0) if m else None


def _settings(rows: list[dict]) -> dict:
    out = {}
    for r in rows:
        try:
            out[str(r.get("key"))] = json.loads(r.get("value"))
        except (TypeError, ValueError):
            out[str(r.get("key"))] = r.get("value")
    return out


def power_due(now: datetime, data: dict) -> list[dict]:
    """Reminders due right now for one Hub. data: settings/weighins/foods/sessions/sets rows."""
    sched = _settings(data.get("settings", [])).get("pushReminders") or {}
    today = now.date()
    day = DAY_KEYS[now.weekday()]
    out = []

    def window(hhmm):
        t = _at(now, hhmm)
        return t is not None and t <= now < t + REMINDER_WINDOW

    w = (sched.get("weighIn") or {}).get("days", {}).get(day)
    if w and window(w):
        weighed = any(str(r.get("date", ""))[:10] == today.isoformat() for r in data.get("weighins", []))
        if not weighed:
            out.append({"kind": "weighIn", "title": "Time to weigh in", "body": "Step on the scale before breakfast and log it."})

    l = (sched.get("lunch") or {}).get("days", {}).get(day)
    if l and window(l):
        logged = any(str(r.get("meal")) == "lunch" and (_local(r.get("date")) or now).date() == today for r in data.get("foods", []))
        if not logged:
            out.append({"kind": "lunch", "title": "Log your lunch", "body": "Add what you ate so the app can plan the rest of today."})

    gap = sched.get("workoutGap") or {}
    if gap.get("time") and window(gap["time"]):
        done = {str(r.get("session_id")) for r in data.get("sets", []) if str(r.get("done")).lower() == "true"}
        dates = [d for r in data.get("sessions", []) if str(r.get("session_id")) in done and (d := _local(r.get("date")))]
        last = max(dates) if dates else None
        since = (today - last.date()).days if last else None
        if since is None or since >= int(gap.get("days") or 2):
            body = "No workouts logged yet. Even 20 minutes counts." if since is None else f"It's been {since} days. Even 20–30 minutes keeps the streak going."
            out.append({"kind": "workoutGap", "title": "Time for a workout", "body": body})
    return out


# ── What's due: Pantry Portal ─────────────────────────────────────────
def pantry_due(now: datetime, data: dict, workspace_id: str, first_run: bool) -> list[dict]:
    """New shipments and newly-low items. Marks what it has seen; on the very
    first run it only records the current state (no flood of old news)."""
    ws = lambda r: str(r.get("workspace_id") or "") == workspace_id
    out = []

    for r in filter(ws, data.get("shipments", [])):
        oid = str(r.get("OrderID") or "").strip()
        if not oid or str(r.get("Status") or "").lower() in ("received", "complete", "completed", "cancelled"):
            continue
        if _seen("shipment", oid) or first_run:
            continue
        when = str(r.get("Order Date") or "")[:10]
        out.append({"kind": f"shipment:{oid}", "title": "New shipment on the way",
                    "body": f"{r.get('Vendor') or 'Order'} {when} #{oid}: check items in when it arrives.", "path": "shipments"})

    names = {str(p.get("ProductID")): p for p in filter(ws, data.get("products", []))}
    low_now = []
    for inv in filter(ws, data.get("inventory", [])):
        pid = str(inv.get("ProductID"))
        p = names.get(pid)
        try:
            qty = float(inv.get("Current Quantity"))
            minq = float((p or {}).get("min_quantity") or 0)
        except (TypeError, ValueError):
            continue
        if p and minq > 0 and qty <= minq:
            low_now.append((pid, str(p.get("name") or "Pantry item")))
    low_ids = {pid for pid, _ in low_now}
    with db() as con:   # items back above their level can alert again next time
        for (item,) in con.execute("select item from seen where kind='low'").fetchall():
            if item not in low_ids:
                con.execute("delete from seen where kind='low' and item=?", (item,))
    fresh = [(pid, name) for pid, name in low_now if not _seen("low", pid)]
    for pid, _ in fresh:
        _mark("low", pid)
    if fresh and not first_run:
        listed = ", ".join(n for _, n in fresh[:4]) + (f" and {len(fresh) - 4} more" if len(fresh) > 4 else "")
        out.append({"kind": "low:" + ",".join(sorted(p for p, _ in fresh)), "title": "Running low",
                    "body": f"{listed} {'is' if len(fresh) == 1 else 'are'} at restock level.", "path": "inventory"})
    for r in filter(ws, data.get("shipments", [])):
        oid = str(r.get("OrderID") or "").strip()
        if oid:
            _mark("shipment", oid)
    return out


def _seen(kind: str, item: str) -> bool:
    with db() as con:
        return con.execute("select 1 from seen where kind=? and item=?", (kind, item)).fetchone() is not None


def _mark(kind: str, item: str) -> None:
    with db() as con:
        con.execute("insert or ignore into seen (kind, item, at) values (?,?,?)", (kind, item, _now().isoformat()))


# ── The loop ──────────────────────────────────────────────────────────
async def _read(client: httpx.AsyncClient, url: str, sheet: str):
    r = await client.get(url, params={"action": "read", "sheet": sheet})
    r.raise_for_status()
    return r.json()


async def check_once(client: httpx.AsyncClient, sender: Sender = webpush_send, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    sent = 0
    # Power Tracker: one read per Hub, shared by that person's devices.
    by_hub: dict[str, list[dict]] = {}
    for s in _subs("power"):
        by_hub.setdefault(s["hub_url"], []).append(s)
    for hub, subs in by_hub.items():
        try:
            names = {"settings": "Settings", "weighins": "Weighins", "foods": "Foods", "sessions": "Sessions", "sets": "Sets"}
            data = {k: _rows(await _read(client, hub, v)) for k, v in names.items()}
        except Exception as e:  # one person's script being down shouldn't stop the rest
            log.warning("push: reading hub failed: %s", e)
            continue
        for n in power_due(now, data):
            if _once(f"power|{hub}|{n['kind']}|{now.date().isoformat()}"):
                for s in subs:
                    sent += send(s["endpoint"], s["sub"], {**n, "url": APP_URLS["power"], "tag": n["kind"]}, sender)

    # Pantry Portal: the family sheet, daytime only.
    pantry_subs = _subs("pantry")
    cfg = rescan.load_config()
    if pantry_subs and cfg.get("sheets_url") and PANTRY_HOURS[0] <= now.hour < PANTRY_HOURS[1]:
        try:
            names = {"shipments": "Shipment", "products": "Products", "inventory": "Inventory"}
            data = {k: _rows(await _read(client, cfg["sheets_url"], v)) for k, v in names.items()}
            first = _once("pantry|baseline")
            for n in pantry_due(now, data, cfg.get("workspace_id") or "", first):
                if _once(f"pantry|{n['kind']}"):
                    url = APP_URLS["pantry"] + (n.get("path") or "")
                    for s in pantry_subs:
                        sent += send(s["endpoint"], s["sub"], {**n, "url": url, "tag": n["kind"].split(":")[0]}, sender)
        except Exception as e:
            log.warning("push: pantry check failed: %s", e)
    return {"sent": sent}


async def loop() -> None:
    load_keys()
    while True:
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await check_once(client)
                if r["sent"]:
                    log.info("push: sent %s notification(s)", r["sent"])
        except Exception as e:
            log.warning("push loop: %s", e)
        await asyncio.sleep(CHECK_EVERY_S)
