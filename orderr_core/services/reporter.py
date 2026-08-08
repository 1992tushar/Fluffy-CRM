import os
from orderr_core.utils import fmt_qty
import json
import smtplib
from datetime import datetime, date, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from sqlalchemy.orm import Session
from sqlalchemy import func

from orderr_core.models.order import Order
from orderr_core.models.inbound_message import InboundMessage
from orderr_core.models.customer import Customer
from orderr_core.services.notifier import send_whatsapp_message
from orderr_core.services.order_service import get_current_business_date_str

from orderr_core.config import MANAGER_PHONE, PLANT_NAME
from orderr_core.constants import IST

# ── Email config ──────────────────────────────────────────────────────────────
REPORT_EMAIL   = os.getenv("REPORT_EMAIL", "")
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "")
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", f"{PLANT_NAME} OrdeRR")


from orderr_core.utils import safe_list as _safe_list


def report_product_name(product: str) -> str:
    """Display name for the production report — the exact Vasy ERP item name
    when the product maps to the ERP catalog, else the name unchanged.

    Thin alias over the app-wide erp_display_name() so the report labels
    products identically to every other surface (messages, invoices,
    dashboard). Stored data / rate keys stay on the friendly name."""
    from orderr_core.services.template_parser import erp_display_name
    return erp_display_name(product)


def report_short_product_name(product: str) -> str:
    """Compact display name for the per-hotel rows on the printed sheet, where
    width is tight. Full Vasy names stay in the top Product Summary; each hotel's
    line items use the short canonical form (e.g. 'Curry Cut') so nothing wraps."""
    from orderr_core.services.template_parser import short_product_name
    return short_product_name(product)


def _product_totals_for(orders: list) -> dict:
    """Sum parsed items across a set of orders into
    {key: {product, unit, total_quantity}} keyed by product+unit."""
    totals: dict = {}
    for order in orders:
        for item in _safe_list(order.parsed_items):
            product  = report_product_name(item.get("product", "Unknown").strip())
            quantity = item.get("quantity", 0)
            unit     = item.get("unit", "kg").lower()
            key      = f"{product.lower()}||{unit}"
            if key not in totals:
                totals[key] = {"product": product, "unit": unit, "total_quantity": 0}
            totals[key]["total_quantity"] += quantity
    return totals


def merge_items(items: list) -> list:
    merged = {}
    for item in items:
        product  = report_product_name(item.get("product", "Unknown").strip())
        quantity = item.get("quantity", 0)
        unit     = item.get("unit", "kg").lower()
        key      = f"{product.lower()}||{unit}"
        if key not in merged:
            merged[key] = {"product": product, "quantity": 0, "unit": unit}
        merged[key]["quantity"] += quantity
    return list(merged.values())


def get_todays_customer_notes(db: Session, target_date: date | None = None) -> list[dict]:
    day = target_date if target_date is not None else datetime.now(IST).date()
    note_messages = (
        db.query(InboundMessage)
        .filter(
            func.date(InboundMessage.received_at) == day,
            InboundMessage.processing_status == "NOTE",
            InboundMessage.is_duplicate == False,
        )
        .order_by(InboundMessage.customer_phone, InboundMessage.received_at.asc())
        .all()
    )
    if not note_messages:
        return []
    phones = list({m.customer_phone for m in note_messages})
    customers = db.query(Customer).filter(Customer.phone_number.in_(phones)).all()
    phone_to_name = {c.phone_number: c.restaurant_name or c.phone_number for c in customers}
    notes = []
    for m in note_messages:
        notes.append({
            "restaurant_name": phone_to_name.get(m.customer_phone, m.customer_phone),
            "phone":           m.customer_phone,
            "note":            m.raw_message or "",
            "time":            m.received_at.strftime("%I:%M %p") if m.received_at else "",
        })
    return notes


