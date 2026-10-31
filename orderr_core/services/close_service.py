"""
5-Day Close & Audit service (P1 — read-only).

Computes a mini period-close over a [from, to] window from data OrdeRR already
holds: WhatsApp-sourced orders/invoices and the Vasy money mirrors (receipts,
outstanding snapshots, purchases, expenses, payments, supplier bills). See
FIVE_DAY_CLOSE_REQUIREMENTS.md for the design.

The close proves three identities (cash is the master check) and auto-generates
an exceptions list so the audit is "review the flagged items", not "rebuild
totals from chat". Everything here is READ-ONLY: no writes, no new tables. The
two inherently-manual numbers — opening cash and physically-counted cash — are
keyed in the browser (client-side gap), so this service never invents them.

Reuses analytics_service helpers (fmt_inr, the range/snapshot/collection
queries) so figures match the analytics pages exactly rather than drifting.
"""
import json
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from orderr_core.models.order import Order
from orderr_core.models.invoice import Invoice
from orderr_core.models.customer import Customer
from orderr_core.models.customer_receipt import CustomerReceipt
from orderr_core.models.outstanding_snapshot import OutstandingSnapshot
from orderr_core.models.vasy_invoice import VasyInvoice
from orderr_core.models.vasy_purchase import VasyPurchase
from orderr_core.models.vasy_expense import VasyExpense
from orderr_core.models.vasy_payment import VasyPayment
from orderr_core.models.vasy_supplier_bill import VasySupplierBill
from orderr_core.models.import_log import ImportLog
from orderr_core.models.rate_unclear import RateUnclearItem
from orderr_core.models.ocr_unmatched import OcrUnmatchedLine
from orderr_core.models.close_period import ClosePeriod
from orderr_core.models.bank_transaction import BankTransaction

from orderr_core.services.analytics_service import (
    fmt_inr,
    _sales_for_range,          # OrdeRR invoices (rupees, kg)
    _vasy_sales_for_range,     # Vasy invoices (rev, count, customers)
    _collections_for_range,    # receipts (total, cash, bank)
    _outstanding_total,        # gross AR at a snapshot date
)

# Default window length (inclusive) when the caller doesn't pass one.
CLOSE_WINDOW_DAYS = 5

# import_logs.entity values that count as an import of each logical stream
# (see vasy_import.py — sales are logged as either sales_invoices or sales_items).
_ENTITY_IMPORT_KEYS = {
    "receipts": {"receipts"},
    "outstanding": {"outstanding"},
    "sales": {"sales_invoices", "sales_items"},
    "purchases": {"purchases"},
    "expenses": {"expenses"},
    "payments": {"payments"},
    "supplier_bills": {"supplier_bills"},
}


# ── window ──────────────────────────────────────────────────────────────────

def default_window(today: date, db: Session = None):
    """Default close window. If a prior close has been signed, start the day
    after it (continuous coverage, no gaps/overlaps); otherwise fall back to the
    last CLOSE_WINDOW_DAYS days. Always ends at `today`."""
    to_date = today
    from_date = today - timedelta(days=CLOSE_WINDOW_DAYS - 1)
    if db is not None:
        prev = last_close(db)
        if prev is not None:
            candidate = prev.to_date + timedelta(days=1)
            if candidate <= to_date:          # ignore if it would be an empty/future window
                from_date = candidate
    return from_date, to_date


# ── close history (P2 — sign-off) ───────────────────────────────────────────

def last_close(db: Session):
    """Most recently signed close (by to_date, then signed_at), or None."""
    return (db.query(ClosePeriod)
            .order_by(ClosePeriod.to_date.desc(), ClosePeriod.signed_at.desc())
            .first())


def close_before(db: Session, from_date: date):
    """The signed close immediately preceding a window (to_date < from_date) —
    the correct source for this window's opening cash, so re-opening an
    already-signed window doesn't show its own counted cash as opening."""
    return (db.query(ClosePeriod)
            .filter(ClosePeriod.to_date < from_date)
            .order_by(ClosePeriod.to_date.desc(), ClosePeriod.signed_at.desc())
            .first())


