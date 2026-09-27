#!/usr/bin/env python
"""
Regression test: ledger-deletion audit trail for receipts/purchases/expenses/
payments/sales-invoices.

WHY THIS EXISTS
---------------
_purge_stale_ledger_rows() (vasy_import.py) silently deletes any row whose key
vanished from a re-uploaded ledger export -- e.g. a receipt deleted in Vasy
after a prior import. Before this change that deletion left no trace at all.
LedgerDeletionHistory is append-only and records entity_type/key/party/amount
for every such purge, across all five ledgers that share this code path.

Runs against a THROWAWAY sqlite file (set before any orderr_core import) so it
can never touch the real local db or the production Postgres.

No pytest dependency -- run directly:

    venv/Scripts/python tests/test_ledger_deletion_history.py
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP_DB = os.path.join(tempfile.gettempdir(), "orderr_test_ledger_deletion_history.sqlite")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.pop("META_ACCESS_TOKEN", None)

import openpyxl  # noqa: E402

from orderr_core.database import Base, engine, SessionLocal  # noqa: E402
from orderr_core.models.salesperson import Salesperson  # noqa: E402,F401
from orderr_core.models.customer_alias import CustomerAlias  # noqa: E402,F401
from orderr_core.models.customer import Customer  # noqa: E402,F401
from orderr_core.models.customer_receipt import CustomerReceipt  # noqa: E402
from orderr_core.models.vasy_expense import VasyExpense  # noqa: E402,F401
from orderr_core.models.ledger_deletion_history import LedgerDeletionHistory  # noqa: E402
from orderr_core.services.vasy_import import import_receipts, import_expenses, ledger_deletions_report  # noqa: E402

Base.metadata.create_all(engine)

_FAILURES = []


def check(label, got, want):
    if got != want:
        _FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def _receipts_xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Receipt No.", "Party Name", "Mode", "Date", "Amount", "Status", "Created By"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _expenses_xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Expense No.", "Expense Date", "Party Name", "Total", "Paid", "UnPaid"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_deleted_receipt_is_logged():
    db = SessionLocal()
    try:
        db.query(LedgerDeletionHistory).delete()
        db.query(CustomerReceipt).delete()
        db.commit()

        f1 = _receipts_xlsx([
            ["RCP-1", "Tambda Pandhra", "cash", "01/08/2026", 20000, "cleared", "owner"],
        ])
        import_receipts(db, f1, source_file="r1.xlsx")
        check("receipt present after first sync",
              [r.receipt_no for r in db.query(CustomerReceipt).all()], ["RCP-1"])

        # Receipt deleted in Vasy before the next sync — the re-upload's date
        # range must span RCP-1's own date for the purge to consider it.
        f2 = _receipts_xlsx([
            ["RCP-2", "Someone Else", "cash", "01/08/2026", 500, "cleared", "owner"],
            ["RCP-3", "Someone Else", "cash", "03/08/2026", 500, "cleared", "owner"],
        ])
        import_receipts(db, f2, source_file="r2.xlsx")
        check("deleted receipt is gone from the mirror",
              db.query(CustomerReceipt).filter_by(receipt_no="RCP-1").first(), None)

        report = ledger_deletions_report(db)
        check("one deletion logged", len(report), 1)
        check("logged entity is the deleted receipt",
              (report[0]["entity_type"], report[0]["entity_key"], report[0]["party_name"], report[0]["amount"]),
              ("receipt", "RCP-1", "Tambda Pandhra", 20000.0))
    finally:
        db.close()


def test_deleted_expense_is_logged_separately_from_receipts():
    db = SessionLocal()
    try:
        db.query(LedgerDeletionHistory).delete()
        db.query(VasyExpense).delete()
        db.commit()

        f1 = _expenses_xlsx([
            ["EXP-1", "01/08/2026", "Diesel Vendor", 5000, 5000, 0],
        ])
        import_expenses(db, f1, source_file="e1.xlsx")

        f2 = _expenses_xlsx([
            ["EXP-2", "01/08/2026", "Other Vendor", 100, 100, 0],
            ["EXP-3", "03/08/2026", "Other Vendor", 100, 100, 0],
        ])
        import_expenses(db, f2, source_file="e2.xlsx")

        report = ledger_deletions_report(db)
        check("one expense deletion logged",
              [(r["entity_type"], r["entity_key"]) for r in report], [("expense", "EXP-1")])
    finally:
        db.close()


if __name__ == "__main__":
    test_deleted_receipt_is_logged()
    test_deleted_expense_is_logged_separately_from_receipts()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")