def generate_daily_report(db: Session, target_date: date | None = None) -> dict:
    now_ist   = datetime.now(IST)
    if target_date is None:
        business_date_str = get_current_business_date_str()
        date_str          = now_ist.strftime("%d %B %Y")
    else:
        business_date_str = target_date.strftime("%Y-%m-%d")
        date_str          = target_date.strftime("%d %B %Y")

    orders = db.query(Order).filter(
        Order.business_date == business_date_str,
        Order.is_cancelled  == False,
    ).all()

    clear_orders   = [o for o in orders if not o.is_unclear]
    unclear_orders = [o for o in orders if o.is_unclear]

    # ── Product totals (summary section) ─────────────────────────────────────
    product_totals = _product_totals_for(clear_orders)

    # ── Per-area groups (busiest area first, "Area not set" last) ─────────────
    # Each group carries its own hotels + a per-area product subtotal so the
    # printed sheet reads route-by-route for dispatch.
    from orderr_core.services.order_service import group_orders_by_area
    area_groups = group_orders_by_area(db, clear_orders)
    for g in area_groups:
        g["product_totals"] = _product_totals_for(g["orders"])

    # ── WhatsApp-friendly product summary string ──────────────────────────────
    if not orders:
        product_summary = "No orders received today"
    else:
        lines = []
        for data in product_totals.values():
            qty     = data["total_quantity"]
            qty_str = fmt_qty(qty)
            lines.append(f"{data['product']} - {qty_str} {data['unit']}")
        if unclear_orders:
            lines.append(f"Unclear: {len(unclear_orders)} (need follow up)")
        product_summary = " | ".join(lines)

    total_items = sum(len(_safe_list(o.parsed_items)) for o in clear_orders)

    return {
        "date_str":        date_str,
        "total_orders":    str(len(clear_orders)),
        "total_items":     str(total_items),
        "product_summary": product_summary,
        "_clear_orders":   clear_orders,
        "_unclear_orders": unclear_orders,
        "_product_totals": product_totals,
        "_area_groups":    area_groups,
    }


# ── Printable delivery sheet (HTML) ──────────────────────────────────────────

