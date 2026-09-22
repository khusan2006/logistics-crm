"""Birja yuklari come off the oldest birja kelishuv — the owner's rule of 2026-09-22.

However many kg a birja truck carries, they come off the OLDEST birja kelishuv that
still has the marka to send, its lots taken in the order they stand on it. A truck
bigger than what is left there becomes two yuklar, one per kelishuv. Nobody picks a
kelishuv on the form any more, so these pin what the form does instead — and the
one-off command that puts the trucks booked by hand before the rule where it would
have put them.
"""
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from conftest import line_data, make_shipment
from django.core.management import call_command

from crm.birja import apply_redistribution, plan_redistribution
from crm.models import (
    AuditLog, Contract, ContractLine, Customer, Sale, Shipment, ShipmentExpense,
    ShipmentStatus, birja_partner,
)

pytestmark = pytest.mark.django_db

BRAND = "и 1561"


# --- helpers ---------------------------------------------------------------

def _kelishuv(created, *lots, brand=BRAND, **kw):
    """A birja kelishuv of `lots` — (kg, narx) each — in the order they stand."""
    contract = Contract.objects.create(partner=birja_partner(), created=created, **kw)
    for position, (kg, price) in enumerate(lots):
        ContractLine.objects.create(contract=contract, brand=brand, kg=Decimal(kg),
                                    price=Decimal(price), position=position)
    return contract


def _status():
    return ShipmentStatus.for_kind(birja=True).first()


def _post(client, *rows, **extra):
    """A new birja truck through the form: marka/kg rows and nothing else."""
    data = {"status": _status().pk, "sent": "2026-09-20", "eta": "2026-09-25",
            "transport": "01 777 AAA", "note": "",
            **line_data(*({"brand": brand, "kg": kg} for brand, kg in rows))}
    data.update(extra)
    return client.post("/birja/yuklar/new/", data)


def _booked(contract):
    """[(lot narx, kg)] on the kelishuv's trucks, lot by lot, in lot order."""
    return [(line.price, line.shipped_kg) for line in contract.lines.order_by("position")
            if line.shipped_kg]


def _edit(client, shipment, *rows, **extra):
    shipment.refresh_from_db()
    data = {"status": shipment.status_id, "sent": str(shipment.sent),
            "eta": str(shipment.eta or ""), "transport": shipment.transport,
            "note": shipment.note,
            **line_data(*({"brand": brand, "kg": kg} for brand, kg in rows),
                        initial=len({ln.contract_line.brand for ln in shipment.lines.all()}))}
    data.update(extra)
    return client.post(f"/shipments/{shipment.pk}/edit/", data)


# --- a new truck ---------------------------------------------------------------

def test_a_truck_comes_off_the_oldest_kelishuv_not_the_one_entered_first(admin_client):
    """Oldest by the kelishuv sanasi: birja-1 here was struck after birja-2."""
    later = _kelishuv("2026-09-10", ("30000", "1.00"))
    older = _kelishuv("2026-09-07", ("30000", "1.00"))
    assert _post(admin_client, (BRAND, "25000")).status_code == 302
    shipment = Shipment.objects.get()
    assert shipment.contract == older
    assert later.shipped_kg == 0


def test_the_next_truck_takes_what_is_left_before_a_newer_kelishuv(admin_client):
    older = _kelishuv("2026-09-07", ("30000", "1.00"))
    _kelishuv("2026-09-10", ("30000", "1.00"))
    _post(admin_client, (BRAND, "20000"))
    _post(admin_client, (BRAND, "10000"))
    assert [s.contract for s in Shipment.objects.order_by("pk")] == [older, older]


def test_a_kelishuv_s_lots_fill_in_the_order_they_stand(admin_client):
    """Not the cheapest, not the one the operator fancied — the first one on the
    kelishuv, then the next. One truck reaching across two lots stays one yuk."""
    contract = _kelishuv("2026-09-07", ("30000", "1.30"), ("30000", "1.20"),
                         ("30000", "1.10"))
    _post(admin_client, (BRAND, "40000"))
    shipment = Shipment.objects.get()
    assert [(ln.contract_line.price, ln.kg) for ln in shipment.lines.all()] == [
        (Decimal("1.3000"), Decimal("30000")), (Decimal("1.2000"), Decimal("10000"))]
    assert _booked(contract) == [(Decimal("1.3000"), Decimal("30000")),
                                 (Decimal("1.2000"), Decimal("10000"))]


