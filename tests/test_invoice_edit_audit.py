#!/usr/bin/env python
"""
Regression test: invoice-edit audit (rate/qty changed after first entry).

Covers the three sources in invoice_edit_history:
  vasy            -- voucher lines differ between two consecutive syncs
  orderr_vs_vasy  -- voucher differs from the OrdeRR invoice at first Vasy sight
  orderr          -- reissue-style diff (log_reissue_edit)

Runs against a THROWAWAY sqlite file (set before any orderr_core import).
No pytest dependency -- run directly:

    venv/Scripts/python tests/test_invoice_edit_audit.py
"""
import io
import os
import sys
import tempfile
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP_DB = os.path.join(tempfile.gettempdir(), "orderr_test_invoice_edit_audit.sqlite")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.pop("META_ACCESS_TOKEN", None)

import openpyxl  # noqa: E402

from orderr_core.database import Base, engine, SessionLocal  # noqa: E402
from orderr_core.models.salesperson import Salesperson  # noqa: E402,F401
from orderr_core.models.customer_alias import CustomerAlias  # noqa: E402,F401
from orderr_core.models.customer import Customer  # noqa: E402,F401
from orderr_core.models.customer_receipt import CustomerReceipt  # noqa: E402,F401
from orderr_core.models.invoice import Invoice, InvoiceItem  # noqa: E402
from orderr_core.models.invoice_edit_history import InvoiceEditHistory  # noqa: E402
from orderr_core.models.vasy_sales_item import VasySalesItem  # noqa: E402,F401
from orderr_core.models.vasy_invoice import VasyInvoice, VasyInvoiceItem  # noqa: E402,F401
from orderr_core.models.vasy_invoice_history import VasyInvoiceHistory  # noqa: E402,F401
from orderr_core.models.import_log import ImportLog  # noqa: E402,F401
from orderr_core.services.vasy_import import import_sales_items  # noqa: E402
from orderr_core.services.invoice_edit_audit import (  # noqa: E402
    invoice_edits_report, log_reissue_edit, snapshot_invoice_lines,
)

Base.metadata.create_all(engine)

_FAILURES = []


def check(label, got, want):
    if got != want:
        _FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def _sales_xlsx(rows):
    """rows: (voucher, party, product, qty, rate)"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Sr.No", "Date", "Voucher No", "Sale Type", "Party Name", "Product Name",
               "Item Code", "QTY", "Taxable Amount", "Net Amount"])
    for i, (vno, party, prod, qty, rate) in enumerate(rows, 1):
        amt = qty * rate
        ws.append([i, "30/09/2026", vno, "Invoice", party, prod, "", qty, amt, amt])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_vasy_rate_reduction_between_syncs():
    db = SessionLocal()
    try:
        db.query(InvoiceEditHistory).delete()
        db.commit()

        import_sales_items(db, _sales_xlsx([
            ("INV1", "Hotel Alpha", "Test Boneless", 3, 320),
            ("INV1", "Hotel Alpha", "Test Curry Cut", 2, 240),
        ]))
        check("first sight logs nothing (no OrdeRR invoice)", len(invoice_edits_report(db)), 0)

        import_sales_items(db, _sales_xlsx([
            ("INV1", "Hotel Alpha", "Test Boneless", 3, 300),     # rate lowered 320 -> 300
            ("INV1", "Hotel Alpha", "Test Curry Cut", 2, 240),
        ]))
        rep = invoice_edits_report(db)
        check("one edit logged", len(rep), 1)
        r = rep[0]
        check("edit details",
              (r["source"], r["voucher_no"], r["product_name"], r["change_type"],
               r["old_rate"], r["new_rate"], r["impact"], r["reduced"]),
              ("vasy", "INV1", "Test Boneless", "rate", 320.0, 300.0, -60.0, True))

        import_sales_items(db, _sales_xlsx([
            ("INV1", "Hotel Alpha", "Test Boneless", 3, 300),
            ("INV1", "Hotel Alpha", "Test Curry Cut", 2, 240),
        ]))
        check("unchanged resync adds nothing", len(invoice_edits_report(db)), 1)

        import_sales_items(db, _sales_xlsx([
            ("INV1", "Hotel Alpha", "Test Boneless", 3, 300),
        ]))
        kinds = sorted(x["change_type"] for x in invoice_edits_report(db))
        check("removed line is logged", kinds, ["rate", "removed"])
    finally:
        db.close()


def test_edit_before_first_sync_is_caught_via_orderr_invoice():
    db = SessionLocal()
    try:
        db.query(InvoiceEditHistory).delete()
        inv = Invoice(invoice_number="FLUFFY-20260930-001", order_id=9001,
                      customer_phone="9999999999", business_date=date(2026, 9, 30),
                      subtotal=960, total=960, vasy_synced_at=datetime.now(timezone.utc),
                      vasy_voucher_no="INV2")
        db.add(inv)
        db.flush()
        db.add(InvoiceItem(invoice_id=inv.id, product="Test Boneless Z", quantity=3, unit="kg",
                           rate_used=320, amount=960, rate_source="daily_rate"))
        db.commit()

        import_sales_items(db, _sales_xlsx([("INV2", "Hotel Beta", "Test Boneless Z", 3, 290)]))
        rep = [x for x in invoice_edits_report(db) if x["voucher_no"] == "INV2"]
        check("orderr_vs_vasy edit logged", len(rep), 1)
        check("orderr_vs_vasy details",
              (rep[0]["source"], rep[0]["old_rate"], rep[0]["new_rate"], rep[0]["impact"]),
              ("orderr_vs_vasy", 320.0, 290.0, -90.0))
    finally:
        db.close()


def test_orderr_reissue_diff_is_logged():
    db = SessionLocal()
    try:
        db.query(InvoiceEditHistory).delete()
        inv = db.query(Invoice).filter_by(invoice_number="FLUFFY-20260930-001").one()
        old = snapshot_invoice_lines(inv)
        n = log_reissue_edit(db, inv, old, [
            {"product": "Test Boneless Z", "quantity": 3, "amount": 900},   # rate 320 -> 300
        ])
        db.commit()
        check("reissue edit count", n, 1)
        rep = invoice_edits_report(db)
        check("reissue edit details",
              (rep[0]["source"], rep[0]["old_rate"], rep[0]["new_rate"], rep[0]["impact"]),
              ("orderr", 320.0, 300.0, -60.0))
        check("no-op reissue logs nothing",
              log_reissue_edit(db, inv, old, [
                  {"product": "Test Boneless Z", "quantity": 3, "amount": 960}]), 0)
    finally:
        db.close()


if __name__ == "__main__":
    test_vasy_rate_reduction_between_syncs()
    test_edit_before_first_sync_is_caught_via_orderr_invoice()
    test_orderr_reissue_diff_is_logged()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")
