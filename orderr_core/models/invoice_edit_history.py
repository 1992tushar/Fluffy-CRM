"""
invoice_edit_history + vasy_line_snapshot — audit trail for invoice LINES that
were changed after they were first entered (typically a rate quietly lowered).

Two gaps this closes:
  * Vasy side — import_sales_items() snapshot-replaces vasy_sales_items every
    sync, so a rate/qty edited on an existing voucher just overwrote the old
    line with no trace. vasy_line_snapshot remembers each voucher's lines as of
    the previous sync; the next sync diffs against it.
  * OrdeRR side — reissue_invoice() rebuilds invoice_items in place, so the old
    rates vanished. It now logs the diff before replacing the rows.

A third source, 'orderr_vs_vasy', compares what OrdeRR invoiced (the rate that
was dictated) with the voucher the first time it shows up in Vasy — this
catches an edit made in Vasy BEFORE the first sync that saw the voucher.

invoice_edit_history is append-only: one row per (voucher, product) change.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Numeric, Date, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class InvoiceEditHistory(Base):
    __tablename__ = "invoice_edit_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 'vasy' (changed between two Vasy syncs) | 'orderr' (reissue in OrdeRR)
    # | 'orderr_vs_vasy' (Vasy voucher differs from the OrdeRR invoice at first sight)
    source: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    voucher_no: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    party_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    customer_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=True, index=True
    )
    invoice_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    product_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 'rate' | 'qty' | 'rate+qty' | 'added' | 'removed' | 'total'
    change_type: Mapped[str] = mapped_column(String(12), nullable=False)
    old_qty: Mapped[Optional[float]] = mapped_column(Numeric(14, 3), nullable=True)
    new_qty: Mapped[Optional[float]] = mapped_column(Numeric(14, 3), nullable=True)
    old_rate: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), nullable=True)
    new_rate: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), nullable=True)
    old_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    new_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    # new_amount - old_amount; negative = the bill went DOWN (the flagged direction)
    impact: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class VasyLineSnapshot(Base):
    """Per-voucher, per-product state as of the last sync (aggregated if a
    product appears on several lines). Rewritten each sync after diffing."""
    __tablename__ = "vasy_line_snapshot"
    __table_args__ = (UniqueConstraint("voucher_no", "product_key", name="uq_vasy_line_snapshot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    voucher_no: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    product_key: Mapped[str] = mapped_column(String(120), nullable=False)
    product_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    qty: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
