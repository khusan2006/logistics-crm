"""The monitoring app: error alerts, /healthz and the daily report."""
import logging
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

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


def _report(now=None):
    return "\n\n".join(build_report(now))


def test_report_without_errors(db):
    text = _report()
    assert "No errors in the last 24h" in text
    assert "Sales: none" in text
    assert "Nothing unusual" in text


def test_report_lists_errors_and_scales(db):
    now = timezone.now()
    ErrorEvent.objects.create(fingerprint="a" * 40, title="ValueError: x", location="crm/views.py:1",
                              count=7, first_seen=now, last_seen=now)
    ErrorEvent.objects.create(fingerprint="b" * 40, title="Old", count=1,
                              first_seen=now - timedelta(days=3), last_seen=now - timedelta(days=3))
    Scale.objects.create(name="Tarozi 1")

    text = _report()
    assert "1 kinds, 1 new" in text
    assert "ValueError: x" in text and "7x total" in text
    assert "Old (" not in text
    assert "Tarozi 1: ⚠️ no data for 24h+" in text


def test_broken_section_is_shown_not_hidden(db, settings):
    settings.MONITOR_REPORT_SECTIONS = ["crm.monitoring_report.does_not_exist"]
    assert "section crm.monitoring_report.does_not_exist failed" in _report()


def test_long_report_is_split_into_several_messages(db, settings):
    settings.MONITOR_REPORT_SECTIONS = ["tests.test_monitoring.long_section"]
    messages = build_report()
    assert len(messages) > 1
    assert all(len(m) <= 3800 for m in messages)


def long_section(start, end):
    return ["LONG"] + [f"line {i} " + "x" * 80 for i in range(120)]


# --- the GranulaLog sections, on a fixed "yesterday" ----------------------------

NOW = datetime(2026, 9, 10, 4, 0, tzinfo=dt_timezone.utc)   # 09:00 Tashkent on the 10th
DAY = date(2026, 9, 9)


def _at(hour, minute=0, day=DAY):
    tz = timezone.get_current_timezone()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def _audit(user, action, target_type, target_id, summary, when):
    entry = AuditLog.record(user, action, target_type, target_id, summary)
    AuditLog.objects.filter(pk=entry.pk).update(created_at=when)
    return entry


@pytest.fixture
def op(db):
    return User.objects.create_user(username="ali", password="p", role=User.Role.ADMIN,
                                    first_name="Ali", last_name="Valiyev")


def test_business_counts_yesterdays_sales_and_payments(db, op):
    from conftest import make_lot
    from crm.models import Customer, CustomerPayment, Sale
    customer = Customer.objects.create(name="Rise", phone="+998901112233")
    lot = make_lot()
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"), price=Decimal("1.2"),
                        date=DAY, created_by=op)
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("500"), price=Decimal("1.2"),
                        date=DAY - timedelta(days=3), created_by=op)
    CustomerPayment.objects.create(customer=customer, amount=Decimal("700"), date=DAY,
                                   created_by=op)

    text = _report(NOW)
    assert "Wednesday 09.09.2026" in text
    assert "Sales: 1 sales, 1 000 kg" in text
    assert "turnover $1 200" in text
    assert "top: Rise 1 000 kg" in text
    assert "Customer payments: 1 · $700" in text


def test_activity_counts_users_and_busiest_hour(db, op):
    for minute in (5, 10, 20):
        _audit(op, AuditLog.Action.CREATE, "Sotuv", 1, "sotuv", _at(11, minute))
    _audit(op, AuditLog.Action.PAYMENT, "Mijoz to'lovi", 1, "to'lov", _at(15))
    _audit(op, AuditLog.Action.CREATE, "Sotuv", 1, "today, not yesterday", _at(9, day=DAY + timedelta(days=1)))

    text = _report(NOW)
    assert "4 actions by 1 users" in text
    assert "Ali Valiyev: 4 (Sotuv 3, Mijoz to'lovi 1)" in text
    assert "Busiest hour 11:00 (3 actions); first 11:05, last 15:00" in text


