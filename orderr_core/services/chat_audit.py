"""
chat_audit.py — audit a customer's WhatsApp chat export against Vasy entries.

Flow (Customer Edit tab):
  1. parse_chat()      — WhatsApp "Export chat" .txt (or .zip containing it) →
                         list of messages (date, time, sender, text, is_media).
  2. run_audit()       — keeps only messages AFTER the customer's last audit
                         watermark, extracts
                           • order lines   (re-uses the production order parser)
                           • payment mentions (amount + payment cue word)
                         and compares them to what Vasy has for that customer:
                           • vasy_sales_items  (per day, per product)
                           • customer_receipts (money in)
  3. The caller saves a CustomerChatAudit row; `audited_through` becomes the
     last COMPLETE day in the file (never today — today's orders may simply not
     be punched yet), so the next run starts the day after.

Heuristics are deliberately conservative and always show the source chat text
beside the finding so a human makes the final call.
"""
import io
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from orderr_core.constants import IST, RESET_HOUR, UNIT_AMBIGUOUS_MARKER
from orderr_core.models.customer import Customer
from orderr_core.models.customer_receipt import CustomerReceipt
from orderr_core.models.vasy_sales_item import VasySalesItem

QTY_TOLERANCE_PCT = 0.10     # billed within 10% of ordered counts as matching
QTY_TOLERANCE_ABS = 0.25     # …or within a quarter kg/piece for small orders
RECEIPT_WINDOW_BEFORE = 1    # receipt may be dated up to 1 day before the message
RECEIPT_WINDOW_AFTER = 7     # …or up to 7 days after (entry is often punched later)
MIN_PAYMENT_AMOUNT = 100

# ── Chat parsing ──────────────────────────────────────────────────────────────

_MSG_RE = re.compile(
    r"^\[?(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4}),?\s+"
    r"(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*([APap]\.?[Mm]\.?)?\]?\s*(?:-\s*)?(.*)$"
)
_MEDIA_RE = re.compile(
    r"<media omitted>|<attached:|\b(image|video|audio|sticker|document|gif|photo)\s+omitted\b"
    r"|\(file attached\)|\.(jpg|jpeg|png|opus|mp4|pdf|webp)\b",
    re.IGNORECASE,
)
_SKIP_TEXT_RE = re.compile(
    r"this message was deleted|you deleted this message|waiting for this message"
    r"|messages and calls are end-to-end encrypted",
    re.IGNORECASE,
)


def _clean(line: str) -> str:
    return (line.replace("‎", "").replace("‏", "").replace(" ", " ")
            .replace(" ", " ").replace("﻿", "").rstrip("\r\n"))


def read_chat_text(filename: str, data: bytes) -> str:
    """Return the chat text from an uploaded .txt, or the first .txt in a .zip."""
    if (filename or "").lower().endswith(".zip") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not names:
                raise ValueError("No .txt chat file found inside the zip.")
            data = zf.read(sorted(names, key=lambda n: -zf.getinfo(n).file_size)[0])
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def parse_chat(text: str) -> list:
    """WhatsApp export text → [{dt, sender, text, media}] in file order.

    Handles Android ("12/03/2026, 4:15 pm - Name: msg") and iOS
    ("[12/03/2026, 4:15:23 PM] Name: msg") layouts, multi-line messages, and
    auto-detects day-first vs month-first dates (defaults to day-first)."""
    raw = []   # (a, b, y, hh, mm, ampm, rest)
    for line in text.split("\n"):
        line = _clean(line)
        m = _MSG_RE.match(line)
        if m:
            a, b, y, hh, mm, _ss, ap, rest = m.groups()
            raw.append([int(a), int(b), int(y), int(hh), int(mm), ap, rest])
        elif raw:
            raw[-1][6] += "\n" + line          # continuation of previous message

    # Default day-first (India); switch only if the 2nd number is provably the day.
    month_first = any(r[1] > 12 for r in raw) and not any(r[0] > 12 for r in raw)

    msgs = []
    for a, b, y, hh, mm, ap, rest in raw:
        day, mon = (b, a) if month_first else (a, b)
        y = y + 2000 if y < 100 else y
        if ap:
            ap = ap.lower().replace(".", "")
            if ap == "pm" and hh < 12:
                hh += 12
            elif ap == "am" and hh == 12:
                hh = 0
        try:
            dt = datetime(y, mon, day, hh, mm)
        except ValueError:
            continue
        if ": " in rest:
            sender, body = rest.split(": ", 1)
        else:
            continue                            # system line (joined, created group…)
        body = body.strip()
        if not body or _SKIP_TEXT_RE.search(body):
            continue
        msgs.append({"dt": dt, "sender": sender.strip(), "text": body,
                     "media": bool(_MEDIA_RE.search(body))})
    return msgs


