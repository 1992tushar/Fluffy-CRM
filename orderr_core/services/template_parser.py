"""
template_parser.py
------------------
Fuzzy template parser — accepts messy human input like:
  "wings - 2"  /  "Wings-2kg"  /  "breast boneless- 5 kg"
  "CC 3"  /  "lollipop-10 nos"  /  "td 5"  /  "kaleji 1kg"

Full Fluffy product catalog with Pune restaurant/hotel customer
aliases (English shortcodes + Hindi/Marathi slang + noisy variants).

v2 changes:
- Accepts optional `db` session to check unclear_item_aliases before flagging unclear
- Returns `unclear_items` list (raw strings that couldn't be parsed) separately from `items`
- `is_unclear` is only True when ZERO items parsed (whole order unreadable)
- Partial orders (some parsed, some not) are accepted — unclear_items stored separately

v3 changes:
- Noise filtering: skips header/footer lines like restaurant name, dates,
  "ORDER FOR THE DAY", "kg( 900 gm size)" annotations, emoji-only lines etc.

v4 changes:
- Unit inference: bare numbers on kg products are checked against customer
  history and global fallback bands (FRD §4). Ambiguous quantities are
  routed to the Unclear tab with a __qty_ambiguous__ sentinel instead of
  silently defaulting to kg.
"""

import re
from orderr_core.utils import fmt_qty
import os
from orderr_core.models.customer_product_alias import CustomerProductAlias
from orderr_core.models.unclear_item_alias import UnclearItemAlias
from orderr_core.models.noise_phrase import NoisePhrase

# Sentinel stored in a parsed item's "unit" field when the quantity is
# ambiguous and needs manager resolution.  order_service.py and admin.py
# both check for this value to route the item to the unclear-items flow.
# Canonical definition lives in orderr_core.constants; re-exported here for
# the existing `from template_parser import UNIT_AMBIGUOUS_MARKER` importers.
from orderr_core.constants import UNIT_AMBIGUOUS_MARKER

from orderr_core.config import PLANT_NAME

# ── Multi-item single-line splitter ───────────────────────────────────────────
# Some customers send several items on ONE line/string instead of one per
# line, e.g.:
#   "Chicken 5kg boneless 3kg lolipop 2kg tandoori 3"
# Without splitting, the whole string fails the single-item regex below and
# the entire line gets dumped into unclear_items as one unresolved blob.
#
# This regex finds repeated "<name> <qty> [unit]" runs within a single line
# and breaks it into separate item-strings, each of which is then fed through
# the normal per-line parsing pipeline independently. If a line only
# contains a single such run (the normal case), it is returned unchanged so
# existing single-item parsing behaviour is unaffected.
_MULTI_ITEM_RE = re.compile(
    r"([A-Za-z\u0900-\u097F][A-Za-z\u0900-\u097F\s/\.]*?)\s*[-:]?\s*"
    r"(\d+(?:\.\d+)?)\s*"
    r"(kg|kgs|kilo|kilos|kilogram|kilograms|grams|gram|gms|gm|g|pies|nos|no|nos\.|pcs|psc|pc|pis|pieces|piece|k)?",
    re.IGNORECASE,
)


def _split_on_connectors(line: str) -> list:
    """Split a line on item connectors \u2014 commas, '+', '&', and the word 'and'
    (surrounded by spaces so 'sandwich'/'grand' are never split). Lets
    'breast 2 kg and curry 3 kg' or '2 kg wings, 3 kg breast' resolve as
    separate items. Returns [line] unchanged when no connector is present."""
    parts = re.split(r"\s*,\s*|\s*\+\s*|\s*&\s*|\s+and\s+", line, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p and p.strip()]
    return parts if len(parts) > 1 else [line]


def _split_multi_item_line(line: str) -> list:
    """Split a single line containing multiple '<name> <qty> [unit]' runs
    into separate item-strings. Returns [line] unchanged if fewer than 2
    runs are found (i.e. normal single-item line, date, phone number,
    header, delivery-time line, etc.)."""
    matches = list(_MULTI_ITEM_RE.finditer(line))
    if len(matches) < 2:
        return [line]

    segments = []
    for m in matches:
        name = m.group(1).strip()
        qty = m.group(2).strip()
        unit = (m.group(3) or "").strip()
        if not name:
            continue
        segment = f"{name} {qty}" + (f" {unit}" if unit else "")
        segments.append(segment.strip())

    return segments if segments else [line]
# ───────────────────────────────────────────────────────────────────────────

# ── Product Catalog ───────────────────────────────────────────────────────────
# Format: (Display Name, default unit, [aliases])
# All aliases must be lowercase.
# Order matters for ambiguous shortcodes — more specific products first.

