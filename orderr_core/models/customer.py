from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric
)

from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey

from orderr_core.database import Base


class Customer(Base):

    __tablename__ = "customers"

    id = Column(
        Integer,
        primary_key=True,
        index=True
    )

    restaurant_name = Column(
        String,
        nullable=True
    )

    owner_name = Column(
        String,
        nullable=True
    )

    # Nullable: customers imported from the outstanding sheet may have no phone
    # number on record. Such customers are flagged RED on the dashboard. The
    # unique constraint still holds for non-NULL values (SQLite & Postgres both
    # allow multiple NULLs in a UNIQUE column).
    phone_number = Column(
        String,
        unique=True,
        index=True,
        nullable=True
    )

    address = Column(
        String,
        nullable=True
    )

    city = Column(
        String,
        nullable=True
    )

    onboarding_status = Column(
        String,
        default="awaiting_name"
    )

    is_active = Column(
        Boolean,
        default=True
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    # ── New fields for salesperson / pending order feature ──────────────

    area = Column(
        String,
        nullable=True
    )

    salesperson_id = Column(
        Integer,
        ForeignKey("salespersons.id"),
        nullable=True,
        index=True
    )

    # True  → expect a daily order; include in pending checks
    # False → irregular customer; never chase for missing orders
    is_daily_order_customer = Column(
        Boolean,
        default=True
    )

    ledger_token = Column(
    String,
    unique=True,
    nullable=True,
    index=True,
    )

    # Current receivables balance (₹) — a snapshot imported from the customer
    # outstanding sheet. NOT recalculated from orders/payments; refreshed only
    # when a newer outstanding file is uploaded. Used as the invoice "Due Amount"
    # base (outstanding + current invoice total).
    outstanding = Column(
        Numeric(12, 2),
        nullable=False,
        default=0,
        server_default="0",
    )

    # Credit limit (₹) for this customer — owner-set ceiling on receivables
    # exposure. NULL = no limit set. Used by the Phase-3 credit-risk / breach
    # alert (exposure crosses limit) — see analytics credit intelligence.
    credit_limit = Column(
        Numeric(12, 2),
        nullable=True,
    )

    # Starred on the dashboard Customers tab — favourites sort to the top.
    # Shared across everyone using the dashboard (stored here, not per browser).
    is_favorite = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # Relationship
    salesperson = relationship("Salesperson", back_populates="customers")

