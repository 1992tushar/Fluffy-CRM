"""
app/services/unit_inference.py

Smart unit inference for bare-number order quantities.

Implements FRD §4 (decision logic) and §5.3 (stats write path).

Hot-path contract (FRD §6 PERF-1):
  infer_unit() performs AT MOST one indexed DB read per call.
  It never scans order history.

Write-path contract (FRD §5.3):
  record_confirmed_qty() is called ONLY for confirmed quantities
  (explicit unit, or already confidently auto-resolved).
  Quantities still pending manual review must NOT update stats.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from orderr_core.models.customer_product_stats import CustomerProductStats

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# Minimum number of confirmed orders before a customer's own history is
# trusted over the global fallback band.  Tunable post-launch (FRD §8.2).
MIN_SAMPLES: int = 3

# A candidate is "comfortably within" a range when it sits inside the range
# AND is at least this many times more plausible under that unit than the
# other.  Using a ratio test rather than a fixed absolute margin so it scales
# with order size (FRD §8.2 open question on buffer-zone width).
PLAUSIBILITY_RATIO: float = 5.0

# A confirmed value this many times above/below the running average is
# treated as a likely data-entry mistake (manager misclick on kg/g toggle,
# customer typo, etc.) once there is enough history to trust the average.
# It still counts toward order_count/avg, but is NOT allowed to redefine
# min/max — otherwise a single bad value can permanently corrupt the band
# and make every future bare-number order "ambiguous" forever.
OUTLIER_GUARD_RATIO: float = 10.0

# ── Global fallback bands (FRD §4.3) ─────────────────────────────────────────
# Keyed by canonical product display name (matches PRODUCT_DEFINITIONS).
# Values are (min_kg, max_kg) inclusive.
#
# Products NOT in this dict are "Unclassified / new SKU" — always route to
# review until either a global band is added here or the customer accumulates
# MIN_SAMPLES of their own history.
#
# ACTION REQUIRED before launch: engineering + Fluffy ops to jointly review
# and finalize this table for every SKU in PRODUCT_DEFINITIONS (FRD §4.3).

GLOBAL_FALLBACK_BANDS: dict[str, tuple[float, float]] = {
    # ── Bulk cuts (1–50 kg) ───────────────────────────────────────────────
    "Curry Cut":             (1.0,  50.0),
    "Biryani Cut":           (1.0,  50.0),
    "WS Regular Chicken":    (1.0,  50.0),
    "W/O Skin Regular Chicken": (1.0, 50.0),
    "Breast Boneless":       (1.0,  50.0),
    "Leg Boneless":          (1.0,  50.0),
    "Thai Boneless":         (1.0,  50.0),
    "Supreme Boneless":      (1.0,  50.0),
    "Wings":                 (1.0,  50.0),
    "Drumstick":             (1.0,  50.0),
    "Whole Leg":             (1.0,  50.0),
    "WS Tandoor Chicken":    (1.0,  50.0),
    "W/O Skin Tandoor Big Chicken": (1.0, 50.0),
    "Carcass":               (1.0,  50.0),

    # ── Offal / byproducts / small-portion (0.25–8 kg) ────────────────────
    # Bare numbers >= ~100 are far more likely grams than kg at small-
    # restaurant scale.
    "Liver":                 (0.25, 8.0),
    "Gizzard":               (0.25, 8.0),
    "Kheema":                (0.25, 8.0),
    "Chicken Neck":          (0.25, 8.0),
    "Chicken Skin":          (0.25, 8.0),
    "Chicken Feet":          (0.25, 8.0),
    "Chicken Mundi":         (0.25, 8.0),

    # ── Counted items (nos) ───────────────────────────────────────────────
    # These are sold by piece, not weight — the kg/grams ambiguity does not
    # apply.  Omitted from this dict intentionally; see _is_kg_product().
    # "W/O Skin Tandoor Chicken": N/A (nos)  ← the ONLY nos product
}
# Canonical definition lives in orderr_core.constants; re-exported here for
# the existing `from unit_inference import UNIT_AMBIGUOUS_MARKER` importers.
from orderr_core.constants import UNIT_AMBIGUOUS_MARKER

# ── Result type ───────────────────────────────────────────────────────────────

class InferenceResult:
    """
    Outcome of a unit-inference check.

    outcome: one of "explicit", "confident", "ambiguous", "no_signal"
      - "explicit"  : caller already had a unit; inference not invoked.
      - "confident" : bare number confidently resolved to `unit`.
      - "ambiguous" : needs human review (unclear item).
      - "no_signal" : no band and no history; route to review.

    unit: resolved unit string when outcome is "confident", else None.
    source: "customer_history" | "global_fallback" | None
    """

    __slots__ = ("outcome", "unit", "source")

    def __init__(self, outcome: str, unit: Optional[str] = None, source: Optional[str] = None):
        self.outcome = outcome
        self.unit    = unit
        self.source  = source

    @property
    def needs_review(self) -> bool:
        return self.outcome in ("ambiguous", "no_signal")

    @property
    def is_confident(self) -> bool:
        return self.outcome == "confident"

    def __repr__(self) -> str:
        return f"<InferenceResult outcome={self.outcome!r} unit={self.unit!r} source={self.source!r}>"


# ── Public API ────────────────────────────────────────────────────────────────

def infer_unit(
    product: str,
    raw_number: float,
    customer_phone: str,
    default_unit: str,
    db: Optional[Session],
) -> InferenceResult:
    """
    Determine the correct unit for a bare-number quantity (FRD §4.2).

    Args:
        product:        Canonical product display name.
        raw_number:     The bare numeric value parsed from the customer's text.
        customer_phone: Normalized phone number of the ordering customer.
        default_unit:   The product's nominal unit from PRODUCT_DEFINITIONS.
        db:             SQLAlchemy session.  May be None (falls back to global
                        band only, which is acceptable for tests / dry-runs).

    Returns:
        InferenceResult — see class docstring.

    Hot-path guarantee: at most ONE indexed DB read (the stats row lookup).
    """
    # Only kg products have a kg-vs-grams ambiguity.
    if not _is_kg_product(default_unit):
        return InferenceResult("confident", unit=default_unit, source="explicit_unit_type")

    # ── Step 1: try customer history ──────────────────────────────────────
    if db is not None:
        stats = _get_stats(customer_phone, product, db)
        if stats is not None and stats.order_count >= MIN_SAMPLES:
            result = _check_against_band(
                raw_number,
                stats.min_qty_kg,
                stats.max_qty_kg,
                source="customer_history",
            )
            logger.debug(
                "unit_inference customer_history product=%r number=%s phone=%s → %r",
                product, raw_number, customer_phone, result,
            )
            return result

    # ── Step 2: fall back to global band ─────────────────────────────────
    band = GLOBAL_FALLBACK_BANDS.get(product)
    if band is not None:
        result = _check_against_band(
            raw_number,
            band[0],
            band[1],
            source="global_fallback",
        )
        logger.debug(
            "unit_inference global_fallback product=%r number=%s band=%s → %r",
            product, raw_number, band, result,
        )
        return result

    # ── Step 3: no signal at all ─────────────────────────────────────────
    logger.debug(
        "unit_inference no_signal product=%r number=%s — no band, insufficient history",
        product, raw_number,
    )
    return InferenceResult("no_signal")


def record_confirmed_qty(
    product: str,
    customer_phone: str,
    qty_kg: float,
    db: Session,
) -> None:
    """
    Update the customer_product_stats row for a CONFIRMED quantity (FRD §5.3).

    Must only be called for quantities that are:
      - explicitly stated by the customer, OR
      - confidently auto-resolved by infer_unit().

    Quantities still pending manual review must NOT call this function —
    doing so would corrupt the training signal with a potentially wrong value.

    This is a single upsert; cost is flat regardless of order history size.
    """
    if qty_kg <= 0:
        return

    try:
        stats = _get_stats(customer_phone, product, db)

        if stats is None:
            stats = CustomerProductStats(
                customer_phone=customer_phone,
                product=product,
                order_count=0,
                avg_qty_kg=None,
                min_qty_kg=None,
                max_qty_kg=None,
            )
            db.add(stats)

        new_count       = (stats.order_count or 0) + 1
        old_avg         = stats.avg_qty_kg or qty_kg
        new_avg         = old_avg + (qty_kg - old_avg) / new_count

        stats.order_count = new_count
        stats.avg_qty_kg  = new_avg

        # Guard against a single bad confirmation (manager kg/g misclick,
        # customer typo) permanently collapsing/exploding the band. Once
        # there's enough history to trust the average, only let min/max
        # widen for values within OUTLIER_GUARD_RATIO of it. The value still
        # updates avg/order_count above, but won't redefine the band.
        is_outlier = (
            (stats.order_count or 0) >= MIN_SAMPLES
            and old_avg > 0
            and (qty_kg < old_avg / OUTLIER_GUARD_RATIO or qty_kg > old_avg * OUTLIER_GUARD_RATIO)
        )

        if is_outlier:
            logger.warning(
                "unit_inference outlier_guard phone=%r product=%r qty_kg=%s avg=%s — "
                "not widening min/max band (likely data-entry mistake)",
                customer_phone, product, qty_kg, old_avg,
            )
        else:
            stats.min_qty_kg = min(stats.min_qty_kg, qty_kg) if stats.min_qty_kg is not None else qty_kg
            stats.max_qty_kg = max(stats.max_qty_kg, qty_kg) if stats.max_qty_kg is not None else qty_kg

        # Caller (order_service / admin resolver) is responsible for db.commit()
        logger.debug(
            "unit_inference stats_updated phone=%r product=%r qty_kg=%s n=%s range=[%s, %s]",
            customer_phone, product, qty_kg, new_count, stats.min_qty_kg, stats.max_qty_kg,
        )

    except Exception:
        logger.exception(
            "unit_inference stats write failed phone=%r product=%r qty_kg=%s — swallowing to protect order flow",
            customer_phone, product, qty_kg,
        )


def update_stats(
    product: str,
    qty_kg: float,
    customer_phone: str,
    db: Session,
) -> None:
    """
    Thin alias for record_confirmed_qty() with the (product, qty_kg,
    customer_phone, db) argument order expected by callers/tests that use
    this name. record_confirmed_qty() itself is unmodified.
    """
    record_confirmed_qty(product=product, customer_phone=customer_phone, qty_kg=qty_kg, db=db)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_kg_product(default_unit: str) -> bool:
    """Only kg-unit products have a kg-vs-grams ambiguity."""
    return default_unit.lower() == "kg"


def _get_stats(
    customer_phone: str,
    product: str,
    db: Session,
) -> Optional[CustomerProductStats]:
    """Single indexed read — the entire hot-path cost (FRD §5.4)."""
    return (
        db.query(CustomerProductStats)
        .filter(
            CustomerProductStats.customer_phone == customer_phone,
            CustomerProductStats.product == product,
        )
        .first()
    )


def _check_against_band(
    raw_number: float,
    min_kg: float,
    max_kg: float,
    source: str,
) -> InferenceResult:
    """
    Compare raw_number against a [min_kg, max_kg] band.

    Also checks raw_number / 1000 (treating raw_number as grams).

    IMPORTANT: a value that doesn't literally fall *inside* [min_kg, max_kg]
    on either interpretation is NOT automatically ambiguous. A customer's
    biggest-ever order (above max) or smallest-ever order (below min) is
    completely normal and should still resolve confidently as kg if it's
    overwhelmingly more plausible than the grams interpretation. We always
    score *how implausible* each interpretation is relative to the band and
    pick the dominant one, rather than giving up the moment a number sits
    outside the band on both sides.

    Decision rules (FRD §4.2 step 3, generalized):
      - One interpretation is comfortably inside (or only mildly outside)
        the band, and the other is wildly implausible by at least
        PLAUSIBILITY_RATIO× → confident in the plausible one.
      - Both interpretations are equally (im)plausible, or neither clears
        the ratio bar → ambiguous.
    """
    kg_value    = raw_number
    grams_as_kg = raw_number / 1000.0

    # 0.0 == comfortably inside the band; >1 == how many multiples outside.
    implaus_kg    = _distance_ratio(kg_value, min_kg, max_kg)
    implaus_grams = _distance_ratio(grams_as_kg, min_kg, max_kg)

    # Both interpretations fit cleanly inside the band — genuinely ambiguous
    # (e.g. band is [1, 50] and raw_number is 5: both 5kg and "5" read as
    # grams-of-something could theoretically fit a wide enough band).
    if implaus_kg == 0.0 and implaus_grams == 0.0:
        return InferenceResult("ambiguous")

    # Use a tiny epsilon instead of 0 for the "inside the band" case so the
    # ratio comparison below works uniformly whether a value is inside,
    # mildly outside, or wildly outside the band.
    _EPS = 1e-6
    kg_score    = implaus_kg if implaus_kg > 0 else _EPS
    grams_score = implaus_grams if implaus_grams > 0 else _EPS

    if grams_score / kg_score >= PLAUSIBILITY_RATIO:
        return InferenceResult("confident", unit="kg", source=source)

    if kg_score / grams_score >= PLAUSIBILITY_RATIO:
        return InferenceResult("confident", unit="g", source=source)

    return InferenceResult("ambiguous")



def _distance_ratio(value: float, min_kg: float, max_kg: float) -> float:
    """
    How far outside [min_kg, max_kg] is `value`, expressed as a ratio to
    the nearest boundary?

    Returns a large number when value is clearly outside the band, and < 1
    when value is inside.  Used as a plausibility ratio.

    Examples:
      value=0.005, band=[1, 50] → nearest boundary is 1.0 → ratio = 1.0/0.005 = 200
      value=0.8,   band=[1, 50] → nearest boundary is 1.0 → ratio = 1.0/0.8   = 1.25
    """
    if min_kg <= value <= max_kg:
        # Inside the band — "other" interpretation is plausible too
        return 0.0
    if value < min_kg:
        return min_kg / value if value > 0 else float("inf")
    # value > max_kg
    return value / max_kg