PRODUCT_DEFINITIONS = [

    # ── Whole Chicken: Without Skin ───────────────────────────────────────────

    ("W/O Skin Tandoor Chicken", "nos", [
        "without skin tandoor", "without skin whole chicken tandoor",
        "wo skin tandoor", "w/o skin tandoor",
        "skinless tandoor", "skinless tandoor chicken",
        "tandoor skinless", "skin out tandoor",
        "clean tandoor", "clean small chicken", "cleaned tandoor",
        "no skin tandoor", "no skin td",
        "sl tandoor", "wos tandoor",
        "al faham", "al-faham", "alfaham",
        "skin remove tandoor",
        # NOTE: bare/generic tandoor (tandoor, tandur, tandoori, तंदूर, …) is
        # intentionally NOT listed here. It doesn't state skin, so it is routed
        # to the Unclear flow via AMBIGUOUS_SKIN_TERMS below — the manager picks
        # W/O Skin vs WS Tandoor once, and the learned alias auto-maps after.
    ]),

    ("W/O Skin Regular Chicken", "kg", [
        "without skin regular", "without skin whole chicken regular",
        "wo skin regular", "w/o skin regular",
        "whole chicken without skin", "whole chicken no skin",
        "whole chicken skin out", "whole chicken skinless",
        "skinless regular", "skinless regular chicken",
        "regular skinless", "skin out regular",
        "clean regular", "clean big chicken", "cleaned regular",
        "no skin regular", "no skin reg",
        "sl regular", "wos regular",
    ]),

   
    ("WS Regular Chicken", "kg", [
        "with skin whole chicken regular", "ws whole chicken regular",
        "with skin regular", "ws regular chicken", "ws regular",
        "skin regular",
        # NOTE: skin-ambiguous whole-chicken terms (regular, big chicken,
        # broiler/boiler, murgi, whole chicken, …) are NOT listed here. They
        # don't state skin, so they route to the Unclear flow via
        # AMBIGUOUS_SKIN_TERMS below rather than silently defaulting to WS.
    ]),

    # ── Boneless ──────────────────────────────────────────────────────────────

    ("Breast Boneless", "kg", [
        "breast boneless", "boneless breast", "breast",
        "breast piece", "breast boneless piece",
        "chest boneless", "chest bonless", "cast bonlas",
        "berst boneless", "berst",
        "bonless", "boneless",
        "bb", "cb",
        "bl breast", "breast bl", "b/l breast", "b.l breast",
        "brest", "brest boneless", "breast bnls","bonlesh", "bonles", "bonless chicken", "bonlesh chicken",
        "chicken breast", "chiken breast", "chicken brest",
    ]),

    ("Leg Boneless", "kg", [
        "leg boneless", "boneless leg",
        "thigh boneless", "thigh", "boneless thigh",
        "chicken thigh", "dark meat",
        "lag bonlas", "leg bonless", "leg bnls", "bonless leg",
        "lb",
        "bl leg", "leg bl", "b/l leg", "b.l leg",
    ]),

    # ── Wings ─────────────────────────────────────────────────────────────────

    ("Wings", "kg", [
        "wings", "wing", "chicken wings", "wing piece", "hot wings",
        "wngs", "wingz", "lollipop", "ready lollipop", "lollypop", "lolipop",
        "chicken lollipop", "ready lollypop",
        "lp", "lpop",
        "loli", "lolypop",
    ]),

    # ── Bone Products ─────────────────────────────────────────────────────────

    ("Carcass", "kg", [
        "carcass", "carcus", "chicken carcass",
        "frame", "chicken frame",
        "haddi", "bones", "chicken bones", "bone",
        "cat pis", "cat piece", "cat pieces",
        "haddi chicken",
    ]),

    # ── Cut Variants ──────────────────────────────────────────────────────────

    ("Curry Cut", "kg", [
        "curry cut", "currycut", "curry cut chicken",
        "curry chicken", "curry piece", "curry pcs",
        "cc", "c/c", "c.c",
        "kari cut", "kadi cut", "kari", "rassa cut", "rassa",
        "thali", "thali chicken", "thali cut",
        "chikn", "cury", "curry",
        "pcs",
    ]),

    ("Biryani Cut", "kg", [
        "biryani cut", "biryani cut chicken",
        "biryani chicken", "biryani pcs", "biryani piece",
        "bc", "b/c", "brc",
        "biriyani cut", "biriyani",
        "biryani cut piece", "biryani chik", "biryani",
    ]),

    # ── Leg Parts ─────────────────────────────────────────────────────────────

    ("Drumstick", "kg", [
        "drumstick", "drumsticks", "drum stick", "drum sticks",
        "chicken drumstick",
        "ds", "drum", "drums",
        "drmstk", "drumstik", "darmistk",
    ]),

    ("Whole Leg", "kg", [
        "whole leg", "full leg", "full chicken leg",
        "leg piece", "complete leg",
        "wl",
        "tangdi", "tangadi", "t leg",
        "full tangdi", "full tangadi", "whole tangdi", "whole tangadi",
        "wholeleg", "fullleg","leg piece", "leg pcs", "leg pscs", "leg psc",
    ]),

    # ── Organ Meat ────────────────────────────────────────────────────────────

    ("Liver", "kg", [
        "liver", "chicken liver", "liver piece",
        "kaleji", "kaleja", "kalgi",
        "liv", "lvr",
        "kaliji",
    ]),

    ("Gizzard", "kg", [
        "gizzard", "gizzards", "chicken gizzard", "gizzard piece",
        "gurda", "pota",
        "giz", "gizz",
        "gizerd",
    ]),

    # ── Mince ─────────────────────────────────────────────────────────────────

    ("Kheema", "kg", [
        "kheema", "keema", "chicken kheema", "chicken keema",
        "mince", "chicken mince", "minced chicken",
        "khima", "qeema",
    ]),

    # ── Whole Chicken: With Skin Tandoor (ERP CH1024560) ──────────────────────
    # Was previously offered in the order template but missing here, so
    # "with skin tandoor" orders never matched. Generic/Hindi "tandoor" stays
    # mapped to the W/O Skin (skinless) variant above to preserve behaviour.

    ("WS Tandoor Chicken", "kg", [
        "with skin tandoor", "with skin whole chicken tandoor",
        "ws tandoor", "ws tandoor chicken", "skin tandoor",
        "tandoor with skin", "with skin td",
    ]),

    # ── Specialty Boneless (ERP) ──────────────────────────────────────────────

    ("Thai Boneless", "kg", [
        "thai boneless", "thai bonless", "thai boneless chicken",
        "thai bnls", "thai bl",
    ]),

    ("Supreme Boneless", "kg", [
        "supreme boneless", "supreme bonless", "supreme boneless chicken",
        "supreme bnls", "supreme",
    ]),

    # ── Byproducts (ERP) ──────────────────────────────────────────────────────

    ("Chicken Neck", "kg", [
        "chicken neck", "neck", "gardan", "gala", "chicken gardan",
    ]),

    ("Chicken Skin", "kg", [
        "chicken skin", "only skin", "skin only", "chamdi", "chicken chamdi",
    ]),

    ("Chicken Feet", "kg", [
        "chicken feet", "feet", "chicken paw", "paw", "paws", "panje", "chicken panje",
    ]),

    ("Chicken Mundi", "kg", [
        "chicken mundi", "mundi", "chicken head", "head", "mundya", "chicken mundya",
    ]),

]

# Flat set of all valid canonical product names (for alias resolution validation)
VALID_PRODUCT_NAMES = {d for d, _, _ in PRODUCT_DEFINITIONS}


# ── Skin-ambiguous whole-chicken terms ────────────────────────────────────────
# Whole-chicken words that DON'T state With Skin vs Without Skin. With/Without
# Skin are different priced products, so the parser must not guess: these route
# to the Unclear flow (manager picks the variant from the dropdown). Once
# resolved, the learned alias (global or per-customer) auto-maps next time —
# checked BEFORE the catalog — so this is a one-time cleanup per term.
# Matching is by token SET (word order & simple punctuation ignored), so
# "chicken big" and "big chicken" are both caught.
AMBIGUOUS_SKIN_TERMS = [
    # Regular whole chicken — no skin stated
    "regular", "regular chicken", "whole chicken regular", "reg chicken",
    "reg chik", "reguler", "reglar",
    "big chicken", "chicken big", "big chik", "full chicken", "large chicken",
    "heavy chicken", "big bird", "large bird", "regular bird",
    "whole chicken", "whole broiler", "whole broiler chicken", "broiler",
    "boiler", "full bird", "wbc",
    "murgi", "murg", "kombdi", "kombadi",
    # Tandoor whole chicken — no skin stated
    "tandoor", "tandur", "tandoori", "tanduri",
    "tandoor chicken", "tandoori chicken", "tandur chicken", "tanduri chicken",
    "तंदूर", "तंदूरी", "तंदूर चिकन", "तंदूरी चिकन", "तंदूर साइज", "तंदूर बर्ड",
]