# ── Extraction ────────────────────────────────────────────────────────────────

_PAY_CUE_RE = re.compile(
    r"\b(paid|pay|payment|sent|send|transfer(?:red)?|gpay|g\s?pay|phonepe|paytm|upi|neft|rtgs|imps|"
    r"cheque|check|cash|received|recd|rec|credited|deposit(?:ed)?|screenshot|done|clear(?:ed)?|"
    r"bhej\w*|diye|diya|kele|kela|pathv\w*|payment\s*kel\w*)\b|₹|\brs\.?\b|पाठव|भेज|दिया|दिले|पेमेंट",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d{1,3}(?:,\d{2,3})+|\d{3,7})(?:\.\d{1,2})?(?:\s*/-)?", re.IGNORECASE)
_PHONE_LIKE_RE = re.compile(r"\b\d{10}\b|\b\d{12}\b")


def _business_date(dt: datetime) -> date:
    """Order messages sent after the daily cut-off are for the next day."""
    return dt.date() + timedelta(days=1) if dt.hour >= RESET_HOUR else dt.date()


def _extract_order_items(db: Session, phone: Optional[str], text: str) -> tuple:
    """(items, unresolved_lines) using the production order parser. Never raises."""
    from orderr_core.services.template_parser import parse_template_order, GRAM_UNITS
    try:
        parsed = parse_template_order(phone or "", text, db=db)
    except Exception:
        return [], []
    items = []
    for it in parsed.get("items", []):
        qty = float(it.get("quantity") or 0)
        unit = (it.get("unit") or "kg").lower()
        if unit in GRAM_UNITS:
            qty, unit = qty / 1000.0, "kg"
        elif unit == UNIT_AMBIGUOUS_MARKER:
            unit = "kg"
        if qty > 0:
            items.append({"product": it["product"], "qty": qty, "unit": unit})
    unresolved = [u for u in (parsed.get("unclear_items") or [])
                  if isinstance(u, str) and not u.startswith("__qty_ambiguous__")]
    return items, unresolved


def _extract_payment_amounts(text: str) -> list:
    if not _PAY_CUE_RE.search(text):
        return []
    cleaned = _PHONE_LIKE_RE.sub(" ", text)
    out = []
    for m in _AMOUNT_RE.finditer(cleaned):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if v >= MIN_PAYMENT_AMOUNT:
            out.append(v)
    return out


# ── Vasy side ─────────────────────────────────────────────────────────────────

def _billed_by_day(db: Session, customer_id: int, d0: date, d1: date) -> dict:
    """{date: {product: {"qty", "vouchers": set}}} — net of sales returns."""
    from orderr_core.services.template_parser import short_product_name
    rows = (db.query(VasySalesItem)
            .filter(VasySalesItem.customer_id == customer_id,
                    VasySalesItem.invoice_date >= d0,
                    VasySalesItem.invoice_date <= d1)
            .all())
    out = defaultdict(lambda: defaultdict(lambda: {"qty": 0.0, "vouchers": set()}))
    for r in rows:
        if not r.invoice_date:
            continue
        sign = -1.0 if "return" in (r.sale_type or "").lower() else 1.0
        prod = short_product_name(r.product_name or "")
        slot = out[r.invoice_date][prod]
        slot["qty"] += sign * float(r.qty or 0)
        if r.voucher_no:
            slot["vouchers"].add(r.voucher_no)
    return out


