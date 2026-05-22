"""
Direct IMAP email parser.
Connects to any IMAP server (NetZero, Gmail, Yahoo, Outlook, etc.) using
the user's credentials, finds emails from configured senders, extracts deals.
"""

import email
import imaplib
import re
from datetime import datetime, timedelta
from typing import Optional

# Auto-detected IMAP settings by account domain
IMAP_PRESETS = {
    # NetZero Free does not support IMAP — only paid (Platinum) accounts do.
    # We still include it so premium users can override manually.
    "netzero.net": ("imap.netzero.net", 993),
    "netzero.com": ("imap.netzero.net", 993),
    "gmail.com":   ("imap.gmail.com", 993),
    "yahoo.com":   ("imap.mail.yahoo.com", 993),
    "ymail.com":   ("imap.mail.yahoo.com", 993),
    "outlook.com": ("imap-mail.outlook.com", 993),
    "hotmail.com": ("imap-mail.outlook.com", 993),
    "live.com":    ("imap-mail.outlook.com", 993),
    "aol.com":     ("imap.aol.com", 993),
    "icloud.com":  ("imap.mail.me.com", 993),
}


def detect_imap(email_address: str) -> tuple[str, int]:
    domain = email_address.split("@")[-1].lower()
    if domain in IMAP_PRESETS:
        return IMAP_PRESETS[domain]
    return (f"imap.{domain}", 993)


def _get_text_body(msg) -> str:
    """Extract plain text from an email.message.Message object."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
        return str(msg.get_payload())
    return ""


def _parse_deals(text: str) -> list[dict]:
    """Regex-based deal extractor: finds product name + price pairs."""
    lines = [
        re.sub(r"\s+", " ", ln).strip()
        for ln in text.splitlines()
        if len(ln.strip()) > 3
    ]
    deals = []
    for line in lines:
        product_name = price = unit = promo = ""

        # "3 for $5" or "2/$3.00"
        mb = re.search(r"(\d+)\s*(?:for|/)\s*\$?([\d.]+)", line, re.I)
        if mb:
            per = float(mb.group(2)) / int(mb.group(1))
            price = f"${per:.2f}"
            promo = f"{mb.group(1)} for ${mb.group(2)}"
            idx = mb.start()
            product_name = (line[:idx] if idx > 0 else line[mb.end():]).strip()
        else:
            # "$3.99" or "$3.99/lb"
            pm = re.search(r"\$?([\d]+\.[\d]{2})\s*(?:/\s*(\w+))?", line)
            if pm:
                price = f"${pm.group(1)}"
                unit = pm.group(2) or ""
                idx = pm.start()
                product_name = (line[:idx] if idx > 0 else line[pm.end():]).strip()

        product_name = re.sub(r"[,;:•*\-–]+$", "", product_name).strip()
        if price and len(product_name) > 1:
            deals.append(
                {"product_name": product_name, "price": price, "unit": unit, "promo": promo}
            )
    return deals


def fetch_and_parse(
    host: str,
    port: int,
    username: str,
    password: str,
    sources: list[dict],
    days_back: int = 8,
) -> list[dict]:
    """
    Connect to IMAP, find emails from each source, parse deals.
    Returns a list of { store, domain, deals, deal_count } dicts.
    """
    since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
    results = []

    with imaplib.IMAP4_SSL(host, port) as mail:
        mail.login(username, password)
        mail.select("inbox")

        for src in sources:
            domain = src.get("domain", "").strip()
            label = src.get("label", "") or domain
            start_kw = src.get("startKeyword") or src.get("start_keyword") or ""
            end_kw   = src.get("endKeyword")   or src.get("end_keyword")   or ""
            if not domain:
                continue

            # Build IMAP search — FROM can be full address or domain fragment
            is_full_addr = "@" in domain
            from_filter = f'FROM "{domain}"' if is_full_addr else f'FROM "@{domain}"'
            _, data = mail.search(None, f"({from_filter} SINCE {since_date})")
            uids = data[0].split() if data[0] else []
            if not uids:
                results.append({"store": label, "domain": domain, "deals": [], "deal_count": 0, "note": "No emails found"})
                continue

            # Fetch the most recent matching email
            _, msg_data = mail.fetch(uids[-1], "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            body = _get_text_body(msg)

            if len(body) < 50:
                results.append({"store": label, "domain": domain, "deals": [], "deal_count": 0, "note": "Email body too short to parse"})
                continue

            # Keyword bounding — only parse the relevant section
            if start_kw and start_kw in body:
                body = body[body.index(start_kw) + len(start_kw):]
            if end_kw and end_kw in body:
                body = body[:body.index(end_kw)]

            deals = _parse_deals(body)
            results.append({"store": label, "domain": domain, "deals": deals, "deal_count": len(deals)})

    return results
