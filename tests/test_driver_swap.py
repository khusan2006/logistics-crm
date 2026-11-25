"""Haydovchilarni almashtirish: two yuklar of one kelishuv whose haydovchi, telefon
and mashina were typed onto each other get them swapped back in one step."""
from decimal import Decimal

import pytest
from conftest import make_contract, make_shipment

from crm.models import (
    AuditLog, Contract, ContractLine, Shipment, ShipmentLeg, ShipmentStatus,
    birja_partner,
)

pytestmark = pytest.mark.django_db


def _url(contract):
    return f"/contracts/{contract.pk}/driver-swap/"


def _two_loads(**kw):
    contract = make_contract(kg="2000")
    first = make_shipment(contract=contract, kg="400", driver_name="Akmal",
                          driver_phone="+998901111111", transport="01 111 AAA",
                          container="MSCU 111111 1", **kw)
    second = make_shipment(contract=contract, kg="400", driver_name="Bobur",
                           driver_phone="+998902222222", transport="01 222 BBB",
                           container="MSCU 222222 2", **kw)
    return contract, first, second


def _swap(client, contract, first, second, container=True):
    data = {"first": first.pk, "second": second.pk}
    if container:
        data["container"] = "on"
    return client.post(_url(contract), data)


def test_swaps_name_phone_plate_and_container(admin_client):
    contract, first, second = _two_loads()
    assert _swap(admin_client, contract, first, second).status_code == 302
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.driver_name, first.driver_phone, first.transport, first.container) == (
        "Bobur", "+998902222222", "01 222 BBB", "MSCU 222222 2")
    assert (second.driver_name, second.driver_phone, second.transport, second.container) == (
        "Akmal", "+998901111111", "01 111 AAA", "MSCU 111111 1")


def test_container_can_stay_where_it_is(admin_client):
    contract, first, second = _two_loads()
    _swap(admin_client, contract, first, second, container=False)
    first.refresh_from_db()
    assert first.driver_name == "Bobur" and first.container == "MSCU 111111 1"


def test_nothing_but_driver_details_moves(admin_client):
    contract, first, second = _two_loads()
    first.note = "birinchi"
    first.save()
    _swap(admin_client, contract, first, second)
    first.refresh_from_db()
    assert first.note == "birinchi" and first.kg == Decimal("400")


def test_the_plate_shown_from_a_bosqich_is_swapped_too(admin_client):
    """The list shows a bosqich's raqam once the yuk has one, so that is the plate
    that has to move — otherwise the swap looks like it did nothing."""
    contract, first, second = _two_loads()
    ShipmentLeg.objects.create(shipment=first, from_location="Tehron",
                               to_location="Chegara", transport="10 LEG 777",
                               departed="2026-07-05")
    _swap(admin_client, contract, first, second)
    first = Shipment.objects.get(pk=first.pk)
    second = Shipment.objects.get(pk=second.pk)
    assert first.current_transport == "01 222 BBB"
    assert second.current_transport == "10 LEG 777"
    # The yuk's own raqam under the bosqich is left as it was, not lost.
    assert first.transport == "01 111 AAA"


def test_each_load_gets_an_audit_row(admin_client):
    contract, first, second = _two_loads()
    _swap(admin_client, contract, first, second)
    rows = AuditLog.objects.filter(summary__startswith="Haydovchi almashtirildi")
    assert sorted(r.target_id for r in rows) == sorted([first.pk, second.pk])


def test_the_same_load_twice_is_refused(admin_client):
    contract, first, _ = _two_loads()
    resp = _swap(admin_client, contract, first, first)
    assert resp.status_code == 200
    first.refresh_from_db()
    assert first.driver_name == "Akmal"


def test_a_load_from_another_kelishuv_is_refused(admin_client):
    contract, first, _ = _two_loads()
    stranger = make_shipment(driver_name="Begona")
    resp = _swap(admin_client, contract, first, stranger)
    assert resp.status_code == 200
    stranger.refresh_from_db()
    assert stranger.driver_name == "Begona"


def test_a_tarjimon_may_swap_on_the_eron_road(translator_client):
    contract, first, second = _two_loads()
    assert _swap(translator_client, contract, first, second).status_code == 302
    first.refresh_from_db()
    assert first.driver_name == "Bobur"


def _birja_kelishuv(created="2026-09-07"):
    contract = Contract.objects.create(partner=birja_partner(), created=created)
    ContractLine.objects.create(contract=contract, brand="и 1561", kg=Decimal("30000"),
                                price=Decimal("1.00"))
    return contract


def test_a_tarjimon_cannot_touch_a_birja_kelishuv(translator_client):
    contract = _birja_kelishuv()
    status = ShipmentStatus.for_kind(birja=True).first()
    first = make_shipment(contract=contract, kg="100", status=status, driver_name="A")
    second = make_shipment(contract=contract, kg="100", status=status, driver_name="B")
    assert _swap(translator_client, contract, first, second).status_code == 403


def test_a_birja_truck_part_on_another_kelishuv_follows_the_swap(admin_client):
    """A truck split across two kelishuvlar has one driver; the part booked on the
    next kelishuv has to follow when its head yuk's driver is swapped."""
    status = ShipmentStatus.for_kind(birja=True).first()
    older, newer = _birja_kelishuv("2026-09-07"), _birja_kelishuv("2026-09-10")
    head = make_shipment(contract=older, kg="100", status=status, driver_name="Akmal",
                         transport="01 111 AAA")
    part = make_shipment(contract=newer, kg="100", status=status, driver_name="Akmal",
                         transport="01 111 AAA", truck=head)
    other = make_shipment(contract=older, kg="100", status=status, driver_name="Bobur",
                          transport="01 222 BBB")
    assert _swap(admin_client, older, head, other).status_code == 302
    part.refresh_from_db()
    assert (part.driver_name, part.transport) == ("Bobur", "01 222 BBB")


def test_two_parts_of_one_truck_are_refused(admin_client):
    status = ShipmentStatus.for_kind(birja=True).first()
    contract = _birja_kelishuv()
    head = make_shipment(contract=contract, kg="100", status=status, driver_name="A")
    part = make_shipment(contract=contract, kg="100", status=status, driver_name="A",
                         truck=head)
    assert _swap(admin_client, contract, head, part).status_code == 200


def test_the_list_offers_the_button_only_with_two_loads(admin_client):
    contract, _, _ = _two_loads()
    lone = make_shipment()
    page = admin_client.get("/shipments/").content.decode()
    assert _url(contract) in page
    assert _url(lone.contract) not in page


def test_the_modal_preselects_the_pair(admin_client):
    contract, first, second = _two_loads()
    resp = admin_client.get(_url(contract), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Akmal" in body and "Bobur" in body


def test_a_refusal_inside_the_modal_says_why(admin_client):
    contract, first, _ = _two_loads()
    resp = admin_client.post(_url(contract), {"first": first.pk, "second": first.pk},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 422
    body = resp.content.decode()
    assert "Ikki xil yukni tanlang" in body
    assert 'type="checkbox"' in body
