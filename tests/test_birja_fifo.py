"""Birja yuklari go out oldest kelishuv first — the owner's rule of 2026-09-22.

The rule is the client's: a birja truck comes off the OLDEST birja kelishuv that
still has the marka to send. The operator still picks the kelishuv (the owner's
call — not automatic), so the yuk form only leads with the oldest and opens on it.
The trucks booked before the rule are put where it would have put them by the
one-off `manage.py birja_fifo`, pinned below.
"""
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from conftest import line_data, make_contract, make_shipment
from django.core.management import call_command

from crm.birja import apply_redistribution, plan_redistribution
from crm.models import (
    AuditLog, Contract, ContractLine, Customer, Partner, Sale, Shipment,
    ShipmentExpense, ShipmentStatus, birja_partner,
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


def _booked(contract):
    """[(lot narx, kg)] on the kelishuv's trucks, lot by lot, in lot order."""
    return [(line.price, line.shipped_kg) for line in contract.lines.order_by("position")
            if line.shipped_kg]


# --- the yuk form ------------------------------------------------------------------

def _yuk_form(client, url="/birja/yuklar/new/"):
    return client.get(url).context["form"]


def test_the_birja_kelishuv_picker_leads_with_the_oldest(admin_client):
    newest = _kelishuv("2026-09-18", ("30000", "1.00"))
    oldest = _kelishuv("2026-09-07", ("30000", "1.00"))
    middle = _kelishuv("2026-09-10", ("30000", "1.00"))
    picker = _yuk_form(admin_client).fields["contract"].queryset
    assert list(picker) == [oldest, middle, newest]


def test_a_new_birja_truck_opens_on_the_oldest_kelishuv_with_kg_left(admin_client):
    """A kelishuv already sent in full is off the list, so the next one leads."""
    sent = _kelishuv("2026-09-04", ("3000", "1.00"))
    make_shipment(contract_line=sent.lines.get(), kg="3000", status=_status())
    oldest_open = _kelishuv("2026-09-07", ("30000", "1.00"))
    _kelishuv("2026-09-10", ("30000", "1.00"))
    resp = admin_client.get("/birja/yuklar/new/")
    assert resp.context["form"].initial["contract"] == oldest_open.pk
    assert f'<option value="{oldest_open.pk}" selected>' in resp.content.decode()


def test_the_operator_can_still_book_a_newer_kelishuv(admin_client):
    """Only the default moved — the owner's call: not automatic."""
    _kelishuv("2026-09-07", ("30000", "1.00"))
    newer = _kelishuv("2026-09-10", ("30000", "1.00"))
    resp = admin_client.post("/birja/yuklar/new/", {
        "contract": newer.pk, "status": _status().pk, "sent": "2026-09-20",
        "eta": "2026-09-25", "transport": "01 777 AAA", "note": "",
        **line_data({"contract_line": newer.lines.get().pk, "kg": "25000"})})
    assert resp.status_code == 302
    assert Shipment.objects.get().contract == newer


def test_editing_a_birja_truck_keeps_its_own_kelishuv(admin_client):
    _kelishuv("2026-09-07", ("30000", "1.00"))
    newer = _kelishuv("2026-09-10", ("30000", "1.00"))
    truck = make_shipment(contract_line=newer.lines.get(), kg="1000", status=_status())
    form = _yuk_form(admin_client, f"/shipments/{truck.pk}/edit/")
    assert form.initial["contract"] == newer.pk


def test_the_eron_picker_is_left_as_it_was(admin_client):
    """Newest first and nothing chosen — the rule is the birja's alone."""
    pars = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    older = make_contract(partner=pars, brand="2102", created="2026-07-01")
    newer = make_contract(partner=pars, brand="7000F", created="2026-08-01")
    form = _yuk_form(admin_client, "/shipments/new/")
    assert list(form.fields["contract"].queryset) == [newer, older]
    assert "contract" not in form.initial


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
