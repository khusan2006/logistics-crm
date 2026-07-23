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


def _lot(brand="LLDPE", kg="400", price=None, arrived="2026-07-16", partner="Pars"):
    """One arrived lot of `brand`, optionally at its own USD/kg (its landed cost)."""
    p = Partner.objects.create(name=partner, phone="1", city="T")
    c = Contract.objects.create(partner=p, brand=brand, kg=Decimal("100000"),
                                price=Decimal("1.00"), created="2026-07-01",
                                deadline="2026-08-01")
    return Shipment.objects.create(contract=c, kg=Decimal(kg),
                                   price=Decimal(price) if price else None,
                                   status=ShipmentStatus.arrival(), arrived=arrived)


def test_ombor_groups_lots_of_one_marka_into_a_single_row(admin_client, db):
    """The same granula arriving twice at different prices is ONE ombor row —
    the differing landed costs live on the lots inside it, not in the table."""
    cheap = _lot(brand="2102 kampaund", kg="48000", price="1.20", arrived="2026-07-19")
    dear = _lot(brand="2102 kampaund", kg="72000", price="1.30", arrived="2026-07-23")
    other = _lot(brand="7000 kampaund", kg="24000", price="1.36")

    resp = admin_client.get("/ombor/")
    groups = resp.context["page"].object_list
    by_brand = {g["brand"]: g for g in groups}
    assert set(by_brand) == {"2102 kampaund", "7000 kampaund"}

    merged = by_brand["2102 kampaund"]
    assert [lot.pk for lot in merged["lots"]] == [cheap.pk, dear.pk]   # FIFO order
    assert merged["kirim"] == Decimal("120000")
    assert merged["available"] == Decimal("120000")
    assert merged["cost_min"] == Decimal("1.2000")
    assert merged["cost_max"] == Decimal("1.3000")
    assert by_brand["7000 kampaund"]["lots"] == [other]


def test_ombor_row_carries_every_lot_for_selling_separately(admin_client, db):
    """Each lot inside the row keeps its own Sotish link, locked to that lot."""
    cheap = _lot(brand="2102 kampaund", kg="48000", price="1.20", arrived="2026-07-19")
    dear = _lot(brand="2102 kampaund", kg="72000", price="1.30", arrived="2026-07-23")
    html = admin_client.get("/ombor/").content.decode()
    assert f"/sales/new/?lot={cheap.pk}" in html
    assert f"/sales/new/?lot={dear.pk}" in html
    assert "1,2000" in html or "1.2000" in html      # per-lot tan narx is shown inside


def test_ombor_group_totals_net_out_sales(admin_client, db):
    from crm.models import Customer, Sale
    lot = _lot(brand="2102 kampaund", kg="1000", price="1.20")
    customer = Customer.objects.create(name="Ali")
    Sale.objects.create(customer=customer, shipment=lot, kg=Decimal("400"),
                        price=Decimal("2"), cost_price=Decimal("1.20"), date="2026-07-20")

    group = admin_client.get("/ombor/").context["page"].object_list[0]
    assert group["sold"] == Decimal("400") and group["available"] == Decimal("600")


def test_ombor_search_still_matches_by_marka(admin_client, db):
    _lot(brand="2102 kampaund")
    _lot(brand="7000 kampaund")
    groups = admin_client.get("/ombor/", {"q": "2102"}).context["page"].object_list
    assert [g["brand"] for g in groups] == ["2102 kampaund"]


def test_marka_row_sotish_preselects_that_marka(admin_client, db):
    """The row-level Sotish is still the FIFO path — it just arrives with the marka
    already chosen; the per-lot Sotish links live inside the row."""
    _lot(brand="2102 kampaund", kg="1000", price="1.20")
    html = admin_client.get("/ombor/").content.decode()
    assert "/sales/new/?brand=2102%20kampaund" in html or "/sales/new/?brand=2102+kampaund" in html

    form = admin_client.get("/sales/new/", {"brand": "2102 kampaund"}).context["form"]
    assert form.initial["brand"] == "2102 kampaund"
