"""
Purchase analytics — what we bought, from whom, at what rate, and where a bill's
rate looks off. Pure read-side over the Vasy purchase mirror
(vasy_purchases / vasy_purchase_items); nothing here writes.

Effective rate = Σ amount ÷ Σ qty (not the mean of the `rate` column), so it
reflects what was actually paid per unit once all lines are weighed by volume.
"""
from datetime import date, timedelta
from statistics import median

from sqlalchemy import func
from sqlalchemy.orm import Session

from orderr_core.models.vasy_sales_item import VasySalesItem
from orderr_core.models.tandoor_nos import TandoorNos
from orderr_core.models.vasy_purchase import VasyPurchase, VasyPurchaseItem
from orderr_core.services.analytics_service import fmt_inr
from orderr_core.utils import fmt_qty

# A line is flagged when its rate is this far above the item's trailing median.
RATE_SPIKE_PCT = 8.0
# Trailing window (days before the bill) and minimum prior lines for a baseline.
BASELINE_DAYS = 30
BASELINE_MIN_LINES = 3
MAX_FLAGS = 40


def _pct(new, old):
    return round((new - old) / old * 100, 1) if old else None


def _lines(db: Session, start: date, end: date):
    """(date, bill_no, supplier, item, qty, amount, rate) for every line in range."""
    q = (db.query(VasyPurchase.bill_date, VasyPurchase.bill_no, VasyPurchase.party_name,
                  VasyPurchaseItem.product_name, VasyPurchaseItem.qty,
                  VasyPurchaseItem.amount, VasyPurchaseItem.rate)
         .join(VasyPurchaseItem, VasyPurchaseItem.vasy_purchase_id == VasyPurchase.id)
         .filter(VasyPurchase.bill_date != None,                      # noqa: E711
                 VasyPurchase.bill_date >= start,
                 VasyPurchase.bill_date <= end))
    return [{"date": d, "bill": b, "supplier": s or "?", "item": i or "?",
             "qty": float(qy or 0), "amt": float(a or 0), "rate": float(r or 0)}
            for d, b, s, i, qy, a, r in q.all()]


def _totals(lines):
    return {"qty": sum(l["qty"] for l in lines),
            "amt": sum(l["amt"] for l in lines),
            "bills": len({l["bill"] for l in lines}),
            "suppliers": len({l["supplier"] for l in lines})}


def _group(lines, key):
    g = {}
    for l in lines:
        a = g.setdefault(l[key], {"qty": 0.0, "amt": 0.0, "bills": set(), "rates": [],
                                  "other": set()})
        a["qty"] += l["qty"]
        a["amt"] += l["amt"]
        a["bills"].add(l["bill"])
        if l["rate"]:
            a["rates"].append(l["rate"])
        a["other"].add(l["supplier"] if key == "item" else l["item"])
    return g


def _weekly_rate_series(lines, start: date, end: date):
    """Effective ₹/unit per ISO-week bucket (Monday start) across the window."""
    buckets = {}
    for l in lines:
        wk = l["date"] - timedelta(days=l["date"].weekday())
        b = buckets.setdefault(wk, [0.0, 0.0])
        b[0] += l["amt"]
        b[1] += l["qty"]
    out = []
    wk = start - timedelta(days=start.weekday())
    while wk <= end:
        amt, qty = buckets.get(wk, [0.0, 0.0])
        out.append(round(amt / qty, 2) if qty else None)
        wk += timedelta(days=7)
    return out


