"""
monthly_overheads — manually-entered accrual overheads that Vasy's expense
register never sees. Heads: **Salaries**, **Daily saving** (the daily-collection
amount the owner sets aside), **Electricity bill**, and **Pune loan** (a
recurring loan installment); the `head` field leaves room for more later.

The Business "Net" (Sales − Purchases − Expenses) understated profit because
these are paid outside the Vasy ledger. Each row is one month's figure for one
head; business_overview() sums the rows whose `period` (always the 1st of the
month) falls in the viewed window and folds them into money-out / Net.

These are treated as accrual P&L lines here, so they belong in Net — unlike
loans / owner cash-in which are cash-only and live in the Cash Book.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Numeric, Date, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base

# Canonical heads the Business box edits. Keep these stable — the (month, head)
# upsert keys on them, and the UI prefills the current month from them.
HEAD_SALARIES = "Salaries"
HEAD_SAVING = "Daily saving"
HEAD_ELECTRICITY = "Electricity bill"
HEAD_PUNE_LOAN = "Pune loan"


class MonthlyOverhead(Base):
    __tablename__ = "monthly_overheads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # always normalised to the 1st of the month the figure is for
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    head: Mapped[str] = mapped_column(String(40), nullable=False, default=HEAD_SALARIES)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
