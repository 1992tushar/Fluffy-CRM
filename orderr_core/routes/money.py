"""
Money tab (/dashboard/analytics/money) — one page answering "where is our
money going?": position, outflows by class, cash bridge, customers, leakage
watch, controls log and alerts. Read-only except the owner's pursue/provide
call on a dormant balance. Payee tagging reuses the Bank-recon endpoint
(/dashboard/analytics/bankrecon/categorize) so there is a single tagging path.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from orderr_core.auth import require_auth
from orderr_core.config import PLANT_NAME
from orderr_core.constants import IST
from orderr_core.database import get_db
from orderr_core.dates import get_today_ist
from orderr_core.services import bank_categorize, money_service
from orderr_core.templating import make_templates

router = APIRouter()
templates = make_templates()


@router.get("/analytics/money", response_class=HTMLResponse)
def analytics_money(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    today = get_today_ist()
    data = money_service.build(db, today)
    return templates.TemplateResponse(
        request=request,
        name="dashboard_analytics_money.html",
        context={
            "plant_name": PLANT_NAME,
            "current_time": datetime.now(IST).strftime("%d %b %Y, %I:%M %p"),
            "m": data, "cat_action": money_service.CAT_ACTION, "cat_name": money_service.CAT_NAME,
            "categories": [c for c in bank_categorize.CATEGORIES if c[2] in ("out", "nonpl", "other")],
            "analytics_view": "money",
        },
    )


@router.get("/analytics/money/drill")
def analytics_money_drill(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    klass: str = Query(...),
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    if klass not in money_service.CLASSES + list(money_service.OWNER_IN_CLASSES):
        raise HTTPException(status_code=400, detail="Unknown class.")
    rows = money_service.txn_drilldown(db, month, klass)
    for r in rows:
        r["date"] = r["date"].isoformat() if r["date"] else ""
    return JSONResponse({"rows": rows})


@router.post("/analytics/money/decision")
async def analytics_money_decision(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(require_auth),
):
    body = await request.json()
    try:
        cid = int(body.get("customer_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="customer_id is required.")
    try:
        result = money_service.set_decision(db, cid, (body.get("decision") or "").strip().lower() or None,
                                            body.get("note"), username, get_today_ist())
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"result": result})