def purchase_overview(db: Session, start: date, end: date) -> dict:
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)

    cur = _lines(db, start, end)
    prev = _lines(db, prev_start, prev_end)
    tc, tp = _totals(cur), _totals(prev)
    avg_c = tc["amt"] / tc["qty"] if tc["qty"] else 0.0
    avg_p = tp["amt"] / tp["qty"] if tp["qty"] else 0.0

    # ── by item ──────────────────────────────────────────────────────────────
    prev_items = _group(prev, "item")
    sup_by_item = {}
    for l in cur:
        s = sup_by_item.setdefault(l["item"], {}).setdefault(l["supplier"], [0.0, 0.0])
        s[0] += l["amt"]
        s[1] += l["qty"]
    last_rate = {}
    for l in sorted(cur, key=lambda x: (x["date"], x["bill"])):
        if l["rate"]:
            last_rate[l["item"]] = l["rate"]

    items = []
    for name, a in _group(cur, "item").items():
        rate = a["amt"] / a["qty"] if a["qty"] else 0.0
        p = prev_items.get(name)
        prate = (p["amt"] / p["qty"]) if p and p["qty"] else 0.0
        # cheapest supplier by effective rate (only meaningful with ≥2 suppliers)
        sup_rates = {s: v[0] / v[1] for s, v in sup_by_item[name].items() if v[1]}
        best = min(sup_rates, key=sup_rates.get) if len(sup_rates) > 1 else None
        item_lines = [l for l in cur if l["item"] == name]
        items.append({
            "item": name,
            "qty": fmt_qty(round(a["qty"], 1)),
            "amt": a["amt"], "amt_fmt": fmt_inr(a["amt"]),
            "share": round(a["amt"] / tc["amt"] * 100, 1) if tc["amt"] else 0.0,
            "rate": round(rate, 2),
            "min_rate": min(a["rates"]) if a["rates"] else 0,
            "max_rate": max(a["rates"]) if a["rates"] else 0,
            "last_rate": last_rate.get(name, 0),
            "rate_chg": _pct(rate, prate) if prate else None,
            "bills": len(a["bills"]),
            "suppliers": len(a["other"]),
            "best_supplier": best,
            "best_rate": round(sup_rates[best], 2) if best else None,
            "spark": _weekly_rate_series(item_lines, start, end),
        })
    items.sort(key=lambda x: x["amt"], reverse=True)

    # ── by supplier ──────────────────────────────────────────────────────────
    prev_sup = _group(prev, "supplier")
    suppliers = []
    for name, a in _group(cur, "supplier").items():
        p = prev_sup.get(name)
        suppliers.append({
            "supplier": name,
            "qty": fmt_qty(round(a["qty"], 1)),
            "amt": a["amt"], "amt_fmt": fmt_inr(a["amt"]),
            "share": round(a["amt"] / tc["amt"] * 100, 1) if tc["amt"] else 0.0,
            "rate": round(a["amt"] / a["qty"], 2) if a["qty"] else 0.0,
            "bills": len(a["bills"]),
            "n_items": len(a["other"]),
            "chg": _pct(a["amt"], p["amt"]) if p and p["amt"] else None,
        })
    suppliers.sort(key=lambda x: x["amt"], reverse=True)

    return {
        "has_data": bool(cur),
        "from": start.isoformat(), "to": end.isoformat(), "days": span,
        "prev_from": prev_start.strftime("%d %b"), "prev_to": prev_end.strftime("%d %b"),
        "kpi": {
            "amt_fmt": fmt_inr(tc["amt"]), "amt_chg": _pct(tc["amt"], tp["amt"]),
            "qty": fmt_qty(round(tc["qty"], 1)), "qty_chg": _pct(tc["qty"], tp["qty"]),
            "rate": round(avg_c, 2), "rate_chg": _pct(avg_c, avg_p),
            "bills": tc["bills"], "suppliers": tc["suppliers"],
            "per_day_fmt": fmt_inr(tc["amt"] / span),
        },
        "by_item": items,
        "by_supplier": suppliers,
        "flags": rate_flags(db, start, end),
        "tandoor": tandoor_summary(db, start, end),
    }


