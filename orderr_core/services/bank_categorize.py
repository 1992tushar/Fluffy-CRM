"""
Bank-transaction reconciliation / categorization ("bank-recon" screen).

Design (from the owner's actual July statement, 626 rows, cross-checked
against the reconciled sheet the owner sends the CA):

Three separate matching mechanisms feed the same category list — the
category itself never depends on HOW it was resolved:

  1. Counterparty-alias lookup (~24% of rows) — a stable counterparty always
     means the same thing (ice vendor, a regular customer, the PhonePe
     settlement aggregator). Tag once, auto-fills forever after.
  2. Narration-keyword rules (~7%) — fuel/CNG purchases: the vendor changes
     almost every time (whichever pump is nearest), but the narration itself
     usually names the fuel, so category can still be inferred even though
     the counterparty can't be aliased.
  3. Everything else stays manual — most importantly the Kunal/Mayur Hotel
     walk-in-scanner payments (~36% of rows: a different payer name every
     time, same remark), which are decided over WhatsApp/screenshot before
     the row is ever seen here, and relay/internal-person accounts (owner,
     managers, the salesperson), which must NEVER auto-apply because the
     same counterparty means something different every transaction.

See memory/bank-reconciliation-infra.md (design doc) for the full rationale.
"""
import re
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from orderr_core.constants import IST
from orderr_core.models.bank_counterparty_alias import BankCounterpartyAlias
from orderr_core.models.bank_transaction import BankTransaction
from orderr_core.services.vasy_import import normalize_name

# ── category taxonomy ───────────────────────────────────────────────────────
# (key, label, group) — group is "in" | "out" | "nonpl" (balance-sheet, no P&L
# impact) purely for grouping the dropdown; doesn't affect matching.
CATEGORIES = [
    ("customer_payment",      "Customer Payment",                      "in"),
    ("cash_sales_deposit",    "Cash Sales Deposit",                    "in"),
    ("customer_advance",      "Customer Advance",                      "in"),
    ("refund_received",       "Refund / Reversal Received",            "in"),
    ("purchase_raw_material", "Purchase — Raw Material/Trade Goods",   "out"),
    ("ice_purchase",          "Ice Purchase",                          "out"),
    ("fuel_vehicle",          "Fuel & Vehicle",                        "out"),
    ("packaging_consumables", "Packaging & Consumables",               "out"),
    ("employee_salary",       "Employee Salary",                      "out"),
    ("employee_advance",      "Employee Advance",                     "out"),
    ("employee_reimbursement","Employee Reimbursement/Misc",          "out"),
    ("rent",                  "Rent",                                  "out"),
    ("utilities",             "Utilities",                            "out"),
    ("loan_emi",              "Loan/EMI Repayment",                    "out"),
    ("statutory_licence",     "Statutory/Licence",                     "out"),
    ("repairs_maintenance",   "Repairs & Maintenance",                 "out"),
    ("commission",            "Commission",                           "out"),
    ("credit_card_bill",      "Credit Card Bill",                     "out"),
    ("investment_savings",    "Investment/Savings",                   "nonpl"),
    ("internal_transfer",     "Internal Transfer",                    "nonpl"),
    ("other",                 "Other / Uncategorized",                "other"),
]
CATEGORY_KEYS = {k for k, _, _ in CATEGORIES}
CATEGORY_LABEL = {k: label for k, label, _ in CATEGORIES}

# Hotels whose own retail customers pay via Fluffy's scanner/QR — confirmed
# manual-only (WhatsApp screenshot + amount cross-check happens before the
# row is ever seen here). Pinned as one-click quick-picks, not matched.
QUICK_PICK_HOTELS = ["Kunal Hotel", "Mayur Hotel"]

# Narration keywords → category, for the fuel/vehicle case where the vendor
# changes almost every time but the narration names the fuel/consumable.
_KEYWORD_RULES = [
    (("diesel",), "fuel_vehicle"),
    (("petrol", "petro"), "fuel_vehicle"),
    (("cng",), "fuel_vehicle"),
    (("tyre", "tayer", "puncture"), "fuel_vehicle"),
]


def _norm(s: Optional[str]) -> str:
    return normalize_name(s) if s else ""


