"""
sundry_items / sundry_purchases — the sundries purchase register (Registers &
Reminders spec §2): item-level record of small non-trade buying (cleaning
material, carry bags, stationery…) with price/vendor/cadence memory.

STANDALONE — no sync, link or reconciliation to Vasy (owner decision
2026-07-13). Holds no balances, so nothing to double-count. NOT stock
tracking: no quantities on hand, no consumption math — cadence memory only.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Numeric, Boolean, Date, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class SundryItem(Base):
    __tablename__ = "sundry_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    name_key: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)  # normalized (dedup)
    category: Mapped[str] = mapped_column(String, nullable=False, default="other")
    unit: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # learned buying cadence (median days between purchases; needs ≥3 buys)
    typical_gap_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SundryPurchase(Base):
    __tablename__ = "sundry_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sundry_items.id"), nullable=False, index=True
    )
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    qty: Mapped[Optional[float]] = mapped_column(Numeric(12, 3), nullable=True)
    rate: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    vendor: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    paid_via: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # cash | bank | other
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # optional invoice photo/PDF — stored in the dedicated Drive account, not in
    # this DB, so only the pointer (file id + shareable link) lives here.
    invoice_drive_file_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invoice_drive_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invoice_filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
