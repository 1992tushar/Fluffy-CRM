from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import os
from datetime import datetime, timezone, timedelta

from orderr_core.config import PLANT_NAME
from orderr_core.database import engine, Base, SessionLocal
from orderr_core.routes import webhook, dashboard
from orderr_core.routes.admin import router as admin_router
from orderr_core.routes.ledger import router as ledger_router          # ← ADD
# Billing module routers (absolute paths: /billing/*, /dashboard/*, /webhook/rates)
from orderr_core.routes.invoices import router as invoices_router
from orderr_core.routes.rates import router as rates_router
from orderr_core.routes.billing import router as billing_router
# Staff Ledger module router (absolute paths: /staff, /staff/api/*)
from orderr_core.routes.staff import router as staff_router
from orderr_core.routes.wastage import router as wastage_router
from orderr_core.routes.reminders import router as reminders_router
from orderr_core.routes.broadcast import router as broadcast_router
from orderr_core.routes.status_report import router as status_report_router
from orderr_core.services.reporter import send_daily_report
from orderr_core.services.pending_notifier import notify_salespersons_pending
from orderr_core.services.retry_scheduler import retry_failed_messages
from orderr_core.services.webhook_health import check_webhook_health

# Import ALL models — order matters for FK resolution
from orderr_core.models.salesperson import Salesperson
from orderr_core.models.customer import Customer
from orderr_core.models.order import Order
from orderr_core.models.inbound_message import InboundMessage  # ← reliability layer
from orderr_core.models.customer_product_alias import CustomerProductAlias  # noqa: F401
from orderr_core.models.customer_alias import CustomerAlias  # noqa: F401  ← Vasy split-customer merge
from orderr_core.models.customer_product_stats import CustomerProductStats  # noqa: F401  ← unit inference (FRD §5.1)

# Billing module models — billing owns these tables; shares OrdeRR's Base/metadata
from orderr_core.models.daily_rate import DailyRate                # noqa: F401
from orderr_core.models.rate_override import CustomerRateOverride  # noqa: F401
from orderr_core.models.actuals import OrderItemActual             # noqa: F401
from orderr_core.models.invoice import Invoice, InvoiceItem        # noqa: F401
from orderr_core.models.rate_unclear import RateUnclearItem        # noqa: F401
from orderr_core.models.ocr_unmatched import OcrUnmatchedLine      # noqa: F401

# Staff Ledger module models — owns employees/advances/leaves; shares Base/metadata
from orderr_core.models.employee import Employee                   # noqa: F401
from orderr_core.models.advance import Advance                     # noqa: F401
from orderr_core.models.advance_repayment import AdvanceRepayment   # noqa: F401
from orderr_core.models.leave import Leave                         # noqa: F401
from orderr_core.models.late_mark import LateMark                   # noqa: F401

# Analytics Phase 2 — Vasy money mirrors (read-only; Vasy = source of truth)
from orderr_core.models.customer_receipt import CustomerReceipt     # noqa: F401
from orderr_core.models.outstanding_snapshot import OutstandingSnapshot  # noqa: F401
from orderr_core.models.import_log import ImportLog                 # noqa: F401
from orderr_core.models.vasy_invoice import VasyInvoice, VasyInvoiceItem  # noqa: F401
from orderr_core.models.vasy_purchase import VasyPurchase, VasyPurchaseItem  # noqa: F401
from orderr_core.models.vasy_expense import VasyExpense                 # noqa: F401
from orderr_core.models.vasy_payment import VasyPayment                 # noqa: F401
from orderr_core.models.vasy_supplier_bill import VasySupplierBill       # noqa: F401
from orderr_core.models.vasy_sales_item import VasySalesItem             # noqa: F401

# Analytics — bad-debt write-offs (customer balances marked unrecoverable)
from orderr_core.models.bad_debt import BadDebt                          # noqa: F401

# Registers & Reminders — sundries register, critical notes, important dates
from orderr_core.models.sundry import SundryItem, SundryPurchase         # noqa: F401
from orderr_core.models.critical_note import CriticalNote                # noqa: F401
from orderr_core.models.important_date import ImportantDate              # noqa: F401

# Cash Book — manual drawer lines (drawings, bank deposits, float, spot counts)
from orderr_core.models.cash_entry import CashEntry                      # noqa: F401

