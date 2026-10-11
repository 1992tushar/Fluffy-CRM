"""
Money tab — "where is our money going?" (read-only analytics over the data
already in the DB; the only writes are the owner's dormant-balance decisions).

Design rules (learned from the Sep-2026 reconciliation):
  * Never guess. A bank payment is classified only from the owner's own
    category tag (bank-recon screen); untagged / "other" rows are reported as
    UNCLASSIFIED, never folded into expenses or drawings.
  * Always show data freshness and gaps (a missing statement month looks like
    "no spending" otherwise).
  * Supplier payable = bills - payments (Vasy never allocates payments to
    bills, so its own "due" column is misleading).
  * Receivable ageing is a FIFO model on the nightly ledger snapshot: oldest
    invoices assumed paid first. It is an estimate, labelled as such.

Pure functions of (db, today). No HTTP, no templates.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from orderr_core.models.salesperson import Salesperson  # noqa: F401  (mapper registry)
from orderr_core.models.order import Order  # noqa: F401
from orderr_core.models.bank_transaction import BankTransaction
from orderr_core.models.bad_debt import BadDebt
from orderr_core.models.cash_entry import CashEntry
from orderr_core.models.customer import Customer
from orderr_core.models.customer_receipt import CustomerReceipt
from orderr_core.models.import_log import ImportLog
from orderr_core.models.invoice_edit_history import InvoiceEditHistory
from orderr_core.models.ledger_deletion_history import LedgerDeletionHistory
from orderr_core.models.money_decision import MoneyDecision, DECISIONS
from orderr_core.models.outstanding_snapshot import OutstandingSnapshot
from orderr_core.models.vasy_invoice import VasyInvoice
from orderr_core.models.vasy_invoice_history import VasyInvoiceHistory
from orderr_core.models.vasy_payment import VasyPayment
from orderr_core.models.vasy_purchase import VasyPurchase, VasyPurchaseItem
from orderr_core.models.vasy_sales_item import VasySalesItem
from orderr_core.models.vasy_supplier_bill import VasySupplierBill
from orderr_core.services.vasy_import import normalize_name

# ── thresholds (owner-set 2026-10-11; change here only) ─────────────────────
ALERT_TRANSFER_THRESHOLD = 25_000      # single non-supplier/non-payroll payment out of the bank
RAW_SHARE_ALERT = 0.72                 # purchases / sales
DEAD_PCT_ALERT = 0.04                  # dead-bird kg / live kg bought
STALE_DAYS = 2                         # feed older than this is flagged
GROW_DAYS = 14                         # customer balance growing with no payment
# Credits from these payer-name fragments, booked as customer receipts, are
# flagged (promoter money vs customer cash must not be mixed up).
OWNER_NAME_TOKENS = ("TANAJI", "THOPTE")

# ── bank categories → money class ───────────────────────────────────────────
CLASSES = ["supplier", "payroll", "operating", "loan", "owner", "reversal", "unclassified"]
CLASS_LABEL = {
    "supplier": "Suppliers (birds, ice)",
    "payroll": "Payroll & staff",
    "operating": "Operating costs",
    "loan": "Loans / EMI",
    "owner": "Owner / related / savings",
    "reversal": "Reversals / returned cheques",
    "unclassified": "Unclassified (tag me)",
}
_CAT_CLASS = {
    "purchase_raw_material": "supplier", "ice_purchase": "supplier",
    "employee_salary": "payroll", "employee_advance": "payroll", "employee_food": "payroll",
    "employee_reimbursement": "payroll",
    "fuel_vehicle": "operating", "packaging_consumables": "operating", "rent": "operating",
    "utilities": "operating", "statutory_licence": "operating", "repairs_maintenance": "operating",
    "commission": "operating",
    "loan_emi": "loan",
    "investment_savings": "owner", "internal_transfer": "owner", "credit_card_bill": "owner",
}
_INFLOW_CATS = {"customer_payment", "cash_sales_deposit", "customer_advance", "refund_received"}
LIVE_BIRD_PRODUCTS = ("small live boiler bird", "live boiler bird")
TANDOOR_LIVE = "tandoor live boiler bird"


def _f(x) -> float:
    return float(x or 0)


def classify_txn(t) -> str:
    """Money class of one bank row. Debits only meaningful; credits → 'in'."""
    if t.direction == "cr":
        return "in"
    cat = t.category
    if cat == "customer_payment":           # a debit tagged customer_payment = returned cheque / refund
        return "reversal"
    return _CAT_CLASS.get(cat, "unclassified")


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    return date(y, m + 1, 1)


def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


# ═══════════════════════════════════════════════════════════════════════════
# 1. Data freshness
# ═══════════════════════════════════════════════════════════════════════════
def freshness(db: Session, today: date) -> list[dict]:
    def row(label, last, kind):
        age = (today - last).days if last else None
        return {"label": label, "last": last, "age_days": age,
                "stale": last is None or age > STALE_DAYS, "kind": kind}
    bank = db.query(func.max(BankTransaction.value_date)).scalar()
    inv = db.query(func.max(VasyInvoice.invoice_date)).scalar()
    rec = db.query(func.max(CustomerReceipt.receipt_date)).scalar()
    snap = db.query(func.max(OutstandingSnapshot.snapshot_date)).scalar()
    pay = db.query(func.max(VasyPayment.payment_date)).scalar()
    return [row("Bank statement", bank, "bank"), row("Vasy invoices", inv, "vasy"),
            row("Customer receipts", rec, "vasy"), row("Supplier payments", pay, "vasy"),
            row("Ledger snapshot", snap, "vasy")]


def bank_month_gaps(db: Session, today: date, months: int = 6) -> list[str]:
    """Months in the window with (almost) no bank rows — statement not uploaded.
    Fewer than 10 rows in a past month is treated as a gap (a stray row can
    exist without a real statement). Only months after the first bank row."""
    first = db.query(func.min(BankTransaction.value_date)).scalar()
    if not first:
        return []
    counts: dict[str, int] = defaultdict(int)
    for (d,) in db.query(BankTransaction.value_date):
        if d:
            counts[_month_key(d)] += 1
    out = []
    for i in range(months - 1, -1, -1):
        m = _add_months(_month_start(today), -i)
        if m >= _month_start(first) and m < _month_start(today) and counts.get(_month_key(m), 0) < 10:
            out.append(_month_key(m))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 2. Receivables: snapshot + FIFO ageing
# ═══════════════════════════════════════════════════════════════════════════
def _latest_snapshot_date(db: Session) -> Optional[date]:
    return db.query(func.max(OutstandingSnapshot.snapshot_date)).scalar()


def receivable_model(db: Session, today: date) -> dict:
    """Per-customer receivable from the latest nightly snapshot, aged FIFO
    (newest invoices first; oldest assumed paid first).

    pre_fy  = max(0, opening - FY credits): balance older than the financial
              year start (proven by the ledger itself).
    The rest is allocated to invoices newest-first; any remainder predates the
    invoices we hold -> 'older' bucket.
    """
    snap_date = _latest_snapshot_date(db)
    if not snap_date:
        return {"has_data": False, "customers": [], "snap_date": None}
    snaps = db.query(OutstandingSnapshot).filter(OutstandingSnapshot.snapshot_date == snap_date).all()
    prev_date = db.query(func.max(OutstandingSnapshot.snapshot_date)).filter(
        OutstandingSnapshot.snapshot_date <= snap_date - timedelta(days=GROW_DAYS)).scalar()
    prev = {}
    if prev_date:
        prev = {s.party_key: _f(s.closing) for s in db.query(OutstandingSnapshot).filter(
            OutstandingSnapshot.snapshot_date == prev_date)}

    invs: dict[str, list] = defaultdict(list)
    for key, d, tot in db.query(VasyInvoice.party_key, VasyInvoice.invoice_date, VasyInvoice.total).filter(
            VasyInvoice.invoice_date.isnot(None)).order_by(VasyInvoice.invoice_date.desc()):
        invs[key].append((d, _f(tot)))

    d60 = today - timedelta(days=60)
    sales60: dict[str, float] = defaultdict(float)
    sales30: dict[str, float] = defaultdict(float)
    for key, d, tot in db.query(VasyInvoice.party_key, VasyInvoice.invoice_date, VasyInvoice.total).filter(
            VasyInvoice.invoice_date >= d60):
        sales60[key] += _f(tot)
        if d >= today - timedelta(days=30):
            sales30[key] += _f(tot)
    rec60: dict[str, float] = defaultdict(float)
    last_rec: dict[str, date] = {}
    for key_name, cid, d, amt in db.query(CustomerReceipt.party_name, CustomerReceipt.customer_id,
                                          CustomerReceipt.receipt_date, CustomerReceipt.amount).filter(
            CustomerReceipt.receipt_date.isnot(None)):
        k = normalize_name(key_name)
        if d >= d60:
            rec60[k] += _f(amt)
        if k not in last_rec or d > last_rec[k]:
            last_rec[k] = d

    bad = {c for (c,) in db.query(BadDebt.customer_id)}
    # table is created at app start; tolerate a DB that has not been migrated yet
    from sqlalchemy import inspect as _inspect
    decisions = ({d.customer_id: d for d in db.query(MoneyDecision)}
                 if _inspect(db.get_bind()).has_table("money_decisions") else {})

    rows = []
    for s in snaps:
        closing = _f(s.closing)
        age = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0, "pre_fy": 0.0}
        if closing > 0:
            opening = _f(s.opening_balance)
            pre = min(max(opening - _f(s.credit), 0.0), closing) if opening > 0 else 0.0
            age["pre_fy"] = pre
            rem = closing - pre
            for d, tot in invs.get(s.party_key, []):
                if rem <= 0.005:
                    break
                take = min(tot, rem)
                rem -= take
                days = (snap_date - d).days
                b = "0-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"
                age[b] += take
            age["90+"] += max(rem, 0.0)           # older than the invoices we hold
        over30 = age["31-60"] + age["61-90"] + age["90+"] + age["pre_fy"]
        old60 = age["61-90"] + age["90+"] + age["pre_fy"]
        monthly = sales60.get(s.party_key, 0.0) / 2.0
        coll = (rec60.get(s.party_key, 0.0) / sales60[s.party_key]) if sales60.get(s.party_key) else None
        months = (closing / monthly) if monthly > 0 else None
        cat = _category(closing, monthly, coll, months, old60)
        p = prev.get(s.party_key)
        rows.append({
            "customer_id": s.customer_id, "name": s.party_name, "key": s.party_key,
            "closing": closing, "age": age, "over30": over30, "old60": old60,
            "sales30": sales30.get(s.party_key, 0.0), "monthly": monthly, "coll": coll, "months": months,
            "last_receipt": last_rec.get(s.party_key),
            "days_since_receipt": (today - last_rec[s.party_key]).days if s.party_key in last_rec else None,
            "prev": p, "trend": (closing - p) if p is not None else None,
            "cat": cat, "reason": _reason(cat, closing, monthly, coll, months, old60),
            "written_off": s.customer_id in bad,
            "decision": decisions.get(s.customer_id),
            "dormant": closing > 0 and sales60.get(s.party_key, 0.0) == 0,
        })
    return {"has_data": True, "customers": rows, "snap_date": snap_date}


CAT_ACTION = {
    "A": "Retain on current terms",
    "B": "Retain, reduced credit limit (about 15 days of sales; no growth)",
    "C": "Advance / cash-on-delivery for new orders; weekly instalments on the old balance",
    "D": "No new credit until part-payment of the old balance arrives; cash only meanwhile",
    "F": "No billing for 60+ days: confirm balance, then pursue or provide",
}
CAT_NAME = {"A": "Retain", "B": "Reduced limit", "C": "Advance / COD", "D": "Credit hold", "F": "Dormant"}


def _reason(cat, closing, monthly, coll, months, old60) -> str:
    """One line saying WHY this customer is in its category (same numbers the rule uses)."""
    stale = (old60 / closing) if closing > 0 else 0
    if cat == "F":
        return "No invoices in the last 60 days" + (f"; owes {closing:,.0f}" if closing > 0 else "")
    bits = []
    if coll is not None:
        bits.append(f"collected {coll:.0%} of last-60-day billing")
    if months is not None:
        bits.append(f"owes {months:.1f} months of sales")
    bits.append(f"{stale:.0%} of balance older than 60 days")
    return "; ".join(bits)


def _category(closing, monthly, coll, months, old60) -> str:
    """A retain · B reduced limit · C advance/COD · D credit hold · F dormant."""
    if monthly <= 0:
        return "F" if closing > 0 else "A"
    if closing <= 2000:
        return "A"
    stale = old60 / closing if closing > 0 else 0
    if months is not None and months <= 1.0 and stale == 0:
        return "A"
    c = coll if coll is not None else 0
    if months is not None and months <= 3 and stale <= 0.30:
        return "B" if c >= 0.5 else "C"
    if c >= 0.5 and (months is None or months <= 8):
        return "C"
    return "D"


def customers_block(db: Session, today: date) -> dict:
    m = receivable_model(db, today)
    if not m["has_data"]:
        return {"has_data": False}
    rows = [r for r in m["customers"] if r["closing"] > 0 and not r["written_off"]]
    gross = sum(r["closing"] for r in rows)
    over30 = sum(r["over30"] for r in rows)
    cats = {}
    for c in "ABCDF":
        rs = [r for r in rows if r["cat"] == c]
        cats[c] = {"n": len(rs), "bal": sum(r["closing"] for r in rs), "sales30": sum(r["sales30"] for r in rs),
                   "over30": sum(r["over30"] for r in rs)}
    ages = {k: sum(r["age"][k] for r in rows) for k in ("0-30", "31-60", "61-90", "90+", "pre_fy")}
    top = sorted(rows, key=lambda r: r["closing"], reverse=True)[:20]
    dormant = sorted([r for r in rows if r["dormant"]], key=lambda r: r["closing"], reverse=True)
    return {"has_data": True, "snap_date": m["snap_date"], "gross": gross, "over30": over30,
            "over30_pct": (over30 / gross) if gross else 0, "cats": cats, "ages": ages,
            "top": top, "dormant": dormant[:40], "dormant_total": sum(r["closing"] for r in dormant),
            "dormant_n": len(dormant), "all": rows}


# ═══════════════════════════════════════════════════════════════════════════
# 3. Suppliers
# ═══════════════════════════════════════════════════════════════════════════
def supplier_position(db: Session) -> dict:
    bills: dict[str, dict] = {}
    for key, name, amt in db.query(VasySupplierBill.vendor_key, VasySupplierBill.vendor, VasySupplierBill.amount):
        b = bills.setdefault(key, {"name": name, "bills": 0.0, "paid": 0.0})
        b["bills"] += _f(amt)
    for key, amt in db.query(VasyPayment.party_key, VasyPayment.amount):
        if key in bills:
            bills[key]["paid"] += _f(amt)
    rows = []
    for key, b in bills.items():
        b["key"] = key
        b["net"] = b["bills"] - b["paid"]
        rows.append(b)
    rows.sort(key=lambda r: r["net"], reverse=True)
    return {"rows": [r for r in rows if abs(r["net"]) >= 1], "total": sum(r["net"] for r in rows),
            "vasy_due": _f(db.query(func.coalesce(func.sum(VasySupplierBill.due), 0)).scalar())}


# ═══════════════════════════════════════════════════════════════════════════
# 4. Where the money went (bank outflows by class)
# ═══════════════════════════════════════════════════════════════════════════
def outflow_by_class(db: Session, today: date, months: int = 4) -> dict:
    start = _add_months(_month_start(today), -(months - 1))
    rows = db.query(BankTransaction).filter(BankTransaction.value_date >= start).all()
    keys = [_month_key(_add_months(start, i)) for i in range(months)]
    grid = {k: {c: 0.0 for c in CLASSES} for k in keys}
    inflow = {k: 0.0 for k in keys}
    nrows = {k: 0 for k in keys}
    for t in rows:
        k = _month_key(t.value_date)
        if k not in grid:
            continue
        nrows[k] += 1
        c = classify_txn(t)
        if c == "in":
            inflow[k] += _f(t.amount)
        else:
            grid[k][c] += _f(t.amount)
    out = []
    for k in keys:
        tot = sum(grid[k].values())
        owner = grid[k]["owner"]
        out.append({"month": k, "classes": grid[k], "total": tot, "inflow": inflow[k], "rows": nrows[k],
                    "owner_pct": (owner / tot) if tot else 0, "unclass_pct": (grid[k]["unclassified"] / tot) if tot else 0})
    return {"months": out, "labels": CLASS_LABEL, "classes": CLASSES}


def txn_drilldown(db: Session, month: str, klass: str, limit: int = 300) -> list[dict]:
    y, m = int(month[:4]), int(month[5:7])
    a = date(y, m, 1)
    b = _add_months(a, 1)
    out = []
    if klass in OWNER_IN_CLASSES:
        q = db.query(BankTransaction).filter(BankTransaction.value_date >= a, BankTransaction.value_date < b,
                                             BankTransaction.direction == "cr").order_by(BankTransaction.amount.desc())
        for t in q:
            if owner_in_kind(t) != klass:
                continue
            out.append({"id": t.id, "date": t.value_date, "amount": _f(t.amount), "desc": (t.description or "")[:90],
                        "counterparty": t.counterparty_raw or "", "category": t.category, "remark": (t.remark or "") + (f" -> {t.party_label}" if t.party_label else "")})
        return out[:limit]
    q = db.query(BankTransaction).filter(BankTransaction.value_date >= a, BankTransaction.value_date < b,
                                         BankTransaction.direction == "dr").order_by(BankTransaction.amount.desc())
    for t in q:
        if classify_txn(t) != klass:
            continue
        out.append({"id": t.id, "date": t.value_date, "amount": _f(t.amount), "desc": (t.description or "")[:90],
                    "counterparty": t.counterparty_raw or "", "category": t.category, "remark": t.remark or ""})
        if len(out) >= limit:
            break
    if klass == "owner":                      # cash drawings from the Cash Book belong to the same story
        for e in db.query(CashEntry).filter(CashEntry.entry_date >= a, CashEntry.entry_date < b, CashEntry.type == "drawing"):
            out.append({"id": None, "date": e.entry_date, "amount": _f(e.amount), "desc": "Cash drawing (Cash Book)",
                        "counterparty": "Owner", "category": "drawing", "remark": e.note or ""})
        out.sort(key=lambda r: -r["amount"])
    return out


def unclassified_recent(db: Session, today: date, days: int = 45, limit: int = 40) -> list[dict]:
    a = today - timedelta(days=days)
    out = []
    for t in db.query(BankTransaction).filter(BankTransaction.value_date >= a,
                                              BankTransaction.direction == "dr").order_by(BankTransaction.amount.desc()):
        if classify_txn(t) == "unclassified":
            out.append({"id": t.id, "date": t.value_date, "amount": _f(t.amount), "desc": (t.description or "")[:80],
                        "counterparty": t.counterparty_raw or ""})
            if len(out) >= limit:
                break
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 4b. Owner account — taken OUT of Fluffy vs put IN
# ═══════════════════════════════════════════════════════════════════════════
OWNER_IN_CLASSES = ("owner_in", "owner_in_unconfirmed")


def _promoter_credit(t) -> bool:
    nm = (t.counterparty_raw or "").upper()
    return t.direction == "cr" and any(tok in nm for tok in OWNER_NAME_TOKENS)


def owner_in_kind(t) -> Optional[str]:
    """Credit that is owner/related money coming back into Fluffy.
    owner_in             - tagged internal_transfer (you confirmed it is yours)
    owner_in_unconfirmed - promoter-named payer booked as a customer payment
                           (could be customer cash collected by an individual)"""
    if t.direction != "cr":
        return None
    if t.category == "internal_transfer":
        return "owner_in"
    if t.category in (None, "customer_payment", "other") and _promoter_credit(t):
        return "owner_in_unconfirmed"
    return None


def owner_flows(db: Session, today: date, months: int = 6) -> dict:
    """Per month: money taken out of the Fluffy account (savings, transfers,
    cards, cash drawings) against money put in. 'Unconfirmed' credits are shown
    separately and NOT netted until you confirm them."""
    start = _add_months(_month_start(today), -(months - 1))
    keys = [_month_key(_add_months(start, i)) for i in range(months)]
    g = {k: {"savings": 0.0, "transfers": 0.0, "cards": 0.0, "drawings": 0.0, "in_confirmed": 0.0,
             "in_unconfirmed": 0.0, "bank_in": 0.0, "bank_out": 0.0, "rows": 0} for k in keys}
    untagged = 0.0
    for t in db.query(BankTransaction).filter(BankTransaction.value_date >= start):
        k = _month_key(t.value_date)
        if k not in g:
            continue
        g[k]["rows"] += 1
        amt = _f(t.amount)
        if t.direction == "dr" and classify_txn(t) == "unclassified":
            untagged += amt
        if t.direction == "cr":
            g[k]["bank_in"] += amt
            kind = owner_in_kind(t)
            if kind == "owner_in":
                g[k]["in_confirmed"] += amt
            elif kind == "owner_in_unconfirmed":
                g[k]["in_unconfirmed"] += amt
        else:
            g[k]["bank_out"] += amt
            if t.category == "investment_savings":
                g[k]["savings"] += amt
            elif t.category == "internal_transfer":
                g[k]["transfers"] += amt
            elif t.category == "credit_card_bill":
                g[k]["cards"] += amt
    for e in db.query(CashEntry).filter(CashEntry.entry_date >= start, CashEntry.type == "drawing"):
        k = _month_key(e.entry_date)
        if k in g:
            g[k]["drawings"] += _f(e.amount)
    rows = []
    tot = {"out": 0.0, "in": 0.0, "unconf": 0.0}
    for k in keys:
        x = g[k]
        out = x["savings"] + x["transfers"] + x["cards"] + x["drawings"]
        net = out - x["in_confirmed"]
        rows.append({"month": k, **x, "out": out, "net": net,
                     "net_if_unconf": net - x["in_unconfirmed"],
                     "out_pct": (out / x["bank_out"]) if x["bank_out"] else None,
                     "no_bank": x["rows"] < 10 and k != _month_key(today),
                     "partial": k == _month_key(today)})
        tot["out"] += out; tot["in"] += x["in_confirmed"]; tot["unconf"] += x["in_unconfirmed"]
    months_with = [r for r in rows if not r["no_bank"] and not r["partial"]]
    avg = (sum(r["out"] for r in months_with) / len(months_with)) if months_with else 0.0
    return {"rows": rows, "total_out": tot["out"], "total_in": tot["in"], "total_unconf": tot["unconf"],
            "net": tot["out"] - tot["in"], "net_if_unconf": tot["out"] - tot["in"] - tot["unconf"],
            "avg_month_out": avg, "untagged_out": untagged}


# ═══════════════════════════════════════════════════════════════════════════
# 5. Position strip + monthly cash bridge
# ═══════════════════════════════════════════════════════════════════════════
def _day_chain(db: Session, since: date) -> list[dict]:
    """Per value-date opening/closing balance from the statement's own running
    balance. Within a day the rows' order is unreliable, so: closing = the one
    balance that is nobody's 'balance before' on that day; opening = the one
    'balance before' that is no row's balance. Returns days oldest-first."""
    rows = db.query(BankTransaction).filter(BankTransaction.value_date >= since,
                                            BankTransaction.balance.isnot(None)).all()
    by = defaultdict(list)
    for r in rows:
        by[r.value_date].append(r)
    out = []
    for d in sorted(by):
        bal = []
        pri = []
        for r in by[d]:
            signed = _f(r.amount) if r.direction == "cr" else -_f(r.amount)
            bal.append(round(_f(r.balance), 2))
            pri.append(round(_f(r.balance) - signed, 2))
        closing = set(bal) - set(pri)
        opening = set(pri) - set(bal)
        if not closing and bal:            # all balances repeat (e.g. zero-net day) - ambiguous
            closing = {bal[0]}
        if not opening and pri:
            opening = {pri[0]}
        out.append({"date": d, "closing": next(iter(closing)) if len(closing) == 1 else None,
                    "opening": next(iter(opening)) if len(opening) == 1 else None,
                    "ambiguous": len(closing) != 1 or len(opening) != 1})
    return out