def _build_print_html(data: dict, notes: list[dict]) -> str:
    date_str       = data["date_str"]
    clear_orders   = data["_clear_orders"]
    unclear_orders = data["_unclear_orders"]
    product_totals = data["_product_totals"]
    area_groups    = data.get("_area_groups", [])
    generated_at   = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")

    def _hotel_block(idx: int, order) -> str:
        name     = order.customer_name or order.customer_phone
        delivery = f" &nbsp;·&nbsp; {order.delivery_time}" if order.delivery_time else ""
        item_rows = ""
        for item in _safe_list(order.parsed_items):
            qty_str = fmt_qty(item.get("quantity", 0))
            item_rows += f"""
                    <tr>
                        <td class="product-name" style="padding-left:20px;">{report_short_product_name(item.get('product','—'))}</td>
                        <td class="qty-ordered">{qty_str} {item.get('unit','kg')}</td>
                        <td class="qty-delivered"><div class="write-box"></div></td>
                    </tr>"""
        for raw in _safe_list(order.unclear_items):
            item_rows += f"""
                    <tr>
                        <td class="product-name" style="padding-left:24px;color:#e67e22;">⚠️ {raw}</td>
                        <td class="qty-ordered" style="color:#e67e22;">unclear</td>
                        <td class="qty-delivered"><div class="write-box"></div></td>
                    </tr>"""
        return f"""
            <div class="hotel-block">
                <div class="hotel-name">{idx}.&nbsp; {name}{delivery}</div>
                <table class="data-table">
                    <tbody>{item_rows}</tbody>
                </table>
            </div>"""

    # ── Product summary rows ──────────────────────────────────────────────────
    summary_rows = ""
    for entry in product_totals.values():
        qty = entry["total_quantity"]
        qty_str = fmt_qty(qty)
        summary_rows += f"""
                <tr>
                    <td class="product-name">{entry['product']}</td>
                    <td class="qty-ordered">{qty_str} {entry['unit']}</td>
                    <td class="qty-delivered"><div class="write-box"></div></td>
                </tr>"""

    if not summary_rows:
        summary_rows = '<tr><td colspan="3" style="text-align:center;color:#999;padding:16px;">No orders today</td></tr>'

    # ── Per-area sections: each area gets its own product subtotal + hotels ───
    area_sections = ""
    for group in area_groups:
        area   = group["area"]
        orders = group["orders"]
        totals = group.get("product_totals", {})

        subtotal_rows = "".join(
            f"""
                    <tr>
                        <td class="product-name">{entry['product']}</td>
                        <td class="qty-ordered">{fmt_qty(entry['total_quantity'])} {entry['unit']}</td>
                    </tr>"""
            for entry in totals.values()
        )

        hotel_blocks = "".join(_hotel_block(idx, o) for idx, o in enumerate(orders, 1))

        area_sections += f"""
        <div class="area-section">
            <div class="area-title">📍 {area} <span class="area-count">{len(orders)} hotel{'s' if len(orders) != 1 else ''}</span></div>
            <table class="data-table area-subtotal">
                <thead>
                    <tr><th class="product-name">Product (this area)</th><th class="qty-ordered">Ordered Qty</th></tr>
                </thead>
                <tbody>{subtotal_rows}</tbody>
            </table>
            <div class="hotel-grid">{hotel_blocks}</div>
        </div>"""

    if not area_sections:
        area_sections = '<p style="text-align:center;color:#999;padding:24px 0;">No confirmed orders</p>'

    # ── Unclear section ───────────────────────────────────────────────────────
    unclear_section = ""
    if unclear_orders:
        u_rows = "".join(
            f'<tr><td style="padding:6px 8px;">{o.customer_name or o.customer_phone}</td>'
            f'<td style="padding:6px 8px;font-style:italic;color:#666;">&ldquo;{o.raw_message or ""}&rdquo;</td>'
            f'<td style="padding:6px 8px;color:#999;font-size:11px;">{o.unclear_reason or "—"}</td></tr>'
            for o in unclear_orders
        )
        unclear_section = f"""
            <div class="section" style="margin-top:24px;">
                <div class="section-title" style="color:#c0392b;">&#9888; Unclear Orders — Follow Up ({len(unclear_orders)})</div>
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Customer</th>
                            <th>Message Received</th>
                            <th>Reason</th>
                        </tr>
                    </thead>
                    <tbody>{u_rows}</tbody>
                </table>
            </div>"""

    # ── Notes section ─────────────────────────────────────────────────────────
    notes_section = ""
    if notes:
        n_rows = "".join(
            f'<tr><td style="padding:6px 8px;font-weight:600;">{n["restaurant_name"]}'
            f'{"<span style=color:#999;font-size:11px;> (" + n["time"] + ")</span>" if n["time"] else ""}</td>'
            f'<td style="padding:6px 8px;font-style:italic;">&ldquo;{n["note"]}&rdquo;</td></tr>'
            for n in notes
        )
        notes_section = f"""
            <div class="section" style="margin-top:24px;">
                <div class="section-title">&#128221; Customer Notes</div>
                <table class="data-table">
                    <thead><tr><th>Customer</th><th>Note</th></tr></thead>
                    <tbody>{n_rows}</tbody>
                </table>
            </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Daily Production Report — {PLANT_NAME} — {date_str}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}

  body {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 12px;
    color: #1a1a1a;
    background: #fff;
    padding: 18px 22px;
  }}

  /* ── Header ── */
  .header {{
    border-bottom: 3px solid #1a1a1a;
    padding-bottom: 10px;
    margin-bottom: 20px;
  }}
  .header-plant {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: #666;
  }}
  .header-title {{
    font-size: 20px;
    font-weight: 700;
    margin: 2px 0;
  }}
  .header-meta {{
    font-size: 12px;
    color: #555;
    margin-top: 4px;
    display: flex;
    justify-content: space-between;
  }}

  /* ── Sections ── */
  .section {{ margin-bottom: 16px; }}
  .section-title {{
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: #333;
    border-bottom: 1.5px solid #333;
    padding-bottom: 5px;
    margin-bottom: 10px;
  }}

  /* ── Tables ── */
  .data-table {{
    width: 100%;
    border-collapse: collapse;
  }}
  .data-table thead tr {{
    background: #f0f0f0;
  }}
  .data-table th {{
    padding: 7px 8px;
    text-align: left;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .05em;
    border-bottom: 1.5px solid #ccc;
  }}
  .data-table td {{
    border-bottom: 1px solid #e8e8e8;
    vertical-align: middle;
  }}
  .data-table tr:last-child td {{ border-bottom: none; }}

  /* ── Column widths ── */
  .product-name   {{ padding: 3px 8px; width: 55%; }}
  .qty-ordered    {{ padding: 3px 8px; width: 20%; font-weight: 600; }}
  .qty-delivered  {{ padding: 3px 8px; width: 25%; }}

  /* ── Write box (blank for pen — kept generous so the photo/OCR stays legible) ── */
  .write-box {{
    border-bottom: 1.5px solid #333;
    height: 18px;
    width: 90%;
  }}

  /* ── Hotel blocks — single full-width column, roomy for pen entry ── */
  /* One hotel per row: the full width lets short product names sit on one line  */
  /* (no wrapping) with a generous delivered-qty box. Each block is self-        */
  /* contained (name + its items) and never splits across a page break.          */
  .hotel-grid {{ }}
  .hotel-block {{
    margin: 0 0 14px;
    break-inside: avoid;
    page-break-inside: avoid;
  }}
  .hotel-name {{
    font-size: 13px;
    font-weight: 700;
    background: #f5f5f5;
    padding: 5px 9px;
    border-left: 4px solid #1a1a1a;
    margin-bottom: 4px;
  }}
  /* Roomier line-item rows inside each hotel (doesn't touch the summary table) */
  .hotel-block .data-table td {{ padding: 6px 8px; }}
  .hotel-block .write-box {{ width: 70%; }}

  /* ── Area sections ── */
  .area-section {{
    margin-bottom: 22px;
    break-inside: avoid-column;
  }}
  .area-title {{
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .05em;
    background: #1a1a1a;
    color: #fff;
    padding: 5px 9px;
    margin-bottom: 8px;
  }}
  .area-title .area-count {{
    float: right;
    font-weight: 600;
    font-size: 11px;
    opacity: .85;
    letter-spacing: 0;
    text-transform: none;
  }}
  .area-subtotal {{
    margin-bottom: 10px;
    border: 1px solid #ddd;
  }}
  .area-subtotal th {{ background: #f0f0f0; }}
  .area-subtotal td {{ padding: 3px 8px; }}
  .area-subtotal .qty-ordered {{ font-weight: 600; }}

  /* ── Footer ── */
  .footer {{
    margin-top: 32px;
    border-top: 1px solid #ccc;
    padding-top: 8px;
    font-size: 10px;
    color: #999;
    display: flex;
    justify-content: space-between;
  }}

  /* ── Signature row ── */
  .sign-row {{
    display: flex;
    gap: 40px;
    margin-top: 24px;
    break-inside: avoid;
    page-break-inside: avoid;
  }}
  .sign-box {{
    flex: 1;
    border-top: 1.5px solid #333;
    padding-top: 6px;
    font-size: 11px;
    color: #555;
    text-align: center;
  }}

  /* ── Print styles ── */
  @media print {{
    body {{ padding: 12px 18px; }}
    @page {{ margin: 12mm 14mm; }}
    .no-print {{ display: none; }}
  }}
</style>
</head>
<body>

<!-- Print button — works because this is opened directly in browser, not inside Gmail -->
<div class="no-print" style="text-align:right;margin-bottom:16px;">
  <button onclick="window.print()"
    style="padding:8px 20px;background:#1a1a1a;color:#fff;border:none;
           border-radius:6px;font-size:13px;cursor:pointer;font-family:Arial,sans-serif;">
    🖨️ Print
  </button>
</div>

<!-- Header -->
<div class="header">
  <div class="header-plant">{PLANT_NAME}</div>
  <div class="header-title">Daily Production Report</div>
  <div class="header-meta">
    <span>{date_str}</span>
    <span>Total Hotels: {len(clear_orders)} &nbsp;|&nbsp; Generated: {generated_at}</span>
  </div>
</div>

<!-- Section 1: Product Summary -->
<div class="section">
  <div class="section-title">Product Summary — Total Quantities</div>
  <table class="data-table">
    <thead>
      <tr>
        <th class="product-name">Product</th>
        <th class="qty-ordered">Ordered Qty</th>
        <th class="qty-delivered">Delivered Qty ✏️</th>
      </tr>
    </thead>
    <tbody>
      {summary_rows}
    </tbody>
  </table>
</div>

<!-- Section 2: Area-wise Orders (each area: subtotal + 2-column hotel grid) -->
<div class="section">
  <div class="section-title">Area-wise Orders</div>
  {area_sections}
</div>

{unclear_section}
{notes_section}

<!-- Signature row -->
<div class="sign-row">
  <div class="sign-box">Prepared by</div>
  <div class="sign-box">Checked by</div>
  <div class="sign-box">Accountant</div>
</div>

<div class="footer">
  <span>OrdeRR &middot; {PLANT_NAME}</span>
  <span>{generated_at}</span>
</div>

</body>
</html>"""