def _qty_matches(ordered: float, billed: float) -> bool:
    diff = abs(ordered - billed)
    return diff <= QTY_TOLERANCE_ABS or diff <= QTY_TOLERANCE_PCT * max(ordered, billed)


def _fmt_qty(q: float) -> str:
    return ("%g" % round(q, 3))


def _inr(v: float) -> str:
    return "₹{:,.0f}".format(v)


# ── Main entry ────────────────────────────────────────────────────────────────

def last_audit_watermark(db: Session, customer_id: int) -> Optional[date]:
    from orderr_core.models.customer_chat_audit import CustomerChatAudit
    row = (db.query(CustomerChatAudit)
           .filter(CustomerChatAudit.customer_id == customer_id,
                   CustomerChatAudit.audited_through.isnot(None))
           .order_by(CustomerChatAudit.audited_through.desc(), CustomerChatAudit.id.desc())
           .first())
    return row.audited_through if row else None


def run_audit(db: Session, customer: Customer, messages: list, our_names: list,
              since: Optional[date], today: date) -> dict:
    """Audit `messages` (already parsed) for `customer`. Only messages dated
    strictly after `since` are considered. Returns a JSON-serialisable result;
    persisting the run / watermark is the caller's job."""
    our = {n.strip().lower() for n in our_names if n.strip()}
    in_scope = [m for m in messages if since is None or m["dt"].date() > since]
    # Today's messages stay for the next run — their entries may not be punched yet.
    complete = [m for m in in_scope if m["dt"].date() < today]
    deferred = len(in_scope) - len(complete)

    result = {
        "customer": customer.restaurant_name,
        "since": since.isoformat() if since else None,
        "messages_in_file": len(messages),
        "messages_considered": len(complete),
        "deferred_today": deferred,
        "from_date": None, "to_date": None,
        "orders": [], "payments": [], "extras": [], "unresolved": [],
        "media_days": [], "summary": {},
    }
    if not complete:
        result["summary"] = {k: 0 for k in
                             ("order_days", "order_lines", "ok", "missing", "qty_mismatch",
                              "no_invoice_day", "payments", "payments_missing", "extras")}
        return result

    d0 = min(m["dt"].date() for m in complete)
    d1 = max(m["dt"].date() for m in complete)
    result["from_date"], result["to_date"] = d0.isoformat(), d1.isoformat()

    # ---- chat side --------------------------------------------------------
    ordered = defaultdict(lambda: defaultdict(lambda: {"qty": 0.0, "unit": "kg", "src": []}))
    pay_seen, payments = set(), []
    media_by_day = defaultdict(int)
    unresolved = []
    for m in complete:
        d = m["dt"].date()
        if m["media"]:
            media_by_day[d] += 1
            continue
        txt = m["text"]
        is_ours = m["sender"].strip().lower() in our
        for amt in _extract_payment_amounts(txt):
            key = (d, amt)
            if key not in pay_seen:
                pay_seen.add(key)
                payments.append({"date": d, "amount": amt, "sender": m["sender"],
                                 "text": txt[:160], "time": m["dt"].strftime("%I:%M %p")})
        if is_ours:
            continue                               # our confirmations aren't orders
        items, unres = _extract_order_items(db, customer.phone_number, txt)
        for u in unres:
            unresolved.append({"date": d.isoformat(), "sender": m["sender"], "text": u[:140]})
        if items:
            bd = _business_date(m["dt"])
            for it in items:
                slot = ordered[bd][it["product"]]
                slot["qty"] += it["qty"]
                slot["unit"] = it["unit"]
                slot["src"].append(f'{m["dt"].strftime("%d %b %I:%M %p")} {m["sender"]}: {txt[:90]}')

    # ---- Vasy side --------------------------------------------------------
    b0 = d0 - timedelta(days=1)
    b1 = d1 + timedelta(days=2)
    billed = _billed_by_day(db, customer.id, b0, b1)

    ok = missing = mismatch = no_inv = 0
    order_rows = []
    for bd in sorted(ordered):
        day_billed = billed.get(bd, {})
        # An order is also "covered" if punched on the adjacent day (late/early billing).
        for prod, o in sorted(ordered[bd].items()):
            b = day_billed.get(prod)
            alt = None
            if not b:
                for off in (-1, 1):
                    cand = billed.get(bd + timedelta(days=off), {}).get(prod)
                    if cand and _qty_matches(o["qty"], cand["qty"]):
                        alt = (bd + timedelta(days=off), cand)
                        break
            if b and _qty_matches(o["qty"], b["qty"]):
                status = "ok"; ok += 1
            elif alt:
                status = "ok"; ok += 1; b = alt[1]
            elif b:
                status = "qty_mismatch"; mismatch += 1
            elif not day_billed:
                status = "no_invoice_day"; no_inv += 1
            else:
                status = "missing"; missing += 1
            order_rows.append({
                "date": bd.isoformat(), "product": prod, "unit": o["unit"],
                "ordered": round(o["qty"], 3), "billed": round(b["qty"], 3) if b else 0,
                "vouchers": sorted(b["vouchers"]) if b else [],
                "status": status, "chat": o["src"][:3],
            })
    result["orders"] = order_rows

    # Billed but never mentioned in chat — informational (could be phone orders).
    extras = []
    chat_days = set(ordered)
    for d, prods in sorted(billed.items()):
        if d not in chat_days or not (d0 <= d <= d1 + timedelta(days=1)):
            continue
        for prod, b in prods.items():
            if prod not in ordered[d] and b["qty"] > 0:
                extras.append({"date": d.isoformat(), "product": prod,
                               "billed": round(b["qty"], 3), "vouchers": sorted(b["vouchers"])})
    result["extras"] = extras

    # ---- payments vs receipts --------------------------------------------
    rcpts = (db.query(CustomerReceipt)
             .filter(CustomerReceipt.customer_id == customer.id,
                     CustomerReceipt.receipt_date >= d0 - timedelta(days=RECEIPT_WINDOW_BEFORE),
                     CustomerReceipt.receipt_date <= d1 + timedelta(days=RECEIPT_WINDOW_AFTER))
             .all())
    used = set()
    pay_missing = 0
    pay_rows = []
    for p in sorted(payments, key=lambda x: (x["date"], x["amount"])):
        hit = None
        for r in rcpts:
            if r.id in used or r.receipt_date is None:
                continue
            if abs(float(r.amount) - p["amount"]) <= 1.0 and \
               p["date"] - timedelta(days=RECEIPT_WINDOW_BEFORE) <= r.receipt_date <= p["date"] + timedelta(days=RECEIPT_WINDOW_AFTER):
                hit = r
                break
        if hit:
            used.add(hit.id)
        else:
            pay_missing += 1
        pay_rows.append({
            "date": p["date"].isoformat(), "amount": p["amount"], "amount_fmt": _inr(p["amount"]),
            "sender": p["sender"], "text": p["text"],
            "receipt_no": hit.receipt_no if hit else None,
            "receipt_date": hit.receipt_date.isoformat() if hit else None,
            "status": "ok" if hit else "missing",
        })
    result["payments"] = pay_rows
    result["unresolved"] = unresolved[:60]
    result["media_days"] = [{"date": d.isoformat(), "count": c} for d, c in sorted(media_by_day.items())]
    result["summary"] = {
        "order_days": len(ordered), "order_lines": len(order_rows),
        "ok": ok, "missing": missing, "qty_mismatch": mismatch, "no_invoice_day": no_inv,
        "payments": len(pay_rows), "payments_missing": pay_missing, "extras": len(extras),
    }
    return result


def issues_count(result: dict) -> int:
    s = result.get("summary") or {}
    return (s.get("missing", 0) + s.get("qty_mismatch", 0)
            + s.get("no_invoice_day", 0) + s.get("payments_missing", 0))


def watermark_for(result: dict) -> Optional[date]:
    """Last complete chat day covered by this run (None if nothing was audited)."""
    td = result.get("to_date")
    return date.fromisoformat(td) if td else None