# ── Vasy ERP catalog mapping ──────────────────────────────────────────────────
# Maps each canonical (friendly) product name to its exact Vasy ERP item, so
# parsed orders line up 1:1 with the ERP catalog for push / reconciliation.
# Source: Vasy ERP "Product List" export (FY 2026-27).
#
# Friendly names above stay stable (they key rates, invoices, stats, history);
# this dict is the sole ERP integration point. `erp_code` / `erp_name` are the
# exact ERP Item Code and Name.
#
# Deliberately EXCLUDED from matching (not customer-orderable / not selected):
#   Delivery Charges, Plant Wastage, Dead Bird           (internal, non-product)
#   Live/Gavran birds (CH1024551/552/553/554)            (not sold to hotels)
#   Pure Gavran Chicken Curry Cut (CH1024556)            (gavran range)
#   Chicken Liver and Gizzard combo (CH1024577)          (kept Liver & Gizzard separate)
#   Chicken Curry Cut With Skin (CH1024562, Deactive)    (generic Curry Cut → Without Skin)
#
# 2026-08-15: entire Vasy product catalog was deleted and re-added, so every
# item got a new code (old CH10245xx codes → new 900xxx codes). Re-synced from
# the "Fluffy Fresh Foods Private Limited (9)" Product List export, 2026-08-16.
# "Thai Boneless" (FS10246370) and "Supreme Boneless" (CH1024578) were NOT
# re-added in Vasy yet — left on their old (now-invalid) codes until they are;
# invoices containing those two items will keep failing until then.
ERP_ITEMS = {
    "WS Regular Chicken":       {"erp_code": "900006", "erp_name": "With Skin whole chicken Regular",    "category": "Chicken"},
    "WS Tandoor Chicken":       {"erp_code": "900005", "erp_name": "With Skin whole chicken Tandoor",    "category": "Chicken"},
    "W/O Skin Regular Chicken": {"erp_code": "900004", "erp_name": "Without Skin whole chicken Regular", "category": "Chicken"},
    "W/O Skin Tandoor Chicken": {"erp_code": "900003", "erp_name": "Without Skin whole chicken Tandoor", "category": "Chicken"},
    "Curry Cut":                {"erp_code": "900007", "erp_name": "Chicken Curry Cut Without Skin",     "category": "Chicken"},
    "Biryani Cut":              {"erp_code": "900020", "erp_name": "Chicken Biryani Cut",                "category": "Chicken"},
    "Breast Boneless":          {"erp_code": "900018", "erp_name": "Chicken Breast boneless",            "category": "Chicken"},
    "Leg Boneless":             {"erp_code": "900002", "erp_name": "Chicken Leg Boneless",               "category": "Chicken"},
    "Thai Boneless":            {"erp_code": "FS10246370","erp_name": "THAI BONLESS",                       "category": "Chicken"},
    "Supreme Boneless":         {"erp_code": "CH1024578", "erp_name": "Supreme Bonless",                    "category": "Chicken"},
    "Wings":                    {"erp_code": "900019", "erp_name": "Chicken Wings with Skin",            "category": "Chicken"},
    "Drumstick":                {"erp_code": "900017", "erp_name": "Chicken Drumstick",                  "category": "Chicken"},
    "Whole Leg":                {"erp_code": "900016", "erp_name": "Chicken Whole Leg",                  "category": "Chicken"},
    "Carcass":                  {"erp_code": "900011", "erp_name": "Chicken Carcass",                    "category": "Chicken"},
    "Chicken Neck":             {"erp_code": "900009", "erp_name": "Chicken Neck",                       "category": "Chicken"},
    "Chicken Skin":             {"erp_code": "900008", "erp_name": "Chicken Skin",                       "category": "Chicken"},
    "Chicken Feet":             {"erp_code": "900012", "erp_name": "Chicken Feet",                       "category": "Chicken"},
    "Chicken Mundi":            {"erp_code": "900010", "erp_name": "Chicken Mundi",                      "category": "Chicken"},
    "Liver":                    {"erp_code": "900014", "erp_name": "Chicken Liver",                      "category": "Chicken"},
    "Gizzard":                  {"erp_code": "900013", "erp_name": "Chicken Gizzard",                    "category": "Chicken"},
    "Kheema":                   {"erp_code": "900015", "erp_name": "Chicken Kheema",                     "category": "Chicken"},
}

# Safety net: every ERP-mapped name must be a real canonical product, and every
# canonical product must have an ERP mapping — catches drift the moment it happens.
assert set(ERP_ITEMS) == VALID_PRODUCT_NAMES, (
    "ERP_ITEMS / PRODUCT_DEFINITIONS mismatch — "
    f"only in ERP_ITEMS: {set(ERP_ITEMS) - VALID_PRODUCT_NAMES}; "
    f"only in PRODUCT_DEFINITIONS: {VALID_PRODUCT_NAMES - set(ERP_ITEMS)}"
)


def get_erp_item(canonical_name: str) -> dict | None:
    """Return the Vasy ERP item ({erp_code, erp_name, category}) for a canonical
    product name, or None if the name isn't in the catalog."""
    return ERP_ITEMS.get(canonical_name)


# Item codes these products carried BEFORE the 2026-08-15 Vasy catalog rewrite
# (and earlier resets going back to 2024). Vasy support confirmed the deleted
# catalog cannot be restored — historical invoices already posted in Vasy
# still carry these old codes, so analytics imports (which re-read the whole
# FY every sync) need them to keep resolving to a clean product name instead
# of falling back to the report's bare category.
# Source: Vasy support's raw product-table dump ("78 product all data"), 2026-08-17.
HISTORICAL_ERP_CODE_ALIASES = {
    "CH1024561": "WS Regular Chicken",
    "CH1024560": "WS Tandoor Chicken",
    "CH1024559": "W/O Skin Regular Chicken",
    "CH1024558": "W/O Skin Tandoor Chicken",
    "CH1024563": "Curry Cut",
    "CH1024576": "Biryani Cut", "102453": "Biryani Cut", "20": "Biryani Cut",
    "CH1024574": "Breast Boneless", "102459": "Breast Boneless", "18": "Breast Boneless",
    "CH1024557": "Leg Boneless", "102460": "Leg Boneless", "1": "Leg Boneless",
    "CH1024575": "Wings",
    "CH1024573": "Drumstick", "102461": "Drumstick", "17": "Drumstick",
    "CH1024572": "Whole Leg", "102462": "Whole Leg", "16": "Whole Leg",
    "CH1024567": "Carcass", "102467": "Carcass", "11": "Carcass",
    "CH1024565": "Chicken Neck", "102469": "Chicken Neck", "9": "Chicken Neck",
    "CH1024564": "Chicken Skin", "102470": "Chicken Skin", "8": "Chicken Skin",
    "CH1024568": "Chicken Feet", "102466": "Chicken Feet", "12": "Chicken Feet",
    "CH1024566": "Chicken Mundi", "102468": "Chicken Mundi", "10": "Chicken Mundi",
    "CH1024570": "Liver", "102464": "Liver", "14": "Liver",
    "CH1024569": "Gizzard", "102465": "Gizzard", "13": "Gizzard",
    "CH1024571": "Kheema", "102463": "Kheema", "15": "Kheema",
}


def get_erp_name_for_any_code(code: str) -> str | None:
    """Resolve an item code to its ERP display name across BOTH the current
    Vasy catalog and the pre-rewrite historical codes above. Use this (not
    ERP_ITEMS directly) wherever a report line's item code might predate the
    2026-08-15 catalog rewrite — e.g. importing a full-FY analytics export."""
    code = (code or "").strip()
    if not code:
        return None
    for item in ERP_ITEMS.values():
        if item["erp_code"] == code:
            return item["erp_name"]
    short = HISTORICAL_ERP_CODE_ALIASES.get(code)
    return ERP_ITEMS[short]["erp_name"] if short else None


