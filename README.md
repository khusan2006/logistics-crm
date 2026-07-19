# GranulaLog

A logistics CRM for a granula (plastic granule) import/resale business. It
tracks the whole flow: supplier contracts and payments in Iran → shipments in
transit → arrived warehouse lots → sales to local customers, returns, and
customer payments/debts — plus a money overview (Kassa), a filterable reports
dashboard, Excel exports, a Telegram overdue-shipments digest, and role-based
access (admin vs. translator).

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