# 5-Day Close (P2) — signed-off close history
from orderr_core.models.close_period import ClosePeriod                  # noqa: F401
# 5-Day Close (bank reconciliation) — uploaded bank statement mirror
from orderr_core.models.bank_transaction import BankTransaction          # noqa: F401
# Bank-recon — counterparty → category/remark alias, learned from manual tagging
from orderr_core.models.bank_counterparty_alias import BankCounterpartyAlias  # noqa: F401

# 📣 Broadcast — owner-curated order-reminder list (manual send)
from orderr_core.models.broadcast_recipient import BroadcastRecipient    # noqa: F401

# Outbound WhatsApp delivery-status journal (send failures are only visible here)
from orderr_core.models.wa_status_event import WaStatusEvent             # noqa: F401

# Analytics UI prefs — pinned/bookmarked subnav tabs (server-side, synced)
from orderr_core.models.analytics_pref import AnalyticsPref              # noqa: F401

# Manual accrual overheads (Salaries + Daily saving) folded into Business Net
from orderr_core.models.monthly_overhead import MonthlyOverhead          # noqa: F401


Base.metadata.create_all(bind=engine)


def _ensure_leaves_paid_column():
    """
    Lightweight migration: add leaves.paid to a pre-existing table.
    create_all() only creates missing tables, never alters existing ones, so
    the complementary-leave column must be added explicitly on older DBs.
    Idempotent and safe on both SQLite (local) and PostgreSQL (prod).
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "leaves" not in insp.get_table_names():
        return  # fresh DB — create_all already made the column
    cols = {c["name"] for c in insp.get_columns("leaves")}
    if "paid" in cols:
        return
    default = "false" if engine.dialect.name == "postgresql" else "0"
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE leaves ADD COLUMN paid BOOLEAN NOT NULL DEFAULT {default}"))
    print("✅ Migration: added leaves.paid column")


_ensure_leaves_paid_column()


def _ensure_customer_outstanding_and_nullable_phone():
    """
    Lightweight migration for the customer-outstanding import feature:

      1. Add `customers.outstanding` (receivables snapshot) if missing.
      2. Drop the NOT NULL constraint on `customers.phone_number` so customers
         imported from the outstanding sheet without a phone number can be
         stored (they're flagged RED on the dashboard).

    create_all() only creates missing tables, never alters existing ones, so
    both changes must be applied explicitly on older DBs. Idempotent and safe
    on both SQLite (local) and PostgreSQL (prod).
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "customers" not in insp.get_table_names():
        return  # fresh DB — create_all already made the current schema

    dialect = engine.dialect.name
    cols = {c["name"]: c for c in insp.get_columns("customers")}

    # ── 1. add outstanding column ──────────────────────────────────────────
    if "outstanding" not in cols:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN outstanding "
                "NUMERIC(12,2) NOT NULL DEFAULT 0"
            ))
        print("✅ Migration: added customers.outstanding column")

    # ── 2. make phone_number nullable ──────────────────────────────────────
    phone_col = cols.get("phone_number")
    phone_is_notnull = phone_col is not None and not phone_col.get("nullable", True)

    if phone_is_notnull:
        if dialect == "postgresql":
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE customers ALTER COLUMN phone_number DROP NOT NULL"
                ))
            print("✅ Migration: customers.phone_number is now nullable")
        elif dialect == "sqlite":
            # SQLite can't ALTER a column's NOT NULL in place — rebuild the
            # table. Batched inside one transaction; PRAGMA disables FK checks
            # during the swap. The tiny local test DB makes this cheap.
            with engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("""
                    CREATE TABLE customers_new (
                        id INTEGER NOT NULL PRIMARY KEY,
                        restaurant_name VARCHAR,
                        owner_name VARCHAR,
                        phone_number VARCHAR,
                        address VARCHAR,
                        city VARCHAR,
                        onboarding_status VARCHAR,
                        is_active BOOLEAN,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        area VARCHAR,
                        salesperson_id INTEGER,
                        is_daily_order_customer BOOLEAN,
                        ledger_token VARCHAR,
                        outstanding NUMERIC(12,2) NOT NULL DEFAULT 0,
                        FOREIGN KEY(salesperson_id) REFERENCES salespersons (id)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO customers_new (
                        id, restaurant_name, owner_name, phone_number, address,
                        city, onboarding_status, is_active, created_at, area,
                        salesperson_id, is_daily_order_customer, ledger_token,
                        outstanding
                    )
                    SELECT
                        id, restaurant_name, owner_name, phone_number, address,
                        city, onboarding_status, is_active, created_at, area,
                        salesperson_id, is_daily_order_customer, ledger_token,
                        outstanding
                    FROM customers
                """))
                conn.execute(text("DROP TABLE customers"))
                conn.execute(text("ALTER TABLE customers_new RENAME TO customers"))
                # recreate the indexes create_all() originally made
                conn.execute(text(
                    "CREATE UNIQUE INDEX ix_customers_phone_number "
                    "ON customers (phone_number)"
                ))
                conn.execute(text(
                    "CREATE INDEX ix_customers_salesperson_id "
                    "ON customers (salesperson_id)"
                ))
                conn.execute(text(
                    "CREATE UNIQUE INDEX ix_customers_ledger_token "
                    "ON customers (ledger_token)"
                ))
                conn.execute(text(
                    "CREATE INDEX ix_customers_id ON customers (id)"
                ))
                conn.execute(text("PRAGMA foreign_keys=ON"))
            print("✅ Migration: rebuilt customers table (phone_number nullable)")


