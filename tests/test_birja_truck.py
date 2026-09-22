"""A birja truck bigger than what its kelishuv has left — the owner's rule, confirmed
2026-09-22: a 20 t load on a kelishuv with 15 t left takes the other 5 t off the
next one. A yuk belongs to one kelishuv, so that truck is two yuklar, linked by
`Shipment.truck` so it still reads as the one 20 t load it is (crm.birja.spill_truck).
"""
from decimal import Decimal

import pytest
from conftest import line_data, make_shipment

from crm.birja import apply_redistribution, plan_redistribution
from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentExpense, ShipmentStatus,
    birja_partner,
)

pytestmark = pytest.mark.django_db

BRAND = "и 1561"


def _kelishuv(created, *lots, brand=BRAND, **kw):
    """A birja kelishuv of `lots` — (kg, narx) each — in the order they stand."""
    contract = Contract.objects.create(partner=birja_partner(), created=created, **kw)
    for position, (kg, price) in enumerate(lots):
        ContractLine.objects.create(contract=contract, brand=brand, kg=Decimal(kg),
                                    price=Decimal(price), position=position)
    return contract


def _status():
    return ShipmentStatus.for_kind(birja=True).first()


def _post_truck(client, contract, kg, lot=None):
    lot = lot or contract.lines.order_by("position").first()
    return client.post("/birja/yuklar/new/", {
        "contract": contract.pk, "status": _status().pk, "sent": "2026-09-20",
        "eta": "2026-09-25", "transport": "01 777 AAA", "note": "",
        **line_data({"contract_line": lot.pk, "kg": kg})})


def _truck_kg(truck):
    """{kelishuv kod: kg} of every yuk of the truck."""
    return {yuk.contract.code: yuk.kg for yuk in truck.truck_yuklar}


def _older_with_15t_left():
    older = _kelishuv("2026-09-07", ("30000", "1.00"))
    make_shipment(contract_line=older.lines.get(), kg="15000", status=_status())
    newer = _kelishuv("2026-09-10", ("30000", "1.10"))
    return older, newer


def test_a_truck_bigger_than_the_kelishuvs_rest_takes_it_off_the_next(admin_client):
    older, newer = _older_with_15t_left()
    resp = _post_truck(admin_client, older, "20000")
    assert resp.status_code == 302

    truck = Shipment.objects.filter(truck__isnull=True).latest("pk")
    part = truck.truck_parts.get()
    assert (truck.contract, truck.kg) == (older, Decimal("15000.000"))
    assert (part.contract, part.kg) == (newer, Decimal("5000.000"))
    assert truck.truck_kg == Decimal("20000.000")
    assert older.lines.get().remaining_kg == 0
    # One truck: the part carries the same holat, dates and plate.
    for name in ("status_id", "sent", "eta", "transport"):
        assert getattr(part, name) == getattr(truck, name)


def test_the_rest_of_the_kelishuvs_own_lots_is_used_before_the_next_kelishuv(
        admin_client):
    older = _kelishuv("2026-09-07", ("10000", "1.00"), ("10000", "0.90"))
    newer = _kelishuv("2026-09-10", ("30000", "1.10"))
    _post_truck(admin_client, older, "25000")
    truck = Shipment.objects.get(truck__isnull=True)
    assert sorted(ln.kg for ln in truck.lines.all()) == [Decimal("10000.000")] * 2
    assert _truck_kg(truck) == {older.code: Decimal("20000.000"),
                                newer.code: Decimal("5000.000")}


def test_the_next_kelishuv_is_the_oldest_open_one(admin_client):
    older, newer = _older_with_15t_left()
    _kelishuv("2026-09-08", ("30000", "1.05"))            # older than `newer`
    closed = _kelishuv("2026-09-08", ("30000", "1.05"), closed_short=True)
    _post_truck(admin_client, older, "20000")
    truck = Shipment.objects.filter(truck__isnull=True).latest("pk")
    codes = list(_truck_kg(truck))
    assert newer.code not in codes and closed.code not in codes
    assert len(codes) == 2


def test_a_truck_the_birja_cannot_fill_is_refused(admin_client):
    older, newer = _older_with_15t_left()
    resp = _post_truck(admin_client, older, "50000")        # 15 000 + 30 000 left
    assert resp.status_code == 200
    assert "45 000 kg qolgan" in resp.content.decode()
    assert Shipment.objects.count() == 1


