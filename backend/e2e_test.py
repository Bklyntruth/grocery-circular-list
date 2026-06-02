import asyncio, sys, time, os
sys.path.insert(0, '.')
os.environ['GEMINI_API_KEY'] = ''  # force regions fallback for ShopRite

from app.adapters.foodway import FoodwayAdapter
from app.adapters.keyfood import KeyFoodAdapter
from app.adapters.flipp import FlippAdapter
from app.adapters.shoprite import ShopRiteAdapter
from app.locator import geocode, find_stores_near
from app.adapters import registry as adapter_registry
from app.models import Store
import httpx

ROUNDS = 8

async def run_round(rnd):
    r = {}

    # 1. STORE DISCOVERY
    try:
        t = time.time()
        coords = await geocode('11236')
        stores = await find_stores_near(coords[0], coords[1], radius_m=6000)
        for s in stores:
            a = adapter_registry.find_for(s)
            if a: s.adapter = a.name
        found = {}
        for s in stores:
            n = ((s.name or '')+(s.brand or '')).lower()
            if ('shoprite' in n or 'shop rite' in n) and 'shoprite' not in found: found['shoprite'] = s
            if 'foodway' in n and 'foodway' not in found: found['foodway'] = s
            if ('key food' in n or 'keyfood' in n) and 'keyfood' not in found: found['keyfood'] = s
            if ('c-town' in n or 'ctown' in n) and 'ctown' not in found: found['ctown'] = s
        missing = [k for k in ['shoprite','foodway','keyfood','ctown'] if k not in found]
        r['discovery'] = {'ok': not missing, 'found': 4-len(missing), 'time': round(time.time()-t,1),
                          'error': f'Missing: {missing}' if missing else None}
    except Exception as e:
        r['discovery'] = {'ok': False, 'found': 0, 'time': 0, 'error': str(e)}
        found = {}

    def get_store(key, fallback):
        return found.get(key) or fallback

    # 2. FOODWAY
    try:
        s = get_store('foodway', Store(id='manual:foodway', name='Foodway', brand='Foodway', lat=40.6251127, lon=-73.9177145))
        t = time.time()
        items = await FoodwayAdapter().fetch(s, postal_code='11234')
        r['foodway'] = {'ok': len(items)>50, 'deals': len(items), 'time': round(time.time()-t,1),
                        'error': f'Only {len(items)} deals' if len(items)<=50 else None, 'items': items}
    except Exception as e:
        r['foodway'] = {'ok': False, 'deals': 0, 'time': 0, 'error': str(e), 'items': []}

    # 3. KEY FOOD
    try:
        s = get_store('keyfood', Store(id='kf:ralph', name='Key Food', brand='Key Food', lat=40.6331, lon=-73.9190))
        t = time.time()
        items = await KeyFoodAdapter().fetch(s, postal_code='11234')
        r['keyfood'] = {'ok': len(items)>10, 'deals': len(items), 'time': round(time.time()-t,1),
                        'error': f'Only {len(items)} deals' if len(items)<=10 else None, 'items': items}
    except Exception as e:
        r['keyfood'] = {'ok': False, 'deals': 0, 'time': 0, 'error': str(e), 'items': []}

    # 4. C-TOWN
    try:
        s = get_store('ctown', Store(id='ct:flatlands', name='C-Town', brand='C-Town', lat=40.6358, lon=-73.9025))
        t = time.time()
        items = await FlippAdapter().fetch(s, postal_code='11236')
        r['ctown'] = {'ok': len(items)>5, 'deals': len(items), 'time': round(time.time()-t,1),
                      'error': f'Only {len(items)} deals — Flipp may not carry C-Town this week' if len(items)<=5 else None,
                      'items': items}
    except Exception as e:
        r['ctown'] = {'ok': False, 'deals': 0, 'time': 0, 'error': str(e), 'items': []}

    # 5. SHOPRITE (regions fallback — no Gemini)
    try:
        s = get_store('shoprite', Store(id='rp:289', name='ShopRite Gateway', brand='ShopRite', lat=40.6404, lon=-73.9011, ref='289'))
        t = time.time()
        async with httpx.AsyncClient(
            headers={'User-Agent':'Mozilla/5.0','Accept':'application/json',
                     'Referer':'https://app.redpepper.digital/a10/publications/home/4573'},
            timeout=20, follow_redirects=True) as client:
            _, cats = await ShopRiteAdapter()._find_store_catalogues(client, s)
            if not cats: raise ValueError('No active circular found')
            nid, meta = cats[0]
            regions = await ShopRiteAdapter()._fetch_regions(client, nid)
            pages = await ShopRiteAdapter()._fetch_page_image_urls(client, nid)
        named = [x for x in regions if x.get('field_product_title')]
        r['shoprite'] = {'ok': len(named)>100, 'circular': meta['title'], 'products': len(named),
                         'pages': len(pages), 'time': round(time.time()-t,1),
                         'error': 'Prices need Gemini (quota resets nightly)' if len(named)>100 else f'Only {len(named)} products',
                         'items': named}
    except Exception as e:
        r['shoprite'] = {'ok': False, 'circular': 'N/A', 'products': 0, 'pages': 0, 'time': 0, 'error': str(e), 'items': []}

    return r


