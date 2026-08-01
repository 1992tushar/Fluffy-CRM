import json
from orderr_core.utils import fmt_qty
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone, timedelta

from sqlalchemy.orm import Session

from orderr_core.models.order import Order
from orderr_core.models.customer import Customer
from orderr_core.models.salesperson import Salesperson
from orderr_core.services.notifier import (
    send_order_confirmation,
    send_manager_alert,
    send_whatsapp_message,
    send_replace_confirmation_request,
    send_repeat_order_confirmation_request,
)
from orderr_core.services.template_parser import (
    parse_template_order,
    erp_display_name,
    _split_on_connectors,
    _split_multi_item_line,
)
from orderr_core.services.customer_service import get_customer_by_phone, create_new_customer
from orderr_core.services.adhoc_reporter import is_report_keyword, handle_adhoc_report_request
from orderr_core.services.intent_classifier import (
    Intent,
    classify_intent,
    CANCEL_KEYWORDS,
    REPEAT_KEYWORDS,
    HISTORY_KEYWORDS,
    CONFIRM_YES_WORDS,
    CONFIRM_NO_WORDS,
    GREETINGS,
    FILLER_PHRASES,
)

logger = logging.getLogger(__name__)

from orderr_core.config import MANAGER_PHONE, PLANT_NAME
BASE_URL             = os.getenv("BASE_URL", "")   # e.g. https://orderr.onrender.com
from orderr_core.constants import IST
DISPATCH_CUTOFF_HOUR = int(os.getenv("DISPATCH_CUTOFF_HOUR", "9"))


# ── Date helpers ──────────────────────────────────────────────────────────────
# Canonical implementations live in orderr_core.dates; re-exported here so the
# many `from order_service import get_current_business_date[_str]` / RESET_HOUR
# importers keep working unchanged.
from orderr_core.constants import RESET_HOUR
from orderr_core.dates import (
    get_today_ist,
    compute_business_date,
    get_current_business_date,
    get_current_business_date_str,
    get_delivery_date_str,
)


# ── JSON-safety helper ────────────────────────────────────────────────────────

from orderr_core.utils import safe_list as _safe_load_list


# ── DB helpers ────────────────────────────────────────────────────────────────

UNASSIGNED_AREA = "Area not set"


def group_orders_by_area(db: Session, orders: list) -> list[dict]:
    """Group a list of orders by the ordering customer's assigned area.

    Area lives on the Customer (free text, nullable); orders join by phone.
    Returns a list of {"area": str, "orders": [...]} sorted by descending
    order count, with the UNASSIGNED_AREA bucket always last. Ties broken
    alphabetically. Order objects are returned unchanged (same references).
    """
    phones = {o.customer_phone for o in orders if o.customer_phone}
    phone_to_area: dict[str, str] = {}
    if phones:
        rows = (
            db.query(Customer.phone_number, Customer.area)
            .filter(Customer.phone_number.in_(phones))
            .all()
        )
        phone_to_area = {p: (a or "").strip() for p, a in rows}

    groups: dict[str, list] = {}
    for o in orders:
        area = phone_to_area.get(o.customer_phone, "") or UNASSIGNED_AREA
        groups.setdefault(area, []).append(o)

    def _sort_key(item):
        area, order_list = item
        return (area == UNASSIGNED_AREA, -len(order_list), area.lower())

    return [
        {"area": area, "orders": order_list}
        for area, order_list in sorted(groups.items(), key=_sort_key)
    ]


def get_internal_phones(db: Session) -> set:
    salesperson_phones = {
        sp.phone for sp in
        db.query(Salesperson).filter(Salesperson.active == True).all()
    }
    internal = salesperson_phones
    if MANAGER_PHONE:
        internal = internal | {MANAGER_PHONE}
    return internal


def get_todays_active_order(db: Session, customer_phone: str) -> Order | None:
    business_date_str = get_current_business_date_str()
    return (
        db.query(Order)
        .filter(
            Order.customer_phone == customer_phone,
            Order.business_date  == business_date_str,
            Order.is_cancelled   == False,
            Order.status.notin_(["pending_replace", "pending_repeat"]),
        )
        .order_by(Order.created_at.desc())
        .first()
    )


