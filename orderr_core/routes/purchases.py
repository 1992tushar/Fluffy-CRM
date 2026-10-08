"""
Purchase analytics route — own router (like wastage) so dashboard.py stays
untouched. Mounted under /dashboard in main.py → /dashboard/analytics/purchases.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from orderr_core.database import get_db
from orderr_core.auth import require_auth
from orderr_core.config import PLANT_NAME
from orderr_core.constants import IST
from orderr_core.services.order_service import get_current_business_date
from orderr_core.templating import make_templates

router = APIRouter()
templates = make_templates()


def _parse_iso(s):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


@router.get("/analytics/purchases", response_class=HTMLResponse)
def analytics_purchases(
    request: Request,
    frm: str = Query(default=None, alias="from", description="window start YYYY-MM-DD"),
    to: str = Query(default=None, description="window end YYYY-MM-DD"),
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """What we bought — spend/qty by item and supplier, rate trend, rate-spike flags.
    Defaults to the current month-to-date."""
    from orderr_core.services import purchase_analytics

    today = get_current_business_date()
    from_date, to_date = _parse_iso(frm), _parse_iso(to)
    if from_date is None:
        from_date = today.replace(day=1)
    if to_date is None:
        to_date = today
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    data = purchase_analytics.purchase_overview(db, from_date, to_date)

    return templates.TemplateResponse(
        request=request,
        name="dashboard_analytics_purchases.html",
        context={
            "plant_name": PLANT_NAME,
            "current_time": datetime.now(IST).strftime("%d %b %Y, %I:%M %p"),
            "p": data,
            "today_iso": today.isoformat(),
            "month_start_iso": today.replace(day=1).isoformat(),
            "analytics_view": "purchases",
        },
    )


@router.get("/analytics/purchases/tandoor-days")
def tandoor_days(
    side: str,
    kind: str,
    frm: str = Query(alias="from"),
    to: str = Query(),
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """Per-day Vasy qty + saved nos for one Tandoor row (feeds the Nos editor)."""
    from orderr_core.services import purchase_analytics

    start, end = _parse_iso(frm), _parse_iso(to)
    if side not in purchase_analytics.TANDOOR_SIDES or not start or not end:
        raise HTTPException(status_code=400, detail="Bad request.")
    return JSONResponse({"days": purchase_analytics.tandoor_days(db, side, kind, start, end)})


@router.post("/analytics/purchases/bill-nos")
async def bill_nos_save(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """Save the bird count (nos) for one Tandoor line on one purchase bill."""
    from orderr_core.services import purchase_analytics

    body = await request.json()
    err = purchase_analytics.save_bill_nos(db, body)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return JSONResponse({"status": "ok"})


@router.post("/analytics/purchases/tandoor-nos")
async def tandoor_nos_save(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    """Save the bird counts (nos) entered for one Tandoor row."""
    from orderr_core.services import purchase_analytics

    body = await request.json()
    err = purchase_analytics.save_tandoor_nos(db, body)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return JSONResponse({"status": "ok"})
