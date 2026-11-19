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
