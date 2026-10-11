"""
ignored_customers — customers the owner has told the analytics to leave out of
AR / credit totals (stale accounts, e.g. an old advance that can't be deleted
in Vasy and just distorts the overall picture).

One row per customer. OrdeRR-side overlay only — the Vasy ledger is never
touched, so deleting the row instantly brings the customer back into the totals.
Unlike bad_debts this is not a claim about recoverability: it hides both debit
and credit balances.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Numeric, Date, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class IgnoredCustomer(Base):
    __tablename__ = "ignored_customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=False, unique=True, index=True
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)  # signed balance when ignored
    reason: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ignored_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
