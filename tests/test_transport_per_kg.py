"""Birja transport: a kelishuv's per-kg rate, logged as a xarajat when a yuk lands.

On the exchange road transport is not a figure anybody types. The kelishuv names a
price per kilo, the haydovchi is paid on what he brings in, and he is paid when he
brings it — so the xarajat is derived from three facts already being maintained:
the rate, the yuk's kg, and the day it arrived.

That makes the row unlike every other xarajat in the books, and these tests are
mostly about the consequences. It follows the arrival date. It disappears when the
yuk goes back on the road. It re-prices when the rate is corrected — landed loads
included, by the owner's decision — but it keeps the kurs it was booked at, because
re-rating money already handed over would restate a past figure from an unrelated
edit. And no screen that edits xarajatlar by hand is allowed near it.
"""
from decimal import Decimal

import pytest

from crm.forms import ExpenseGridForm
from crm.models import (
    Contract, ContractLine, Currency, Partner, Shipment, ShipmentExpense,
    ShipmentLine, ShipmentStatus, birja_partner, sync_contract_birja_transport,
)

pytestmark = pytest.mark.django_db

KG = Decimal("400")
RATE = Decimal("500")          # so'm a kilo
TOTAL = KG * RATE              # 200 000 so'm
ARRIVED = "2026-07-20"


def _contract(partner, rate=None, currency=Currency.UZS):
    contract = Contract.objects.create(partner=partner, created="2026-07-01",
                                       currency=currency,
                                       transport_rate_per_kg=rate)
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("20000"), price=Decimal("1.00"),
                                price_uzs=Decimal("12000"))
    return contract


def _shipment(contract, kg=KG, arrived=None, birja=True):
    statuses = ShipmentStatus.for_kind(birja=birja)
    status = (statuses.filter(is_arrival=True) if arrived else statuses).first()
    shipment = Shipment.objects.create(contract=contract, status=status,
                                       sent="2026-07-05", arrived=arrived)
    ShipmentLine.objects.create(shipment=shipment,
                                contract_line=contract.lines.first(), kg=kg)
    shipment.save()            # lines now exist, so the kg is knowable
    return shipment


def _auto(shipment):
    return shipment.expenses.filter(is_auto_transport=True).first()


@pytest.fixture
def birja_contract(db):
    return _contract(birja_partner(), rate=RATE)


# --- when the row exists at all -------------------------------------------------

def test_a_landed_yuk_logs_its_transport(birja_contract):
    row = _auto(_shipment(birja_contract, arrived=ARRIVED))
    assert row is not None
    assert row.rate_per_kg == RATE
    assert row.amount_uzs == TOTAL
    assert row.currency == Currency.UZS
    assert row.category == ShipmentExpense.Category.TRANSPORT
    assert str(row.date) == ARRIVED


def test_a_yuk_on_the_road_logs_nothing(birja_contract):
    """He is paid when he brings it in. Until then there is nothing to log — the
    kelishuv's rate is an arrangement, not a xarajat."""
    assert _auto(_shipment(birja_contract)) is None


def test_a_kelishuv_with_no_rate_logs_nothing(db):
    """Empty means there is no such arrangement, which is what a birja kelishuv says
    until somebody fills it in."""
    assert _auto(_shipment(_contract(birja_partner()), arrived=ARRIVED)) is None