_ensure_customer_outstanding_and_nullable_phone()


def _ensure_customer_credit_limit_column():
    """Add customers.credit_limit (nullable) if missing — Phase-3 credit
    limit / breach alert. Nullable, so a plain ADD COLUMN works on both SQLite
    and PostgreSQL. Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "customers" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("customers")}
    if "credit_limit" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE customers ADD COLUMN credit_limit NUMERIC(12,2)"))
    print("✅ Migration: added customers.credit_limit column")


_ensure_customer_credit_limit_column()


def _ensure_invoice_printed_at_column():
    """Add invoices.printed_at (nullable) if missing — powers print tracking so
    "Print all" only prints newly-invoiced bills. Nullable, so a plain ADD COLUMN
    works on both SQLite and PostgreSQL. Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "invoices" not in insp.get_table_names():
        return  # fresh DB — create_all already made the column
    cols = {c["name"] for c in insp.get_columns("invoices")}
    if "printed_at" in cols:
        return
    coltype = ("TIMESTAMP WITH TIME ZONE"
               if engine.dialect.name == "postgresql" else "DATETIME")
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE invoices ADD COLUMN printed_at {coltype}"))
    print("✅ Migration: added invoices.printed_at column")


_ensure_invoice_printed_at_column()


def _ensure_invoice_vasy_sync_columns():
    """Add invoices.vasy_synced_at / .vasy_voucher_no (nullable) if missing — the
    vasy-invoice-bot's POST /billing/api/vasy-synced callback sets them once a bill
    is created in Vasy, powering the "Synced to Vasy" indicator. Nullable, so a plain
    ADD COLUMN works on both SQLite and PostgreSQL. Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "invoices" not in insp.get_table_names():
        return  # fresh DB — create_all already made the columns
    cols = {c["name"] for c in insp.get_columns("invoices")}
    ts_type = ("TIMESTAMP WITH TIME ZONE"
               if engine.dialect.name == "postgresql" else "DATETIME")
    with engine.begin() as conn:
        if "vasy_synced_at" not in cols:
            conn.execute(text(f"ALTER TABLE invoices ADD COLUMN vasy_synced_at {ts_type}"))
            print("✅ Migration: added invoices.vasy_synced_at column")
        if "vasy_voucher_no" not in cols:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN vasy_voucher_no VARCHAR(30)"))
            print("✅ Migration: added invoices.vasy_voucher_no column")


_ensure_invoice_vasy_sync_columns()


def _ensure_vasy_expense_mode_columns():
    """Add vasy_expenses.cash_paid / .noncash_paid (nullable) if missing —
    Cash Book cross-check needs the Expense Register's per-expense payment-mode
    split. create_all never alters existing tables, so add explicitly.
    Idempotent; safe on SQLite and PostgreSQL."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "vasy_expenses" not in insp.get_table_names():
        return  # fresh DB — create_all already made the current schema
    cols = {c["name"] for c in insp.get_columns("vasy_expenses")}
    with engine.begin() as conn:
        for col in ("cash_paid", "noncash_paid"):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE vasy_expenses ADD COLUMN {col} NUMERIC(14,2)"))
                print(f"✅ Migration: added vasy_expenses.{col} column")