def rate_flags(db: Session, start: date, end: date) -> list:
    """Lines whose rate is ≥ RATE_SPIKE_PCT above that item's trailing-median rate
    (the BASELINE_DAYS before the bill, same item, any supplier). Biggest ₹ impact
    first — a spike on a big line matters more than on a small one."""
    window = _lines(db, start - timedelta(days=BASELINE_DAYS), end)
    by_item = {}
    for l in window:
        if l["rate"] > 0:
            by_item.setdefault(l["item"], []).append(l)

    flags = []
    for item, ls in by_item.items():
        ls.sort(key=lambda x: x["date"])
        for l in ls:
            if l["date"] < start:
                continue
            base = [x["rate"] for x in ls
                    if x["date"] < l["date"]
                    and (l["date"] - x["date"]).days <= BASELINE_DAYS]
            if len(base) < BASELINE_MIN_LINES:
                continue
            ref = median(base)
            over = _pct(l["rate"], ref)
            if over is not None and over >= RATE_SPIKE_PCT:
                extra = (l["rate"] - ref) * l["qty"]
                flags.append({
                    "date": l["date"].strftime("%d %b"), "bill": l["bill"],
                    "supplier": l["supplier"], "item": item,
                    "rate": round(l["rate"], 2), "ref": round(ref, 2), "over": over,
                    "qty": fmt_qty(round(l["qty"], 1)),
                    "extra": extra, "extra_fmt": fmt_inr(extra),
                })
    flags.sort(key=lambda f: f["extra"], reverse=True)
    return flags[:MAX_FLAGS]


def _tandoor_kind(name: str):
    """Classify a product name into a Tandoor form, or None if not Tandoor."""
    n = (name or "").lower()
    if "tandoor" not in n:
        return None
    if "live" in n:
        return "Live bird"
    if "without skin" in n or "w/o skin" in n:
        return "Dressed - without skin"
    if "with skin" in n:
        return "Dressed - with skin"
    return "Other"


def tandoor_summary(db: Session, start: date, end: date) -> dict:
    """Tandoor in one place: what we bought (live bird / dressed, direct) and what
    we sold (with / without skin) in the window - qty, amount, effective rate.
    Sales are net of Sales Returns; zero-value internal lines are excluded."""
    bought, sold = {}, {}
    for l in _lines(db, start, end):
        k = _tandoor_kind(l["item"])
        if k:
            b = bought.setdefault(k, [0.0, 0.0, set()])
            b[0] += l["qty"]; b[1] += l["amt"]; b[2].add(l["bill"])

    rows = (db.query(VasySalesItem.product_name, VasySalesItem.sale_type,
                     func.sum(VasySalesItem.qty), func.sum(VasySalesItem.taxable_amount))
            .filter(VasySalesItem.invoice_date != None,               # noqa: E711
                    VasySalesItem.invoice_date >= start,
                    VasySalesItem.invoice_date <= end,
                    VasySalesItem.net_amount != 0)
            .group_by(VasySalesItem.product_name, VasySalesItem.sale_type).all())
    for name, stype, qty, amt in rows:
        k = _tandoor_kind(name)
        if not k:
            continue
        sign = -1 if stype == "Sales Return" else 1
        a = sold.setdefault(k, [0.0, 0.0])
        a[0] += sign * float(qty or 0); a[1] += sign * float(amt or 0)

    nos = {}
    for side, kind, n in (db.query(TandoorNos.side, TandoorNos.kind, func.sum(TandoorNos.nos))
                          .filter(TandoorNos.entry_date >= start, TandoorNos.entry_date <= end)
                          .group_by(TandoorNos.side, TandoorNos.kind).all()):
        nos[(side, kind)] = int(n or 0)

    def pack(d, side, with_bills):
        out = []
        tot_nos = 0
        for k in sorted(d):
            v = d[k]
            n = nos.get((side, k), 0)
            tot_nos += n
            out.append({"kind": k, "side": side, "qty": fmt_qty(round(v[0], 1)),
                        "amt_fmt": fmt_inr(v[1]),
                        "rate": round(v[1] / v[0], 2) if v[0] else 0.0,
                        "bills": len(v[2]) if with_bills else None,
                        "nos": n or None,
                        "avg_wt": round(v[0] / n, 2) if n and v[0] else None})
        return out, sum(v[0] for v in d.values()), sum(v[1] for v in d.values()), tot_nos

    b_rows, b_q, b_a, b_n = pack(bought, "purchase", True)
    s_rows, s_q, s_a, s_n = pack(sold, "sale", False)
    # Window profit = sold revenue - purchased cost (cash view; ignores stock timing,
    # live-bird shrinkage and processing cost). Only meaningful with both sides.
    profit = s_a - b_a
    both = bool(b_rows and s_rows and s_a)
    return {
        "has_data": bool(b_rows or s_rows),
        "profit_ok": both,
        "profit_fmt": fmt_inr(profit),
        "profit_neg": profit < 0,
        "profit_pct": round(profit / s_a * 100, 1) if both else None,
        "bought_nos": b_n or None, "sold_nos": s_n or None,
        "bought": b_rows, "bought_qty": fmt_qty(round(b_q, 1)),
        "bought_amt_fmt": fmt_inr(b_a), "bought_rate": round(b_a / b_q, 2) if b_q else 0.0,
        "sold": s_rows, "sold_qty": fmt_qty(round(s_q, 1)),
        "sold_amt_fmt": fmt_inr(s_a), "sold_rate": round(s_a / s_q, 2) if s_q else 0.0,
    }


