"""SQLite-backed TTL cache for circular fetches."""
import json
import os
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "cache.db"
DEFAULT_TTL = int(os.environ.get("CACHE_TTL", 86400))


def _conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute(
        "CREATE TABLE IF NOT EXISTS circular_cache ("
        "key TEXT PRIMARY KEY, payload TEXT NOT NULL, "
        "item_count INTEGER NOT NULL, fetched_at REAL NOT NULL)"
    )
    return c


def get(adapter_name, zip_code, ttl=DEFAULT_TTL):
    key = adapter_name + "|" + zip_code
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT payload, fetched_at FROM circular_cache WHERE key = ?",
                (key,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    payload, fetched_at = row
    if time.time() - fetched_at > ttl:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def put(adapter_name, zip_code, items):
    if not items:
        return
    rows = []
    for it in items:
        if hasattr(it, "model_dump"):
            rows.append(it.model_dump(mode="json"))
        else:
            rows.append(it)
    key = adapter_name + "|" + zip_code
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO circular_cache VALUES (?, ?, ?, ?)",
                (key, json.dumps(rows), len(rows), time.time()),
            )
    except sqlite3.Error:
        pass


def stale(adapter_name, zip_code):
    return get(adapter_name, zip_code, ttl=10**9)


def health():
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT key, item_count, fetched_at FROM circular_cache "
                "ORDER BY fetched_at DESC"
            ).fetchall()
    except sqlite3.Error:
        return []
    now = time.time()
    out = []
    for k, n, ts in rows:
        out.append({"key": k, "items": n, "age_hours": round((now - ts) / 3600, 1)})
    return out
