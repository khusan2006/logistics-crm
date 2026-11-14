"""A logging handler that turns ERROR records into Telegram alerts.

Wired in settings.LOGGING on the root logger, so it sees unhandled view exceptions
(Django logs them on `django.request`) and any `logger.error(...)` in the code.

Per error kind: the first occurrence alerts at once, repeats alert at most once per
ALERT_EVERY. Occurrences are counted in the ErrorEvent table, which the daily report
reads. When the database itself is the problem, a per-process throttle is used
instead, so the alert still goes out."""

import hashlib
import logging
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path

from django.conf import settings

ALERT_EVERY = timedelta(hours=1)

_guard = threading.local()
_memory_throttle = {}  # fingerprint -> time.monotonic() of last alert, used when the DB is down
_memory_lock = threading.Lock()


def _app_frame(tb):
    """The deepest traceback frame inside this project (not in site-packages)."""
    base = str(settings.BASE_DIR)
    frames = traceback.extract_tb(tb)
    ours = [f for f in frames if f.filename.startswith(base) and "site-packages" not in f.filename]
    frame = (ours or frames or [None])[-1]
    if frame is None:
        return ""
    path = Path(frame.filename)
    try:
        path = path.relative_to(base)
    except ValueError:
        pass
    return f"{path}:{frame.lineno} in {frame.name}"


def describe(record):
    """What an alert needs from a log record. Request bodies and query strings are
    left out on purpose: they can carry customer data."""
    if record.exc_info and record.exc_info[0]:
        exc_type, exc_value, tb = record.exc_info
        title = f"{exc_type.__name__}: {exc_value}"
        location = _app_frame(tb)
        key = f"{exc_type.__module__}.{exc_type.__qualname__}|{location}"
    else:
        title = record.getMessage()
        location = f"{record.pathname}:{record.lineno}"
        key = f"{record.name}|{record.msg}|{location}"

    request = getattr(record, "request", None)
    path = method = user = ""
    if request is not None and hasattr(request, "path"):
        path = request.path
        method = getattr(request, "method", "")
        u = getattr(request, "user", None)
        if u is not None and getattr(u, "is_authenticated", False):
            user = u.get_username()

    return {
        "fingerprint": hashlib.sha1(key.encode("utf-8")).hexdigest(),
        "title": title[:300],
        "location": location[:300],
        "logger": record.name[:100],
        "path": f"{method} {path}".strip()[:300],
        "user": user,
    }


def record_event(info):
    """Count the occurrence. Returns (should_alert, total_count)."""
    from django.utils import timezone

    from monitoring.models import ErrorEvent

    now = timezone.now()
    event, created = ErrorEvent.objects.get_or_create(
        fingerprint=info["fingerprint"],
        defaults={"title": info["title"], "location": info["location"],
                  "logger": info["logger"], "last_path": info["path"],
                  "first_seen": now, "last_seen": now, "last_alerted": now},
    )
    if created:
        return True, 1

    should_alert = event.last_alerted is None or now - event.last_alerted >= ALERT_EVERY
    event.count += 1
    event.last_seen = now
    event.last_path = info["path"] or event.last_path
    if should_alert:
        event.last_alerted = now
    event.save(update_fields=["count", "last_seen", "last_path", "last_alerted"])
    return should_alert, event.count


def _memory_should_alert(fingerprint):
    now = time.monotonic()
    with _memory_lock:
        last = _memory_throttle.get(fingerprint)
        if last is not None and now - last < ALERT_EVERY.total_seconds():
            return False
        _memory_throttle[fingerprint] = now
        return True


def format_alert(info, count, db_note=""):
    lines = [f"🔴 {settings.MONITOR_APP_NAME} — error"]
    if count > 1:
        lines[0] += f" (seen {count}x)"
    lines.append(info["title"])
    if info["location"]:
        lines.append(f"📍 {info['location']}")
    if info["path"]:
        lines.append(f"🌐 {info['path']}")
    if info["user"]:
        lines.append(f"👤 {info['user']}")
    if db_note:
        lines.append(db_note)
    return "\n".join(lines)


class TelegramErrorHandler(logging.Handler):
    def __init__(self, level=logging.ERROR):
        super().__init__(level)

    def emit(self, record):
        # An error raised while handling an error must not loop back in here.
        if getattr(_guard, "busy", False):
            return
        _guard.busy = True
        try:
            from monitoring import telegram

            info = describe(record)
            db_note = ""
            try:
                should_alert, count = record_event(info)
            except Exception as exc:  # noqa: BLE001 — the DB may be what is broken
                should_alert, count = _memory_should_alert(info["fingerprint"]), 1
                db_note = f"⚠️ could not record it in the DB: {type(exc).__name__}"
            if should_alert:
                telegram.send_in_background(format_alert(info, count, db_note))
        except Exception:  # noqa: BLE001
            self.handleError(record)  # prints the traceback to stderr
        finally:
            _guard.busy = False
