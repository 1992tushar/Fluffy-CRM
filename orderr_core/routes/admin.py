"""
admin.py — Admin API routes (Basic Auth protected)

Salesperson:  GET/POST /admin/salespersons
              PUT/DELETE /admin/salespersons/{id}
Customer:     GET /admin/customers
              GET /admin/customers/unassigned
              GET /admin/customers/{id}/orders
              POST /admin/customers/{id}/assign
              PUT  /admin/customers/{id}/status
              PUT  /admin/customers/{id}            ← full customer edit (new)
              POST /customers
Pending:      GET /admin/pending
Window:       GET /admin/window-status
Unclear:      GET /admin/unclear-items
              GET /admin/unclear-items/aliases
              GET /admin/product-names
              POST /admin/unclear-items/resolve
              POST /admin/unclear-items/resolve-qty   ← unit-inference resolution
              POST /admin/unclear-items/resolve-word-qty  ← word-quantity resolution (spec §6)
              DELETE /admin/unclear-items/aliases/{id}
Customer Aliases:
              GET  /admin/customer-aliases
              GET  /admin/customer-aliases/{phone}
              POST /admin/customer-aliases
              DELETE /admin/customer-aliases/{alias_id}
"""

import os
import re
import json
import logging
from datetime import date, datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from orderr_core.auth import require_auth
from orderr_core.database import get_db
from orderr_core.models.customer import Customer
from orderr_core.models.order import Order
from orderr_core.models.salesperson import Salesperson
from orderr_core.models.inbound_message import InboundMessage
from orderr_core.models.unclear_item_alias import UnclearItemAlias
from orderr_core.models.customer_product_alias import CustomerProductAlias
from orderr_core.services.customer_service import normalize_phone, validate_phone
from orderr_core.services.notifier import send_whatsapp_message
from orderr_core.services.pending_orders import get_pending_customers, get_delivery_date_for_now
from orderr_core.services.template_parser import (
    PRODUCT_DEFINITIONS,
    normalize_alias_key,
    alias_keys_match,
    strip_list_marker,
)
from orderr_core.services.customer_service import create_customer_manually
from orderr_core.services.customer_service import import_customers_from_xlsx
from orderr_core.services.order_service import process_incoming_order
from orderr_core.services.order_service import get_current_business_date_str, get_current_business_date, RESET_HOUR
from orderr_core.services.notifier import send_manager_alert
from orderr_core.services.customer_service import get_customer_by_phone
from orderr_core.models.noise_phrase import NoisePhrase
from orderr_core.services.notifier import send_whatsapp_message
from sqlalchemy.exc import IntegrityError

from fastapi.responses import HTMLResponse
from orderr_core.services.reporter import generate_daily_report, _build_print_html, get_todays_customer_notes

logger = logging.getLogger(__name__)


router     = APIRouter()
from orderr_core.config import MANAGER_PHONE, PLANT_NAME
from orderr_core.constants import IST


# ── Schemas ───────────────────────────────────────────────────────────────────

class SalespersonCreate(BaseModel):
    name: str
    phone: str
    area: str        # remove Optional and default

class SalespersonUpdate(BaseModel):
    name:   Optional[str]  = None
    phone:  Optional[str]  = None
    area:   Optional[str]  = None
    active: Optional[bool] = None

class CustomerAssign(BaseModel):
    area:           Optional[str] = None
    salesperson_id: Optional[int] = None

class CustomerStatus(BaseModel):
    is_active: bool

class CustomerCreate(BaseModel):
    phone: str
    restaurant_name: str
    area: Optional[str] = None
    salesperson_id: Optional[int] = None

class CustomerEdit(BaseModel):
    """Full-edit payload for a customer (Edit modal). All fields optional —
    only provided fields are updated (partial update via exclude_unset)."""
    restaurant_name:         Optional[str]  = None
    owner_name:               Optional[str]  = None
    phone_number:             Optional[str]  = None
    address:                  Optional[str]  = None
    city:                     Optional[str]  = None
    area:                     Optional[str]  = None
    salesperson_id:           Optional[int]  = None
    is_daily_order_customer:  Optional[bool] = None

class CustomerBulkStatus(BaseModel):
    """Bulk activate/deactivate a set of customers."""
    customer_ids: List[int]
    is_active: bool

class CustomerBulkAssign(BaseModel):
    """Bulk assign area and/or salesperson to a set of customers."""
    customer_ids: List[int]
    salesperson_id: Optional[int] = None
    area: Optional[str] = None

class NextDayOverride(BaseModel):
    is_next_day: bool

class PostOrderPayload(BaseModel):
    """Payload for admin posting an order on behalf of a customer."""
    message: str
    acknowledge_credit: bool = False   # set true to post past a credit warning

class CancelOrderPayload(BaseModel):
    reason: Optional[str] = None

class NoisePhraseCreate(BaseModel):
    raw_text: str

class ResolveUnclearItem(BaseModel):
    raw_text: str
    canonical_product_name: str
    customer_phone: Optional[str] = None
    scope: str = "customer"  # "customer" | "global"

class CustomerAliasCreate(BaseModel):
    customer_phone: str
    raw_text: str
    canonical_product_name: str

class ResolveQtyAmbiguity(BaseModel):
    """
    Payload for resolving a quantity-ambiguous unclear item (FRD §3.4, §5.3).
    The manager picks the confirmed unit for a specific (order, product) pair
    from the Unclear tab kg/g toggle UI.
    """
    order_id:       int
    product:        str          # canonical product name
    quantity:       float        # the original raw number
    confirmed_unit: str          # "kg" or "g"
    customer_phone: str

class ResolveWordQtyItem(BaseModel):
    """
    Payload for resolving a __word_qty__ unclear item (spec §6).

    order_id:       the specific order being resolved right now
    product:        canonical product name (manager-confirmed)
    quantity:       manager-confirmed quantity (pre-filled from sentinel, editable)
    unit:           manager-confirmed unit — "kg" or "nos"
    customer_phone: used to scope the product-only alias (Option B)
    """
    order_id:       int
    product:        str
    quantity:       float
    unit:           str
    customer_phone: str


# ── Helpers ───────────────────────────────────────────────────────────────────

from orderr_core.utils import safe_list as _safe_list


LINE_RE = re.compile(
    r"^(.+?)\s*[-:]?\s*([\d\.]+)\s*(kg|kgs|nos|pcs|pis|psc|pc|pieces?|piece|pies|k)?\s*$",
    re.IGNORECASE,
)


def _extract_product_name(raw_line: str) -> tuple[str, float]:
    """
    Given a raw unclear line like "Raan -5", "kaleji 2kg", "tandoori chicken 30pis",
    returns (product_name_lower, quantity).
    """
    line_clean = re.sub(r'__+', '', raw_line).strip()
    # Drop a leading list marker ("1)", "2.") so it isn't mistaken for the name
    # or the quantity — mirrors the parser, which strips it before matching.
    line_clean = strip_list_marker(line_clean)
    line_clean = re.sub(r'(\d+)\s*k\b', r'\1 kg', line_clean)
    m = LINE_RE.match(line_clean)
    if m:
        name = m.group(1).strip().lower()
        try:
            qty = float(m.group(2))
        except (TypeError, ValueError):
            qty = 1.0
        return name, qty
    fallback = re.sub(r'\s*[-:]?\s*[\d\.]+\s*[a-zA-Z]*\s*$', '', line_clean).strip()
    return (fallback.lower() if fallback else line_clean.lower()), 1.0


