import imaplib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import locator, grocery_list
from .adapters import email_imap
from .adapters import registry as adapter_registry
from .adapters import vision_extractor
from .adapters.foodway import _img_store
from .categorizer import categorize
from .models import Circular, CircularItem, Store


class ImapSource(BaseModel):
    domain: str
    label: str = ""
    startKeyword: str = ""
    endKeyword: str = ""


class ImapParseRequest(BaseModel):
    email_address: str
    password: str
    host: Optional[str] = None
    port: int = 993
    sources: list[ImapSource]
    days_back: int = 8

_ZIP_RE = re.compile(r"\b(\d{5})\b")


def _extract_zip(location: str) -> Optional[str]:
    m = _ZIP_RE.search(location)
    return m.group(1) if m else None


async def _fetch_items(adapter, store: Store, zip_code: str) -> list[CircularItem]:
    """Fetch circular items for a store.

    Primary path: adapter.fetch().
    Fallback: if fetch() returns nothing and the adapter implements
    page_image_urls(), run per-page vision extraction automatically.
    This means any new adapter only needs page_image_urls() to get full
    deal extraction — no separate vision wiring required.
    """
    try:
        items = await adapter.fetch(store, postal_code=zip_code)
    except Exception:
        items = []

    if not items:
        try:
            page_urls = await adapter.page_image_urls(store, postal_code=zip_code)
        except Exception:
            page_urls = []

        if page_urls:
            cache_key = f"{adapter.name}:{store.id}"
            deals = await vision_extractor.extract_deals(page_urls, cache_key=cache_key)
            items = [
                CircularItem(
                    name=d["name"],
                    price=d.get("price"),
                    unit=d.get("unit"),
                    category=categorize(d["name"]),
                )
                for d in deals
                if d.get("name")
            ]

    return items


