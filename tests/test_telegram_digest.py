from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import override_settings

from crm.management.commands.send_telegram_digest import build_digest, NO_OVERDUE_MESSAGE
from crm.models import Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus


@pytest.fixture
def partner():
    return Partner.objects.create(name="Pars", phone="1", city="Tehran")


@pytest.fixture
def contract(partner):
    _contract_obj = Contract.objects.create(partner=partner, created="2026-07-01")
    _contract_obj_line = ContractLine.objects.create(
        contract=_contract_obj, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"))
    return _contract_obj


def test_build_digest_lists_overdue_shipment(db, contract):
    shipment = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.first(), eta=date.today() - timedelta(days=3), transport="01A111AA", container="MSCU-1")
    shipment_line = ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal("500"))

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
