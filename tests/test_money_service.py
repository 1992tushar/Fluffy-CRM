#!/usr/bin/env python
"""
Regression test for the Money tab (services/money_service.py + routes/money.py).

Runs against a THROWAWAY sqlite file (set before any orderr_core import), so it
can never touch the real local db or production Postgres.

    venv/Scripts/python tests/test_money_service.py
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP_DB = os.path.join(tempfile.gettempdir(), "orderr_test_money_service.sqlite")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.pop("META_ACCESS_TOKEN", None)

import pkgutil, importlib  # noqa: E402
import orderr_core.models as _M  # noqa: E402
for _m in pkgutil.iter_modules(_M.__path__):
    importlib.import_module("orderr_core.models." + _m.name)

from orderr_core.database import Base, engine, SessionLocal  # noqa: E402
from orderr_core.models.bank_transaction import BankTransaction  # noqa: E402
from orderr_core.models.customer import Customer  # noqa: E402
from orderr_core.models.money_decision import MoneyDecision  # noqa: E402
from orderr_core.models.outstanding_snapshot import OutstandingSnapshot  # noqa: E402
from orderr_core.models.vasy_invoice import VasyInvoice  # noqa: E402
from orderr_core.services import money_service as ms  # noqa: E402

Base.metadata.create_all(bind=engine)
TODAY = date(2026, 10, 11)
_n = 0


def txn(db, d, amt, direction, cat=None, cp="X", bal=None, desc="d"):
    global _n
    _n += 1
    db.add(BankTransaction(value_date=d, txn_date=d, description=desc, ref_no=str(_n), amount=amt, direction=direction,
                           balance=bal, dedupe_key=f"k{_n}", counterparty_raw=cp, category=cat))


def check(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("ok  :", msg)


db = SessionLocal()

# ---- classification: only owner tags count; untagged is unclassified, never guessed
class T:  # minimal stand-in
    def __init__(s, d, c): s.direction, s.category = d, c
check(ms.classify_txn(T("dr", "investment_savings")) == "owner", "savings tag -> owner")
check(ms.classify_txn(T("dr", None)) == "unclassified", "untagged debit -> unclassified")
check(ms.classify_txn(T("dr", "other")) == "unclassified", "'other' debit -> unclassified")
check(ms.classify_txn(T("dr", "customer_payment")) == "reversal", "debit tagged customer_payment -> reversal")
check(ms.classify_txn(T("cr", None)) == "in", "credit -> in")

# ---- bank: two days, balances chain; second day also has a break-free chain
d1, d2 = date(2026, 10, 8), date(2026, 10, 9)
# day1: opening 1000, +500 (bal 1500), -200 (bal 1300); rows stored newest-first like the statement
txn(db, d1, 200, "dr", "utilities", bal=1300); txn(db, d1, 500, "cr", "customer_payment", bal=1500)
# day2: opening 1300, -30000 transfer (bal -28700) -> large untagged
txn(db, d2, 30000, "dr", None, cp="PERSON", bal=-28700)
db.commit()
bal, bd = ms.bank_balance(db)
check(bal == -28700 and bd == d2, f"bank balance from chain = {bal}")
check(ms.bank_chain_breaks(db, d1) == 0, "no chain breaks on consistent statement")
txn(db, date(2026, 10, 10), 100, "dr", "fuel_vehicle", bal=5000)   # opening 5100 != 28700 closing
db.commit()
check(ms.bank_chain_breaks(db, d1) == 1, "chain break detected")

# ---- outflow classes + drilldown
fl = ms.outflow_by_class(db, TODAY, months=2)
oct_ = [m for m in fl["months"] if m["month"] == "2026-10"][0]
check(abs(oct_["classes"]["unclassified"] - 30000) < 0.01, "30,000 untagged shows as unclassified")
check(abs(oct_["classes"]["operating"] - 300) < 0.01, "utilities+fuel = operating 300")
dr = ms.txn_drilldown(db, "2026-10", "unclassified")
check(len(dr) == 1 and dr[0]["amount"] == 30000, "drilldown returns the payment")

# ---- alerts: threshold applies to unclassified, ignored for supplier
txn(db, date(2026, 10, 9), 90000, "dr", "purchase_raw_material", cp="YEWALE", bal=-118700)
db.commit()
fresh = ms.freshness(db, TODAY)
pos = ms.position(db, TODAY, {"has_data": False}, {"total": 0})
al = ms.alerts(db, TODAY, {"has_data": False}, pos, {"trend": []}, fresh, [])
texts = " ".join(a["text"] for a in al)
check("30,000 out" in texts and "90,000 out" not in texts, "alert fires for 30,000 untagged, not for 90,000 supplier payment")

# ---- receivable model + decisions
c = Customer(restaurant_name="Old Hotel", phone_number="9000000001")
db.add(c); db.commit()
db.add(OutstandingSnapshot(customer_id=c.id, party_name="Old Hotel", party_key="OLDHOTEL", opening_balance=50000,
                           debit=0, credit=0, closing=50000, snapshot_date=TODAY))
db.add(VasyInvoice(voucher_no="INV1", invoice_date=TODAY - timedelta(days=200), party_name="Old Hotel", party_key="OLDHOTEL", total=1000))
db.commit()
cb = ms.customers_block(db, TODAY)
row = [r for r in cb["all"] if r["key"] == "OLDHOTEL"][0]
check(row["dormant"] and row["cat"] == "F", "no billing in 60 days -> dormant, category F")
check(abs(row["age"]["pre_fy"] - 50000) < 0.01, "opening balance never credited -> all pre-FY")
check(ms.set_decision(db, c.id, "pursue", "call him", "owner", TODAY) == "pursue", "decision saved")
check(ms.set_decision(db, c.id, "provide", None, "owner", TODAY) == "provide", "decision upserted")
check(db.query(MoneyDecision).count() == 1, "one row per customer")
try:
    ms.set_decision(db, c.id, "ignore", None, "owner", TODAY); check(False, "bad decision rejected")
except ValueError:
    check(True, "bad decision rejected")
check(ms.set_decision(db, c.id, "", None, "owner", TODAY) == "cleared" and db.query(MoneyDecision).count() == 0, "decision cleared")

# ---- whole page builds and renders
from orderr_core.templating import make_templates  # noqa: E402
from orderr_core.services import bank_categorize  # noqa: E402
m = ms.build(db, TODAY)
html = make_templates().get_template("dashboard_analytics_money.html").render(
    request=None, plant_name="T", current_time="now", m=m, analytics_view="money",
    categories=[x for x in bank_categorize.CATEGORIES if x[2] in ("out", "nonpl", "other")])
check("Today's position" in html and "Controls log" in html, "page renders all sections")
print("ALL PASSED")

# ---- owner account: out vs in
from orderr_core.models.cash_entry import CashEntry  # noqa: E402
db2 = SessionLocal()
txn(db2, date(2026, 10, 5), 7000, "dr", "investment_savings", cp="SAVER")
txn(db2, date(2026, 10, 6), 40000, "dr", "internal_transfer", cp="TUSHAR TAN")
txn(db2, date(2026, 10, 6), 10000, "cr", "internal_transfer", cp="TUSHAR TANAJI")
txn(db2, date(2026, 10, 7), 25000, "cr", "customer_payment", cp="SANDEEP TANAJI")
txn(db2, date(2026, 10, 7), 5000, "cr", "customer_payment", cp="REAL CUSTOMER")
db2.add(CashEntry(entry_date=date(2026, 10, 8), type="drawing", direction="out", amount=3000))
db2.commit()
of = ms.owner_flows(db2, TODAY, months=2)
r = [x for x in of["rows"] if x["month"] == "2026-10"][0]
check(r["savings"] == 7000 and r["transfers"] == 40000 and r["drawings"] == 3000, "owner OUT: savings 7k + transfers 40k + drawings 3k")
check(r["out"] == 50000 and r["in_confirmed"] == 10000 and r["net"] == 40000, "net out = 50k - 10k confirmed in")
check(r["in_unconfirmed"] == 25000 and r["net_if_unconf"] == 15000, "promoter-named customer credit is unconfirmed, not netted")
check(len(ms.txn_drilldown(db2, "2026-10", "owner_in_unconfirmed")) == 1, "unconfirmed drill shows 1 credit")
check(any(x["category"] == "drawing" for x in ms.txn_drilldown(db2, "2026-10", "owner")), "owner drill includes cash drawing")
print("OWNER FLOW PASSED")
