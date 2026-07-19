# GranulaLog Phase 3 (Money View & Polish) Implementation Plan

> Execute task-by-task with the subagent-driven loop. Checkbox steps.

**Goal:** Complete the product: a Kassa (till) money overview, a filterable Reports dashboard, Excel exports + print view, user management, the overdue Telegram digest, the live currency-toggle UX polish, an Excel opening-balance importer, and Railway deploy config + a demo seed command.

**Architecture:** Extends the `crm` app. All money canonical USD. Kassa is derived (no new balance table): inflows = customer payments; outflows = supplier payments + shipment expenses; grouped by method (naqd/karta/bank). Reports reuse existing derived properties. Exports via openpyxl (already a dependency). Telegram via a management command run by Railway cron.

## Global Constraints

- Root `/Users/khusan/Desktop/logistic-crm`. Admin-only for all Phase 3 features (translators still see only Yuklar). Money USD via `{{ value|usd }}`.
- Reuse modal helpers, `usd` filter, existing list/detail template conventions, `@role_required(ADMIN)`.
- Tests `pytest`/SQLite. Text files end with newline. Commit per task; trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- External-credential features (Telegram token, Railway secrets, client Excel format) are BUILT and DOCUMENTED but can't be exercised live here — each such task must degrade gracefully (clear settings, no crash when unset) and note the required env var.

---

### Task 1: Kassa (till) — per-method balances + in/out feed

**Files:** modify `crm/views.py`, `config/urls.py`, `templates/base.html`; create `templates/crm/kassa.html`; test `tests/test_kassa.py`.

**Produces:** URL `kassa`. A derived money overview — no new model.

**Money flows (all USD):**
- IN: `CustomerPayment.amount` (grouped by `method`).
- OUT: `SupplierPayment.amount` + `ShipmentExpense.amount` (grouped by `method`).
- Per-method balance = IN(method) − OUT(method), for each of naqd/karta/bank. Total balance = ΣIN − ΣOUT.

- [ ] Step 1: failing test — with one $500 cash customer payment, one $200 cash supplier payment, one $100 cash expense: cash balance == $200, total == $200; a bank customer payment shows under bank; a date-range filter (`?from=&to=`) narrows the feed and totals; translator 403.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement `kassa` view (admin-only): compute per-method IN/OUT/balance via aggregates over the three models (optionally filtered by `?from`/`?to` on `date`); build a unified chronological feed (each row: date, type [Mijoz to'lovi / Hamkor to'lovi / Xarajat], counterparty, method, signed amount) by merging the three querysets sorted by date desc, paginated. Template: method-balance cards (Naqd/Karta/Bank + Jami) using `usd`; date filter; the in/out feed table with green in / red out (reuse `.txn-ic--out`/`.late`/`.avans` or similar existing classes). Nav "Kassa" admin-only.
- [ ] Step 4: tests green; full suite; check clean.
- [ ] Step 5: commit `feat: kassa per-method balances and in/out feed`.

---

### Task 2: Reports dashboard with filters

**Files:** modify `crm/views.py`, `config/urls.py`, `templates/base.html`; create `templates/crm/reports.html`; test `tests/test_reports.py`.

**Produces:** URL `reports`. Filterable KPI + table dashboard mirroring the mockup's Hisobotlar page.

- [ ] Step 1: failing test — reports page (admin-only) with filters `?from&to&partner&brand&status`: KPI cards compute (kelishilgan kg, yuborilgan kg, omborga kelgan kg, kontrakt summasi, hamkorga to'langan, hamkor qarzi, mijoz qarzi, sotuvdan foyda, kechikkan yuk soni); a per-partner table (kg, contract value, paid, debt); a per-customer table (sotildi, to'landi, qarz); a late-shipments table; filters narrow results; translator 403.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement `reports` view: read filters, compute the KPIs and tables from Contracts/Shipments/SupplierPayments (import side) and Sales/CustomerPayments/Returns (sell side). Profit = Σ sale.profit over the period. Template: filter bar (date/partner/brand/status selects), KPI card grid, per-partner financial table, per-customer financial table, late-shipments table. Nav "Hisobotlar" admin-only. Keep money via `usd`.
- [ ] Step 4: tests green; full suite; check.
- [ ] Step 5: commit `feat: reports dashboard with filters and KPIs`.

---

### Task 3: Excel (.xlsx) exports

**Files:** modify `crm/views.py`, `config/urls.py`, `templates/crm/reports.html` (export buttons), possibly `templates/crm/*_list.html`; create `crm/exports.py` (openpyxl helpers); test `tests/test_exports.py`.

