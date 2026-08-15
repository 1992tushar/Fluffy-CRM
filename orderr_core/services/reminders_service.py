"""
Registers & Reminders service (spec: REGISTERS_REMINDERS_REQUIREMENTS.md).

Three registers with one promise — the system never forgets:
  A. Sundries purchase register — item-level small buying with price/vendor/
     cadence memory. STANDALONE from Vasy (owner decision 2026-07-13); holds
     no balances. NOT stock tracking — cadence memory only.
  B. Critical notes — the don't-forget ledger. A note is a memory with a nag,
     never a ledger entry; it touches no AR/analytics number.
  C. Important dates — renewals & recurring maintenance with lead windows.

Everything with a next-action date joins ONE attention feed (§6), consumed by
the Reminders screen, the main-dashboard strip, the daily manager digest, and
a dedicated WhatsApp message (only when non-empty).

Mutating functions follow the analytics_service convention: return an error
string on failure, None on success.
"""
import re
from datetime import date, timedelta
from statistics import median

from sqlalchemy.orm import Session

from orderr_core.models.sundry import SundryItem, SundryPurchase
from orderr_core.models.critical_note import CriticalNote
from orderr_core.models.important_date import ImportantDate
from orderr_core.models.customer import Customer
from orderr_core.services.analytics_service import fmt_inr
from orderr_core.services import google_drive

SUNDRY_CATEGORIES = ["cleaning", "packaging", "stationery", "kitchen", "other"]
DATE_CATEGORIES = ["insurance", "vehicle_service", "license", "amc", "subscription", "other"]
DATE_CATEGORY_LABELS = {
    "insurance": "Insurance", "vehicle_service": "Vehicle service", "license": "License",
    "amc": "AMC", "subscription": "Subscription", "other": "Other",
}
RECURRENCES = ["none", "days", "monthly", "quarterly", "yearly"]

# reorder nudge: needs ≥3 buys and last buy older than cadence + 25% grace
NUDGE_MIN_PURCHASES = 3
NUDGE_GRACE = 1.25


def _name_key(name: str) -> str:
    """Normalized dedup key for sundry item names (same idea as party_key)."""
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def _parse_date(raw) -> date | None:
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _fmt_day(d: date | None) -> str:
    return d.strftime("%d %b %Y") if d else ""


# ── A · Sundries register ───────────────────────────────────────────────────

def _learn_gap(dates: list) -> int | None:
    """Median gap in days between consecutive purchases (needs ≥3 buys)."""
    if len(dates) < NUDGE_MIN_PURCHASES:
        return None
    ds = sorted(dates)
    gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if (b - a).days > 0]
    return round(median(gaps)) if gaps else None


def add_sundry_purchase(db: Session, data: dict, today: date, invoice_file: tuple = None):
    """Record a buy; auto-creates the item on first use (30-second capture —
    only item name + amount are mandatory). Relearns the item's cadence.

    invoice_file, if given, is (bytes, filename, content_type) for an optional
    invoice photo/PDF — uploaded to the dedicated Google Drive account so
    only a pointer (not the bytes) lands in this DB."""
    name = (data.get("item_name") or "").strip()
    key = _name_key(name)
    if not key:
        return "Item name is required."
    try:
        amount = float(str(data.get("amount") or "").replace(",", "").strip())
    except ValueError:
        return "Enter the amount paid."
    if amount <= 0:
        return "Amount must be more than zero."
    pdate = _parse_date(data.get("purchase_date")) or today
    if pdate > today:
        return "Purchase date can't be in the future."

    qty = rate = None
    for field, label in (("qty", "quantity"), ("rate", "rate")):
        raw = str(data.get(field) or "").replace(",", "").strip()
        if raw:
            try:
                val = float(raw)
            except ValueError:
                return f"Invalid {label}."
            if val < 0:
                return f"{label.capitalize()} can't be negative."
            if field == "qty":
                qty = val
            else:
                rate = val

    item = db.query(SundryItem).filter_by(name_key=key).first()
    if item is None:
        category = (data.get("category") or "other").strip().lower()
        item = SundryItem(name=name, name_key=key,
                          category=category if category in SUNDRY_CATEGORIES else "other",
                          unit=(data.get("unit") or "").strip() or None)
        db.add(item)
        db.flush()
    else:
        item.is_active = True
        if (data.get("unit") or "").strip() and not item.unit:
            item.unit = data["unit"].strip()

    invoice_drive_file_id = invoice_drive_link = invoice_filename = None
    if invoice_file:
        file_bytes, filename, content_type = invoice_file
        if not google_drive.is_configured():
            return "Invoice storage isn't set up yet — ask the admin to run the Drive setup script."
        try:
            uploaded = google_drive.upload_invoice(file_bytes, filename, content_type)
        except Exception:
            return "Couldn't upload the invoice — check the connection and try again."
        invoice_drive_file_id = uploaded["file_id"]
        invoice_drive_link = uploaded["view_link"]
        invoice_filename = filename

    db.add(SundryPurchase(
        item_id=item.id, purchase_date=pdate, qty=qty, rate=rate, amount=round(amount, 2),
        vendor=(data.get("vendor") or "").strip() or None,
        paid_via=(data.get("paid_via") or "").strip().lower() or None,
        note=(data.get("note") or "").strip() or None,
        invoice_drive_file_id=invoice_drive_file_id,
        invoice_drive_link=invoice_drive_link,
        invoice_filename=invoice_filename,
    ))
    db.flush()
    dates = [p.purchase_date for p in
             db.query(SundryPurchase.purchase_date).filter_by(item_id=item.id).all()]
    item.typical_gap_days = _learn_gap([d[0] if isinstance(d, tuple) else d for d in dates])
    db.commit()
    return None


