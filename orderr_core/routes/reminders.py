"""
Registers & Reminders routes — 📌 Reminders screen + register mutations.
Mounted under /dashboard (like the wastage router). Spec:
REGISTERS_REMINDERS_REQUIREMENTS.md.
"""
from datetime import datetime

from typing import Optional

from fastapi import APIRouter, Depends, Request, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from orderr_core.auth import require_auth
from orderr_core.config import PLANT_NAME
from orderr_core.constants import IST
from orderr_core.database import get_db
from orderr_core.services.order_service import get_current_business_date
from orderr_core.services import reminders_service
from orderr_core.templating import make_templates

router = APIRouter()
templates = make_templates()


@router.get("/reminders", response_class=HTMLResponse)
def reminders_screen(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """📌 Reminders — attention feed + the three registers (P1)."""
    today = get_current_business_date()
    return templates.TemplateResponse(
        request=request,
        name="dashboard_reminders.html",
        context={
            "plant_name": PLANT_NAME,
            "current_time": datetime.now(IST).strftime("%d %b %Y, %I:%M %p"),
            "today": today.strftime("%Y-%m-%d"),
            "today_display": today.strftime("%d %b %Y"),
            "feed": reminders_service.attention_feed(db, today),
            "sundries": reminders_service.sundries_overview(db, today),
            "notes": reminders_service.notes_overview(db, today),
            "dates": reminders_service.dates_overview(db, today),
        },
    )


def _ok_or_400(err):
    if err:
        raise HTTPException(status_code=400, detail=err)
    return JSONResponse({"status": "ok"})


@router.post("/reminders/sundry")
async def reminders_add_sundry(
    item_name: str = Form(""),
    amount: str = Form(""),
    purchase_date: str = Form(""),
    category: str = Form(""),
    qty: str = Form(""),
    unit: str = Form(""),
    rate: str = Form(""),
    vendor: str = Form(""),
    paid_via: str = Form(""),
    note: str = Form(""),
    invoice: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    body = {
        "item_name": item_name, "amount": amount, "purchase_date": purchase_date,
        "category": category, "qty": qty, "unit": unit, "rate": rate,
        "vendor": vendor, "paid_via": paid_via, "note": note,
    }
    invoice_file = None
    if invoice is not None and invoice.filename:
        invoice_file = (await invoice.read(), invoice.filename,
                        invoice.content_type or "application/octet-stream")
    return _ok_or_400(reminders_service.add_sundry_purchase(
        db, body, get_current_business_date(), invoice_file=invoice_file))


@router.post("/reminders/note")
async def reminders_add_note(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    body = await request.json()
    return _ok_or_400(reminders_service.add_note(
        db, body, get_current_business_date()))


@router.post("/reminders/note/{note_id}/close")
async def reminders_close_note(
    note_id: int,
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    body = await request.json()
    return _ok_or_400(reminders_service.close_note(
        db, note_id, str(body.get("status") or ""), str(body.get("resolution_note") or "")))


@router.post("/reminders/note/{note_id}/snooze")
async def reminders_snooze_note(
    note_id: int,
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    body = await request.json()
    return _ok_or_400(reminders_service.snooze_note(
        db, note_id, body.get("until"), get_current_business_date()))


@router.post("/reminders/date")
async def reminders_add_date(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    body = await request.json()
    return _ok_or_400(reminders_service.add_important_date(db, body))


@router.post("/reminders/date/{date_id}/done")
async def reminders_date_done(
    date_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    return _ok_or_400(reminders_service.mark_date_done(
        db, date_id, get_current_business_date()))


@router.post("/reminders/date/{date_id}/toggle-pause")
async def reminders_date_toggle_pause(
    date_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    return _ok_or_400(reminders_service.toggle_date_paused(db, date_id))
