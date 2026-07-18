from decimal import Decimal

from crm.models import Contract, Partner, Shipment, ShipmentStatus


def _contract(kg="1000", brand="LLDPE"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    return Contract.objects.create(partner=partner, brand=brand, kg=Decimal(kg),
                                   price=Decimal("1.00"), created="2026-07-01",
                                   deadline="2026-08-01")


def _arrived_shipment(kg="400", brand="LLDPE"):
    c = _contract(brand=brand)
    return Shipment.objects.create(
        contract=c, kg=Decimal(kg), status=ShipmentStatus.arrival(),
        sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16",
        transport="01A111AA", container="MSCU-1",
    )


def _non_arrived_shipment(kg="200", brand="HDPE"):
    c = _contract(brand=brand)
    return Shipment.objects.create(
        contract=c, kg=Decimal(kg), status=ShipmentStatus.objects.first(),
        sent="2026-07-05", eta="2026-08-01",
    )


def test_arrived_shipment_is_lot_with_full_available_kg(db):
    s = _arrived_shipment(kg="400")
    assert s.is_lot is True
    assert s.sold_kg == Decimal("0")
    assert s.reserved_kg == Decimal("0")
    assert s.returned_kg == Decimal("0")
    assert s.available_kg == s.kg == Decimal("400")


def test_non_arrived_shipment_is_not_a_lot(db):
    s = _non_arrived_shipment()
    assert s.is_lot is False


def test_ombor_lists_only_arrived_lots(admin_client, db):
    lot = _arrived_shipment(kg="400", brand="LLDPE")
    not_lot = _non_arrived_shipment(kg="200", brand="HDPE-Excluded")
    html = admin_client.get("/ombor/").content.decode()
    assert lot.contract.brand in html
    assert not_lot.contract.brand not in html


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/ombor/").status_code == 403


def test_admin_sees_lot_brand(admin_client, db):
    lot = _arrived_shipment(kg="400")
    resp = admin_client.get("/ombor/")
    assert resp.status_code == 200
    assert lot.contract.brand in resp.content.decode()
