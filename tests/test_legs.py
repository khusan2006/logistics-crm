from decimal import Decimal

from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentLeg, ShipmentLine, ShipmentStatus,
)


def _shipment():
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal("5000"), price=Decimal("1"))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.first(), transport="OLD-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal("1000"))
    return _ship_obj


def test_add_leg_appends_with_order(admin_client, db):
    s = _shipment()
    resp = admin_client.post(f"/legs/new/?shipment={s.pk}", {
        "from_location": "Tehron", "to_location": "Chegara", "transport": "12 A 345",
        "container": "", "departed": "2026-07-05", "arrived": "2026-07-08", "note": ""})
    assert resp.status_code == 302
    leg = ShipmentLeg.objects.get()
    assert leg.shipment_id == s.pk and leg.order == 1 and leg.from_location == "Tehron"


def test_translator_can_manage_legs(translator_client, db):
    """Legs are physical movement (no money) — translators coordinate drivers, so
    they can add/manage legs (unlike the admin-only money features)."""
    s = _shipment()
    resp = translator_client.post(f"/legs/new/?shipment={s.pk}", {
        "from_location": "A", "to_location": "B", "transport": "01 777 AAA",
        "container": "", "departed": "", "arrived": "", "note": ""})
    assert resp.status_code == 302
    assert ShipmentLeg.objects.filter(shipment=s).exists()


def test_current_transport_is_the_active_leg(db):
    s = _shipment()
    # leg 1 done (departed + arrived), leg 2 in progress (departed, not arrived)
    ShipmentLeg.objects.create(shipment=s, order=1, from_location="A", to_location="B",
                               transport="D1", departed="2026-07-05", arrived="2026-07-08")
    ShipmentLeg.objects.create(shipment=s, order=2, from_location="B", to_location="C",
                               transport="D2", departed="2026-07-09")
    s.refresh_from_db()
    assert s.current_transport == "D2"


def test_current_transport_falls_back_to_shipment_when_no_legs(db):
    s = _shipment()
    assert s.current_transport == "OLD-1"


def test_leg_move_reorders(admin_client, db):
    s = _shipment()
    a = ShipmentLeg.objects.create(shipment=s, order=1, from_location="A", to_location="B")
    b = ShipmentLeg.objects.create(shipment=s, order=2, from_location="B", to_location="C")
    admin_client.post(f"/legs/{b.pk}/move/", {"dir": "up"})
    a.refresh_from_db(); b.refresh_from_db()
    assert b.order < a.order  # b moved ahead of a (unplanned stop slotted between)


def test_arrived_before_departed_rejected(admin_client, db):
    s = _shipment()
    resp = admin_client.post(f"/legs/new/?shipment={s.pk}", {
        "from_location": "A", "to_location": "B", "transport": "", "container": "",
        "departed": "2026-07-10", "arrived": "2026-07-05", "note": ""})
    assert resp.status_code == 200 and not ShipmentLeg.objects.exists()