def _sundry_nudges(db: Session, today: date) -> list:
    """Items probably due for a re-buy (advisory only)."""
    nudges = []
    for item in db.query(SundryItem).filter_by(is_active=True).all():
        if not item.typical_gap_days:
            continue
        last = (db.query(SundryPurchase).filter_by(item_id=item.id)
                .order_by(SundryPurchase.purchase_date.desc()).first())
        if last is None:
            continue
        overdue_after = round(item.typical_gap_days * NUDGE_GRACE)
        days_since = (today - last.purchase_date).days
        if days_since > overdue_after:
            nudges.append({
                "item_id": item.id, "name": item.name,
                "days_since": days_since, "typical_gap_days": item.typical_gap_days,
                "last_bought": _fmt_day(last.purchase_date),
            })
    nudges.sort(key=lambda n: n["days_since"] - n["typical_gap_days"], reverse=True)
    return nudges


def sundries_overview(db: Session, today: date) -> dict:
    """Register list (per-item stats), recent buys, month category totals."""
    items = db.query(SundryItem).order_by(SundryItem.name).all()
    purchases = (db.query(SundryPurchase)
                 .order_by(SundryPurchase.purchase_date.desc(), SundryPurchase.id.desc()).all())
    by_item = {}
    for p in purchases:
        by_item.setdefault(p.item_id, []).append(p)

    month_start = today.replace(day=1)
    month_total = 0.0
    month_by_cat = {}
    nudge_ids = {n["item_id"] for n in _sundry_nudges(db, today)}

    rows = []
    for item in items:
        ps = by_item.get(item.id, [])
        last = ps[0] if ps else None
        mtd = sum(float(p.amount) for p in ps if p.purchase_date >= month_start)
        rows.append({
            "item_id": item.id, "name": item.name, "category": item.category,
            "unit": item.unit or "", "buys": len(ps),
            "last_date": _fmt_day(last.purchase_date) if last else "",
            "last_amount_fmt": fmt_inr(last.amount) if last else "—",
            "last_rate_fmt": (fmt_inr(last.rate) if last and last.rate is not None else ""),
            "last_vendor": (last.vendor or "") if last else "",
            "gap_days": item.typical_gap_days or "",
            "mtd_fmt": fmt_inr(mtd) if mtd else "—",
            "probably_due": item.id in nudge_ids,
        })
    rows.sort(key=lambda r: (not r["probably_due"], r["name"]))

    item_name = {i.id: i.name for i in items}
    recent = [{
        "id": p.id, "item": item_name.get(p.item_id, "?"),
        "date": _fmt_day(p.purchase_date),
        "qty": (f"{float(p.qty):g}" if p.qty is not None else ""),
        "rate_fmt": fmt_inr(p.rate) if p.rate is not None else "",
        "amount_fmt": fmt_inr(p.amount),
        "vendor": p.vendor or "", "paid_via": p.paid_via or "", "note": p.note or "",
        "invoice_link": p.invoice_drive_link or "",
    } for p in purchases[:30]]

    for p in purchases:
        if p.purchase_date >= month_start:
            month_total += float(p.amount)
            item = next((i for i in items if i.id == p.item_id), None)
            cat = item.category if item else "other"
            month_by_cat[cat] = month_by_cat.get(cat, 0.0) + float(p.amount)

    return {
        "rows": rows, "recent": recent,
        "month_label": today.strftime("%B %Y"),
        "month_total_fmt": fmt_inr(month_total),
        "month_by_cat": [{"category": c, "amount_fmt": fmt_inr(a)}
                         for c, a in sorted(month_by_cat.items(), key=lambda kv: kv[1], reverse=True)],
        "categories": SUNDRY_CATEGORIES,
    }


