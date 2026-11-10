#!/usr/bin/env python
"""
Customer Edit tab: WhatsApp chat audit against Vasy entries.

Checks chat parsing (Android + iOS layouts), order-vs-billing matching, payment
-vs-receipt matching, our-team sender exclusion, and the audit watermark
(second run must only consider chat AFTER the first run's last complete day).

Throwaway sqlite DB; no pytest dependency:

    venv/Scripts/python tests/test_chat_audit.py
"""
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP_DB = os.path.join(tempfile.gettempdir(), "orderr_test_chat_audit.sqlite")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

from orderr_core.database import Base, engine, SessionLocal  # noqa: E402
from orderr_core.models.salesperson import Salesperson  # noqa: E402,F401
from orderr_core.models.customer_alias import CustomerAlias  # noqa: E402,F401
from orderr_core.models.customer import Customer  # noqa: E402
from orderr_core.models.customer_receipt import CustomerReceipt  # noqa: E402
from orderr_core.models.vasy_sales_item import VasySalesItem  # noqa: E402
from orderr_core.models.customer_chat_audit import CustomerChatAudit  # noqa: E402,F401
from orderr_core.models.customer_product_stats import CustomerProductStats  # noqa: E402,F401
from orderr_core.models.customer_product_alias import CustomerProductAlias  # noqa: E402,F401
from orderr_core.models.unclear_item_alias import UnclearItemAlias  # noqa: E402,F401
from orderr_core.models.noise_phrase import NoisePhrase  # noqa: E402,F401
from orderr_core.services import chat_audit  # noqa: E402

Base.metadata.create_all(bind=engine)

ANDROID = """12/03/2026, 6:10 pm - Messages and calls are end-to-end encrypted.
12/03/2026, 6:15 pm - Hotel Sai: Wings 5 kg
Curry cut 10 kg
12/03/2026, 6:20 pm - Fluffy Desk: ok noted wings 5 kg
13/03/2026, 7:00 am - Hotel Sai: Breast boneless 4 kg
13/03/2026, 5:30 pm - Hotel Sai: paid 15000 gpay
14/03/2026, 6:00 pm - Hotel Sai: Drumstick 3 kg
14/03/2026, 6:05 pm - Hotel Sai: <Media omitted>
"""
IOS = """[12/03/2026, 6:15:10 PM] Hotel Sai: Wings 5 kg
[13/03/2026, 5:30:00 PM] Hotel Sai: sent 15,000 phonepe
"""


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        check.failed = True


check.failed = False

# -- parsing ----------------------------------------------------------------
msgs = chat_audit.parse_chat(ANDROID)
check("android: 6 text/media messages (system line skipped)", len(msgs) == 6)
check("android: multi-line message joined", "Curry cut 10 kg" in msgs[0]["text"])
check("android: media flagged", msgs[-1]["media"])
check("ios: parsed with seconds + PM", len(chat_audit.parse_chat(IOS)) == 2
      and chat_audit.parse_chat(IOS)[1]["dt"].hour == 17)
check("month-first auto-detect", chat_audit.parse_chat("03/14/2026, 1:00 PM - A: hi")[0]["dt"].day == 14)

# -- audit ------------------------------------------------------------------
db = SessionLocal()
cust = Customer(restaurant_name="Hotel Sai", phone_number="919999999999")
db.add(cust)
db.commit()


def sale(d, prod, qty, vno="INV1", typ="Invoice"):
    db.add(VasySalesItem(invoice_date=d, voucher_no=vno, sale_type=typ, party_name="Hotel Sai",
                         party_key="hotel sai", customer_id=cust.id, product_name=prod, qty=qty))


# 12 Mar 6pm order -> delivery 12 Mar. Wings billed correctly, curry cut never billed.
sale(date(2026, 3, 12), "Chicken Wings with Skin", 5)
# 13 Mar 7am order -> breast billed only 2 kg (mismatch)
sale(date(2026, 3, 13), "Chicken Breast boneless", 2, "INV2")
# 14 Mar order: nothing billed at all that day
db.add(CustomerReceipt(receipt_no="PAY1", party_name="Hotel Sai", customer_id=cust.id, mode="bank",
                       amount=15000, receipt_date=date(2026, 3, 14)))
db.commit()

today = date(2026, 3, 15)
res = chat_audit.run_audit(db, cust, msgs, ["Fluffy Desk"], None, today)
st = {(o["date"], o["product"]): o["status"] for o in res["orders"]}
check("wings matched", st.get(("2026-03-12", "Wings")) == "ok")
check("curry cut flagged not billed", st.get(("2026-03-12", "Curry Cut")) == "missing")
check("breast qty mismatch", st.get(("2026-03-13", "Breast Boneless")) == "qty_mismatch")
check("drumstick: no invoice that day", st.get(("2026-03-14", "Drumstick")) == "no_invoice_day"
      or st.get(("2026-03-15", "Drumstick")) == "no_invoice_day")
check("our-team message not double-counted", res["orders"][0]["ordered"] == 5.0 or
      [o for o in res["orders"] if o["product"] == "Wings"][0]["ordered"] == 5.0)
check("payment matched to receipt", [p for p in res["payments"] if p["amount"] == 15000][0]["status"] == "ok")
check("media day reported", res["media_days"] and res["media_days"][0]["count"] == 1)
check("watermark = last complete day", chat_audit.watermark_for(res) == date(2026, 3, 14))

# -- watermark: second run only sees newer chat ------------------------------
db.add(CustomerChatAudit(customer_id=cust.id, audited_through=date(2026, 3, 14),
                         from_date=date(2026, 3, 12), to_date=date(2026, 3, 14)))
db.commit()
check("watermark stored", chat_audit.last_audit_watermark(db, cust.id) == date(2026, 3, 14))
more = ANDROID + "16/03/2026, 7:00 am - Hotel Sai: Liver 2 kg\n"
res2 = chat_audit.run_audit(db, cust, chat_audit.parse_chat(more), [], chat_audit.last_audit_watermark(db, cust.id),
                            date(2026, 3, 17))
check("2nd run ignores audited days", res2["messages_considered"] == 1 and
      [o["product"] for o in res2["orders"]] == ["Liver"])
res3 = chat_audit.run_audit(db, cust, msgs, [], date(2026, 3, 14), date(2026, 3, 15))
check("nothing new -> no watermark", res3["messages_considered"] == 0 and chat_audit.watermark_for(res3) is None)

db.close()
print("\nALL PASSED" if not check.failed else "\nFAILURES")
sys.exit(1 if check.failed else 0)