def bank_balance(db: Session) -> tuple[Optional[float], Optional[date]]:
    last = db.query(func.max(BankTransaction.value_date)).scalar()
    if not last:
        return None, None
    days = _day_chain(db, last)
    if days and days[-1]["closing"] is not None:
        return days[-1]["closing"], days[-1]["date"]
    t = db.query(BankTransaction).filter(BankTransaction.value_date == last, BankTransaction.balance.isnot(None)).first()
    return (_f(t.balance), last) if t else (None, None)


def position(db: Session, today: date, cust: dict, supp: dict) -> dict:
    bal, bal_date = bank_balance(db)
    a = today - timedelta(days=28)
    outs = [t for t in db.query(BankTransaction).filter(BankTransaction.value_date >= a, BankTransaction.direction == "dr")
            if classify_txn(t) not in ("reversal",)]
    out28 = sum(_f(t.amount) for t in outs)
    week = out28 / 4.0 if outs else 0.0
    cover = (bal / (week / 7.0)) if (bal is not None and week) else None
    return {"balance": bal, "balance_date": bal_date, "week_outflow": week, "cover_days": cover,
            "reserve_gap": (week - bal) if (bal is not None and week) else None,
            "recv": cust.get("gross") if cust.get("has_data") else None,
            "over30_pct": cust.get("over30_pct") if cust.get("has_data") else None,
            "supplier_net": supp["total"]}