def erp_display_name(product: str) -> str:
    """Human-facing display name for a product — the EXACT Vasy ERP item name when
    the product maps to the ERP catalog, else the name unchanged.

    Use this at EVERY surface where a product name is shown to a human (WhatsApp
    messages, reports, invoices, dashboard, ledger). Storage and lookup KEYS
    (rates, stats, aliases, form field names) must keep the friendly canonical
    name — only the display is swapped."""
    erp = get_erp_item((product or "").strip())
    return erp["erp_name"] if erp else (product or "")


# Reverse lookup: exact Vasy ERP name → friendly canonical (short) name.
_ERP_NAME_TO_SHORT = {v["erp_name"]: k for k, v in ERP_ITEMS.items()}


def short_product_name(product: str) -> str:
    """Compact product label for width-constrained surfaces (the per-hotel rows
    on the printed production sheet). Returns the friendly canonical name — e.g.
    'Curry Cut', 'W/O Skin Tandoor Chicken' — for anything in the ERP catalog,
    whether the product is given as the canonical name OR the long ERP name.
    Falls back to the name unchanged. Display only — storage/lookup keys stay on
    the canonical name (see erp_display_name)."""
    name = (product or "").strip()
    if name in ERP_ITEMS:
        return name
    return _ERP_NAME_TO_SHORT.get(name, name)


# ── Normalizers ───────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return (
        text.lower()
        .replace("*", "")
        .replace("_", "")
        .replace("/", "")
        .replace(".", "")
        .replace("-", " ")
        .strip()
    )


def _squish(text: str) -> str:
    return re.sub(r'\s+', '', _normalize(text))


def _tokenize(text: str) -> set:
    return set(_normalize(text).split())


# ── Alias-key normalization ───────────────────────────────────────────────────
# A SINGLE canonical key for matching unclear-item aliases, applied IDENTICALLY
# when an alias is STORED (admin resolve endpoint) and when it is LOOKED UP
# (parser). Without this, a resolved phrase silently fails to match the very next
# order, because the stored key kept surrounding noise that the parser strips:
#   stored   "1)chicken big ------"    (list marker + dash tail retained)
#   lookup   "chicken big ------"      (list marker stripped by the parser)
# Normalizing BOTH sides down to "chicken big" makes the match survive list
# numbers ("1)", "2."), dash decoration ("------"), and a trailing quantity/unit
# ("30 kg"), which is exactly the noise that differs between two sends of the
# same item.
_LIST_MARKER_RE = re.compile(r'^\s*(?:\d+\s*[)\.\-:]|[•\*–—\-])\s*')


def strip_list_marker(text: str) -> str:
    """Remove a single leading list marker — "1)", "2.", "3-", "•", "-", "*" —
    from the start of a line. Leaves a bare decimal quantity untouched is NOT a
    concern here (callers pass product-name text, not qty-first lines)."""
    if not text:
        return text
    return _LIST_MARKER_RE.sub('', text.strip(), count=1)


def normalize_alias_key(raw: str) -> str:
    """Canonical, human-readable alias key: lowercased product phrase with any
    leading list marker, leading quantity(+unit), parenthetical size annotation
    ("(900 gm)"), trailing quantity/unit, and trailing dash/punctuation
    decoration removed. Used for STORAGE (and display) so the saved key stays
    readable AND matches the name the parser looks up.

        "1)Chicken big ------- 30 kg"        -> "chicken big"
        "chicken big (900 gm) 30 kg"         -> "chicken big"
        "chicken big ------"                 -> "chicken big"
        "5 तंदूर"                             -> "तंदूर"
        "5 kg breast"                        -> "breast"
        "tandoori"                           -> "tandoori"
    """
    if not raw:
        return ""
    s = strip_list_marker(str(raw))                              # leading "1)" etc.
    # Leading quantity(+unit): "5 तंदूर" -> "तंदूर", "5 kg breast" -> "breast".
    # Mirrors the parser's quantity-first path (_QTY_FIRST_RE + _LEADING_UNIT_RE)
    # so the stored key equals the raw_name the parser looks up. WITHOUT this,
    # a quantity-first order ("5 तंदूर") re-enters the Unclear flow on every
    # send even after the manager has resolved it, because the stored key kept
    # the leading "5" that the parser had already split off as the quantity.
    _lead = re.match(r'^\s*[\d]+(?:[./][\d]+)?\s+(.+)$', s)
    if _lead:
        _rest = _lead.group(1)
        _lead_unit = _LEADING_UNIT_RE.match(_rest)
        s = _lead_unit.group(2) if _lead_unit else _rest
    s = re.sub(r'\([^)]*\)', ' ', s)                            # "(900 gm)" size notes
    s = re.sub(r'\s*[-:]?\s*[\d\.]+\s*[a-zA-Z]*\s*$', '', s)     # trailing "30 kg"
    s = re.sub(r'[\s\-–—:_.]+$', '', s)                # trailing "------"
    s = re.sub(r'\s+', ' ', s).strip().lower()                   # collapse + lower
    return s


def alias_token_set(raw: str) -> frozenset:
    """Word-set of an alias key — the matching unit. Order-independent so
    "chicken big" and "big chicken" (the same product, typed either way) match."""
    return frozenset(normalize_alias_key(raw).split())


def alias_keys_match(a: str, b: str) -> bool:
    """True when two raw alias texts refer to the same product, ignoring list
    markers, dash decoration, trailing quantity, size annotations, AND word
    order. This is THE alias-matching predicate — used on both lookup and the
    retroactive patch so a single resolution covers every spelling variant."""
    ta = alias_token_set(a)
    return bool(ta) and ta == alias_token_set(b)


# Token-sets of the skin-ambiguous terms, built once (lazily so it can rely on
# _tokenize being defined). Compared as frozensets so word order doesn't matter.
_AMBIGUOUS_TOKEN_SETS = None


def _is_skin_ambiguous(raw_tokens: set) -> bool:
    """True when the input is exactly a known skin-ambiguous whole-chicken term
    (see AMBIGUOUS_SKIN_TERMS). Such inputs must fall through to the Unclear
    flow rather than being matched/guessed to a skin variant."""
    global _AMBIGUOUS_TOKEN_SETS
    if _AMBIGUOUS_TOKEN_SETS is None:
        _AMBIGUOUS_TOKEN_SETS = {frozenset(_tokenize(t)) for t in AMBIGUOUS_SKIN_TERMS}
    return bool(raw_tokens) and frozenset(raw_tokens) in _AMBIGUOUS_TOKEN_SETS


