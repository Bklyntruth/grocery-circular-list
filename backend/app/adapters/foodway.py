"""Foodway (Georgetowne Shopping Center, Brooklyn).

Parses the weekly circular PDF published at foodwayshop.com/circular.
Layout: multi-column grid, item descriptions in small text (sz<15) above
large price numbers (sz≥30). Cents/prefix tokens sit at sz 18-29.
"""

import asyncio
import hashlib
import io
import re
from typing import Optional

import httpx
import pdfplumber

from ..categorizer import categorize
from ..models import CircularItem, Store
from .base import CircularAdapter

_SECTION_MAP = {
    "meat": "Meat", "seafod": "Seafood", "seafood": "Seafood",
    "produce": "Produce", "deli": "Deli", "bakery": "Bakery",
    "dairy": "Dairy & Eggs", "frozen": "Frozen", "grocery": "Pantry",
    "kosher": "Kosher", "natural": "Natural & Organic",
}

# key → (bytes, mime_type)  — repopulated on each PDF fetch, lost on restart
_img_store: dict[str, tuple[bytes, str]] = {}

CIRCULAR_PAGE = "https://www.foodwayshop.com/circular"
PDF_RE = re.compile(r"https?://\S+\.pdf", re.I)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_SKIP_CHARS = frozenset({"•", "...", "..", ".", "–", "-", "—", "□", "◆", "★", "●", "�", "·"})
# Matches size/unit tokens: "12 Oz", "1.5 Lb", or standalone unit words
_UNIT_RE = re.compile(
    r"^[\d.\-/]*(oz|fl|lb|lbs|pk|ct|ml|g|kg|btl|can|pkg|qt|gal|cont|pint|doz)\.?$",
    re.I,
)
_ALL_NUM = re.compile(r"^[\d\s./\-%¢$#@!]+$")
_PRICE_TOK = re.compile(r"^\$$|^¢$|^\d+/\$$|^2/\$$|^3/\$$|^5/\$$", re.I)
_URL_PHONE = re.compile(r"\.com|www\.|@|\d{3}-\d{3}|\d{7,}|thru|valid|week|shoprite|foodway|f.?od.?w.?ay", re.I)
_PROMO_RE = re.compile(
    r"\bMIX\s+OR\b|\bMATCH\b|\bCHOICE\b|\bClub\s+Card\b|\bAd['']?l\b|"
    r"\bLimit\s+Ofer\b|\bMust\s+Buy\b|\bExcludes\b|\bAplicable\b|\bDetails\b|"
    r"\bGET\s*\d\b|\bSAVE\b|\bDeposit\b|\bVarieties\b",
    re.I,
)
_HEADER_WORDS = re.compile(
    r"^(grocery|meat|seafod|seafood|produce|deli|bakery|dairy|frozen|kosher|"
    r"natural|organic|plant|chese|cheese|wekly|weekly|special|coupon|"
    r"drink|beverage|from|our|chef|kitchen|sho[pc]|online|delivery|same|day|"
    r"reserve|quantities|locations|typographical|alcoholic|beverages|"
    r"pack|pkg|Pkg|Cont|Btl|Lbs|lbs|lb)$",
    re.I,
)


def _extract_page_images(page) -> list[dict]:
    """Load embedded product photos into memory; return list of {x0,top,x1,bottom,cx,cy,url}."""
    result: list[dict] = []
    seen: set[str] = set()
    for img in page.images:
        w = img["x1"] - img["x0"]
        h = img["bottom"] - img["top"]
        if w < 40 or h < 40 or img.get("imagemask"):
            continue
        try:
            data = img["stream"].get_data()
            if not data or len(data) < 200:
                continue
            is_jpeg = data[:2] == b"\xff\xd8"
            is_png  = data[:4] == b"\x89PNG"
            if not (is_jpeg or is_png):
                continue
            key = hashlib.md5(data).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            mime = "image/jpeg" if is_jpeg else "image/png"
            _img_store[key] = (data, mime)
            cx = (img["x0"] + img["x1"]) / 2
            cy = (img["top"] + img["bottom"]) / 2
            result.append({"x0": img["x0"], "top": img["top"],
                            "x1": img["x1"], "bottom": img["bottom"],
                            "cx": cx, "cy": cy, "url": f"/api/img/{key}"})
        except Exception:
            continue
    return result


def _match_image(col_x: float, prev_y: float, row_y: float,
                 images: list[dict],
                 x_lo: float = 0, x_hi: float = 9999) -> Optional[str]:
    best_dist, best_idx = float("inf"), -1
    item_cy = (prev_y + row_y) / 2
    for i, img in enumerate(images):
        if img["bottom"] < prev_y or img["top"] > row_y + 30:
            continue
        if img["cx"] < x_lo or img["cx"] > x_hi:
            continue
        dist = ((img["cx"] - col_x) ** 2 + (img["cy"] - item_cy) ** 2) ** 0.5
        if dist < best_dist:
            best_dist, best_idx = dist, i
    if best_idx == -1:
        return None
    return images.pop(best_idx)["url"]


