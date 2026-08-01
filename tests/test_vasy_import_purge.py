#!/usr/bin/env python
"""
Regression test: Vasy ledger imports (receipts, sales-invoices, purchases,
expenses, payments) must remove a row when it's deleted in Vasy and the file
is re-uploaded.

WHY THIS EXISTS
---------------
import_expenses()/import_receipts()/etc. used to only upsert by document key
(Expense No., Receipt No., ...) — nothing ever deleted a row, so a document
deleted in Vasy (e.g. the MSEB Electricity Bill ₹55,950 expense, 2026-08-01)
stayed in OrdeRR forever after re-sync. Only the daily `outstanding` snapshot
purged stale rows. Fixed by _purge_stale_ledger_rows(): after a re-upload,
delete existing rows whose key vanished from the file, but ONLY if the row's
own date falls within the min..max date span actually present in that file
(ledger exports can cover any partial date range, so a row from a month not
included in this upload must survive untouched).

Runs against a THROWAWAY sqlite file (set before any orderr_core import) so it
can never touch the real local db or the production Postgres.

No pytest dependency — run directly:

    venv/Scripts/python tests/test_vasy_import_purge.py
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP_DB = os.path.join(tempfile.gettempdir(), "orderr_test_vasy_import_purge.sqlite")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.pop("META_ACCESS_TOKEN", None)

import openpyxl  # noqa: E402

from orderr_core.database import Base, engine, SessionLocal  # noqa: E402
from orderr_core.models.salesperson import Salesperson  # noqa: E402,F401
from orderr_core.models.customer_alias import CustomerAlias  # noqa: E402,F401
from orderr_core.models.vasy_expense import VasyExpense  # noqa: E402
from orderr_core.models.customer_receipt import CustomerReceipt  # noqa: E402
from orderr_core.services.vasy_import import import_expenses, import_receipts  # noqa: E402

Base.metadata.create_all(engine)

_FAILURES = []


def check(label, got, want):
    if got != want:
        _FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def _xlsx(headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_expense_deleted_in_vasy_is_purged_on_resync():
    db = SessionLocal()
    try:
        # Initial import: two expenses in August.
        f1 = _xlsx(
            ["Sr. No.", "Expense No.", "Expense Date", "Party Name", "Total", "Paid", "UnPaid"],
            [
                [1, "EXP-1", "01/08/2026", "MSEB Electricity Bill", 55950, 55950, 0],
                [2, "EXP-2", "01/08/2026", "Plastic bags", 4700, 4700, 0],
            ],
        )
        import_expenses(db, f1, source_file="f1.xlsx")
        check("both expenses present after first import",
              sorted(e.expense_no for e in db.query(VasyExpense).all()),
              ["EXP-1", "EXP-2"])

        # EXP-1 deleted in Vasy; re-sync only sends EXP-2 for the same date.
        f2 = _xlsx(
            ["Sr. No.", "Expense No.", "Expense Date", "Party Name", "Total", "Paid", "UnPaid"],
            [
                [1, "EXP-2", "01/08/2026", "Plastic bags", 4700, 4700, 0],
            ],
        )
        result = import_expenses(db, f2, source_file="f2.xlsx")
        check("EXP-1 purged, EXP-2 survives",
              sorted(e.expense_no for e in db.query(VasyExpense).all()),
              ["EXP-2"])
        check("removed count reported", result["removed"], 1)
    finally:
        db.close()


def test_expense_outside_file_date_range_is_not_touched():
    db = SessionLocal()
    try:
        db.query(VasyExpense).delete()
        db.commit()
        # July expense already in the DB.
        july = _xlsx(
            ["Sr. No.", "Expense No.", "Expense Date", "Party Name", "Total", "Paid", "UnPaid"],
            [[1, "EXP-JUL", "15/07/2026", "Old rent", 10000, 10000, 0]],
        )
        import_expenses(db, july, source_file="july.xlsx")

        # A later re-sync covers only August — must NOT delete the July row.
        august = _xlsx(
            ["Sr. No.", "Expense No.", "Expense Date", "Party Name", "Total", "Paid", "UnPaid"],
            [[1, "EXP-AUG", "01/08/2026", "New rent", 12000, 12000, 0]],
        )
        import_expenses(db, august, source_file="august.xlsx")
        check("July expense outside file's date range survives",
              sorted(e.expense_no for e in db.query(VasyExpense).all()),
              ["EXP-AUG", "EXP-JUL"])
    finally:
        db.close()


def test_receipt_deleted_in_vasy_is_purged_on_resync():
    db = SessionLocal()
    try:
        f1 = _xlsx(
            ["Receipt No.", "Party Name", "Mode", "Date", "Amount", "Status"],
            [
                ["RCP-1", "Test Hotel", "cash", "01/08/2026", 1000, "confirmed"],
                ["RCP-2", "Test Hotel", "cash", "01/08/2026", 2000, "confirmed"],
            ],
        )
        import_receipts(db, f1, source_file="f1.xlsx")
        check("both receipts present after first import",
              sorted(r.receipt_no for r in db.query(CustomerReceipt).all()),
              ["RCP-1", "RCP-2"])

        f2 = _xlsx(
            ["Receipt No.", "Party Name", "Mode", "Date", "Amount", "Status"],
            [["RCP-2", "Test Hotel", "cash", "01/08/2026", 2000, "confirmed"]],
        )
        result = import_receipts(db, f2, source_file="f2.xlsx")
        check("RCP-1 purged, RCP-2 survives",
              sorted(r.receipt_no for r in db.query(CustomerReceipt).all()),
              ["RCP-2"])
        check("removed count reported", result["removed"], 1)
    finally:
        db.close()


if __name__ == "__main__":
    test_expense_deleted_in_vasy_is_purged_on_resync()
    test_expense_outside_file_date_range_is_not_touched()
    test_receipt_deleted_in_vasy_is_purged_on_resync()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")
