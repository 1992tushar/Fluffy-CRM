"""
Invoice-edit audit: detect and log invoice lines whose rate/qty changed after
they were first entered (e.g. a manager quietly lowering a rate after the owner
dictated it). See orderr_core/models/invoice_edit_history.py for the three
sources this feeds:

  vasy            — diff of a voucher's lines between two consecutive Vasy syncs
  orderr          — reissue_invoice() rebuilt an invoice with different lines
  orderr_vs_vasy  — a voucher's lines at first Vasy sight vs what OrdeRR invoiced

Everything here only ADDS to the session; callers own the commit.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from orderr_core.models.customer import Customer
from orderr_core.models.invoice import Invoice
from orderr_core.models.invoice_edit_history import InvoiceEditHistory, VasyLineSnapshot

# Tolerances: below these a "change" is rounding noise, not an edit.
_QTY_EPS = 0.0005
_RATE_EPS = 0.005
_TOTAL_EPS = 1.0            # ₹ — voucher-level safety net
# Only compare a voucher to its OrdeRR invoice if the bot posted it recently;
# older vouchers have long since been legitimately corrected in either system.
_ORDERR_COMPARE_DAYS = 3


def _key(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())[:120]


def _rate(qty: float, amount: float) -> Optional[float]:
    return round(amount / qty, 4) if abs(qty) > _QTY_EPS else None


def diff_lines(old: dict, new: dict) -> list:
    """old/new: {product_key: (display_name, qty, amount)} → list of change dicts."""
    out = []
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k), new.get(k)
        name = (n or o)[0]
        if o and not n:
            out.append(dict(product_name=name, change_type="removed",
                            old_qty=o[1], new_qty=None,
                            old_rate=_rate(o[1], o[2]), new_rate=None,
                            old_amount=o[2], new_amount=0.0))
        elif n and not o:
            out.append(dict(product_name=name, change_type="added",
                            old_qty=None, new_qty=n[1],
                            old_rate=None, new_rate=_rate(n[1], n[2]),
                            old_amount=0.0, new_amount=n[2]))
        else:
            o_rate, n_rate = _rate(o[1], o[2]), _rate(n[1], n[2])
            qty_changed = abs(o[1] - n[1]) > _QTY_EPS
            rate_changed = (o_rate is not None and n_rate is not None
                            and abs(o_rate - n_rate) > _RATE_EPS)
            if not (qty_changed or rate_changed):
                continue
            ctype = "rate+qty" if (qty_changed and rate_changed) else ("rate" if rate_changed else "qty")
            out.append(dict(product_name=name, change_type=ctype,
                            old_qty=o[1], new_qty=n[1], old_rate=o_rate, new_rate=n_rate,
                            old_amount=o[2], new_amount=n[2]))
    return out


def _add_rows(db: Session, source: str, voucher_no: str, party_name, customer_id,
              invoice_date, changes: list) -> int:
    for c in changes:
        db.add(InvoiceEditHistory(
            source=source, voucher_no=voucher_no, party_name=party_name,
            customer_id=customer_id, invoice_date=invoice_date,
            impact=round(c["new_amount"] - c["old_amount"], 2), **{
                **c,
                "old_amount": round(c["old_amount"], 2),
                "new_amount": round(c["new_amount"], 2),
            }))
    return len(changes)


def _aggregate_vasy_lines(lines: list) -> dict:
    """lines (import_sales_items dicts) → {voucher: {key: (name, qty, amount)}}."""
    agg: dict = {}
    for ln in lines:
        vno = ln.get("voucher_no")
        if not vno:
            continue
        name = ln.get("product_name") or ln.get("item_code") or ""
        k = _key(name)
        if not k:
            continue
        amount = float(ln.get("taxable_amount") or 0) or float(ln.get("net_amount") or 0)
        cur = agg.setdefault(vno, {})
        _, q, a = cur.get(k, (name, 0.0, 0.0))
        cur[k] = (name, q + float(ln.get("qty") or 0), a + amount)
    return agg


def _orderr_lines(inv: Invoice) -> dict:
    from orderr_core.services.template_parser import erp_display_name
    out: dict = {}
    for it in inv.items:
        name = erp_display_name(it.product)
        k = _key(name)
        _, q, a = out.get(k, (name, 0.0, 0.0))
        out[k] = (name, q + float(it.quantity), a + float(it.amount))
    return out


def record_vasy_sync_edits(db: Session, lines: list, vouchers: dict, voucher_cid: dict) -> dict:
    """Called from import_sales_items() with this sync's parsed lines/vouchers
    (before anything is committed). Diffs each voucher against its snapshot from
    the previous sync, compares brand-new vouchers with the OrdeRR invoice that
    produced them, and refreshes the snapshot. Returns {"edits": n}."""
    current = _aggregate_vasy_lines(lines)

    snap: dict = {}
    for s in db.query(VasyLineSnapshot).all():
        snap.setdefault(s.voucher_no, {})[s.product_key] = (s.product_name, float(s.qty), float(s.amount))

    # OrdeRR invoices behind the vouchers we are seeing for the first time.
    new_vnos = [v for v in current if v not in snap]
    since = datetime.now(timezone.utc) - timedelta(days=_ORDERR_COMPARE_DAYS)
    orderr_inv: dict = {}
    for i in range(0, len(new_vnos), 400):
        chunk = new_vnos[i:i + 400]
        for inv in (db.query(Invoice)
                    .filter(Invoice.vasy_voucher_no.in_(chunk),
                            Invoice.vasy_synced_at >= since).all()):
            orderr_inv[inv.vasy_voucher_no] = inv

    edits = 0
    for vno, new_state in current.items():
        meta = vouchers.get(vno, {})
        party, cid, vdate = meta.get("party"), voucher_cid.get(vno), meta.get("date")
        old_state = snap.get(vno)

        if old_state is not None:
            ch = diff_lines(old_state, new_state)
            edits += _add_rows(db, "vasy", vno, party, cid, vdate, ch)
        elif vno in orderr_inv:
            inv = orderr_inv[vno]
            o_state = _orderr_lines(inv)
            matched = set(o_state) & set(new_state)
            ch = diff_lines({k: o_state[k] for k in matched}, {k: new_state[k] for k in matched})
            if not ch:
                vasy_total = sum(a for _, _, a in new_state.values())
                gap = vasy_total - float(inv.total)
                if abs(gap) > _TOTAL_EPS:
                    ch = [dict(product_name="(voucher total)", change_type="total",
                               old_qty=None, new_qty=None, old_rate=None, new_rate=None,
                               old_amount=float(inv.total), new_amount=vasy_total)]
            edits += _add_rows(db, "orderr_vs_vasy", vno, party, cid, vdate, ch)

        if old_state != new_state:
            db.query(VasyLineSnapshot).filter_by(voucher_no=vno).delete()
            for k, (name, q, a) in new_state.items():
                db.add(VasyLineSnapshot(voucher_no=vno, product_key=k, product_name=name,
                                        qty=round(q, 3), amount=round(a, 2)))
    return {"edits": edits}


def log_reissue_edit(db: Session, invoice: Invoice, old_lines: dict, new_items: list) -> int:
    """Called by reissue_invoice() right before the items are replaced.
    old_lines: {product_key: (name, qty, amount)} captured from the existing
    invoice; new_items: the replacement item dicts (product/quantity/amount)."""
    from orderr_core.services.template_parser import erp_display_name
    new_state: dict = {}
    for it in new_items:
        name = erp_display_name(it["product"])
        k = _key(name)
        _, q, a = new_state.get(k, (name, 0.0, 0.0))
        new_state[k] = (name, q + float(it["quantity"]), a + float(it["amount"]))
    changes = diff_lines(old_lines, new_state)
    if not changes:
        return 0
    cust = db.query(Customer).filter(Customer.phone_number == invoice.customer_phone).first()
    return _add_rows(db, "orderr", invoice.vasy_voucher_no or invoice.invoice_number,
                     cust.restaurant_name if cust else None, cust.id if cust else None,
                     invoice.business_date, changes)


def snapshot_invoice_lines(invoice: Invoice) -> dict:
    """Current OrdeRR invoice lines in diff_lines() form (call BEFORE editing)."""
    return _orderr_lines(invoice)


def invoice_edits_report(db: Session, days: int = 90) -> list:
    """Edits from the last `days`, newest first. `reduced` = the bill went down."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(InvoiceEditHistory)
            .filter(InvoiceEditHistory.detected_at >= cutoff)
            .order_by(InvoiceEditHistory.detected_at.desc(), InvoiceEditHistory.id.desc())
            .all())
    return [{
        "source": r.source, "voucher_no": r.voucher_no, "party_name": r.party_name or "",
        "customer_id": r.customer_id, "invoice_date": r.invoice_date,
        "product_name": r.product_name or "", "change_type": r.change_type,
        "old_qty": None if r.old_qty is None else float(r.old_qty),
        "new_qty": None if r.new_qty is None else float(r.new_qty),
        "old_rate": None if r.old_rate is None else float(r.old_rate),
        "new_rate": None if r.new_rate is None else float(r.new_rate),
        "old_amount": float(r.old_amount), "new_amount": float(r.new_amount),
        "impact": float(r.impact), "reduced": float(r.impact) < 0,
        "detected_at": r.detected_at,
    } for r in rows]
