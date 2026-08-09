"""
rate_lookup.py
--------------
Single source of truth for "what is today's rate for product X".

Rule (non-negotiable, from spec):
  1. customer_rate_overrides first (if customer_phone given and an active
     override exists for this product+date).
  2. Else daily_rates for today's business_date, if confirmed today.
  3. Else the most recent PRIOR daily_rates row for that product, flagged
     not_confirmed_today=True.
  4. NEVER return ₹0 / None silently — if truly no rate exists anywhere,
     that is reported explicitly so it can be surfaced (not invoiced).
"""
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select

from orderr_core.models.daily_rate import DailyRate
from orderr_core.models.rate_override import CustomerRateOverride


class RateResult:
    def __init__(
        self,
        product: str,
        rate_per_unit: Optional[float],
        unit: Optional[str],
        source: str,
        not_confirmed_today: bool,
        rate_date: Optional[date],
    ):
        self.product = product
        self.rate_per_unit = rate_per_unit
        self.unit = unit
        self.source = source  # "override" | "daily_rate" | "stale_daily_rate" | "none"
        self.not_confirmed_today = not_confirmed_today
        self.rate_date = rate_date

    @property
    def found(self) -> bool:
        return self.rate_per_unit is not None


def get_rate(
    db: Session,
    product: str,
    business_date: date,
    customer_phone: Optional[str] = None,
) -> RateResult:
    # 1. Customer override
    if customer_phone:
        override = db.execute(
            select(CustomerRateOverride).where(
                CustomerRateOverride.customer_phone == customer_phone,
                CustomerRateOverride.product == product,
                CustomerRateOverride.effective_from <= business_date,
                (CustomerRateOverride.effective_to.is_(None))
                | (CustomerRateOverride.effective_to >= business_date),
            ).order_by(
                CustomerRateOverride.effective_from.desc(),
                CustomerRateOverride.id.desc(),
            )
        ).scalars().first()
        if override is not None and override.rate_per_unit > 0:
            return RateResult(
                product, float(override.rate_per_unit), override.unit,
                "override", False, business_date,
            )

    # 2. Today's daily_rate
    todays = db.execute(
        select(DailyRate).where(
            DailyRate.product == product,
            DailyRate.business_date == business_date,
        ).order_by(DailyRate.created_at.desc())
    ).scalars().first()
    if todays is not None and todays.rate_per_unit > 0:
        return RateResult(
            product, float(todays.rate_per_unit), todays.unit,
            "daily_rate", False, business_date,
        )

    # 3. Most recent prior daily_rate
    prior = db.execute(
        select(DailyRate).where(
            DailyRate.product == product,
            DailyRate.business_date < business_date,
            DailyRate.rate_per_unit > 0,
        ).order_by(DailyRate.business_date.desc(), DailyRate.created_at.desc())
    ).scalars().first()
    if prior is not None:
        return RateResult(
            product, float(prior.rate_per_unit), prior.unit,
            "stale_daily_rate", True, prior.business_date,
        )

    # 4. Truly nothing — surfaced explicitly, never defaulted to 0.
    return RateResult(product, None, None, "none", True, None)
