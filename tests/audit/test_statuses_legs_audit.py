"""Audit pass over Holatlar (statuses), the status flow, bosqichlar (legs),
kechikish (delays) and muddat uzaytirish (extend).

A diagnosis, not a fix: every test either PASSES (the behaviour is what the model
and view docstrings say it should be) or is marked xfail carrying the defect it
documents.

The probe families, mapped onto an area that holds almost no money of its own but
decides where the money-bearing rows LIVE:
  (a) round-trip   — a status/leg reorder taken there and back must restore the
                     exact chain, not an equivalent-looking one
  (b) no-drift     — replaying the same action (extend, set-status, move) must not
                     move a date, an order number or a tan narx
  (c) stickiness   — the arrival designation is the one flag the ombor keys off;
                     it must stay where the admin put it
  (d) aggregates   — the pipeline tab counts must account for every load shown, and
                     the FIFO queue (which decides WHICH lot's tan narx a sotuv
                     takes) must not reorder itself behind the operator's back
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import make_contract, make_shipment

from crm.models import (
    Currency, Customer, Sale, Shipment, ShipmentDelay, ShipmentLeg, ShipmentLine,
    ShipmentStatus, arrived_lots, brand_on_hand_kg, fifo_lots,
)

pytestmark = pytest.mark.django_db


# --- helpers ---------------------------------------------------------------

def _chain():
    """The pipeline as the screens see it: (pk, order) in (order, id) sequence."""
    return [(s.pk, s.order) for s in ShipmentStatus.objects.all()]


def _named(name):
    return ShipmentStatus.objects.get(name=name)


def _set_status(client, shipment, status):
    return client.post(f"/shipments/{shipment.pk}/status/", {"status": status.pk})


def _move_status(client, pk, direction=None):
    data = {} if direction is None else {"dir": direction}
    return client.post(f"/statuses/{pk}/move/", data)


def _leg_payload(**kw):
    data = {"from_location": "A", "to_location": "B", "transport": "", "container": "",
            "departed": "", "arrived": "", "note": ""}
    data.update(kw)
    return data


def _add_leg(client, shipment, **kw):
    resp = client.post(f"/legs/new/?shipment={shipment.pk}", _leg_payload(**kw))
    assert resp.status_code == 302, "leg was rejected"
    return ShipmentLeg.objects.filter(shipment=shipment).order_by("-pk").first()


def _leg_chain(shipment):
    return [(leg.from_location, leg.order) for leg in shipment.legs.all()]


def _customer():
    return Customer.objects.create(name="Mijoz", phone="9")


# ---------------------------------------------------------------------------
# status_move — the pipeline order every load list is scanned in
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("edge,direction", [("first", "up"), ("last", "down")])
def test_moving_a_status_off_the_end_of_the_chain_changes_nothing(admin_client, edge, direction):
    """The ends are dead stops. The template disables the buttons there, but the
    view is the thing that must hold — a replayed POST is one page-back away."""
    before = _chain()
    status = ShipmentStatus.objects.first() if edge == "first" else ShipmentStatus.objects.last()
    assert _move_status(admin_client, status.pk, direction).status_code == 302
    assert _chain() == before


def test_moving_a_status_down_then_up_restores_the_exact_chain(admin_client):
    """(a) round-trip. Not merely 'the same sequence' — the same order NUMBERS,
    since anything else means the swap leaked a value somewhere."""
    before = _chain()
    status = ShipmentStatus.objects.all()[1]
    _move_status(admin_client, status.pk, "down")
    assert _chain() != before                      # it really did move
    _move_status(admin_client, status.pk, "up")
    assert _chain() == before


def test_walking_the_last_status_to_the_top_keeps_the_orders_unique_and_dense(admin_client):
    """(d) After a full walk the order column must still be the same set of
    numbers — no duplicate (which would freeze the row) and no drift."""
    before_orders = sorted(s.order for s in ShipmentStatus.objects.all())
    walker = ShipmentStatus.objects.last()
    for _ in range(ShipmentStatus.objects.count() - 1):
        _move_status(admin_client, walker.pk, "up")
    orders = [s.order for s in ShipmentStatus.objects.all()]
    assert sorted(orders) == before_orders
    assert len(set(orders)) == len(orders)
    assert ShipmentStatus.objects.first().pk == walker.pk


def test_moving_the_only_remaining_status_is_a_no_op(admin_client):
    """Boundary: one row, both directions, nothing to swap with."""
    keep = ShipmentStatus.arrival()
    ShipmentStatus.objects.exclude(pk=keep.pk).delete()
    before = _chain()
    for direction in ("up", "down"):
        assert _move_status(admin_client, keep.pk, direction).status_code == 302
    assert _chain() == before


def test_moving_a_deleted_status_is_a_404_and_not_a_reshuffle(admin_client):
    """Boundary: the row the button belonged to is gone (another tab deleted it).
    A stale POST must 404, not silently shuffle whatever sits at that index."""
    victim = _named("Bojxona")
    pk = victim.pk
    admin_client.post(f"/statuses/{pk}/delete/")
    before = _chain()
    assert _move_status(admin_client, pk, "up").status_code == 404
    assert _chain() == before


@pytest.mark.xfail(reason="BUG: status_move (crm/views.py:894) reads the direction as "
                          "`index - 1 if request.POST.get('dir') == 'up' else index + 1`, "
                          "so ANY value that is not exactly 'up' — a missing field, a "
                          "typo, a future 'top'/'bottom' button — is silently treated as "
                          "'down' and reorders the pipeline. leg_move (crm/views.py:1155) "
                          "has the identical shape. An unrecognised direction should be a "
                          "no-op, not a move.",
                   strict=False)
def test_a_move_with_no_direction_does_not_reorder_the_pipeline(admin_client):
    before = _chain()
    _move_status(admin_client, ShipmentStatus.objects.first().pk, direction=None)
    assert _chain() == before


@pytest.mark.xfail(reason="BUG: both reorder views implement 'move' as a swap of the two "
                          "rows' `order` VALUES (crm/views.py:904 and :1164). When two "
                          "rows share an order the swap writes the number back onto "
                          "itself, so the arrows become permanently dead — the operator "
                          "clicks and nothing happens, forever. Nothing prevents equal "
                          "orders: ShipmentLeg.order/ShipmentStatus.order are "
                          "PositiveSmallIntegerField(default=0) with no unique constraint, "
                          "and every row created outside the two create views (import, "
                          "seeding, a shell fix-up, two concurrent creates racing on "
                          "Max('order')) lands on the default. A position-rewrite would "
                          "survive this; a value swap cannot.",
                   strict=False)
def test_two_legs_that_share_an_order_can_still_be_reordered(admin_client):
    shipment = make_shipment()
    first = ShipmentLeg.objects.create(shipment=shipment, from_location="A", to_location="B")
    second = ShipmentLeg.objects.create(shipment=shipment, from_location="B", to_location="C")
    assert first.order == second.order == 0        # the model default, unguarded
    admin_client.post(f"/legs/{second.pk}/move/", {"dir": "up"})
    assert [leg.pk for leg in shipment.legs.all()] == [second.pk, first.pk]


# ---------------------------------------------------------------------------
# the arrival designation — the single flag the whole ombor hangs off
# ---------------------------------------------------------------------------

def test_designating_another_status_as_arrival_demotes_the_old_one(admin_client):
    """(c) Documented in ShipmentStatus (crm/models.py:617): saving another row as
    arrival demotes the rest. Exactly one, always."""
    target = _named("Chegarada")
    admin_client.post(f"/statuses/{target.pk}/edit/", {"name": "Chegarada", "is_arrival": "on"})
    assert ShipmentStatus.objects.filter(is_arrival=True).count() == 1
    assert ShipmentStatus.arrival().pk == target.pk


@pytest.mark.xfail(reason="BUG: ShipmentStatus's docstring (crm/models.py:617) states the "
                          "invariant — 'Exactly one row is the arrival status ... it is "
                          "protected'. Two of the three write paths honour it (save() "
                          "demotes the others, status_delete refuses the arrival row) but "
                          "status_edit (crm/views.py:852) does not: ShipmentStatusForm "
                          "exposes `is_arrival` as a plain checkbox with no clean(), so "
                          "opening the arrival status to fix a typo and leaving the box "
                          "unticked drops the flag entirely. ShipmentStatus.arrival() then "
                          "returns None, no load can ever be marked arrived again, and the "
                          "ombor silently stops receiving stock. Unticking should be "
                          "refused (the flag can only MOVE, never vanish).",
                   strict=False)
def test_the_arrival_flag_cannot_be_edited_away(admin_client):
    arrival = ShipmentStatus.arrival()
    admin_client.post(f"/statuses/{arrival.pk}/edit/", {"name": arrival.name})
    assert ShipmentStatus.arrival() is not None


def test_deleting_a_status_a_yuk_sits_in_is_refused(admin_client):
    """Shipment.status is PROTECT and status_delete catches ProtectedError, so a
    status in use survives the delete instead of taking its loads with it."""
    status = _named("Yo'lda")
    make_shipment(status=status)
    resp = admin_client.post(f"/statuses/{status.pk}/delete/")
    assert resp.status_code == 302
    assert ShipmentStatus.objects.filter(pk=status.pk).exists()
    assert Shipment.objects.get().status_id == status.pk


def test_deleting_an_unused_status_leaves_the_rest_in_order(admin_client):
    """Gaps in the order column are harmless — the chain is read by (order, id) —
    but the surviving rows must keep their relative places."""
    victim = _named("Bojxona")
    expected = [pk for pk, _ in _chain() if pk != victim.pk]
    admin_client.post(f"/statuses/{victim.pk}/delete/")
    assert [pk for pk, _ in _chain()] == expected


# ---------------------------------------------------------------------------
# shipment_set_status — the moment a yuk becomes a warehouse lot
# ---------------------------------------------------------------------------

def test_re_marking_an_already_arrived_yuk_keeps_its_original_date(admin_client):
    """(b) no-drift. `shipment.arrived or timezone.localdate()` exists precisely so
    a repeated arrival does not restamp the date."""
    shipment = make_shipment()
    arrival = ShipmentStatus.arrival()
    Shipment.objects.filter(pk=shipment.pk).update(status=arrival, arrived=date(2026, 1, 15))
    _set_status(admin_client, shipment, arrival)
    shipment.refresh_from_db()
    assert shipment.arrived == date(2026, 1, 15)


@pytest.mark.xfail(reason="BUG: shipment_set_status (crm/views.py:1208) writes "
                          "`shipment.arrived = (shipment.arrived or localdate()) if "
                          "status.is_arrival else None`. Clearing the date on the way out "
                          "is deliberate (tests/test_status_flow.py::"
                          "test_leaving_arrival_clears_date), but it makes the `or` half — "
                          "which exists precisely to PRESERVE a known arrival date — "
                          "unreachable across a round trip: one mis-click onto another "
                          "status DELETES `arrived`, and putting the status straight back "
                          "stamps TODAY. The real landing date is then unrecoverable, and "
                          "no AuditLog row carries the old value either (the trail records "
                          "only the status names). The exit path should park the date, not "
                          "destroy it.",
                   strict=False)
def test_a_status_round_trip_keeps_the_original_arrival_date(admin_client):
    shipment = make_shipment()
    arrival, on_road = ShipmentStatus.arrival(), _named("Yo'lda")
    Shipment.objects.filter(pk=shipment.pk).update(status=arrival, arrived=date(2026, 1, 15))
    _set_status(admin_client, shipment, on_road)      # mis-click
    _set_status(admin_client, shipment, arrival)      # …and put it straight back
    shipment.refresh_from_db()
    assert shipment.arrived == date(2026, 1, 15)


@pytest.mark.xfail(reason="BUG (the money-visible end of the same root): fifo_lots "
                          "(crm/models.py:1272) consumes lots ordered by "
                          "`shipment__arrived`, and each lot carries its OWN "
                          "landed_cost_per_kg. Because a status round trip restamps "
                          "`arrived` to today, a mis-click on an old lot throws it to the "
                          "back of the FIFO queue, so the very next sotuv of that marka "
                          "silently books a DIFFERENT tan narx (here 2.00 instead of "
                          "1.00 $/kg) and reports a different foyda. Nothing on screen "
                          "says why.",
                   strict=False)
def test_a_status_round_trip_does_not_reprice_the_next_sotuv(admin_client):
    contract = make_contract(kg="10000", price="1.00")
    line = contract.lines.first()
    arrival, on_road = ShipmentStatus.arrival(), _named("Yo'lda")

    cheap = make_shipment(contract_line=line, kg="100", price="1.00")
    dear = make_shipment(contract_line=line, kg="100", price="2.00")
    Shipment.objects.filter(pk=cheap.pk).update(status=arrival, arrived=date(2026, 1, 10))
    Shipment.objects.filter(pk=dear.pk).update(status=arrival, arrived=date(2026, 6, 10))

    queue = fifo_lots("LLDPE")
    assert [lot.shipment_id for lot in queue] == [cheap.pk, dear.pk]
    assert queue[0].landed_cost_per_kg == Decimal("1.0000")

    cheap.refresh_from_db()
    _set_status(admin_client, cheap, on_road)
    _set_status(admin_client, cheap, arrival)

    queue = fifo_lots("LLDPE")
    assert [lot.shipment_id for lot in queue] == [cheap.pk, dear.pk]
    assert queue[0].landed_cost_per_kg == Decimal("1.0000")


@pytest.mark.xfail(reason="BUG: shipment_set_status (crm/views.py:1204) gates only the "
                          "way IN — `if status.is_arrival and not "
                          "request.user.is_admin_role: raise PermissionDenied`. The way "
                          "OUT is ungated, so a translator (who by design sees no money "
                          "at all — see tests/test_delays.py::"
                          "test_translator_detail_has_no_money) can pick any other status "
                          "on an arrived load and thereby delete its arrival date, pull "
                          "the lot out of the ombor and remove its tan narx from stock "
                          "valuation. Marking a load arrived is admin-only; un-marking it "
                          "must be too.",
                   strict=False)
def test_a_translator_cannot_pull_an_arrived_lot_back_out_of_the_ombor(admin_client,
                                                                       translator_client):
    shipment = make_shipment()
    _set_status(admin_client, shipment, ShipmentStatus.arrival())
    assert arrived_lots().filter(shipment=shipment).exists()

    _set_status(translator_client, shipment, _named("Yo'lda"))
    shipment.refresh_from_db()
    assert shipment.arrived is not None
    assert arrived_lots().filter(shipment=shipment).exists()


@pytest.mark.xfail(reason="BUG: shipment_set_status (crm/views.py:1201) un-arrives a load "
                          "with no regard for what already hangs off it. A lot that has "
                          "been SOLD from leaves arrived_lots() the moment its status is "
                          "moved back, so the ombor loses the physical kg (60 of 100 here "
                          "drop to 0) while the Sale, the mijoz's qarz and the profit "
                          "computed off that lot's tan narx all survive — the books now "
                          "carry goods sold from a load the system says never landed. "
                          "Shipment.status is PROTECTed against this kind of orphaning "
                          "and ShipmentLine.sales is PROTECT too; the un-arrive path is "
                          "the hole. It should be refused while sotuvlar exist.",
                   strict=False)
def test_a_lot_that_has_been_sold_from_cannot_be_un_arrived(admin_client, admin_user):
    shipment = make_shipment(kg="100", price="1.00")
    _set_status(admin_client, shipment, ShipmentStatus.arrival())
    lot = shipment.lines.first()
    Sale.objects.create(customer=_customer(), line=lot, kg=Decimal("40"),
                        price=Decimal("2"), currency=Currency.USD,
                        exchange_rate=Decimal("12000"), created_by=admin_user)
    assert brand_on_hand_kg("LLDPE") == Decimal("60")

    _set_status(admin_client, shipment, _named("Yo'lda"))
    assert Sale.objects.filter(line=lot).exists()
    assert brand_on_hand_kg("LLDPE") == Decimal("60")


@pytest.mark.xfail(reason="BUG: shipment_list (crm/views.py:960) builds its tabs as "
                          "`[... for st in statuses if show_all or not st.is_arrival]`, "
                          "on the stated assumption 'in the active view nothing can be "
                          "sitting in it'. Nothing enforces that: status_edit can move the "
                          "arrival designation onto a status loads are ALREADY sitting in "
                          "(and does not stamp `arrived` on them), so those loads stay in "
                          "the active queryset — counted in `total`, rendered as rows — "
                          "with no tab to reach them and no tab counting them. The tab "
                          "counts stop adding up to the list they describe and the loads "
                          "drop out of the pipeline the operator scans.",
                   strict=False)
def test_the_pipeline_tabs_account_for_every_active_load(admin_client):
    bojxona = _named("Bojxona")
    make_shipment(status=bojxona)
    admin_client.post(f"/statuses/{bojxona.pk}/edit/", {"name": "Bojxona", "is_arrival": "on"})
    context = admin_client.get("/shipments/").context
    assert context["total"] == 1
    assert sum(tab["count"] for tab in context["tabs"]) == context["total"]


# ---------------------------------------------------------------------------
# legs (bosqichlar)
# ---------------------------------------------------------------------------

def test_legs_are_appended_with_a_dense_gapless_order(admin_client):
    shipment = make_shipment()
    for name in ("A", "B", "C"):
        _add_leg(admin_client, shipment, from_location=name, to_location=name + "2")
    assert _leg_chain(shipment) == [("A", 1), ("B", 2), ("C", 3)]


@pytest.mark.parametrize("edge,direction", [(0, "up"), (-1, "down")])
def test_moving_a_leg_off_the_end_changes_nothing(admin_client, edge, direction):
    shipment = make_shipment()
    for name in ("A", "B", "C"):
        _add_leg(admin_client, shipment, from_location=name, to_location=name + "2")
    before = _leg_chain(shipment)
    leg = list(shipment.legs.all())[edge]
    admin_client.post(f"/legs/{leg.pk}/move/", {"dir": direction})
    assert _leg_chain(shipment) == before


def test_moving_a_leg_up_then_down_restores_the_exact_order_values(admin_client):
    """(a) round-trip on the leg chain: same rows, same numbers."""
    shipment = make_shipment()
    for name in ("A", "B", "C"):
        _add_leg(admin_client, shipment, from_location=name, to_location=name + "2")
    before = _leg_chain(shipment)
    middle = list(shipment.legs.all())[1]
    admin_client.post(f"/legs/{middle.pk}/move/", {"dir": "up"})
    assert _leg_chain(shipment) == [("B", 1), ("A", 2), ("C", 3)]
    admin_client.post(f"/legs/{middle.pk}/move/", {"dir": "down"})
    assert _leg_chain(shipment) == before


def test_moving_a_leg_never_touches_another_loads_legs(admin_client):
    """Two trucks, both with legs numbered from 1. Reordering one must not reach
    into the other's chain."""
    contract = make_contract(kg="10000")
    line = contract.lines.first()
    mine = make_shipment(contract_line=line, kg="100")
    theirs = make_shipment(contract_line=line, kg="100")
    for shipment in (mine, theirs):
        for name in ("A", "B"):
            _add_leg(admin_client, shipment, from_location=name, to_location=name + "2")
    untouched = _leg_chain(theirs)
    second = list(mine.legs.all())[1]
    admin_client.post(f"/legs/{second.pk}/move/", {"dir": "up"})
    assert _leg_chain(mine) == [("B", 1), ("A", 2)]
    assert _leg_chain(theirs) == untouched


