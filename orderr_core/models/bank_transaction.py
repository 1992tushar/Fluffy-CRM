"""
bank_transactions — mirror of the company's bank statement (money that actually
moved through the account), uploaded per period. Used by the 5-Day Close bank
reconciliation: bank in/out vs what Vasy recorded (receipts / payments).

The bank is the one source that can't be fudged, so this is the check that
catches money that left/entered the account but was never recorded in Vasy
(bank charges, missed entries), and timing gaps.

Idempotent on `dedupe_key` (value_date + ref + amount + direction), so
re-uploading an overlapping statement updates rather than duplicates.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Integer, String, Numeric, Date, DateTime, Text, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column

from orderr_core.database import Base


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    value_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    txn_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ref_no: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)  # always positive
    direction: Mapped[str] = mapped_column(String(2), nullable=False)  # 'cr' (in) | 'dr' (out)
    balance: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    source_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Daily reconciliation / categorization (bank-recon screen). txn_type is the
    # narration shape (UPI/NEFT/IMPS/CLG/...); counterparty_raw/counterparty_key
    # are the parsed-and-normalized payer/payee name used for alias lookups.
    txn_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    counterparty_raw: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    counterparty_key: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Which ledger party this transaction belongs to — e.g. the actual Customer
    # record for "NIRRVANNA RESTAURANT", not just the category. party_label is
    # denormalized (cached at tag time) so the screen never needs a join.
    party_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)   # "customer" | "employee"
    party_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    party_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<BankTxn {self.value_date} {self.direction} {self.amount}>"