def _extract_product_name_from_line(line: str) -> str:
    name, _ = _extract_product_name(line)
    return name


def _extract_qty_from_line(line: str) -> float:
    # Strip a leading list marker first — otherwise "1)Chicken big --- 30 kg"
    # returns 1.0 (the "1" from "1)") instead of the real quantity 30.
    cleaned = strip_list_marker(line)
    m = re.search(r"([\d]+(?:[./][\d]+)?)", cleaned)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 1.0


def _get_unit_for_canonical(canonical: str) -> str:
    for display_name, unit, _ in PRODUCT_DEFINITIONS:
        if display_name.lower() == canonical.lower():
            return unit
    return "kg"


# ── Retroactive patch helpers ─────────────────────────────────────────────────
# All functions here use the ORM directly (no json.dumps, no raw SQL).
# JSONB columns store and return native Python lists — treat them that way.

def _retroactive_remove_noise(normalized: str, db: Session) -> int:
    """
    Remove all unclear_items matching `normalized` from every existing order.
    Uses ORM — assigns native Python lists directly to JSONB columns.
    """
    orders = db.query(Order).filter(
        Order.unclear_items.isnot(None),
        Order.is_cancelled == False,
    ).all()

    patched_ids = []
    for order in orders:
        unclear = _safe_list(order.unclear_items)
        if not unclear:
            continue

        remaining = []
        changed   = False
        for raw_line in unclear:
            product_name, _ = _extract_product_name(raw_line)
            if product_name == normalized:
                changed = True
            else:
                remaining.append(raw_line)

        if changed:
            # ✅ Assign native list — no json.dumps needed for JSONB
            order.unclear_items = remaining if remaining else []
            if not remaining:
                order.is_unclear = False
            patched_ids.append(order.id)

    if patched_ids:
        db.commit()
        print(f"✅ Noise patch committed for orders: {patched_ids}")

    return len(patched_ids)


def _retroactive_patch_global(raw: str, canonical: str, db: Session) -> int:
    """
    Move unclear items matching `raw` into parsed_items for ALL orders.
    Uses ORM — assigns native Python lists directly to JSONB columns.
    Skips __qty_ambiguous__ sentinels (those are resolved by resolve-qty endpoint).
    Skips __word_qty__ sentinels (those are resolved by resolve-word-qty endpoint).
    """
    orders = db.query(Order).filter(
        Order.unclear_items.isnot(None),
    ).all()

    unit = _get_unit_for_canonical(canonical)
    patched_ids = []

    for order in orders:
        unclear = _safe_list(order.unclear_items)
        parsed  = list(_safe_list(order.parsed_items))
        if not unclear:
            continue

        remaining     = []
        matched_lines = []
        for line in unclear:
            # Never retroactively patch sentinels — those require explicit
            # manager resolution via their respective endpoints
            if line.startswith("__qty_ambiguous__") or line.startswith("__word_qty__"):
                remaining.append(line)
                continue
            product_part = _extract_product_name_from_line(line)
            if (alias_keys_match(line, raw)
                    or product_part == raw or product_part.startswith(raw)
                    or raw in product_part):
                matched_lines.append(line)
            else:
                remaining.append(line)

        if not matched_lines:
            continue

        for line in matched_lines:
            qty = _extract_qty_from_line(line)
            parsed.append({"product": canonical, "quantity": qty, "unit": unit})

        # ✅ Assign native lists — no json.dumps needed for JSONB
        order.parsed_items  = parsed
        order.unclear_items = remaining if remaining else []
        if not remaining:
            order.is_unclear = False
        patched_ids.append(order.id)

    if patched_ids:
        db.commit()
        print(f"✅ Alias patch committed for orders: {patched_ids}")

    return len(patched_ids)


def _retroactive_patch_customer(raw: str, canonical: str, phone: str, db: Session) -> int:
    """
    Move unclear items matching `raw` into parsed_items for ONE customer's orders.
    Uses ORM — assigns native Python lists directly to JSONB columns.
    Skips __qty_ambiguous__ and __word_qty__ sentinels.
    """
    orders = db.query(Order).filter(
        Order.customer_phone == phone,
        Order.unclear_items.isnot(None),
    ).all()

    unit = _get_unit_for_canonical(canonical)
    patched_ids = []

    for order in orders:
        unclear = _safe_list(order.unclear_items)
        parsed  = list(_safe_list(order.parsed_items))
        if not unclear:
            continue

        remaining     = []
        matched_lines = []
        for line in unclear:
            # Never retroactively patch sentinels
            if line.startswith("__qty_ambiguous__") or line.startswith("__word_qty__"):
                remaining.append(line)
                continue
            product_part = _extract_product_name_from_line(line)
            if (alias_keys_match(line, raw)
                    or product_part == raw or product_part.startswith(raw)
                    or raw in product_part):
                matched_lines.append(line)
            else:
                remaining.append(line)

        if not matched_lines:
            continue

        for line in matched_lines:
            qty = _extract_qty_from_line(line)
            parsed.append({"product": canonical, "quantity": qty, "unit": unit})

        # ✅ Assign native lists — no json.dumps needed for JSONB
        order.parsed_items  = parsed
        order.unclear_items = remaining if remaining else []
        if not remaining:
            order.is_unclear = False
        patched_ids.append(order.id)

    if patched_ids:
        db.commit()
        print(f"✅ Customer alias patch committed for orders: {patched_ids}")

    return len(patched_ids)


def _patch_order_unclear(order: Order, raw: str, canonical: str, db: Session) -> int:
    """
    Single-order patch used by _retroactive_patch_customer.
    NO db.commit() here — caller commits after all orders are patched.
    """
    unclear = _safe_list(order.unclear_items)
    if not unclear:
        return 0

    parsed  = list(_safe_list(order.parsed_items))
    unit    = _get_unit_for_canonical(canonical)

    remaining     = []
    matched_lines = []
    for line in unclear:
        product_part = _extract_product_name_from_line(line)
        if (alias_keys_match(line, raw)
                or product_part == raw or product_part.startswith(raw)
                or raw in product_part):
            matched_lines.append(line)
        else:
            remaining.append(line)

    if not matched_lines:
        return 0

    for line in matched_lines:
        qty = _extract_qty_from_line(line)
        parsed.append({"product": canonical, "quantity": qty, "unit": unit})

    # ✅ Assign native lists — no json.dumps needed for JSONB
    order.parsed_items  = parsed
    order.unclear_items = remaining if remaining else []
    if not remaining:
        order.is_unclear = False

    # ✅ No db.commit() here — caller commits
    return 1