def get_last_order(db: Session, customer_phone: str) -> Order | None:
    return (
        db.query(Order)
        .filter(
            Order.customer_phone == customer_phone,
            Order.is_cancelled   == False,
            Order.is_unclear     == False,
        )
        .order_by(Order.created_at.desc())
        .first()
    )


def _get_pending_repeat(db: Session, customer_phone: str) -> Order | None:
    return (
        db.query(Order)
        .filter(
            Order.customer_phone == customer_phone,
            Order.status         == "pending_repeat",
            Order.delivery_date  == get_delivery_date_str(),
        )
        .order_by(Order.created_at.desc())
        .first()
    )


def _get_pending_replace(db: Session, customer_phone: str) -> Order | None:
    return (
        db.query(Order)
        .filter(
            Order.customer_phone == customer_phone,
            Order.status         == "pending_replace",
            Order.delivery_date  == get_delivery_date_str(),
        )
        .order_by(Order.created_at.desc())
        .first()
    )


# ── Salesperson helper ────────────────────────────────────────────────────────

def _get_assigned_salesperson(db: Session, customer: Customer) -> Salesperson | None:
    """Return the active salesperson assigned to this customer, or None."""
    if not customer.salesperson_id:
        return None
    return (
        db.query(Salesperson)
        .filter(
            Salesperson.id     == customer.salesperson_id,
            Salesperson.active == True,
        )
        .first()
    )


def _notify_salesperson(
    db: Session,
    customer: Customer,
    parsed: dict,
    label: str = "",
) -> None:
    """
    Send the assigned salesperson the same order alert the manager receives.
    Uses the approved template (manager_new_order) so it works even when
    the salesperson's 24hr window is closed.
    Silently swallows all errors so it never affects the customer flow.
    """
    sp = _get_assigned_salesperson(db, customer)
    if not sp or not sp.phone:
        return
    try:
        send_manager_alert(
            manager_phone   = sp.phone,
            customer_phone  = customer.phone_number,
            parsed          = parsed,
            restaurant_name = customer.restaurant_name,
        )
    except Exception as e:
        logger.warning("Failed to notify salesperson %s: %s", sp.phone, e)


# ── Stats write helper (unit inference learning loop) ─────────────────────────

def _update_stats_for_items(customer_phone: str, parsed_items: list, db) -> None:
    """
    Write confirmed quantities to customer_product_stats (FRD §5.3).

    Called ONLY after an order is fully confirmed (saved to DB with a
    resolved unit).  Skips items that are still ambiguous
    (UNIT_AMBIGUOUS_MARKER) — those must be resolved by the manager first
    via /admin/unclear-items/resolve-qty before stats are written.

    Also skips non-kg items (nos, etc.) — only kg quantities feed the
    inference model.

    Swallows all errors so it never interrupts the order flow.
    """
    from orderr_core.services.unit_inference import record_confirmed_qty, UNIT_AMBIGUOUS_MARKER

    for item in parsed_items:
        unit = item.get("unit", "")

        # Never write stats for items pending manager review
        if unit == UNIT_AMBIGUOUS_MARKER:
            continue

        # Only track kg quantities (nos/pieces handled separately; g
        # quantities are stored as kg by the admin resolver)
        if unit.lower() != "kg":
            continue

        product  = item.get("product", "")
        quantity = item.get("quantity", 0)
        if not product or quantity <= 0:
            continue

        record_confirmed_qty(
            product=product,
            customer_phone=customer_phone,
            qty_kg=float(quantity),
            db=db,
        )


# ── Notification helpers ──────────────────────────────────────────────────────

def _build_unclear_alert(
    restaurant_name: str,
    customer_phone: str,
    unclear_items: list,
    parsed_items: list,
) -> str:
    lines = [
        f"⚠️ *Unclear Items in Order — {PLANT_NAME}*\n",
        f"🏪 {restaurant_name}",
        f"📱 {customer_phone}\n",
    ]
    if parsed_items:
        lines.append("*Parsed items (confirmed):*")
        for i in parsed_items:
            qty = fmt_qty(i['quantity'])
            lines.append(f"  ✅ {erp_display_name(i['product'])} — {qty} {i['unit']}")
        lines.append("")
    lines.append("*Could not understand:*")
    for raw in unclear_items:
        lines.append(f"  ❓ {raw}")
    lines.append("\nPlease review on the dashboard and assign correct product names.")
    return "\n".join(lines)