def test_each_part_pays_transport_at_its_own_kelishuvs_rate(admin_client):
    older, newer = _older_with_15t_left()
    Contract.objects.filter(pk=older.pk).update(transport_rate_per_kg=Decimal("100"))
    Contract.objects.filter(pk=newer.pk).update(transport_rate_per_kg=Decimal("110"))
    _post_truck(admin_client, older, "20000")
    truck = Shipment.objects.filter(truck__isnull=True).latest("pk")

    arrival = ShipmentStatus.arrival()
    admin_client.post(f"/shipments/{truck.pk}/status/", {"status": arrival.pk})
    part = truck.truck_parts.get()
    assert part.arrived is not None                          # the holat reached it
    own = {e.shipment_id: e.rate_per_kg for e in
           ShipmentExpense.objects.filter(is_auto_transport=True,
                                          shipment__in=[truck, part])}
    assert own == {truck.pk: Decimal("100"), part.pk: Decimal("110")}


def test_growing_a_split_truck_adds_to_its_part_rather_than_a_third_yuk(admin_client):
    """The row is this yuk's own share: 15 000 → 26 000 is 11 000 more than its lot
    holds, and they join the 5 000 already on the truck's birja-2 part."""
    older, newer = _older_with_15t_left()
    _post_truck(admin_client, older, "20000")
    truck = Shipment.objects.filter(truck__isnull=True).latest("pk")
    resp = admin_client.post(f"/shipments/{truck.pk}/edit/", {
        "contract": older.pk, "status": truck.status_id, "sent": "2026-09-20",
        "eta": "2026-09-25", "transport": "01 777 AAA", "note": "",
        **line_data({"id": truck.lines.get().pk,
                     "contract_line": older.lines.get().pk, "kg": "26000"},
                    initial=1)})
    assert resp.status_code in (204, 302)
    assert _truck_kg(truck) == {older.code: Decimal("15000.000"),
                                newer.code: Decimal("16000.000")}
    assert Shipment.objects.filter(contract=newer).count() == 1


def test_an_eron_truck_still_may_not_carry_more_than_its_lot(admin_client):
    pars = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=pars, created="2026-09-01")
    lot = ContractLine.objects.create(contract=contract, brand="2102",
                                      kg=Decimal("1000"), price=Decimal("1"))
    resp = admin_client.post("/shipments/new/", {
        "contract": contract.pk, "status": ShipmentStatus.for_kind(False).first().pk,
        "sent": "2026-09-20", "eta": "2026-09-25", "transport": "", "note": "",
        **line_data({"contract_line": lot.pk, "kg": "1500"})})
    assert resp.status_code == 200
    assert not Shipment.objects.exists()


def test_the_list_and_the_yuk_page_read_the_whole_truck(admin_client):
    older, newer = _older_with_15t_left()
    _post_truck(admin_client, older, "20000")
    truck = Shipment.objects.filter(truck__isnull=True).latest("pk")
    part = truck.truck_parts.get()
    listing = admin_client.get("/birja/yuklar/").content.decode()
    # One row for the truck — its birja-2 share is a line inside it, not a row.
    assert f'href="/shipments/{truck.pk}/">#{truck.pk}' in listing
    assert f'href="/shipments/{part.pk}/">#{part.pk}' not in listing
    assert "jami 20\u00a0000" in listing
    assert f'href="/shipments/{part.pk}/">{newer.code}</a>' in listing
    page = admin_client.get(f"/shipments/{truck.pk}/").content.decode()
    assert "Bitta mashina" in page and newer.code in page


def test_birja_fifo_links_the_truck_it_splits(admin_client):
    """The one-off redistribution splits a truck the same way — and now links it."""
    newer = _kelishuv("2026-09-08", ("30000", "1.70"))
    older = _kelishuv("2026-09-07", ("20000", "1.60"))
    arrival = ShipmentStatus.arrival()
    truck = make_shipment(contract_line=newer.lines.get(), kg="26000", status=arrival,
                          sent="2026-09-17", arrived="2026-09-17")
    apply_redistribution(plan_redistribution())
    assert _truck_kg(truck) == {older.code: Decimal("20000.000"),
                                newer.code: Decimal("6000.000")}


def test_a_truck_filled_off_two_kelishuvlar_is_booked_as_one_truck(admin_client):
    """What the modal posts once the kg box has filled the rows: lots of two
    kelishuvlar, no kelishuv named. The oldest is the yuk; the other its part."""
    older, newer = _older_with_15t_left()
    resp = admin_client.post("/birja/yuklar/new/", {
        "status": _status().pk, "sent": "2026-09-20", "eta": "2026-09-25",
        "transport": "01 777 AAA", "note": "",
        **line_data({"contract_line": newer.lines.get().pk, "kg": "5000"},
                    {"contract_line": older.lines.get().pk, "kg": "15000"})})
    assert resp.status_code == 302
    truck = Shipment.objects.filter(truck__isnull=True).latest("pk")
    assert truck.contract == older
    assert _truck_kg(truck) == {older.code: Decimal("15000.000"),
                                newer.code: Decimal("5000.000")}