def _retroactive_patch_word_qty(
    product:  str,
    unit:     str,
    phone:    str,
    db:       Session,
) -> int:
    """
    Spec §6.3 — resolve past __word_qty__ rows for THIS customer only.

    Finds every order for `phone` whose unclear_items contains a sentinel
    starting with __word_qty__{product}:: and resolves each using its OWN
    parsed quantity — never the current order's quantity.

    The current order has already been patched and committed before this
    runs, so it won't match (its sentinel has been removed).
    """
    prefix = f"__word_qty__{product}::"

    orders = db.query(Order).filter(
        Order.customer_phone == phone,
        Order.unclear_items.isnot(None),
        Order.is_cancelled == False,
    ).all()

    patched_ids = []

    for order in orders:
        unclear = _safe_list(order.unclear_items)
        if not unclear:
            continue

        parsed    = list(_safe_list(order.parsed_items))
        remaining = []
        matched   = False

        for entry in unclear:
            if not entry.startswith(prefix):
                remaining.append(entry)
                continue

            # Extract this row's own qty from the sentinel.
            # Format: __word_qty__Wings::1.5::kg::lollipop  [डेढ़ → ...]
            # After stripping prefix and hint: Wings::1.5::kg::lollipop
            # Split on ::                    ["Wings", "1.5", "kg", "lollipop"]
            try:
                body    = entry[len("__word_qty__"):]    # "Wings::1.5::kg::lollipop  [...]"
                body    = body.split("  [")[0].strip()   # "Wings::1.5::kg::lollipop"
                parts   = body.split("::")               # ["Wings", "1.5", "kg", "lollipop"]
                row_qty = float(parts[1]) if len(parts) > 1 else 1.0
            except (IndexError, ValueError):
                # Malformed sentinel — leave it, don't lose it
                remaining.append(entry)
                logger.warning(
                    f"word_qty retro: malformed sentinel in order {order.id}: {entry!r}"
                )
                continue

            parsed.append({
                "product":        product,
                "quantity":       row_qty,
                "unit":           unit,
                "explicit_unit":  True,
                "_resolved_from": "word_qty_retro",
            })
            matched = True
            # Don't append to remaining — this entry is resolved

        if not matched:
            continue

        order.parsed_items  = parsed
        order.unclear_items = remaining if remaining else []
        if not remaining:
            order.is_unclear = False

        patched_ids.append(order.id)

    if patched_ids:
        db.commit()
        logger.info(f"word_qty retro patch committed for orders: {patched_ids}")

    return len(patched_ids)


def _lookup_alias(raw_text: str, db: Session) -> Optional[str]:
    alias_match = db.query(UnclearItemAlias).filter(
        UnclearItemAlias.raw_text == raw_text.strip().lower()
    ).first()
    return alias_match.canonical_product_name if alias_match else None


# ── Noise Phrases ─────────────────────────────────────────────────────────────

