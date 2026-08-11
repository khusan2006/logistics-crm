from datetime import date
from decimal import Decimal

import pytest
from django.utils.formats import date_format

from crm.models import (
    AuditLog, Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus,
)


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal("500"))
    return _ship_obj


def _set(client, shipment, status):
    return client.post(f"/shipments/{shipment.pk}/status/", {"status": status.pk})


def _set_ajax(client, shipment, status):
    return client.post(f"/shipments/{shipment.pk}/status/", {"status": status.pk},
                       HTTP_X_REQUESTED_WITH="XMLHttpRequest")


def test_admin_moves_nonfinal(admin_client, shipment):
    target = ShipmentStatus.objects.get(name="Bojxona")
    assert _set(admin_client, shipment, target).status_code == 302
    shipment.refresh_from_db()
    assert shipment.status == target
    assert AuditLog.objects.filter(action="status", target_id=shipment.pk).exists()


def test_a_tarjimon_cannot_move_the_holat_at_all(translator_client, shipment):
    """A tarjimon used to move a load between non-arrival statuses. The holat drives
    the whole board — what counts as in transit, what is overdue, when a bron becomes
    sellable — so it is admin-only now, arrival or not."""
    before = shipment.status
    for target in (ShipmentStatus.objects.get(name="Bojxona"), ShipmentStatus.arrival()):
        assert _set(translator_client, shipment, target).status_code == 403
        shipment.refresh_from_db()
        assert shipment.status == before


def test_admin_finish_stamps_arrival(admin_client, shipment):
    _set(admin_client, shipment, ShipmentStatus.arrival())
    shipment.refresh_from_db()
    assert shipment.status.is_arrival and shipment.arrived == date.today()


def test_leaving_arrival_clears_date(admin_client, shipment):
    _set(admin_client, shipment, ShipmentStatus.arrival())
    _set(admin_client, shipment, ShipmentStatus.objects.get(name="Bojxona"))
    shipment.refresh_from_db()
    assert shipment.arrived is None


def test_ajax_arrival_returns_exact_date(admin_client, shipment):
    """Reaching the arrival status swaps the tahminiy ETA for the exact date."""
    resp = _set_ajax(admin_client, shipment, ShipmentStatus.arrival())
    data = resp.json()
    assert data["arrived"] is True
    assert "Yetib keldi" in data["date_html"]
    # The exact arrival date is rendered (in the app's localized date format).
    assert date_format(date.today(), "DATE_FORMAT") in data["date_html"]


def test_ajax_move_back_drops_date(admin_client, shipment):
    """Moving the status back clears the date and the ETA view returns."""
    _set_ajax(admin_client, shipment, ShipmentStatus.arrival())
    resp = _set_ajax(admin_client, shipment, ShipmentStatus.objects.get(name="Bojxona"))
    data = resp.json()
    assert data["arrived"] is False
    assert "Yetib keldi" not in data["date_html"]