def test_a_truck_bigger_than_the_oldest_s_rest_is_split_into_two_yuklar(admin_client):
    """A yuk belongs to one kelishuv, so the truck becomes two — the same truck on
    both: holat, sanalar and raqam copied, each half saying what it is part of."""
    older = _kelishuv("2026-09-04", ("3000", "1.00"))
    newer = _kelishuv("2026-09-07", ("30000", "1.00"))
    resp = _post(admin_client, (BRAND, "30000"), note="Tarozi: 30.1 t")
    assert resp.status_code == 302

    first, second = Shipment.objects.order_by("pk")
    assert (first.contract, first.kg) == (older, Decimal("3000"))
    assert (second.contract, second.kg) == (newer, Decimal("27000"))
    for yuk in (first, second):
        assert yuk.transport == "01 777 AAA" and yuk.status == _status()
        assert yuk.sent == date(2026, 9, 20) and yuk.origin == "Birja"
        assert yuk.note.startswith("Tarozi: 30.1 t\n")
        assert "Bitta mashina: birja-1 · 3 000 kg + birja-2 · 27 000 kg" in yuk.note
    assert first.created_at == second.created_at


def test_each_half_of_a_split_truck_pays_its_own_kelishuv_s_transport(admin_client):
    _kelishuv("2026-09-04", ("3000", "1.00"), transport_rate_per_kg=Decimal("100"))
    _kelishuv("2026-09-07", ("30000", "1.00"), transport_rate_per_kg=Decimal("110"))
    _post(admin_client, (BRAND, "30000"), status=ShipmentStatus.arrival().pk)
    rows = sorted((e.shipment.contract.code, e.rate_per_kg, e.amount)
                  for e in ShipmentExpense.objects.filter(is_auto_transport=True))
    assert rows == [("birja-1", Decimal("100.0000"), Decimal("300000.00")),
                    ("birja-2", Decimal("110.0000"), Decimal("2970000.00"))]


def test_the_driver_advance_goes_on_one_half_only(admin_client):
    """It is handed to the driver once, however many kelishuvlar the truck spans."""
    from crm.models import Logist
    logist = Logist.objects.create(name="Logist", phone="1")
    _kelishuv("2026-09-04", ("3000", "1.00"))
    _kelishuv("2026-09-07", ("30000", "1.00"))
    _post(admin_client, (BRAND, "30000"), logist=logist.pk, driver_advance="100")
    advances = ShipmentExpense.objects.filter(is_driver_advance=True)
    assert advances.count() == 1
    assert advances.get().shipment == Shipment.objects.order_by("pk").first()


def test_more_than_the_birja_has_left_is_refused_with_the_figure(admin_client):
    _kelishuv("2026-09-04", ("3000", "1.00"))
    resp = _post(admin_client, (BRAND, "5000"))
    assert resp.status_code == 200 and not Shipment.objects.exists()
    assert "Birja kelishuvlarida bu markadan 3 000 kg qolgan" in resp.content.decode()


def test_a_kelishuv_moved_to_kam_qoldiq_is_passed_over(admin_client):
    """The operator has said what it still owes is not coming."""
    short = _kelishuv("2026-09-04", ("30000", "1.00"))
    Contract.objects.filter(pk=short.pk).update(closed_short=True)
    next_one = _kelishuv("2026-09-07", ("30000", "1.00"))
    _post(admin_client, (BRAND, "1000"))
    assert Shipment.objects.get().contract == next_one


def test_the_form_asks_for_the_marka_and_says_where_it_comes_from(admin_client):
    _kelishuv("2026-09-07", ("30000", "1.00"), ("7000", "1.00"))
    _kelishuv("2026-09-04", ("5000", "1.00"), brand="2102")
    html = admin_client.get("/birja/yuklar/new/").content.decode()
    assert 'name="contract"' not in html
    assert "и 1561 · 37 000 kg qolgan · birja-1 dan" in html
    assert "2102 · 5 000 kg qolgan · birja-2 dan" in html
    assert "eng eski birja kelishuvidan olinadi" in html


def test_the_eron_form_still_names_its_kelishuv(admin_client):
    html = admin_client.get("/shipments/new/").content.decode()
    assert 'name="contract"' in html


# --- correcting a truck already booked -------------------------------------------