def test_the_eron_road_is_untouched(db):
    """A hamkor kelishuv is not offered the rate at all, and pays its driver through
    the logist's avans instead."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    shipment = _shipment(_contract(partner), arrived=ARRIVED, birja=False)
    assert _auto(shipment) is None
    assert not shipment.expenses.exists()


# --- it follows the yuk ---------------------------------------------------------

def test_the_xarajat_moves_with_the_arrival_date(birja_contract):
    shipment = _shipment(birja_contract, arrived=ARRIVED)
    shipment.arrived = "2026-07-25"
    shipment.save(update_fields=["arrived"])
    assert str(_auto(shipment).date) == "2026-07-25"


def test_going_back_on_the_road_takes_the_xarajat_with_it(birja_contract):
    """The mirror of the haydovchi avansi going when its logist is removed. A row
    left behind would sit in the kassa as money owed on a load that has not landed."""
    shipment = _shipment(birja_contract, arrived=ARRIVED)
    assert _auto(shipment) is not None
    shipment.status = ShipmentStatus.for_kind(birja=True).filter(is_arrival=False).first()
    shipment.arrived = None
    shipment.save(update_fields=["status", "arrived"])
    assert _auto(shipment) is None


def test_a_kg_correction_re_prices_it(birja_contract):
    shipment = _shipment(birja_contract, arrived=ARRIVED)
    line = shipment.lines.first()
    line.kg = Decimal("300")
    line.save(update_fields=["kg"])
    shipment.save()
    assert _auto(shipment).amount_uzs == Decimal("300") * RATE


# --- it follows the kelishuv ----------------------------------------------------

def test_correcting_the_rate_re_prices_landed_yuklar(birja_contract):
    """The owner's decision, and the opposite of what a typed CashEntry does. It is
    right here because nobody typed this one: a kelishuv whose rate was wrong was
    wrong on every truck that ever moved under it."""
    landed = _shipment(birja_contract, arrived=ARRIVED)
    on_road = _shipment(birja_contract)
    birja_contract.transport_rate_per_kg = Decimal("600")
    birja_contract.save(update_fields=["transport_rate_per_kg"])
    sync_contract_birja_transport(birja_contract)
    assert _auto(landed).amount_uzs == KG * Decimal("600")
    assert _auto(on_road) is None


def test_the_xarajatlar_modal_re_prices_through_to_the_yuk(birja_contract,
                                                          admin_client):
    """The wiring, not just the rule: a rate typed on the kelishuv's Xarajatlar
    screen reaches a yuk that nobody opened. Without the form's re-sync the
    correction would sit on the kelishuv and only reach a truck the next time
    somebody happened to save it.

    That screen is also the ONLY place the rate is typed now — it used to be a box
    on the kelishuv header form, and this test used to post there."""
    landed = _shipment(birja_contract, arrived=ARRIVED)
    admin_client.post("/contract-expenses/new/", {
        "contract": birja_contract.pk, "date": ARRIVED, "category": "transport",
        "rate_per_kg": "600", "percent": "", "amount": "", "currency": "uzs",
        "exchange_rate": "12000", "method": "cash", "fee_percent": "0", "note": ""})
    birja_contract.refresh_from_db()
    assert birja_contract.transport_rate_per_kg == Decimal("600")
    assert _auto(landed).amount_uzs == KG * Decimal("600")


def test_filling_the_rate_in_backfills_yuklar_that_already_landed(db):
    """The path every birja yuk in the books takes the day this ships: they landed
    long before any kelishuv carried a rate, so they carry no xarajat. Typing the
    rate has to reach them, not just the trucks that land afterwards."""
    contract = _contract(birja_partner(), rate=None)
    landed = _shipment(contract, arrived=ARRIVED)
    assert _auto(landed) is None
    contract.transport_rate_per_kg = RATE
    contract.save(update_fields=["transport_rate_per_kg"])
    sync_contract_birja_transport(contract)
    row = _auto(landed)
    assert row is not None and row.amount_uzs == TOTAL
    assert str(row.date) == ARRIVED


def test_clearing_the_rate_removes_the_xarajatlar(birja_contract):
    landed = _shipment(birja_contract, arrived=ARRIVED)
    birja_contract.transport_rate_per_kg = None
    birja_contract.save(update_fields=["transport_rate_per_kg"])
    sync_contract_birja_transport(birja_contract)
    assert _auto(landed) is None


def test_re_pricing_keeps_the_kurs_it_was_booked_at(birja_contract):
    """Re-rating a landed row at today's kurs would restate the so'm value of cash
    handed over weeks ago, triggered by an edit somewhere else entirely."""
    landed = _shipment(birja_contract, arrived=ARRIVED)
    booked = _auto(landed).exchange_rate
    ShipmentExpense.objects.filter(pk=_auto(landed).pk).update(
        exchange_rate=Decimal("9999"))
    birja_contract.transport_rate_per_kg = Decimal("600")
    birja_contract.save(update_fields=["transport_rate_per_kg"])
    sync_contract_birja_transport(birja_contract)
    row = _auto(landed)
    assert row.exchange_rate == Decimal("9999") != booked
    assert row.amount_uzs == KG * Decimal("600")
    assert row.amount == (KG * Decimal("600") / Decimal("9999")).quantize(Decimal("0.01"))


def test_an_unchanged_sync_leaves_the_row_alone(birja_contract):
    """This runs on every yuk save, so a rewrite that changed nothing would churn the
    audit trail and the row's own ordering on every holat click."""
    shipment = _shipment(birja_contract, arrived=ARRIVED)
    before = _auto(shipment)
    shipment.save()
    shipment.save()
    after = _auto(shipment)
    assert after.pk == before.pk
    assert shipment.expenses.filter(is_auto_transport=True).count() == 1


# --- the kassa reads it like any other transport --------------------------------

def test_the_money_leaves_on_the_day_it_landed(birja_contract):
    """Transport is in ARRIVAL_CATEGORIES, so the till waits for the ombor gate. The
    row is dated the arrival, so the two agree and nothing is deferred twice."""
    row = _auto(_shipment(birja_contract, arrived=ARRIVED))
    assert not row.is_pending
    assert str(row.cash_date) == ARRIVED
    assert row.total_out_uzs == TOTAL


def test_it_lands_in_the_yuks_tannarx(birja_contract):
    shipment = _shipment(birja_contract, arrived=ARRIVED)
    assert shipment.expenses_total_uzs == TOTAL
    assert shipment.expense_per_kg == shipment.expenses_total / KG


# --- no hand-editing screen may touch it ----------------------------------------

def test_the_grid_does_not_speak_for_it(birja_contract):
    """The box would be rewritten by the next sync, so it is named under the box and
    not shown in it — the same answer the haydovchi avansi gets."""
    shipment = _shipment(birja_contract, arrived=ARRIVED)
    form = ExpenseGridForm(shipment=shipment)
    transport = ShipmentExpense.Category.TRANSPORT
    assert transport not in form.recorded
    assert form.others[transport] == [_auto(shipment)]
    assert form["amount_transport"].value() is None


def test_the_grid_cannot_delete_it(birja_contract):
    """An empty Transport box means "no hand-entered transport", not "remove the
    kelishuv's"."""
    shipment = _shipment(birja_contract, arrived=ARRIVED)
    data = {"shipment": shipment.pk, "date": "2026-07-21", "currency": "uzs",
            "method": "cash", "exchange_rate": "12000", "note": "", "fee_percent": "0",
            "amount_loader": "65000"}
    form = ExpenseGridForm(data, shipment=shipment)
    assert form.is_valid(), form.errors
    form.save(None)
    assert _auto(shipment) is not None


def test_the_edit_screen_sends_you_to_the_kelishuv(birja_contract, admin_client):
    shipment = _shipment(birja_contract, arrived=ARRIVED)
    row = _auto(shipment)
    for url in (f"/expenses/{row.pk}/edit/", f"/expenses/{row.pk}/delete/"):
        response = admin_client.get(url)
        assert response.status_code == 302
        assert response.url == f"/shipments/{shipment.pk}/"
    assert _auto(shipment) is not None