print(f'Running {ROUNDS} rounds across 4 stores in ZIP 11236...')
all_rounds = []
for rnd in range(1, ROUNDS+1):
    sys.stdout.write(f'  Round {rnd}/{ROUNDS}... ')
    sys.stdout.flush()
    r = asyncio.run(run_round(rnd))
    all_rounds.append(r)
    ok = sum(1 for s in ['discovery','foodway','keyfood','ctown','shoprite'] if r[s]['ok'])
    print(f'{ok}/5 passed')

STEPS = [
    ('discovery', 'Store Discovery — 4 stores in ZIP 11236'),
    ('foodway',   'Foodway — 2149 Ralph Ave  (PDF/pdfplumber)'),
    ('keyfood',   'Key Food — 1804 Ralph Ave  (Swiftly API)'),
    ('ctown',     'C-Town — 7924 Flatlands  (Flipp API)'),
    ('shoprite',  'ShopRite — Gateway Plaza  (RedPepper regions)'),
]

print()
print('='*74)
print(f'  RESULTS  ({ROUNDS} rounds)')
print('='*74)
total_ok = total_checks = 0
for step, label in STEPS:
    passes = sum(1 for r in all_rounds if r[step]['ok'])
    pct = passes/ROUNDS*100
    total_ok += passes
    total_checks += ROUNDS
    mark = 'PASS' if pct >= 98 else ('WARN' if pct >= 62 else 'FAIL')
    r = all_rounds[-1][step]
    if step == 'discovery':
        detail = f"{r['found']}/4  {r['time']}s"
    elif step == 'shoprite':
        detail = f"{r['circular']}  {r['products']} products  {r['pages']} pages  {r['time']}s"
    else:
        detail = f"{r['deals']} deals  {r['time']}s"
    errors = list({r[step]['error'] for r in all_rounds if not r[step]['ok'] and r[step].get('error')})
    err_str = '  <- ' + errors[0] if errors else ''
    print(f'  [{mark}] {pct:>5.1f}%  {label:<43} {detail}{err_str}')

print()
print(f'  TOTAL: {total_ok}/{total_checks} checks passed = {total_ok/total_checks*100:.1f}%')

# Price comparison
fw_items = all_rounds[-1]['foodway']['items']
kf_items = all_rounds[-1]['keyfood']['items']
ct_items = all_rounds[-1]['ctown']['items']

def best(items, kw):
    for it in items:
        if kw in (it.name or '').lower() and it.price:
            return (it.name[:32], it.price)
    return None

kws = ['chicken','beef','pork','shrimp','salmon','juice','water','rice','eggs','milk','bread','butter','pasta','bacon','apple']
rows = [(kw, best(fw_items,kw), best(kf_items,kw), best(ct_items,kw)) for kw in kws]
rows = [(kw,fw,kf,ct) for kw,fw,kf,ct in rows if any(x for x in [fw,kf,ct])]

print()
print('='*74)
print('  PRICE COMPARISON  (Foodway / Key Food / C-Town)')
print('='*74)
print(f'  {"ITEM":<10}  {"FOODWAY":<30} {"KEY FOOD":<28} C-TOWN')
print('  ' + '-'*70)
for kw,fw,kf,ct in rows[:14]:
    f2 = f'{fw[1]} {fw[0][:22]}' if fw else '—'
    k2 = f'{kf[1]} {kf[0][:20]}' if kf else '—'
    c2 = f'{ct[1]} {ct[0][:18]}' if ct else '—'
    print(f'  {kw.capitalize():<10}  {f2:<30} {k2:<28} {c2}')

# Grocery list
print()
print('='*74)
print('  GROCERY LIST — "Week of June 2"  (15 items, multi-store)')
print('='*74)
all_deals = []
for it in fw_items:
    all_deals.append((it.name, it.price, 'Foodway'))
for it in kf_items:
    all_deals.append((it.name, it.price, 'Key Food'))
for it in ct_items:
    all_deals.append((it.name, it.price, 'C-Town'))
sr_items = all_rounds[-1]['shoprite']['items']
for it in sr_items[:40]:
    nm = it.get('field_product_title','')
    if nm:
        all_deals.append((nm, 'see circular', 'ShopRite'))

priced = [(n,p,s) for n,p,s in all_deals if p and len(n or '')>5]
list_items = priced[:15]
seen_stores = set()
for i,(n,p,s) in enumerate(list_items, 1):
    print(f'  {i:>2}. {n[:50]:<50} {p:<14} @ {s}')
    seen_stores.add(s)

print()
print(f'  Stores on list: {sorted(seen_stores)}')
print()