**Produces:** URLs `export_contracts`, `export_supplier_payments`, `export_shipments`, `export_sales`, `export_debts` — each streams a `.xlsx` (respecting the same `?from&to&partner&brand&status` filters as reports where applicable).

- [ ] Step 1: failing test — GET `/reports/export/contracts.xlsx` returns 200, `Content-Type` the openpyxl spreadsheet type, a non-empty body, and (load with openpyxl from the response content) a header row + one row per contract; translator 403 on each export.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement `crm/exports.py` with a helper that builds a `Workbook` from (headers, rows) and returns an `HttpResponse` with the right content-type + `Content-Disposition`. One view per dataset (admin-only), reusing the reports filter parsing. Add export buttons to the reports page. Mirror client-crm's export style if present.
- [ ] Step 4: tests green (open the xlsx bytes with openpyxl and assert cells); full suite; check.
- [ ] Step 5: commit `feat: xlsx exports for contracts/payments/shipments/sales/debts`.

---

### Task 4: Print / PDF view

**Files:** modify `templates/crm/reports.html` (print button + print CSS), `static/css/app.css` (a `@media print` block if not present); test `tests/test_reports.py` (a small assertion the print button exists).

**Produces:** a browser-print-friendly reports view (the mockup's 🖨 button) — `window.print()` with a `@media print` stylesheet hiding sidebar/topbar/filters and laying tables out cleanly.

- [ ] Step 1: failing test — reports page contains a print trigger (`onclick="window.print()"` or a `data-print` button).
- [ ] Step 2: FAIL.
- [ ] Step 3: add the print button to reports; add/confirm a `@media print` block in app.css hiding `.sidebar`, `.topbar`, `.fab`, filter bar, and export buttons, and making tables full-width. (Phase 1's app.css may already have print rules — extend, don't duplicate.)
- [ ] Step 4: test green; full suite.
- [ ] Step 5: commit `feat: print/PDF-friendly reports view`.

---

### Task 5: User management (admin CRUD)

**Files:** modify `accounts/views.py`, `accounts/forms.py`, `config/urls.py`, `templates/base.html`; create `templates/accounts/user_list.html`; test `tests/test_users.py`.

**Produces:** URLs `user_list`, `user_create`, `user_edit` — admin creates/edits users, assigning role (admin|translator), setting a password on create.

- [ ] Step 1: failing test — admin creates a translator user (role set, can log in); admin edits a user's role; a non-admin (translator) gets 403 on `/users/`; the creating admin isn't locked out.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement a `UserForm` (username, first/last name, phone, role, password on create) using Django's user-creation patterns (hash the password). Views admin-only, modal convention where it fits (or full-page form — match client-crm's user pages if present). Nav "Foydalanuvchilar" admin-only (Boshqaruv group). Audit target_type "Foydalanuvchi".
- [ ] Step 4: tests green; full suite; check.
- [ ] Step 5: commit `feat: admin user management`.

---

### Task 6: Telegram overdue digest (management command)

**Files:** modify `config/settings.py` (TELEGRAM_* settings), `.env.example`; create `crm/management/commands/send_telegram_digest.py`; test `tests/test_telegram_digest.py`.

**Produces:** management command `send_telegram_digest` that composes the daily overdue/arriving-soon message and POSTs it to a Telegram group. Settings `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (both from env, blank by default).

- [ ] Step 1: failing test — the command's message-building function returns text listing overdue shipments (days late, transport, container) and loads with ETA today/tomorrow; when no token/chat configured, the command logs a clear message and exits 0 WITHOUT attempting a network call (test asserts no send attempted); the digest text is correct for a known fixture. (Test the message builder + the no-config guard; do NOT hit the network — the send function is isolated so it can be asserted/mocked.)
- [ ] Step 2: FAIL.
- [ ] Step 3: implement the command: a `build_digest()` returning the message string (empty/"no overdue" when none), and a `send(text)` that POSTs to `https://api.telegram.org/bot<token>/sendMessage` via `urllib` (stdlib, no new dep) only when token+chat set; `handle()` wires them and guards on missing config. Document the Railway cron in a comment + README note.
- [ ] Step 4: tests green (message builder + guard); full suite; check.
- [ ] Step 5: commit `feat: telegram overdue digest command (guarded on config)`.

---

### Task 7: Currency-toggle UX + money-form polish