def _has_digits(text: str) -> bool:
    """Return True if the message contains any digit — signals a quantity/order attempt."""
    return bool(re.search(r'\d', text))


def _unparseable_segments(message: str) -> list:
    """Break an order the parser couldn't read AT ALL into the same per-item
    segments it would have tried, keeping only the digit-bearing chunks.

    Lets a fully-unrecognised (but numeric) message still surface as individual
    dropdown-correctable rows in the Unclear tab, instead of a contentless
    'couldn't read anything' order with nothing to fix. Falls back to the whole
    message when no cleaner split is possible."""
    def _clean(seg: str) -> str:
        # Customers use runs of dots as separators ("Chicken..15"); collapse
        # those to a space so the name/qty read cleanly — but leave single dots
        # alone so decimals like "2.5" survive. Trim stray leading/trailing
        # dots and whitespace.
        seg = re.sub(r"\.{2,}", " ", seg)
        seg = re.sub(r"\s+", " ", seg).strip(" .")
        return seg

    segments = []
    for line in message.splitlines():
        for part in _split_on_connectors(line):
            segments.extend(_split_multi_item_line(part))
    segments = [_clean(s) for s in segments]
    segments = [s for s in segments if s and _has_digits(s)]
    # De-dupe while preserving order.
    seen = set()
    unique = [s for s in segments if not (s in seen or seen.add(s))]
    return unique or [_clean(message) or message.strip()]