def _send_email_report(data: dict, notes: list[dict]):
    """Send the printable Daily Production Report as an HTML email. No-op if env vars not set."""
    if not REPORT_EMAIL:
        print("ℹ️  REPORT_EMAIL not set — skipping email delivery")
        return
    if not SMTP_USER or not SMTP_PASSWORD:
        print("⚠️  SMTP_USER/SMTP_PASSWORD not set — cannot send email report")
        return

    recipients = [r.strip() for r in REPORT_EMAIL.split(",") if r.strip()]
    if not recipients:
        return

    subject   = f"📋 Daily Production Report — {PLANT_NAME} · {data['date_str']}"
    html_body = _build_print_html(data, notes)

    # ── Plain text email body ─────────────────────────────────────────────────
    plain_body = (
        f"Daily Production Report — {PLANT_NAME}\n"
        f"{data['date_str']}\n\n"
        f"Total Orders : {data['total_orders']}\n"
        f"Product Summary : {data['product_summary']}\n\n"
        f"Open the attached HTML file in your browser and press Ctrl+P to print the delivery sheet."
    )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = ", ".join(recipients)

    # Plain text part
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))

    # HTML delivery sheet as attachment
    filename = f"production_report_{data['date_str'].replace(' ', '_')}.html"
    attachment = MIMEBase("text", "html")
    attachment.set_payload(html_body.encode("utf-8"))
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", "attachment", filename=filename)
    attachment.add_header("Content-Type", "text/html; charset=utf-8")
    msg.attach(attachment)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
        print(f"✅ Daily Production Report email sent → {recipients}")
    except Exception as exc:
        print(f"❌ Email report failed: {exc}")


