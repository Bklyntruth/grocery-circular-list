"""Pure-data helpers that turn pantry sheet rows into readable lines.

Kept out of main.py so the tests don't drag in pdfplumber, fastapi and the
rest of the HTTP stack. The /api/pantry/* endpoints in main.py are thin
wrappers that read the rows from Google Sheets and hand them to these."""


def low_text(products: list[dict], inventory: list[dict], limit: int = 40) -> str:
    """What Pantry Portal highlights on Home: products with an inventory row
    AND a restock level, whose on-hand quantity is at or below that level.
    Sorted by name; a long list is capped with a '... and N more' line.

    Walking Inventory (not Products) is what the app does, and it matters:
    there are hundreds of stale catalog entries with no stock row. Treating
    those as 'zero on hand' used to report 440 low items on a household
    that really has a few dozen."""
    by_pid = {str(p.get("ProductID")): p for p in products}
    low: list[tuple[str, float, float]] = []
    for inv in inventory:
        pid = str(inv.get("ProductID"))
        p = by_pid.get(pid)
        if not p:
            continue
        try:
            qty = float(inv.get("Current Quantity") or 0)
            minq = float(p.get("min_quantity") or 0)
        except (TypeError, ValueError):
            continue
        if minq <= 0:
            continue
        if qty <= minq:
            name = str(p.get("name") or "Item").strip()
            low.append((name, qty, minq))
    if not low:
        return "Nothing is at its restock level right now."
    low.sort(key=lambda t: t[0].lower())
    lines = [f"{len(low)} item{'s' if len(low) != 1 else ''} at or below the restock level:"]
    for name, q, minq in low[:limit]:
        lines.append(f"  {name} ({q:g} of {minq:g})")
    if len(low) > limit:
        lines.append(f"  ... and {len(low) - limit} more")
    return "\n".join(lines)


def _price_cents(price: str) -> int:
    """Parse "$2.99", "2/$5", "3 for $5" to cents per unit. Returns a very
    large number when a price can't be read, so bad entries sort last."""
    import re
    s = str(price or "").strip().lower()
    if not s:
        return 10 ** 9
    # "2/$5" or "3 for $5" — per unit price.
    m = re.search(r"(\d+)\s*(?:/|for)\s*\$?\s*(\d+(?:\.\d+)?)", s)
    if m:
        n, total = int(m.group(1)), float(m.group(2))
        return int(round(total * 100 / n)) if n else 10 ** 9
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", s)
    return int(round(float(m.group(1)) * 100)) if m else 10 ** 9


def find_text(deals: list[dict], today: str, query: str, limit: int = 10) -> str:
    """Live deals whose product_name shares a word with `query`, sorted by
    cheapest first. Returns a short readable summary so a phone or P³
    bubble can show it as-is."""
    import re
    q_words = {w for w in re.findall(r"[a-z0-9]+", (query or "").lower())
               if len(w) >= 3}
    if not q_words:
        return 'Say what to search for, like "any deals on tuna?" or "best price on soda".'
    live = [d for d in deals
            if not str(d.get("valid_to") or "")[:10] or str(d.get("valid_to"))[:10] >= today]
    hits = []
    for d in live:
        name = str(d.get("product_name") or "").strip()
        if not name:
            continue
        name_words = set(re.findall(r"[a-z0-9]+", name.lower()))
        if not q_words & name_words:
            continue
        cents = _price_cents(d.get("price"))
        hits.append((cents, name, str(d.get("store_name") or "").strip(),
                     str(d.get("price") or "").strip()))
    if not hits:
        return f"No live deals found for {query!r}. Try rescanning or a shorter word."
    hits.sort(key=lambda t: (t[0], t[1].lower()))
    best_c, best_name, best_store, best_price = hits[0]
    good = sum(1 for c, *_ in hits if c < 10 ** 9)
    lead = (f"Best price for {query}: {best_price} at {best_store} ({best_name})"
            if best_c < 10 ** 9 and good
            else f"{len(hits)} match{'es' if len(hits) != 1 else ''} for {query!r}:")
    lines = [lead]
    for cents, name, store, price in hits[:limit]:
        lines.append(f"  {price:>8}  {store:<18}  {name}")
    if len(hits) > limit:
        lines.append(f"  ... and {len(hits) - limit} more")
    return "\n".join(lines)


def deals_text(deals: list[dict], today: str) -> str:
    """Live circular deals grouped by store, biggest first. A deal with no
    end date counts as live; one that ended before `today` (YYYY-MM-DD) is
    dropped."""
    live = [d for d in deals if not str(d.get("valid_to") or "")[:10]
            or str(d.get("valid_to"))[:10] >= today]
    by_store: dict[str, int] = {}
    for d in live:
        name = str(d.get("store_name") or "").strip()
        if name:
            by_store[name] = by_store.get(name, 0) + 1
    if not by_store:
        return "No live circular deals right now — try a rescan."
    ordered = sorted(by_store.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(by_store.values())
    lines = [f"{total} deal{'s' if total != 1 else ''} live across {len(by_store)} store{'s' if len(by_store) != 1 else ''}:"]
    for name, n in ordered:
        lines.append(f"  {n:3d}  {name}")
    return "\n".join(lines)
