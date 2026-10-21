"""
routes/billing.py  (refactored — orders now sourced directly from orderr-core)

Key change: billing no longer waits for an HTML "production report" upload to
learn what was ordered today. orderr-core already writes a row to `orders`
the moment a customer places an order, with:
    customer_phone   -- reliable join key (indexed)
    customer_name    -- hotel name as known to orderr-core
    parsed_items     -- [{"product","quantity","unit"}, ...]
    unclear_items    -- ["raw text the AI parser could not resolve", ...]
    business_date    -- "YYYY-MM-DD" string
    is_cancelled / status

Billing's job is now:
  1. Read today's orders straight from `orders` (no upload step).
  2. Lazily seed `OrderItemActual` rows from `parsed_items` the first time an
     order is viewed (this replaces the old HTML-report-driven seeding).
  3. Surface `unclear_items` as review items, tagged with a distinct reason.

Delivered quantities are entered manually per hotel (one Confirm button per
order). Photo/OCR capture was removed: handwritten quantities on the production
sheet were being misread (e.g. 8.5->6.5, 30->80) and, once auto-accepted, could
not be corrected before invoicing. Manual entry keeps the handwritten sheet as
the source of truth and every quantity editable until confirmed.
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from orderr_core.database import get_db
from orderr_core.models.actuals import OrderItemActual
from orderr_core.models.daily_rate import DailyRate
from orderr_core.models.invoice import Invoice
from orderr_core.models.ocr_unmatched import OcrUnmatchedLine
from orderr_core.services.invoice_generator import (
    InvoiceAlreadyExistsError,
    InvoiceHoldError,
    generate_invoice,
    reissue_invoice,
)
from orderr_core.services.invoice_pdf import generate_invoice_pdf, render_invoices_combined
from orderr_core.services.order_service import get_current_business_date_str
from orderr_core.services.rate_lookup import get_rate
from orderr_core.services.rate_parser import ACTIVE_PRODUCTS
from orderr_core.models.rate_override import CustomerRateOverride

import json
logger = logging.getLogger(__name__)
router = APIRouter()
from orderr_core.templating import make_templates
templates = make_templates()

def _today() -> date:
    """Billing 'today' = the current BUSINESS date (rolls over at the 9 PM
    cutoff), matching the Orders/Rates tabs. Using the plain calendar date here
    made Billing lag Orders by a day after the cutoff."""
    return date.fromisoformat(get_current_business_date_str())

ORDER_TIME_UNCLEAR_REASON = "Unclear at order time (could not parse product/quantity)"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@router.get("/billing", response_class=HTMLResponse)
def billing_home(request: Request):
    return templates.TemplateResponse(request, "billing.html", {})


# ---------------------------------------------------------------------------
# customer-rates (unchanged)
# ---------------------------------------------------------------------------

@router.get("/billing/api/customer-rates")
def api_customer_rates(db: Session = Depends(get_db)):
    today = _today()

    customers_raw = db.execute(
        text("""
            SELECT phone_number, restaurant_name
            FROM customers
            WHERE is_active = TRUE
            ORDER BY restaurant_name
        """)
    ).fetchall()
    customers = [{"phone": r[0], "name": r[1]} for r in customers_raw]

    product_rates = []
    for display_name, default_unit in ACTIVE_PRODUCTS:
        rr = get_rate(db, display_name, today)
        product_rates.append({
            "product":   display_name,
            "unit":      rr.unit or default_unit,
            "rate":      rr.rate_per_unit,
            "stale":     rr.not_confirmed_today,
            "rate_date": rr.rate_date.isoformat() if rr.rate_date else None,
        })

    overrides_raw = db.execute(
        text("""
            SELECT customer_phone, product, rate_per_unit
            FROM customer_rate_overrides
            WHERE effective_to IS NULL
            ORDER BY customer_phone, product
        """)
    ).fetchall()

    customer_overrides = [
        {"phone": r[0], "product": r[1], "rate": float(r[2])}
        for r in overrides_raw
    ]

    return {
        "customers":          customers,
        "products":           product_rates,
        "today":              today.isoformat(),
        "customer_overrides": customer_overrides,
    }


# ---------------------------------------------------------------------------
# save-rates (unchanged)
# ---------------------------------------------------------------------------

@router.post("/billing/api/save-rates")
async def api_save_rates(request: Request, db: Session = Depends(get_db)):
    body  = await request.json()
    today = _today()
    saved = []

    for item in body.get("rates", []):
        product = (item.get("product") or "").strip()
        unit    = (item.get("unit") or "kg").strip()
        try:
            rate_value = float(item.get("rate") or 0)
        except (TypeError, ValueError):
            continue
        if not product or rate_value <= 0:
            continue

        existing = db.scalars(
            select(DailyRate).where(
                DailyRate.product == product,
                DailyRate.business_date == today,
            )
        ).first()
        if existing:
            existing.rate_per_unit = rate_value
            existing.unit          = unit
        else:
            db.add(DailyRate(
                product=product,
                business_date=today,
                rate_per_unit=rate_value,
                unit=unit,
                source="dashboard",
                created_by="billing_dashboard",
            ))
        saved.append(product)

    db.commit()
    return {"ok": True, "saved": saved}


@router.post("/billing/api/save-customer-rate")
async def api_save_customer_rate(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    customer_phone = (body.get("customer_phone") or "").strip()
    rates = body.get("rates") or []

    if not customer_phone:
        return JSONResponse(status_code=400, content={"error": "No customer selected"})

    today = _today()
    saved = []

    for item in rates:
        product = (item.get("product") or "").strip()
        unit    = (item.get("unit") or "kg").strip()
        try:
            rate_value = float(item.get("rate") or 0)
        except (TypeError, ValueError):
            continue
        if not product or rate_value <= 0:
            continue

        # Deactivate any currently-active override for this customer+product
        active = db.scalars(
            select(CustomerRateOverride).where(
                CustomerRateOverride.customer_phone == customer_phone,
                CustomerRateOverride.product == product,
                CustomerRateOverride.effective_to.is_(None),
            )
        ).all()
        for ov in active:
            ov.effective_to = today

        db.add(CustomerRateOverride(
            customer_phone=customer_phone,
            product=product,
            rate_per_unit=rate_value,
            unit=unit,
            effective_from=today,
            effective_to=None,
        ))
        saved.append(product)

    db.commit()
    return {"ok": True, "saved": saved}


# ---------------------------------------------------------------------------
# set-item-rate — set/correct the rate for ONE hotel+product straight from an
# order card. Writes a customer-specific override (so it sticks for that hotel
# until changed, and survives global rate saves), keyed to today. If the order
# is already invoiced, the bill is regenerated in place (same invoice number)
# with the new rate — the only way to push a rate change into an issued bill,
# since rate_used is otherwise frozen at generation time. A synced Vasy voucher
# still has to be corrected by hand (the bot only ever CREATES vouchers).
# ---------------------------------------------------------------------------

@router.post("/billing/api/set-item-rate")
async def api_set_item_rate(request: Request, db: Session = Depends(get_db)):
    body     = await request.json()
    order_id = body.get("order_id")
    product  = (body.get("product") or "").strip()
    unit     = (body.get("unit") or "kg").strip()
    try:
        rate_value = float(body.get("rate") or 0)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "Invalid rate"})

    if order_id is None or not product or rate_value <= 0:
        return JSONResponse(
            status_code=400,
            content={"error": "order_id, product and a rate greater than 0 are required"},
        )

    # Resolve the hotel's phone authoritatively from the order (never trust a
    # client-supplied phone) — same precedence as _build_hotel_record: the
    # order's own phone first, then a name lookup as a fallback.
    order_row = db.execute(
        text("SELECT customer_phone, customer_name FROM orders WHERE id = :oid"),
        {"oid": order_id},
    ).fetchone()
    if not order_row:
        return JSONResponse(status_code=404, content={"error": f"Order {order_id} not found"})
    customer_phone = (order_row[0] or "").strip()
    if not customer_phone:
        crow = db.execute(
            text("""
                SELECT phone_number FROM customers
                WHERE LOWER(restaurant_name) = LOWER(:name)
                  AND is_active = TRUE
                ORDER BY id DESC
                LIMIT 1
            """),
            {"name": order_row[1]},
        ).fetchone()
        customer_phone = (crow[0] if crow else "").strip()
    if not customer_phone:
        return JSONResponse(
            status_code=422,
            content={"error": "This hotel has no phone on record, so a per-hotel "
                              "rate can't be keyed to it. Set the customer's phone first."},
        )

    today = _today()

    # Deactivate any currently-active override for this hotel+product, then add
    # the new one (mirrors save-customer-rate so both paths behave identically).
    active = db.scalars(
        select(CustomerRateOverride).where(
            CustomerRateOverride.customer_phone == customer_phone,
            CustomerRateOverride.product == product,
            CustomerRateOverride.effective_to.is_(None),
        )
    ).all()
    for ov in active:
        ov.effective_to = today
    db.add(CustomerRateOverride(
        customer_phone=customer_phone,
        product=product,
        rate_per_unit=rate_value,
        unit=unit,
        effective_from=today,
        effective_to=None,
    ))
    db.flush()

    # If already invoiced, rebuild the bill in place with the new rate.
    existing_invoice = db.scalars(
        select(Invoice).where(Invoice.order_id == order_id)
    ).first()
    if not existing_invoice:
        db.commit()
        return {"ok": True, "invoice_number": None}

    was_synced = bool(existing_invoice.vasy_synced_at)
    try:
        invoice = reissue_invoice(db, order_id, refresh_rates=True)
    except InvoiceHoldError as e:
        db.rollback()
        return JSONResponse(status_code=422, content={"error": str(e)})
    except Exception as e:
        db.rollback()
        logger.exception("set-item-rate reissue failed")
        return JSONResponse(status_code=500, content={"error": str(e)})

    # Cached PDF is now stale — drop it so it regenerates, and clear printed_at
    # so "Print all" reprints the corrected bill.
    try:
        for p in Path("invoices").glob(f"*_{invoice.invoice_number}.pdf"):
            p.unlink()
    except Exception:
        logger.warning("could not clear cached PDF for %s", invoice.invoice_number)
    invoice.printed_at = None
    db.commit()

    return {
        "ok":             True,
        "invoice_number": invoice.invoice_number,
        "total":          float(invoice.total),
        "vasy_warning":   was_synced,
    }


# ---------------------------------------------------------------------------
# Seeding: turn an orderr-core order into OrderItemActual + OcrUnmatchedLine
# rows the first time it's viewed. Idempotent — safe to call every request.
# ---------------------------------------------------------------------------

def _ensure_order_seeded(db: Session, order_id: int, parsed_items: list, unclear_items: list) -> None:
    """Mirror the order's CURRENT parsed_items / unclear_items into billing's
    OrderItemActual + OcrUnmatchedLine tables. Runs on every view and is
    idempotent, but — unlike the old seed-once version — it also *reconciles*
    changes the order-side unclear-items flow makes after billing first saw the
    order. Without this, a line the order has since resolved (moved into
    parsed_items and cleared from unclear_items) would keep being demanded in
    billing forever, because billing snapshotted it once and never looked again.
    """
    # ── 1. Seed parsed_items → actuals, per product. Doing this per-product
    #       (rather than only on the very first view) means a product the order
    #       flow adds later — e.g. when an unclear line is resolved into a real
    #       product — is picked up on the next billing view. Existing actuals
    #       (and their confirmed delivered quantities) are never touched.
    existing_products = {
        a.product for a in db.scalars(
            select(OrderItemActual).where(OrderItemActual.order_id == order_id)
        ).all()
    }
    for it in (parsed_items or []):
        product = (it.get("product") or "").strip()
        if not product or product in existing_products:
            continue
        try:
            qty = Decimal(str(it.get("quantity") or 0))
        except Exception:
            qty = Decimal("0")
        unit = (it.get("unit") or "kg").strip()

        db.add(OrderItemActual(
            order_id=order_id,
            product=product,
            ordered_quantity=qty,
            ordered_unit=unit,
            actual_quantity=None,
            actual_unit=unit,
            capture_source="orderr_core",
            confidence=None,
            confirmed_by=None,
            confirmed_at=None,
        ))
        existing_products.add(product)

    # ── 2. Reconcile order-time unclear lines with the order's CURRENT
    #       unclear_items. Add lines that newly appeared; auto-resolve lines the
    #       order no longer considers unclear (the order flow already handled
    #       them) so billing stops asking for input on an already-cleared line.
    current_unclear = {str(r).strip() for r in (unclear_items or []) if str(r).strip()}
    seeded_lines = db.scalars(
        select(OcrUnmatchedLine).where(
            OcrUnmatchedLine.order_id == order_id,
            OcrUnmatchedLine.reason == ORDER_TIME_UNCLEAR_REASON,
        )
    ).all()
    seeded_raw = {line.raw_line.strip() for line in seeded_lines}

    for raw in current_unclear:
        if raw not in seeded_raw:
            db.add(OcrUnmatchedLine(
                order_id=order_id,
                raw_line=raw,
                reason=ORDER_TIME_UNCLEAR_REASON,
                resolved=False,
            ))

    now = datetime.now(timezone.utc)
    for line in seeded_lines:
        if not line.resolved and line.raw_line.strip() not in current_unclear:
            line.resolved    = True
            line.resolved_by = "order_flow_sync"
            line.resolved_at = now

    db.commit()


# ---------------------------------------------------------------------------
# Build the dashboard record for one order. Seeds actuals/unclear-items from
# orderr-core data on first view.
# ---------------------------------------------------------------------------

def _build_hotel_record(db: Session, hotel_name: str, order_id: int) -> dict:
    order_row = db.execute(
        text("SELECT parsed_items, unclear_items, customer_phone FROM orders WHERE id = :oid"),
        {"oid": order_id},
    ).fetchone()
    parsed_items, unclear_items, order_phone = (order_row or (None, None, None))

    # parsed_items / unclear_items are stored as JSON text — deserialize if needed
    if isinstance(parsed_items, str):
        parsed_items = json.loads(parsed_items) if parsed_items else []
    if isinstance(unclear_items, str):
        unclear_items = json.loads(unclear_items) if unclear_items else []

    _ensure_order_seeded(db, order_id, parsed_items or [], unclear_items or [])

    existing_invoice = db.scalars(
        select(Invoice).where(Invoice.order_id == order_id)
    ).first()

    # The order already knows its customer — that is authoritative. Only fall
    # back to a name lookup if the order somehow has no phone on record. The old
    # code did the reverse (name LIKE first), which picked the wrong customer
    # when two restaurant names collided (e.g. "SAIRAT BIRYANI" also matched
    # "Sairat Biryani Ravet", and ORDER BY id DESC chose the wrong one).
    if order_phone:
        customer_phone = order_phone
    else:
        customer_row = db.execute(
            text("""
                SELECT phone_number FROM customers
                WHERE LOWER(restaurant_name) = LOWER(:name)
                  AND is_active = TRUE
                ORDER BY id DESC
                LIMIT 1
            """),
            {"name": hotel_name},
        ).fetchone()
        customer_phone = customer_row[0] if customer_row else ""

    actuals = db.scalars(
        select(OrderItemActual).where(OrderItemActual.order_id == order_id)
    ).all()

    rate_date = _today()
    items_out = []
    for a in actuals:
        # The rate that WOULD apply to this hotel+product right now (customer
        # override first, then today's / carried-forward daily rate). Surfaced
        # per line so the card can show it and let the operator correct it.
        rr = get_rate(db, a.product, rate_date, customer_phone or None)
        items_out.append({
            "actual_id":     a.id,
            "product":       a.product,
            "ordered_qty":   float(a.ordered_quantity) if a.ordered_quantity is not None else None,
            "actual_qty":    float(a.actual_quantity)  if a.actual_quantity  is not None else None,
            "unit":          a.actual_unit or a.ordered_unit,
            "needs_review":  a.confidence == "needs_review" and not a.confirmed_by,
            "review_reason": None,
            "rate":          float(rr.rate_per_unit) if rr.found else None,
            "rate_unit":     rr.unit,
            "rate_is_custom": rr.source == "override",
        })

    unmatched_lines = db.scalars(
        select(OcrUnmatchedLine).where(
            OcrUnmatchedLine.order_id == order_id,
            OcrUnmatchedLine.resolved == False,  # noqa
        )
    ).all()

    has_needs_review = any(i["needs_review"] for i in items_out)
    has_unmatched     = len(unmatched_lines) > 0
    any_actual_null   = any(i["actual_qty"] is None for i in items_out)
    all_actuals_null  = len(items_out) == 0 or all(i["actual_qty"] is None for i in items_out)

    if existing_invoice:
        status = "invoiced"
    elif all_actuals_null:
        # Order exists, items seeded, but no delivery confirmation yet.
        status = "pending"
    elif has_needs_review or has_unmatched or any_actual_null:
        # Partially confirmed counts as "needs attention", not "clear". Items
        # seeded from orderr_core start with actual_qty=NULL and confidence=NULL
        # (so they never trip needs_review); confirming ONE item must NOT flip
        # the whole hotel to Ready while the rest have no delivered quantity —
        # otherwise generate_invoice bills their ordered qty as if delivered.
        status = "unclear"
    else:
        status = "clear"

    return {
        "hotel_name":     hotel_name,
        "order_id":       order_id,
        "customer_phone": customer_phone,
        "status":         status,
        "invoice_number": existing_invoice.invoice_number if existing_invoice else None,
        "invoice_id":     existing_invoice.id             if existing_invoice else None,
        "invoice_total":  float(existing_invoice.total)   if existing_invoice else None,
        # Vasy-sync state (set by the bot's /billing/api/vasy-synced callback).
        "vasy_synced":     bool(existing_invoice and existing_invoice.vasy_synced_at),
        "vasy_voucher_no": existing_invoice.vasy_voucher_no if existing_invoice else None,
        "items":          items_out,
        "unmatched": [
            {"id": u.id, "raw_line": u.raw_line, "reason": u.reason}
            for u in unmatched_lines
        ],
    }


# ---------------------------------------------------------------------------
# today-results — now the single source of truth, straight off orderr-core
# ---------------------------------------------------------------------------

@router.get("/billing/api/today-results")
def api_today_results(db: Session = Depends(get_db)):
    today = _today()

    order_rows = db.execute(
        text("""
            SELECT id, customer_name FROM orders
            WHERE business_date = :today
              AND is_cancelled = FALSE
              AND status != 'cancelled'
            ORDER BY id
        """),
        {"today": today.isoformat()},
    ).fetchall()

    if not order_rows:
        return {
            "hotels":          [],
            "today":           today.isoformat(),
            "total_hotels":    0,
            "delivered_count": 0,
            "pending_count":   0,
        }

    hotels_out      = [_build_hotel_record(db, hotel_name=row[1], order_id=row[0]) for row in order_rows]
    total_hotels    = len(hotels_out)
    pending_count   = sum(1 for h in hotels_out if h["status"] == "pending")
    delivered_count = total_hotels - pending_count

    return {
        "hotels":          hotels_out,
        "today":           today.isoformat(),
        "total_hotels":    total_hotels,
        "delivered_count": delivered_count,
        "pending_count":   pending_count,
    }


# ---------------------------------------------------------------------------
# "Enter deliveries" screen — a report-styled sheet the accountant transcribes
# hand-written delivered quantities into. Mirrors the printed Daily Production
# Report so paper -> screen is a 1:1 read. Shows ONLY hotels still awaiting
# delivered-quantity entry (not invoiced, at least one blank/unconfirmed item);
# each hotel keeps its ORIGINAL number from the full day's order sequence so it
# still matches the printout even though done hotels are hidden. Per-hotel
# Confirm reuses /billing/api/confirm-items — no new write path.
# ---------------------------------------------------------------------------

@router.get("/billing/deliveries", response_class=HTMLResponse)
def deliveries_entry(request: Request, db: Session = Depends(get_db)):
    today = _today()

    order_rows = db.execute(
        text("""
            SELECT id, customer_name FROM orders
            WHERE business_date = :today
              AND is_cancelled = FALSE
              AND status != 'cancelled'
            ORDER BY id
        """),
        {"today": today.isoformat()},
    ).fetchall()

    # Number every hotel by the full order sequence FIRST, then keep only those
    # still awaiting entry. This preserves the printout's numbering (gaps where
    # already-invoiced/confirmed hotels drop out).
    awaiting = []
    for idx, row in enumerate(order_rows, 1):
        rec = _build_hotel_record(db, hotel_name=row[1], order_id=row[0])
        needs_entry = rec["status"] != "invoiced" and any(
            it["actual_qty"] is None or it["needs_review"] for it in rec["items"]
        )
        if needs_entry:
            rec["number"] = idx
            awaiting.append(rec)

    try:
        date_label = date.fromisoformat(today.isoformat()).strftime("%d %B %Y")
    except Exception:
        date_label = today.isoformat()

    return templates.TemplateResponse(request, "billing_deliveries.html", {
        "hotels":     awaiting,
        "today":      today.isoformat(),
        "date_label": date_label,
    })


# ---------------------------------------------------------------------------
# fix-item (unchanged)
# ---------------------------------------------------------------------------

@router.post("/billing/api/fix-item")
async def api_fix_item(request: Request, db: Session = Depends(get_db)):
    body          = await request.json()
    actual_id     = body.get("actual_id")
    confirmed_by  = (body.get("confirmed_by") or "plant_manager").strip()
    corrected_qty = body.get("actual_qty")

    actual = db.get(OrderItemActual, actual_id)
    if not actual:
        return JSONResponse(status_code=404, content={"error": "Item not found"})

    if corrected_qty is not None:
        try:
            actual.actual_quantity = Decimal(str(corrected_qty))
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Invalid quantity"})

    actual.confidence   = "auto"
    actual.confirmed_by = confirmed_by
    actual.confirmed_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# confirm-items — confirm every delivered quantity for one order in a single
# atomic write. Backs the one-button-per-hotel flow: validate the whole batch
# first, then apply, so a bad row never leaves the order half-confirmed.
# ---------------------------------------------------------------------------

@router.post("/billing/api/confirm-items")
async def api_confirm_items(request: Request, db: Session = Depends(get_db)):
    body         = await request.json()
    confirmed_by = (body.get("confirmed_by") or "plant_manager").strip()
    items        = body.get("items") or []

    if not items:
        return JSONResponse(status_code=400, content={"error": "No items to confirm"})

    # Pass 1: resolve + validate everything before touching any row.
    resolved: list[tuple[OrderItemActual, Decimal]] = []
    for entry in items:
        actual_id = entry.get("actual_id")
        actual    = db.get(OrderItemActual, actual_id)
        if not actual:
            return JSONResponse(status_code=404, content={"error": f"Item {actual_id} not found"})
        try:
            qty = Decimal(str(entry.get("actual_qty")))
        except Exception:
            return JSONResponse(status_code=400, content={"error": f"Invalid quantity for {actual.product}"})
        if qty <= 0:
            return JSONResponse(status_code=400, content={"error": f"Quantity for {actual.product} must be greater than 0"})
        resolved.append((actual, qty))

    # Pass 2: apply.
    now = datetime.now(timezone.utc)
    for actual, qty in resolved:
        actual.actual_quantity = qty
        actual.actual_unit     = actual.actual_unit or actual.ordered_unit
        actual.confidence      = "auto"
        actual.confirmed_by    = confirmed_by
        actual.confirmed_at    = now

    db.commit()
    return {"ok": True, "confirmed": [a.id for a, _ in resolved]}


# ---------------------------------------------------------------------------
# resolve-unmatched — now also resolves order-time unclear_items, not just OCR
# ---------------------------------------------------------------------------

@router.post("/billing/api/resolve-unmatched")
async def api_resolve_unmatched(request: Request, db: Session = Depends(get_db)):
    body         = await request.json()
    line_id      = body.get("line_id")
    product      = (body.get("product") or "").strip()
    qty          = body.get("qty")
    unit         = (body.get("unit") or "kg").strip()
    order_id     = body.get("order_id")
    confirmed_by = (body.get("confirmed_by") or "plant_manager").strip()

    line = db.get(OcrUnmatchedLine, line_id)
    if not line:
        return JSONResponse(status_code=404, content={"error": "Line not found"})

    if product and qty and order_id:
        try:
            quantity = Decimal(str(qty))
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Invalid quantity"})

        now = datetime.now(timezone.utc)

        # Order-time unclear items had no ordered_quantity at all (it was
        # never parsed) -- the resolved qty becomes both ordered & actual.
        # OCR-unmatched lines (from a photo) already have an ordered qty
        # seeded on the order; only set actual_quantity in that case.
        existing = None
        if line.reason == ORDER_TIME_UNCLEAR_REASON:
            existing = db.scalars(
                select(OrderItemActual).where(
                    OrderItemActual.order_id == order_id,
                    OrderItemActual.product == product,
                )
            ).first()

        if existing:
            existing.actual_quantity = quantity
            existing.actual_unit     = unit
            existing.confidence      = "auto"
            existing.confirmed_by    = confirmed_by
            existing.confirmed_at    = now
        else:
            db.add(OrderItemActual(
                order_id=order_id,
                product=product,
                ordered_quantity=quantity,
                ordered_unit=unit,
                actual_quantity=quantity,
                actual_unit=unit,
                capture_source="manual_resolve",
                confidence="auto",
                confirmed_by=confirmed_by,
                confirmed_at=now,
            ))

        line.resolved          = True
        line.resolved_product  = product
        line.resolved_quantity = quantity
        line.resolved_unit     = unit
        line.resolved_by       = confirmed_by
        line.resolved_at       = now
        db.commit()

    return {"ok": True}


# ---------------------------------------------------------------------------
# add-item — add a product to an order right on the billing card, even if the
# customer never ordered it (e.g. a standing daily item we always send). Creates
# a confirmed OrderItemActual so it bills at its delivered quantity. If the same
# product is already on the order, its quantity is updated instead of duplicated.
#
# For an already-invoiced order this only adds to the actuals — the caller then
# reissues the invoice (correct-invoice) to fold the new line into the bill.
# ---------------------------------------------------------------------------

@router.post("/billing/api/add-item")
async def api_add_item(request: Request, db: Session = Depends(get_db)):
    body         = await request.json()
    order_id     = body.get("order_id")
    product      = (body.get("product") or "").strip()
    unit         = (body.get("unit") or "kg").strip()
    confirmed_by = (body.get("confirmed_by") or "plant_manager").strip()

    if not order_id or not product:
        return JSONResponse(status_code=400, content={"error": "order_id and product required"})
    try:
        qty = Decimal(str(body.get("qty")))
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid quantity"})
    if qty <= 0:
        return JSONResponse(status_code=400, content={"error": "Quantity must be greater than 0"})

    now = datetime.now(timezone.utc)

    # Don't create a duplicate line — update the existing one if the product is
    # already on this order.
    existing = db.scalars(
        select(OrderItemActual).where(
            OrderItemActual.order_id == order_id,
            OrderItemActual.product == product,
        )
    ).first()

    if existing:
        existing.actual_quantity = qty
        existing.actual_unit     = unit
        existing.confidence      = "auto"
        existing.confirmed_by    = confirmed_by
        existing.confirmed_at    = now
        actual = existing
    else:
        actual = OrderItemActual(
            order_id=order_id,
            product=product,
            ordered_quantity=qty,
            ordered_unit=unit,
            actual_quantity=qty,
            actual_unit=unit,
            capture_source="manual_add",
            confidence="auto",
            confirmed_by=confirmed_by,
            confirmed_at=now,
        )
        db.add(actual)
        db.flush()

    db.commit()
    return {
        "ok":        True,
        "actual_id": actual.id,
        "product":   product,
        "unit":      unit,
        "qty":       float(qty),
        "updated":   existing is not None,
    }


# ---------------------------------------------------------------------------
# generate-invoice (unchanged)
# ---------------------------------------------------------------------------

@router.post("/billing/api/generate-invoice")
async def api_generate_invoice(request: Request, db: Session = Depends(get_db)):
    body              = await request.json()
    order_id          = body.get("order_id")
    customer_phone    = (body.get("customer_phone") or "").strip()
    business_date_str = body.get("business_date") or _today().isoformat()

    try:
        business_date = date.fromisoformat(business_date_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date"})

    if not customer_phone:
        row = db.execute(
            text("SELECT customer_phone FROM orders WHERE id = :oid"),
            {"oid": order_id},
        ).fetchone()
        if row:
            customer_phone = row[0] or ""

    try:
        invoice = generate_invoice(
            db=db,
            order_id=order_id,
            customer_phone=customer_phone,
            business_date=business_date,
        )
        return {
            "ok":             True,
            "invoice_number": invoice.invoice_number,
            "invoice_id":     invoice.id,
            "total":          float(invoice.total),
        }
    except InvoiceAlreadyExistsError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    except InvoiceHoldError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.exception("Unexpected invoice generation error")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# correct-invoice — fix an ALREADY-invoiced order in place (same invoice number).
# Applies any corrected delivered quantities, then rebuilds the existing invoice
# from the corrected actuals (reissue_invoice) so its line items / total / PDF
# match — without minting a new number or a duplicate downstream (Vasy).
#
# The cached PDF is deleted so it regenerates on next view. NOTE: this only fixes
# OrdeRR's records; the matching Vasy voucher must be edited in place by hand
# (the bot only ever CREATES vouchers — re-running it would duplicate).
# ---------------------------------------------------------------------------

@router.post("/billing/api/correct-invoice")
async def api_correct_invoice(request: Request, db: Session = Depends(get_db)):
    body         = await request.json()
    order_id     = body.get("order_id")
    confirmed_by = (body.get("confirmed_by") or "plant_manager").strip()
    items        = body.get("items") or []

    if order_id is None:
        return JSONResponse(status_code=400, content={"error": "order_id required"})

    # 1) Apply any corrected quantities first (same validate-then-apply as
    #    confirm-items, so a bad row never leaves the order half-corrected).
    resolved: list[tuple[OrderItemActual, Decimal]] = []
    for entry in items:
        actual = db.get(OrderItemActual, entry.get("actual_id"))
        if not actual:
            return JSONResponse(status_code=404,
                                content={"error": f"Item {entry.get('actual_id')} not found"})
        try:
            qty = Decimal(str(entry.get("actual_qty")))
        except Exception:
            return JSONResponse(status_code=400,
                                content={"error": f"Invalid quantity for {actual.product}"})
        if qty <= 0:
            return JSONResponse(status_code=400,
                                content={"error": f"Quantity for {actual.product} must be greater than 0"})
        resolved.append((actual, qty))

    now = datetime.now(timezone.utc)
    for actual, qty in resolved:
        actual.actual_quantity = qty
        actual.actual_unit     = actual.actual_unit or actual.ordered_unit
        actual.confidence      = "auto"
        actual.confirmed_by    = confirmed_by
        actual.confirmed_at    = now
    if resolved:
        db.flush()

    # 2) Rebuild the invoice in place from the corrected actuals.
    try:
        invoice = reissue_invoice(db, order_id)
    except InvoiceHoldError as e:
        db.rollback()
        return JSONResponse(status_code=422, content={"error": str(e)})
    except ValueError as e:
        db.rollback()
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        db.rollback()
        logger.exception("Unexpected invoice reissue error")
        return JSONResponse(status_code=500, content={"error": str(e)})

    # 3) Drop the cached PDF(s) for this invoice so it regenerates on next view.
    try:
        for p in Path("invoices").glob(f"*_{invoice.invoice_number}.pdf"):
            p.unlink()
    except Exception:
        logger.warning("could not clear cached PDF for %s", invoice.invoice_number)

    # 4) The printed copy is now stale — clear printed_at so "Print all" reprints
    #    this corrected bill (its total/items changed).
    invoice.printed_at = None
    db.commit()

    return {
        "ok":             True,
        "invoice_number": invoice.invoice_number,
        "invoice_id":     invoice.id,
        "total":          float(invoice.total),
    }


# ---------------------------------------------------------------------------
# vasy-synced — callback from the vasy-invoice-bot. After the bot posts a bill
# into Vasy ERP it POSTs the OrdeRR invoice number(s) here so the billing page
# can show a "Synced to Vasy" indicator. Idempotent: marking an already-synced
# bill just refreshes its voucher number, never errors. The bot posts its whole
# "already pushed" ledger each run (voucher_no only for bills pushed this run),
# so historical bills get backfilled the first time this runs.
#
# Body: {"invoices": [{"invoice_number": "FLUFFY-YYYYMMDD-NNN",
#                      "voucher_no": "INV14207"?}, ...]}
# ---------------------------------------------------------------------------

@router.post("/billing/api/vasy-synced")
async def api_vasy_synced(request: Request, db: Session = Depends(get_db)):
    body     = await request.json()
    entries  = body.get("invoices") or []
    now      = datetime.now(timezone.utc)
    marked, updated, unknown = [], [], []

    for entry in entries:
        number = (entry.get("invoice_number") or "").strip()
        if not number:
            continue
        voucher = (entry.get("voucher_no") or "").strip() or None

        invoice = db.scalars(
            select(Invoice).where(Invoice.invoice_number == number)
        ).first()
        if not invoice:
            unknown.append(number)
            continue

        if invoice.vasy_synced_at is None:
            invoice.vasy_synced_at = now
            marked.append(number)
        else:
            updated.append(number)
        # Always keep the latest voucher we were told about (don't blank a known
        # one when a later backfill call omits it).
        if voucher:
            invoice.vasy_voucher_no = voucher

    db.commit()
    return {
        "ok":            True,
        "marked":        marked,     # newly flagged synced this call
        "already":       updated,    # were already synced (voucher refreshed)
        "unknown":       unknown,    # invoice numbers OrdeRR doesn't have
        "marked_count":  len(marked),
        "already_count": len(updated),
        "unknown_count": len(unknown),
    }


# ---------------------------------------------------------------------------
# invoices/all (unchanged)
# ---------------------------------------------------------------------------

@router.get("/billing/api/invoices/all")
def api_invoices_all(db: Session = Depends(get_db)):
    rows = db.execute(
        text("""
            SELECT
                i.invoice_number,
                i.business_date,
                i.customer_phone,
                i.total,
                o.customer_name AS hotel_name,
                i.vasy_synced_at,
                i.vasy_voucher_no
            FROM invoices i
            LEFT JOIN orders o ON o.id = i.order_id
            ORDER BY i.business_date DESC, i.invoice_number DESC
        """)
    ).fetchall()

    return {
        "invoices": [
            {
                "invoice_number":  r[0],
                "business_date":   str(r[1])[:10],
                "customer_phone":  r[2],
                "total":           float(r[3]),
                "hotel_name":      r[4],
                "vasy_synced":     r[5] is not None,
                "vasy_voucher_no": r[6],
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# PDF download (unchanged)
# ---------------------------------------------------------------------------

@router.get("/billing/api/invoices/{invoice_number}/pdf")
def api_invoice_pdf_by_number(invoice_number: str, db: Session = Depends(get_db)):
    invoice = db.scalar(select(Invoice).where(Invoice.invoice_number == invoice_number))
    if not invoice:
        return JSONResponse(
            status_code=404,
            content={"error": f"Invoice {invoice_number!r} not found."},
        )

    row = db.execute(
        text("SELECT customer_name FROM orders WHERE id = :oid"),
        {"oid": invoice.order_id},
    ).first()
    hotel_name = row[0] if row else invoice.customer_phone

    safe_name = (hotel_name or "").strip().replace(" ", "_").replace("/", "-")
    pdf_path  = Path("invoices") / f"{safe_name}_{invoice.invoice_number}.pdf"

    # Always regenerate: the page geometry changed to full-A4/top-half, so any
    # legacy cached file would print at the old (config-hostile) size.
    try:
        generate_invoice_pdf(invoice, hotel_name)
    except Exception as e:
        logger.exception("PDF regeneration failed for invoice %s", invoice_number)
        return JSONResponse(status_code=500, content={"error": f"PDF generation failed: {e}"})

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=f"{safe_name}_{invoice.invoice_number}.pdf",
    )


# ---------------------------------------------------------------------------
# Bulk ZIP (unchanged)
# ---------------------------------------------------------------------------

@router.get("/billing/api/invoices/pdf/bulk")
def api_invoices_pdf_bulk(
    business_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if business_date:
        try:
            target_date = date.fromisoformat(business_date)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": f"Invalid date: {business_date!r}"})
    else:
        target_date = _today()

    invoices = db.scalars(
        select(Invoice)
        .where(Invoice.business_date == target_date)
        .order_by(Invoice.invoice_number)
    ).all()

    if not invoices:
        return JSONResponse(
            status_code=404,
            content={"error": f"No invoices found for {target_date.isoformat()}."},
        )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for invoice in invoices:
            row = db.execute(
                text("SELECT customer_name FROM orders WHERE id = :oid"),
                {"oid": invoice.order_id},
            ).first()
            hotel_name = row[0] if row else invoice.customer_phone

            safe_name = (hotel_name or "").strip().replace(" ", "_").replace("/", "-")
            pdf_path  = Path("invoices") / f"{safe_name}_{invoice.invoice_number}.pdf"

            # Always regenerate so the zip never ships a legacy half-A4 file.
            try:
                generate_invoice_pdf(invoice, hotel_name)
            except Exception:
                logger.exception(
                    "Skipping invoice %s in bulk zip -- PDF generation failed",
                    invoice.invoice_number,
                )
                continue

            if pdf_path.exists():
                zf.write(pdf_path, arcname=f"{safe_name}_{invoice.invoice_number}.pdf")

    zip_buffer.seek(0)
    filename = f"invoices-{target_date.isoformat()}.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Combined print sheet — one multi-page A4 PDF, one bill per page.
#
# This is the print-ready daily run: feed pre-cut half-sheets, open this PDF,
# print once → one bill per half-sheet. Because every page is a standard A4 with
# the invoice in the top half, the printer needs NO configuration (paper stays on
# plain A4 for this and every other job). Served inline so the browser's PDF
# viewer opens straight to Print.
#
# Print tracking (invoices.printed_at): scope="new" (default) prints only bills
# not yet printed for the date and marks them printed, so clicking "Print all"
# again after more invoicing prints ONLY the newly-added bills. scope="all"
# reprints every bill for the date and (re)marks them. The count printed / count
# already-done is returned in the X-Printed-Count / X-Already-Printed headers.
# ---------------------------------------------------------------------------

def _hotel_name_for(db: Session, invoice: Invoice) -> str:
    row = db.execute(
        text("SELECT customer_name FROM orders WHERE id = :oid"),
        {"oid": invoice.order_id},
    ).first()
    return row[0] if row else invoice.customer_phone


UNASSIGNED_AREA = "Unassigned"


def _area_map(db: Session) -> dict:
    """customer phone -> area (blank areas read as 'Unassigned')."""
    rows = db.execute(text("SELECT phone_number, area FROM customers")).all()
    return {r[0]: ((r[1] or "").strip() or UNASSIGNED_AREA) for r in rows}


@router.get("/billing/api/invoices/areas")
def api_invoice_areas(business_date: Optional[str] = None, db: Session = Depends(get_db)):
    """Areas present among the day's invoices, with total / not-yet-printed counts."""
    try:
        target_date = date.fromisoformat(business_date) if business_date else _today()
    except ValueError:
        return JSONResponse(status_code=400, content={"error": f"Invalid date: {business_date!r}"})
    amap = _area_map(db)
    invs = db.scalars(select(Invoice).where(Invoice.business_date == target_date)).all()
    out: dict = {}
    for inv in invs:
        a = amap.get(inv.customer_phone, UNASSIGNED_AREA)
        d = out.setdefault(a, {"area": a, "total": 0, "unprinted": 0})
        d["total"] += 1
        if inv.printed_at is None:
            d["unprinted"] += 1
    return {"areas": sorted(out.values(), key=lambda d: d["area"].lower())}