def parse_counterparty(description: Optional[str]) -> dict:
    """Split a raw bank narration into (txn_type, counterparty_raw, memo).

    Heuristic, not exhaustive — good enough to drive alias matching; a miss
    just means that row falls through to manual review, never a wrong guess.
    """
    d = (description or "").strip()
    if not d:
        return {"txn_type": "OTHER", "counterparty_raw": None, "memo": None}

    if d.upper().startswith("UPI/") or d.upper().startswith("REV-UPI/"):
        prefix_len = len("REV-UPI/") if d.upper().startswith("REV-UPI/") else len("UPI/")
        tokens = d[prefix_len:].split("/")
        name = tokens[0].strip() if tokens else None
        memo = None
        # Walk past bank-code tokens (letters) to the first purely-numeric
        # token (the UPI ref); anything after that is the free-text memo.
        rest = tokens[1:]
        ref_idx = next((i for i, t in enumerate(rest) if t.strip().isdigit()), None)
        if ref_idx is not None and ref_idx + 1 < len(rest):
            candidate = "/".join(rest[ref_idx + 1:]).strip()
            if candidate and candidate.upper() != "NA":
                memo = candidate
        return {"txn_type": "REVERSAL" if "REV-UPI" in d.upper() else "UPI",
                "counterparty_raw": name, "memo": memo}

    if "PHONEPE LIMITED PAYMENT AGG" in d.upper():
        return {"txn_type": "NEFT", "counterparty_raw": "PHONEPE LIMITED PAYMENT AGG", "memo": None}

    if d.upper().startswith("NEFT"):
        m = re.match(r"NEFT\s+\S+\s+(.+?)\s+\S+$", d, re.IGNORECASE)
        name = m.group(1).strip() if m else None
        return {"txn_type": "NEFT", "counterparty_raw": name, "memo": None}

    if d.upper().startswith("SENTIMPS") or "RECD:IMPS" in d.upper():
        m = re.search(r"IMPS\d*[:/]?([A-Za-z][A-Za-z .]{2,})", d)
        name = m.group(1).strip() if m else None
        return {"txn_type": "IMPS", "counterparty_raw": name, "memo": None}

    if "BY CLG INST" in d.upper() or d.upper().startswith("CLG TO AC") or "O/W RTN" in d.upper():
        # Cheque clearing/return: bank narration carries no party name at all
        # (just an instrument number) — always manual, confirmed against a
        # full month's data (no name token ever appears here).
        return {"txn_type": "CLG", "counterparty_raw": None, "memo": None}

    if d.upper().startswith("PG "):
        return {"txn_type": "CARD", "counterparty_raw": "CREDIT CARD SETTLEMENT", "memo": None}

    if "CASH DEPOSIT BY" in d.upper():
        return {"txn_type": "CASH_DEPOSIT", "counterparty_raw": "CASH DEPOSIT", "memo": None}

    if d.upper().startswith("MB:") or d.upper().startswith("AP:") or "BILLPAY" in d.upper():
        m = re.search(r"FOR\s+([A-Z]+)", d, re.IGNORECASE)
        name = m.group(1).strip() if m else None
        return {"txn_type": "BILLPAY", "counterparty_raw": name, "memo": None}

    return {"txn_type": "OTHER", "counterparty_raw": d[:40].strip() or None, "memo": None}


def _keyword_category(description: Optional[str]) -> Optional[str]:
    low = (description or "").lower()
    for keywords, cat in _KEYWORD_RULES:
        if any(kw in low for kw in keywords):
            return cat
    return None


def suggest_category(db: Session, txn: BankTransaction) -> dict:
    """Best-effort suggestion for one transaction. Never writes to the DB —
    the caller decides whether/when to persist (only on explicit save), so a
    suggestion is always a proposal the user can see and override, not a
    silent auto-tag."""
    parsed = parse_counterparty(txn.description)
    key = _norm(parsed["counterparty_raw"])

    if key:
        alias = db.query(BankCounterpartyAlias).filter_by(alias_key=key).first()
        if alias:
            if alias.alias_type == "relay":
                return {
                    "category": None, "remark": parsed["memo"], "confidence": "none",
                    "source": "relay", "is_relay": True,
                    "txn_type": parsed["txn_type"], "counterparty_raw": parsed["counterparty_raw"],
                    "counterparty_key": key,
                    "party_type": None, "party_id": None, "party_label": None,
                }
            return {
                "category": alias.category, "remark": alias.remark_template, "confidence": "high",
                "source": "alias", "is_relay": False,
                "txn_type": parsed["txn_type"], "counterparty_raw": parsed["counterparty_raw"],
                "counterparty_key": key,
                "party_type": alias.party_type, "party_id": alias.party_id, "party_label": alias.party_label,
            }

    kw_cat = _keyword_category(txn.description)
    if kw_cat:
        return {
            "category": kw_cat, "remark": None, "confidence": "medium", "source": "keyword",
            "is_relay": False, "txn_type": parsed["txn_type"],
            "counterparty_raw": parsed["counterparty_raw"], "counterparty_key": key or None,
            "party_type": None, "party_id": None, "party_label": None,
        }

    return {
        "category": None, "remark": None, "confidence": "none", "source": "manual",
        "is_relay": False, "txn_type": parsed["txn_type"],
        "counterparty_raw": parsed["counterparty_raw"], "counterparty_key": key or None,
        "party_type": None, "party_id": None, "party_label": None,
    }