def test_deleting_a_middle_leg_leaves_the_rest_reorderable(admin_client):
    """Boundary: delete a row the others' numbering was built around. Gaps are
    fine (the chain reads by (order, id)); a broken ↑/↓ would not be."""
    shipment = make_shipment()
    legs = [_add_leg(admin_client, shipment, from_location=name, to_location=name + "2")
            for name in ("A", "B", "C")]
    admin_client.post(f"/legs/{legs[1].pk}/delete/")
    assert _leg_chain(shipment) == [("A", 1), ("C", 3)]
    fresh = _add_leg(admin_client, shipment, from_location="D", to_location="D2")
    assert _leg_chain(shipment) == [("A", 1), ("C", 3), ("D", 4)]
    admin_client.post(f"/legs/{fresh.pk}/move/", {"dir": "up"})
    assert [name for name, _ in _leg_chain(shipment)] == ["A", "D", "C"]


def test_moving_the_only_leg_is_a_no_op(admin_client):
    shipment = make_shipment()
    only = _add_leg(admin_client, shipment, from_location="A", to_location="B")
    for direction in ("up", "down"):
        admin_client.post(f"/legs/{only.pk}/move/", {"dir": direction})
    assert _leg_chain(shipment) == [("A", 1)]
    only.refresh_from_db()
    assert only.order == 1