def _match_product(raw_name: str):
    """
    Returns (display_name, unit) or None.
    Priority: exact → partial/startswith → token subset
 
    startswith guard: a single-token input (e.g. "chicken") must not
    startswith-match a multi-token alias (e.g. "chicken breast") — that
    would cause "Chicken 10" to match Breast Boneless instead of going
    to unclear items.  startswith only fires when BOTH sides are
    multi-token, or the input has more tokens than the alias (i.e. the
    input is more specific than the alias).
    """
    n = _normalize(raw_name)
    s = _squish(raw_name)
    raw_tokens = _tokenize(raw_name)

    # 0. Skin-ambiguous whole-chicken terms → no catalog match, so they route to
    #    the Unclear flow instead of being guessed to a With/Without Skin variant.
    #    Runs before any (exact/fuzzy) matching. Learned aliases are checked
    #    upstream of _match_product, so a resolved term still auto-maps.
    if _is_skin_ambiguous(raw_tokens):
        return None

    # 1. Exact
    for display, unit, aliases in PRODUCT_DEFINITIONS:
        for alias in aliases:
            if n == _normalize(alias) or s == _squish(alias):
                return display, unit
 
    # 2. Partial / startswith
    # Guard: only fire startswith when the input has >= 2 tokens OR the
    # alias is a single token.  Prevents "chicken" matching "chicken breast".
    for display, unit, aliases in PRODUCT_DEFINITIONS:
        for alias in aliases:
            a  = _normalize(alias)
            aq = _squish(alias)
            alias_tokens = _tokenize(alias)
 
            if len(raw_tokens) >= 2 or len(alias_tokens) == 1:
                if n.startswith(a) or a.startswith(n):
                    return display, unit
                if s.startswith(aq) or aq.startswith(s):
                    return display, unit
 
# 3. Token subset
    # Guard: BOTH sides must have >= 2 tokens.
    # A single generic word like "chicken" must never subset-match
    # a multi-token alias like "chicken breast" or "bonless chicken".
    if raw_tokens:
        for display, unit, aliases in PRODUCT_DEFINITIONS:
            for alias in aliases:
                alias_tokens = _tokenize(alias)
                if not alias_tokens:
                    continue
                if len(raw_tokens) >= 2 and len(alias_tokens) >= 2:
                    if (raw_tokens.issubset(alias_tokens)
                            or alias_tokens.issubset(raw_tokens)):
                        return display, unit
 
    return None


def _lookup_alias(raw_name: str, db) -> tuple | None:
    if db is None:
        return None

    # Try exact match first
    normalized = raw_name.strip().lower()
    row = db.query(UnclearItemAlias).filter(
        UnclearItemAlias.raw_text == normalized
    ).first()
    if row:
        unit = _get_unit_for_canonical(row.canonical_product_name)
        return (row.canonical_product_name, unit)

    # Strip quantity/unit suffix and try again
    # Handles "tandoori chicken 30pis" → "tandoori chicken"
    stripped = re.sub(r'\s*[-:]?\s*[\d\.]+\s*[a-zA-Z]*\s*$', '', normalized).strip()
    if stripped and stripped != normalized:
        row = db.query(UnclearItemAlias).filter(
            UnclearItemAlias.raw_text == stripped
        ).first()
        if row:
            unit = _get_unit_for_canonical(row.canonical_product_name)
            return (row.canonical_product_name, unit)

    # Fallback: word-set match so aliases match across list markers / dash
    # decoration / trailing qty / size annotations / word order that the exact
    # queries above miss (e.g. stored "1)chicken big ------" or "big chicken"
    # vs lookup "chicken big"). Old rows are matched without any migration.
    key = alias_token_set(raw_name)
    if key:
        for row in db.query(UnclearItemAlias).all():
            if alias_token_set(row.raw_text) == key:
                unit = _get_unit_for_canonical(row.canonical_product_name)
                return (row.canonical_product_name, unit)

    return None

def _get_unit_for_canonical(canonical_name: str) -> str:
    """Find the unit for a canonical product name from PRODUCT_DEFINITIONS."""
    for display_name, unit, _ in PRODUCT_DEFINITIONS:
        if display_name.lower() == canonical_name.lower():
            return unit
    return "kg"  # default fallback

def _lookup_customer_alias(raw_name: str, customer_phone: str, db) -> tuple | None:
    """
    Check customer_product_aliases for a phone + raw_text match.
    Returns (canonical_product_name, unit) or None.
    db can be None (caller doesn't always have a session).
    """
    if db is None or not customer_phone:
        return None
    normalized = raw_name.strip().lower()
    row = db.query(CustomerProductAlias).filter(
        CustomerProductAlias.customer_phone == customer_phone,
        CustomerProductAlias.raw_text == normalized,
    ).first()
    if not row:
        # Fallback: word-set match across list markers / dash decoration /
        # trailing qty / size annotations / word order that exact match misses
        # (stored "1)chicken big ------" or "big chicken" vs lookup "chicken
        # big"). A customer has few aliases, so scanning all of theirs is cheap
        # and matches old rows without any migration.
        key = alias_token_set(raw_name)
        if key:
            rows = db.query(CustomerProductAlias).filter(
                CustomerProductAlias.customer_phone == customer_phone,
            ).all()
            row = next(
                (r for r in rows if alias_token_set(r.raw_text) == key),
                None,
            )
    if not row:
        return None
    # Find the unit for this canonical product (same logic as _lookup_alias)
    for display_name, unit, aliases in PRODUCT_DEFINITIONS:
        if display_name.lower() == row.canonical_product_name.lower():
            return (row.canonical_product_name, unit)
    # Fallback: return with default unit kg
    return (row.canonical_product_name, "kg")


def _is_noise_phrase(raw_name: str, db) -> bool:
    if db is None:
        return False
    normalized = raw_name.strip().lower()
    # Try exact match
    row = db.query(NoisePhrase).filter(NoisePhrase.raw_text == normalized).first()
    if row:
        return True
    # Try after stripping quantity suffix
    stripped = re.sub(r'\s*[-:]?\s*[\d\.]+\s*[a-zA-Z]*\s*$', '', normalized).strip()
    if stripped and stripped != normalized:
        row = db.query(NoisePhrase).filter(NoisePhrase.raw_text == stripped).first()
        if row:
            return True
    return False


# ── Unit normalization ────────────────────────────────────────────────────────

UNIT_ALIASES = {
    "kg":        "kg",
    "kgs":       "kg",
    "kilo":      "kg",
    "kilos":     "kg",
    "kilogram":  "kg",
    "kilograms": "kg",
    "k":         "kg",
    "nos":       "nos",
    "no":        "nos",
    "nos.":      "nos",
    "pcs":       "nos",
    "pies":       "nos",
    "psc":       "nos",
    "pc":        "nos",
    "pis":       "nos",
    "pieces":    "nos",
    "piece":     "nos",
    "number":    "nos",
    "numbers":   "nos",
}


def _normalize_unit(raw: str) -> str | None:
    return UNIT_ALIASES.get(raw.lower().strip().rstrip("."), None)


# Gram units are handled specially: the quantity is divided by 1000 and the
# unit becomes kg (e.g. "500 g" / "500 gm" → 0.5 kg). Longest-first ordering
# matters inside regex alternations (grams|gram|gms|gm|g).
GRAM_UNITS = {"g", "gm", "gms", "gram", "grams"}

# Leading unit token in a quantity-first line, e.g. "5 kg breast" → unit "kg",
# name "breast".  Includes gram units so "500 g breast" is handled too.
_LEADING_UNIT_RE = re.compile(
    r"^(kg|kgs|kilo|kilos|kilogram|kilograms|grams|gram|gms|gm|g|nos|no|pcs|psc|pc|pis|pieces|piece)\b\s*(.+)$",
    re.IGNORECASE,
)


