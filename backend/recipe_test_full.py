"""
8-recipe multi-store grocery list test using LIVE inventory from Pantry Portal.
ZIP 11236 — Brooklyn-appropriate recipes, 8 rounds, OCR.space primary extraction.

Recipes range from everyday to extravagant:
  1. Brown Stew Chicken                (everyday Caribbean)
  2. Pasta e Fagioli                   (Italian comfort)
  3. Jerk Pork Shoulder                (BBQ weekend)
  4. Garlic Butter Shrimp & Linguine   (elevated weeknight)
  5. Salmon en Papillote               (healthy elevated)
  6. Oxtail Stew with Rice & Peas      (special occasion)
  7. Whole Roasted Snapper             (celebratory seafood)
  8. Lobster Bisque with Crusty Bread  (extravagant)
"""

import asyncio, sys, time, os, re, json
sys.path.insert(0, ".")

from app.adapters.foodway import FoodwayAdapter
from app.adapters.keyfood import KeyFoodAdapter
from app.adapters.flipp import FlippAdapter
from app.adapters.shoprite import ShopRiteAdapter
from app.adapters import vision_extractor
from app.locator import geocode, find_stores_near
from app.adapters import registry as adapter_registry
from app.models import Store, CircularItem
import httpx

SHEETS_URL = "https://script.google.com/macros/s/AKfycbx4miCEVM7A84ciqc-Xl7fZiFiQwUSAVHqZRMXdBDokjM7els_PpJhvscIqBHxvqWj3Ww/exec"
ROUNDS = 8

# ── RECIPES ──────────────────────────────────────────────────────────────────
RECIPES = {
    "Brown Stew Chicken": {
        "ingredients": ["chicken", "onion", "garlic", "tomato", "thyme",
                        "scotch bonnet", "soy sauce", "browning", "potato", "carrot"],
        "servings": 4, "level": "Everyday"
    },
    "Pasta e Fagioli": {
        "ingredients": ["pasta", "kidney bean", "tomato", "garlic", "onion",
                        "chicken broth", "olive oil", "parmesan", "spinach", "sausage"],
        "servings": 6, "level": "Comfort"
    },
    "Jerk Pork Shoulder": {
        "ingredients": ["pork", "jerk", "garlic", "ginger", "scallion",
                        "thyme", "lime", "rice", "coleslaw", "mango"],
        "servings": 8, "level": "BBQ Weekend"
    },
    "Garlic Butter Shrimp & Linguine": {
        "ingredients": ["shrimp", "pasta", "butter", "garlic", "lemon",
                        "tomato", "olive oil", "white wine", "parsley", "parmesan"],
        "servings": 4, "level": "Elevated Weeknight"
    },
    "Salmon en Papillote": {
        "ingredients": ["salmon", "lemon", "asparagus", "tomato", "garlic",
                        "olive oil", "thyme", "white wine", "butter", "capers"],
        "servings": 4, "level": "Healthy Elevated"
    },
    "Oxtail Stew with Rice & Peas": {
        "ingredients": ["oxtail", "kidney bean", "coconut milk", "scotch bonnet",
                        "rice", "garlic", "thyme", "carrot", "potato", "browning"],
        "servings": 6, "level": "Special Occasion"
    },
    "Whole Roasted Snapper": {
        "ingredients": ["snapper", "lemon", "garlic", "thyme", "scotch bonnet",
                        "tomato", "olive oil", "scallion", "butter", "rice"],
        "servings": 4, "level": "Celebratory Seafood"
    },
    "Lobster Bisque": {
        "ingredients": ["lobster", "shrimp", "butter", "onion", "garlic",
                        "tomato", "cream", "white wine", "thyme", "bread"],
        "servings": 6, "level": "Extravagant"
    },
}


