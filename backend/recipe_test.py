"""
End-to-end recipe-based multi-store grocery list test.
ZIP 11236 — 3 Brooklyn-appropriate recipes, 3 rounds.

Recipes:
  1. Jerk Chicken with Rice & Peas          (everyday Caribbean, moderate)
  2. Garlic Butter Shrimp & Linguine        (elevated weeknight)
  3. Oxtail Stew with Butter Beans          (special occasion, extravagant)
"""

import asyncio, sys, time, os, re
sys.path.insert(0, '.')

from app.adapters.foodway import FoodwayAdapter
from app.adapters.keyfood import KeyFoodAdapter
from app.adapters.flipp import FlippAdapter
from app.adapters.shoprite import ShopRiteAdapter
from app.adapters import vision_extractor
from app.locator import geocode, find_stores_near
from app.adapters import registry as adapter_registry
from app.models import Store, CircularItem
import httpx

RECIPES = {
    "Jerk Chicken with Rice & Peas": [
        "chicken", "rice", "kidney bean", "coconut milk", "scotch bonnet",
        "scallion", "thyme", "garlic", "ginger", "jerk", "lime"
    ],
    "Garlic Butter Shrimp & Linguine": [
        "shrimp", "linguine", "pasta", "butter", "garlic", "lemon",
        "tomato", "white wine", "parsley", "olive oil"
    ],
    "Oxtail Stew with Butter Beans": [
        "oxtail", "potato", "carrot", "butter bean", "lima bean",
        "browning", "onion", "garlic", "scotch bonnet", "thyme", "beef"
    ],
}

ROUNDS = 3


def find_ingredient(items: list, keyword: str):
    kw = keyword.lower()
    for it in items:
        name = (it.name if isinstance(it, CircularItem) else it.get("name", "")) or ""
        price = (it.price if isinstance(it, CircularItem) else it.get("price", "")) or ""
        if kw in name.lower() and price:
            short = name[:36]
            return {"name": short, "price": price}
    return None


async def fetch_all_circulars():
    coords = await geocode("11236")
    stores = await find_stores_near(coords[0], coords[1], radius_m=6000)
    for s in stores:
        a = adapter_registry.find_for(s)
        if a:
            s.adapter = a.name

    found = {}
    for s in stores:
        n = ((s.name or "") + (s.brand or "")).lower()
        if ("shoprite" in n or "shop rite" in n) and "shoprite" not in found:
            found["shoprite"] = s
        if "foodway" in n and "foodway" not in found:
            found["foodway"] = s
        if ("key food" in n or "keyfood" in n) and "keyfood" not in found:
            found["keyfood"] = s
        if ("c-town" in n or "ctown" in n) and "ctown" not in found:
            found["ctown"] = s

    circulars = {}

    # Foodway (PDF — cached after first call)
    fw_store = found.get("foodway") or Store(
        id="manual:foodway", name="Foodway", brand="Foodway",
        lat=40.6251127, lon=-73.9177145)
    circulars["Foodway"] = await FoodwayAdapter().fetch(fw_store, postal_code="11234")

    # Key Food (Swiftly API)
    kf_store = found.get("keyfood") or Store(
        id="kf:ralph", name="Key Food", brand="Key Food",
        lat=40.6331, lon=-73.9190)
    circulars["Key Food"] = await KeyFoodAdapter().fetch(kf_store, postal_code="11234")

    # C-Town (Flipp)
    ct_store = found.get("ctown") or Store(
        id="ct:flatlands", name="C-Town", brand="C-Town",
        lat=40.6358, lon=-73.9025)
    circulars["C-Town"] = await FlippAdapter().fetch(ct_store, postal_code="11236")

    # ShopRite (Gemini → OCR.space fallback)
    sr_store = found.get("shoprite") or Store(
        id="rp:289", name="ShopRite Gateway", brand="ShopRite",
        lat=40.6404, lon=-73.9011, ref="289")
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
            sr_items = await vision_extractor.extract_deals(page_urls, cache_key=nid)
            # Convert to CircularItem-like objects for uniform access
            circulars["ShopRite"] = [
                CircularItem(name=d["name"], price=d.get("price"), unit=d.get("unit"),
                             category="General")
                for d in sr_items
            ]
        else:
            circulars["ShopRite"] = []

    return circulars


