# Monitoring

Three layers, all reporting to the developer's private Telegram chat. None of this
reaches the client's own Telegram digest (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`).

| Layer | What catches it | Where it lives |
|---|---|---|
| App errors (bugs) | `monitoring.handler.TelegramErrorHandler` on the root logger | in the app |
| App / server down | UptimeRobot pinging `/healthz/` and the tarozi TCP port | external |
| Daily report | `manage.py monitor_report` on a Railway cron service | Railway |
| Deploy failed / crashed | Railway's own notifications | Railway settings |

## App errors

Every `ERROR` log record is captured, including unhandled view exceptions, which
Django logs on `django.request`. Each error is grouped by exception type and the
project line that raised it, then stored in `monitoring.ErrorEvent`.

- **First occurrence:** a Telegram alert goes out immediately.
- **Repeats:** at most one alert per hour per error, showing the running count.
- **Alert contents:** the app, the exception, the file and line, the method and
  path, and the username.
- **Left out:** query strings, request bodies and form data.
- **Exception message:** only its first line is sent. A database error's `DETAIL:`
  line, which can quote row values, is dropped.
- **Database down:** a per-process throttle is used instead, so the alert still
  goes out.
- **Ignored:** `DisallowedHost` noise from bots.

## Setup (once per app)

1. **Bot.** Create one bot with @BotFather; it is reused by every app. Send it any
   message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to read your chat id.
2. **Web service variables.** On the web service (`heartfelt-courtesy` in
   `refreshing-rejoicing`), set `MONITOR_TELEGRAM_BOT_TOKEN`,
   `MONITOR_TELEGRAM_CHAT_ID` and optionally `MONITOR_APP_NAME`.
3. **Report cron service.** Add a service from the same repo:
   - Variables: `PROCESS=report`, the two `MONITOR_*` variables, and the same
     `POSTGRES_*` and `SECRET_KEY` variables as the web service.
   - Settings → Cron Schedule: `0 4 * * *` (09:00 Tashkent).
   - It runs, sends, and exits. Billing for cron runs is not confirmed yet; check
     during setup.
   - A failed send exits non-zero, so with `restartPolicyType: ON_FAILURE` from
     `railway.json`, Railway retries it.
4. **UptimeRobot**, or any pinger. Its free-tier limits are not confirmed yet;
   check during setup.
   - An HTTP(s) monitor on `https://<domain>/healthz/`. It expects 200 and gets 503
     when the database is unreachable. HEAD requests work too.
   - A Port monitor on `TAROZI_PUBLIC_ADDR`, the scale receiver's TCP proxy.
   - Alert contact: Telegram, using UptimeRobot's own Telegram integration.
5. **Railway.** Turn on deploy-failed and crash notifications. Where that setting
   lives is not confirmed yet; check during setup. `restartPolicyMaxRetries` means a crash-looping service
   gives up and stays down.

## Daily report

It runs at 09:00 Tashkent and covers **yesterday**, as a local calendar day. It
arrives every day, so a missing report means the cron or the database is broken.
A long report is split into several messages. The sections, in order, come from
`settings.MONITOR_REPORT_SECTIONS`:

- **📈 Business** (`crm.monitoring_report.business`). Each figure reads the row's own
  `date`.
  - Sales: count, kg, change against the 7-day average, turnover per currency, and
    the top 3 customers.
  - Customer payments, supplier payments and all expenses.
  - Trucks sent and arrived.
  - New contracts, customers, brons and returns.
  - What customers owe now, and overdue trucks.
- **👥 Activity** (`activity`). Built from `AuditLog` and the django-axes login log.
  - Logins.
  - Actions per user, with their top record types.
  - Actions by type.
  - The busiest hour, the first and last action, and an hour-by-hour bar chart.
- **🕵️ Worth a look** (`suspicious`):
  - every deletion
  - money records more than 3 days old that were edited
  - money records entered with a date more than 3 days back
  - amounts more than 3× the 30-day average (sales and payments)
  - actions between 22:00 and 07:00
  - failed logins and lockouts
  - user-account changes

  The cut-offs are constants at the top of `crm/monitoring_report.py`.
- **⚙️ Technical** (`monitoring.report.technical`):
  - errors in the last 24h
  - database size and its growth since the previous report (`DailySnapshot`)
  - open connections
- **⚖️ Scales** (`scales`): the state of each active scale.

The report quotes audit summaries, so customer names and amounts appear in it. It goes
only to the developer's private chat.

Preview it without sending:

```
python manage.py monitor_report --dry-run
```

## Reusing in another Django app

1. Copy the `monitoring/` package.
2. Add it to `INSTALLED_APPS`.
3. Copy the `MONITOR_*` and `LOGGING` blocks from `config/settings.py`.
4. Add the `healthz/` route.
5. Write that app's own report sections.
