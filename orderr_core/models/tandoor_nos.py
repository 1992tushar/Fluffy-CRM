"""
tandoor_nos — manually-entered bird COUNT (nos) for Tandoor, per day / side / form.

Vasy books one quantity per line and has no field for a count, so the number of
birds is kept here, next to the Vasy quantity, to get kg-per-bird and a profit
that matches stock. One row per (entry_date, side, kind); a blank/0 entry deletes it.

  side: "purchase" | "sale"
  kind: the Tandoor form, e.g. "Live bird", "Dressed - without skin",
        "Dressed - with skin" (same labels purchase_analytics._tandoor_kind returns)
"""
from datetime import date, datetime

from sqlalchemy import Integer, String, Date, DateTime, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class TandoorNos(Base):
    __tablename__ = "tandoor_nos"
    __table_args__ = (UniqueConstraint("entry_date", "side", "kind", name="uq_tandoor_nos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    nos: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