def cash_bridge(db: Session, today: date, months: int = 3) -> list[dict]:
    out = []
    pay_keys = {k for (k,) in db.query(VasySupplierBill.vendor_key).distinct()}
    for i in range(months - 1, -1, -1):
        a = _add_months(_month_start(today), -i)
        b = _add_months(a, 1)
        sales = _f(db.query(func.coalesce(func.sum(VasySalesItem.net_amount), 0)).filter(
            VasySalesItem.invoice_date >= a, VasySalesItem.invoice_date < b).scalar())
        coll = _f(db.query(func.coalesce(func.sum(CustomerReceipt.amount), 0)).filter(
            CustomerReceipt.receipt_date >= a, CustomerReceipt.receipt_date < b).scalar())
        purch = _f(db.query(func.coalesce(func.sum(VasyPurchase.total), 0)).filter(
            VasyPurchase.bill_date >= a, VasyPurchase.bill_date < b).scalar())
        paid = 0.0
        for key, amt in db.query(VasyPayment.party_key, VasyPayment.amount).filter(
                VasyPayment.payment_date >= a, VasyPayment.payment_date < b):
            if key in pay_keys:
                paid += _f(amt)
        out.append({"month": _month_key(a), "sales": sales, "collected": coll, "customer_credit": sales - coll,
                    "coll_pct": (coll / sales) if sales else None, "purchases": purch, "paid": paid,
                    "supplier_credit": purch - paid, "partial": (a.year, a.month) == (today.year, today.month)})
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 6. Leakage watch
# ═══════════════════════════════════════════════════════════════════════════
def leakage(db: Session, today: date, months: int = 6) -> dict:
    start = _add_months(_month_start(today), -(months - 1))
    live_kg = defaultdict(float); live_amt = defaultdict(float); purch_tot = defaultdict(float)
    for bill_date, pname, qty, amt in db.query(VasyPurchase.bill_date, VasyPurchaseItem.product_name,
                                               VasyPurchaseItem.qty, VasyPurchaseItem.amount).join(
            VasyPurchaseItem, VasyPurchaseItem.vasy_purchase_id == VasyPurchase.id).filter(VasyPurchase.bill_date >= start):
        k = _month_key(bill_date)
        purch_tot[k] += _f(amt)
        if (pname or "").strip().lower() in LIVE_BIRD_PRODUCTS:
            live_kg[k] += _f(qty); live_amt[k] += _f(amt)
    sales = defaultdict(float); dead = defaultdict(float); waste = defaultdict(float)
    price_num = defaultdict(float); price_den = defaultdict(float)     # (product, month) realised price
    zero_issue = []                                                    # nil-value lines to wastage
    prod_cust = defaultdict(lambda: [0.0, 0.0])                         # (product, party) -> [amt, qty]
    for d, party, prod, qty, net in db.query(VasySalesItem.invoice_date, VasySalesItem.party_name,
                                             VasySalesItem.product_name, VasySalesItem.qty,
                                             VasySalesItem.net_amount).filter(VasySalesItem.invoice_date >= start):
        k = _month_key(d); q = _f(qty); n = _f(net); pn = (prod or "").strip()
        sales[k] += n
        if pn == "Dead Bird":
            dead[k] += q
        elif pn == "Plant Wastage":
            waste[k] += q
        if n > 0 and q > 0 and "andoor" not in pn:
            price_num[(pn, k)] += n; price_den[(pn, k)] += q
            if d >= today - timedelta(days=30):
                pc = prod_cust[(pn, (party or "").strip())]; pc[0] += n; pc[1] += q
        if n == 0 and (party or "").strip().upper() == "PLANT WASTAGE" and pn not in ("Plant Wastage", "Dead Bird"):
            zero_issue.append((k, pn, q))
    keys = [_month_key(_add_months(start, i)) for i in range(months)]
    trend = []
    for k in keys:
        avg = (live_amt[k] / live_kg[k]) if live_kg[k] else 0.0
        trend.append({"month": k, "live_kg": live_kg[k], "dead_kg": dead[k], "dead_pct": (dead[k] / live_kg[k]) if live_kg[k] else None,
                      "dead_value": dead[k] * avg, "waste_kg": waste[k],
                      "raw_share": (purch_tot[k] / sales[k]) if sales[k] else None, "sales": sales[k],
                      "partial": k == _month_key(today)})
    # high-value wastage valued at that month's realised price
    wv = defaultdict(lambda: [0.0, 0.0])
    for k, pn, q in zero_issue:
        den = price_den.get((pn, k))
        px = (price_num[(pn, k)] / den) if den else 0.0
        wv[pn][0] += q; wv[pn][1] += q * px
    waste_rows = sorted(([pn, v[0], v[1]] for pn, v in wv.items()), key=lambda r: r[2], reverse=True)[:8]
    # price realisation: last two months, top products, lowest-priced customers
    cur = _month_key(today); prev = _month_key(_add_months(_month_start(today), -1))
    top_products = sorted({p for (p, k) in price_den if k in (cur, prev)},
                          key=lambda p: -(price_num.get((p, cur), 0) + price_num.get((p, prev), 0)))[:6]
    price_rows = []
    for p in top_products:
        pr = lambda k: (price_num[(p, k)] / price_den[(p, k)]) if price_den.get((p, k)) else None
        low = sorted(((c, v[0] / v[1], v[1]) for (pp, c), v in prod_cust.items() if pp == p and v[1] >= 20), key=lambda r: r[1])[:2]
        price_rows.append({"product": p, "prev": pr(prev), "cur": pr(cur), "low": low})
    return {"trend": trend, "waste_rows": waste_rows, "waste_total": sum(r[2] for r in waste_rows), "price_rows": price_rows}


