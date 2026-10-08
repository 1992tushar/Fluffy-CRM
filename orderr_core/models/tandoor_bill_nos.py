"""
tandoor_bill_nos — manually-entered bird COUNT (nos) for a Tandoor line on a purchase bill.

Vasy has no field for a bird count, so it is kept here against the bill. One row per
(bill_no, kind); a blank/0 entry deletes it. Keyed by bill_no (not the Vasy row id) so
it survives a purchase re-import. `kind` is the label purchase_analytics._tandoor_kind
returns ("Live bird", "Dressed - without skin", ...).
"""
from datetime import datetime

from sqlalchemy import Integer, String, DateTime, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class TandoorBillNos(Base):
    __tablename__ = "tandoor_bill_nos"
    __table_args__ = (UniqueConstraint("bill_no", "kind", name="uq_tandoor_bill_nos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bill_no: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    nos: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
