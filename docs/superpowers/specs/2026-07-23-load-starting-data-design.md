# Load Starting Data — Design

**Date:** 2026-07-23
**Status:** Approved

## Problem

The real Django CRM currently has no canonical starting dataset. An earlier
JS/localStorage prototype ("Demo tizim") produced a concrete dataset — 3
partners, 3 contracts, 3 supplier payments, 4 shipments — that we want to
establish as the **baseline/starting data** in the real database.

Requirements from the user:

- **Wipe** existing business data first — this is starting data, not an addition.
- **One-time load** (run once per environment), but safe to re-run.
- Must be **applicable on production** (Railway), not just the dev SQLite DB.
- Records owned by a new user, **Otabek Yo'ldoshev** (the prototype's `currentUser`).

## Approach

A new Django **management command**: `python manage.py load_starting_data`.

A management command (not a throwaway script) is the correct vehicle because it
must run on prod. It is destructive, so it prompts for confirmation unless
`--noinput` is passed. Everything runs inside a single atomic transaction.

The existing `seed_demo` command (a different, fictional dataset) is left
untouched.

## Behaviour (ordered, atomic)

### 1. Wipe existing business data

Delete in FK-safe order (children before parents):

```
PaymentAllocation → Return → CustomerPayment → Sale → Reservation →
ShipmentExpense → ShipmentDelay → ShipmentLeg → Shipment →
SupplierPayment → Contract → Customer → Partner → AuditLog
```

**Kept (not wiped):**

- **`ShipmentStatus`** — reference/config data seeded by migration `0006`. The
  shipment statuses in the dataset ("Yo'lda", "Chegarada", "Bojxona",
  "Tayyorlanmoqda") are looked up **by name** from these rows; wiping them would
  break the load.
- **Users** — auth accounts. Wiping login accounts (especially on prod) is
  dangerous and is not really "business data". Existing users are kept; only
  Otabek is added.

### 2. Create owner user

`Otabek Yo'ldoshev` — username `otabek`, `role=admin`, `is_staff=True`,
`is_superuser=True`. Created via `get_or_create` on the username so re-runs don't
duplicate. On creation, a temporary password `otabek12345` is set and **printed
loudly** with a recommendation to change it (especially on prod). All records
below are attributed to this user via `created_by`.

### 3. Load the dataset

Loaded exactly as in the prototype JSON.

**Partners (3):**

| name | phone | city | note |
|---|---|---|---|
| Pars Polymer Co. | +98 912 440 1122 | Tehron | Asosiy yetkazib beruvchi |
| Arya Petrochem | +98 917 201 8877 | Shiroz | HDPE va LDPE |
| Toshkent Polimer Savdo | +998 90 555 44 33 | Toshkent | Mahalliy hamkor |

**Contracts (3):**

| partner | brand | kg | price | created | deadline |
|---|---|---|---|---|---|
| Pars Polymer Co. | LLDPE 209AA | 50000 | 0.96 | 2026-07-28 | 2026-07-28 |
| Arya Petrochem | HDPE 7000F | 30000 | 1.05 | 2026-08-05 | 2026-08-05 |
| Pars Polymer Co. | LDPE 2420H | 20000 | 1.12 | 2026-08-12 | 2026-08-12 |

**Supplier payments (3):** method mapped "Bank o'tkazmasi" → `transfer`,
"Naqd" → `cash`.

| contract (brand) | amount | date | method |
|---|---|---|---|
| LLDPE 209AA | 18000 | 2026-07-02 | transfer |
| LLDPE 209AA | 12000 | 2026-07-09 | transfer |
| HDPE 7000F | 10000 | 2026-07-11 | cash |

**Shipments (4):** status string mapped to `ShipmentStatus` by name; empty date
strings (`""`) → `NULL`; `logist` stored in `note`.

| contract (brand) | kg | status | sent | eta | transport | container | note |
|---|---|---|---|---|---|---|---|
| LLDPE 209AA | 20000 | Yo'lda | 2026-07-06 | 2026-07-19 | 01 777 AAA | MSCU-442109 | Logist: Akmal |
| LLDPE 209AA | 15000 | Chegarada | 2026-07-08 | 2026-07-17 | 10 888 BBB | TGHU-771200 | Logist: Javlon |
| HDPE 7000F | 12000 | Bojxona | 2026-07-03 | 2026-07-14 | 01 909 CCC | CAIU-902811 | Logist: Akmal |
| LDPE 2420H | 8000 | Tayyorlanmoqda | (null) | 2026-07-29 | (blank) | (blank) | Logist: Javlon |

## Handling unmapped data

- **`logist`** (Akmal / Javlon) — no model field → stored as
  `note = "Logist: <name>"` on each shipment (preserved rather than dropped).
- **`audit`** log — freeform text that doesn't fit `AuditLog`'s constrained
  schema (`action` is a max-10 choices field) → **skipped**; it repopulates
  naturally as users act.
- **`settings.usdRate` (12649)** — no settings model, and all payments are USD →
  **skipped**.
- **`sales`, `cashEntries`, `debtPayments`** — empty in the source → nothing to
  load.

## Faithfulness note

The prototype data has some internal oddities: contract `created` dates equal
their `deadline`, and some supplier payments predate the contract's `created`
date. These are loaded **as-is**, not "corrected".

## Idempotency

Because the command wipes-then-loads, re-running resets the DB to exactly this
baseline. This satisfies "one-time load" (run once per environment) while
remaining safe to re-run.

## Safety

- Destructive: prompts `Type 'yes' to continue` unless `--noinput` is given.
- Atomic: the whole wipe+load runs in one `transaction.atomic()` block, so a
  failure leaves the DB untouched.

## Testing

A pytest test (following the existing `tests/` layout) that runs the command
with `--noinput` against a fresh test DB and asserts the resulting row counts
(3 partners, 3 contracts, 3 supplier payments, 4 shipments, Otabek user exists),
plus a spot-check that method/status strings mapped correctly and that a re-run
leaves counts unchanged.

## Out of scope

- Importing the `audit` trail, `usdRate` setting, or `logist` as a first-class
  field/model (would require schema changes; not requested).
- Any change to the existing `seed_demo` command.
