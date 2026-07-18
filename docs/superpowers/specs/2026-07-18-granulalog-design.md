# GranulaLog — Logistics CRM Design Spec

**Date:** 2026-07-18
**Client:** Granula import business (polymer granules imported from Iran, sold locally in Uzbekistan)
**Source material:** client's AI-generated mockup (`GranulaLog_ERP_Mijoz_Qarz_Avans_TAYYOR.html`) + the existing `client-crm` codebase (`/Users/khusan/Desktop/client-crm`) whose stack, UI and selling patterns this project copies.

## 1. What we're building

A Django web app that tracks the full import chain: supplier contracts → payments to suppliers → shipments (loads) with statuses and road/customs expenses → arrival into the warehouse as cost-accurate lots → sales to local customers with debts, advances and reservations → money overview and reports.

## 2. Decisions (agreed in planning interview, 2026-07-18)

| # | Topic | Decision |
|---|-------|----------|
| 1 | Architecture | **Standalone Django project** in `/Users/khusan/Desktop/logistic-crm`, bootstrapped by copying client-crm's structure (config/, accounts app, base.html, app.css, audit pattern, pytest setup). Separate DB, separate deploy. |
| 2 | Currency | **USD canonical everywhere.** Every money column stores USD. Payments/expenses may be handed over in so'm: entered with a per-entry exchange rate, converted to USD at entry; original amount + rate stored (mirror of client-crm's pattern, reversed). |
| 3 | Road/customs expenses | Attach to **exactly one shipment** each. Categories: bojxona, transport, yo'l xarajati, sertifikat, boshqa. They **roll into landed cost**: landed cost per kg = contract price + (shipment expenses ÷ shipment kg). No general (non-shipment) expenses for now. |
| 4 | Customer payments | **Hybrid ledger:** payment belongs to the customer; auto-allocates to unpaid sales oldest-first (FIFO), with optional manual pick of specific sales in the form. Unallocated remainder = advance (avans), auto-applied to future sales. |
| 5 | Supplier payments | **Per-contract, overpay blocked** (mockup behavior kept). One payment targets one contract; cannot exceed remaining contract debt. No supplier prepayments. |
| 6 | Overdue loads | ETA passed + not arrived ⇒ overdue. In-app alerts (red badges, dashboard block, sidebar count) **plus Telegram daily morning digest to one company group** (bot token + chat id in settings; scheduled management command). Extending ETA requires a new date + delay reason; every extension saved as history (old→new, reason, who, when) visible on the shipment. |
| 7 | Stock granularity | **Sell from a specific lot.** A lot = an arrived shipment. Warehouse lists lots with remaining kg and landed cost; a sale picks the lot; cost price snapshot = that lot's landed cost. |
| 8 | Roles | **Admin** — everything. **Translator (Tarjimon)** — talks to drivers: sees ONLY the Shipments section (no money anywhere: no prices, payments, debts, expenses); can move statuses except the final arrival status; can extend ETA with a reason. Only admin can mark a load arrived. |
| 9 | Pre-sale money | All scenarios supported via three independent blocks: **Advance** (money with no link), **Reservation/bron** (customer + kg on an incoming or in-stock lot; blocks over-committing kg; optionally has earmarked payments; optional agreed price as prefill), **Sale** (created on handover; converting a reservation auto-applies earmarked money first, then FIFO advance; unpaid rest = debt with deadline). |
| 10 | Sale shape | **Single-line sales** (one sale = one lot + kg + price), like the mockup. **Returns supported**: credit the customer's debt at sale price, restock kg into the lot. |
| 11 | Kassa | Yes — per-method (naqd/karta/bank) balances in USD, unified chronological in/out feed (customer payments in; supplier payments + expenses out), date filters. |
| 12 | Contract shape | One contract = one brand + kg + unit price + deadline. Multi-brand deals = multiple contracts. |
| 13 | Shipment statuses | **Admin-editable ordered list** stored in DB (seeded with the mockup's six: Tayyorlanmoqda, Yuklanmoqda, Yo'lda, Chegarada, Bojxona, Omborga yetib keldi). Exactly one status is flagged `is_arrival`; setting it is admin-only and turns the shipment into a warehouse lot. |
| 14 | Reports | Filterable report dashboard (date/partner/brand/status), KPI cards, late-shipments table, per-partner and per-customer financial summaries; **Excel (.xlsx) exports** via openpyxl; **print/PDF view**. |
| 15 | Deploy & brand | Railway + Postgres + gunicorn + whitenoise + django-axes, same as client-crm. Brand: **GranulaLog**. UI language: Uzbek. Telegram digest via Railway cron. |
| 16 | Initial data | Excel import required, **format unknown yet** — client will provide files. Build a one-off import management command in Phase 3 once samples arrive. Opening balances supported regardless. |
| 17 | Phasing | Core first, then extras (see §7). |

