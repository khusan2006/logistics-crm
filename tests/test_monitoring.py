"""The monitoring app: error alerts, /healthz and the daily report."""
import logging
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import DatabaseError
from django.utils import timezone

from accounts.models import User
from crm.models import AuditLog, Scale
from monitoring import handler as handler_module
from monitoring import telegram
from monitoring.handler import TelegramErrorHandler
from monitoring.models import ErrorEvent
from monitoring.report import build_report, prune_old_errors


@pytest.fixture
def sent(monkeypatch, settings):
    """Capture alerts instead of calling Telegram; sending happens inline."""
    settings.MONITOR_TELEGRAM_BOT_TOKEN = "t"
    settings.MONITOR_TELEGRAM_CHAT_ID = "c"
    messages = []
    monkeypatch.setattr(telegram, "send_in_background", messages.append)
    monkeypatch.setattr(telegram, "send_message", lambda text: messages.append(text) or True)
    return messages


def _raise_and_log(logger, exc):
    try:
        raise exc
    except type(exc):
        logger.exception("boom")


@pytest.fixture
def log():
    logger = logging.getLogger("tests.monitoring")
    h = TelegramErrorHandler()
    logger.addHandler(h)
    logger.propagate = False  # the root handler from settings would double-count
    yield logger
    logger.removeHandler(h)
    logger.propagate = True


def test_first_error_alerts_and_repeats_are_throttled(db, sent, log):
    for _ in range(3):
        _raise_and_log(log, ValueError("bad value"))

    event = ErrorEvent.objects.get()
    assert event.count == 3
    assert event.title == "ValueError: bad value"
    assert "tests/test_monitoring.py" in event.location
    assert len(sent) == 1
    assert "ValueError: bad value" in sent[0]


def test_repeat_alerts_again_after_an_hour(db, sent, log):
    _raise_and_log(log, ValueError("x"))
    ErrorEvent.objects.update(last_alerted=timezone.now() - timedelta(hours=2))
    _raise_and_log(log, ValueError("x"))

    assert len(sent) == 2
    assert "seen 2x" in sent[1]


def test_different_errors_are_separate_events(db, sent, log):
    _raise_and_log(log, ValueError("a"))
    _raise_and_log(log, KeyError("b"))
    assert ErrorEvent.objects.count() == 2
    assert len(sent) == 2


def test_warnings_are_ignored(db, sent, log):
    log.warning("just a warning")
    assert not ErrorEvent.objects.exists() and not sent


def test_alert_still_goes_out_when_db_is_down(db, sent, log, monkeypatch):
    def broken(info):
        raise DatabaseError("connection refused")
    monkeypatch.setattr(handler_module, "record_event", broken)
    handler_module._memory_throttle.clear()

    _raise_and_log(log, RuntimeError("db gone"))
    _raise_and_log(log, RuntimeError("db gone"))

    assert len(sent) == 1
    assert "could not record it in the DB" in sent[0]


def test_unhandled_view_error_is_captured(db, sent, client, settings):
    """End to end through Django's own `django.request` logging."""
    from django.urls import path

    def explode(request):
        raise ZeroDivisionError("division by zero")

    import config.urls as urls
    settings.ROOT_URLCONF = type("U", (), {
        "urlpatterns": urls.urlpatterns + [path("explode/", explode)]})
    user = User.objects.create_user(username="boss", password="p", role=User.Role.ADMIN)
    client.force_login(user)
    client.raise_request_exception = False

    resp = client.get("/explode/?secret=1")
    assert resp.status_code == 500
    assert len(sent) == 1
    assert "ZeroDivisionError" in sent[0]
    assert "GET /explode/" in sent[0]
    assert "secret" not in sent[0]  # query strings are left out
    assert "boss" in sent[0]


def test_healthz_is_public_and_checks_db(client, db):
    resp = client.get("/healthz/", secure=True)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


def test_healthz_reports_503_when_db_fails(client, db, monkeypatch):
    from monitoring import views

    class Broken:
        def cursor(self):
            raise DatabaseError("down")
    monkeypatch.setattr(views, "connection", Broken())
    resp = client.get("/healthz/", secure=True)
    assert resp.status_code == 503


def test_report_without_errors(db):
    text = build_report()
    assert "daily report" in text
    assert "No errors in the last 24h" in text
    assert "Users who logged in: 0" in text


def test_report_lists_errors_activity_and_scales(db):
    now = timezone.now()
    ErrorEvent.objects.create(fingerprint="a" * 40, title="ValueError: x", location="crm/views.py:1",
                              count=7, first_seen=now, last_seen=now)
    ErrorEvent.objects.create(fingerprint="b" * 40, title="Old", count=1,
                              first_seen=now - timedelta(days=3), last_seen=now - timedelta(days=3))
    user = User.objects.create_user(username="op", password="p", role=User.Role.ADMIN)
    AuditLog.record(user, AuditLog.Action.CREATE, "Sale", 1, "sotuv")
    Scale.objects.create(name="Tarozi 1")

    text = build_report()
    assert "1 kinds, 1 new" in text
    assert "ValueError: x" in text and "7x total" in text
    assert "Old" not in text
    assert "Activity: 1 actions by 1 users" in text
    assert "Tarozi 1: ⚠️ no data for 24h+" in text


def test_broken_section_is_shown_not_hidden(db, settings):
    settings.MONITOR_REPORT_SECTIONS = ["crm.monitoring_report.does_not_exist"]
    assert "section crm.monitoring_report.does_not_exist failed" in build_report()


def test_prune_keeps_recent_errors(db):
    now = timezone.now()
    ErrorEvent.objects.create(fingerprint="a" * 40, title="new", first_seen=now, last_seen=now)
    ErrorEvent.objects.create(fingerprint="b" * 40, title="old", first_seen=now,
                              last_seen=now - timedelta(days=31))
    assert prune_old_errors() == 1
    assert list(ErrorEvent.objects.values_list("title", flat=True)) == ["new"]


def test_report_command_sends(db, sent):
    call_command("monitor_report")
    assert len(sent) == 1 and "daily report" in sent[0]


def test_report_command_fails_loudly_when_unconfigured(db, settings):
    from django.core.management.base import CommandError
    settings.MONITOR_TELEGRAM_BOT_TOKEN = ""
    with pytest.raises(CommandError):
        call_command("monitor_report")
