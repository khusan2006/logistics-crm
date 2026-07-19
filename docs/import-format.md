# Opening-balance Excel import format

`python manage.py import_opening path/to/opening.xlsx`

> **This format is our own definition.** The client's real historical Excel
> files were not available when this command was built, so this document
> defines the layout they should populate (or that we adapt from once we see
> their actual files). If the client's real files use different sheet names
> or columns, update the column-mapping in
> `crm/management/commands/import_opening.py` and this doc together — the
> row-by-row import logic (get_or_create by natural key, skip-and-log bad
> rows) does not need to change.

## General rules

- The workbook may contain any subset of the four sheets below. A missing
  sheet is skipped with a message — it is not an error.
- Row 1 of every sheet is the header row. Column order does not matter;
  columns are matched by header name, case-insensitive. Extra columns are
  ignored.
- Row 2 onward is data. A completely blank row is skipped silently.
- A row missing a required value (see per-sheet notes) is skipped with a
  logged warning; the rest of the sheet still imports. The command never
  crashes on a single bad row.
- The import is **idempotent**: re-running the same file does not create
  duplicates. Matching is by natural key (see per-sheet notes), using
  `get_or_create`.
- This command seeds **opening balances**, not full transaction history. It
  does not import payments, shipments, sales, or returns.

## Sheet: `Partners`

Suppliers (hamkorlar).

| column | required | notes |
|---|---|---|
| `name` | yes | natural key — matched case-sensitively as stored |
| `phone` | no | |
| `city` | no | |
| `note` | no | |

Matched by `name` (`get_or_create`). If a Partner with that name already
exists, the row is not modified — its existing phone/city/note are kept as-is
(this command only fills in new partners, it does not overwrite existing
data on re-run).

Example:

| name | phone | city | note |
|---|---|---|---|
| Pars Polymer | +98 912 440 1122 | Tehron | yaxshi hamkor |
| Arya Petrochem | +98 21 555 0000 | Shiroz | |

## Sheet: `Customers`

Buyers (mijozlar).

| column | required | notes |
|---|---|---|
| `name` | yes | natural key |
| `phone` | no | |
| `address` | no | |
| `note` | no | |

Matched by `name` (`get_or_create`), same overwrite behavior as Partners.

Example:

| name | phone | address | note |
|---|---|---|---|
| Alisher Trading | +998 90 123 45 67 | Toshkent, Chilonzor | |

## Sheet: `Contracts`

Open (or historical) supplier contracts (kelishuvlar).

| column | required | notes |
|---|---|---|
| `partner` | yes | Partner name; created automatically if it doesn't exist yet |
| `brand` | yes | granula brand/marka |
| `kg` | yes | decimal; agreed kg |
| `price` | yes | decimal; USD per kg |
| `created` | yes | date, ISO format `YYYY-MM-DD` (or an Excel date cell) |
| `deadline` | yes | date, same format |
| `note` | no | |

Matched by `(partner, brand, created)` (`get_or_create`) — this triple is
treated as the natural key so re-importing the same file never duplicates a
contract, while genuinely distinct contracts (same partner+brand signed on
different dates) still import separately.

A row with a missing partner/brand, an unparseable kg/price, or an
unparseable date is skipped with a warning.

Example:

| partner | brand | kg | price | created | deadline | note |
|---|---|---|---|---|---|---|
| Pars Polymer | PP-R | 5000 | 1.25 | 2026-01-10 | 2026-03-01 | birinchi partiya |

## Sheet: `CustomerOpeningDebt`

Customer balances owed **before** this system went live.

| column | required | notes |
|---|---|---|
| `customer` | yes | Customer name |
| `amount` | yes | decimal; USD owed |

### Why this sheet is report-only, not auto-imported

In this system a customer's debt is always the difference between their
`Sale`s and their `CustomerPayment`s — there is no standalone "debt" record.
An opening debt with no corresponding `Sale` has nothing to attach to:
creating a fake `Sale` would need a shipment/lot and would distort real
inventory and profit numbers; creating a `CustomerPayment` can't represent a
debt because payment amounts are always positive (money received, not owed).

So this command does **not** create any financial rows from this sheet. It
only parses and prints each row (`customer — amount`) as a checklist for
manual entry: for each listed customer, enter an opening `Sale` (or a
`Sale` + immediate partial `CustomerPayment`) by hand that reflects the real
pre-launch state, once the client confirms the correct opening figures.

Example:

| customer | amount |
|---|---|
| Alisher Trading | 340.50 |

Running the import against this sheet prints, e.g.:

```
CustomerOpeningDebt: qarzlar avtomatik yozilmaydi (sotuvsiz qarz obyekti yo'q) — quyidagilarni qo'lda kiriting:
  qo'lda kiritish kerak: Alisher Trading — 340.50
CustomerOpeningDebt: 1 qator ro'yxatga olindi (qo'lda kiritish uchun), 0 o'tkazib yuborildi
```

## Command output

The command prints a one-line summary per sheet: created / already-existed /
skipped counts, plus a warning line for every skipped row explaining why.
Nothing is written to the database for skipped rows or for
`CustomerOpeningDebt` entries.
