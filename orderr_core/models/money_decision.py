"""
money_decisions — the owner's call on an old unpaid customer balance
("pursue" it or "provide" for it), recorded from the Money tab.

One row per customer (unique customer_id), upserted. This is an OrdeRR-side
note only: it never touches the Vasy ledger, the receivables snapshot or the
bad_debts overlay, so recording or clearing a decision cannot change any
other screen's numbers. (Writing a customer off for AR purposes stays on the
existing bad-debt flow.)
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Date, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base

DECISIONS = ("pursue", "provide")


class MoneyDecision(Base):
    __tablename__ = "money_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=False, unique=True, index=True
    )
    decision: Mapped[str] = mapped_column(String(10), nullable=False)   # pursue | provide
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    decided_on: Mapped[date] = mapped_column(Date, nullable=False)
    decided_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
