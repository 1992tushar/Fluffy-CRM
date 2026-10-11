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
    request=None, plant_name="T", current_time="now", m=m, analytics_view="money", cat_action=ms.CAT_ACTION, cat_name=ms.CAT_NAME, all_categories=bank_categorize.CATEGORIES,
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

# ---- regular hotel billed under a short name but linked by customer_id is NOT dormant
c2 = Customer(restaurant_name="Baba Chinese", phone_number="9000000002")
db2.add(c2); db2.commit()
db2.add(OutstandingSnapshot(customer_id=c2.id, party_name="Baba Chinese", party_key="BABACHINESE", opening_balance=0,
                            debit=0, credit=0, closing=80000, snapshot_date=TODAY))
db2.add(VasyInvoice(voucher_no="INVB1", invoice_date=TODAY - timedelta(days=3), party_name="Baba", party_key="BABA",
                    customer_id=c2.id, total=9000))
db2.commit()
cb2 = ms.customers_block(db2, TODAY)
rb = [r for r in cb2["all"] if r["key"] == "BABACHINESE"][0]
check(not rb["dormant"] and rb["cat"] != "F", "short-name invoices linked by customer_id -> not dormant")
check(abs(rb["sales30"] - 9000) < 0.01, "linked invoice counted in last-30-day sales")
print("LINKED-NAME PASSED")

# ---- hint when a dormant ledger row looks like another, actively billed customer record
c3 = Customer(restaurant_name="Sairat Ravet", phone_number="9000000003"); c4 = Customer(restaurant_name="Sairat Biryani", phone_number="9000000004")
db2.add_all([c3, c4]); db2.commit()
db2.add(OutstandingSnapshot(customer_id=c3.id, party_name="Sairat Biryani Ravet", party_key="SAIRATBIRYANIRAVET", opening_balance=0,
                            debit=0, credit=0, closing=150000, snapshot_date=TODAY))
db2.add(VasyInvoice(voucher_no="INVS1", invoice_date=TODAY - timedelta(days=2), party_name="Sairat Biryani", party_key="SAIRATBIRYANI",
                    customer_id=c4.id, total=5000)); db2.commit()
rs = [r for r in ms.customers_block(db2, TODAY)["all"] if r["key"] == "SAIRATBIRYANIRAVET"][0]
check(rs["dormant"] and rs["hint"] and "Sairat Biryani" in rs["hint"], "dormant row gets a 'possibly same hotel' hint")
print("HINT PASSED")

# ---- written-off customers stay visible (category W) but are excluded from live receivable totals
from orderr_core.models.bad_debt import BadDebt  # noqa: E402
c5 = Customer(restaurant_name="Monty", phone_number="9000000005"); db2.add(c5); db2.commit()
db2.add(OutstandingSnapshot(customer_id=c5.id, party_name="Monty Dabhade", party_key="MONTYDABHADE", opening_balance=199410,
                            debit=0, credit=0, closing=199410, snapshot_date=TODAY))
db2.add(BadDebt(customer_id=c5.id, amount=199410, reason="Dispute", note="parked", written_off_on=TODAY)); db2.commit()
before = ms.customers_block(db2, TODAY)
mrow = [r for r in before["all"] if r["key"] == "MONTYDABHADE"]
check(len(mrow) == 1 and mrow[0]["cat"] == "W" and "Written off" in mrow[0]["reason"], "written-off customer is listed as W with the reason")
check(before["cats"]["W"]["n"] == 1 and abs(before["written_total"] - 199410) < 0.01, "written-off total reported separately")
check(not any(r["key"] == "MONTYDABHADE" for r in before["dormant"]), "written-off customer not in the dormant list")
html = make_templates().get_template("dashboard_analytics_money.html").render(
    request=None, plant_name="T", current_time="now", m=ms.build(db2, TODAY), analytics_view="money",
    cat_action=ms.CAT_ACTION, cat_name=ms.CAT_NAME, categories=[], all_categories=[])
check("Monty Dabhade" in html, "written-off customer appears on the page")
print("WRITTEN-OFF PASSED")

# ---- a month whose payments are mostly untagged is flagged incomplete (not shown as "no drawings")
for i in range(12):
    txn(db2, date(2026, 8, 3), 1000 + i, "dr", None, cp=f"P{i}")          # untagged, in a past month
db2.commit()
of2 = ms.owner_flows(db2, TODAY, months=3)
aug = [x for x in of2["rows"] if x["month"] == "2026-08"][0]
check(aug["incomplete"] and "2026-08" in of2["incomplete_months"], "mostly-untagged month flagged incomplete")
check(any("INCOMPLETE" in a["text"] for a in ms.alerts(db2, TODAY, {"has_data": False}, ms.position(db2, TODAY, {"has_data": False}, {"total": 0}), {"trend": []}, [], [])), "alert raised for incomplete month")
print("INCOMPLETE PASSED")

# ---- an untagged credit is listed (so money the owner puts in is never invisible)
txn(db2, date(2026, 10, 9), 88000, "cr", None, cp="TUSHAR TANAJI")
db2.commit()
uc = ms.unclassified_credits(db2, TODAY)
check(any(x["amount"] == 88000 for x in uc), "untagged 88,000 credit is listed for tagging")
print("UNTAGGED-CREDIT PASSED")

# ---- every owner-table number can be drilled: the parts add up to the cell
sv = ms.txn_drilldown(db2, "2026-10", "owner_savings"); tr = ms.txn_drilldown(db2, "2026-10", "owner_transfers")
dw = ms.txn_drilldown(db2, "2026-10", "owner_drawings"); tot = ms.txn_drilldown(db2, "2026-10", "owner")
rr = [x for x in ms.owner_flows(db2, TODAY, months=2)["rows"] if x["month"] == "2026-10"][0]
check(abs(sum(x["amount"] for x in sv) - rr["savings"]) < 0.01, "savings drill adds up to the savings cell")
check(abs(sum(x["amount"] for x in tr) - rr["transfers"]) < 0.01, "transfers drill adds up to the transfers cell")
check(abs(sum(x["amount"] for x in dw) - rr["drawings"]) < 0.01, "drawings drill adds up to the drawings cell")
check(abs(sum(x["amount"] for x in tot) - rr["out"]) < 0.01, "total-out drill adds up to the total cell")
nt = ms.txn_drilldown(db2, "2026-10", "owner_net")
check(abs(sum(x["amount"] for x in nt if x["dir"] == "out") - sum(x["amount"] for x in nt if x["dir"] == "in") - rr["net"]) < 0.01, "net drill: out minus in equals the net cell")
inn = ms.txn_drilldown(db2, "2026-10", "owner_in")
check(abs(sum(x["amount"] for x in inn) - rr["in_confirmed"]) < 0.01, "put-in drill adds up to the put-in cell")
print("DRILL PASSED")