# ═══════════════════════════════════════════════════════════════════════════
# 7. Controls log
# ═══════════════════════════════════════════════════════════════════════════
def controls_log(db: Session, today: date, days: int = 30) -> dict:
    cut = datetime.combine(today - timedelta(days=days), datetime.min.time())
    dels = db.query(LedgerDeletionHistory).filter(LedgerDeletionHistory.deleted_at >= cut).order_by(
        LedgerDeletionHistory.deleted_at.desc()).all()
    gone = db.query(VasyInvoiceHistory).filter(VasyInvoiceHistory.disappeared_at.isnot(None),
                                               VasyInvoiceHistory.disappeared_at >= cut).order_by(
        VasyInvoiceHistory.disappeared_at.desc()).all()
    edits = db.query(InvoiceEditHistory).filter(InvoiceEditHistory.detected_at >= cut).order_by(
        InvoiceEditHistory.detected_at.desc()).limit(40).all()
    nedits = db.query(func.count(InvoiceEditHistory.id)).filter(InvoiceEditHistory.detected_at >= cut).scalar() or 0
    bad = db.query(BadDebt).filter(BadDebt.written_off_on >= today - timedelta(days=days)).all()
    return {"days": days, "dels": dels, "gone": gone, "edits": edits, "n_edits": nedits, "bad": bad,
            "del_total": sum(_f(d.amount) for d in dels), "gone_total": sum(_f(g.total) for g in gone),
            "edit_impact": sum(_f(e.impact) for e in edits), "bad_total": sum(_f(b.amount) for b in bad)}


