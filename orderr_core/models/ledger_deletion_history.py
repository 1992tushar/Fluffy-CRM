"""
ledger_deletion_history — append-only audit trail of every row silently
dropped by `_purge_stale_ledger_rows()` (vasy_import.py), i.e. every receipt,
sales invoice, purchase bill, expense or payment that existed in a prior Vasy
sync and vanished from a later one (deleted in Vasy after the fact).

Unlike vasy_invoice_history (which snapshot-tracks invoices across the
wipe-and-rebuild sales-item sync and adds a receipt-matching fraud check),
this table is a flat, no-heuristic log: one row per deletion, across every
ledger type that shares the purge-on-reimport path. It exists purely so a
deletion is never silent — the accountant reviews the list and judges each
entry themselves rather than the system guessing "suspicious" vs "benign".
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Numeric, Date, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class LedgerDeletionHistory(Base):
    __tablename__ = "ledger_deletion_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    entity_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    party_name: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    entry_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    source_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
