#!/usr/bin/env python
"""
Regression test: fraud-detection audit trail for Vasy sales invoices.

WHY THIS EXISTS
---------------
import_sales_items() snapshot-replaces VasyInvoice/VasyInvoiceItem on every
sync (the Vasy export carries the full FY each run) — so a voucher deleted in
Vasy between two syncs used to vanish from OrdeRR with no trace at all: no
timestamp, no record of who/what/when, nothing to investigate. That's exactly
the gap a dishonest manager could exploit (punch an invoice, let it sync, then
delete it from Vasy and collect the amount off-books).

VasyInvoiceHistory is append-only and never wiped by a sync: every voucher
seen gets first_seen_at/last_seen_at, and a voucher present in a prior sync
but missing from the latest one gets disappeared_at stamped. This test proves
that flow, plus the disappeared_invoices_report() no-matching-receipt flag.

Runs against a THROWAWAY sqlite file (set before any orderr_core import) so it
can never touch the real local db or the production Postgres.

No pytest dependency — run directly:

    venv/Scripts/python tests/test_vasy_invoice_history.py
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP_DB = os.path.join(tempfile.gettempdir(), "orderr_test_vasy_invoice_history.sqlite")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.pop("META_ACCESS_TOKEN", None)

import openpyxl  # noqa: E402

from orderr_core.database import Base, engine, SessionLocal  # noqa: E402
from orderr_core.models.salesperson import Salesperson  # noqa: E402,F401
from orderr_core.models.customer_alias import CustomerAlias  # noqa: E402,F401
from orderr_core.models.customer import Customer  # noqa: E402
from orderr_core.models.customer_receipt import CustomerReceipt  # noqa: E402
from orderr_core.models.vasy_invoice import VasyInvoice  # noqa: E402
from orderr_core.models.vasy_invoice_history import VasyInvoiceHistory  # noqa: E402
from orderr_core.models.vasy_sales_item import VasySalesItem  # noqa: E402,F401
from orderr_core.services.vasy_import import import_sales_items, disappeared_invoices_report  # noqa: E402

Base.metadata.create_all(engine)

_FAILURES = []


def check(label, got, want):
    if got != want:
        _FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def _sales_xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Voucher No", "Party Name", "Date", "Sale Type", "Product Name",
              "Item Code", "Qty", "Net Amount"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_deleted_invoice_is_flagged_disappeared_not_erased():
    db = SessionLocal()
    try:
        db.query(VasyInvoiceHistory).delete()
        db.query(VasyInvoice).delete()
        db.query(CustomerReceipt).delete()
        db.commit()

        # Sync 1: manager punches INV-1000 (₹50,000) for Test Hotel.
        f1 = _sales_xlsx([
            ["INV-1000", "Test Hotel", "01/08/2026", "Sale", "Chicken", "CH1", 10, 50000],
        ])
        import_sales_items(db, f1, source_file="f1.xlsx")
        check("invoice present after first sync",
              sorted(v.voucher_no for v in db.query(VasyInvoice).all()),
              ["INV-1000"])
        h = db.query(VasyInvoiceHistory).filter_by(voucher_no="INV-1000").one()
        check("history row created, not disappeared", h.disappeared_at is None, True)

        # Sync 2: INV-1000 deleted in Vasy before the next sync (no receipt
        # ever recorded for it) — the fraud pattern.
        f2 = _sales_xlsx([])
        result = import_sales_items(db, f2, source_file="f2.xlsx")
        check("VasyInvoice mirror no longer has it",
              db.query(VasyInvoice).filter_by(voucher_no="INV-1000").first(), None)
        check("import reports 1 disappeared", result["disappeared"], 1)

        h = db.query(VasyInvoiceHistory).filter_by(voucher_no="INV-1000").one()
        check("history row survives the wipe with its original data",
              (h.party_name, float(h.total)), ("Test Hotel", 50000.0))
        check("history row now flagged disappeared", h.disappeared_at is not None, True)

        report = disappeared_invoices_report(db)
        check("report lists it", [r["voucher_no"] for r in report], ["INV-1000"])
        check("flagged: no matching receipt found", report[0]["no_matching_receipt"], True)
    finally:
        db.close()


def test_disappeared_invoice_with_matching_receipt_is_not_flagged():
    db = SessionLocal()
    try:
        db.query(VasyInvoiceHistory).delete()
        db.query(VasyInvoice).delete()
        db.query(CustomerReceipt).delete()
        db.commit()

        f1 = _sales_xlsx([
            ["INV-2000", "Legit Hotel", "01/08/2026", "Sale", "Chicken", "CH1", 5, 20000],
        ])
        import_sales_items(db, f1, source_file="f1.xlsx")
        cust_id = db.query(VasyInvoice).filter_by(voucher_no="INV-2000").one().customer_id
        assert cust_id is not None, "sales-item import should auto-create the customer"

        # Customer pays properly (receipt on record close to the deletion date).
        db.add(CustomerReceipt(receipt_no="RCP-9", party_name="Legit Hotel",
                               customer_id=cust_id, mode="cash", amount=20000,
                               receipt_date=__import__("datetime").date(2026, 8, 2),
                               status="confirmed"))
        db.commit()

        # Invoice corrected/reversed in Vasy after being paid — legitimate.
        f2 = _sales_xlsx([])
        import_sales_items(db, f2, source_file="f2.xlsx")

        report = disappeared_invoices_report(db)
        check("report lists it", [r["voucher_no"] for r in report], ["INV-2000"])
        check("NOT flagged: receipt covers the amount", report[0]["no_matching_receipt"], False)
    finally:
        db.close()


def test_reappearing_invoice_clears_disappeared_flag():
    db = SessionLocal()
    try:
        db.query(VasyInvoiceHistory).delete()
        db.query(VasyInvoice).delete()
        db.commit()

        f1 = _sales_xlsx([["INV-3000", "Flappy Diner", "01/08/2026", "Sale", "Chicken", "CH1", 1, 1000]])
        import_sales_items(db, f1, source_file="f1.xlsx")
        f2 = _sales_xlsx([])   # gone
        import_sales_items(db, f2, source_file="f2.xlsx")
        h = db.query(VasyInvoiceHistory).filter_by(voucher_no="INV-3000").one()
        check("flagged disappeared after sync 2", h.disappeared_at is not None, True)

        f3 = _sales_xlsx([["INV-3000", "Flappy Diner", "01/08/2026", "Sale", "Chicken", "CH1", 1, 1000]])
        import_sales_items(db, f3, source_file="f3.xlsx")
        h = db.query(VasyInvoiceHistory).filter_by(voucher_no="INV-3000").one()
        check("disappeared flag cleared once it reappears", h.disappeared_at is None, True)
    finally:
        db.close()


if __name__ == "__main__":
    test_deleted_invoice_is_flagged_disappeared_not_erased()
    test_disappeared_invoice_with_matching_receipt_is_not_flagged()
    test_reappearing_invoice_clears_disappeared_flag()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")