# ---------------------------------------------------------------------------
# extend / delays
# ---------------------------------------------------------------------------

def test_extend_records_exactly_one_delay_and_moves_the_eta_once(admin_client):
    shipment = make_shipment(eta=date(2026, 7, 10))
    resp = admin_client.post(f"/shipments/{shipment.pk}/extend/",
                             {"new_eta": "2026-07-20", "reason": "Chegarada navbat"})
    assert resp.status_code == 302
    shipment.refresh_from_db()
    assert shipment.eta == date(2026, 7, 20)
    delay = ShipmentDelay.objects.get()
    assert (delay.old_eta, delay.new_eta) == (date(2026, 7, 10), date(2026, 7, 20))


def test_a_rejected_extend_leaves_the_eta_and_the_history_alone(admin_client):
    """Boundary: blank sabab. The audit trail exists so a push always carries a
    reason — a refused submission must move nothing."""
    shipment = make_shipment(eta=date(2026, 7, 10))
    resp = admin_client.post(f"/shipments/{shipment.pk}/extend/",
                             {"new_eta": "2026-07-20", "reason": ""})
    assert resp.status_code == 200
    shipment.refresh_from_db()
    assert shipment.eta == date(2026, 7, 10)
    assert not ShipmentDelay.objects.exists()


def test_re_submitting_the_same_extend_does_not_move_the_eta_further(admin_client):
    """(b) no-drift: a double-tapped modal must land on the SAME date, not push
    the deadline twice."""
    shipment = make_shipment(eta=date(2026, 7, 10))
    payload = {"new_eta": "2026-07-20", "reason": "Chegarada navbat"}
    admin_client.post(f"/shipments/{shipment.pk}/extend/", payload)
    admin_client.post(f"/shipments/{shipment.pk}/extend/", payload)
    shipment.refresh_from_db()
    assert shipment.eta == date(2026, 7, 20)


