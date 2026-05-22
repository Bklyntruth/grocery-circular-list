# Grocery Circular List

Self-hosted server that finds grocery stores near a given location and returns parsed weekly-circular data for each. Exposes a JSON API plus a minimal web frontend that any device on the same Wi-Fi can open.

## Layout

- `backend/app/`
  - `main.py` — FastAPI app and routes
  - `locator.py` — geocoding (Nominatim) + nearby-store search (Overpass / OpenStreetMap)
  - `models.py` — shared Pydantic schemas
  - `adapters/` — one adapter per circular source; each declares which stores it can fetch for
- `frontend/` — vanilla HTML/JS UI served at `/`

No API keys are required: geocoding and store lookup use OpenStreetMap.

## First-time setup (Windows / PowerShell)

```powershell
cd "C:\Users\tbyer\grocery circular list"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

Then open:

- On the host machine: <http://localhost:8000>
- From a phone or tablet on the same Wi-Fi: `http://<your-pc-ip>:8000` (find the IP with `ipconfig`)

## REST API

- `GET /api/health` &mdash; liveness probe
- `GET /api/stores?location=11236&radius_m=3200` &mdash; list nearby grocery stores. Each result includes an `adapter` field naming the parser that can fetch its circular (or `null`).
- `GET /api/circulars/{store_id}?location=11236` &mdash; circular items for a specific store, or `[]` if no adapter is registered or implemented yet.

## Adding a new circular source

1. Create a file in `backend/app/adapters/` that subclasses `CircularAdapter` and implements `matches(store)` and `async fetch(store)`.
2. Register it in `backend/app/adapters/__init__.py`.

The locator is data-driven: any chain that exists in OpenStreetMap will appear in the results automatically. Adding an adapter only controls whether circular *parsing* is available for that brand.

## Status

- [x] Store locator (any US zip / address)
- [x] Adapter framework
- [x] Flipp adapter &mdash; covers C-Town, Food Bazaar, Foodtown, Food Universe, Key Food, Met (Fresh / Foods), Pioneer, Associated, Gristedes, Fairway (whichever have an active flyer in the requested postal code)
- [x] Foodway adapter (Georgetowne Brooklyn) &mdash; scrapes the current week's PDF link from foodwayshop.com; item-level extraction is future work
- [x] Manual stores registry (`backend/app/extra_stores.py`) for places OSM doesn't have
- [ ] Aldi adapter
- [ ] ShopRite adapter
- [ ] Whole Foods adapter (blocked &mdash; requires logged-in Amazon session)
- [ ] PDF item-level parser (would benefit Foodway and any future PDF chains)
- [ ] Persistent cache (SQLite or in-memory TTL)