def test_suspicious_flags_deletes_old_edits_backdating_night_and_users(db, op):
    from conftest import make_lot
    from crm.models import Customer, Sale
    customer = Customer.objects.create(name="Rise", phone="+998901112233")
    old_sale = Sale.objects.create(customer=customer, line=make_lot(), kg=Decimal("10"),
                                   price=Decimal("1"), date=DAY - timedelta(days=20), created_by=op)
    Sale.objects.filter(pk=old_sale.pk).update(created_at=_at(10, day=DAY - timedelta(days=20)))
    backdated = Sale.objects.create(customer=customer, line=make_lot(), kg=Decimal("10"),
                                    price=Decimal("1"), date=DAY - timedelta(days=6), created_by=op)
    Sale.objects.filter(pk=backdated.pk).update(created_at=_at(12))

    _audit(op, AuditLog.Action.DELETE, "Mijoz", 5, "Mijoz o'chirildi: Rise", _at(10))
    _audit(op, AuditLog.Action.UPDATE, "Sotuv", old_sale.pk, "Sotuv tahrirlandi", _at(10, 30))
    _audit(op, AuditLog.Action.CREATE, "Sotuv", 99, "late night", _at(23, 15))
    _audit(op, AuditLog.Action.UPDATE, "Foydalanuvchi", op.pk, "rol o'zgardi", _at(14))

    text = _report(NOW)
    assert "Deleted (1)" in text and "Mijoz o'chirildi: Rise" in text
    assert f"Old money records edited (1 edits):\n• Ali Valiyev — Sotuv ×1, 20 days old (#{old_sale.pk})" in text
    assert f"Ali Valiyev — Sotuv ×1, 6 days back (#{backdated.pk})" in text
    assert "Off-hours actions" in text and "Ali Valiyev 1" in text
    assert "User accounts changed (1)" in text
    assert "Nothing unusual" not in text


def test_suspicious_flags_failed_logins(db):
    from axes.models import AccessAttempt
    attempt = AccessAttempt.objects.create(username="admin", ip_address="1.2.3.4",
                                           failures_since_start=6, user_agent="x",
                                           get_data="", post_data="", http_accept="", path_info="/login/")
    AccessAttempt.objects.filter(pk=attempt.pk).update(attempt_time=_at(3))
    text = _report(NOW)
    assert "Failed logins: 6" in text
    assert "admin from 1.2.3.4: 6x — LOCKED OUT" in text


def test_prune_keeps_recent_errors(db):
    now = timezone.now()
    ErrorEvent.objects.create(fingerprint="a" * 40, title="new", first_seen=now, last_seen=now)
    ErrorEvent.objects.create(fingerprint="b" * 40, title="old", first_seen=now,
                              last_seen=now - timedelta(days=31))
    assert prune_old_errors() == 1
    assert list(ErrorEvent.objects.values_list("title", flat=True)) == ["new"]


def test_report_command_sends(db, sent):
    call_command("monitor_report")
    assert sent and "BUSINESS" in sent[0]


def test_report_command_fails_loudly_when_unconfigured(db, settings):
    from django.core.management.base import CommandError
    settings.MONITOR_TELEGRAM_BOT_TOKEN = ""
    with pytest.raises(CommandError):
        call_command("monitor_report")


def test_healthz_answers_head_for_pingers(client, db):
    assert client.head("/healthz/", secure=True).status_code == 200


def test_only_first_line_of_the_message_is_sent(db, sent, log):
    _raise_and_log(log, ValueError('duplicate key\nDETAIL:  Key (phone)=(+998901234567) exists.'))
    assert "998901234567" not in sent[0]
    assert "ValueError: duplicate key" in sent[0]


def test_alert_survives_a_request_user_that_raises(db, sent, monkeypatch):
    class Request:
        path, method = "/x/", "GET"

        @property
        def user(self):
            raise DatabaseError("db down")

    logger = logging.getLogger("django.request")
    try:
        raise RuntimeError("view broke")
    except RuntimeError:
        logger.error("Internal Server Error", exc_info=True, extra={"request": Request()})
    assert len(sent) == 1 and "👤 ?" in sent[0]
