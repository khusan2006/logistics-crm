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
- **Caveat:** the exception *message* is sent as-is and can occasionally include a
  value.
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
   - It runs, sends, and exits. It is billed only while it runs.
4. **UptimeRobot** (free tier, 5-minute checks):
   - An HTTP(s) monitor on `https://<domain>/healthz/`. It expects 200 and gets 503
     when the database is unreachable.
   - A Port monitor on `TAROZI_PUBLIC_ADDR`, the scale receiver's TCP proxy.
   - Alert contact: Telegram, using UptimeRobot's own Telegram integration.
5. **Railway.** Turn on deploy-failed and crash notifications in the account
   notification settings. `restartPolicyMaxRetries` means a crash-looping service
   gives up and stays down.

## Daily report

The report arrives every day, errors or not. A day without a report means the cron
or the database is broken.

Contents:

- errors seen in the last 24h, split into new and recurring, top 5 by count
- database size and open connections
- users who logged in
- `AuditLog` activity by action
- scale status

App-specific sections are listed in `settings.MONITOR_REPORT_SECTIONS`.

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