app = FastAPI(
    title="Grocery Circular List",
    description=(
        "Finds grocery stores near a location and returns weekly circular data. "
        "No API keys required — store lookup uses OpenStreetMap. "
        "Interactive docs: /docs"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/img/{key}")
def get_image(key: str) -> Response:
    entry = _img_store.get(key)
    if not entry:
        raise HTTPException(404, "Image not found or server was restarted — reload the circular")
    data, mime = entry
    return Response(content=data, media_type=mime, headers={"Cache-Control": "public, max-age=604800"})



@app.get("/api/geocode")
async def geocode_location(
    location: str = Query(..., description="Zip code, address, or place name"),
) -> dict:
    coords = await locator.geocode(location)
    if not coords:
        raise HTTPException(404, f"Could not geocode '{location}'")
    lat, lon = coords
    return {"lat": lat, "lon": lon}


@app.get("/api/stores", response_model=list[Store])
async def list_stores(
    location: str = Query(..., description="Zip code, address, or place name"),
    radius_m: int = Query(4800, ge=200, le=20000, description="Search radius in meters (default ≈ 3 mi)"),
) -> list[Store]:
    coords = await locator.geocode(location)
    if not coords:
        raise HTTPException(404, f"Could not geocode '{location}'")
    lat, lon = coords
    stores = await locator.find_stores_near(lat, lon, radius_m)
    for s in stores:
        adapter = adapter_registry.find_for(s)
        if adapter:
            s.adapter = adapter.name
    return stores


@app.get("/api/circulars/{store_id}", response_model=Circular)
async def get_circular(
    store_id: str,
    location: str = Query(..., description="Zip code or address; used to derive postal_code if not given"),
    postal_code: Optional[str] = Query(None, description="5-digit zip; inferred from `location` if omitted"),
    name: Optional[str] = Query(None, description="Store name from /api/stores; skips slow re-lookup when provided"),
    brand: Optional[str] = Query(None, description="Store brand from /api/stores"),
    lat: Optional[float] = Query(None),
    lon: Optional[float] = Query(None),
    ref: Optional[str] = Query(None, description="Adapter-specific store code (e.g. Red Pepper store ID)"),
) -> Circular:
    if name and lat is not None and lon is not None:
        store = Store(id=store_id, name=name, brand=brand, lat=lat, lon=lon, ref=ref)
    else:
        coords = await locator.geocode(location)
        if not coords:
            raise HTTPException(404, f"Could not geocode '{location}'")
        clat, clon = coords
        stores = await locator.find_stores_near(clat, clon, radius_m=20000)
        match = next((s for s in stores if s.id == store_id), None)
        if not match:
            raise HTTPException(404, f"Store '{store_id}' not found near '{location}'")
        store = match

    zip_code = postal_code or _extract_zip(location) or (store.address and _extract_zip(store.address)) or ""

    adapter = adapter_registry.find_for(store)
    items = await _fetch_items(adapter, store, zip_code) if adapter else []
    return Circular(
        store_id=store.id,
        store_name=store.name,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        items=items,
    )


@app.get("/api/circulars", response_model=list[Circular], summary="Fetch all circulars near a location")
async def get_all_circulars(
    location: str = Query(..., description="Zip code, address, or place name"),
    radius_m: int = Query(4800, ge=200, le=20000),
    postal_code: Optional[str] = Query(None),
) -> list[Circular]:
    """
    Convenience endpoint for Pantry Portal and other integrations.
    Returns circulars for every store with a supported adapter near the given location.
    Stores without a parser return an empty items list (still included in response).
    """
    coords = await locator.geocode(location)
    if not coords:
        raise HTTPException(404, f"Could not geocode '{location}'")
    lat, lon = coords
    stores = await locator.find_stores_near(lat, lon, radius_m)
    zip_code = postal_code or _extract_zip(location) or ""

    now = datetime.now(timezone.utc).isoformat()
    results: list[Circular] = []
    seen_adapters: set[str] = set()

    for store in stores:
        adapter = adapter_registry.find_for(store)
        if not adapter:
            continue
        # Deduplicate by adapter name — same chain flyer fetched once
        cache_key = f"{adapter.name}:{zip_code}"
        if cache_key in seen_adapters:
            continue
        seen_adapters.add(cache_key)
        items = await _fetch_items(adapter, store, zip_code)
        if items:
            results.append(Circular(
                store_id=store.id,
                store_name=store.name,
                fetched_at=now,
                items=items,
            ))

    return results


@app.post("/api/emails/detect-imap")
def detect_imap_settings(body: dict) -> dict:
    """Return the auto-detected IMAP host/port for a given email address."""
    addr = body.get("email_address", "")
    host, port = email_imap.detect_imap(addr)
    return {"host": host, "port": port}


@app.post("/api/emails/parse-imap")
def parse_imap_emails(req: ImapParseRequest) -> dict:
    """
    Connect directly to an IMAP server (NetZero, Gmail, Yahoo, etc.),
    find emails from each configured sender, and return parsed deals.
    Credentials never leave the local machine.
    """
    host = req.host or email_imap.detect_imap(req.email_address)[0]
    sources = [s.model_dump() for s in req.sources]
    try:
        results = email_imap.fetch_and_parse(
            host=host,
            port=req.port,
            username=req.email_address,
            password=req.password,
            sources=sources,
            days_back=req.days_back,
        )
        return {"ok": True, "results": results}
    except imaplib.IMAP4.error as exc:
        raise HTTPException(status_code=401, detail=f"Login failed — check your email and password: {exc}")
    except ConnectionRefusedError:
        raise HTTPException(status_code=502, detail=f"Could not reach IMAP server at {host}:{req.port}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------- grocery lists ----------

class ListIn(BaseModel):
    name: str
    id: Optional[str] = None          # client may supply its own ID for sync


class ListItemIn(BaseModel):
    model_config = {"extra": "allow"} # accept qty, boughtQty, product_id, etc.
    store_id: Optional[str] = None
    store_name: Optional[str] = None
    name: str
    price: Optional[str] = None
    unit: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None


@app.get("/api/lists")
def get_all_lists() -> list:
    return grocery_list.get_lists()


@app.get("/api/lists/full")
def get_all_lists_full() -> list:
    """All lists with their items — used for full sync between apps."""
    return grocery_list.get_lists_full()


@app.post("/api/lists")
def create_list(body: ListIn) -> dict:
    return grocery_list.create_list(body.name.strip() or "My List", list_id=body.id)


@app.patch("/api/lists/{list_id}")
def rename_list(list_id: str, body: ListIn) -> dict:
    result = grocery_list.rename_list(list_id, body.name.strip() or "My List")
    if not result:
        raise HTTPException(404, "List not found")
    return result


@app.delete("/api/lists/{list_id}")
def delete_list(list_id: str) -> dict:
    if not grocery_list.delete_list(list_id):
        raise HTTPException(404, "List not found")
    return {"ok": True}


@app.get("/api/lists/{list_id}/items")
def get_list_items(list_id: str) -> list:
    items = grocery_list.get_items(list_id)
    if items is None:
        raise HTTPException(404, "List not found")
    return items


@app.post("/api/lists/{list_id}/items")
def add_list_item(list_id: str, item: ListItemIn) -> dict:
    result = grocery_list.add_item(list_id, item.model_dump())
    if not result:
        raise HTTPException(404, "List not found")
    return result


@app.patch("/api/lists/{list_id}/items/{item_id}")
def toggle_list_item(list_id: str, item_id: str, body: dict) -> dict:
    updated = grocery_list.toggle_item(list_id, item_id, bool(body.get("checked")))
    if not updated:
        raise HTTPException(404, "Item not found")
    return updated


@app.delete("/api/lists/{list_id}/items/{item_id}")
def delete_list_item(list_id: str, item_id: str) -> dict:
    if not grocery_list.remove_item(list_id, item_id):
        raise HTTPException(404, "Item not found")
    return {"ok": True}


@app.put("/api/lists/{list_id}/items")
def replace_list_items(list_id: str, items: list) -> list:
    """Bulk-replace all items in a list — used for full sync from Pantry Portal."""
    result = grocery_list.replace_items(list_id, items)
    if result is None:
        raise HTTPException(404, "List not found")
    return result


@app.delete("/api/lists/{list_id}/items")
def clear_list(list_id: str) -> dict:
    grocery_list.clear_items(list_id)
    return {"ok": True}


# ---------- settings ----------

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@app.get("/api/settings")
def get_settings() -> dict:
    key = os.environ.get("GEMINI_API_KEY", "")
    return {"gemini_key_set": bool(key), "gemini_key_hint": (key[:8] + "..." + key[-4:]) if len(key) > 12 else ""}


@app.post("/api/settings")
def save_settings(body: dict) -> dict:
    new_key = (body.get("gemini_key") or "").strip()
    if not new_key:
        raise HTTPException(400, "gemini_key is required")
    lines = _ENV_FILE.read_text(encoding="utf-8").splitlines() if _ENV_FILE.exists() else []
    new_lines = [l for l in lines if not l.startswith("GEMINI_API_KEY=")]
    new_lines.append(f"GEMINI_API_KEY={new_key}")
    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.environ["GEMINI_API_KEY"] = new_key
    return {"ok": True}


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