@router.get("/billing/api/invoices/pdf/print")
def api_invoices_pdf_print(
    business_date: Optional[str] = None,
    scope: str = "new",
    area: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if business_date:
        try:
            target_date = date.fromisoformat(business_date)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": f"Invalid date: {business_date!r}"})
    else:
        target_date = _today()

    all_for_date = db.scalars(
        select(Invoice)
        .where(Invoice.business_date == target_date)
        .order_by(Invoice.invoice_number)
    ).all()

    if area:
        amap = _area_map(db)
        all_for_date = [i for i in all_for_date
                        if amap.get(i.customer_phone, UNASSIGNED_AREA) == area]

    if not all_for_date:
        return JSONResponse(
            status_code=404,
            content={"error": f"No invoices found for {target_date.isoformat()}"
                              + (f" in area {area}." if area else ".")},
        )

    already = sum(1 for inv in all_for_date if inv.printed_at is not None)
    if scope == "all":
        to_print = list(all_for_date)
    else:
        to_print = [inv for inv in all_for_date if inv.printed_at is None]

    if not to_print:
        return JSONResponse(
            status_code=409,
            content={
                "error": (f"All {already} bill(s) for {target_date.isoformat()} "
                          "have already been printed."),
                "already_printed": already,
            },
        )

    items = [(inv, _hotel_name_for(db, inv)) for inv in to_print]

    try:
        pdf_bytes = render_invoices_combined(items)
    except Exception as e:
        logger.exception("Combined print PDF failed for %s", target_date)
        return JSONResponse(status_code=500, content={"error": f"Print sheet failed: {e}"})

    # Mark as printed only after the PDF built successfully.
    now = datetime.now(timezone.utc)
    for inv in to_print:
        inv.printed_at = now
    db.commit()

    filename = f"invoices-print-{target_date.isoformat()}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Printed-Count": str(len(to_print)),
            "X-Already-Printed": str(already),
        },
    )


# ---------------------------------------------------------------------------
# Single-invoice print — inline PDF, marks the bill printed, freely repeatable.
# Used by the per-bill "Print" button next to "Download PDF".
# ---------------------------------------------------------------------------

@router.get("/billing/api/invoices/{invoice_number}/print")
def api_invoice_print_one(invoice_number: str, db: Session = Depends(get_db)):
    invoice = db.scalar(select(Invoice).where(Invoice.invoice_number == invoice_number))
    if not invoice:
        return JSONResponse(status_code=404, content={"error": f"Invoice {invoice_number!r} not found."})

    hotel_name = _hotel_name_for(db, invoice)
    try:
        pdf_bytes = render_invoices_combined([(invoice, hotel_name)])
    except Exception as e:
        logger.exception("Single print PDF failed for %s", invoice_number)
        return JSONResponse(status_code=500, content={"error": f"Print failed: {e}"})

    invoice.printed_at = datetime.now(timezone.utc)
    db.commit()

    safe_name = (hotel_name or "").strip().replace(" ", "_").replace("/", "-")
    filename = f"{safe_name}_{invoice.invoice_number}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )