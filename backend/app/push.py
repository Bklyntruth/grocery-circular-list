"""Lock-screen notifications (Web Push) for Power Tracker and Pantry Portal.

Phones subscribe from the apps (POST /api/push/subscribe, exposed publicly
through the Tailscale Funnel path /push). Every few minutes this server reads
the Google Sheets the apps already use and sends what's due:

  Power Tracker (per person, read from their Hub script):
    - weigh-in reminder at the day's time, unless already weighed in today
    - log-lunch reminder at the day's time, unless lunch is logged
    - no-workout nudge once a day when the last workout is N+ days old
    (schedule saved by the app in the Hub's Settings as `pushReminders`)
    - problems the Hub noticed (a workout that didn't go on the calendar,
      photo reading that stopped working), from Settings `alerts`, once each

  Pantry Portal (the family pantry sheet from rescan.json):
    - a new shipment arrived from the email scanner (Pending)
    - pantry items dropping to their restock level
    - a watched item on sale at a saved store (CircularDeals tab), once per price
    - items expiring within 3 days (dates the phone shares: the sheet has none)
    Each phone shares its alert switches, watchlist and upcoming expiry dates
    (POST /api/push/prefs), so these come with the app closed, like Power
    Tracker's reminders, and only the alerts that phone has turned on.

Files in DATA_DIR (bind-mounted, survives rebuilds):
  push.json  VAPID keys, generated on first start (public key is served to
             the apps; the private key never leaves this machine)
  push.db    subscriptions + what was already sent (so nothing repeats)
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
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
ALERT_FRESH = timedelta(hours=24)         # a Hub problem older than this is old news: Settings still lists it
PANTRY_HOURS = (8, 21)                    # pantry alerts wait for daytime
MAX_SUBSCRIPTIONS = 60
APPS = {"power", "pantry"}
APP_URLS = {"power": "https://portal-hub-pearl.vercel.app/", "pantry": "https://pantry-portal-c7rp.vercel.app/"}
PUSH_HOSTS = ("fcm.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com",
              ".notify.windows.com", "push.services.mozilla.com")
SCRIPT_URL_RE = re.compile(r"^https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec$")
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]   # datetime.weekday() order
PANTRY_CATEGORIES = ("deal_alerts", "low_stock", "expiration")   # the app's switch names the server honours
MAX_WATCH = 300
MAX_EXPIRING = 500
EXPIRING_DAYS = 3                         # the app's own rule: within 3 days
EXPIRED_GRACE_DAYS = 7                    # an item that expired longer ago than this is old news
DEALS_EVERY_S = 3600                      # deals change twice a week: read the tab at most hourly
_DEALS = {"at": 0.0, "rows": []}


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
        if "prefs" not in [r[1] for r in con.execute("pragma table_info(subs)")]:
            con.execute("alter table subs add column prefs text")
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
        rows = con.execute("select endpoint, sub_json, hub_url, prefs from subs where app=?", (app,)).fetchall()
    out = []
    for e, s, h, p in rows:
        try:
            prefs = json.loads(p) if p else {}
        except json.JSONDecodeError:
            prefs = {}
        out.append({"endpoint": e, "sub": json.loads(s), "hub_url": h, "prefs": prefs})
    return out


def _clean_prefs(prefs) -> dict:
    """What a Pantry Portal phone may share: its alert switches, its watchlist
    (id + name), and items with an expiry date (id + name + YYYY-MM-DD)."""
    if not isinstance(prefs, dict):
        raise ValueError("prefs must be an object")
    cats = prefs.get("categories") if isinstance(prefs.get("categories"), dict) else {}
    out = {"categories": {k: bool(cats[k]) for k in PANTRY_CATEGORIES if k in cats}}
    watch = []
    for w in (prefs.get("watch") or [])[:MAX_WATCH]:
        if isinstance(w, dict) and str(w.get("name") or "").strip():
            watch.append({"id": str(w.get("id") or w["name"])[:80], "name": str(w["name"]).strip()[:120]})
    out["watch"] = watch
    expiring = []
    for x in (prefs.get("expiring") or [])[:MAX_EXPIRING]:
        if isinstance(x, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x.get("date") or "")[:10]):
            expiring.append({"id": str(x.get("id") or x.get("name") or "")[:80], "name": str(x.get("name") or "Item").strip()[:120],
                             "date": str(x["date"])[:10]})
    out["expiring"] = expiring
    return out


def set_prefs(endpoint: str, prefs) -> dict:
    clean = _clean_prefs(prefs)
    with db() as con:
        row = con.execute("select app from subs where endpoint=?", (endpoint,)).fetchone()
        if not row:
            raise LookupError("This phone isn't registered")
        if row[0] != "pantry":
            raise ValueError("only Pantry Portal shares alert settings")
        con.execute("update subs set prefs=? where endpoint=?", (json.dumps(clean), endpoint))
    return {"ok": True, "watch": len(clean["watch"]), "expiring": len(clean["expiring"])}


def _wants(sub: dict, category: str) -> bool:
    """A phone that never shared its switches gets everything (the app's defaults are on)."""
    return (sub.get("prefs") or {}).get("categories", {}).get(category, True)


def _tag(endpoint: str) -> str:
    return hashlib.sha1(endpoint.encode()).hexdigest()[:12]


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


# ── Rescan-failure alerts ─────────────────────────────────────────────
# A scheduled rescan happens Thursday and Saturday at 10 pm; if it fails,
# the family finds out next time someone opens Pantry Portal and the deals
# are missing. This sends one lock-screen alert per failed slot (deduped
# through _once) so somebody can retry the rescan in the morning.
def handle_rescan_result(state: dict, slot: Optional[str], sender: Sender = webpush_send) -> int:
    """Called by rescan.py after each run. Alerts pantry subs when a run
    tagged with a schedule slot ends with an error. Manual /api/rescan runs
    (slot is None) are silent — the caller sees them. Returns the number of
    notifications sent."""
    if not slot or not state.get("error"):
        return 0
    if not _once("rescan|failed|" + slot):
        return 0
    body = "Try again from Pantry Portal: " + str(state.get("error") or "")[:120]
    sent = 0
    for s in _subs("pantry"):
        sent += send(s["endpoint"], s["sub"], {
            "title": "Grocery rescan failed",
            "body": body,
            "url": APP_URLS["pantry"] + "grocery-circulars",
            "tag": "rescan-failed",
        }, sender)
    log.info("rescan-failed alert sent to %d phones (slot=%s)", sent, slot)
    return sent


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


def power_alerts(now: datetime, data: dict) -> list[dict]:
    """Problems the Hub listed in Settings as `alerts` (Hub 2026.11.04+): the
    last day's, minus any it already showed as a Google Calendar pop-up."""
    out = []
    for a in _settings(data.get("settings", [])).get("alerts") or []:
        if not isinstance(a, dict) or not a.get("id") or a.get("calendar"):
            continue
        when = _local(a.get("at"))
        if not when or not now - ALERT_FRESH <= when <= now + timedelta(minutes=5):
            continue
        body = str(a.get("details") or "").split("\n\n")[0][:180]
        out.append({"kind": f"alert:{a['id']}", "title": f"Power Tracker problem: {a.get('title') or 'something needs a look'}", "body": body})
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


def _words(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).strip()


def name_match(target: str, candidate: str) -> bool:
    """The app's loose match (dealWatcher.fuzzyMatchName: "Skippy" finds
    "Skippy Peanut Butter Smooth 16 oz"), plus: every word of the watched name
    is in the deal ("Skippy Peanut Butter" finds "Skippy Creamy Peanut Butter")."""
    a, b = _words(target), _words(candidate)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < 4:
        return False
    if shorter[: -(-len(shorter) * 65 // 100)] in longer:
        return True
    want = [w for w in a.split() if len(w) >= 3]
    have = set(b.split())
    return len(want) >= 2 and all(w in have for w in want)


def deal_matches(now: datetime, deals: list[dict], workspace_id: str, watch: list[dict]) -> list[dict]:
    """Watched items on sale now at the saved stores: [{watch, store, product, price}]."""
    today = now.date().isoformat()
    out = []
    for d in deals:
        if str(d.get("workspace_id") or "") != workspace_id:
            continue
        valid_to = str(d.get("valid_to") or "")[:10]
        if valid_to and valid_to < today:
            continue
        product = str(d.get("product_name") or "")
        for w in watch:
            if name_match(w["name"], product):
                out.append({"watch": w, "store": str(d.get("store_name") or "a saved store"), "product": product,
                            "price": str(d.get("price") or "").strip()})
                break
    return out


def expiring_soon(now: datetime, expiring: list[dict]) -> list[tuple[dict, int]]:
    """Shared items expiring within EXPIRING_DAYS (or expired in the last week): [(item, days)]."""
    out = []
    for x in expiring:
        try:
            days = (date.fromisoformat(x["date"]) - now.date()).days
        except ValueError:
            continue
        if -EXPIRED_GRACE_DAYS <= days <= EXPIRING_DAYS:
            out.append((x, days))
    return sorted(out, key=lambda t: t[1])


def _when(days: int) -> str:
    if days < 0:
        return f"expired {-days} day{'s' if days != -1 else ''} ago"
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"


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
            log.warning("push: reading hub failed: %r", e)
            continue
        for n in power_due(now, data):
            if _once(f"power|{hub}|{n['kind']}|{now.date().isoformat()}"):
                for s in subs:
                    sent += send(s["endpoint"], s["sub"], {**n, "url": APP_URLS["power"], "tag": n["kind"]}, sender)
        for n in power_alerts(now, data):         # each problem's id already includes its day
            if _once(f"power|{hub}|{n['kind']}"):
                for s in subs:
                    sent += send(s["endpoint"], s["sub"], {**n, "url": APP_URLS["power"], "tag": n["kind"]}, sender)

    # Pantry Portal: the family sheet, daytime only.
    pantry_subs = _subs("pantry")
    cfg = rescan.load_config()
    if pantry_subs and cfg.get("sheets_url") and PANTRY_HOURS[0] <= now.hour < PANTRY_HOURS[1]:
        ws = cfg.get("workspace_id") or ""
        try:
            names = {"shipments": "Shipment", "products": "Products", "inventory": "Inventory"}
            data = {k: _rows(await _read(client, cfg["sheets_url"], v)) for k, v in names.items()}
            first = _once("pantry|baseline")
            for n in pantry_due(now, data, ws, first):
                if _once(f"pantry|{n['kind']}"):
                    url = APP_URLS["pantry"] + (n.get("path") or "")
                    for s in pantry_subs:
                        if n["kind"].startswith("low:") and not _wants(s, "low_stock"):
                            continue
                        sent += send(s["endpoint"], s["sub"], {**n, "url": url, "tag": n["kind"].split(":")[0]}, sender)
        except Exception as e:
            log.warning("push: pantry check failed: %r", e)
        # Watched items on sale: each phone's own watchlist, once per store and price.
        watchers = [s for s in pantry_subs if (s.get("prefs") or {}).get("watch") and _wants(s, "deal_alerts")]
        if watchers:
            try:
                if time.time() - _DEALS["at"] > DEALS_EVERY_S:
                    _DEALS.update(rows=_rows(await _read(client, cfg["sheets_url"], "CircularDeals")), at=time.time())
                deals = _DEALS["rows"]
                for s in watchers:
                    fresh = [m for m in deal_matches(now, deals, ws, s["prefs"]["watch"])
                             if _once(f"pantry|deal|{_tag(s['endpoint'])}|{m['watch']['id']}|{m['store']}|{m['price'].lower()}")]
                    if fresh:
                        first_m = fresh[0]
                        title = f"On sale: {first_m['watch']['name']}" if len(fresh) == 1 else f"{len(fresh)} watched items on sale"
                        body = "; ".join(f"{m['product']} {m['price']} at {m['store']}".replace("  ", " ") for m in fresh[:3])
                        if len(fresh) > 3:
                            body += f" and {len(fresh) - 3} more"
                        sent += send(s["endpoint"], s["sub"], {"title": title, "body": body[:200], "url": APP_URLS["pantry"] + "watchlist",
                                                              "tag": "deal"}, sender)
            except Exception as e:
                log.warning("push: deal check failed: %r", e)
        # Expiring soon: the dates this phone shared (the sheet has none), once per item and date.
        for s in pantry_subs:
            if not _wants(s, "expiration"):
                continue
            fresh = [(x, d) for x, d in expiring_soon(now, (s.get("prefs") or {}).get("expiring") or [])
                     if _once(f"pantry|exp|{_tag(s['endpoint'])}|{x['id']}|{x['date']}")]
            if fresh:
                x, d = fresh[0]
                title = (f"{x['name']} {'expired' if d < 0 else 'expiring soon'}" if len(fresh) == 1
                         else f"{len(fresh)} items expiring soon")
                body = ", ".join(f"{x['name']} ({_when(d)})" for x, d in fresh[:4]) + (f" and {len(fresh) - 4} more" if len(fresh) > 4 else "")
                sent += send(s["endpoint"], s["sub"], {"title": title, "body": body[:200], "url": APP_URLS["pantry"] + "inventory",
                                                      "tag": "expiration"}, sender)
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
