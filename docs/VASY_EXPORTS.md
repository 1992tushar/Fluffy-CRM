# Vasy → OrdeRR Analytics — Export & Auto-Import Spec

Reference for feeding the OrdeRR analytics layer from Vasy ERP. Written so an
external bot can produce these files and push them directly to OrdeRR's import
endpoints. **Vasy is the source of truth for money; OrdeRR only mirrors it.**

## How the importers behave

- Columns are matched by **header label (case-insensitive); column order and
  extra columns don't matter.** The header row may sit below title rows (the
  importer scans the first ~15 rows).
- A footer **`Total`** row (blank/`Total` in the key column) is auto-skipped.
- Amounts may carry `₹`, commas, or a non-breaking space before a minus sign —
  all handled. Dates accept `DD/MM/YYYY` or `YYYY-MM-DD`.
- **Ledgers** (receipts, sales-invoices, purchases, expenses, payments) upsert
  on their document key → **cumulative & idempotent**: send any date range;
  re-sending overlaps updates, never duplicates. A document **deleted in
  Vasy** is also removed on re-send, but only within the date span the
  re-sent file actually covers (min..max date in that file) — a document
  outside that window is never touched, so a partial-range re-upload can't
  wipe out unrelated history.
- **Outstanding** is a **daily snapshot** stamped "today" → send **once per day**
  (that's what builds the balance trend / rising-falling / sharper credit scores).
- Format: `.xlsx` (or `.xlsm`).

## Upload protocol (how the bot pushes)

`POST {BASE_URL}/dashboard/analytics/import/<entity>`
- `multipart/form-data`, form field name **`file`**, body = the `.xlsx`.
- Response `200 {"status":"ok","summary":{…}}` or `400 {"detail":"…"}`.
- `BASE_URL` = the Render prod URL.
- **Auth:** the dashboard's `require_auth` is currently a **no-op** — no
  credentials needed today. If HTTP Basic is re-enabled later, add a Basic-auth
  header (ask the maintainer for the exact creds/header at that time).

```
curl -F "file=@outstanding.xlsx" {BASE_URL}/dashboard/analytics/import/outstanding
```

## The 7 exports

| # | Export | Vasy source | Endpoint (`/dashboard/analytics/import/…`) | Key | Cadence |
|---|---|---|---|---|---|
| 1 | Receipts (money in) | Bank/Cash → Receipt | `receipts` | Receipt No. | any range |
| 2 | Customer Outstanding (AR) | Reports → Outstanding | `outstanding` | Party Name (per day) | **daily** |
| 3 | Sales Invoices (revenue) | Sales invoice export | `sales-invoices` | Voucher No | any range |
| 4 | Purchases (COGS) | Purchase export | `purchases` | Bill No | any range |
| 5 | Expenses (opex) | Expense export | `expenses` | Expense No. | any range |
| 6 | Payments (money out) | Bank/Cash → Payment | `payments` | Payment No | any range |
| 7 | Supplier Outstanding (AP) | Supplier Bill List | `supplier-outstanding` | Bill No | re-send regularly |

### Columns per export

Headers exactly as Vasy emits them. **Required** = importer errors without it;
the rest are read when present.

**1. Receipts** — `receipts`
`# · Receipt No.* · Party Name* · Mode · Date · Amount · Status · Created By`

**2. Customer Outstanding** — `outstanding`  *(daily snapshot)*
`# · Party Name* · Contact No. · Location · Opening Balance · Debit · Credit · Closing*`

**3. Sales Invoices** — `sales-invoices`  *(line-item; grouped by Voucher No; has a footer Total row)*
`Sr. No · Date · Voucher No* · Branch · Party Name* · Mobile No. · Category Name · Item Code · QTY · Net Amount · Sales Man · Receipt Data · Created By · Address · Description · Note`

**4. Purchases** — `purchases`  *(line-item; grouped by Bill No; footer Total row)*
`Sr No · Bill Date · Bill No* · Voucher No · Party Name · GST No · Pan No · HSN · Product Name · Item Code · Rate · QTY · Total Amount* · Location · Total Bill Amount · Created By`

**5. Expenses** — `expenses`  *(footer Total row)*
`Sr. No. · Expense No.* · Expense Date · Party Name · Total · Paid · UnPaid · Branch · Created By · Created From`

**6. Payments** — `payments`
`# · Payment No* · Party Name · Payment Mode · Date · Amount · Status · Created By`

**7. Supplier Outstanding** — `supplier-outstanding`  *(bill-level AP; footer Total row)*
`Status · Bill No* · Bill Date · Vendor · Amount · Paid Amount · Due Amount* · Tax Amount · Due Date · Created By`

`*` = required.

## Recommended routine

1. **One-time backfill:** for the ledgers (1, 3, 4, 5, 6) and AP (7), send the
   widest date range Vasy allows — history populates in one shot.
2. **Daily:** send yesterday (overlap is safe) for all; **Outstanding every day**.
3. **For a correct P&L,** keep Sales Invoices + Purchases + Expenses over the
   **same date range** (else the Financials page shows its coverage warning).

Check **Analytics → Imports** any time to see per-entity coverage (rows, date
range, staleness) and import history.

## Known data gap (not yet supported)

- **Customer AR open-bills / aging export** (the customer-side equivalent of #7):
  would upgrade AR aging (P2-12) from a payment-recency proxy to true 30/60/90
  aging + DSO. If Vasy has a "Customer Bill List" (invoice-level, with due
  dates), send a sample and the maintainer will add its importer + endpoint.