# ═══════════════════════════════════════════════════════════════════════════
# 8. Alerts
# ═══════════════════════════════════════════════════════════════════════════
def alerts(db: Session, today: date, cust: dict, pos: dict, leak: dict, fresh: list, gaps: list) -> list[dict]:
    out = []

    def add(sev, text, link=None):
        out.append({"sev": sev, "text": text, "link": link})

    for f in fresh:
        if f["stale"]:
            add("red", f"{f['label']} is stale — last {f['last'] or 'never'}"
                       + (f" ({f['age_days']} days ago)" if f["age_days"] is not None else ""))
    for g in gaps:
        add("amber", f"No bank statement uploaded for {g}: spending for that month is invisible.", "/dashboard/analytics/bankrecon")
    # large non-supplier / non-payroll payments out (last 7 days)
    a = today - timedelta(days=7)
    for t in db.query(BankTransaction).filter(BankTransaction.value_date >= a, BankTransaction.direction == "dr",
                                              BankTransaction.amount >= ALERT_TRANSFER_THRESHOLD):
        c = classify_txn(t)
        if c in ("owner", "unclassified", "loan", "reversal"):
            add("red" if c in ("owner", "unclassified") else "amber",
                f"₹{_f(t.amount):,.0f} out on {t.value_date:%d %b} · {CLASS_LABEL[c]} · {(t.counterparty_raw or t.description or '')[:40]}",
                "/dashboard/analytics/bankrecon")
    # promoter-named payer booked as a customer receipt (one summary alert)
    hits = []
    for t in db.query(BankTransaction).filter(BankTransaction.value_date >= today - timedelta(days=30),
                                              BankTransaction.direction == "cr", BankTransaction.category == "customer_payment"):
        nm = (t.counterparty_raw or "").upper()
        if any(tok in nm for tok in OWNER_NAME_TOKENS):
            hits.append(t)
    if hits:
        who = sorted({(t.party_label or "no customer") for t in hits})
        add("amber", f"{len(hits)} credits worth ₹{sum(_f(t.amount) for t in hits):,.0f} from promoter-named payers in the last 30 days "
                     f"are booked as customer payments ({', '.join(who[:4])}{'…' if len(who) > 4 else ''}). "
                     f"Promoter money or customer cash?", "/dashboard/analytics/bankrecon")
    if pos.get("reserve_gap") is not None and pos["reserve_gap"] > 0:
        add("red", f"Bank ₹{pos['balance']:,.0f} covers {pos['cover_days']:.1f} days of outflows — "
                   f"₹{pos['reserve_gap']:,.0f} below a one-week reserve.")
    if cust.get("has_data"):
        for r in cust["all"]:
            if r["trend"] and r["trend"] > 0 and r["closing"] > 10000 and r["cat"] in ("C", "D") and r["coll"] is not None and r["coll"] < 0.5:
                add("amber", f"{r['name']}: balance up ₹{r['trend']:,.0f} in {GROW_DAYS} days, collected {r['coll']:.0%} of billing "
                             f"(category {r['cat']}).", "/dashboard/analytics/chase")
    cur = [t for t in leak["trend"] if t["partial"] is False and t["raw_share"] is not None]
    if cur and cur[-1]["raw_share"] > RAW_SHARE_ALERT:
        add("amber", f"Raw material is {cur[-1]['raw_share']:.0%} of sales in {cur[-1]['month']} (alert above {RAW_SHARE_ALERT:.0%}).")
    if cur and cur[-1]["dead_pct"] and cur[-1]["dead_pct"] > DEAD_PCT_ALERT:
        add("amber", f"Dead birds {cur[-1]['dead_pct']:.1%} of live kg in {cur[-1]['month']} (≈₹{cur[-1]['dead_value']:,.0f} paid for, sold at nil).")
    n_unc = len(unclassified_recent(db, today, 30, 500))
    if n_unc:
        add("amber", f"{n_unc} bank payments in the last 30 days are unclassified — tag them below.", "#unclassified")
    # reconciliation check: bank running-balance chain (last 30 days)
    brk = bank_chain_breaks(db, today - timedelta(days=30))
    if brk:
        add("red", f"Bank statement chain broken at {brk} row(s) in the last 30 days — a statement is missing or duplicated.")
    return out


