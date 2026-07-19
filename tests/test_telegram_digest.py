from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import override_settings

from crm.management.commands.send_telegram_digest import build_digest, NO_OVERDUE_MESSAGE
from crm.models import Contract, Partner, Shipment, ShipmentStatus


@pytest.fixture
def partner():
    return Partner.objects.create(name="Pars", phone="1", city="Tehran")


@pytest.fixture
def contract(partner):
    return Contract.objects.create(
        partner=partner, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"),
        created="2026-07-01", deadline="2026-08-01",
    )


def test_build_digest_lists_overdue_shipment(db, contract):
    shipment = Shipment.objects.create(
        contract=contract, kg=Decimal("500"), status=ShipmentStatus.objects.first(),
        eta=date.today() - timedelta(days=3), transport="01A111AA", container="MSCU-1",
    )

    text = build_digest()

    assert "LLDPE" in text
    assert "kechikdi" in text
    assert str(shipment.days_late) in text


def test_build_digest_no_overdue_or_arriving_returns_fixed_message(db):
    text = build_digest()

    assert text == NO_OVERDUE_MESSAGE


def test_command_without_config_does_not_send_or_raise(db, capsys):
    with override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID=""):
        call_command("send_telegram_digest")

    captured = capsys.readouterr()
    assert "sozlanmagan" in captured.out