## 3. Roles & permissions matrix

| Capability | Admin | Translator |
|---|---|---|
| Dashboard, partners, contracts, supplier payments | ✔ | ✖ (redirected to Yuklar) |
| Shipments: view list/detail (kg, transport, container, dates, status — **no money columns**) | ✔ | ✔ |
| Shipment status change (non-arrival statuses) | ✔ | ✔ |
| Set arrival status ("Omborga yetib keldi") | ✔ | ✖ |
| Extend ETA with delay reason | ✔ | ✔ |
| Expenses, warehouse, customers, sales, returns, payments, reservations, kassa, reports, exports | ✔ | ✖ |
| Manage users, statuses | ✔ | ✖ |

## 4. Domain model

Money: `MONEY = Decimal(14,2)` USD. Unit price: `Decimal(14,4)` USD/kg. Quantity: `QTY = Decimal(12,3)` kg. Original-currency amounts: `Decimal(18,2)` (so'm values are large).

Currency entry pattern (shared by all money forms): `currency` (usd|uzs, default usd), `amount` in that currency, `exchange_rate` (so'm per $1, required for uzs). Stored: canonical USD `amount`, plus `currency`, `exchange_rate`, `amount_original`.

### accounts.User
`AbstractUser` + `role` (admin | translator) + `phone`. Property `is_admin_role`.

### crm.Partner (Hamkor — supplier)
name, phone, city, note, created_at. Derived: contract count, total kg, total debt.

### crm.Contract (Kelishuv)
partner FK(PROTECT), brand, kg, price (USD/kg), created (date), deadline (date), note, created_by.
Derived: `total_value = kg × price`; `shipped_kg` (sum of shipments); `remaining_kg`; `paid_total` (sum of supplier payments); `debt = total_value − paid_total`.

### crm.SupplierPayment (To'lov — hamkorga)
contract FK(PROTECT), date, amount (USD), currency/exchange_rate/amount_original, method (naqd|karta|bank), note, created_by, created_at.
Validation: amount ≤ contract debt (editing excludes own old amount).

### crm.ShipmentStatus
name (unique), order, is_arrival (exactly one row true — enforced on save: setting true clears others; the arrival row cannot be deleted; statuses in use cannot be deleted).

### crm.Shipment (Yuk)
contract FK(PROTECT), kg, status FK(PROTECT), sent (date, null), eta (date, null), arrived (date, null), transport, container, note, created_by, created_at.
Validation: kg ≤ contract remaining kg (+own old kg on edit); container unique (case-insensitive) when non-empty; eta ≥ sent.
Derived: `is_arrived` (arrived set), `is_overdue` (not arrived ∧ eta < today), `days_late`, `expenses_total`, `landed_cost_per_kg = contract.price + expenses_total ÷ kg` (4 dp), and — once arrived (it *is* the lot) — `sold_kg`, `reserved_kg`, `available_kg = kg − sold_kg − active reserved_kg (+ restocked returns)`.
Status transitions: translator may set any non-arrival status; admin any. Setting the arrival status stamps `arrived = today` (editable); leaving it clears `arrived`.

### crm.ShipmentDelay
shipment FK(CASCADE, related `delays`), old_eta, new_eta, reason (required), created_by, created_at. Created by the "extend ETA" flow, which also updates `shipment.eta`.

### crm.ShipmentExpense (Yuk xarajati)
shipment FK(CASCADE, related `expenses`), date, category (customs|transport|road|cert|other), amount (USD), currency/exchange_rate/amount_original, note, created_by, created_at. Admin only.

### crm.Customer (Mijoz)
name, phone, address, note, created_at.
Derived: sales total, paid total (allocated + unallocated), balance → qarz (debt) or avans (credit).

### crm.Reservation (Bron) — Phase 2
customer FK(PROTECT), shipment FK(PROTECT — may be in transit or arrived), kg, price (nullable — agreed price, used as prefill on convert), status (active | converted | cancelled), note, created_by, created_at.
Active reservations reduce a lot's available kg and an in-transit shipment's reservable kg. Convert-to-sale: one click on arrival → creates the Sale, marks reservation converted, allocates earmarked payments then FIFO advance.

### crm.Sale (Sotuv) — Phase 2
customer FK(PROTECT), shipment FK(PROTECT — the lot), kg, price (USD/kg), cost_price (USD/kg — snapshot of the lot's landed cost at sale time), date, debt_deadline (nullable), reservation FK(SET_NULL, nullable — origin), note, created_by, created_at.
Derived: `total = kg × price`, `returned_amount`, `net_total`, `paid` (sum of allocations), `remaining`, `is_overdue` (remaining > 0 ∧ deadline passed). Profit = `kg × (price − cost_price)` minus returns' profit share.
Validation: kg ≤ lot available kg.

### crm.Return (Qaytarish) — Phase 2
sale FK(CASCADE), kg, price (defaults to sale price), date, restock (bool, default true — flows kg back into the lot), note, created_by. Credits the customer's debt.

### crm.CustomerPayment (Mijoz to'lovi) — Phase 2
customer FK(PROTECT), date, amount (USD), currency/exchange_rate/amount_original, method, reservation FK(SET_NULL, nullable — earmark), note, created_by, created_at.

### crm.PaymentAllocation — Phase 2
payment FK(CASCADE, related `allocations`), sale FK(CASCADE, related `allocations`), amount (USD).
Invariants: per payment, Σ allocations ≤ payment.amount; per sale, Σ allocations ≤ sale.net_total. A payment's unallocated part is the customer's advance.
`allocate_customer_payment(payment, picks=None)`: manual picks first (validated), else FIFO over the customer's outstanding sales by (date, id). On new sale/conversion: `apply_customer_advance(sale)` pulls earmarked-payment remainders first, then oldest unallocated payment money.

### crm.AuditLog
Copy of client-crm's append-only pattern: user, action (create/update/delete/status/payment/return), target_type, target_id, summary, created_at. Written explicitly from views.

## 5. Pages / navigation (Uzbek, GranulaLog brand, client-crm shell + app.css)

Admin sidebar: Dashboard · Hamkorlar · Kelishuvlar · To'lovlar (hamkor) · Yuklar · Ombor · Mijozlar · Sotuvlar · Qarzlar · Bronlar · Mijoz to'lovlari · Kassa · Hisobotlar · Audit · Foydalanuvchilar · Holatlar (statuses).
Translator sidebar: Yuklar only.

Key screens:
- **Dashboard:** KPI cards (kelishilgan kg, yuborilgan kg, omborga kelgan kg, jami to'langan, hamkor qarzi, kechikkan yuklar), contract progress bars, status counts, overdue table.
- **Yuklar:** search + status filter; row shows contract/partner, kg, status dropdown (permission-aware), transport/container, sent/eta with overdue badge and delay count; actions: status change, extend ETA (modal: new date + reason), expenses (admin), edit/delete (admin). Detail view: delay history + expense list + landed cost (admin only).
- **Ombor:** arrived lots — contract, brand, partner, arrived date, kirim kg, sotilgan kg, bron kg, qoldiq kg, tan narx (landed) — with "Sotish" and "Bron" actions.
- **Mijozlar:** balance column (qarz red / avans green), actions: to'lov, bron, sotuvlar.
- **To'lov modal (customer):** amount+currency+rate, then allocation block — auto (FIFO preview) or manual tick/amount per outstanding sale; remainder shown as "Avans bo'lib qoladi".
- **Kassa:** per-method USD balances + unified dated in/out feed with filters.
- **Hisobotlar:** mockup's filter set (sana, hamkor, marka, holat), KPI cards, late loads, per-partner and per-customer tables, xlsx export buttons, print view.

## 6. Alerts

- Overdue = `arrived IS NULL AND eta < today`. Red "N kun kechikdi" badges (list + dashboard), sidebar count bubble.
- Telegram: management command `send_telegram_digest` (settings `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) posting every morning via Railway cron: overdue loads (days late, transport, container) + loads with ETA today/tomorrow.

## 7. Phases

**Phase 1 — Import core** (plan: `docs/superpowers/plans/2026-07-18-granulalog-phase-1.md`):
skeleton copied from client-crm, auth + admin/translator roles, audit log, partners CRUD, contracts CRUD with progress, supplier payments (overpay-blocked), editable statuses, shipments CRUD + permission-aware status flow, ETA extension with delay history + overdue badges, shipment expenses + landed cost, dashboard v1, translator lockdown.

**Phase 2 — Warehouse & selling** (plan written at phase start):
ombor lots from arrived shipments, customers CRUD, single-line sales with lot cost snapshot + debt deadlines, returns with restock, customer payment ledger + PaymentAllocation (FIFO + manual pick), advances, reservations incl. earmarked payments + convert-to-sale, qarzlar page with overdue debts.

**Phase 3 — Money view & polish** (plan written at phase start):
kassa per-method balances + feed, reports dashboard + filters, xlsx exports, print view, Telegram digest command + cron, user management pages, Excel import command (**blocked on client sample files**), Railway deploy config, seed/demo data, final UI pass.

## 8. Non-goals (explicitly out, per interview)

- Supplier prepayments / supplier ledger (per-contract only, overpay blocked)
- Multi-line sale receipts; multi-shipment expense splitting; general company expenses
- Per-seller ombor/kassa, remittances, profit payouts (client-crm machinery not applicable)
- Email notifications; per-user Telegram linking (single group digest only)
- Multi-language UI (Uzbek only)

## 9. Open items

1. Excel import file formats — waiting on client files (Phase 3).
2. Telegram bot token + group chat id — client to create bot (Phase 3, `.env`).
3. Railway project/domain names — at deploy time.
