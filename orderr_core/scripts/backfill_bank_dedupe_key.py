"""
One-off backfill: the bank-recon dedupe_key used to be
value_date|ref|amount|direction only. Kotak reuses the same ref across
distinct instruments cleared in one batch (e.g. CLG/NEFT), so two different
same-day/same-amount/same-direction transactions could collide under the old
key and the second one would get silently dropped on import.

This script:
  1. Recomputes dedupe_key for every existing BankTransaction row using the
     new formula (adds description), matching the fix in bank_import.py.
  2. Inserts the transaction that was silently dropped on 10-Aug-2026 import
     (712 Hotel Lonavla, Rs 1,00,000 CR clearing instrument 000858) which
     collided with the Karla Gate clearing instrument 000200 under the old key.

Run once: python -m orderr_core.scripts.backfill_bank_dedupe_key
Safe to re-run — step 1 is idempotent (same formula every time), step 2
checks for the row before inserting.
"""
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from orderr_core.database import SessionLocal, DATABASE_URL
from orderr_core.models.bank_transaction import BankTransaction


def _dedupe_key(vdate, ref, amt, direction, desc):
    return f"{vdate.isoformat()}|{ref or ''}|{amt:.2f}|{direction}|{desc or ''}"


def main():
    print(f"Connecting to: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")
    db = SessionLocal()
    try:
        rows = db.query(BankTransaction).all()
        updated = 0
        for row in rows:
            new_key = _dedupe_key(row.value_date, row.ref_no, float(row.amount), row.direction, row.description)
            if row.dedupe_key != new_key:
                row.dedupe_key = new_key
                updated += 1
        db.flush()
        print(f"Recomputed dedupe_key for {len(rows)} rows ({updated} changed).")

        missing_key = _dedupe_key(
            date(2026, 8, 10), "NCROUT_2_10082026179", 100000.00, "cr",
            "BY CLG INST 000858/06-08-26/ICIC/PUNE",
        )
        existing = db.query(BankTransaction).filter_by(dedupe_key=missing_key).first()
        if existing:
            print("Missing row already present, nothing to insert.")
        else:
            db.add(BankTransaction(
                value_date=date(2026, 8, 10),
                txn_date=date(2026, 8, 10),
                description="BY CLG INST 000858/06-08-26/ICIC/PUNE",
                ref_no="NCROUT_2_10082026179",
                amount=100000.00,
                direction="cr",
                balance=210832.55,
                dedupe_key=missing_key,
                source_file="10 August.csv (backfill)",
                remark="712 Hotel Lonavla",
            ))
            print("Inserted missing row: 10-Aug-2026 CR Rs 1,00,000 (712 Hotel Lonavla).")

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