def _parse_quantity(raw: str):
    try:
        qty = float(raw.strip())
        if qty <= 0:
            return None
        return qty
    except (ValueError, AttributeError):
        return None


# ── Noise filtering ───────────────────────────────────────────────────────────

# Date patterns: 10/06/2026  |  10-06-26  |  10/06  |  June 10  |  10 June
_DATE_LINE_RE = re.compile(
    r'^[\d]{1,2}[\/\-\.][\d]{1,2}([\/\-\.][\d]{2,4})?$'
    r'|^[\d]{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*$'
    r'|^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+[\d]{1,2}',
    re.IGNORECASE,
)

import re
 
# Matches lines where quantity comes FIRST: "3 तंदूर", "4 Leg piece", "2 lollipop"
_QTY_FIRST_RE = re.compile(
    r"^([\d]+(?:[./][\d]+)?)\s+(.+)$"
)

# Matches a standalone delivery-time line with no label, e.g. "7am",
# "8:00 AM", "09:30". Requires either an am/pm marker or a colon, so a bare
# number (a quantity) is never mistaken for a time.
_TIME_LINE_RE = re.compile(
    r"^\d{1,2}(:\d{2})?\s*(am|pm)$"
    r"|^\d{1,2}:\d{2}$",
    re.IGNORECASE,
)


# Common header/footer filler phrases customers add
_FILLER_RE = re.compile(
    r'^(order\s+for(\s+the\s+day)?'
    r'|daily\s+order'
    r'|today[\'s]*\s+order'
    r'|order\s+of\s+the\s+day'
    r'|good\s+morning'
    r'|good\s+evening'
    r'|good\s+afternoon'
    r'|good\s+night'
    r'|please\s+find'
    r'|kindly\s+(note|send|arrange)'
    r'|hi\s*,?$'
    r'|hello\s*,?$'
    r'|dear\s+'
    r'|greetings'
    r'|thank\s+you'
    r'|thanks'
    r'|regards'
    r'|warm\s+regards'
    r'|as\s+usual'
    r'|same\s+as\s+(yesterday|last\s+(time|order))'
    r'|hotel\s+order$'
    r'|order\s+list$'
    r'|todays\s+order)',
    re.IGNORECASE,
)

# Lines starting with a unit word are size annotations, not order lines
# e.g. "kg( 900 gm size)"  /  "pcs (big)"
_UNIT_ANNOTATION_RE = re.compile(
    r'^(kg|kgs|gm|gms|gram|grams|nos|pcs|pc|psc|pies)\b',
    re.IGNORECASE,
)


def _strip_emojis(text: str) -> str:
    """Remove emoji characters, returning plain text."""
    return re.sub(
        r'[\U00010000-\U0010ffff'   # supplementary multilingual plane
        r'\U0001F300-\U0001F9FF'    # misc symbols & pictographs
        r'\u2600-\u26FF'            # misc symbols
        r'\u2700-\u27BF'            # dingbats
        r']+',
        '',
        text,
        flags=re.UNICODE,
    ).strip()


