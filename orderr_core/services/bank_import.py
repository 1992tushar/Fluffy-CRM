"""
Bank statement import for the 5-Day Close bank reconciliation.

Parses a downloaded bank-statement CSV (Kotak format seen in production) into
BankTransaction rows. Robust to the metadata/header/footer noise banks put
around the data, and idempotent (dedupe_key), so re-uploading an overlapping
statement updates rather than duplicates.

Kept separate from vasy_import.py on purpose (different source, different owner).
"""
import csv
import io
from datetime import date, datetime

from sqlalchemy.orm import Session

from orderr_core.models.bank_transaction import BankTransaction
from orderr_core.models.import_log import ImportLog
from orderr_core.services.customer_service import _to_amount


def _parse_date(s):
    """Parse a bank date cell; takes the date part of 'DD/MM/YYYY HH:MM' etc."""
    if not s:
        return None
    token = str(s).strip().split(" ")[0]          # drop any time component
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    return None


def _decode(file_bytes: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="ignore")


def _locate_header(rows):
    """Find the header row index and a column map. Returns (idx, colmap) or
    (None, None). Matches on the distinctive 'value date' + 'amount' + a
    direction column."""
    for i, row in enumerate(rows[:40]):
        low = [str(c).strip().lower() for c in row]
        if "amount" in low and any("value date" == c for c in low):
            col = {}
            for j, c in enumerate(low):
                if c == "value date":
                    col["value_date"] = j
                elif c == "transaction date":
                    col["txn_date"] = j
                elif c == "description":
                    col["desc"] = j
                elif "ref" in c or "chq" in c:
                    col.setdefault("ref", j)
                elif c == "amount":
                    col["amount"] = j
                elif c in ("dr / cr", "dr/cr", "cr/dr", "type"):
                    # first one after amount = txn direction; second = balance dir
                    if "amount" in col and j > col["amount"] and "dir" not in col:
                        col["dir"] = j
                elif c == "balance":
                    col["balance"] = j
            if "value_date" in col and "amount" in col and "dir" in col:
                return i, col
    return None, None


def import_bank_statement(db: Session, file_bytes: bytes, source_file: str = None) -> dict:
    """Import a bank-statement CSV into BankTransaction (idempotent)."""
    text = _decode(file_bytes)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("The bank statement file is empty.")

    header_idx, col = _locate_header(rows)
    if header_idx is None:
        raise ValueError("Couldn't find the statement header (need 'Value Date', "
                         "'Amount' and a 'Dr / Cr' column). Is this the right CSV?")

    def cell(row, key):
        i = col.get(key)
        return row[i] if (i is not None and i < len(row)) else None

    existing = {k for (k,) in db.query(BankTransaction.dedupe_key).all()}
    created = skipped = 0
    in_total = out_total = 0.0
    dmin = dmax = None

    for row in rows[header_idx + 1:]:
        if not row or not str(row[0]).strip().isdigit():
            continue                                  # footer / blank / non-data
        vdate = _parse_date(cell(row, "value_date"))
        if vdate is None:
            continue
        amt = float(_to_amount(cell(row, "amount")))
        if amt == 0:
            continue
        drcr = str(cell(row, "dir") or "").strip().lower()
        direction = "cr" if drcr.startswith("cr") else "dr"
        ref = str(cell(row, "ref") or "").strip() or None
        desc = str(cell(row, "desc") or "").strip() or None
        bal = float(_to_amount(cell(row, "balance"))) if cell(row, "balance") else None

        # ref_no alone isn't always unique: Kotak reuses the same batch/session
        # ref (e.g. clearing-instrument "NCROUT_..." refs) across multiple
        # distinct instruments cleared together, which silently collapsed
        # same-day/same-amount/same-direction transactions into one row.
        # Folding in description (which carries the actual instrument number)
        # keeps those distinct.
        key = f"{vdate.isoformat()}|{ref or ''}|{amt:.2f}|{direction}|{desc or ''}"
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        db.add(BankTransaction(
            value_date=vdate, txn_date=_parse_date(cell(row, "txn_date")),
            description=desc, ref_no=ref, amount=amt, direction=direction,
            balance=bal, dedupe_key=key, source_file=source_file,
        ))
        created += 1
        if direction == "cr":
            in_total += amt
        else:
            out_total += amt
        dmin = vdate if dmin is None or vdate < dmin else dmin
        dmax = vdate if dmax is None or vdate > dmax else dmax

    db.add(ImportLog(entity="bank", source_file=source_file, rows_total=created + skipped,
                     created=created, updated=0, unmatched=0,
                     notes=(f"in={round(in_total,2)}; out={round(out_total,2)}; "
                            f"skipped_dupes={skipped}; "
                            f"range={dmin.isoformat() if dmin else '-'}..{dmax.isoformat() if dmax else '-'}")))
    db.commit()

    return {
        "entity": "bank",
        "rows": created + skipped,
        "created": created,
        "skipped_duplicates": skipped,
        "money_in": round(in_total, 2),
        "money_out": round(out_total, 2),
        "from": dmin.isoformat() if dmin else None,
        "to": dmax.isoformat() if dmax else None,
    }
