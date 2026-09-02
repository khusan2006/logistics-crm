# GranulaLog

A logistics CRM for a granula (plastic granule) import/resale business. It
tracks the whole flow: supplier contracts and payments in Iran → shipments in
transit → arrived warehouse lots → sales to local customers, returns, and
customer payments/debts — plus a money overview (Kassa), a filterable reports
dashboard, Excel exports, a Telegram overdue-shipments digest, and role-based
access (admin vs. translator).

## Birja

Not every load comes from Iran any more: some granula is bought on the exchange
(**birja**) inside Uzbekistan. Those purchases get two screens of their own —
**Birja kelishuvlar** and **Birja yuklar** — and nothing else. Everything
downstream stays one set of books: a birja load lands in the same Ombor, is sold
through the same Sotuvlar, and its money runs through the same Kassa, Qarzlar
and Hisobotlar.

That works because a birja kelishuv **is** a `Contract` and a birja yuk **is** a
`Shipment`. What separates them is the counterparty: a singleton `Partner` row
flagged `is_birja`, created on first use by `crm.models.birja_partner()`. It also
mints the codes — `slugify("Birja")` is `birja`, so the existing per-hamkor
counter hands out `birja-1`, `birja-2`, … the same way it hands out `sobir-3`.

A birja load carries no **QR kod** and no **bojxona**: it never crossed a border,
so those fields are off its form, off its list, and excluded from the "Bojxona
to'lanmagan" group. Its holat chain is separate too — `ShipmentStatus.scope` is
`hamkor`, `birja` or `umumiy`, and the arrival status is the one row both chains
share, since reaching it is what turns any yuk into a warehouse lot. The birja
statuses ship as placeholders (**Sotib olindi → Yuklandi → Yetkazilmoqda →
Omborga yetib keldi**) and are meant to be renamed on the Holatlar page once the
real chain is known.

Its transport is not typed per truck at all. A birja haydovchi is paid so much a
kilo of what he brings in, and paid when he brings it — three facts the operator
already maintains — so a birja **kelishuv** carries a `transport_rate_per_kg`
(entered in its Xarajatlar modal, see below) and `sync_birja_transport` turns it
into a xarajat on each of its yuklar as they land: rate × the yuk's kg, dated the
day it arrived.

The row is derived, not entered, and behaves like it. Move the arrival date and
it moves; take the yuk back off arrival and it goes, the way a haydovchi avansi
goes when its logist is removed. Correcting the rate on the kelishuv re-prices
every yuk under it, landed ones included — the opposite of what a hand-entered
`CashEntry` does, and right here precisely because nobody typed this one. Only
the kurs is kept once booked, so an edit elsewhere never restates the so'm value
of cash already handed over.

It hangs off `Shipment.save()`, which all four arrival writers pass through, plus
a second call from the yuk views once the mahsulot rows exist (a yuk being
created has no kg yet at its first save) and one from the kelishuv form when the
rate itself changes. The row is flagged `is_auto_transport`, which keeps every
by-hand screen off it: the xarajat grid will not show or delete it, and
`expense_edit`/`expense_delete` send the operator to the kelishuv instead of
letting them make a correction the next sync would silently undo.

Nothing was needed for the kassa timing: Transport is already in
`ShipmentExpense.ARRIVAL_CATEGORIES`, and the row is dated the arrival, so
`cash_date` is that day. The Eron road is untouched — there a logist quotes the
run and hands the driver an avans out of it, and the rate field is not on its
kelishuv form.

The Birja pages are admin-only — a tarjimon's job is the Iran road.

## Kelishuv xarajatlari

Some costs belong to the AGREEMENT, not to any one truck. A broker is paid a
percentage of the whole kelishuv, once, for the kelishuv. Before `ContractExpense`
every xarajat had to hang off a yuk, so such a cost was either left out of the
books or pinned to whichever truck happened to be open — inflating that one load's
tannarx and every foyda taken off it.

They are entered from the **Xarajatlar** action on a kelishuv row (a kelishuv has
no detail page; the row is the kelishuv, the same way its To'lov and Tahrirlash
modals work). This is the one screen for "what does this agreement cost us", and
the turkum decides the shape:

- **Broker** — a percentage of the kelishuv's full value, booked in the kelishuv's
  own currency.
- **Transport** (birja only) — a price per kg. The odd one out: it saves no row
  here at all. It writes `Contract.transport_rate_per_kg`, and the money appears
  later on each yuk as it lands. It shows in the table as a row that states what it
  does and how many yuklar it has already been written to.
- **Boshqa** — a sum somebody was quoted.

The picker hides the boxes that do not apply and `clean` refuses them, so a figure
can never be read back as the wrong kind of number.

The money leaves the kassa **once**, at kelishuv level — its own `Kelishuv
xarajatlari` bar in the Oqim and its own row in the Chiqim daftar. Each yuk carries
its share through `Contract.expenses_per_kg` → `ShipmentLine.landed_cost_per_kg`,
which is the same spread the vositachi cut has always had and the one funnel every
foyda in the app reads. Nothing is pushed down as a `ShipmentExpense`: a copy on
every truck would put one payment in the kassa N+1 times.

`sync_contract_expenses` re-multiplies the percentage rows when the kelishuv's own
value moves — a marka added, a narx corrected. Sums somebody typed are left where
they were put.

This is deliberately **not** the vositachi cut, which stays exactly where it is on
the hamkor to'lovlar (`SupplierPayment.commission_percent`, live on most of them).
That one is a slice of each payment as it goes out; this is a share of the
agreement. Both spread per kg, and they add up side by side in the tannarx.

