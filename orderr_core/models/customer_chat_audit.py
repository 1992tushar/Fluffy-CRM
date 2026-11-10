"""
customer_chat_audits — one row per WhatsApp-chat audit run for a customer.

The Customer Edit tab lets the owner upload a WhatsApp chat export (individual
or group) for one customer and checks it against what was punched into Vasy
(sales lines + receipts). Each run records `audited_through` — the last
*complete* chat day covered — so the next run for the same customer only looks
at messages AFTER that date. `result_json` keeps the full findings so an old
run can be reopened from the history list.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Text, Date, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class CustomerChatAudit(Base):
    __tablename__ = "customer_chat_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=False, index=True
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    file_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    our_names: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # comma list of our-side senders
    from_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)   # first chat day considered
    to_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)     # last chat day considered
    # Watermark: next run only considers messages dated AFTER this day.
    audited_through: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    messages_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issues_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