TANDOOR_SIDES = ("purchase", "sale")


def tandoor_days(db: Session, side: str, kind: str, start: date, end: date) -> list:
    """Per-day Vasy qty and entered nos for one Tandoor row - feeds the Nos editor.
    Includes every day that has either a Vasy quantity or a saved count."""
    qty = {}
    if side == "purchase":
        for l in _lines(db, start, end):
            if _tandoor_kind(l["item"]) == kind:
                qty[l["date"]] = qty.get(l["date"], 0.0) + l["qty"]
    else:
        q = (db.query(VasySalesItem.invoice_date, VasySalesItem.product_name,
                      VasySalesItem.sale_type, func.sum(VasySalesItem.qty))
             .filter(VasySalesItem.invoice_date != None,               # noqa: E711
                     VasySalesItem.invoice_date >= start,
                     VasySalesItem.invoice_date <= end,
                     VasySalesItem.net_amount != 0)
             .group_by(VasySalesItem.invoice_date, VasySalesItem.product_name,
                       VasySalesItem.sale_type))
        for d, name, stype, qy in q.all():
            if _tandoor_kind(name) == kind:
                qty[d] = qty.get(d, 0.0) + (-1 if stype == "Sales Return" else 1) * float(qy or 0)

    saved = {r.entry_date: r.nos for r in
             db.query(TandoorNos).filter(TandoorNos.side == side, TandoorNos.kind == kind,
                                         TandoorNos.entry_date >= start,
                                         TandoorNos.entry_date <= end).all()}
    return [{"date": d.isoformat(), "label": d.strftime("%d %b"),
             "qty": fmt_qty(round(qty.get(d, 0.0), 1)), "nos": saved.get(d)}
            for d in sorted(set(qty) | set(saved))]


def save_tandoor_nos(db: Session, data: dict):
    """Upsert the nos entries for one (side, kind). Blank / 0 clears that day.
    Returns an error string, or None on success (house convention)."""
    side = (data.get("side") or "").strip()
    kind = (data.get("kind") or "").strip()
    if side not in TANDOOR_SIDES or not kind:
        return "Unknown Tandoor row."
    entries = data.get("entries")
    if not isinstance(entries, list) or len(entries) > 400:
        return "Nothing to save."
    parsed = []
    for e in entries:
        try:
            d = date.fromisoformat(str(e.get("date")))
        except (ValueError, TypeError, AttributeError):
            return "Bad date in entries."
        raw = str(e.get("nos") if e.get("nos") is not None else "").replace(",", "").strip()
        if raw == "":
            n = 0
        else:
            try:
                f = float(raw)
            except ValueError:
                return "Nos must be a whole number."
            if f != int(f) or f < 0 or f > 1_000_000:
                return "Nos must be a whole number."
            n = int(f)
        parsed.append((d, n))

    for d, n in parsed:
        row = (db.query(TandoorNos)
               .filter_by(entry_date=d, side=side, kind=kind).first())
        if n == 0:
            if row:
                db.delete(row)
        elif row:
            row.nos = n
        else:
            db.add(TandoorNos(entry_date=d, side=side, kind=kind, nos=n))
    db.commit()
    return None
