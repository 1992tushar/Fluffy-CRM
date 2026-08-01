#!/usr/bin/env python
"""
Regression test for admin "Post Order on Behalf" no longer overriding a
customer's existing order for the day.

WHY THIS EXISTS
---------------
process_incoming_order()/_handle_order() already has correct duplicate-order
handling for the WhatsApp pipeline (replace-confirmation before the dispatch
cutoff, additional-order after it). admin.post_order_on_behalf() used to
bypass all of that by force-cancelling every existing active order for the
customer/day BEFORE calling the pipeline — silently wiping out the first
order whenever staff posted a second one. The fix threads a
force_additional=True flag through so the admin path always keeps the
existing order and saves the new one as an additional order instead.

Runs against a THROWAWAY sqlite file (set before any orderr_core import) so it
can never touch the real local db or the production Postgres.

No pytest dependency — run directly:

    venv/Scripts/python tests/test_order_duplicate_handling.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP_DB = os.path.join(tempfile.gettempdir(), "orderr_test_order_duplicate.sqlite")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.pop("META_ACCESS_TOKEN", None)  # ensure notifier no-ops, no network calls

from orderr_core.database import Base, engine, SessionLocal  # noqa: E402
from orderr_core.models.customer import Customer  # noqa: E402
from orderr_core.models.order import Order  # noqa: E402
from orderr_core.services.order_service import process_incoming_order  # noqa: E402

Base.metadata.create_all(engine)

_FAILURES = []


def check(label, got, want):
    if got != want:
        _FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


PHONE = "919999900010"


def seed_customer():
    db = SessionLocal()
    db.add(Customer(
        phone_number=PHONE, restaurant_name="Test Hotel",
        onboarding_status="done", is_active=True,
    ))
    db.commit()
    db.close()


def test_second_admin_post_does_not_cancel_first():
    print("admin post-order: second order does not override first")
    db = SessionLocal()

    r1 = process_incoming_order(
        db=db, customer_phone=PHONE, message="2 paneer, 1 curd", force_additional=True,
    )
    check("first order saved", r1["status"], "received")

    r2 = process_incoming_order(
        db=db, customer_phone=PHONE, message="3 butter", force_additional=True,
    )
    check("second order saved", r2["status"], "received")

    orders = (
        db.query(Order)
        .filter(Order.customer_phone == PHONE)
        .order_by(Order.created_at.asc())
        .all()
    )
    check("two orders exist", len(orders), 2)
    check("first order NOT cancelled", orders[0].is_cancelled, False)
    check("second order NOT cancelled", orders[1].is_cancelled, False)

    db.close()


if __name__ == "__main__":
    seed_customer()
    test_second_admin_post_does_not_cancel_first()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")
