from datetime import date
from decimal import Decimal

import pytest

from crm.models import AuditLog, Contract, Partner, Shipment, ShipmentStatus


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("1000"),
                                       price=Decimal("1"), created="2026-07-01",
                                       deadline="2026-08-01")
    return Shipment.objects.create(contract=contract, kg=Decimal("500"),
                                   status=ShipmentStatus.objects.first())


def _set(client, shipment, status):
    return client.post(f"/shipments/{shipment.pk}/status/", {"status": status.pk})


def test_translator_moves_nonfinal(translator_client, shipment):
    target = ShipmentStatus.objects.get(name="Bojxona")
    assert _set(translator_client, shipment, target).status_code == 302
    shipment.refresh_from_db()
    assert shipment.status == target
    assert AuditLog.objects.filter(action="status", target_id=shipment.pk).exists()


def test_translator_cannot_finish(translator_client, shipment):
    resp = _set(translator_client, shipment, ShipmentStatus.arrival())
    assert resp.status_code == 403
    shipment.refresh_from_db()
    assert not shipment.status.is_arrival


def test_admin_finish_stamps_arrival(admin_client, shipment):
    _set(admin_client, shipment, ShipmentStatus.arrival())
    shipment.refresh_from_db()
    assert shipment.status.is_arrival and shipment.arrived == date.today()


def test_leaving_arrival_clears_date(admin_client, shipment):
    _set(admin_client, shipment, ShipmentStatus.arrival())
    _set(admin_client, shipment, ShipmentStatus.objects.get(name="Bojxona"))
    shipment.refresh_from_db()
    assert shipment.arrived is None