def _is_noise_line(line: str, restaurant_name_norm: str | None) -> bool:
    """
    Return True if this line is a header/footer/annotation and should be
    skipped entirely — not treated as an unclear item.

    Handles:
      • Restaurant name used as header/footer  e.g. "Test hotel 10", "Amrai hotel Order 🏨"
      • Date-only lines                         e.g. "10/06/2026", "10/06"
      • Filler phrases                          e.g. "ORDER FOR THE DAY", "Good morning"
      • Unit-annotation lines                   e.g. "kg( 900 gm size)"
      • Emoji/symbol-only lines                 e.g. "🏨🌟"
      • Numbered-list header lines              e.g. "1." alone with no product text
    """
    stripped = line.strip()
    if not stripped:
        return True

    # Strip emojis for text-based checks
    text_only = _strip_emojis(stripped)

    # 1. Nothing left after removing emojis → purely decorative line
    if not text_only:
        return True

    # 2. No alphanumeric content at all
    if not re.search(r'[a-zA-Z0-9]', text_only):
        return True

    norm = _normalize(text_only)

    # 3. Date-only line. Check the raw text too, not just `norm`: _normalize
    #    strips "/" separators, which would hide slash dates ("16/07/2026") from
    #    the regex and dump them into the Unclear tab as a bogus item.
    if _DATE_LINE_RE.match(norm.strip()) or _DATE_LINE_RE.match(text_only.strip()):
        return True

    # 4. Filler header/footer phrase
    if _FILLER_RE.match(norm.strip()):
        return True

    # 5. Unit-annotation line (e.g. "kg( 900 gm size)")
    if _UNIT_ANNOTATION_RE.match(norm.strip()):
        return True

    # 6. Restaurant name appears anywhere in the line
    #    Covers: "Test hotel 10"  /  "Amrai hotel Order 🏨"  /  footer variants
    if restaurant_name_norm and restaurant_name_norm in norm:
        return True

    return False


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_template_order(customer_phone: str, message: str, db=None) -> dict:
    """
    Parse a free-form or template-style order message.

    Args:
        customer_phone: sender's phone number
        message:        raw WhatsApp message text
        db:             optional SQLAlchemy Session — used to check alias table
                        and unit inference stats.  Pass None to skip both
                        (e.g. in tests that don't need the DB).

    Returns:
        {
            customer_phone,
            items,           ← successfully parsed items
            unclear_items,   ← list of raw strings that couldn't be parsed
                               (after alias check) — stored on Order for manager review
            delivery_date,
            delivery_time,
            is_unclear,      ← True ONLY when items == [] (total failure)
            unclear_reason,
            errors           ← internal detail list (unit mismatches etc.)
        }
    """
    items          = []
    unclear_items  = []
    errors         = []
    delivery_time  = None

    # ── ONE-TIME: resolve restaurant name for noise filtering ─────────────────
    restaurant_name_norm = None
    if db:
        try:
            from orderr_core.models.customer import Customer
            from orderr_core.services.customer_service import normalize_phone
            cust = db.query(Customer).filter(
                Customer.phone_number == normalize_phone(customer_phone)
            ).first()
            if cust and cust.restaurant_name:
                restaurant_name_norm = _normalize(cust.restaurant_name)
        except Exception:
            pass  # never let this crash the parser
    # ─────────────────────────────────────────────────────────────────────────

    lines = message.strip().splitlines()

    # Expand any single line that actually contains multiple items
    # (e.g. "Chicken 5kg boneless 3kg lolipop 2kg tandoori 3") into separate
    # item-strings so each one is parsed/matched independently instead of
    # the whole line being dumped into unclear_items as one blob.
    expanded_lines = []
    for raw_line in lines:
        # First split on item connectors (comma / + / & / "and"), then split
        # any remaining multi-run segment ("wings 2kg curry 3kg") individually.
        for part in _split_on_connectors(raw_line):
            expanded_lines.extend(_split_multi_item_line(part))
    lines = expanded_lines

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        lower = _normalize(line)

        # Delivery time line
        if lower.startswith("🕒") or "delivery time" in lower:
            time_match = re.search(r"[:\-]\s*(.+)$", line)
            if time_match:
                t = time_match.group(1).strip()
                if t and t not in ("__", "-", ""):
                    delivery_time = t
            continue

        # Standalone delivery-time line with no label, e.g. "7am", "8:00 AM",
        # "09:30" — must have either an am/pm marker or a colon, so a bare
        # number like "10" is never mistaken for a time.
        if _TIME_LINE_RE.match(line.strip()):
            delivery_time = line.strip()
            continue

        # Skip header/instruction/note lines (existing hard-coded list)
        if any(skip in lower for skip in [
            "place your order", "copy below", "fill in", "example",
            "delete what", "delivery time", "fluffy", "order —",
            "note -", "note-",
        ]):
            continue

        # Skip noise lines (dates, hotel names, filler phrases etc.)
        if _is_noise_line(line, restaurant_name_norm):
            continue

        # Strip placeholder tokens
        line_clean = re.sub(r'__+', '', line).strip()

        # Strip an inline per-piece size annotation — "(900 gm)", "( 900 gm )",
        # "(900 gm size)". It's the bird's target weight, never the ORDER
        # quantity (that's the kg figure outside the parens), but glued onto the
        # line it makes the tolerant qty regex read "900 gm" as the quantity and
        # the item then fails to match. Only parentheticals naming a gram/size
        # unit are removed, so a genuine "(30 kg)" style qty is left untouched.
        line_clean = re.sub(
            r'\([^)]*\b(?:gm|gms|gram|grams|g|size)\b[^)]*\)', ' ',
            line_clean, flags=re.IGNORECASE,
        ).strip()

        # Collapse runs of 2+ dots to a space. Customers use ".." / "..." as a
        # separator ("Tangdi..4kg", "wings...5 kg"), but the tolerant quantity
        # regex ([\d\.]+) would swallow them into the number as "..4", which
        # float() can't parse — the line then falls into `errors` and is silently
        # dropped. A genuine decimal is a SINGLE dot between digits (1.5, .5), so
        # only runs of two-or-more dots are decoration and safe to strip.
        line_clean = re.sub(r'\.{2,}', ' ', line_clean).strip()

        # Empty after stripping placeholders → skip
        if not line_clean:
            continue

        # Lines with no ASCII digit may carry word quantities (डेढ़, half…)
        # Don't skip — flag has_digit so we know whether to try word parser.
        has_digit = bool(re.search(r'\d', line_clean))

        # Handle "3k" style
        line_clean = re.sub(r'(\d+)\s*k\b', r'\1 kg', line_clean)

        # Convert simple single-digit fractions to decimals: "1/2"→"0.5",
        # "3/4"→"0.75". Lookarounds keep multi-digit dates (10/06, 1/2/2026)
        # untouched — only an isolated n/m with single digits is converted.
        line_clean = re.sub(
            r'(?<!\d)([1-9])\s*/\s*([1-9])(?!\d)',
            lambda m: ("%g" % (int(m.group(1)) / int(m.group(2)))),
            line_clean,
        )

        # Strip leading list markers: "1)", "1.", "1-", "•", "-", "*".
        # The (?!\d) guard stops a leading decimal quantity ("1.5 kg", "0.5 kg")
        # from being mistaken for a "1." list marker and mangled into "5 kg".
        line_clean = re.sub(r'^[\d]+[)\.\-](?!\d)\s*', '', line_clean).strip()
        line_clean = re.sub(r'^[•\-\*]\s*', '', line_clean).strip()

        # ── Word-quantity path (no ASCII digit in line) ───────────────────────
        if not has_digit:
            from orderr_core.services.word_quantity import parse_word_quantity
            wq = parse_word_quantity(line_clean)
            if wq:
                # We have a quantity but still need to identify the product.
                # Run the normal name matcher against the full line minus the
                # quantity word so "डेढ़ किलो lollipop" → raw_name="lollipop".
                # Simple approach: remove the matched word token(s) and unit
                # hint words, treat remainder as product name.
                import re as _re
                _unit_words = {
                    "kg","kgs","kilo","किलो","किलोग्राम","kilogram",
                    "nos","pcs","pc","piece","pieces","nag","नग",
                }
                name_tokens = [
                    t for t in line_clean.split()
                    if t.lower() not in _unit_words
                    and t not in wq.raw_word.split()
                    and t.lower() not in wq.raw_word.lower().split()
                ]
                candidate_name = " ".join(name_tokens).strip()
                product_match  = _lookup_customer_alias(candidate_name, customer_phone, db) if candidate_name else None
                if not product_match:
                    product_match = _lookup_alias(candidate_name, db) if candidate_name else None
                if not product_match:
                    product_match = _match_product(candidate_name) if candidate_name else None

                # if product_match:
                #     display_name, expected_unit = product_match
                #     hint = (
                #         f"__word_qty__{display_name}"
                #         f"::{wq.quantity}"
                #         f"::{wq.unit_hint or expected_unit}"
                #         f"  [{wq.raw_word} → {wq.hint_str}]"
                #     )
                # else:
                #     hint = f"__word_qty__UNKNOWN::{wq.quantity}::{wq.unit_hint or '?'}  [{wq.raw_word} → {wq.hint_str}] | raw: {line_clean}"


                if product_match:
                    display_name, expected_unit = product_match
                    hint = (
                        f"__word_qty__{display_name}"
                        f"::{wq.quantity}"
                        f"::{wq.unit_hint or expected_unit}"
                        f"::{candidate_name}"
                        f"  [{wq.raw_word} → {wq.hint_str}]"
                    )
                else:
                    hint = (
                        f"__word_qty__UNKNOWN"
                        f"::{wq.quantity}"
                        f"::{wq.unit_hint or '?'}"
                        f"::{candidate_name}"
                        f"  [{wq.raw_word} → {wq.hint_str}] | raw: {line_clean}"
                    )    

                unclear_items.append(hint)
            else:
                # No word quantity found either — check the noise-phrase
                # table before dumping into unclear_items. This check was
                # previously skipped entirely on this code path.
                if _is_noise_phrase(line_clean, db):
                    continue
                unclear_items.append(line)
            continue
        # ── End word-quantity path ────────────────────────────────────────────

        # ── Parse "<name> <qty> [unit]" via a fallback ladder ─────────────────
        # Shared unit alternation (kg / gram / nos families). kg variants come
        # before the bare "g" so "kg" is never mis-read as grams; gram units
        # are converted to kg further below.
        _units = (r"kg|kgs|kilo|kilos|kilogram|kilograms|grams|gram|gms|gm|g"
                  r"|pies|nos|no|nos\.|pcs|psc|pc|pis|pieces|piece|k")

        # 1. Anchored: quantity (+ optional unit) at the END of the line.
        split_match = re.match(
            rf"^(.+?)\s*[-:]?\s*([\d\.]+)\s*({_units})?\s*$",
            line_clean, re.IGNORECASE,
        )

        # 2. Tolerant: trailing words AFTER an explicit unit, e.g.
        #    "chicken drumsticks 3 kg pathva", "wings 2 nos please". Name must
        #    start non-digit so leading-number names ("1.5kg chicken") are left
        #    to the normal path; a unit is required before the trailing text.
        if not split_match:
            split_match = re.match(
                rf"^(\D.*?)\s*[-:]?\s*([\d\.]+)\s*({_units})\s+\S.*$",
                line_clean, re.IGNORECASE,
            )

        raw_unit_str = ""
        if split_match:
            raw_name     = split_match.group(1).strip()
            raw_qty      = split_match.group(2).strip()
            raw_unit_str = (split_match.group(3) or "").strip()
        else:
            # 3. Unrecognized / mistyped unit after the number ("curry cut 3 kig",
            #    "wings 2 kgg"): still capture the quantity instead of defaulting
            #    to 1. Name must start non-digit; unit stays unknown → inference.
            junk_match = re.match(r"^(\D.*?)\s*[-:]?\s*([\d\.]+)\b.*$", line_clean, re.IGNORECASE)
            if junk_match:
                raw_name = junk_match.group(1).strip()
                raw_qty  = junk_match.group(2).strip()
            else:
                # 4. Quantity-first, e.g. "3 तंदूर", "4 Leg piece", "5 kg breast".
                qty_first_match = _QTY_FIRST_RE.match(line_clean)
                if qty_first_match:
                    raw_qty  = qty_first_match.group(1).strip()
                    raw_name = qty_first_match.group(2).strip()
                    # Strip a leading unit word: "kg breast" → unit "kg", "breast".
                    lead = _LEADING_UNIT_RE.match(raw_name)
                    if lead:
                        raw_unit_str = lead.group(1).strip()
                        raw_name     = lead.group(2).strip()
                else:
                    # 5. No quantity at all — whole line is the name, qty=1.
                    raw_name = line_clean
                    raw_qty  = "1"

        # Resolve unit, converting grams → kg (÷1000): "500 g" / "500gm" → 0.5 kg.
        if raw_unit_str.lower().strip().rstrip(".") in GRAM_UNITS:
            _q = _parse_quantity(raw_qty)
            if _q is not None:
                raw_qty = "%g" % (_q / 1000.0)
            raw_unit = "kg"
        else:
            raw_unit = _normalize_unit(raw_unit_str) if raw_unit_str else None
        # ─────────────────────────────────────────────────────────────────────

        if raw_qty in ("__", "0", ""):
            continue

        # ── Lookup chain ──────────────────────────────────────────────────────
        # Customer-specific and manager-resolved global aliases take priority
        # over the static catalog, so an explicit correction (e.g. "kaleji" →
        # "Mutton") can override a hardcoded catalog default ("kaleji" →
        # "Liver"). No effect when no DB session is passed.
        # 1. Customer-specific alias
        product_match = _lookup_customer_alias(raw_name, customer_phone, db)

        # 2. Global unclear_item_aliases
        if not product_match:
            product_match = _lookup_alias(raw_name, db)

        # 3. Global catalog
        if not product_match:
            product_match = _match_product(raw_name)

        # 4. Noise check — skip silently
        if not product_match:
            if _is_noise_phrase(raw_name, db):
                continue
            # 5. Truly unclear — store raw line for manager review
            unclear_items.append(line)
            continue
        # ─────────────────────────────────────────────────────────────────────

        display_name, expected_unit = product_match

        qty = _parse_quantity(raw_qty)
        if qty is None:
            errors.append({
                "line":       line,
                "reason":     f"Quantity *{raw_qty}* doesn't look right",
                "suggestion": f"{display_name} - 1 {expected_unit}",
            })
            # The PRODUCT was recognized but the quantity is unreadable. Never
            # drop it silently — surface the raw line as unclear so the manager
            # sees it (dashboard renders such orders RED / "check order").
            unclear_items.append(line)
            continue

        # Unit mismatch — only flag if customer explicitly typed a wrong unit
        if raw_unit and raw_unit != expected_unit:
            errors.append({
                "line":       line,
                "reason":     f"*{display_name}* is ordered in *{expected_unit}* (you sent {raw_unit_str})",
                "suggestion": f"{display_name} - {fmt_qty(qty)} {expected_unit}",
            })
            # Recognized product, but the customer's unit conflicts with the
            # catalog unit — don't drop it; surface for manager review.
            unclear_items.append(line)
            continue

        # ── explicit_unit: True when the customer's text contained a unit token ──
        # raw_unit is set only when the regex captured a unit token from the text.
        explicit_unit = (raw_unit is not None)

        # ── Unit inference (FR-3) ─────────────────────────────────────────────
        # Only applies to kg products where the customer gave a bare number
        # (no explicit unit token).  Products with nos/pcs default unit, and
        # any line where the customer stated the unit, bypass inference entirely.
        if explicit_unit or expected_unit != "kg":
            # FR-2: explicit unit → use as-is.
            # Non-kg products (nos) → no ambiguity possible.
            final_unit = raw_unit if raw_unit else expected_unit

            # Merge duplicates
            for item in items:
                if item["product"] == display_name and item["unit"] == final_unit:
                    item["quantity"] += qty
                    break
            else:
                items.append({
                    "product":       display_name,
                    "quantity":      qty,
                    "unit":          final_unit,
                    "explicit_unit": explicit_unit,
                })
        else:
            # FR-3: bare number on a kg product → run inference.
            # Import here (lazy) so the module is usable without the service
            # present in test environments that only import the parser.
            from orderr_core.services.unit_inference import infer_unit

            result = infer_unit(
                product=display_name,
                raw_number=qty,
                customer_phone=customer_phone,
                default_unit=expected_unit,
                db=db,
            )

            if result.is_confident:
                # Confident resolution — use inferred unit and continue normally.
                final_unit = result.unit  # "kg" or "g"

                # Merge duplicates
                for item in items:
                    if item["product"] == display_name and item["unit"] == final_unit:
                        item["quantity"] += qty
                        break
                else:
                    items.append({
                        "product":        display_name,
                        "quantity":       qty,
                        "unit":           final_unit,
                        "explicit_unit":  False,
                        "_inferred_from": result.source,  # for debugging
                    })
            else:
                # Ambiguous or no signal — route to unclear items.
                # Store UNIT_AMBIGUOUS_MARKER so the dashboard renders a
                # kg/g toggle instead of the product-name dropdown.
                items.append({
                    "product":       display_name,
                    "quantity":      qty,
                    "unit":          UNIT_AMBIGUOUS_MARKER,
                    "explicit_unit": False,
                })
                unclear_items.append(
                    f"__qty_ambiguous__{display_name}::{qty}"
                )
        # ── End of unit inference block ───────────────────────────────────────

    # is_unclear = truly nothing parseable at all
    is_unclear = len(items) == 0 and len(unclear_items) == 0

    return {
        "customer_phone": customer_phone,
        "items":          items,
        "unclear_items":  unclear_items,
        "delivery_date":  None,
        "delivery_time":  delivery_time,
        "is_unclear":     is_unclear,
        "unclear_reason": "No valid items found" if is_unclear else None,
        "errors":         errors,
    }


def build_error_message(errors: list) -> str:
    lines = ["⚠️ *Small issue with your order:*\n"]
    for e in errors:
        lines.append(f"❌ {e['reason']}")
        if e.get("suggestion"):
            lines.append(f"✅ Please send like:\n{e['suggestion']}\n")
    lines.append(
        "\nFor the full product list, type *order* to get the template again."
    )
    return "\n".join(lines)