# ── B · Critical notes ──────────────────────────────────────────────────────

def add_note(db: Session, data: dict, today: date):
    text = (data.get("note") or "").strip()
    if not text:
        return "Write the note first."
    amount = None
    raw_amt = str(data.get("amount") or "").replace(",", "").strip()
    if raw_amt:
        try:
            amount = round(float(raw_amt), 2)
        except ValueError:
            return "Invalid amount."
    customer_id = None
    raw_cid = data.get("customer_id")
    if raw_cid not in (None, "", "null"):
        try:
            customer_id = int(raw_cid)
        except (TypeError, ValueError):
            return "Invalid customer."
        if db.query(Customer).get(customer_id) is None:
            return "Customer not found."
    db.add(CriticalNote(
        note=text, amount=amount, customer_id=customer_id,
        person=(data.get("person") or "").strip() or None,
        event_date=_parse_date(data.get("event_date")) or today,
        follow_up_date=_parse_date(data.get("follow_up_date")),
        priority="high" if data.get("priority") == "high" else "normal",
    ))
    db.commit()
    return None


def close_note(db: Session, note_id: int, status: str, resolution_note: str):
    """Resolve or drop — both require a resolution note (the audit trail)."""
    if status not in ("resolved", "dropped"):
        return "Invalid status."
    if not (resolution_note or "").strip():
        return "Write what happened before closing the note."
    rec = db.query(CriticalNote).get(note_id)
    if rec is None or rec.status != "open":
        return "Open note not found."
    from datetime import datetime, timezone
    rec.status = status
    rec.resolution_note = resolution_note.strip()
    rec.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return None


def snooze_note(db: Session, note_id: int, until, today: date):
    rec = db.query(CriticalNote).get(note_id)
    if rec is None or rec.status != "open":
        return "Open note not found."
    d = _parse_date(until)
    if d is None or d <= today:
        return "Pick a future date to snooze to."
    rec.follow_up_date = d
    db.commit()
    return None


def notes_overview(db: Session, today: date) -> dict:
    cust = {c.id: (c.restaurant_name or c.phone_number or f"#{c.id}")
            for c in db.query(Customer).all()}

    def row(n: CriticalNote) -> dict:
        overdue_days = ((today - n.follow_up_date).days
                        if (n.status == "open" and n.follow_up_date and n.follow_up_date <= today)
                        else None)
        return {
            "id": n.id, "note": n.note,
            "amount_fmt": fmt_inr(n.amount) if n.amount is not None else "",
            "customer_id": n.customer_id,
            "customer": cust.get(n.customer_id, "") if n.customer_id else "",
            "person": n.person or "",
            "event_date": _fmt_day(n.event_date),
            "follow_up": _fmt_day(n.follow_up_date),
            "priority": n.priority, "status": n.status,
            "overdue_days": overdue_days,
            "resolution_note": n.resolution_note or "",
            "resolved_at": n.resolved_at.strftime("%d %b %Y") if n.resolved_at else "",
        }

    notes = db.query(CriticalNote).order_by(CriticalNote.created_at.desc()).all()
    open_rows = [row(n) for n in notes if n.status == "open"]
    # overdue first (most overdue on top), then by follow-up date, dateless last
    open_rows.sort(key=lambda r: (-(r["overdue_days"] if r["overdue_days"] is not None else -1),
                                  r["follow_up"] == "", r["id"]))
    closed_rows = [row(n) for n in notes if n.status != "open"][:20]
    return {
        "open": open_rows, "closed": closed_rows,
        "open_count": len(open_rows),
        "overdue_count": sum(1 for r in open_rows if r["overdue_days"] is not None),
        "customers": sorted(
            ({"id": cid, "name": name} for cid, name in cust.items()),
            key=lambda c: c["name"]),
    }


def open_notes_for_customer(db: Session, customer_id: int, today: date) -> list:
    """Open critical notes linked to one customer — for the analytics profile
    page and the order-posting warning (credit-gate pattern)."""
    rows = (db.query(CriticalNote)
            .filter(CriticalNote.customer_id == customer_id, CriticalNote.status == "open")
            .order_by(CriticalNote.created_at.desc()).all())
    return [{
        "id": n.id, "note": n.note,
        "amount_fmt": fmt_inr(n.amount) if n.amount is not None else "",
        "event_date": _fmt_day(n.event_date),
        "overdue": bool(n.follow_up_date and n.follow_up_date <= today),
        "priority": n.priority,
    } for n in rows]


