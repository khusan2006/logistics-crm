"""The daily report.

It covers YESTERDAY, as a local calendar day (TIME_ZONE), since it runs in the morning.
Which sections it has, and in what order, comes from settings.MONITOR_REPORT_SECTIONS:
dotted paths to functions taking (start, end), the day's bounds as aware datetimes,
and returning a list of lines. The first line of each section is its title.
`monitoring.report.technical` is this app's own section.

Sections are packed into as few Telegram messages as fit (see MESSAGE_LIMIT)."""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone
from django.utils.module_loading import import_string

from monitoring.models import DailySnapshot, ErrorEvent

KEEP_ERRORS_FOR = timedelta(days=30)
TOP_ERRORS = 5
MESSAGE_LIMIT = 3800
DEFAULT_SECTIONS = ["monitoring.report.technical"]


def report_window(now=None):
    """(start, end) of yesterday in local time, as aware datetimes."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    tz = timezone.get_current_timezone()
    start = datetime.combine(today - timedelta(days=1), time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _errors_lines(now):
    since = now - timedelta(hours=24)
    active = ErrorEvent.objects.filter(last_seen__gte=since)
    if not active.exists():
        return ["✅ No errors in the last 24h"]
    new = active.filter(first_seen__gte=since).count()
    lines = [f"⚠️ Errors in the last 24h: {active.count()} kinds, {new} new"]
    for event in active.order_by("-count")[:TOP_ERRORS]:
        mark = "🆕 " if event.first_seen >= since else ""
        lines.append(f"• {mark}{event.title[:120]} — {event.location} ({event.count}x total)")
    return lines


def _database_lines(day):
    if connection.vendor != "postgresql":
        return []
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        size = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        connections = cursor.fetchone()[0]
    previous = (DailySnapshot.objects.filter(day__lt=day, db_bytes__isnull=False)
                .order_by("-day").first())
    DailySnapshot.objects.update_or_create(day=day, defaults={"db_bytes": size})
    growth = ""
    if previous:
        delta = size - previous.db_bytes
        growth = f" ({'+' if delta >= 0 else '−'}{human_bytes(abs(delta))} since {previous.day:%d.%m})"
    return [f"🗄 Database: {human_bytes(size)}{growth}, {connections} open connections"]


def technical(start, end):
    now = timezone.now()
    return ["⚙️ TECHNICAL", *_errors_lines(now), *_database_lines(start.date())]


def _run_section(path, start, end):
    try:
        lines = list(import_string(path)(start, end))
    except Exception as exc:  # noqa: BLE001 — shown in the report, not hidden
        return [f"❗ section {path} failed: {type(exc).__name__}: {exc}"]
    return lines


def _pack(blocks):
    """Join blocks (each a list of lines) into messages of at most MESSAGE_LIMIT."""
    messages, current = [], ""
    for block in blocks:
        for chunk in _split_block("\n".join(block)):
            candidate = f"{current}\n\n{chunk}" if current else chunk
            if len(candidate) > MESSAGE_LIMIT and current:
                messages.append(current)
                candidate = chunk
            current = candidate
    if current:
        messages.append(current)
    return messages


def _split_block(text):
    if len(text) <= MESSAGE_LIMIT:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MESSAGE_LIMIT and current:
            chunks.append(current)
            candidate = line
        current = candidate[:MESSAGE_LIMIT]
    return chunks + [current]


def build_report(now=None):
    """The report as a list of Telegram messages."""
    start, end = report_window(now)
    header = [f"📊 {settings.MONITOR_APP_NAME} — {start:%A %d.%m.%Y}"]
    blocks = [header]
    for path in getattr(settings, "MONITOR_REPORT_SECTIONS", DEFAULT_SECTIONS):
        lines = _run_section(path, start, end)
        if lines:
            blocks.append(lines)
    return _pack(blocks)


def prune_old_errors(now=None):
    now = now or timezone.now()
    deleted, _ = ErrorEvent.objects.filter(last_seen__lt=now - KEEP_ERRORS_FOR).delete()
    return deleted