def _save_and_notify(
    db: Session,
    customer: Customer,
    parsed: dict,
    raw_message: str,
    is_photo: bool = False,
    is_edit: bool = False,
) -> dict:
    customer_phone  = customer.phone_number
    restaurant_name = customer.restaurant_name
    unclear_items   = parsed.get("unclear_items", [])
    parsed_items    = parsed.get("items", [])

    # FIX: pass Python lists directly — SQLAlchemy/JSONB serializes them.
    # Previously json.dumps() was called here, which double-encoded the data
    # on Postgres (storing a JSON string instead of a JSON array). This caused
    # admin.py's resolver to write a clean list back while the original save
    # had written a string, creating an inconsistency that broke the confirmed-
    # order display after word-qty resolution.
    order = Order(
        plant_name     = PLANT_NAME,
        customer_name  = restaurant_name,
        customer_phone = customer_phone,
        raw_message    = raw_message,
        is_photo_order = is_photo,
        parsed_items   = parsed_items,
        unclear_items  = unclear_items or None,
        delivery_date  = get_delivery_date_str(),
        delivery_time  = parsed.get("delivery_time"),
        is_unclear     = parsed.get("is_unclear", False),
        unclear_reason = parsed.get("unclear_reason"),
        status         = "received",
        business_date  = get_current_business_date_str(),
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    if is_edit:
        items_text = "\n".join(
            f"• {erp_display_name(i['product'])} — {fmt_qty(i['quantity'])} {i['unit']}"
            for i in parsed_items
        )
        # Notify customer (free-form — always within their window)
        send_whatsapp_message(
            customer_phone,
            f"✅ *Order Updated — {PLANT_NAME}*\n\n"
            f"Your previous order has been replaced with:\n\n"
            f"{items_text}\n\nThank you!",
        )
        # Notify manager via approved template — works even if window is closed
        try:
            send_manager_alert(
                manager_phone   = MANAGER_PHONE,
                customer_phone  = customer_phone,
                parsed          = parsed,
                restaurant_name = restaurant_name,
            )
        except Exception as e:
            logger.warning("Manager edit alert failed: %s", e)

        # Notify assigned salesperson via approved template
        _notify_salesperson(db, customer, parsed)

        # For edits, confirmation is the update message above — treat as sent
        order.confirmation_sent    = True
        order.forwarded_to_manager = True

    else:
        # Capture actual send result — confirmation_sent reflects reality
        confirmation_sent = send_order_confirmation(
            customer_phone  = customer_phone,
            parsed          = parsed,
            restaurant_name = restaurant_name,
        )

        if not unclear_items:
            # Notify manager via approved template — works even if window is closed
            send_manager_alert(
                manager_phone   = MANAGER_PHONE,
                customer_phone  = customer_phone,
                parsed          = parsed,
                restaurant_name = restaurant_name,
            )
            # Notify assigned salesperson via approved template
            _notify_salesperson(db, customer, parsed)

        if unclear_items:
            unclear_msg = _build_unclear_alert(
                restaurant_name, customer_phone, unclear_items, parsed_items
            )
            # Unclear alerts are free-form — manager/salesperson are internal
            # users who interact daily so their window is almost always open.
            try:
                send_whatsapp_message(MANAGER_PHONE, unclear_msg)
            except Exception as e:
                logger.warning("Unclear items manager alert failed: %s", e)

            sp = _get_assigned_salesperson(db, customer)
            if sp and sp.phone:
                try:
                    send_whatsapp_message(sp.phone, unclear_msg)
                except Exception as e:
                    logger.warning("Unclear items salesperson alert failed: %s", e)

        order.confirmation_sent    = confirmation_sent  # True only if WA API succeeded
        order.forwarded_to_manager = True

    db.commit()

    # ── Update unit-inference stats for confirmed kg items ────────────────────
    # Only write stats for items that have a resolved unit (not UNIT_AMBIGUOUS_MARKER).
    # Items still pending manager review must not poison the training signal.
    # Commit separately so the order save above is isolated from any stats error.
    if parsed_items:
        _update_stats_for_items(customer_phone, parsed_items, db)
        db.commit()  # commit stats update independently

    return {
        "order_id"      : order.id,
        "customer_phone": customer_phone,
        "parsed"        : parsed,
        "status"        : order.status,
        "is_edit"       : is_edit,
        "saved"         : True,
    }


# ── Intent handlers ───────────────────────────────────────────────────────────

def _handle_onboarding(db: Session, customer: Customer, message: str) -> dict:
    customer_phone = customer.phone_number
    error = validate_restaurant_name(message.strip())
    if error:
        send_whatsapp_message(
            customer_phone,
            f"⚠️ {error}\n\n"
            "Please reply with your *restaurant or hotel name* to continue.",
        )
        return {"order_id": None, "status": "invalid_restaurant_name", "parsed": None}

    customer.restaurant_name   = message.strip()
    customer.onboarding_status = "active"
    db.commit()

    send_whatsapp_message(
        customer_phone,
        f"✅ *Welcome, {customer.restaurant_name}!*\n\n"
        f"You're all set. Just send your order anytime — "
        f"list the items and quantities in your own way and we'll take care of it. 🙌",
    )
    try:
        send_whatsapp_message(
            MANAGER_PHONE,
            f"🆕 *New Customer Registered — {PLANT_NAME}*\n\n"
            f"🏪 {customer.restaurant_name}\n"
            f"📱 {customer_phone}\n\n"
            f"Please assign area and salesperson on the dashboard.",
        )
    except Exception as e:
        logger.warning("New customer manager alert failed: %s", e)

    return {"order_id": None, "status": "customer_onboarded", "parsed": None}


def _handle_cancel(db: Session, customer: Customer) -> dict:
    customer_phone = customer.phone_number
    existing = get_todays_active_order(db, customer_phone)
    if not existing:
        send_whatsapp_message(
            customer_phone,
            "ℹ️ No active order found for today to cancel.",
        )
        return {"order_id": None, "status": "no_order_to_cancel", "parsed": None}

    existing.is_cancelled = True
    existing.cancelled_at = datetime.now(IST)
    existing.status       = "cancelled"
    db.commit()

    send_whatsapp_message(
        customer_phone,
        "✅ Your order has been cancelled.\n\nJust send your order anytime to place a new one.",
    )
    # Cancel alerts are free-form — manager/salesperson interact daily
    # so their 24hr window is almost always open for these operational messages.
    try:
        send_whatsapp_message(
            MANAGER_PHONE,
            f"❌ *Order Cancelled — {PLANT_NAME}*\n\n"
            f"🏪 {customer.restaurant_name}\n"
            f"📱 {customer_phone}\n\n"
            f"Their order for today has been cancelled.",
        )
    except Exception:
        pass

    sp = _get_assigned_salesperson(db, customer)
    if sp and sp.phone:
        try:
            send_whatsapp_message(
                sp.phone,
                f"❌ *Order Cancelled — {PLANT_NAME}*\n\n"
                f"🏪 {customer.restaurant_name}\n"
                f"📱 {customer_phone}\n\n"
                f"Their order for today has been cancelled.",
            )
        except Exception as e:
            logger.warning("Cancel salesperson alert failed: %s", e)

    return {"order_id": existing.id, "status": "order_cancelled", "parsed": None}


def _handle_repeat(db: Session, customer: Customer) -> dict:
    customer_phone = customer.phone_number
    last = get_last_order(db, customer_phone)
    if not last or not last.parsed_items:
        send_whatsapp_message(
            customer_phone,
            "ℹ️ No previous order found.\n\nJust send your order anytime to place a new one.",
        )
        return {"order_id": None, "status": "no_last_order", "parsed": None}

    # FIX: use _safe_load_list instead of json.loads — on Postgres/JSONB the
    # column is already a list; json.loads() raises TypeError on a list object.
    items = _safe_load_list(last.parsed_items)
    pending = Order(
        plant_name     = PLANT_NAME,
        customer_name  = customer.restaurant_name,
        customer_phone = customer_phone,
        raw_message    = "repeat",
        parsed_items   = items,   # FIX: pass list directly, not json.dumps(items)
        delivery_date  = get_delivery_date_str(),
        business_date  = get_current_business_date_str(),
        is_unclear     = False,
        status         = "pending_repeat",
    )
    db.add(pending)
    db.commit()

    send_repeat_order_confirmation_request(customer_phone, items)
    return {"order_id": pending.id, "status": "repeat_requested", "parsed": None}


def _handle_history(db: Session, customer: Customer) -> dict:
    """Send customer a WhatsApp link to their personal order history ledger."""
    customer_phone = customer.phone_number

    # Generate token once; reuse on all subsequent requests (same link forever)
    if not customer.ledger_token:
        customer.ledger_token = secrets.token_urlsafe(24)
        db.commit()

    ledger_url = f"{BASE_URL}/ledger/{customer.ledger_token}"

    send_whatsapp_message(
        customer_phone,
        f"📋 *Your Order History — {PLANT_NAME}*\n\n"
        f"Here's your personal order ledger for the last 7 days:\n\n"
        f"🔗 {ledger_url}\n\n"
        f"The link always works for you — feel free to bookmark it.",
    )
    return {"order_id": None, "status": "history_sent", "parsed": None}


def _handle_confirm_yes(db: Session, customer: Customer) -> dict:
    """Customer confirmed a pending repeat or replace."""
    customer_phone    = customer.phone_number
    pending_repeat    = _get_pending_repeat(db, customer_phone)
    pending_replace   = _get_pending_replace(db, customer_phone)

    if pending_repeat:
        # FIX: use _safe_load_list — json.loads() raises TypeError on Postgres
        # when the JSONB column is already deserialized to a Python list.
        items  = _safe_load_list(pending_repeat.parsed_items)
        parsed = {
            "items": items, "unclear_items": [],
            "delivery_date": None, "delivery_time": None,
            "is_unclear": False, "unclear_reason": None,
        }
        pending_repeat.status = "received"
        db.commit()

        confirmation_sent = send_order_confirmation(customer_phone, parsed, customer.restaurant_name)
        # Notify manager via approved template
        send_manager_alert(MANAGER_PHONE, customer_phone, parsed, customer.restaurant_name)
        # Notify assigned salesperson via approved template
        _notify_salesperson(db, customer, parsed)

        pending_repeat.confirmation_sent    = confirmation_sent
        pending_repeat.forwarded_to_manager = True
        db.commit()

        # ── Write stats for confirmed repeat items ────────────────────────────
        if items:
            _update_stats_for_items(customer_phone, items, db)
            db.commit()

        return {"order_id": pending_repeat.id, "status": "repeat_confirmed", "parsed": parsed}

    if pending_replace:
        # Cancel the original order
        old_order = (
            db.query(Order)
            .filter(
                Order.customer_phone == customer_phone,
                Order.delivery_date  == get_delivery_date_str(),
                Order.is_cancelled   == False,
                Order.status.notin_(["pending_replace", "pending_repeat"]),
            )
            .order_by(Order.created_at.asc())
            .first()
        )
        if old_order:
            old_order.is_cancelled = True
            old_order.cancelled_at = datetime.now(IST)
            old_order.status       = "cancelled"

        # FIX: use _safe_load_list for both columns
        items         = _safe_load_list(pending_replace.parsed_items)
        unclear_items = _safe_load_list(pending_replace.unclear_items)

        parsed = {
            "items":          items,
            "unclear_items":  unclear_items,
            "delivery_date":  None,
            "delivery_time":  pending_replace.delivery_time,
            "is_unclear":     False,
            "unclear_reason": None,
        }
        pending_replace.status = "received"
        db.commit()

        confirmation_sent = send_order_confirmation(customer_phone, parsed, customer.restaurant_name)

        # Mirror _save_and_notify's branching: only send the normal
        # manager/salesperson alert when nothing is unclear. Otherwise route
        # through the unclear-items alert so the manager actually sees the
        # unresolved line and it isn't lost.
        if not unclear_items:
            # Notify manager via approved template
            send_manager_alert(MANAGER_PHONE, customer_phone, parsed, customer.restaurant_name)
            # Notify assigned salesperson via approved template
            _notify_salesperson(db, customer, parsed)
        else:
            unclear_msg = _build_unclear_alert(
                customer.restaurant_name, customer_phone, unclear_items, items
            )
            try:
                send_whatsapp_message(MANAGER_PHONE, unclear_msg)
            except Exception as e:
                logger.warning("Unclear items manager alert failed (replace-confirm): %s", e)

            sp = _get_assigned_salesperson(db, customer)
            if sp and sp.phone:
                try:
                    send_whatsapp_message(sp.phone, unclear_msg)
                except Exception as e:
                    logger.warning("Unclear items salesperson alert failed (replace-confirm): %s", e)

        pending_replace.confirmation_sent    = confirmation_sent
        pending_replace.forwarded_to_manager = True
        db.commit()

        # ── Write stats for confirmed replace items ───────────────────────────
        if items:
            _update_stats_for_items(customer_phone, items, db)
            db.commit()

        return {"order_id": pending_replace.id, "status": "replace_confirmed", "parsed": parsed}

    # Stale yes with no pending order — treat as a normal order attempt
    return _handle_order(db, customer, "yes", is_photo=False)


def _handle_confirm_no(db: Session, customer: Customer) -> dict:
    """Customer declined a pending repeat or replace."""
    customer_phone  = customer.phone_number
    pending_repeat  = _get_pending_repeat(db, customer_phone)
    pending_replace = _get_pending_replace(db, customer_phone)

    if pending_repeat:
        pending_repeat.is_cancelled = True
        pending_repeat.cancelled_at = datetime.now(IST)
        pending_repeat.status       = "cancelled"
        db.commit()
        send_whatsapp_message(customer_phone, "No problem! Just send your order anytime.")
        return {"order_id": None, "status": "repeat_cancelled", "parsed": None}

    if pending_replace:
        pending_replace.is_cancelled = True
        pending_replace.cancelled_at = datetime.now(IST)
        pending_replace.status       = "cancelled"
        db.commit()
        send_whatsapp_message(customer_phone, "✅ Kept your original order. No changes made.")
        return {"order_id": None, "status": "replace_cancelled", "parsed": None}

    # Stale no with nothing pending — silently ignore
    return {"order_id": None, "status": "stale_confirmation_ignored", "parsed": None}


def _handle_order(
    db: Session,
    customer: Customer,
    message: str,
    is_photo: bool,
    force_additional: bool = False,
) -> dict:
    """Parse the message as an order, handle duplicate/replace flow, and save.

    force_additional skips the before-cutoff replace-confirmation round trip
    (which waits on a customer WhatsApp reply) and always keeps the existing
    order intact, saving the new one as an additional order instead. Used by
    the admin "post order on behalf" action, which is synchronous and has no
    way to wait for a customer's yes/no.
    """
    customer_phone = customer.phone_number
    parsed = parse_template_order(customer_phone, message, db=db)

    # Nothing parseable at all
    if parsed["is_unclear"] and not parsed.get("unclear_items"):
        if _has_digits(message):
            # Contains digits → likely a failed order attempt, not small talk.
            # Save as unclear order so it surfaces immediately on the dashboard.
            # Store the message's number-bearing segments as unclear_items so
            # each becomes a dropdown-correctable row in the Unclear tab — rather
            # than a contentless order with nothing for the manager to fix.
            segments = _unparseable_segments(message)
            order = Order(
                plant_name     = PLANT_NAME,
                customer_phone = customer_phone,
                customer_name  = customer.restaurant_name,
                raw_message    = message,
                is_unclear     = True,
                unclear_items  = segments,
                unclear_reason = "Items not recognised — set each from the dropdown",
                business_date  = get_current_business_date_str(),
                delivery_date  = get_delivery_date_str(),
                status         = "received",
            )
            db.add(order)
            db.commit()

            send_whatsapp_message(
                customer_phone,
                "We received your message but couldn't read the items clearly. "
                "Our team will check and confirm shortly. 🙏",
            )
            return {"order_id": order.id, "status": "order_unclear_no_items", "parsed": None}

        else:
            send_whatsapp_message(
                customer_phone,
                "ℹ️ Sorry, I couldn't understand that as an order.\n\n"
                "Please send your order with item names and quantities, for example:\n"
                "_2 paneer, 1 curd, 3 butter_",
            )
            return {"order_id": None, "status": "unclear_message", "parsed": None}

    existing_order = get_todays_active_order(db, customer_phone)

    if existing_order:
        current_time_ist = datetime.now(IST)
        cutoff_time = current_time_ist.replace(
            hour=DISPATCH_CUTOFF_HOUR, minute=0, second=0, microsecond=0
        )

        if current_time_ist < cutoff_time and not force_additional:
            # Before dispatch cutoff — ask customer to confirm replacement.
            # FIX: pass lists directly to Order columns (no json.dumps).
            pending_items   = parsed.get("items", [])
            pending_unclear = parsed.get("unclear_items") or None
            pending = Order(
                plant_name     = PLANT_NAME,
                customer_name  = customer.restaurant_name,
                customer_phone = customer_phone,
                raw_message    = message,
                parsed_items   = pending_items,
                unclear_items  = pending_unclear,
                delivery_date  = get_delivery_date_str(),
                delivery_time  = parsed.get("delivery_time"),
                business_date  = get_current_business_date_str(),
                is_unclear     = False,
                status         = "pending_replace",
            )
            db.add(pending)
            db.commit()

            # FIX: use _safe_load_list to read existing order's parsed_items
            send_replace_confirmation_request(
                customer_phone = customer_phone,
                existing_items = _safe_load_list(existing_order.parsed_items),
                new_items      = parsed.get("items", []),
            )
            return {"order_id": pending.id, "status": "replace_requested", "parsed": parsed}

        else:
            # After dispatch cutoff — accept as additional order.
            # Save the order first via normal pipeline.
            result = _save_and_notify(db, customer, parsed, message, is_photo=is_photo)

            # Send an additional-order heads-up to manager via approved template
            # (the _save_and_notify call above already sends the standard alert,
            # so this is an extra contextual note about it being a second order).
            existing_items = _safe_load_list(existing_order.parsed_items)
            existing_lines = "\n".join(
                f"• {erp_display_name(i['product'])} — {i['quantity']} {i['unit']}"
                for i in existing_items
            )
            additional_note = (
                f"⚠️ *Additional Order — {PLANT_NAME}*\n\n"
                f"🏪 {customer.restaurant_name}\n"
                f"📱 {customer_phone}\n\n"
                f"*Original order* (placed at "
                f"{existing_order.created_at.strftime('%I:%M %p')}):\n"
                f"{existing_lines}\n\n"
                f"A second order was just placed — see dashboard for details."
            )
            try:
                send_whatsapp_message(MANAGER_PHONE, additional_note)
            except Exception as e:
                logger.warning("Additional order manager note failed: %s", e)

            sp = _get_assigned_salesperson(db, customer)
            if sp and sp.phone:
                try:
                    send_whatsapp_message(sp.phone, additional_note)
                except Exception as e:
                    logger.warning("Additional order salesperson note failed: %s", e)

            return result

    # No existing order — straightforward save
    return _save_and_notify(db, customer, parsed, message, is_photo=is_photo)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_incoming_order(
    db: Session,
    customer_phone: str,
    message: str,
    is_photo: bool = False,
    business_date: str | None = None,
    is_next_day_override: bool = False,
    force_additional: bool = False,
) -> dict:
    if business_date is None:
        business_date = compute_business_date(datetime.now(timezone.utc)).strftime("%Y-%m-%d")

    msg_lower = message.strip().lower()

    # ── 1. Customer lookup / first-time registration ──────────────────────────
    customer = get_customer_by_phone(db, customer_phone)

    if not customer:
        internal_phones = get_internal_phones(db)
        if customer_phone in internal_phones:
            logger.info("Ignored message from internal phone %s", customer_phone)
            return {"order_id": None, "status": "internal_phone_ignored", "parsed": None}

        customer = create_new_customer(db, customer_phone)
        send_whatsapp_message(
            customer_phone,
            f"👋 Welcome to *{PLANT_NAME}* Ordering System!\n\n"
            "Please reply with your *restaurant or hotel name* to continue.",
        )
        return {"order_id": None, "status": "awaiting_restaurant_name", "parsed": None}

    # ── 2. Classify intent ────────────────────────────────────────────────────
    pending_repeat  = _get_pending_repeat(db, customer_phone)
    pending_replace = _get_pending_replace(db, customer_phone)

    intent = classify_intent(
        message,
        onboarding          = (customer.onboarding_status == "awaiting_name"),
        has_pending_repeat  = pending_repeat is not None,
        has_pending_replace = pending_replace is not None,
    )

    # ── 3. Dispatch ───────────────────────────────────────────────────────────
    if intent == Intent.ONBOARDING:
        return _handle_onboarding(db, customer, message)

    if intent == Intent.CANCEL:
        return _handle_cancel(db, customer)

    if intent == Intent.REPEAT_LAST:
        return _handle_repeat(db, customer)

    if intent == Intent.HISTORY:
        return _handle_history(db, customer)

    if intent == Intent.GREETING:
        return {"order_id": None, "status": "greeting_ignored", "parsed": None}

    if intent == Intent.CONFIRM_YES:
        return _handle_confirm_yes(db, customer)

    if intent == Intent.CONFIRM_NO:
        return _handle_confirm_no(db, customer)

    # Intent.ORDER — default
    return _handle_order(db, customer, message, is_photo=is_photo, force_additional=force_additional)


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_all_orders(db: Session) -> list:
    return db.query(Order).order_by(Order.created_at.desc()).all()


def get_unclear_orders(db: Session) -> list:
    return (
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


def get_todays_orders(db: Session) -> list:
    today_str = get_current_business_date_str()
    return (
        db.query(Order)
        .filter(Order.business_date == today_str, Order.is_cancelled == False)
        .order_by(Order.created_at)
        .all()
    )


def get_orders_by_date(db: Session, target_date: date) -> list:
    return (
        db.query(Order)
        .filter(
            Order.business_date == target_date.strftime("%Y-%m-%d"),
            Order.is_cancelled == False,
        )
        .order_by(Order.created_at)
        .all()
    )


def get_customer_order_history(db: Session, customer_id: int) -> list:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        return []
    return (
        db.query(Order)
        .filter(Order.customer_phone == customer.phone_number)
        .order_by(Order.created_at.desc())
        .all()
    )


# ── Validation ────────────────────────────────────────────────────────────────

def validate_restaurant_name(name: str) -> str | None:
    stripped = name.strip()
    lower    = stripped.lower()

    if len(stripped) < 3:
        return "That name is too short."
    if len(stripped) > 60:
        return "That seems too long for a restaurant name."
    if stripped.replace(" ", "").isdigit():
        return "That looks like a number, not a restaurant name."
    if re.match(r'^[^a-zA-Z0-9\u0900-\u097F]+$', stripped):
        return "That doesn't look like a valid restaurant name."
    if len(set(lower.replace(" ", ""))) <= 2 and len(stripped) >= 4:
        return "That doesn't look like a valid restaurant name."

    letters_only = re.sub(r'[^a-zA-Z]', '', lower)
    if len(letters_only) >= 5 and not any(c in 'aeiou' for c in letters_only):
        return "That doesn't look like a valid restaurant name."

    if lower in GREETINGS:
        return "That looks like a greeting, not a restaurant name."
    if lower in FILLER_PHRASES:
        return "That doesn't look like a restaurant name. Please send your actual restaurant or hotel name."

    single_word_fillers = {
        "yes", "no", "ok", "okay", "sure", "fine", "please", "pls",
        "hi", "hello", "hey", "thanks", "thank", "haan", "nahi",
        "ji", "ha", "bhai", "yep", "yup", "nope", "good", "great",
    }
    words = lower.split()
    if len(words) >= 2 and all(w in single_word_fillers for w in words):
        return "That doesn't look like a restaurant name. Please send your actual restaurant or hotel name."

    return None