# ── C · Important dates ─────────────────────────────────────────────────────

def _add_months(d: date, months: int) -> date:
    """Calendar-safe month addition (31 Jan + 1mo → 28/29 Feb)."""
    import calendar
    m = d.year * 12 + (d.month - 1) + months
    y, mo = m // 12, (m % 12) + 1
    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def _next_due(base: date, recurrence: str, recur_days) -> date | None:
    if recurrence == "days":
        return base + timedelta(days=int(recur_days or 0)) if recur_days else None
    if recurrence == "monthly":
        return _add_months(base, 1)
    if recurrence == "quarterly":
        return _add_months(base, 3)
    if recurrence == "yearly":
        return _add_months(base, 12)
    return None


def add_important_date(db: Session, data: dict):
    title = (data.get("title") or "").strip()
    if not title:
        return "Title is required."
    due = _parse_date(data.get("due_date"))
    if due is None:
        return "Pick the due date."
    recurrence = (data.get("recurrence") or "none").strip()
    if recurrence not in RECURRENCES:
        return "Invalid recurrence."
    recur_days = None
    if recurrence == "days":
        try:
            recur_days = int(str(data.get("recur_days") or "").strip())
        except ValueError:
            return "Enter every-how-many days."
        if recur_days <= 0:
            return "Every-N-days must be positive."
    try:
        lead_days = int(str(data.get("lead_days") or "15").strip())
    except ValueError:
        return "Invalid lead days."
    if not (0 <= lead_days <= 365):
        return "Lead days must be 0–365."
    amount = None
    raw_amt = str(data.get("amount_estimate") or "").replace(",", "").strip()
    if raw_amt:
        try:
            amount = round(float(raw_amt), 2)
        except ValueError:
            return "Invalid amount estimate."
    category = (data.get("category") or "other").strip()
    db.add(ImportantDate(
        title=title,
        category=category if category in DATE_CATEGORIES else "other",
        due_date=due, recurrence=recurrence, recur_days=recur_days,
        advance_rule="from_done" if data.get("advance_rule") == "from_done"
                     else "anniversary",
        lead_days=lead_days,
        linked_to=(data.get("linked_to") or "").strip() or None,
        amount_estimate=amount,
        note=(data.get("note") or "").strip() or None,
    ))
    db.commit()
    return None


def mark_date_done(db: Session, date_id: int, today: date):
    """Record completion; recurring items advance by their rule, one-offs
    complete. Anniversary items skip forward past today (marked done late =
    the missed cycles don't queue up)."""
    rec = db.query(ImportantDate).get(date_id)
    if rec is None or rec.status != "active":
        return "Active item not found."
    rec.last_done_on = today
    if rec.recurrence == "none":
        rec.status = "done"
    else:
        base = rec.due_date if rec.advance_rule == "anniversary" else today
        nxt = _next_due(base, rec.recurrence, rec.recur_days)
        if nxt is None:
            rec.status = "done"
        else:
            while rec.advance_rule == "anniversary" and nxt <= today:
                nxt = _next_due(nxt, rec.recurrence, rec.recur_days)
            rec.due_date = nxt
    db.commit()
    return None


def toggle_date_paused(db: Session, date_id: int):
    rec = db.query(ImportantDate).get(date_id)
    if rec is None or rec.status == "done":
        return "Item not found (or already completed)."
    rec.status = "paused" if rec.status == "active" else "active"
    db.commit()
    return None


def dates_overview(db: Session, today: date) -> dict:
    recs = db.query(ImportantDate).order_by(ImportantDate.due_date).all()

    def row(r: ImportantDate) -> dict:
        days_left = (r.due_date - today).days
        return {
            "id": r.id, "title": r.title,
            "category": r.category,
            "category_label": DATE_CATEGORY_LABELS.get(r.category, "Other"),
            "due_date": _fmt_day(r.due_date), "days_left": days_left,
            "overdue": r.status == "active" and days_left < 0,
            "due_soon": r.status == "active" and 0 <= days_left <= r.lead_days,
            "recurrence": r.recurrence, "recur_days": r.recur_days or "",
            "advance_rule": r.advance_rule, "lead_days": r.lead_days,
            "linked_to": r.linked_to or "",
            "amount_fmt": fmt_inr(r.amount_estimate) if r.amount_estimate is not None else "",
            "note": r.note or "", "status": r.status,
            "last_done": _fmt_day(r.last_done_on),
        }

    active = [row(r) for r in recs if r.status == "active"]
    other = [row(r) for r in recs if r.status != "active"]
    return {
        "active": active, "other": other,
        "overdue_count": sum(1 for r in active if r["overdue"]),
        "due_soon_count": sum(1 for r in active if r["due_soon"]),
        "categories": [{"key": k, "label": DATE_CATEGORY_LABELS[k]} for k in DATE_CATEGORIES],
    }