def test_more_kg_on_a_truck_stay_on_its_own_kelishuv(admin_client):
    """A correction does not move a truck: even with an older kelishuv now free, the
    extra kg join the kelishuv the truck is already on."""
    older = _kelishuv("2026-09-04", ("10000", "1.00"))
    newer = _kelishuv("2026-09-07", ("30000", "1.00"))
    _post(admin_client, (BRAND, "10000"))
    _post(admin_client, (BRAND, "20000"))
    truck = Shipment.objects.get(contract=newer)
    Shipment.objects.get(contract=older).delete()      # birja-1 has room again
    assert _edit(admin_client, truck, (BRAND, "25000")).status_code == 302
    truck.refresh_from_db()
    assert (truck.contract, truck.kg) == (newer, Decimal("25000"))


def test_more_kg_than_its_own_kelishuv_holds_are_split_off(admin_client):
    first = _kelishuv("2026-09-04", ("20000", "1.00"))
    second = _kelishuv("2026-09-07", ("30000", "1.00"))
    _post(admin_client, (BRAND, "20000"))
    truck = Shipment.objects.get()
    assert _edit(admin_client, truck, (BRAND, "21000")).status_code == 302
    truck.refresh_from_db()
    split = Shipment.objects.exclude(pk=truck.pk).get()
    assert (truck.contract, truck.kg) == (first, Decimal("20000"))
    assert (split.contract, split.kg) == (second, Decimal("1000"))
    assert "Bitta mashina: birja-1 · 20 000 kg + birja-2 · 1 000 kg" in split.note


def test_fewer_kg_come_off_the_last_lot(admin_client):
    contract = _kelishuv("2026-09-04", ("30000", "1.30"), ("30000", "1.20"))
    _post(admin_client, (BRAND, "40000"))
    truck = Shipment.objects.get()
    _edit(admin_client, truck, (BRAND, "35000"))
    assert _booked(contract) == [(Decimal("1.3000"), Decimal("30000")),
                                 (Decimal("1.2000"), Decimal("5000"))]


def test_a_truck_cannot_go_below_what_has_been_sold_off_it(admin_client, admin_user):
    _kelishuv("2026-09-04", ("30000", "1.00"))
    _post(admin_client, (BRAND, "20000"), status=ShipmentStatus.arrival().pk)
    truck = Shipment.objects.get()
    customer = Customer.objects.create(name="Mijoz", phone="1", address="T")
    Sale.objects.create(customer=customer, line=truck.lines.get(), kg=Decimal("15000"),
                        price=Decimal("2.00"), created_by=admin_user)
    resp = _edit(admin_client, truck, (BRAND, "10000"))
    assert resp.status_code == 200
    assert "Bu yukdan 15 000 kg sotilgan" in resp.content.decode()
    assert truck.kg == Decimal("20000")


# --- putting the trucks booked by hand where the rule says ------------------------

def _hand_booked():
    """birja-2 is the older kelishuv, but the operator put the first truck on the
    newer birja-1; the second truck is bigger than birja-2's rest."""
    newer = _kelishuv("2026-09-08", ("30000", "1.70"))
    older = _kelishuv("2026-09-07", ("20000", "1.60"), ("10000", "1.50"))
    arrival = ShipmentStatus.arrival()
    first = make_shipment(contract_line=newer.lines.get(), kg="25000", status=arrival,
                          sent="2026-09-17", arrived="2026-09-17")
    second = make_shipment(contract_line=older.lines.first(), kg="15000", status=arrival,
                           sent="2026-09-18", arrived="2026-09-18")
    return newer, older, first, second


def test_the_plan_puts_every_truck_in_the_order_it_went_out(admin_client):
    newer, older, first, second = _hand_booked()
    plan = plan_redistribution()
    assert not plan.problems
    moves = {(m.shipment.pk, m.now.code): [(c.code, kg) for c, kg in m.target]
             for m in plan.moves}
    assert moves == {
        (first.pk, newer.code): [(older.code, Decimal("25000"))],
        (second.pk, older.code): [(older.code, Decimal("5000")),
                                  (newer.code, Decimal("10000"))],
    }