def test_the_new_birja_modal_offers_the_marka_and_kg_box(admin_client):
    older, newer = _older_with_15t_left()
    page = admin_client.get("/birja/yuklar/new/").content.decode()
    assert "data-line-fill" in page and "Mashinadagi jami kg" in page
    # Each lot option carries what the box fills from.
    lot = older.lines.get()
    assert f'data-brand="{BRAND}"' in page and f'data-code="{older.code}"' in page
    assert f'value="{lot.pk}"' in page


# --- editing the whole truck ---------------------------------------------------------

def _truck(admin_client):
    older, newer = _older_with_15t_left()
    _post_truck(admin_client, older, "20000")
    return older, newer, Shipment.objects.filter(truck__isnull=True).latest("pk")


def _edit_post(truck, rows, initial):
    return {"status": truck.status_id, "sent": "2026-09-20", "eta": "2026-09-25",
            "transport": "01 777 AAA", "note": "",
            **line_data(*rows, initial=initial)}


def test_either_part_opens_the_whole_truck(admin_client):
    older, newer, truck = _truck(admin_client)
    part = truck.truck_parts.get()
    resp = admin_client.get(f"/shipments/{part.pk}/edit/")
    assert "contract" not in resp.context["form"].fields
    assert resp.context["form"].instance == truck
    rows = [f.instance for f in resp.context["lines"].forms]
    assert [(r.shipment_id, r.kg) for r in rows] == [
        (truck.pk, Decimal("15000.000")), (part.pk, Decimal("5000.000"))]


def test_taking_a_parts_row_off_the_truck_removes_the_part(admin_client):
    older, newer, truck = _truck(admin_client)
    head_row = truck.lines.get()
    part_row = truck.truck_parts.get().lines.get()
    resp = admin_client.post(f"/shipments/{truck.pk}/edit/", _edit_post(truck, [
        {"id": head_row.pk, "contract_line": older.lines.get().pk, "kg": "15000"},
        {"id": part_row.pk, "contract_line": newer.lines.get().pk, "kg": "5000",
         "DELETE": "on"}], initial=2))
    assert resp.status_code in (204, 302)
    assert not truck.truck_parts.exists()
    assert _truck_kg(truck) == {older.code: Decimal("15000.000")}


def test_a_row_moved_to_another_kelishuvs_lot_lands_on_that_part(admin_client):
    """The edit is one list: a row re-pointed at a lot of a kelishuv the truck had
    no part on gets one made, and the part it left is removed when emptied."""
    older, newer, truck = _truck(admin_client)
    third = _kelishuv("2026-09-12", ("30000", "1.20"))
    head_row = truck.lines.get()
    part_row = truck.truck_parts.get().lines.get()
    admin_client.post(f"/shipments/{truck.pk}/edit/", _edit_post(truck, [
        {"id": head_row.pk, "contract_line": older.lines.get().pk, "kg": "15000"},
        {"id": part_row.pk, "contract_line": third.lines.get().pk, "kg": "5000"}],
        initial=2))
    assert _truck_kg(truck) == {older.code: Decimal("15000.000"),
                                third.code: Decimal("5000.000")}
    assert not Shipment.objects.filter(contract=newer).exists()


def test_a_row_a_mijoz_bought_from_cannot_leave_the_truck(admin_client):
    from crm.models import Customer, Sale, SaleLot
    older, newer, truck = _truck(admin_client)
    part = truck.truck_parts.get()
    part_row = part.lines.get()
    customer = Customer.objects.create(name="Ali", phone="1")
    sale = Sale.objects.create(customer=customer, line=part_row, kg=Decimal("1000"),
                               price=Decimal("2"), date="2026-09-21")
    SaleLot.objects.get_or_create(sale=sale, line=part_row,
                                  defaults={"kg": Decimal("1000")})
    resp = admin_client.post(f"/shipments/{truck.pk}/edit/", _edit_post(truck, [
        {"id": truck.lines.get().pk, "contract_line": older.lines.get().pk,
         "kg": "15000"},
        {"id": part_row.pk, "contract_line": newer.lines.get().pk, "kg": "500"}],
        initial=2))
    assert resp.status_code == 200
    assert "kg sotilgan" in resp.content.decode()
    part_row.refresh_from_db()
    assert part_row.kg == Decimal("5000.000")