async def pull_inventory():
    """Pull current Pantry Portal inventory — skip items already in stock."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            rp = await c.get(f"{SHEETS_URL}?action=read&sheet=Products")
            ri = await c.get(f"{SHEETS_URL}?action=read&sheet=Inventory")

        dp = rp.json()
        di = ri.json()

        prod_headers = dp.get("headers", [])
        prod_rows = dp.get("rows", [])
        products = {}
        for row in prod_rows:
            obj = {prod_headers[i]: row[i] for i in range(min(len(prod_headers), len(row)))}
            pid = str(obj.get("ProductID", "")).strip()
            name = str(obj.get("name", "")).strip().lower()
            if pid and name:
                products[pid] = name

        inv_headers = di.get("headers", [])
        inv_rows = di.get("rows", [])
        in_stock = set()
        for row in inv_rows:
            obj = {inv_headers[i]: row[i] for i in range(min(len(inv_headers), len(row)))}
            pid = str(obj.get("ProductID", "")).strip()
            try:
                qty = int(float(obj.get("Current Quantity", 0)))
            except Exception:
                qty = 0
            if qty > 0 and pid in products:
                in_stock.add(products[pid])

        return in_stock
    except Exception as e:
        print(f"  Warning: could not pull inventory ({e}) — proceeding without")
        return set()


def in_inventory(inventory: set, keyword: str) -> bool:
    kw = keyword.lower()
    return any(kw in item for item in inventory)


def find_best(circulars: dict, keyword: str):
    kw = keyword.lower()
    best_deal = None
    best_store = None
    best_val = 9999.0
    for store_name, items in circulars.items():
        for it in items:
            name = (it.name if isinstance(it, CircularItem) else it.get("name", "")) or ""
            price = (it.price if isinstance(it, CircularItem) else it.get("price", "")) or ""
            if kw in name.lower() and price:
                nums = re.findall(r"\d+\.?\d*", price)
                val = float(nums[-1]) if nums else 9999.0
                if val < best_val:
                    best_val = val
                    best_deal = {"name": name[:40], "price": price}
                    best_store = store_name
    return best_deal, best_store


async def fetch_all_circulars():
    """Fetch all 4 store circulars once — cached after first call."""
    coords = await geocode("11236")
    stores = await find_stores_near(coords[0], coords[1], radius_m=6000)
    for s in stores:
        a = adapter_registry.find_for(s)
        if a:
            s.adapter = a.name

    found = {}
    for s in stores:
        n = ((s.name or "") + (s.brand or "")).lower()
        if ("shoprite" in n or "shop rite" in n) and "shoprite" not in found: found["shoprite"] = s
        if "foodway" in n and "foodway" not in found: found["foodway"] = s
        if ("key food" in n or "keyfood" in n) and "keyfood" not in found: found["keyfood"] = s
        if ("c-town" in n or "ctown" in n) and "ctown" not in found: found["ctown"] = s

    circulars = {}

    fw_store = found.get("foodway") or Store(id="manual:foodway", name="Foodway", brand="Foodway", lat=40.6251127, lon=-73.9177145)
    circulars["Foodway"] = await FoodwayAdapter().fetch(fw_store, postal_code="11234")

    kf_store = found.get("keyfood") or Store(id="kf:ralph", name="Key Food", brand="Key Food", lat=40.6331, lon=-73.9190)
    circulars["Key Food"] = await KeyFoodAdapter().fetch(kf_store, postal_code="11234")

    ct_store = found.get("ctown") or Store(id="ct:flatlands", name="C-Town", brand="C-Town", lat=40.6358, lon=-73.9025)
    circulars["C-Town"] = await FlippAdapter().fetch(ct_store, postal_code="11236")

    sr_store = found.get("shoprite") or Store(id="rp:289", name="ShopRite Gateway", brand="ShopRite", lat=40.6404, lon=-73.9011, ref="289")
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                 "Referer": "https://app.redpepper.digital/a10/publications/home/4573"},
        timeout=20, follow_redirects=True,
    ) as client:
        _, cats = await ShopRiteAdapter()._find_store_catalogues(client, sr_store)
        if cats:
            nid = cats[0][0]
            vision_extractor.clear_cache(nid)
            page_urls = await ShopRiteAdapter()._fetch_page_image_urls(client, nid)
            sr_raw = await vision_extractor.extract_deals(page_urls, cache_key=nid)
            circulars["ShopRite"] = [
                CircularItem(name=d["name"], price=d.get("price"), unit=d.get("unit"), category="General")
                for d in sr_raw
            ]
        else:
            circulars["ShopRite"] = []

    return circulars, found


async def main():
    print("Pulling live inventory from Pantry Portal...")
    inventory = await pull_inventory()
    print(f"  {len(inventory)} items currently in stock")

    # Show what's already covered
    all_ingredients = {ing for r in RECIPES.values() for ing in r["ingredients"]}
    already_have = {ing for ing in all_ingredients if in_inventory(inventory, ing)}
    print(f"  Already have: {sorted(already_have)}")

    print()
    print("Fetching circulars from 4 stores (OCR.space primary, Gemini fallback)...")
    t0 = time.time()
    circulars, found_stores = await fetch_all_circulars()
    fetch_time = round(time.time() - t0, 1)
    print(f"  Done in {fetch_time}s: " + ", ".join(f"{s}={len(items)} deals" for s, items in circulars.items()))

    # Run 8 rounds (circulars are cached — rounds are fast)
    all_round_results = []
    for rnd in range(1, ROUNDS + 1):
        round_list = []
        for recipe_name, recipe in RECIPES.items():
            for ing in recipe["ingredients"]:
                if in_inventory(inventory, ing):
                    continue  # already have it
                deal, store = find_best(circulars, ing)
                if deal:
                    round_list.append({
                        "recipe": recipe_name,
                        "level": recipe["level"],
                        "ingredient": ing,
                        "name": deal["name"],
                        "price": deal["price"],
                        "store": store,
                    })
        all_round_results.append(round_list)

    # ── PRINT RESULTS ─────────────────────────────────────────────────────
    print()
    print("=" * 76)
    print(f"  8-ROUND RECIPE TEST — ZIP 11236  |  OCR.space Primary")
    print("=" * 76)

    # Per-recipe summary
    for recipe_name, recipe in RECIPES.items():
        total_ing = len(recipe["ingredients"])
        have = sum(1 for ing in recipe["ingredients"] if in_inventory(inventory, ing))
        on_sale_items = [x for x in all_round_results[-1] if x["recipe"] == recipe_name]
        on_sale = len(on_sale_items)
        need_full_price = total_ing - have - on_sale
        mark = "OK" if (have + on_sale) >= total_ing * 0.6 else "LOW"
        print(f"  [{mark}] {recipe_name:<40} ({recipe['level']})")
        print(f"       In stock: {have}  On sale: {on_sale}  Full price: {need_full_price}  / {total_ing} total")
        for item in on_sale_items:
            print(f"       + {item['ingredient']:<16} {item['price']:<12} {item['name'][:34]} @ {item['store']}")
        missing = [ing for ing in recipe["ingredients"]
                   if not in_inventory(inventory, ing) and not any(x["ingredient"] == ing for x in on_sale_items)]
        if missing:
            print(f"       - Not on sale: {', '.join(missing)}")
        print()

    # Multi-store shopping list (last round)
    last = all_round_results[-1]
    by_store = {}
    for item in last:
        by_store.setdefault(item["store"], []).append(item)

    print("=" * 76)
    print("  SHOPPING LIST — Week of June 2  (buy at store with best price)")
    print("=" * 76)
    total_est = 0.0
    for store in ["Foodway", "Key Food", "C-Town", "ShopRite"]:
        items = by_store.get(store, [])
        if not items:
            continue
        print(f"\n  {store}:")
        store_sub = 0.0
        for item in items:
            nums = re.findall(r"\d+\.?\d*", item["price"])
            val = float(nums[-1]) if nums else 0.0
            store_sub += val
            print(f"    [{item['level'][:12]:<12}] {item['ingredient']:<16} {item['price']:<12} {item['name'][:34]}")
        print(f"    Subtotal: ~${store_sub:.2f}")
        total_est += store_sub

    print(f"\n  Already in pantry (skip buying): {sorted(already_have)}")
    print(f"\n  Estimated grocery spend: ~${total_est:.2f}")

    # Consistency check across rounds
    print()
    print("=" * 76)
    print(f"  CONSISTENCY  ({ROUNDS} rounds — deterministic after first fetch)")
    print("=" * 76)
    sizes = [len(r) for r in all_round_results]
    consistent = all(s == sizes[0] for s in sizes)
    print(f"  Items per round: {sizes}")
    print(f"  Consistent: {'YES — 100%' if consistent else 'NO — variability detected'}")

    # Store deal counts summary
    print()
    print("  Store circular deal counts:")
    for store, items in circulars.items():
        print(f"    {store:<12} {len(items):>4} deals")

    print()
    pct = sum(len(r) for r in all_round_results) / (
        sum(len(r["ingredients"]) - sum(1 for i in r["ingredients"] if in_inventory(inventory, i))
            for r in RECIPES.values()) * ROUNDS
    ) * 100
    print(f"  Ingredient-on-sale rate: {pct:.0f}% (of ingredients NOT already in pantry)")
    print()


asyncio.run(main())