def _dedupe(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        if i + 1 < len(s) and s[i] == s[i + 1] and s[i].isalpha():
            out.append(s[i])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _section_boundaries(words: list[dict]) -> list[tuple[float, str]]:
    """Return sorted (y, section_name) pairs from PDF section header words."""
    found: dict[str, float] = {}
    for w in words:
        t = w["text"].lower().strip()
        if t in _SECTION_MAP and t not in found:
            found[t] = w["top"]
    return sorted([(y, _SECTION_MAP[t]) for t, y in found.items()])


def _section_at(y: float, boundaries: list[tuple[float, str]]) -> Optional[str]:
    current = None
    for sec_y, name in boundaries:
        if sec_y <= y:
            current = name
    return current


def _parse_page(words: list[dict], page_height: float,
                images: Optional[list[dict]] = None) -> list[CircularItem]:
    sections = _section_boundaries(words)

    # Skip the top header band and bottom footer band
    top_cut = page_height * 0.07   # ~100pt of headers on a 1449pt page
    bot_cut = page_height * 0.97   # fine print at the very bottom

    large: list[dict] = []  # sz ≥ 30 — dollar amounts or section headers
    small: list[dict] = []  # sz 18-29 — cents / prefix tokens
    item_ws: list[dict] = []  # sz < 15 — item description words

    for w in words:
        sz = w.get("size", 0)
        text = _dedupe(w["text"]).strip()
        if not text or text in _SKIP_CHARS:
            continue
        y = w["top"]
        if y < top_cut or y > bot_cut:
            continue
        entry = {"x": w["x0"], "y": y, "text": text}
        if sz >= 30:
            if re.match(r"^\d+$", text):  # only numeric large text
                large.append({**entry, "sz": sz})
        elif sz >= 18:
            if re.match(r"^\$$|^\d{2}$|^\d+/\$$|^¢$", text):
                small.append(entry)
        elif sz < 15:
            # Exclude price tokens that bleed into item area
            if not _PRICE_TOK.match(text) and text not in _SKIP_CHARS:
                item_ws.append(entry)

    if not large:
        return []

    # Group large price numbers into rows by y proximity
    large.sort(key=lambda w: w["y"])
    rows: list[list[dict]] = []
    cur: list[dict] = [large[0]]
    for pw in large[1:]:
        if pw["y"] - cur[-1]["y"] < 25:
            cur.append(pw)
        else:
            rows.append(cur)
            cur = [pw]
    rows.append(cur)

    results: list[CircularItem] = []
    prev_y = top_cut

    for row in rows:
        row_y = sum(w["y"] for w in row) / len(row)

        # Separate large numbers into column anchors by x gap
        row.sort(key=lambda w: w["x"])
        anchors: list[list[dict]] = []
        col: list[dict] = [row[0]]
        for pw in row[1:]:
            if pw["x"] - col[-1]["x"] < 90:
                col.append(pw)
            else:
                anchors.append(col)
                col = [pw]
        anchors.append(col)

        # Precompute column centers so we can derive per-column x boundaries.
        all_col_xs = [sum(w["x"] for w in a) / len(a) for a in anchors]

        for idx, col_words in enumerate(anchors):
            col_x = all_col_xs[idx]
            x_lo = (all_col_xs[idx - 1] + col_x) / 2 if idx > 0 else 0
            x_hi = (col_x + all_col_xs[idx + 1]) / 2 if idx < len(anchors) - 1 else 9999
            dollar_text = "".join(w["text"] for w in sorted(col_words, key=lambda w: w["x"]))

            # Only handle pure numeric dollar amounts
            if not re.match(r"^\d+$", dollar_text):
                continue

            # Find small tokens within ±90 x and ±35 y of this price anchor
            nearby = [
                s for s in small
                if abs(s["x"] - col_x) < 90 and abs(s["y"] - row_y) < 35
            ]
            nearby.sort(key=lambda w: w["x"])

            prefix = ""
            cents = "99"
            is_cent_price = False
            for tok in nearby:
                t = tok["text"]
                if re.match(r"^\d+/\$$", t):
                    prefix = t  # "2/$", "3/$" etc.
                elif t == "¢":
                    is_cent_price = True
                elif re.match(r"^\d{2}$", t) and not is_cent_price:
                    cents = t

            if is_cent_price:
                price_str = f"{dollar_text}¢"
            elif prefix:
                price_str = f"{prefix}{dollar_text}.{cents}"
            else:
                price_str = f"${dollar_text}.{cents}"

            # Collect item description text in same column, above this price row
            half_w = 85
            candidates = [
                w for w in item_ws
                if abs(w["x"] - col_x) < half_w
                and prev_y <= w["y"] < row_y - 3
            ]
            if not candidates:
                continue

            # Reconstruct lines (group words within 5pt y of each other)
            candidates.sort(key=lambda w: (round(w["y"] / 4), w["x"]))
            lines: list[list[dict]] = []
            cl: list[dict] = [candidates[0]]
            for iw in candidates[1:]:
                if iw["y"] - cl[-1]["y"] < 5:
                    cl.append(iw)
                else:
                    lines.append(cl)
                    cl = [iw]
            lines.append(cl)

            name_parts: list[str] = []
            for line in lines:
                line_text = " ".join(w["text"] for w in sorted(line, key=lambda w: w["x"]))

                # Skip lines with URL/phone/promo content
                if _URL_PHONE.search(line_text) or _PROMO_RE.search(line_text):
                    continue

                if re.search(r"\.{2,}", line_text):
                    # "11 Oz ... Frosted Flakes" → take part after last separator
                    after = re.split(r"\.{2,}", line_text)[-1].strip()
                    parts = [
                        t for t in after.split()
                        if t not in _SKIP_CHARS
                        and not _UNIT_RE.match(t)
                        and not _PRICE_TOK.match(t)
                        and not _ALL_NUM.match(t)
                    ]
                    if parts:
                        name_parts.append(" ".join(parts))
                else:
                    parts = [
                        t for t in line_text.split()
                        if t not in _SKIP_CHARS
                        and not _UNIT_RE.match(t)
                        and not _PRICE_TOK.match(t)
                        and not _ALL_NUM.match(t)
                        and not _HEADER_WORDS.match(t)
                    ]
                    if parts:
                        name_parts.append(" ".join(parts))

            # De-duplicate consecutive identical parts
            unique_parts: list[str] = []
            for p in name_parts:
                if not unique_parts or p != unique_parts[-1]:
                    unique_parts.append(p)

            name = " ".join(unique_parts).strip().lstrip("• ").strip()

            # Quality filters
            if not name or len(name) < 4:
                continue
            if _ALL_NUM.match(name):
                continue
            if _URL_PHONE.search(name):
                continue
            if _PROMO_RE.search(name):
                continue
            # Truncate very long names (cross-column bleed) to first 8 words
            words_in_name = name.split()
            if len(words_in_name) > 10:
                name = " ".join(words_in_name[:10])
            # Skip if the "name" is mostly punctuation or junk chars
            letter_ratio = sum(1 for c in name if c.isalpha()) / max(len(name), 1)
            if letter_ratio < 0.45:
                continue

            img_url = _match_image(col_x, prev_y, row_y, images or [], x_lo, x_hi)
            cat = _section_at(row_y, sections) or categorize(name)
            results.append(CircularItem(name=name, price=price_str, image_url=img_url, category=cat))

        prev_y = row_y

    return results


class FoodwayAdapter(CircularAdapter):
    name = "foodway"

    def matches(self, store: Store) -> bool:
        label = (store.brand or store.name or "").lower()
        return "foodway" in label.replace(" ", "")

    async def fetch(self, store: Store, *, postal_code: str) -> list[CircularItem]:
        # Foodway's PDF can be 15-20 MB — use a generous timeout so slow
        # WiFi connections (tablets, phones) don't hit a read timeout mid-download.
        async with httpx.AsyncClient(
            headers={"User-Agent": UA},
            timeout=httpx.Timeout(connect=15, read=120, write=30, pool=5),
            follow_redirects=True,
        ) as client:
            r = await client.get(CIRCULAR_PAGE)
            r.raise_for_status()
            urls = list(dict.fromkeys(PDF_RE.findall(r.text)))  # deduplicated, order preserved
            if not urls:
                return []

            # Fetch all PDFs concurrently — handles overlap weeks where the site
            # publishes both the expiring and incoming circular at the same time.
            async def _fetch_pdf(url: str) -> bytes:
                r2 = await client.get(url)
                r2.raise_for_status()
                return r2.content

            pdf_bytes_list = await asyncio.gather(
                *[_fetch_pdf(u) for u in urls], return_exceptions=True
            )

        items: list[CircularItem] = []
        for pdf_bytes, pdf_url in zip(pdf_bytes_list, urls):
            if isinstance(pdf_bytes, Exception):
                continue
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    ws = page.extract_words(extra_attrs=["size", "fontname"])
                    page_imgs = _extract_page_images(page)
                    items.extend(_parse_page(ws, page.height, page_imgs))

        # De-duplicate by name+price across all PDFs
        seen: set[str] = set()
        unique: list[CircularItem] = []
        for it in items:
            key = f"{it.name}|{it.price}"
            if key not in seen:
                seen.add(key)
                unique.append(it)

        if not unique:
            unique = [CircularItem(name="Weekly Flyer (PDF)", source_url=urls[0])]

        return unique