def apply_categorization(
    db: Session, txn_ids: list, category: str, remark: Optional[str], reviewed_by: str,
    save_alias: bool = False, alias_type: str = "direct",
    party_type: Optional[str] = None, party_id: Optional[int] = None, party_label: Optional[str] = None,
) -> int:
    """Persist a category+remark(+party link) to one or more transactions, and
    optionally teach the alias table so the same counterparty auto-fills next
    time — including which ledger party it belongs to."""
    if category and category not in CATEGORY_KEYS:
        raise ValueError(f"Unknown category '{category}'")
    if party_type and party_type not in ("customer", "employee"):
        raise ValueError(f"Unknown party_type '{party_type}'")
    now = datetime.now(IST)
    rows = db.query(BankTransaction).filter(BankTransaction.id.in_(txn_ids)).all()
    if not rows:
        return 0
    for row in rows:
        row.category = category or None
        row.remark = remark or None
        row.party_type = party_type or None
        row.party_id = party_id or None
        row.party_label = party_label or None
        row.reviewed = True
        row.reviewed_by = reviewed_by
        row.reviewed_at = now
        parsed = parse_counterparty(row.description)
        row.txn_type = parsed["txn_type"]
        row.counterparty_raw = parsed["counterparty_raw"]
        row.counterparty_key = _norm(parsed["counterparty_raw"]) or None

    if save_alias:
        # Always derive the alias key from the row's own freshly-parsed
        # narration (set just above) — the UI's "apply to matching rows"
        # selection is a client-side-only concept and must never decide
        # where the alias actually gets written, or a stale/mismatched key
        # would silently save the alias somewhere suggest_category() can
        # never find it again.
        key = rows[0].counterparty_key if rows else None
        if key:
            alias = db.query(BankCounterpartyAlias).filter_by(alias_key=key).first()
            if not alias:
                alias = BankCounterpartyAlias(alias_key=key, source="bankrecon-manual")
                db.add(alias)
            alias.category = category if alias_type == "direct" else None
            alias.remark_template = remark if alias_type == "direct" else None
            alias.party_type = party_type if alias_type == "direct" else None
            alias.party_id = party_id if alias_type == "direct" else None
            alias.party_label = party_label if alias_type == "direct" else None
            alias.alias_type = alias_type
            alias.updated_at = now

    db.commit()
    return len(rows)


# ── seed: aliases already confirmed against the owner's actual July data ───
_SEED_DIRECT = [
    # (counterparty_raw as it appears in narration, category, remark_template)
    ("KOTULKAR YOGESH", "ice_purchase", "Ice"),
    ("M S SARSENAPATI", "investment_savings", "Recurring deposit (returns in ~6 months)"),
    ("INAMDAR SHABBI", "customer_payment", "Shabir K Inamdar"),
    ("YEWALE TRA", "purchase_raw_material", "Purchase — Yewale Traders"),
    ("SAYYED TRA", "purchase_raw_material", "Purchase — Sayyed Traders"),
    ("PHONEPE LIMITED PAYMENT AGG", "customer_payment", "Plant Phonepay Scanner"),
    ("BALAJI ENTERPRI", "fuel_vehicle", "CNG"),
    ("DESALE SANDEEP", "employee_reimbursement", "Bharai / driver food"),
    ("CASH DEPOSIT", "cash_sales_deposit", "Cash deposit"),
    ("CREDIT CARD SETTLEMENT", "credit_card_bill", "Credit card bill"),
]
_SEED_RELAY = [
    "TUSHAR TANAJI",
    "SANDEEP TANAJI",
    "tushar tan",       # IMPS narration truncates/lowercases differently
    "RAUNDHALS REST",
]


def seed_known_aliases(db: Session) -> int:
    """Idempotent: only inserts aliases that don't already exist, so it never
    clobbers something the owner has since edited by hand."""
    created = 0
    for raw, category, remark in _SEED_DIRECT:
        key = _norm(raw)
        if db.query(BankCounterpartyAlias).filter_by(alias_key=key).first():
            continue
        db.add(BankCounterpartyAlias(
            alias_key=key, category=category, remark_template=remark,
            alias_type="direct", source="bankrecon-seed",
        ))
        created += 1
    for raw in _SEED_RELAY:
        key = _norm(raw)
        if db.query(BankCounterpartyAlias).filter_by(alias_key=key).first():
            continue
        db.add(BankCounterpartyAlias(
            alias_key=key, category=None, remark_template=None,
            alias_type="relay", source="bankrecon-seed",
        ))
        created += 1
    if created:
        db.commit()
    return created