# ── Shared attention feed (§6) ──────────────────────────────────────────────

def attention_feed(db: Session, today: date) -> dict:
    """One merged feed, urgency order: overdue (notes + dates, oldest first) →
    due-soon dates → probably-due sundries. Computed, no table."""
    items = []

    for n in (db.query(CriticalNote)
              .filter(CriticalNote.status == "open",
                      CriticalNote.follow_up_date != None,               # noqa: E711
                      CriticalNote.follow_up_date <= today).all()):
        days = (today - n.follow_up_date).days
        amt = f" ({fmt_inr(n.amount)})" if n.amount is not None else ""
        items.append({
            "kind": "note", "urgency": "overdue", "sort": -days,
            "icon": "📝", "title": n.note + amt,
            "detail": ("follow-up was today" if days == 0 else f"follow-up {days}d overdue"),
            "link": "/dashboard/reminders?tab=notes",
        })

    for r in db.query(ImportantDate).filter(ImportantDate.status == "active").all():
        days_left = (r.due_date - today).days
        label = r.title + (f" · {r.linked_to}" if r.linked_to else "")
        if days_left < 0:
            items.append({
                "kind": "date", "urgency": "overdue", "sort": days_left,
                "icon": "📅", "title": label,
                "detail": f"{-days_left}d overdue (was {_fmt_day(r.due_date)})",
                "link": "/dashboard/reminders?tab=dates",
            })
        elif days_left <= r.lead_days:
            items.append({
                "kind": "date", "urgency": "due", "sort": days_left,
                "icon": "📅", "title": label,
                "detail": ("due today" if days_left == 0 else f"due in {days_left}d ({_fmt_day(r.due_date)})"),
                "link": "/dashboard/reminders?tab=dates",
            })

    for n in _sundry_nudges(db, today):
        items.append({
            "kind": "sundry", "urgency": "nudge", "sort": 0,
            "icon": "🧺", "title": n["name"],
            "detail": f"probably due — last bought {n['days_since']}d ago "
                      f"(usually every ~{n['typical_gap_days']}d)",
            "link": "/dashboard/reminders?tab=sundries",
        })

    order = {"overdue": 0, "due": 1, "nudge": 2}
    items.sort(key=lambda i: (order[i["urgency"]], i["sort"]))
    overdue = sum(1 for i in items if i["urgency"] == "overdue")
    due = sum(1 for i in items if i["urgency"] == "due")
    nudge = sum(1 for i in items if i["urgency"] == "nudge")
    return {"items": items, "count": len(items),
            "overdue": overdue, "due_soon": due, "nudges": nudge}


def attention_message(db: Session, today: date) -> str | None:
    """WhatsApp text for the dedicated daily nag — None when the feed is empty
    (no empty-feed spam)."""
    from orderr_core.config import PLANT_NAME
    feed = attention_feed(db, today)
    if not feed["items"]:
        return None
    lines = [f"📌 {PLANT_NAME} — Needs attention", today.strftime("%d %b %Y"), ""]
    section = None
    heads = {"overdue": "🔴 Overdue:", "due": "🟡 Coming up:", "nudge": "🧺 Probably due to buy:"}
    for it in feed["items"][:25]:
        if it["urgency"] != section:
            section = it["urgency"]
            if lines[-1] != "":
                lines.append("")
            lines.append(heads[section])
        lines.append(f"  • {it['title']} — {it['detail']}")
    if feed["count"] > 25:
        lines.append(f"  …and {feed['count'] - 25} more")
    lines.append("")
    lines.append("Open: /dashboard/reminders")
    return "\n".join(lines)


def attention_digest_lines(db: Session, today: date) -> list:
    """Short section for the daily manager digest (counts + worst three)."""
    feed = attention_feed(db, today)
    if not feed["items"]:
        return []
    bits = []
    if feed["overdue"]:
        bits.append(f"{feed['overdue']} overdue")
    if feed["due_soon"]:
        bits.append(f"{feed['due_soon']} due soon")
    if feed["nudges"]:
        bits.append(f"{feed['nudges']} to re-buy")
    lines = [f"📌 Reminders: {' · '.join(bits)}"]
    for it in feed["items"][:3]:
        lines.append(f"  • {it['title']} — {it['detail']}")
    return lines