**Files:** modify `templates/base.html` (a small JS enhancer) or a new `static` snippet inlined; the money-entry modal templates; test via a template-presence assertion.

**Produces:** the deferred UX: in every money-entry modal (supplier payment, customer payment, shipment expense), selecting So'm reveals the exchange-rate field and shows a live USD preview; selecting Dollar hides the rate. Matches client-crm's currency-toggle behavior.

- [ ] Step 1: failing test — a money modal partial renders the currency select with the enhancer hook (a `data-currency-toggle` attribute or equivalent) and the exchange-rate field marked so JS can show/hide it.
- [ ] Step 2: FAIL.
- [ ] Step 3: add a small vanilla-JS enhancer (bound on `modal:loaded`, like client-crm's other enhancers) that toggles the exchange-rate field's visibility and renders a live `amount ÷ rate = $X` preview when So'm is chosen. Wire the money forms' widgets with the needed attributes/classes. No functional change to submission (server still converts).
- [ ] Step 4: test green; manual note to verify in browser; full suite.
- [ ] Step 5: commit `feat: live currency-toggle in money-entry modals`.

---

### Task 8: Excel opening-balance import (management command)

**Files:** create `crm/management/commands/import_opening.py`, `docs/import-format.md`; test `tests/test_import.py`.

**Produces:** management command `import_opening <file.xlsx>` importing partners, customers, open contracts (with shipped/paid so far), and customer opening balances, from a documented sheet layout. **Blocked on the client's real files** — build against a documented format they can populate; note this clearly.

- [ ] Step 1: failing test — build a small in-memory xlsx (openpyxl) matching the documented layout with a Partners sheet + a Customers sheet; run the command; assert the rows imported (Partner/Customer created with the right fields). Import is idempotent (re-run doesn't duplicate — match on name).
- [ ] Step 2: FAIL.
- [ ] Step 3: implement the command reading named sheets (Partners, Customers, Contracts, OpeningDebts) with documented columns; `get_or_create` by natural key; opening customer debt recorded as a zero-lot adjustment or a documented mechanism (simplest: a note + a CustomerPayment/Sale opening entry — keep it simple and reversible). Write `docs/import-format.md` describing every sheet/column. Log a summary (created/updated counts).
- [ ] Step 4: tests green; full suite; check.
- [ ] Step 5: commit `feat: excel opening-balance import command + documented format`.

---

### Task 9: Deploy config + demo seed + dashboard KPIs + README

**Files:** create `railway.json`, `Procfile` (or confirm gunicorn start), `crm/management/commands/seed_demo.py`, `README.md`; modify `crm/views.py` `dashboard` (add sell-side KPIs), `templates/crm/dashboard.html`.

**Produces:** Railway deploy config (gunicorn + whitenoise, `collectstatic`, `migrate` on release), an idempotent `seed_demo` command (replaces the ad-hoc preview script), extended dashboard KPIs (add: omborda qoldiq kg, mijoz qarzi, sotuvdan foyda), and a README covering setup/run/deploy/env-vars/Telegram cron.

- [ ] Step 1: failing test — `seed_demo` command creates a coherent dataset (partners, contracts, lots, sales, payments, a reservation, an overdue shipment) and is idempotent; dashboard shows the new sell-side KPIs (admin) — assert the KPI labels/values render.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement `seed_demo`; extend `dashboard` view context + template with sell-side KPIs; write `railway.json` (build: pip install + collectstatic; deploy: `migrate` then `gunicorn config.wsgi`), `Procfile`; write `README.md` (local dev with settings_dev/sqlite, Postgres via env, running tests, deploy, env vars incl. TELEGRAM_*, the import command, the Telegram cron). Confirm `DEBUG=False` production security block (Phase 1) still holds.
- [ ] Step 4: tests green; full suite; `manage.py check`; `makemigrations --check` none pending; `collectstatic --dry-run` clean.
- [ ] Step 5: commit `feat: railway deploy config, demo seed, dashboard sell-side KPIs, README`.

---

## Phase 3 exit criteria
- `pytest` green; `manage.py check` clean; no pending migrations; `collectstatic` clean.
- Manual: kassa balances reconcile; reports filter correctly; xlsx downloads open; print view clean; admin manages users; `send_telegram_digest` composes the right message (guarded when unconfigured); currency toggle works in a modal; `import_opening` loads a sample sheet; `seed_demo` populates a demo.
- Whole-branch Phase 3 review, then final full-product review.
- Hand back to user with: what's done, what needs their inputs (Telegram token+chat, Railway secrets, real Excel format), and how to deploy.
