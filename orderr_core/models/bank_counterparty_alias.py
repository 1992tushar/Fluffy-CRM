from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, UniqueConstraint
from orderr_core.database import Base

from orderr_core.constants import IST


class BankCounterpartyAlias(Base):
    """Maps a normalized bank-narration counterparty → a category + remark, so
    tagging a bank transaction once teaches the reconciliation screen to
    auto-fill every future transaction from the same counterparty.

    Mirrors CustomerAlias's alias_key → canonical-target pattern. Two
    `alias_type`s:
      - "direct": safe to auto-apply (category, remark_template) on sight —
        a stable counterparty that always means the same thing (a vendor, a
        regular customer, the PhonePe settlement aggregator).
      - "relay": NEVER auto-apply. Some accounts (the owner's own UPI, a
        manager's, a salesperson collecting on the company's behalf) forward
        money that belongs to a different, varying real party each time —
        the counterparty name is stable but what it MEANS isn't. These always
        route to manual review; only a memo-text hint (when the account is
        known to carry one, e.g. the salesperson types the real customer name
        when forwarding) is offered as a suggestion, never applied silently.
    """
    __tablename__ = "bank_counterparty_aliases"

    id              = Column(Integer, primary_key=True, index=True)
    alias_key       = Column(String, nullable=False, unique=True, index=True)  # normalize_name(counterparty)
    category        = Column(String, nullable=True)      # one of bank_categorize.CATEGORY_KEYS; null for relay
    remark_template = Column(String, nullable=True)
    alias_type      = Column(String, nullable=False, default="direct")  # "direct" | "relay"
    source          = Column(String, nullable=True)       # e.g. "bankrecon-manual", "bankrecon-seed"
    created_at      = Column(DateTime, default=lambda: datetime.now(IST))
    updated_at      = Column(DateTime, default=lambda: datetime.now(IST), onupdate=lambda: datetime.now(IST))

    __table_args__ = (
        UniqueConstraint("alias_key", name="uq_bank_counterparty_alias_key"),
    )

    def __repr__(self):
        return f"<BankCounterpartyAlias '{self.alias_key}' → {self.category or self.alias_type}>"