@router.get("/noise-phrases")
def get_noise_phrases(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    phrases = db.query(NoisePhrase).order_by(NoisePhrase.raw_text).all()
    return [
        {
            "id":         p.id,
            "raw_text":   p.raw_text,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in phrases
    ]


@router.post("/noise-phrases")
def create_noise_phrase(
    payload: NoisePhraseCreate,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    normalized = payload.raw_text.strip().lower()
    if not normalized:
        raise HTTPException(status_code=400, detail="raw_text cannot be empty")

    already_existed = False
    existing = db.query(NoisePhrase).filter(NoisePhrase.raw_text == normalized).first()
    if existing:
        already_existed = True
        phrase = existing
    else:
        phrase = NoisePhrase(raw_text=normalized)
        db.add(phrase)
        db.commit()
        db.refresh(phrase)

    patched_count = _retroactive_remove_noise(normalized, db)

    return {
        "id": phrase.id,
        "raw_text": phrase.raw_text,
        "already_existed": already_existed,
        "orders_patched": patched_count,
    }


@router.delete("/noise-phrases/{phrase_id}")
def delete_noise_phrase(
    phrase_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    phrase = db.query(NoisePhrase).filter(NoisePhrase.id == phrase_id).first()
    if not phrase:
        raise HTTPException(status_code=404, detail="Noise phrase not found")
    db.delete(phrase)
    db.commit()
    return {"deleted": True, "raw_text": phrase.raw_text}


# ── Salespersons ──────────────────────────────────────────────────────────────

@router.get("/salespersons")
def list_salespersons(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    sps = db.query(Salesperson).order_by(Salesperson.name).all()
    result = []
    for sp in sps:
        count = db.query(Customer).filter(Customer.salesperson_id == sp.id).count()
        result.append({
            "id": sp.id, "name": sp.name, "phone": sp.phone, "area": sp.area,
            "active": sp.active, "customer_count": count,
            "created_at": sp.created_at.isoformat() if sp.created_at else None,
        })
    return {"salespersons": result, "total": len(result)}


@router.post("/salespersons")
def create_salesperson(payload: SalespersonCreate, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    error = validate_phone(payload.phone)
    if error:
        raise HTTPException(status_code=400, detail=error)

    normalized = normalize_phone(payload.phone)
    if db.query(Salesperson).filter(Salesperson.phone == normalized).first():
        raise HTTPException(status_code=400, detail=f"Salesperson with phone {normalized} already exists")

    sp = Salesperson(name=payload.name.strip(), phone=normalized, area=payload.area.strip() if payload.area else None, active=True)
    db.add(sp); db.commit(); db.refresh(sp)

    try:
        send_whatsapp_message(
            sp.phone,
            f"👋 Hi {sp.name},\n\n"
            f"You've been added to the OrdeRR system for *{PLANT_NAME}*.\n\n"
            f"You will receive a daily WhatsApp message at *11:05 PM* "
            f"with a list of customers who haven't placed their order yet.\n\n"
            f"Please follow up with them for order collection.\n\n"
            f"— {PLANT_NAME} Team"
        )
    except Exception as e:
        print(f"⚠️ Welcome message failed (salesperson still created): {e}")

    return {"status": "created", "salesperson": {"id": sp.id, "name": sp.name, "phone": sp.phone, "area": sp.area, "active": sp.active}}


@router.put("/salespersons/{salesperson_id}")
def update_salesperson(salesperson_id: int, payload: SalespersonUpdate, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    sp = db.query(Salesperson).filter(Salesperson.id == salesperson_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Salesperson not found")
    if payload.name   is not None: sp.name   = payload.name.strip()
    if payload.area is not None: sp.area = payload.area.strip()
    if payload.phone  is not None:
        error = validate_phone(payload.phone)
        if error:
            raise HTTPException(status_code=400, detail=error)
        sp.phone = normalize_phone(payload.phone)
    if payload.active is not None: sp.active = payload.active

    db.commit(); db.refresh(sp)
    return {"status": "updated", "salesperson": {"id": sp.id, "name": sp.name, "phone": sp.phone, "area": sp.area, "active": sp.active}}


@router.delete("/salespersons/{salesperson_id}")
def deactivate_salesperson(salesperson_id: int, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    sp = db.query(Salesperson).filter(Salesperson.id == salesperson_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Salesperson not found")
    sp.active = False; db.commit()
    affected = db.query(Customer).filter(Customer.salesperson_id == salesperson_id).count()
    return {"status": "deactivated", "salesperson_id": salesperson_id, "affected_customers": affected}


@router.delete("/salespersons/{salesperson_id}/purge")
def hard_delete_salesperson(salesperson_id: int, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    sp = db.query(Salesperson).filter(Salesperson.id == salesperson_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Salesperson not found")
    db.query(Customer).filter(Customer.salesperson_id == salesperson_id).update({Customer.salesperson_id: None})
    db.delete(sp)
    db.commit()
    return {"status": "purged", "salesperson_id": salesperson_id}


# ── Customers ─────────────────────────────────────────────────────────────────

def _customer_row(c: Customer, db: Session) -> dict:
    sp_name = None
    if c.salesperson_id:
        sp = db.query(Salesperson).filter(Salesperson.id == c.salesperson_id).first()
        sp_name = sp.name if sp else None
    return {
        "id": c.id, "restaurant_name": c.restaurant_name,
        "owner_name": c.owner_name,
        "phone_number": c.phone_number, "area": c.area,
        "address": c.address, "city": c.city,
        "salesperson_id": c.salesperson_id, "salesperson_name": sp_name,
        "is_active": c.is_active,
        "is_daily_order_customer": c.is_daily_order_customer,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "outstanding": float(c.outstanding) if c.outstanding is not None else 0.0,
        "has_phone": bool(c.phone_number),
    }


@router.get("/customers")
def list_customers(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    today_str = get_current_business_date_str()
    customers = (
        db.query(Customer)
        .filter(Customer.onboarding_status == "active")
        .order_by(Customer.restaurant_name)
        .all()
    )
    ordered_today = {
        row[0]
        for row in db.query(Order.customer_phone)
        .filter(Order.business_date == today_str, Order.is_cancelled == False)
        .all()
    }
    result = []
    for c in customers:
        row = _customer_row(c, db)
        row["ordered_today"] = c.phone_number in ordered_today
        result.append(row)
    return {"customers": result, "total": len(result)}


@router.post("/customers/import")
async def import_customers(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """
    Bulk-import customers from a 'Customer Outstanding' .xlsx export.
    Upserts by phone (or name for phone-less rows); refreshes outstanding.
    """
    fname = (file.filename or "").lower()
    if not fname.endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=400,
            detail="Please upload an Excel .xlsx file (the Customer Outstanding export).",
        )
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    try:
        summary = import_customers_from_xlsx(db, contents)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Customer import failed")
        raise HTTPException(status_code=500, detail=f"Import failed: {e}")
    return {"status": "ok", "summary": summary}


@router.get("/customers/unassigned")
def list_unassigned_customers(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    customers = (
        db.query(Customer)
        .filter(Customer.onboarding_status == "active", Customer.salesperson_id == None)
        .order_by(Customer.created_at.desc())
        .all()
    )
    return {"customers": [_customer_row(c, db) for c in customers], "total": len(customers)}


@router.post("/customers")
def add_customer_manually(
    payload: CustomerCreate,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    error = validate_phone(payload.phone)
    if error:
        raise HTTPException(status_code=400, detail=error)

    try:
        customer = create_customer_manually(
            db=db,
            phone=payload.phone,
            restaurant_name=payload.restaurant_name,
            area=payload.area,
            salesperson_id=payload.salesperson_id,
        )
        try:
            if MANAGER_PHONE:
                sp_name = ""
                if customer.salesperson_id:
                    sp = db.query(Salesperson).filter(Salesperson.id == customer.salesperson_id).first()
                    sp_name = f"\n🧑 Salesperson: {sp.name}" if sp else ""
                send_whatsapp_message(MANAGER_PHONE, (
                    f"🆕 *New customer registered*\n"
                    f"🏪 {customer.restaurant_name}\n"
                    f"📱 {customer.phone_number}\n"
                    f"📍 {customer.area or 'Area not set'}{sp_name}"
                ))
        except Exception as e:
            logger.warning(f"Manager notification failed for new customer {customer.phone_number}: {e}")

        # Welcome template removed 2026-07-14 (owner decision — 3-template setup).

        return {"status": "created", "customer": _customer_row(customer, db)}
    except (ValueError, IntegrityError):
        raise HTTPException(status_code=409, detail="A customer with this phone number already exists.")    


@router.get("/customers/{customer_id}/orders")
def get_customer_orders(customer_id: int, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    orders = (
        db.query(Order)
        .filter(Order.customer_phone == customer.phone_number)
        .order_by(Order.created_at.desc())
        .limit(60)
        .all()
    )
    result = []
    for o in orders:
        result.append({
            "id"            : o.id,
            "delivery_date" : o.delivery_date,
            "delivery_time" : o.delivery_time,
            "status"        : o.status,
            "is_cancelled"  : o.is_cancelled,
            "is_unclear"    : o.is_unclear,
            "unclear_reason": o.unclear_reason,
            "unclear_items" : _safe_list(o.unclear_items),
            "raw_message"   : o.raw_message,
            "items"         : _safe_list(o.parsed_items),
            "created_at"    : o.created_at.isoformat() if o.created_at else None,
        })
    return {
        "customer_id"    : customer_id,
        "restaurant_name": customer.restaurant_name,
        "phone_number"   : customer.phone_number,
        "total_orders"   : len(result),
        "orders"         : result,
    }


@router.put("/customers/bulk/status")
def bulk_update_customer_status(
    payload: CustomerBulkStatus,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """Activate/deactivate multiple customers in one call."""
    ids = list(dict.fromkeys(payload.customer_ids))  # de-dupe, preserve order
    if not ids:
        raise HTTPException(status_code=400, detail="No customers selected")
    customers = db.query(Customer).filter(Customer.id.in_(ids)).all()
    for c in customers:
        c.is_active = payload.is_active
    db.commit()
    return {
        "status": "updated",
        "updated": len(customers),
        "requested": len(ids),
        "is_active": payload.is_active,
    }


@router.post("/customers/bulk/assign")
def bulk_assign_customers(
    payload: CustomerBulkAssign,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """Assign area and/or salesperson to multiple customers in one call."""
    ids = list(dict.fromkeys(payload.customer_ids))  # de-dupe, preserve order
    if not ids:
        raise HTTPException(status_code=400, detail="No customers selected")
    area = payload.area.strip() if payload.area is not None else None
    if not area and payload.salesperson_id is None:
        raise HTTPException(status_code=400, detail="Provide area or salesperson_id")
    if payload.salesperson_id is not None:
        sp = db.query(Salesperson).filter(Salesperson.id == payload.salesperson_id).first()
        if not sp:
            raise HTTPException(status_code=404, detail=f"Salesperson id={payload.salesperson_id} not found")
    customers = db.query(Customer).filter(Customer.id.in_(ids)).all()
    for c in customers:
        if area:
            c.area = area
        if payload.salesperson_id is not None:
            c.salesperson_id = payload.salesperson_id
    db.commit()
    return {
        "status": "assigned",
        "updated": len(customers),
        "requested": len(ids),
    }


@router.post("/customers/{customer_id}/assign")
def assign_customer(customer_id: int, payload: CustomerAssign, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if payload.area is None and payload.salesperson_id is None:
        raise HTTPException(status_code=400, detail="Provide area or salesperson_id")
    if payload.area is not None:
        customer.area = payload.area.strip()
    if payload.salesperson_id is not None:
        sp = db.query(Salesperson).filter(Salesperson.id == payload.salesperson_id).first()
        if not sp:
            raise HTTPException(status_code=404, detail=f"Salesperson id={payload.salesperson_id} not found")
        customer.salesperson_id = payload.salesperson_id
    db.commit(); db.refresh(customer)
    sp_name = None
    if customer.salesperson_id:
        sp = db.query(Salesperson).filter(Salesperson.id == customer.salesperson_id).first()
        sp_name = sp.name if sp else None
    return {"status": "assigned", "customer": {
        "id": customer.id, "restaurant_name": customer.restaurant_name,
        "area": customer.area, "salesperson_id": customer.salesperson_id,
        "salesperson_name": sp_name,
    }}


@router.put("/customers/{customer_id}/status")
def update_customer_status(customer_id: int, payload: CustomerStatus, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer.is_active = payload.is_active
    db.commit(); db.refresh(customer)
    return {"status": "updated", "customer": {
        "id": customer.id, "restaurant_name": customer.restaurant_name,
        "phone_number": customer.phone_number, "is_active": customer.is_active,
    }}


@router.put("/customers/{customer_id}")
def edit_customer(
    customer_id: int,
    payload: CustomerEdit,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """
    Full edit of a customer's profile fields (Edit modal on the dashboard).
    Only fields present in the payload (exclude_unset) are touched.
    """
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    data = payload.dict(exclude_unset=True)

    if "restaurant_name" in data:
        name = (data["restaurant_name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Restaurant name cannot be empty")
        customer.restaurant_name = name

    if "owner_name" in data:
        customer.owner_name = (data["owner_name"] or "").strip() or None

    if "phone_number" in data:
        new_phone = (data["phone_number"] or "").strip()
        if not new_phone:
            raise HTTPException(status_code=400, detail="Phone number cannot be empty")
        error = validate_phone(new_phone)
        if error:
            raise HTTPException(status_code=400, detail=error)
        normalized = normalize_phone(new_phone)
        if normalized != customer.phone_number:
            existing = db.query(Customer).filter(
                Customer.phone_number == normalized,
                Customer.id != customer_id,
            ).first()
            if existing:
                raise HTTPException(status_code=400, detail="Another customer already uses this phone number")
            customer.phone_number = normalized

    if "address" in data:
        customer.address = (data["address"] or "").strip() or None

    if "city" in data:
        customer.city = (data["city"] or "").strip() or None

    if "area" in data:
        customer.area = (data["area"] or "").strip() or None

    if "salesperson_id" in data:
        sp_id = data["salesperson_id"]
        if sp_id is not None:
            sp = db.query(Salesperson).filter(Salesperson.id == sp_id).first()
            if not sp:
                raise HTTPException(status_code=404, detail=f"Salesperson id={sp_id} not found")
        customer.salesperson_id = sp_id

    if "is_daily_order_customer" in data:
        customer.is_daily_order_customer = data["is_daily_order_customer"]

    db.commit()
    db.refresh(customer)

    return {"status": "updated", "customer": _customer_row(customer, db)}


@router.post("/customers/{customer_id}/post-order")
def post_order_on_behalf(
    customer_id: int,
    payload: PostOrderPayload,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not customer.is_active:
        raise HTTPException(status_code=400, detail="Customer is inactive")
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Order message cannot be empty")

    # An order is keyed to the customer's mobile number, so we can't post one for
    # a customer who has none saved (e.g. customers created from the Vasy
    # outstanding/sales import, which carry no phone). Fail with a plain-language
    # message instead of letting the order pipeline crash with a technical error.
    if not (customer.phone_number or "").strip():
        name = customer.restaurant_name or "This customer"
        raise HTTPException(
            status_code=400,
            detail=(f"{name} doesn’t have a mobile number saved, so an order can’t be "
                    "posted for them yet. Please add their mobile number first — open the "
                    "customer, click Edit, enter their WhatsApp mobile number, save, then "
                    "post the order again."),
        )

    # Credit guardrail — warn (once) before posting for an over-limit / long-overdue
    # customer. The manager can acknowledge and proceed (acknowledge_credit=true).
    # Open critical notes on the customer ride the same gate (Registers & Reminders P2).
    if not payload.acknowledge_credit:
        from orderr_core.services import analytics_service
        should_warn, warn_msg = analytics_service.credit_gate(
            db, customer, get_current_business_date())
        note_lines = []
        try:
            from orderr_core.services import reminders_service
            notes = reminders_service.open_notes_for_customer(
                db, customer.id, get_current_business_date())
            note_lines = [f"📝 {n['note']}" + (f" ({n['amount_fmt']})" if n['amount_fmt'] else "")
                          for n in notes[:3]]
        except Exception:
            pass
        if should_warn or note_lines:
            parts = []
            if should_warn:
                parts.append(warn_msg)
            if note_lines:
                name = customer.restaurant_name or "this customer"
                parts.append(f"Open critical note(s) on {name}:\n" + "\n".join(note_lines)
                             + "\nPost the order anyway?")
            raise HTTPException(status_code=409,
                                detail={"code": "credit_warning",
                                        "message": "\n\n".join(parts)})

    # NOTE: a customer with an active order today keeps it — this call never
    # cancels/overrides an existing order. It's saved as an additional order
    # instead (force_additional=True), same as the WhatsApp pipeline does past
    # the dispatch cutoff. We can't use the pre-cutoff replace-confirmation
    # flow here since it waits on a customer WhatsApp reply and this action
    # is synchronous.
    result = process_incoming_order(
        db=db,
        customer_phone=customer.phone_number,
        message=payload.message.strip(),
        force_additional=True,
    )
    status = result.get("status", "")
    if status in ("order_saved", "order_updated", "repeat_confirmed", "unclear", "received"):
        return {
            "ok": True,
            "status": status,
            "order_id": result.get("order_id"),
            "customer": customer.restaurant_name or customer.phone_number,
            "message": result.get("message", "Order posted successfully"),
        }
    raise HTTPException(
        status_code=400,
        detail=result.get("message") or f"Pipeline returned unexpected status: {status}",
    )


@router.post("/orders/{order_id}/cancel")
def cancel_order_on_behalf(
    order_id: int,
    payload: CancelOrderPayload,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.is_cancelled:
        raise HTTPException(status_code=400, detail="Order is already cancelled")

    order.is_cancelled = True
    order.cancelled_at = datetime.now(IST)
    order.status       = "cancelled"
    db.commit()

    reason_text = payload.reason or "We are unable to fulfil this order today."
    try:
        send_whatsapp_message(
            order.customer_phone,
            f"⚠️ *Order Update — {PLANT_NAME}*\n\n"
            f"Your order for today has been cancelled by our team.\n\n"
            f"📋 Reason: {reason_text}\n\n"
            f"Sorry for the inconvenience. Please contact us if you have questions.\n\n"
            f"— {PLANT_NAME} Team"
        )
    except Exception as e:
        print(f"⚠️ Customer cancel notification failed: {e}")
    try:
        if MANAGER_PHONE and order.customer_phone != MANAGER_PHONE:
            send_whatsapp_message(
                MANAGER_PHONE,
                f"❌ *Order Cancelled by Admin — {PLANT_NAME}*\n\n"
                f"🏪 {order.customer_name or order.customer_phone}\n"
                f"📱 {order.customer_phone}\n"
                f"📋 Reason: {reason_text}\n"
                f"🆔 Order #{order.id}"
            )
    except Exception as e:
        print(f"⚠️ Manager cancel notification failed: {e}")

    return {
        "ok": True,
        "order_id": order.id,
        "customer": order.customer_name or order.customer_phone,
        "reason": reason_text,
    }


# ── Pending orders ────────────────────────────────────────────────────────────

@router.get("/pending")
def get_pending_now(delivery_date: Optional[str] = None, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    if delivery_date:
        try:
            target_date = date.fromisoformat(delivery_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        target_date = get_delivery_date_for_now()

    grouped = get_pending_customers(db, target_date)
    result  = []
    total   = 0
    for sp_id, customers in grouped.items():
        sp_name = "Unassigned"; sp_phone = None
        if sp_id is not None:
            sp = db.query(Salesperson).filter(Salesperson.id == sp_id).first()
            sp_name = sp.name if sp else f"Unknown (id={sp_id})"
            sp_phone = sp.phone if sp else None
        result.append({
            "salesperson_id": sp_id, "salesperson_name": sp_name,
            "salesperson_phone": sp_phone, "pending_count": len(customers),
            "customers": [{"id": c.id, "restaurant_name": c.restaurant_name, "phone_number": c.phone_number, "area": c.area} for c in customers],
        })
        total += len(customers)
    return {"delivery_date": target_date.isoformat(), "total_pending": total, "groups": result}


@router.put("/orders/{order_id}/next-day")
def set_next_day_override(
    order_id: int,
    payload: NextDayOverride,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    now_ist = datetime.now(IST)
    if now_ist.hour >= RESET_HOUR:
        raise HTTPException(status_code=403, detail="Override window closed. Orders are locked after 9 PM IST.")

    if order.created_at.tzinfo is None:
        # Some DB backends (e.g. SQLite) can strip tzinfo on read even
        # though the column is timezone-aware. The wall-clock value as
        # stored is already in IST (that's what every writer of this
        # column uses), so attach the tz directly instead of calling
        # .astimezone(), which would incorrectly assume the naive value
        # is in the server's local timezone and shift it again.
        order_ist = order.created_at.replace(tzinfo=IST)
    else:
        order_ist = order.created_at.astimezone(IST)
    cutoff = order_ist.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if order_ist >= cutoff:
        raise HTTPException(status_code=400, detail="This order was placed after 9 PM IST and is already assigned to the next day.")

    if payload.is_next_day:
        order.business_date = (order_ist.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        order.is_next_day_override = True
    else:
        order.business_date = order_ist.date().strftime("%Y-%m-%d")
        order.is_next_day_override = False

    db.commit(); db.refresh(order)
    return {
    "success": True,
    "order": {
        "id": order.id,
        "is_next_day_override": order.is_next_day_override,
        "business_date": order.business_date,
    }
        }


# ── WhatsApp Window Status ────────────────────────────────────────────────────

def _window_status(last_inbound: datetime | None, now: datetime) -> dict:
    if last_inbound is None:
        return {"status": "CLOSED", "status_label": "Never messaged", "hours_remaining": 0,
                "minutes_remaining": 0, "last_seen_ist": None, "last_seen_display": "Never"}

    if last_inbound.tzinfo is None:
        last_inbound = last_inbound.replace(tzinfo=timezone.utc)

    window_expires = last_inbound + timedelta(hours=24)
    remaining = window_expires - now

    if remaining.total_seconds() <= 0:
        return {"status": "CLOSED", "status_label": "Closed", "hours_remaining": 0,
                "minutes_remaining": 0,
                "last_seen_ist": last_inbound.astimezone(IST).isoformat(),
                "last_seen_display": last_inbound.astimezone(IST).strftime("%d %b %H:%M")}

    total_minutes = int(remaining.total_seconds() // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    status = "OPEN" if hours >= 4 else "AT_RISK"
    return {
        "status": status,
        "status_label": f"{hours}h {minutes}m left",
        "hours_remaining": hours,
        "minutes_remaining": minutes,
        "last_seen_ist": last_inbound.astimezone(IST).isoformat(),
        "last_seen_display": last_inbound.astimezone(IST).strftime("%d %b %H:%M"),
    }


@router.get("/window-status")
def get_window_status(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    now = datetime.now(timezone.utc)
    stakeholders = []
    if MANAGER_PHONE:
        stakeholders.append({"name": "Manager", "phone": MANAGER_PHONE, "role": "manager"})
    for sp in db.query(Salesperson).filter(Salesperson.active == True).all():
        stakeholders.append({"name": sp.name, "phone": sp.phone, "role": "salesperson"})
    for c in (
        db.query(Customer)
        .filter(Customer.is_active == True, Customer.onboarding_status == "active")
        .order_by(Customer.restaurant_name)
        .all()
    ):
        stakeholders.append({"name": c.restaurant_name or c.phone_number, "phone": c.phone_number, "role": "customer"})

    all_phones = [s["phone"] for s in stakeholders]
    last_seen_map = {}
    if all_phones:
        rows = (
            db.query(InboundMessage.customer_phone, func.max(InboundMessage.received_at).label("last_seen"))
            .filter(InboundMessage.customer_phone.in_(all_phones))
            .group_by(InboundMessage.customer_phone)
            .all()
        )
        last_seen_map = {row.customer_phone: row.last_seen for row in rows}

    result = []
    closed_count = at_risk_count = 0
    for s in stakeholders:
        window = _window_status(last_seen_map.get(s["phone"]), now)
        result.append({"name": s["name"], "phone": s["phone"], "role": s["role"], **window})
        if window["status"] == "CLOSED":    closed_count += 1
        elif window["status"] == "AT_RISK": at_risk_count += 1

    return {
        "stakeholders" : result,
        "total"        : len(result),
        "closed_count" : closed_count,
        "at_risk_count": at_risk_count,
        "checked_at"   : datetime.now(IST).strftime("%d %b %Y %H:%M IST"),
    }


# ── Unclear Items ─────────────────────────────────────────────────────────────

@router.get("/unclear-items")
def get_unclear_items(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    """Orders that have unresolved unclear items."""
    orders = (
        db.query(Order)
        .filter(
            Order.unclear_items.isnot(None),
            Order.unclear_items != "[]",
            Order.unclear_items != "null",
            Order.is_cancelled == False,
        )
        .order_by(Order.created_at.desc())
        .all()
    )
    result = []
    for o in orders:
        unclear = _safe_list(o.unclear_items)
        if not unclear:
            continue
        result.append({
            "order_id"      : o.id,
            "customer_name" : o.customer_name,
            "customer_phone": o.customer_phone,
            "raw_message"   : o.raw_message,
            "unclear_items" : unclear,
            "parsed_items"  : _safe_list(o.parsed_items),
            "delivery_date" : o.delivery_date,
            "created_at"    : o.created_at.isoformat() if o.created_at else None,
        })
    return result


@router.get("/unclear-items/aliases")
def get_aliases(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    aliases = db.query(UnclearItemAlias).order_by(UnclearItemAlias.raw_text).all()
    return [
        {
            "id"                    : a.id,
            "raw_text"              : a.raw_text,
            "canonical_product_name": a.canonical_product_name,
            "created_at"            : a.created_at.isoformat() if a.created_at else None,
        }
        for a in aliases
    ]


@router.get("/product-names")
def get_product_names(username: str = Depends(require_auth)):
    return sorted([display for display, _, _ in PRODUCT_DEFINITIONS])


@router.post("/unclear-items/resolve")
def resolve_unclear_item(
    payload: ResolveUnclearItem,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    # Canonicalize the alias key the SAME way the parser will on lookup, so a
    # resolved phrase matches the next order regardless of list markers, dash
    # decoration, or a trailing quantity ("1)chicken big ------ 30 kg" → "chicken
    # big"). Without this the alias silently never re-matches.
    raw       = normalize_alias_key(payload.raw_text)
    canonical = payload.canonical_product_name.strip()

    if not raw or not canonical:
        raise HTTPException(status_code=400, detail="raw_text and canonical_product_name are required")

    valid_names = {display for display, _, _ in PRODUCT_DEFINITIONS}
    if canonical not in valid_names:
        raise HTTPException(status_code=400, detail=f"'{canonical}' is not a valid product name")

    if payload.scope == "customer" and not payload.customer_phone:
        raise HTTPException(status_code=400, detail="customer_phone is required when scope='customer'")

    # ── GLOBAL scope ──────────────────────────────────────────────────────────
    if payload.scope == "global" or not payload.customer_phone:
        existing = db.query(UnclearItemAlias).filter(UnclearItemAlias.raw_text == raw).first()
        if existing:
            existing.canonical_product_name = canonical
            existing.updated_at = datetime.now(IST)
        else:
            db.add(UnclearItemAlias(raw_text=raw, canonical_product_name=canonical))
        db.commit()

        patched = _retroactive_patch_global(raw, canonical, db)
        return {"status": "ok", "scope": "global", "orders_patched": patched}

    # ── CUSTOMER scope ────────────────────────────────────────────────────────
    phone = payload.customer_phone.strip()
    existing = db.query(CustomerProductAlias).filter(
        CustomerProductAlias.customer_phone == phone,
        CustomerProductAlias.raw_text == raw,
    ).first()
    if existing:
        existing.canonical_product_name = canonical
    else:
        db.add(CustomerProductAlias(customer_phone=phone, raw_text=raw, canonical_product_name=canonical))
    db.commit()

    patched = _retroactive_patch_customer(raw, canonical, phone, db)
    return {"status": "ok", "scope": "customer", "orders_patched": patched}


@router.post("/unclear-items/resolve-qty")
def resolve_qty_ambiguity(
    payload: ResolveQtyAmbiguity,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """
    Manager resolves a quantity-ambiguous item (FRD §3.4, §5.3).

    Steps:
    1. Patch the order's parsed_items — replace UNIT_AMBIGUOUS_MARKER with
       the confirmed unit.
    2. Remove the matching __qty_ambiguous__ entry from unclear_items.
    3. If unclear_items is now empty (no product-unclear items remain either),
       clear is_unclear on the order.
    4. Call record_confirmed_qty() so the learning loop fires.
    5. Commit.
    """
    from orderr_core.services.unit_inference import record_confirmed_qty, UNIT_AMBIGUOUS_MARKER

    order = db.query(Order).filter(Order.id == payload.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    confirmed_unit = payload.confirmed_unit.lower().strip()
    if confirmed_unit not in ("kg", "g"):
        raise HTTPException(status_code=400, detail="confirmed_unit must be 'kg' or 'g'")

    # ── Patch parsed_items ────────────────────────────────────────────────────
    parsed = list(_safe_list(order.parsed_items))
    patched = False
    for item in parsed:
        if (
            item.get("product") == payload.product
            and item.get("unit") == UNIT_AMBIGUOUS_MARKER
            and abs(item.get("quantity", -1) - payload.quantity) < 0.001
        ):
            item["unit"] = confirmed_unit
            patched = True
            break   # only patch the first matching ambiguous item

    if not patched:
        # No matching UNIT_AMBIGUOUS_MARKER entry in parsed_items (e.g. the
        # order only carries the __qty_ambiguous__ sentinel with an empty/
        # already-cleared parsed_items list) — add the resolved item directly
        # rather than 404ing, since the manager has just confirmed it.
        parsed.append({
            "product":  payload.product,
            "quantity": payload.quantity,
            "unit":     confirmed_unit,
        })

    order.parsed_items = parsed

    # ── Remove matching __qty_ambiguous__ entry from unclear_items ────────────
    # Compare quantities numerically, not via string equality — payload.quantity
    # is a float (e.g. 10.0) but sentinels are stored with whatever formatting
    # the parser used (e.g. "10"), so "10.0" != "10" as strings.
    def _matches_qty_ambiguous_sentinel(entry: str) -> bool:
        prefix = "__qty_ambiguous__"
        if not entry.startswith(prefix) or "::" not in entry:
            return False
        product_part, _, qty_part = entry[len(prefix):].rpartition("::")
        if product_part != payload.product:
            return False
        try:
            return abs(float(qty_part) - payload.quantity) < 0.001
        except ValueError:
            return False

    unclear = _safe_list(order.unclear_items)
    unclear = [u for u in unclear if not _matches_qty_ambiguous_sentinel(u)]
    order.unclear_items = unclear

    # Clear is_unclear only when no unresolved items of any kind remain
    remaining_ambiguous = [u for u in unclear if u.startswith("__qty_ambiguous__")]
    still_ambiguous_items = [i for i in parsed if i.get("unit") == UNIT_AMBIGUOUS_MARKER]
    remaining_product_unclear = [u for u in unclear if not u.startswith("__qty_ambiguous__")]
    if not remaining_ambiguous and not still_ambiguous_items and not remaining_product_unclear:
        order.is_unclear = False

    db.flush()

    # ── Learning loop — update stats with manager-confirmed value ─────────────
    qty_kg = payload.quantity / 1000.0 if confirmed_unit == "g" else payload.quantity

    record_confirmed_qty(
        product=payload.product,
        customer_phone=payload.customer_phone,
        qty_kg=qty_kg,
        db=db,
    )

    db.commit()

    return {
        "status":    "ok",
        "order_id":  order.id,
        "product":   payload.product,
        "confirmed": f"{payload.quantity} {confirmed_unit}",
    }


@router.post("/unclear-items/resolve-word-qty")
def resolve_word_qty_item(
    payload: ResolveWordQtyItem,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """
    Spec §6: resolve a __word_qty__ unclear item.

    Atomically:
      (a) Add a real line item to this order's parsed_items[] with the
          manager-confirmed product, quantity, and unit.
      (b) Remove the matching __word_qty__ sentinel from unclear_items[].
          Clears is_unclear if no unresolved items remain.
      (c) Write a product-only CustomerProductAlias keyed to the raw
          product phrase from the sentinel's parts[3] (Option B — quantity
          is never baked in; word_quantity.py re-derives it on every future
          message). Falls back to canonical name if parts[3] absent.
      (d) Retroactively resolve past orders for THIS customer that have
          a matching __word_qty__{product}:: sentinel, each using its own
          parsed quantity (spec §6.3).
    """
    # ── Validate inputs ───────────────────────────────────────────────────────
    valid_names = {display for display, _, _ in PRODUCT_DEFINITIONS}
    if payload.product not in valid_names:
        raise HTTPException(
            status_code=400,
            detail=f"'{payload.product}' is not a valid product name",
        )
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")

    confirmed_unit = payload.unit.lower().strip()
    if confirmed_unit not in ("kg", "nos"):
        raise HTTPException(status_code=400, detail="unit must be 'kg' or 'nos'")

    phone = payload.customer_phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="customer_phone is required")

    # ── Load order ────────────────────────────────────────────────────────────
    order = db.query(Order).filter(Order.id == payload.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # ── (a) Add confirmed line item to parsed_items ───────────────────────────
    parsed = list(_safe_list(order.parsed_items))
    parsed.append({
        "product":        payload.product,
        "quantity":       payload.quantity,
        "unit":           confirmed_unit,
        "explicit_unit":  True,
        "_resolved_from": "word_qty_manual",
    })
    order.parsed_items = parsed

    # ── (b) Remove matching sentinel from unclear_items ───────────────────────
    # Match on prefix __word_qty__{product}:: for the known-product case.
    # Also match __word_qty__UNKNOWN:: for rows where the parser couldn't
    # identify the product — manager resolved it via the dropdown.
    # Remove only the FIRST match (an order could have two word-qty rows
    # for the same product at different quantities).
    prefix      = f"__word_qty__{payload.product}::"
    unclear     = _safe_list(order.unclear_items)
    new_unclear = []
    removed     = False

    for entry in unclear:
        if not removed and (
            entry.startswith(prefix)
            or entry.startswith("__word_qty__UNKNOWN::")
        ):
            removed = True
            continue          # drop this sentinel
        new_unclear.append(entry)

    if not removed:
        # Sentinel not found — log and continue rather than 404.
        # Can happen on double-click or if the order was already patched
        # by the retroactive pass of a prior resolution.
        logger.warning(
            f"resolve-word-qty: no sentinel found for product={payload.product!r} "
            f"in order {order.id}. unclear_items was: {unclear!r}"
        )

    order.unclear_items = new_unclear if new_unclear else None
    if not new_unclear:
        order.is_unclear = False

    db.flush()  # write parsed_items + unclear_items before alias write

    # ── (c) Write product-only alias (Option B) ───────────────────────────────
    # Extract the raw product phrase from the sentinel's parts[3].
    # Sentinel format (new):  __word_qty__Wings::1.5::kg::lollipop  [डेढ़ → ...]
    # Sentinel format (old):  __word_qty__Wings::1.5::kg  [डेढ़ → ...]
    # parts[3] = "lollipop" → the customer's original product text.
    # Fall back to canonical name for old-format sentinels or empty phrases.
    alias_raw = payload.product.lower()   # safe default

    original_sentinel = next(
        (e for e in unclear if (
            e.startswith(prefix) or e.startswith("__word_qty__UNKNOWN::")
        )),
        None,
    )
    if original_sentinel:
        try:
            body  = original_sentinel[len("__word_qty__"):]  # strip "__word_qty__"
            body  = body.split("  [")[0].strip()             # strip hint "[डेढ़ → ...]"
            parts = body.split("::")                         # ["Wings","1.5","kg","lollipop"]
            if len(parts) > 3 and parts[3].strip():
                alias_raw = parts[3].strip().lower()
        except Exception:
            pass  # fall back to canonical name default

    existing_alias = db.query(CustomerProductAlias).filter(
        CustomerProductAlias.customer_phone == phone,
        CustomerProductAlias.raw_text       == alias_raw,
    ).first()
    if existing_alias:
        existing_alias.canonical_product_name = payload.product
    else:
        db.add(CustomerProductAlias(
            customer_phone         = phone,
            raw_text               = alias_raw,
            canonical_product_name = payload.product,
        ))

    db.commit()   # commit (a), (b), (c) atomically

    # ── (d) Retroactive patch — this customer's past orders only ──────────────
    # Runs after commit so the current order's sentinel is already gone
    # and won't be matched again by the retro pass.
    retro_patched = _retroactive_patch_word_qty(
        product = payload.product,
        unit    = confirmed_unit,
        phone   = phone,
        db      = db,
    )

    return {
        "status":        "ok",
        "order_id":      order.id,
        "product":       payload.product,
        "quantity":      payload.quantity,
        "unit":          confirmed_unit,
        "alias_written": alias_raw,
        "retro_patched": retro_patched,
    }


@router.delete("/unclear-items/aliases/{alias_id}")
def delete_alias(alias_id: int, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    alias = db.query(UnclearItemAlias).filter(UnclearItemAlias.id == alias_id).first()
    if not alias:
        raise HTTPException(status_code=404, detail="Alias not found")
    db.delete(alias)
    db.commit()
    return {"status": "deleted", "id": alias_id}


# ── Customer-scoped aliases ───────────────────────────────────────────────────

@router.get("/customer-aliases")
def list_customer_aliases(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    aliases = (
        db.query(CustomerProductAlias)
        .order_by(CustomerProductAlias.customer_phone, CustomerProductAlias.raw_text)
        .all()
    )
    # Resolve each alias's phone → restaurant name in one query.
    phones = {a.customer_phone for a in aliases}
    name_map = {
        c.phone_number: c.restaurant_name
        for c in db.query(Customer).filter(Customer.phone_number.in_(phones)).all()
    } if phones else {}
    return [
        {
            "id":                     a.id,
            "customer_phone":         a.customer_phone,
            "customer_name":          name_map.get(a.customer_phone) or None,
            "raw_text":               a.raw_text,
            "canonical_product_name": a.canonical_product_name,
            "created_at":             a.created_at.isoformat() if a.created_at else None,
        }
        for a in aliases
    ]


@router.get("/customer-aliases/{phone}")
def list_customer_aliases_by_phone(phone: str, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    aliases = (
        db.query(CustomerProductAlias)
        .filter(CustomerProductAlias.customer_phone == phone)
        .order_by(CustomerProductAlias.raw_text)
        .all()
    )
    return [
        {
            "id":                     a.id,
            "customer_phone":         a.customer_phone,
            "raw_text":               a.raw_text,
            "canonical_product_name": a.canonical_product_name,
            "created_at":             a.created_at.isoformat() if a.created_at else None,
        }
        for a in aliases
    ]


@router.post("/customer-aliases")
def create_customer_alias(payload: CustomerAliasCreate, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    raw = payload.raw_text.strip().lower()
    existing = db.query(CustomerProductAlias).filter(
        CustomerProductAlias.customer_phone == payload.customer_phone,
        CustomerProductAlias.raw_text == raw,
    ).first()
    if existing:
        existing.canonical_product_name = payload.canonical_product_name.strip()
        db.commit()
        return {"status": "updated", "id": existing.id}
    alias = CustomerProductAlias(
        customer_phone=payload.customer_phone,
        raw_text=raw,
        canonical_product_name=payload.canonical_product_name.strip(),
    )
    db.add(alias); db.commit(); db.refresh(alias)
    return {"status": "created", "id": alias.id}


@router.delete("/customer-aliases/{alias_id}")
def delete_customer_alias(alias_id: int, db: Session = Depends(get_db), username: str = Depends(require_auth)):
    alias = db.query(CustomerProductAlias).filter(CustomerProductAlias.id == alias_id).first()
    if not alias:
        raise HTTPException(status_code=404, detail="Alias not found")
    db.delete(alias)
    db.commit()
    return {"status": "deleted", "id": alias_id}


# ── Test notifications ────────────────────────────────────────────────────────

from orderr_core.services.pending_notifier import notify_salespersons_pending
from orderr_core.services.reporter import send_daily_report

# Removed 2026-07-14 (owner decision — reports live on the status page now):
#   /test-notifications/customer-reminders  → manual 📣 Broadcast screen
#   /test-notifications/management-summary  → template dropped

@router.post("/test-notifications/salesperson-pending")
def test_salesperson_pending(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    notify_salespersons_pending(db)
    return {"status": "sent"}

@router.post("/test-notifications/daily-report")
def test_daily_report(db: Session = Depends(get_db), username: str = Depends(require_auth)):
    send_daily_report(db)
    return {"status": "sent"}

@router.get("/download-production-report")
def download_production_report(
    view_date: Optional[str] = None,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    # Honor the dashboard's selected date; fall back to today on missing/bad input.
    target_date = None
    if view_date:
        try:
            target_date = date.fromisoformat(view_date)
        except ValueError:
            target_date = None

    data  = generate_daily_report(db, target_date=target_date)
    notes = get_todays_customer_notes(db, target_date=target_date)
    html  = _build_print_html(data, notes)
    date_slug = data["date_str"].replace(" ", "_")
    filename  = f"production_report_{date_slug}.html"
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