async def run_test():
    print("Fetching circulars from all 4 stores...")
    t0 = time.time()
    circulars = await fetch_all_circulars()
    fetch_time = round(time.time() - t0, 1)

    store_counts = {s: len(items) for s, items in circulars.items()}
    print(f"  Fetched in {fetch_time}s: " +
          ", ".join(f"{s}={n}" for s, n in store_counts.items()))

    all_results = []

    for round_num in range(1, ROUNDS + 1):
        print(f"\n{'='*72}")
        print(f"  ROUND {round_num}/{ROUNDS}")
        print(f"{'='*72}")

        grocery_list = []   # {recipe, ingredient, name, price, store}
        round_stats = {}

        for recipe_name, ingredients in RECIPES.items():
            print(f"\n  Recipe: {recipe_name}")
            print(f"  {'─'*60}")
            found_count = 0
            missing = []

            for ing in ingredients:
                best_deal = None
                best_store = None

                # Check all stores, prefer lowest price
                for store_name, items in circulars.items():
                    deal = find_ingredient(items, ing)
                    if deal:
                        # Simple price comparison — prefer numeric lowest
                        nums = re.findall(r"\d+\.?\d*", deal["price"])
                        price_val = float(nums[-1]) if nums else 999
                        if best_deal is None:
                            best_deal = deal
                            best_store = store_name
                        else:
                            best_nums = re.findall(r"\d+\.?\d*", best_deal["price"])
                            best_val = float(best_nums[-1]) if best_nums else 999
                            if price_val < best_val:
                                best_deal = deal
                                best_store = store_name

                if best_deal:
                    found_count += 1
                    grocery_list.append({
                        "recipe": recipe_name,
                        "ingredient": ing,
                        "name": best_deal["name"],
                        "price": best_deal["price"],
                        "store": best_store,
                    })
                    print(f"    {'✓'} {ing:<18} {best_deal['price']:<12} {best_deal['name'][:32]} @ {best_store}")
                else:
                    missing.append(ing)
                    print(f"    {'✗'} {ing:<18} not on sale this week")

            pct = found_count / len(ingredients) * 100
            round_stats[recipe_name] = {"found": found_count, "total": len(ingredients), "pct": pct}
            if missing:
                print(f"    → Not on sale: {', '.join(missing)}")

        all_results.append((grocery_list, round_stats))

    # Summary across rounds
    print(f"\n{'='*72}")
    print(f"  MULTI-STORE GROCERY LIST — Week of June 2")
    print(f"{'='*72}")

    last_list = all_results[-1][0]
    by_store = {}
    for item in last_list:
        by_store.setdefault(item["store"], []).append(item)

    total_cost_map = {}
    for store, items in sorted(by_store.items()):
        print(f"\n  {store}:")
        store_subtotal = []
        for item in items:
            print(f"    • {item['ingredient']:<18} {item['price']:<12} {item['name'][:35]}")
            print(f"      (for: {item['recipe']})")
            nums = re.findall(r"\d+\.?\d*", item["price"])
            if nums:
                store_subtotal.append(float(nums[-1]))
        if store_subtotal:
            total_cost_map[store] = sum(store_subtotal)
            print(f"    ─ Subtotal ~${sum(store_subtotal):.2f}")

    print(f"\n  Estimated Total: ~${sum(total_cost_map.values()):.2f} across {len(total_cost_map)} stores")

    print(f"\n{'='*72}")
    print(f"  RECIPE COVERAGE  ({ROUNDS} rounds avg)")
    print(f"{'='*72}")
    for recipe_name in RECIPES:
        avg_pct = sum(r[1][recipe_name]["pct"] for r in all_results) / ROUNDS
        found_avg = sum(r[1][recipe_name]["found"] for r in all_results) / ROUNDS
        total = list(RECIPES.values())[list(RECIPES.keys()).index(recipe_name)]
        mark = "✓" if avg_pct >= 50 else "~"
        print(f"  {mark} {recipe_name:<42} {avg_pct:>5.0f}% ({found_avg:.0f}/{len(total)} ingredients on sale)")

    total_found = sum(len(r[0]) for r in all_results)
    total_possible = sum(len(v) for v in RECIPES.values()) * ROUNDS
    overall_pct = total_found / total_possible * 100
    print(f"\n  Overall ingredient coverage: {total_found}/{total_possible} = {overall_pct:.0f}%")
    print(f"  Store counts: " + ", ".join(f"{s}={len(items)}" for s, items in store_counts.items()))
    print()


asyncio.run(run_test())