# ── public entry point ────────────────────────────────────────────────────────

def send_daily_report(db: Session):
    """Daily consolidated report: printable HTML delivery sheet to REPORT_EMAIL
    + customer-notes WhatsApp follow-up. The manager_daily_report WhatsApp
    template was dropped 2026-07-14 (owner decision) — the live status page
    (config.report_url()) replaced WhatsApp report messages."""
    print("\n⏰ Generating Daily Production Report...")

    data = generate_daily_report(db)

    # ── Customer notes follow-up (unchanged) ──────────────────────────────────
    notes = []
    try:
        notes = get_todays_customer_notes(db)
        if notes:
            lines = [f"📝 *Customer Notes — {PLANT_NAME}*", f"{data['date_str']}", ""]
            for n in notes:
                time_str = f" ({n['time']})" if n['time'] else ""
                lines.append(f"• *{n['restaurant_name']}*{time_str}: {n['note']}")
            notes_msg = "\n".join(lines)
            send_whatsapp_message(MANAGER_PHONE, notes_msg)
            print(f"✅ Customer notes sent ({len(notes)} note(s))")
        else:
            print("ℹ️ No customer notes today — skipping notes message")
    except Exception as e:
        print(f"⚠️ Customer notes follow-up failed: {e}")

    # ── 2. Email — printable delivery sheet (new) ─────────────────────────────
    try:
        _send_email_report(data, notes)
    except Exception as e:
        print(f"⚠️ Email report failed: {e}")


# ── P3-6 Manager daily digest (analytics) ─────────────────────────────────

def send_manager_digest(db: Session) -> bool:
    """Compose and WhatsApp the analytics daily digest to MANAGER_PHONE.
    Returns True if a message was sent. No-op (returns False) if MANAGER_PHONE
    is unset. Composition lives in analytics_service.manager_digest (testable)."""
    from orderr_core.services.analytics_service import manager_digest
    from orderr_core.services.order_service import get_current_business_date

    if not MANAGER_PHONE:
        print("⚠️ Manager digest skipped — MANAGER_PHONE not set")
        return False
    today = get_current_business_date()
    digest = manager_digest(db, today)
    resp = send_whatsapp_message(MANAGER_PHONE, digest["text"])

    # Registers & Reminders — dedicated attention message ("both channels",
    # owner decision 2026-07-13). Sent only when the feed is non-empty.
    try:
        from orderr_core.services.reminders_service import attention_message
        nag = attention_message(db, today)
        if nag:
            send_whatsapp_message(MANAGER_PHONE, nag)
            print("✅ Reminders attention message sent")
    except Exception as e:
        print(f"⚠️ Reminders attention message failed: {e}")

    return resp is not None