def opening_cash_default(db: Session):
    """Opening cash for the next close = the last signed close's counted cash
    (continuity). None if never signed or last count wasn't entered."""
    prev = last_close(db)
    return float(prev.counted_cash) if (prev and prev.counted_cash is not None) else None


def recent_closes(db: Session, limit: int = 10) -> list:
    """History rows for the 'Recent closes' table, newest first."""
    rows = (db.query(ClosePeriod)
            .order_by(ClosePeriod.to_date.desc(), ClosePeriod.signed_at.desc())
            .limit(limit).all())
    out = []
    for r in rows:
        out.append({
            "from_fmt": r.from_date.strftime("%d %b"),
            "to_fmt": r.to_date.strftime("%d %b %Y"),
            "counted_cash_fmt": fmt_inr(r.counted_cash) if r.counted_cash is not None else "—",
            "cash_gap_fmt": fmt_inr(r.cash_gap) if r.cash_gap is not None else "—",
            "cash_ties": (r.cash_gap is not None and abs(float(r.cash_gap)) < 1.0),
            "closing_debtors_fmt": fmt_inr(r.closing_debtors) if r.closing_debtors is not None else "—",
            "closing_creditors_fmt": fmt_inr(r.closing_creditors) if r.closing_creditors is not None else "—",
            "exception_count": r.exception_count,
            "warn_count": r.warn_count,
            "signed_by": r.signed_by or "—",
            "signed_at": r.signed_at.strftime("%d %b %Y %I:%M %p") if r.signed_at else "—",
        })
    return out


def record_close(db: Session, from_date: date, to_date: date, today: date,
                 opening_cash, counted_cash, drawings, signed_by: str = None) -> ClosePeriod:
    """Persist (upsert on window) a signed close. Recomputes the close
    server-side so only the manual cash figures come from the caller — the
    computed movement/gaps/exceptions are never client-trusted."""
    close = five_day_close(db, from_date, to_date, today)

    opening = _num(opening_cash)
    counted = _num(counted_cash)
    draw = _num(drawings) or 0.0
    movement = float(close["cash"]["movement"])
    cash_gap = None
    if counted is not None:
        expected = (opening or 0.0) + movement - draw
        cash_gap = round(counted - expected, 2)

    debtors = close["debtors"]
    ar_gap = round(float(debtors["gap"]), 2) if debtors["can_tie"] else None
    closing_debtors = float(debtors["actual_closing"]) if debtors["has_snapshots"] else None
    closing_creditors = float(close["creditors"]["current_ap"]) if close["creditors"]["has_ap"] else None

    exc_summary = [{"title": x["title"], "count": x["count"], "severity": x["severity"]}
                   for x in close["exceptions"]]

    row = (db.query(ClosePeriod)
           .filter(ClosePeriod.from_date == from_date, ClosePeriod.to_date == to_date)
           .first())
    if row is None:
        row = ClosePeriod(from_date=from_date, to_date=to_date)
        db.add(row)
    row.opening_cash = opening
    row.counted_cash = counted
    row.drawings = draw
    row.cash_movement = round(movement, 2)
    row.cash_gap = cash_gap
    row.closing_debtors = closing_debtors
    row.closing_creditors = closing_creditors
    row.ar_gap = ar_gap
    row.exceptions_json = json.dumps(exc_summary)
    row.exception_count = close["exception_count"]
    row.warn_count = close["warn_count"]
    row.signed_by = signed_by
    db.commit()
    return row


