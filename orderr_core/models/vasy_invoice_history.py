"""
vasy_invoice_history — append-only audit trail of every sales invoice Vasy has
ever reported, independent of the current VasyInvoice mirror.

import_sales_items() snapshot-replaces VasyInvoice on every sync (the Vasy
export carries the full FY each run), so a voucher deleted in Vasy between two
syncs used to vanish with no trace. This table never gets wiped: each import
upserts every voucher it sees (first_seen_at / last_seen_at) and, for a
voucher present in the previous sync but absent from this one, stamps
disappeared_at — the fraud-detection signal (an invoice raised then quietly
deleted, e.g. to collect the amount off-books). If a "disappeared" voucher
reappears in a later sync, disappeared_at is cleared.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Numeric, Date, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class VasyInvoiceHistory(Base):
    __tablename__ = "vasy_invoice_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    voucher_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    party_name: Mapped[str] = mapped_column(String, nullable=False)
    party_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    customer_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=True, index=True
    )
    invoice_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    disappeared_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