def test_extend_moves_the_eta_and_nothing_else_on_the_load(admin_client):
    """(b) The report is 'values change with no reason'. Extend saves with
    update_fields=['eta'], so every other column — including the arrival date and
    the lot's money — must read back bit-identical."""
    shipment = make_shipment(kg="100", price="1.2345", eta=date(2026, 7, 10),
                             transport="01 777 AAA")
    lot = shipment.lines.first()
    before_line = (lot.price, lot.price_uzs, lot.currency, lot.exchange_rate, lot.kg)
    before_load = {f.name: getattr(shipment, f.attname) for f in Shipment._meta.fields
                   if f.name != "eta"}

    admin_client.post(f"/shipments/{shipment.pk}/extend/",
                      {"new_eta": "2026-07-25", "reason": "Bojxona tekshiruvi"})

    shipment.refresh_from_db()
    lot.refresh_from_db()
    assert shipment.eta == date(2026, 7, 25)
    assert {f.name: getattr(shipment, f.attname) for f in Shipment._meta.fields
            if f.name != "eta"} == before_load
    assert (lot.price, lot.price_uzs, lot.currency, lot.exchange_rate, lot.kg) == before_line


def test_extending_a_load_that_never_had_an_eta_records_a_null_old_date(admin_client):
    """Boundary: blank/None old value. ShipmentDelay.old_eta is null=True for
    exactly this, and the history table prints an em dash for it."""
    shipment = make_shipment(eta=None)
    admin_client.post(f"/shipments/{shipment.pk}/extend/",
                      {"new_eta": "2026-07-20", "reason": "Kech jo'natildi"})
    delay = ShipmentDelay.objects.get()
    assert delay.old_eta is None and delay.new_eta == date(2026, 7, 20)
    shipment.refresh_from_db()
    assert shipment.eta == date(2026, 7, 20)


def test_a_delay_belongs_only_to_its_own_load(admin_client):
    """(d) The ⏳ counter on the loads list is a per-load count; extending one
    truck must not show up on its stablemate."""
    contract = make_contract(kg="10000")
    line = contract.lines.first()
    late = make_shipment(contract_line=line, kg="100", eta=date.today() - timedelta(days=2))
    other = make_shipment(contract_line=line, kg="100", eta=date.today() + timedelta(days=9))
    admin_client.post(f"/shipments/{late.pk}/extend/",
                      {"new_eta": (date.today() + timedelta(days=5)).isoformat(),
                       "reason": "Navbat"})
    assert late.delays.count() == 1 and other.delays.count() == 0
    other.refresh_from_db()
    assert other.eta == date.today() + timedelta(days=9)
