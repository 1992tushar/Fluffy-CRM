"""
critical_notes — the don't-forget ledger (Registers & Reminders spec §3):
one-off facts with money or consequences attached (e.g. "01 Apr: salesperson
took ₹20,000 from a customer, said he'd return it").

A note is a MEMORY WITH A NAG, never a ledger entry — it holds no balance and
touches no AR/analytics number; actual money corrections happen in Vasy. Open
notes nag (dashboard strip, digest, WhatsApp) until a human resolves or drops
them, and closing requires a resolution note (the audit trail).
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Numeric, Date, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class CriticalNote(Base):
    __tablename__ = "critical_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    note: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    customer_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=True, index=True
    )
    person: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # salesperson/employee — free text
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    follow_up_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="normal")  # normal | high
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="open",
                                        index=True)  # open | resolved | dropped
    resolution_note: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Optional supporting attachment (photo/PDF), stored in the same dedicated
    # Drive account as sundries invoices — only the pointer lives here.
    attachment_drive_file_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    attachment_drive_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    attachment_filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