def test_applying_it_moves_the_trucks_and_splits_the_one_across_the_boundary(admin_client):
    newer, older, first, second = _hand_booked()
    apply_redistribution(plan_redistribution())
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.contract, first.kg) == (older, Decimal("25000"))
    assert (second.contract, second.kg) == (older, Decimal("5000"))
    split = Shipment.objects.exclude(pk__in=[first.pk, second.pk]).get()
    assert (split.contract, split.kg) == (newer, Decimal("10000"))
    assert split.arrived == second.arrived and split.created_at == second.created_at
    # birja-2's lots filled in their order: 20 000, then 10 000.
    assert _booked(older) == [(Decimal("1.6000"), Decimal("20000")),
                              (Decimal("1.5000"), Decimal("10000"))]
    assert newer.shipped_kg == Decimal("10000")


def test_a_moved_truck_pays_the_transport_of_the_kelishuv_it_lands_on(admin_client):
    """Rate × the kg the yuk carries AFTER the move — the kg it came in with would
    price the truck off rows it no longer has."""
    newer, older, first, second = _hand_booked()
    Contract.objects.filter(pk=newer.pk).update(transport_rate_per_kg=Decimal("100"))
    Contract.objects.filter(pk=older.pk).update(transport_rate_per_kg=Decimal("110"))
    apply_redistribution(plan_redistribution())
    rows = {(e.shipment_id, e.rate_per_kg): e.amount
            for e in ShipmentExpense.objects.filter(is_auto_transport=True)}
    split = Shipment.objects.exclude(pk__in=[first.pk, second.pk]).get()
    assert rows == {(first.pk, Decimal("110.0000")): Decimal("2750000.00"),   # 25 000
                    (second.pk, Decimal("110.0000")): Decimal("550000.00"),   # 5 000
                    (split.pk, Decimal("100.0000")): Decimal("1000000.00")}   # 10 000


def test_a_second_run_finds_nothing_to_move(admin_client):
    _hand_booked()
    apply_redistribution(plan_redistribution())
    assert plan_redistribution().moves == []


def test_the_sotuvlar_drawn_off_a_moved_truck_keep_their_kg(admin_client, admin_user):
    """A sotuv's kg and its money never move — only which lot it is costed to, the
    same thing a FIFO replay rewrites."""
    newer, older, first, second = _hand_booked()
    customer = Customer.objects.create(name="Mijoz", phone="1", address="T")
    sale = Sale.objects.create(customer=customer, line=first.lines.get(),
                               kg=Decimal("24000"), price=Decimal("2.00"),
                               date=date(2026, 9, 19), created_by=admin_user)
    plan = plan_redistribution()
    assert plan.sold_slices == 1
    apply_redistribution(plan)
    sale.refresh_from_db()
    assert sale.kg == Decimal("24000")
    assert sum(sl.kg for sl in sale.lots.all()) == Decimal("24000")
    # The truck now sits on birja-2's lots, so that is what the sotuv costs.
    assert {sl.line.contract_line.contract for sl in sale.lots.all()} == {older}


def test_a_truck_carrying_two_markalar_is_left_where_it_is(admin_client):
    contract = _kelishuv("2026-09-08", ("30000", "1.00"))
    ContractLine.objects.create(contract=contract, brand="2102", kg=Decimal("5000"),
                                price=Decimal("1.00"), position=1)
    _kelishuv("2026-09-07", ("30000", "1.00"))
    truck = make_shipment(contract_line=contract.lines.get(brand=BRAND), kg="1000",
                          sent="2026-09-17")
    make_shipment(contract_line=contract.lines.get(brand="2102"), kg="1000",
                  sent="2026-09-17")
    truck.lines.create(contract_line=contract.lines.get(brand="2102"), kg=Decimal("500"))
    plan = plan_redistribution()
    assert [(t.pk, why) for t, why in plan.pinned] == [(truck.pk, "bir nechta marka")]
    assert truck.pk not in {m.shipment.pk for m in plan.moves}


def test_the_command_only_shows_the_plan_until_told_to_apply(admin_client, admin_user):
    newer, older, first, second = _hand_booked()
    out = StringIO()
    call_command("birja_fifo", stdout=out)
    assert "ko'chadi: 2 ta" in out.getvalue()
    assert "ikki yukka bo'linadi" in out.getvalue()
    first.refresh_from_db()
    assert first.contract == newer

    call_command("birja_fifo", "--apply", "--user", admin_user.username, stdout=out)
    first.refresh_from_db()
    assert first.contract == older
    assert AuditLog.objects.filter(summary__startswith="Birja FIFO").count() == 2

    out = StringIO()
    call_command("birja_fifo", stdout=out)
    assert "ko'chiriladigan yuk yo'q" in out.getvalue()