`Contract.expenses_total` and `expenses_per_kg` count only the rows that ARE money
here — a transport rate is not among them, because its money is booked per yuk and
counting it twice is exactly the mistake this separation exists to prevent.

## Stack

Django 6, Postgres (production) / SQLite (local preview), gunicorn +
whitenoise, django-axes (brute-force login protection), openpyxl (Excel
exports/import). All money is stored and displayed in USD.

## Local development

### Option A — SQLite preview (fastest, no Postgres needed)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python manage.py migrate --settings=config.settings_dev
python manage.py seed_demo --settings=config.settings_dev   # optional demo data
python manage.py createsuperuser --settings=config.settings_dev  # or use the seed_demo admin login
python manage.py runserver --settings=config.settings_dev
```

`config/settings_dev.py` inherits the real settings and only swaps the
database for a local `dev.sqlite3` file, so it exercises the same
apps/middleware/templates as production.

### Option B — Postgres via `.env`

Copy `.env.example` to `.env` and fill in the values (see
[Environment variables](#environment-variables) below), make sure Postgres is
running and the database/user exist, then:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python manage.py migrate
python manage.py seed_demo   # optional demo data
python manage.py createsuperuser
python manage.py runserver
```

## Running tests

```bash
pytest
```

Tests run against `config.settings_test` (an isolated SQLite database) via
`pytest.ini` / `pytest-django`. A few tests drive a real browser with
Playwright — after installing dev requirements, run once:

```bash
python -m playwright install chromium
```

## Demo data (`seed_demo`)

```bash
python manage.py seed_demo --settings=config.settings_dev   # or without the flag on Postgres
```

Idempotent — safe to re-run; it won't duplicate rows. It creates a coherent
demo dataset for previews/manual QA:

- 2 partners (suppliers) and 2 contracts
- supplier payments against those contracts
- an arrived warehouse lot with customs/transport expenses
- an in-transit shipment and an overdue shipment
- 2 customers, a sale (partially paid, leaving a debt) and a second customer
  with an unallocated advance payment
- a reservation (bron) on the in-transit lot
- two login users:

  | Username    | Password         | Role       |
  |-------------|------------------|------------|
  | `admin`     | `admin12345`     | Admin      |
  | `tarjimon`  | `tarjimon12345`  | Translator |

## Roles

- **Admin** — full access: contracts, shipments, warehouse, sales, payments,
  reports, Kassa, exports, user management, audit log.
- **Translator** (`tarjimon`) — restricted to the Yuklar (shipments) list/detail
  only; every admin-only view redirects or 403s a translator.

## Opening-balance import (`import_opening`)

```bash
python manage.py import_opening path/to/opening.xlsx
```

Seeds Partners, Customers, and open Contracts from an `.xlsx` file. See
[`docs/import-format.md`](docs/import-format.md) for the exact sheet/column
layout — it's our own defined format (the client's real historical files
weren't available yet); adapt the column mapping there and in
`crm/management/commands/import_opening.py` together once real files arrive.

## Telegram overdue-shipments digest

```bash
python manage.py send_telegram_digest
```

Composes and sends a daily digest of overdue and soon-arriving shipments to a
Telegram chat. Degrades gracefully when `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` are unset — it prints a notice and exits without any
network call. In production, schedule it once a day (e.g. Railway's cron job
feature, schedule `0 4 * * *` for 04:00 UTC).

## Environment variables

Set these in `.env` locally (see `.env.example`) or as Railway service
variables in production:

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | yes | Django secret key. |
| `DEBUG` | yes | `True`/`False`. Must be `False` in production — enables the HTTPS/HSTS/secure-cookie block in `config/settings.py`. |
| `ALLOWED_HOSTS` | yes | Comma-separated hostnames, e.g. `crm.example.com`. |
| `CSRF_TRUSTED_ORIGINS` | production | Comma-separated origins, e.g. `https://crm.example.com`. Needed behind a TLS-terminating proxy. |
| `POSTGRES_DB` | production | Database name. |
| `POSTGRES_USER` | production | Database user. |
| `POSTGRES_PASSWORD` | production | Database password. |
| `POSTGRES_HOST` | production | Database host. |
| `POSTGRES_PORT` | production | Database port (default `5432`). |
| `TELEGRAM_BOT_TOKEN` | optional | Enables `send_telegram_digest`. Leave blank to disable. |
| `TELEGRAM_CHAT_ID` | optional | Chat/channel id the digest is sent to. Leave blank to disable. |

There is no `OMBOR` env var — the warehouse (Ombor) view is derived entirely
from shipment/sale/reservation data, nothing to configure.

## Deploying to Railway

The repo ships a `railway.json` (primary) and a `Procfile` (fallback, same
command) so Railway's Nixpacks builder knows what to do:

- **Build:** `pip install -r requirements.txt && python manage.py collectstatic --noinput`
- **Start:** `python manage.py migrate --noinput && gunicorn config.wsgi --bind 0.0.0.0:$PORT`

Steps:

1. Create a Railway project, add a Postgres plugin, and add this repo as a
   service.
2. Set the [environment variables](#environment-variables) above on the
   service (at minimum `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`,
   `CSRF_TRUSTED_ORIGINS`, and the `POSTGRES_*` vars — Railway's Postgres
   plugin can supply these via reference variables).
3. Deploy. Railway runs the build command, then the start command, which
   migrates the database and boots gunicorn bound to Railway's `$PORT`.
4. Optionally schedule `python manage.py send_telegram_digest` as a daily
   Railway cron job once `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set.
5. Optionally run `python manage.py seed_demo` once (via Railway's shell/run
   command) to populate a demo dataset, or `python manage.py import_opening
   path/to/opening.xlsx` to load real opening balances.
