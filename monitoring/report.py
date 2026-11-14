"""The daily report text. App-specific sections come from
settings.MONITOR_REPORT_SECTIONS: dotted paths to functions taking `since` (a
datetime) and returning a list of lines."""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from django.utils.module_loading import import_string

from monitoring.models import ErrorEvent

KEEP_ERRORS_FOR = timedelta(days=30)
TOP_ERRORS = 5


def _errors_section(since):
    active = ErrorEvent.objects.filter(last_seen__gte=since)
    if not active.exists():
        return ["✅ No errors in the last 24h"]
    new = active.filter(first_seen__gte=since).count()
    lines = [f"⚠️ Errors in the last 24h: {active.count()} kinds, {new} new"]
    for event in active.order_by("-count")[:TOP_ERRORS]:
        mark = "🆕 " if event.first_seen >= since else ""
        lines.append(f"• {mark}{event.title[:120]} — {event.location} ({event.count}x total)")
    return lines


def _database_section():
    if connection.vendor != "postgresql":
        return []
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
        size = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        connections = cursor.fetchone()[0]
    return [f"🗄 Database: {size}, {connections} open connections"]


def _users_section(since):
    logins = get_user_model().objects.filter(last_login__gte=since).count()
    return [f"👥 Users who logged in: {logins}"]


def _extra_sections(since):
    lines = []
    for path in getattr(settings, "MONITOR_REPORT_SECTIONS", []):
        try:
            lines.extend(import_string(path)(since))
        except Exception as exc:  # noqa: BLE001 — shown in the report, not hidden
            lines.append(f"❗ section {path} failed: {type(exc).__name__}: {exc}")
    return lines


def build_report(now=None):
    now = now or timezone.now()
    since = now - timedelta(hours=24)
    day = timezone.localtime(now).strftime("%Y-%m-%d")
    lines = [f"📊 {settings.MONITOR_APP_NAME} — daily report {day}", ""]
    lines += _errors_section(since)
    lines += _database_section()
    lines += _users_section(since)
    lines += _extra_sections(since)
    return "\n".join(lines)


def prune_old_errors(now=None):
    now = now or timezone.now()
    deleted, _ = ErrorEvent.objects.filter(last_seen__lt=now - KEEP_ERRORS_FOR).delete()
    return deleted