def _num(v):
    """Parse a possibly-messy money string/number to float, or None if blank."""
    if v is None:
        return None
    s = str(v).replace(",", "").replace("₹", "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── freshness gate ──────────────────────────────────────────────────────────

def _snapshot_on_or_before(db: Session, d: date):
    """Latest outstanding snapshot_date on or before `d` (None if none)."""
    return (db.query(func.max(OutstandingSnapshot.snapshot_date))
            .filter(OutstandingSnapshot.snapshot_date <= d).scalar())


def _last_import_at(db: Session, keys):
    """Most recent import_logs.imported_at across the given entity keys."""
    return (db.query(func.max(ImportLog.imported_at))
            .filter(ImportLog.entity.in_(list(keys))).scalar())


def freshness(db: Session, to_date: date, today: date) -> dict:
    """Per-stream import freshness. For each Vasy-sourced stream: the newest
    event date present, whether it reaches through `to_date`, and when it was
    last imported. `all_fresh` is False if ANY stream's data stops before the
    window end — the screen warns before presenting the close as authoritative.
    """
    streams = [
        ("Receipts",           CustomerReceipt.receipt_date,   "receipts"),
        ("Outstanding (AR)",   OutstandingSnapshot.snapshot_date, "outstanding"),
        ("Sales invoices",     VasyInvoice.invoice_date,       "sales"),
        ("Purchases",          VasyPurchase.bill_date,         "purchases"),
        ("Expenses",           VasyExpense.expense_date,       "expenses"),
        ("Payments",           VasyPayment.payment_date,       "payments"),
        ("Supplier bills (AP)", VasySupplierBill.bill_date,    "supplier_bills"),
    ]
    rows = []
    all_fresh = True
    for label, dcol, stream in streams:
        latest_event = db.query(func.max(dcol)).scalar()
        covers = latest_event is not None and latest_event >= to_date
        if not covers:
            all_fresh = False
        imported_at = _last_import_at(db, _ENTITY_IMPORT_KEYS[stream])
        rows.append({
            "label": label,
            "stream": stream,
            "latest_event": latest_event.strftime("%d %b %Y") if latest_event else None,
            "covers": covers,
            "imported_at": imported_at.strftime("%d %b %Y %I:%M %p") if imported_at else None,
        })
    return {"rows": rows, "all_fresh": all_fresh}


# ── supplier accounts payable (running-account basis) ──────────────────────

def _running_ap(db: Session):
    """Amount owed to suppliers on a RUNNING-ACCOUNT basis: each supplier's
    total bills minus total payments to that supplier.

    The business pays at supplier level ('Make Payment') and never marks
    individual bills paid, so Vasy's bill-level due/overdue status is not
    meaningful — the real balance is (bills − payments) per supplier. Gross AP =
    sum of positive balances only (suppliers in advance are not netted in),
    mirroring how gross AR treats customers in credit.

    Returns (gross_ap, top_creditors) where top_creditors is a list of
    {vendor, balance, balance_fmt} sorted by balance desc.
    NOTE: accuracy depends on the payments import being complete — a truncated
    payments export understates payments and overstates what's owed.
    """
    bills = dict(db.query(VasySupplierBill.vendor_key,
                          func.coalesce(func.sum(VasySupplierBill.amount), 0))
                 .group_by(VasySupplierBill.vendor_key).all())
    names = dict(db.query(VasySupplierBill.vendor_key, VasySupplierBill.vendor).all())
    pays = dict(db.query(VasyPayment.party_key,
                         func.coalesce(func.sum(VasyPayment.amount), 0))
                .group_by(VasyPayment.party_key).all())
    creditors = []
    for vk, b in bills.items():
        bal = float(b or 0) - float(pays.get(vk, 0) or 0)
        if bal > 1:
            creditors.append({"vendor": names.get(vk) or vk, "balance": round(bal, 2),
                              "balance_fmt": fmt_inr(bal)})
    creditors.sort(key=lambda c: c["balance"], reverse=True)
    gross_ap = sum(c["balance"] for c in creditors)
    return gross_ap, creditors


# ── bank reconciliation (4th check) ─────────────────────────────────────────

def bank_recon(db: Session, from_date: date, to_date: date) -> dict:
    """Reconcile the bank statement (money that actually moved) against what
    Vasy recorded (non-cash receipts vs payments) for the window.

    The bank can't be fudged, so this catches money that entered/left the account
    but was never recorded (charges, missed entries, timing). Totals rarely tie to
    zero — owner transfers, credit-card settlements, EMIs etc. hit the bank but
    aren't sales/purchases — so the gap is a review signal, not a pass/fail.
    """
    has_bank = (db.query(BankTransaction.id)
                .filter(BankTransaction.value_date >= from_date,
                        BankTransaction.value_date <= to_date).first() is not None)

    def _bank_sum(direction):
        return float(db.query(func.coalesce(func.sum(BankTransaction.amount), 0))
                     .filter(BankTransaction.value_date >= from_date,
                             BankTransaction.value_date <= to_date,
                             BankTransaction.direction == direction).scalar() or 0)

    bank_in, bank_out = _bank_sum("cr"), _bank_sum("dr")

    # Vasy non-cash (bank) sides. _collections_for_range returns (total, cash, bank).
    _recv_total, _recv_cash, recv_bank = _collections_for_range(db, from_date, to_date)
    vasy_in = recv_bank
    pay_total = float(db.query(func.coalesce(func.sum(VasyPayment.amount), 0))
                      .filter(VasyPayment.payment_date >= from_date,
                              VasyPayment.payment_date <= to_date).scalar() or 0)
    pay_cash = float(db.query(func.coalesce(func.sum(VasyPayment.amount), 0))
                     .filter(VasyPayment.payment_date >= from_date,
                             VasyPayment.payment_date <= to_date,
                             func.lower(VasyPayment.mode) == "cash").scalar() or 0)
    vasy_out = pay_total - pay_cash

    def _top(direction):
        rows = (db.query(BankTransaction)
                .filter(BankTransaction.value_date >= from_date,
                        BankTransaction.value_date <= to_date,
                        BankTransaction.direction == direction)
                .order_by(BankTransaction.amount.desc()).limit(6).all())
        return [{"desc": (r.description or "")[:48], "amount_fmt": fmt_inr(r.amount),
                 "date": r.value_date.strftime("%d %b")} for r in rows]

    in_gap = bank_in - vasy_in
    out_gap = bank_out - vasy_out
    return {
        "has_bank": has_bank,
        "bank_in": bank_in, "bank_in_fmt": fmt_inr(bank_in),
        "bank_out": bank_out, "bank_out_fmt": fmt_inr(bank_out),
        "vasy_in": vasy_in, "vasy_in_fmt": fmt_inr(vasy_in),
        "vasy_out": vasy_out, "vasy_out_fmt": fmt_inr(vasy_out),
        "in_gap": round(in_gap, 2), "in_gap_fmt": fmt_inr(in_gap),
        "out_gap": round(out_gap, 2), "out_gap_fmt": fmt_inr(out_gap),
        "in_ties": abs(in_gap) < 1.0,
        "out_ties": abs(out_gap) < 1.0,
        "top_out": _top("dr"),
        "top_in": _top("cr"),
    }


# ── tie-outs ────────────────────────────────────────────────────────────────

def _tieouts(db: Session, from_date: date, to_date: date):
    """Compute the three reconciliations for the window. Returns the raw numbers
    plus display strings; the cash tie-out's opening/counted are left for the
    browser (inherently manual) — here we only supply the computed movement."""
    # ── A · Debtors (AR) — Vasy is source of truth for money ──
    # A real tie-out needs an OPENING snapshot at/before the window start AND a
    # closing one — otherwise opening AR is unknown (not zero) and the gap is
    # meaningless. When snapshots don't bracket the window we mark the verdict
    # indeterminate rather than raising a false "doesn't tie" (§9 alignment).
    opening_snap = _snapshot_on_or_before(db, from_date - timedelta(days=1))
    closing_snap = _snapshot_on_or_before(db, to_date)
    opening_ar = _outstanding_total(db, opening_snap)
    closing_ar = _outstanding_total(db, closing_snap)

    vasy_sales, _vc, _vcu = _vasy_sales_for_range(db, from_date, to_date)
    coll_total, coll_cash, coll_bank = _collections_for_range(db, from_date, to_date)

    expected_ar = opening_ar + vasy_sales - coll_total
    ar_gap = closing_ar - expected_ar
    can_tie = opening_snap is not None and closing_snap is not None

    debtors = {
        "opening": opening_ar, "opening_fmt": fmt_inr(opening_ar),
        "opening_as_of": opening_snap.strftime("%d %b %Y") if opening_snap else None,
        "has_opening": opening_snap is not None,
        "sales": vasy_sales, "sales_fmt": fmt_inr(vasy_sales),
        "collections": coll_total, "collections_fmt": fmt_inr(coll_total),
        "expected_closing": expected_ar, "expected_closing_fmt": fmt_inr(expected_ar),
        "actual_closing": closing_ar, "actual_closing_fmt": fmt_inr(closing_ar),
        "actual_as_of": closing_snap.strftime("%d %b %Y") if closing_snap else None,
        "gap": round(ar_gap, 2), "gap_fmt": fmt_inr(ar_gap),
        "can_tie": can_tie,
        "ties": (abs(ar_gap) < 1.0) if can_tie else None,
        "has_snapshots": closing_snap is not None,
    }

    # ── B · Creditors (AP) — running-account basis: what we owe each supplier is
    # (their bills − their payments), NOT the bill-level due/overdue flag (which
    # the business never updates). Window flow (purchases in, payments out) is
    # shown alongside the standing balance.
    purchases = float(db.query(func.coalesce(func.sum(VasyPurchase.total), 0))
                      .filter(VasyPurchase.bill_date >= from_date,
                              VasyPurchase.bill_date <= to_date).scalar() or 0)
    supplier_payments = float(db.query(func.coalesce(func.sum(VasyPayment.amount), 0))
                              .filter(VasyPayment.payment_date >= from_date,
                                      VasyPayment.payment_date <= to_date).scalar() or 0)
    current_ap, top_creditors = _running_ap(db)
    has_ap = db.query(VasySupplierBill.id).first() is not None
    creditors = {
        "purchases": purchases, "purchases_fmt": fmt_inr(purchases),
        "payments": supplier_payments, "payments_fmt": fmt_inr(supplier_payments),
        "net_movement": purchases - supplier_payments,
        "net_movement_fmt": fmt_inr(purchases - supplier_payments),
        "current_ap": current_ap, "current_ap_fmt": fmt_inr(current_ap),
        "top_creditors": top_creditors[:5],
        "has_ap": has_ap,
    }

    # ── C · Cash — computed movement only; opening & counted are manual (JS). ──
    cash_out = float(db.query(func.coalesce(func.sum(VasyPayment.amount), 0))
                     .filter(VasyPayment.payment_date >= from_date,
                             VasyPayment.payment_date <= to_date,
                             func.lower(VasyPayment.mode) == "cash").scalar() or 0)
    cash_movement = coll_cash - cash_out
    cash = {
        "cash_in": coll_cash, "cash_in_fmt": fmt_inr(coll_cash),
        "cash_out": cash_out, "cash_out_fmt": fmt_inr(cash_out),
        "movement": cash_movement, "movement_fmt": fmt_inr(cash_movement),
    }

    # ── Sales source-of-truth reconciliation (OrdeRR vs Vasy) ──
    # Only meaningful when OrdeRR-side invoicing is actually used. If OrdeRR has
    # no invoices for the window (billing done entirely in Vasy), the comparison
    # is not applicable — don't raise a permanent false "differ".
    orderr_sales, _kg = _sales_for_range(db, from_date, to_date)
    sales_diff = orderr_sales - vasy_sales
    orderr_used = orderr_sales > 0
    sales_recon = {
        "orderr": orderr_sales, "orderr_fmt": fmt_inr(orderr_sales),
        "vasy": vasy_sales, "vasy_fmt": fmt_inr(vasy_sales),
        "diff": round(sales_diff, 2), "diff_fmt": fmt_inr(sales_diff),
        "applicable": orderr_used,
        "agrees": (abs(sales_diff) < 1.0) if orderr_used else None,
    }

    return debtors, creditors, cash, sales_recon


# ── exceptions ──────────────────────────────────────────────────────────────

def _delivered_order_ids(db: Session, s: str, e: str):
    return {r[0] for r in db.query(Order.id).filter(
        Order.business_date >= s, Order.business_date <= e,
        Order.is_cancelled == False,                 # noqa: E712
        Order.status == "delivered").all()}


def _exceptions(db: Session, from_date: date, to_date: date, today: date,
                sales_recon: dict, debtors: dict) -> list:
    """Auto-generated review list. Each entry: key, title, count, severity
    ('warn'|'info'), note, and up to a few sample rows. The owner reviews these
    instead of rebuilding the period from chat."""
    s, e = from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d")
    out = []

    cust_by_phone = {c.phone_number: c for c in db.query(Customer).all() if c.phone_number}

    def _order_label(o):
        c = cust_by_phone.get(o.customer_phone)
        return (c.restaurant_name if c and c.restaurant_name else None) or o.customer_phone or f"order #{o.id}"

    # 1 · Delivered but not invoiced (revenue leak)
    delivered = db.query(Order).filter(
        Order.business_date >= s, Order.business_date <= e,
        Order.is_cancelled == False,                 # noqa: E712
        Order.status == "delivered").all()
    invoiced_ids = {r[0] for r in db.query(Invoice.order_id).all()}
    leak = [o for o in delivered if o.id not in invoiced_ids]
    if leak:
        out.append({
            "key": "delivered_not_invoiced", "severity": "warn",
            "title": "Delivered but not invoiced",
            "count": len(leak),
            "note": "Reached delivered but has no invoice — revenue leak.",
            "samples": [{"label": _order_label(o), "detail": o.business_date} for o in leak[:8]],
        })

    # 2 · Ordered but not delivered (stuck / pending)
    stuck = db.query(Order).filter(
        Order.business_date >= s, Order.business_date <= e,
        Order.is_cancelled == False,                 # noqa: E712
        Order.status.in_(["received", "confirmed", "packed"])).all()
    if stuck:
        out.append({
            "key": "ordered_not_delivered", "severity": "warn",
            "title": "Ordered but not delivered",
            "count": len(stuck),
            "note": "Order placed in the window but never reached delivered.",
            "samples": [{"label": _order_label(o), "detail": o.status} for o in stuck[:8]],
        })

    # 3 · Unclear / unparsed orders + rate/OCR queues (never made it to a total)
    unclear_orders = db.query(func.count(Order.id)).filter(
        Order.business_date >= s, Order.business_date <= e,
        Order.is_unclear == True).scalar() or 0                       # noqa: E712
    rate_unclear = db.query(func.count(RateUnclearItem.id)).filter(
        RateUnclearItem.business_date >= from_date,
        RateUnclearItem.business_date <= to_date,
        RateUnclearItem.resolved == False).scalar() or 0              # noqa: E712
    ocr_unmatched = db.query(func.count(OcrUnmatchedLine.id)).filter(
        func.date(OcrUnmatchedLine.created_at) >= s,
        func.date(OcrUnmatchedLine.created_at) <= e,
        OcrUnmatchedLine.resolved == False).scalar() or 0            # noqa: E712
    total_unclear = unclear_orders + rate_unclear + ocr_unmatched
    if total_unclear:
        out.append({
            "key": "unclear", "severity": "warn",
            "title": "Unclear / unresolved items",
            "count": total_unclear,
            "note": (f"{unclear_orders} unclear orders, {rate_unclear} unresolved "
                     f"rate lines, {ocr_unmatched} unmatched OCR lines — resolve so "
                     "they enter the totals."),
            "samples": [],
        })

    # 4 · Invoice with no delivered order (billed something not delivered)
    win_invoices = db.query(Invoice).filter(
        Invoice.business_date >= from_date, Invoice.business_date <= to_date).all()
    delivered_ids = _delivered_order_ids(db, s, e)
    # order_id may point at an order outside the window; only flag when the linked
    # order is missing or is not in a delivered state.
    order_status = {r[0]: (r[1], r[2]) for r in db.query(Order.id, Order.status, Order.is_cancelled).all()}
    orphan_inv = []
    for inv in win_invoices:
        st = order_status.get(inv.order_id)
        if st is None or st[1] != "delivered" or st[0] is None:
            # missing order, or linked order not delivered
            if st is None or st[0] != "delivered":
                orphan_inv.append(inv)
    if orphan_inv:
        out.append({
            "key": "invoice_no_delivery", "severity": "info",
            "title": "Invoice with no delivered order",
            "count": len(orphan_inv),
            "note": "Invoice exists but its order isn't in a delivered state.",
            "samples": [{"label": inv.invoice_number, "detail": inv.customer_phone}
                        for inv in orphan_inv[:8]],
        })

    # 5 · Unattributed receipts (money in, no customer)
    un_rows = db.query(CustomerReceipt).filter(
        CustomerReceipt.receipt_date >= from_date,
        CustomerReceipt.receipt_date <= to_date,
        CustomerReceipt.customer_id == None).all()                    # noqa: E711
    if un_rows:
        un_total = sum(float(r.amount or 0) for r in un_rows)
        out.append({
            "key": "unattributed_receipts", "severity": "info",
            "title": "Unattributed money received",
            "count": len(un_rows),
            "note": f"{fmt_inr(un_total)} received but not linked to a customer "
                    "(cash-customer / walk-in) — confirm attribution.",
            "samples": [{"label": r.party_name or "—", "detail": fmt_inr(r.amount)}
                        for r in un_rows[:8]],
        })

    # 6 · Sales mismatch (OrdeRR vs Vasy) — only when OrdeRR billing is in use
    if sales_recon["applicable"] and not sales_recon["agrees"]:
        out.append({
            "key": "sales_mismatch", "severity": "warn",
            "title": "OrdeRR vs Vasy sales differ",
            "count": None,
            "note": f"OrdeRR billed {sales_recon['orderr_fmt']} vs Vasy "
                    f"{sales_recon['vasy_fmt']} (diff {sales_recon['diff_fmt']}) — "
                    "an order billed in one system but not the other.",
            "samples": [],
        })

    # 7 · AR tie-out gap — only when snapshots bracket the window (a real tie)
    if debtors["can_tie"] and not debtors["ties"]:
        out.append({
            "key": "ar_gap", "severity": "warn",
            "title": "Debtors don't tie",
            "count": None,
            "note": f"Expected closing {debtors['expected_closing_fmt']} vs actual "
                    f"{debtors['actual_closing_fmt']} (gap {debtors['gap_fmt']}).",
            "samples": [],
        })

    # (Bill-level "overdue" is intentionally NOT flagged: the business pays on a
    # running-account basis and never marks individual bills paid, so Vasy's
    # per-bill due/overdue status is meaningless. What's actually owed is the
    # running-account balance shown in the Creditors tie-out.)

    # 9 · Usual suppliers with no purchase this window (derived, until a fixed
    # supplier list is configured — §9). "Usual" = bought from in the prior 30
    # days; flag those absent this window so nothing is silently missed.
    lookback_start = from_date - timedelta(days=30)
    recent_vendors = {k: n for k, n in db.query(VasyPurchase.party_key, VasyPurchase.party_name)
                      .filter(VasyPurchase.bill_date >= lookback_start,
                              VasyPurchase.bill_date < from_date).all() if k}
    window_vendors = {r[0] for r in db.query(VasyPurchase.party_key)
                      .filter(VasyPurchase.bill_date >= from_date,
                              VasyPurchase.bill_date <= to_date).all()}
    missing_suppliers = [n for k, n in recent_vendors.items() if k not in window_vendors]
    if missing_suppliers:
        out.append({
            "key": "supplier_gap", "severity": "info",
            "title": "Usual suppliers — no purchase this window",
            "count": len(missing_suppliers),
            "note": "Bought from these in the prior 30 days but nothing this "
                    "window — confirm a bill wasn't missed (mark Nil if genuinely none).",
            "samples": [{"label": n, "detail": None} for n in sorted(missing_suppliers)[:8]],
        })

    # attach drill-down links to the relevant existing analytics page
    _links = {
        "unattributed_receipts": ("/dashboard/analytics/collections", "Open Collections"),
        "overdue_bills": ("/dashboard/analytics/financials", "Open Financials"),
        "sales_mismatch": ("/dashboard/analytics/reconcile", "Open Reconcile"),
        "ar_gap": ("/dashboard/analytics/receivables", "Open Receivables"),
    }
    for x in out:
        href_label = _links.get(x["key"])
        if href_label:
            x["link"], x["link_label"] = href_label

    return out


# ── entry point ─────────────────────────────────────────────────────────────

def five_day_pnl(db: Session, month_day: date) -> dict:
    """Sales / purchases / expenses / net profit for each calendar 5-day block
    (1–5, 6–10, … 26–month end) of the month containing `month_day`.
    Net profit = sales − purchases − expenses (Vasy bill totals, billed basis)."""
    first = month_day.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    last = nxt - timedelta(days=1)

    def by_day(model, col, amt):
        rows = (db.query(col, func.coalesce(func.sum(amt), 0))
                .filter(col >= first, col <= last).group_by(col).all())
        return {d: float(v or 0) for d, v in rows}

    sales = by_day(VasyInvoice, VasyInvoice.invoice_date, VasyInvoice.total)
    purch = by_day(VasyPurchase, VasyPurchase.bill_date, VasyPurchase.total)
    exps = by_day(VasyExpense, VasyExpense.expense_date, VasyExpense.total)

    rows, tot = [], [0.0, 0.0, 0.0]
    start = first
    while start <= last:
        end = min(start + timedelta(days=4), last) if start.day < 26 else last
        s = p = e = 0.0
        d = start
        while d <= end:
            s += sales.get(d, 0.0)
            p += purch.get(d, 0.0)
            e += exps.get(d, 0.0)
            d += timedelta(days=1)
        net = s - p - e
        rows.append({
            "label": f"{start.day}–{end.day}",
            "sales_fmt": fmt_inr(s), "purchases_fmt": fmt_inr(p),
            "expenses_fmt": fmt_inr(e), "net_fmt": fmt_inr(net), "neg": net < 0,
            "future": start > month_day and not (s or p or e),
        })
        tot[0] += s; tot[1] += p; tot[2] += e
        start = end + timedelta(days=1)
    net = tot[0] - tot[1] - tot[2]
    return {
        "month": first.strftime("%B %Y"), "rows": rows,
        "total": {"sales_fmt": fmt_inr(tot[0]), "purchases_fmt": fmt_inr(tot[1]),
                  "expenses_fmt": fmt_inr(tot[2]), "net_fmt": fmt_inr(net), "neg": net < 0},
    }


def five_day_close(db: Session, from_date: date, to_date: date, today: date) -> dict:
    """Assemble the full close view for the window: freshness, three tie-outs,
    sales reconciliation and the exceptions list. Pure/read-only."""
    debtors, creditors, cash, sales_recon = _tieouts(db, from_date, to_date)
    exceptions = _exceptions(db, from_date, to_date, today, sales_recon, debtors)
    fresh = freshness(db, to_date, today)
    bank = bank_recon(db, from_date, to_date)

    warn_count = sum(1 for x in exceptions if x["severity"] == "warn")

    # continuity: opening cash carries from the close immediately before this window
    prev = close_before(db, from_date)
    opening_cash = float(prev.counted_cash) if (prev and prev.counted_cash is not None) else None
    opening_cash_from = prev.to_date.strftime("%d %b %Y") if prev else None

    # if this exact window was already signed, reload the stored cash figures so
    # the page (and the printable report) is a faithful record of what was signed
    row = (db.query(ClosePeriod)
           .filter(ClosePeriod.from_date == from_date,
                   ClosePeriod.to_date == to_date).first())
    signed = None
    if row is not None:
        if row.opening_cash is not None:
            opening_cash = float(row.opening_cash)
            opening_cash_from = None
        signed = {
            "opening_cash": float(row.opening_cash) if row.opening_cash is not None else None,
            "counted_cash": float(row.counted_cash) if row.counted_cash is not None else None,
            "counted_cash_fmt": fmt_inr(row.counted_cash) if row.counted_cash is not None else "—",
            "drawings": float(row.drawings or 0),
            "cash_gap": float(row.cash_gap) if row.cash_gap is not None else None,
            "cash_gap_fmt": fmt_inr(row.cash_gap) if row.cash_gap is not None else "—",
            "cash_ties": (row.cash_gap is not None and abs(float(row.cash_gap)) < 1.0),
            "signed_by": row.signed_by or "—",
            "signed_at": row.signed_at.strftime("%d %b %Y %I:%M %p") if row.signed_at else "—",
        }

    return {
        "from_date": from_date,
        "to_date": to_date,
        "from_fmt": from_date.strftime("%d %b %Y"),
        "to_fmt": to_date.strftime("%d %b %Y"),
        "days": (to_date - from_date).days + 1,
        "freshness": fresh,
        "debtors": debtors,
        "creditors": creditors,
        "cash": cash,
        "sales_recon": sales_recon,
        "bank": bank,
        "pnl": five_day_pnl(db, to_date),
        "exceptions": exceptions,
        "exception_count": len(exceptions),
        "warn_count": warn_count,
        "opening_cash": opening_cash,
        "opening_cash_from": opening_cash_from,
        "already_signed": row is not None,
        "signed": signed,
        "signed_at": signed["signed_at"] if signed else None,
        "recent_closes": recent_closes(db),
    }