_ensure_vasy_expense_mode_columns()


def _ensure_bank_transaction_categorize_columns():
    """Add the bank-recon columns (txn_type/counterparty_raw/counterparty_key/
    category/remark/reviewed/reviewed_by/reviewed_at) to a pre-existing
    bank_transactions table if missing. Nullable/defaulted, so a plain ADD
    COLUMN works on both SQLite and PostgreSQL. Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "bank_transactions" not in insp.get_table_names():
        return  # fresh DB — create_all already made the current schema
    cols = {c["name"] for c in insp.get_columns("bank_transactions")}
    ts_type = ("TIMESTAMP WITH TIME ZONE"
               if engine.dialect.name == "postgresql" else "DATETIME")
    with engine.begin() as conn:
        for col, coltype in (
            ("txn_type", "VARCHAR"), ("counterparty_raw", "VARCHAR"),
            ("counterparty_key", "VARCHAR"), ("category", "VARCHAR"),
            ("remark", "TEXT"), ("reviewed_by", "VARCHAR"), ("reviewed_at", ts_type),
            ("party_type", "VARCHAR"), ("party_id", "VARCHAR"), ("party_label", "VARCHAR"),
        ):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE bank_transactions ADD COLUMN {col} {coltype}"))
                print(f"Migration: added bank_transactions.{col} column")
        if "reviewed" not in cols:
            default = "FALSE" if engine.dialect.name == "postgresql" else "0"
            conn.execute(text(f"ALTER TABLE bank_transactions ADD COLUMN reviewed BOOLEAN DEFAULT {default}"))
            print("Migration: added bank_transactions.reviewed column")


_ensure_bank_transaction_categorize_columns()


def _ensure_bank_party_id_is_string():
    """party_id started as INTEGER (customer/employee PKs only); adding
    suppliers (identified by VasySupplierBill's string vendor_key, no numeric
    master) needs it widened to VARCHAR. SQLite already tolerates this with
    no DDL change (weak column typing); Postgres needs an explicit ALTER.
    Idempotent — checks the live column type first."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    for table in ("bank_transactions", "bank_counterparty_aliases"):
        if table not in insp.get_table_names():
            continue
        cols = {c["name"]: c["type"] for c in insp.get_columns(table)}
        if "party_id" in cols and "INTEGER" in str(cols["party_id"]).upper():
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN party_id TYPE VARCHAR USING party_id::text"))
            print(f"Migration: widened {table}.party_id to VARCHAR")


_ensure_bank_party_id_is_string()


def _ensure_bank_counterparty_alias_party_columns():
    """Add party_type/party_id/party_label to a pre-existing
    bank_counterparty_aliases table if missing. Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "bank_counterparty_aliases" not in insp.get_table_names():
        return  # fresh DB — create_all already made the current schema
    cols = {c["name"] for c in insp.get_columns("bank_counterparty_aliases")}
    with engine.begin() as conn:
        for col, coltype in (
            ("party_type", "VARCHAR"), ("party_id", "VARCHAR"), ("party_label", "VARCHAR"),
        ):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE bank_counterparty_aliases ADD COLUMN {col} {coltype}"))
                print(f"Migration: added bank_counterparty_aliases.{col} column")


_ensure_bank_counterparty_alias_party_columns()


def _seed_bank_counterparty_aliases():
    """One-time seed of counterparty aliases already confirmed against the
    owner's actual statement data (see bank_categorize.py). Idempotent —
    skips any alias_key that already exists, so it never clobbers a manual
    edit."""
    from orderr_core.database import SessionLocal
    from orderr_core.services.bank_categorize import seed_known_aliases

    db = SessionLocal()
    try:
        created = seed_known_aliases(db)
        if created:
            print(f"Seeded {created} bank counterparty alias(es)")
    finally:
        db.close()


_seed_bank_counterparty_aliases()


def _ensure_sundry_purchase_invoice_columns():
    """Add sundry_purchases.invoice_drive_file_id/invoice_drive_link/
    invoice_filename (nullable) to a pre-existing table if missing — powers
    the optional invoice-photo attachment on the sundries register, stored in
    the dedicated Google Drive account rather than in this DB. Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "sundry_purchases" not in insp.get_table_names():
        return  # fresh DB — create_all already made the columns
    cols = {c["name"] for c in insp.get_columns("sundry_purchases")}
    with engine.begin() as conn:
        for col in ("invoice_drive_file_id", "invoice_drive_link", "invoice_filename"):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE sundry_purchases ADD COLUMN {col} VARCHAR"))
                print(f"Migration: added sundry_purchases.{col} column")