def bank_chain_breaks(db: Session, since: date) -> int:
    """Days whose opening balance != previous day's closing balance. Skips
    calendar gaps with no statement rows (weekends are still dated rows, so a
    gap of >1 day with rows on both sides is itself checked the same way)."""
    days = _day_chain(db, since)
    breaks = 0
    for prev, cur in zip(days, days[1:]):
        if prev["closing"] is None or cur["opening"] is None:
            continue
        if round(prev["closing"] - cur["opening"], 2) != 0:
            breaks += 1
    return breaks


# ═══════════════════════════════════════════════════════════════════════════
# 9. Owner decision on dormant balances (the only write in this module)
# ═══════════════════════════════════════════════════════════════════════════
def set_decision(db: Session, customer_id: int, decision: Optional[str], note: Optional[str],
                 username: str, today: date) -> str:
    """Upsert (or clear, when decision is empty) the owner's call on a customer."""
    cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not cust:
        raise ValueError("Unknown customer.")
    row = db.query(MoneyDecision).filter(MoneyDecision.customer_id == customer_id).first()
    if not decision:
        if row:
            db.delete(row)
            db.commit()
        return "cleared"
    if decision not in DECISIONS:
        raise ValueError("decision must be 'pursue' or 'provide'.")
    note = (note or "").strip()[:300] or None
    if row:
        row.decision, row.note, row.decided_on, row.decided_by = decision, note, today, username
    else:
        db.add(MoneyDecision(customer_id=customer_id, decision=decision, note=note,
                             decided_on=today, decided_by=username))
    db.commit()
    return decision


# ═══════════════════════════════════════════════════════════════════════════
def build(db: Session, today: date) -> dict:
    fresh = freshness(db, today)
    gaps = bank_month_gaps(db, today)
    cust = customers_block(db, today)
    supp = supplier_position(db)
    pos = position(db, today, cust, supp)
    flows = outflow_by_class(db, today)
    owner = owner_flows(db, today)
    bridge = cash_bridge(db, today)
    leak = leakage(db, today)
    ctl = controls_log(db, today)
    al = alerts(db, today, cust, pos, leak, fresh, gaps)
    return {"fresh": fresh, "gaps": gaps, "cust": cust, "supp": supp, "pos": pos, "flows": flows,
            "owner": owner, "bridge": bridge, "leak": leak, "ctl": ctl, "alerts": al,
            "unclass": unclassified_recent(db, today), "threshold": ALERT_TRANSFER_THRESHOLD}