_ensure_sundry_purchase_invoice_columns()


def _ensure_bank_transaction_bill_columns():
    """Add bank_transactions.bill_drive_file_id/bill_drive_link/bill_filename
    (nullable) to a pre-existing table if missing — powers the optional
    supporting-bill attachment on the bank-recon screen, stored in the same
    dedicated Google Drive account as sundries invoices. Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "bank_transactions" not in insp.get_table_names():
        return  # fresh DB — create_all already made the columns
    cols = {c["name"] for c in insp.get_columns("bank_transactions")}
    with engine.begin() as conn:
        for col in ("bill_drive_file_id", "bill_drive_link", "bill_filename"):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE bank_transactions ADD COLUMN {col} VARCHAR"))
                print(f"Migration: added bank_transactions.{col} column")


_ensure_bank_transaction_bill_columns()


def _ensure_critical_note_attachment_columns():
    """Add critical_notes.attachment_drive_file_id/attachment_drive_link/
    attachment_filename (nullable) to a pre-existing table if missing —
    powers the optional attachment on the Reminders notes register, stored
    in the same dedicated Google Drive account as sundries invoices.
    Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "critical_notes" not in insp.get_table_names():
        return  # fresh DB — create_all already made the columns
    cols = {c["name"] for c in insp.get_columns("critical_notes")}
    with engine.begin() as conn:
        for col in ("attachment_drive_file_id", "attachment_drive_link", "attachment_filename"):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE critical_notes ADD COLUMN {col} VARCHAR"))
                print(f"Migration: added critical_notes.{col} column")


_ensure_critical_note_attachment_columns()


from orderr_core.constants import IST

# Track last report time for health check
_last_report_time: str = "Never"


# ── Scheduler job wrappers ────────────────────────────────────────────────────

def daily_report_job():
    global _last_report_time
    db = SessionLocal()
    try:
        print("\n⏰ AUTO SCHEDULER — Daily Report triggered")
        send_daily_report(db)
        _last_report_time = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    finally:
        db.close()


def salesperson_notification_job():
    db = SessionLocal()
    try:
        print("\n⏰ AUTO SCHEDULER — Salesperson Notifications triggered")
        notify_salespersons_pending(db)
    finally:
        db.close()


def manager_digest_job():
    db = SessionLocal()
    try:
        print("\n⏰ AUTO SCHEDULER — Manager Analytics Digest triggered")
        from orderr_core.services.reporter import send_manager_digest
        send_manager_digest(db)
    finally:
        db.close()


def retry_failed_messages_job():
    retry_failed_messages()  # manages its own DB session internally


def webhook_health_job():
    check_webhook_health()



# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):

    report_time  = os.getenv("REPORT_TIME", "22:00")
    hour, minute = map(int, report_time.split(":"))
    scheduler    = BackgroundScheduler()

    # Daily report (configurable via REPORT_TIME env var)
    scheduler.add_job(
        daily_report_job,
        CronTrigger(hour=hour, minute=minute, timezone="Asia/Kolkata"),
        id="daily_report", name=f"Daily Report at {report_time} IST",
    )

    # Customer reminders: auto-send removed 2026-07-14 — now manual via the
    # 📣 Broadcast screen (owner-curated list, /dashboard/broadcast).

    # Salesperson notifications PROD: hour=23, minute=00
    scheduler.add_job(
        salesperson_notification_job,
        CronTrigger(hour=23, minute=15, timezone="Asia/Kolkata"),
        id="salesperson_notifications", name="Salesperson Notifications at 23:05 IST",
    )

    # Management summary job removed 2026-07-14 — replaced by the live status
    # page (/r/<REPORT_LINK_KEY>, config.report_url()).

    # Manager analytics digest — daily (configurable via DIGEST_TIME env var)
    digest_time = os.getenv("DIGEST_TIME", "09:00")
    d_hour, d_min = map(int, digest_time.split(":"))
    scheduler.add_job(
        manager_digest_job,
        CronTrigger(hour=d_hour, minute=d_min, timezone="Asia/Kolkata"),
        id="manager_digest", name=f"Manager Analytics Digest at {digest_time} IST",
    )

    # Keep-alive ping — every 10 min (prevents Render spin-down)
    scheduler.add_job(
        lambda: print("💓 keep-alive ping"),
        IntervalTrigger(minutes=10),
        id="keep_alive", name="Keep-Alive Ping",
    )

    # Retry failed messages — every 1 min (reliability layer)
    scheduler.add_job(
        retry_failed_messages_job,
        IntervalTrigger(minutes=1),
        id="retry_failed_messages", name="Retry Failed Messages",
    )

    # Webhook health check — every 30 min (reliability layer)
    scheduler.add_job(
        webhook_health_job,
        IntervalTrigger(minutes=30),
        id="webhook_health", name="Webhook Health Check",
    )

    scheduler.start()
    app.state.scheduler = scheduler

    print("\n✅ OrdeRR Scheduler Started!")
    print(f"   📅 Daily report (email)  → Every day at {report_time} IST")
    print(f"   📣 Customer reminders    → Manual only (Broadcast screen)")
    print(f"   📋 Salesperson alerts    → Every day at 23:15 IST")
    print(f"   📊 Manager reports       → Live status page (/r/…)")
    print(f"   💓 Keep-alive ping       → Every 10 minutes")
    print(f"   🔁 Retry failed msgs     → Every 1 minute")
    print(f"   🩺 Webhook health check  → Every 30 minutes\n")

    yield

    app.state.scheduler.shutdown()
    print("\n🛑 OrdeRR Scheduler Stopped")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="OrdeRR",
    description="WhatsApp Order Automation for Fluffy Plant",
    version="2.2.0",
    lifespan=lifespan,
)

app.include_router(webhook.router,   prefix="/webhook",   tags=["Webhook"])
app.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
app.include_router(admin_router,     prefix="/admin",     tags=["Admin"])
app.include_router(ledger_router,    prefix="/ledger",    tags=["Ledger"])   # ← ADD

# ── Billing module (merged) — routers carry their own absolute paths ──────────
app.include_router(invoices_router, tags=["Billing"])
app.include_router(rates_router,    tags=["Billing"])
app.include_router(billing_router,  tags=["Billing"])

# ── Staff Ledger module (merged) — router carries its own absolute paths ───────
app.include_router(staff_router,    tags=["Staff"])
app.include_router(wastage_router,  prefix="/dashboard", tags=["Analytics"])
app.include_router(reminders_router, prefix="/dashboard", tags=["Reminders"])
app.include_router(broadcast_router, prefix="/dashboard", tags=["Broadcast"])
app.include_router(status_report_router, tags=["Reports"])   # public /r/<key>, no auth


@app.get("/")
def root():
    return {
        "app"   : "OrdeRR",
        "plant" : PLANT_NAME,
        "status": "running",
    }


@app.get("/health")
def health_check():
    from orderr_core.database import SessionLocal
    from sqlalchemy import text

    db_status = "ok"
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
    except Exception as e:
        db_status = f"error: {str(e)}"

    scheduler    = app.state.scheduler if hasattr(app.state, "scheduler") else None
    job_count    = len(scheduler.get_jobs()) if scheduler else 0
    sched_status = "running" if (scheduler and scheduler.running) else "stopped"

    return {
        "status"          : "ok",
        "app"             : "OrdeRR",
        "plant"           : PLANT_NAME,
        "database"        : db_status,
        "scheduler"       : sched_status,
        "scheduler_jobs"  : job_count,
        "last_report_time": _last_report_time,
        "time_ist"        : datetime.now(IST).strftime("%d %b %Y %I:%M %p"),
    }
