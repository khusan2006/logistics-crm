"""Kelishuv xarajatlari — costs that belong to the agreement, not to one truck.

The broker is why this exists: he is paid a percentage of the whole kelishuv, once,
for the kelishuv. Every xarajat in the app before this had to hang off a yuk, so
such a cost was either left out of the books or pinned to whichever truck happened
to be open — which inflated that one load's tannarx and every foyda taken off it.

The money leaves the kassa ONCE, here, and each yuk carries its share per kg through
`Contract.expenses_per_kg` → `landed_cost_per_kg`. These tests pin both halves of
that, and pin that the existing vositachi cut is untouched and adds up beside it.
"""
from decimal import Decimal

import pytest
from django.utils import timezone

from crm.forms import ContractExpenseForm
from crm.models import (
    Contract, ContractExpense, ContractLine, Currency, Partner, Shipment,
    ShipmentExpense, ShipmentLine, ShipmentStatus, SupplierPayment,
    sync_contract_expenses,
)

pytestmark = pytest.mark.django_db

KG = Decimal("20000")
PRICE = Decimal("1.00")
VALUE = KG * PRICE          # $20 000 agreed


@pytest.fixture
def contract(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    c = Contract.objects.create(partner=partner, created="2026-07-01",
                                currency=Currency.USD)
    ContractLine.objects.create(contract=c, brand="LLDPE", kg=KG, price=PRICE)
    return c


def _post(contract, **over):
    data = {"contract": contract.pk, "date": "2026-07-10", "category": "broker",
            "percent": "2", "currency": "usd", "amount": "", "exchange_rate": "12000",
            "method": "cash", "fee_percent": "0", "note": ""}
    data.update(over)
    return data


def _save(contract, **over):
    form = ContractExpenseForm(_post(contract, **over))
    assert form.is_valid(), form.errors
    return form.save()


# --- the two shapes the form takes --------------------------------------------

def test_a_broker_is_a_percentage_of_the_whole_agreement(contract):
    """Not of what has been paid so far — that is the vositachi cut, and the
    difference between the two is the reason both exist."""
    row = _save(contract)
    assert row.percent == Decimal("2")
    assert row.amount == VALUE * 2 / 100          # $400
    assert row.amount_uzs == Decimal("400") * 12000
    assert row.is_percent


def test_a_broker_follows_the_kelishuvs_currency(db):
    """The base is the kelishuv's value, so the fee is in the kelishuv's money. A
    picker saying otherwise would book a percentage of nothing."""
    partner = Partner.objects.create(name="Birja-ish", phone="1", city="T")
    c = Contract.objects.create(partner=partner, created="2026-07-01",
                                currency=Currency.UZS)
    ContractLine.objects.create(contract=c, brand="LLDPE", kg=KG, price=PRICE,
                                price_uzs=Decimal("12000"))
    row = _save(c, currency="usd")                # picker overruled on purpose
    assert row.currency == Currency.UZS
    assert row.amount_uzs == KG * Decimal("12000") * 2 / 100


def test_anything_else_is_a_sum_somebody_was_quoted(contract):
    row = _save(contract, category="other", percent="", amount="150", note="Ekspertiza")
    assert row.percent is None
    assert row.amount == Decimal("150")
    assert not row.is_percent


def test_a_broker_with_no_foiz_is_refused(contract):
    form = ContractExpenseForm(_post(contract, percent=""))
    assert not form.is_valid()
    assert "percent" in form.errors


def test_a_sum_with_no_figure_is_refused(contract):
    form = ContractExpenseForm(_post(contract, category="other", percent="", amount=""))
    assert not form.is_valid()
    assert "amount" in form.errors


def test_a_foiz_of_an_empty_kelishuv_is_refused(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    empty = Contract.objects.create(partner=partner, created="2026-07-01")
    form = ContractExpenseForm(_post(empty))
    assert not form.is_valid()
    assert "mahsulot" in str(form.errors["percent"])


# --- how a yuk gets its share ---------------------------------------------------

def _shipment(contract, kg=Decimal("5000")):
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.for_kind(birja=False).first(),
        sent="2026-07-05")
    ShipmentLine.objects.create(shipment=shipment,
                                contract_line=contract.lines.first(), kg=kg)
    return shipment


def test_the_cost_is_spread_over_the_whole_agreed_kg(contract):
    _save(contract)                                # $400 broker
    contract.refresh_from_db()
    assert contract.expenses_total == Decimal("400")
    assert contract.expenses_per_kg == Decimal("0.02")   # 400 / 20 000


def test_every_yuk_carries_the_same_share_per_kg(contract):
    """A broker is paid for the agreement, not for the third lorry, so kg is the
    only honest split — and a small truck carries proportionally less."""
    _save(contract)
    small, big = _shipment(contract, Decimal("5000")), _shipment(contract, Decimal("15000"))
    contract.refresh_from_db()
    per_kg = contract.expenses_per_kg
    assert small.kg * per_kg == Decimal("100")
    assert big.kg * per_kg == Decimal("300")


def test_it_reaches_the_tannarx(contract):
    shipment = _shipment(contract)
    line = shipment.lines.first()
    before = line.landed_cost_per_kg
    _save(contract)
    contract.refresh_from_db()
    line.refresh_from_db()
    assert line.landed_cost_per_kg == before + Decimal("0.02")


def test_it_adds_up_beside_the_vositachi_cut(contract):
    """Both are kelishuv-level and both spread per kg, and they are DIFFERENT money:
    the vositachi is a slice of each to'lov, the broker a share of the agreement."""
    SupplierPayment.objects.create(contract=contract, date="2026-07-02",
                                   amount=Decimal("10000"),
                                   amount_uzs=Decimal("120000000"),
                                   commission_percent=Decimal("2"),
                                   exchange_rate=Decimal("12000"))
    _save(contract)
    contract.refresh_from_db()
    assert contract.commission_per_kg == Decimal("0.01")   # 200 / 20 000
    assert contract.expenses_per_kg == Decimal("0.02")     # 400 / 20 000
    line = _shipment(contract).lines.first()
    assert line.landed_cost_per_kg == PRICE + Decimal("0.01") + Decimal("0.02")


def test_it_is_not_pushed_down_onto_the_yuklar(contract):
    """One payment, one row. A copy on every truck would put the same money in the
    kassa N+1 times — the share a yuk carries is arithmetic, not a stored xarajat."""
    shipment = _shipment(contract)
    _save(contract)
    assert not ShipmentExpense.objects.filter(shipment=shipment).exists()
    assert ContractExpense.objects.count() == 1


# --- it follows the agreement ---------------------------------------------------

def test_growing_the_kelishuv_re_prices_the_broker(contract):
    """His fee is a share of the agreement, so it moves when the agreement does."""
    row = _save(contract)
    ContractLine.objects.create(contract=contract, brand="HDPE",
                                kg=Decimal("10000"), price=PRICE)
    contract.refresh_from_db()
    sync_contract_expenses(contract)
    row.refresh_from_db()
    assert row.amount == Decimal("30000") * 2 / 100        # $600
    assert row.percent == Decimal("2")


def test_a_typed_sum_is_left_where_somebody_put_it(contract):
    """A kelishuv growing by one marka is no reason to move a figure a person typed."""
    row = _save(contract, category="other", percent="", amount="150")
    ContractLine.objects.create(contract=contract, brand="HDPE",
                                kg=Decimal("10000"), price=PRICE)
    contract.refresh_from_db()
    sync_contract_expenses(contract)
    row.refresh_from_db()
    assert row.amount == Decimal("150")


def test_the_kelishuv_screen_re_prices_through_to_the_row(contract, admin_client):
    from conftest import line_data
    row = _save(contract)
    line = contract.lines.get()
    admin_client.post(f"/contracts/{contract.pk}/edit/", {
        "partner": contract.partner_id, "currency": "usd", "created": "2026-07-01",
        "note": "", **line_data({"id": line.pk, "brand": line.brand,
                                 "kg": "30000", "price": "1.00"}, initial=1)})
    row.refresh_from_db()
    assert row.amount == Decimal("600")


# --- transport: the one turkum that saves no row --------------------------------
#
# It is an ARRANGEMENT, not a payment. It writes the kelishuv's own rate and the
# money turns up later, per yuk, as each truck lands. It shares this screen because
# to the operator it is the same question — what does this kelishuv cost us.

@pytest.fixture
def birja(db):
    from crm.models import birja_partner
    c = Contract.objects.create(partner=birja_partner(), created="2026-07-01",
                                currency=Currency.UZS)
    ContractLine.objects.create(contract=c, brand="LLDPE", kg=KG, price=PRICE,
                                price_uzs=Decimal("12000"))
    return c


def _landed(contract, kg=Decimal("5000")):
    shipment = Shipment.objects.create(
        contract=contract,
        status=ShipmentStatus.for_kind(birja=True).filter(is_arrival=True).first(),
        sent="2026-07-05", arrived="2026-07-20")
    ShipmentLine.objects.create(shipment=shipment,
                                contract_line=contract.lines.first(), kg=kg)
    shipment.save()
    return shipment


def _post_transport(contract, rate="500"):
    return {"contract": contract.pk, "date": "2026-07-10", "category": "transport",
            "rate_per_kg": rate, "percent": "", "amount": "", "currency": "uzs",
            "exchange_rate": "12000", "method": "cash", "fee_percent": "0",
            "note": ""}


def test_transport_writes_the_kelishuv_and_no_row(birja, admin_client):
    admin_client.post("/contract-expenses/new/", _post_transport(birja))
    birja.refresh_from_db()
    assert birja.transport_rate_per_kg == Decimal("500")
    assert not ContractExpense.objects.exists()


def test_the_audit_line_records_the_rate_that_was_saved(birja, admin_client):
    """Off the instance save() returns, not the copy the view loaded from the URL —
    that one still carries the OLD rate, and a line written from it records what the
    rate used to be while reading as a record of the change."""
    from crm.models import AuditLog
    admin_client.post("/contract-expenses/new/", _post_transport(birja, rate="500"))
    admin_client.post("/contract-expenses/new/", _post_transport(birja, rate="700"))
    latest = AuditLog.objects.filter(target_type="Kelishuv").order_by("-id").first()
    assert "700" in latest.summary


def test_saving_it_reaches_the_yuklar_that_already_landed(birja, admin_client):
    shipment = _landed(birja)
    admin_client.post("/contract-expenses/new/", _post_transport(birja))
    row = shipment.expenses.filter(is_auto_transport=True).first()
    assert row is not None and row.amount_uzs == Decimal("5000") * 500


def test_it_is_not_in_the_kelishuvs_xarajat_total(birja, admin_client):
    """Its money is booked per yuk. Counting it here as well would state it twice —
    once as a kelishuv cost and again on every truck."""
    _landed(birja)
    admin_client.post("/contract-expenses/new/", _post_transport(birja))
    birja.refresh_from_db()
    assert birja.expenses_total == Decimal("0")
    assert birja.expenses_per_kg == Decimal("0")


def test_the_eron_road_is_not_offered_it(contract):
    """There a logist quotes the run and hands the driver an avans, which the yuk
    form already asks for. A per-kg box would be a second answer to one question."""
    form = ContractExpenseForm(contract=contract)
    assert "rate_per_kg" not in form.fields
    assert "transport" not in dict(form.fields["category"].choices)


def test_a_rate_needs_kg_to_multiply(db):
    from crm.models import birja_partner
    empty = Contract.objects.create(partner=birja_partner(), created="2026-07-01",
                                    currency=Currency.UZS)
    form = ContractExpenseForm(_post_transport(empty), contract=empty)
    assert not form.is_valid()
    assert "mahsulot" in str(form.errors["rate_per_kg"])


def test_a_transport_with_no_rate_is_refused(birja):
    form = ContractExpenseForm(_post_transport(birja, rate=""), contract=birja)
    assert not form.is_valid()
    assert "rate_per_kg" in form.errors


def test_clearing_it_takes_the_logged_xarajatlar_with_it(birja, admin_client):
    shipment = _landed(birja)
    admin_client.post("/contract-expenses/new/", _post_transport(birja))
    assert shipment.expenses.filter(is_auto_transport=True).exists()
    admin_client.post(f"/contracts/{birja.pk}/transport/clear/", {})
    birja.refresh_from_db()
    assert birja.transport_rate_per_kg is None
    assert not shipment.expenses.filter(is_auto_transport=True).exists()


# --- the kassa ------------------------------------------------------------------

def test_it_leaves_the_kassa_once_and_says_which_kelishuv(contract, admin_client):
    """Dated today, because the kassa opens on the current period — a row outside the
    window is absent for an honest reason and would make this test pass for a wrong
    one."""
    _save(contract, date=str(timezone.localdate()))
    html = admin_client.get("/kassa/").content.decode()
    row = ContractExpense.objects.get()
    assert contract.code in html
    # Its OWN edit link. The fallback branch in the template points at expense_edit,
    # which for this pk is a different xarajat or a 404 — the bug the comment above
    # that block warns about, and the reason this row has a branch of its own.
    assert f"/contract-expenses/{row.pk}/edit/" in html
    assert f"/expenses/{row.pk}/edit/" not in html


def test_it_moves_the_balance(contract, admin_client):
    """Not just the ledger list: a chiqim the balance did not see would make Kassada
    disagree with the safe, which is the one thing this screen cannot do."""
    before = admin_client.get("/kassa/").context["net_out"]
    _save(contract, date=str(timezone.localdate()))
    after = admin_client.get("/kassa/").context["net_out"]
    assert after - before == Decimal("400")


# --- what the yuk screens say ---------------------------------------------------
#
# Both yuk screens print what the TRUCK's own xarajatlar add to a kg, and that is
# all they printed. The kelishuv's share lands in the same tannarx, so a yuk priced
# at 1,2400 with 0,0150 of xarajat showed a tan narx of 1,2736 with nothing on
# either screen to say where the rest of it came from.

def test_the_yuk_page_names_the_kelishuvs_share(contract, admin_client):
    shipment = _shipment(contract)
    _save(contract)                                     # $400 broker → 0,0200/kg
    html = admin_client.get(f"/shipments/{shipment.pk}/").content.decode()
    assert "Kelishuvdan 1 kg ga yana 0,0200 $" in html


def test_the_yuklar_panel_names_it_too(contract, admin_client):
    """The panel is where the tan narx is read next to the xarajatlar, so it is the
    screen the missing figure was missing from."""
    _shipment(contract)
    _save(contract)
    html = admin_client.get("/shipments/").content.decode()
    assert "Kelishuvdan 1 kg ga yana 0,0200 $" in html


def test_it_names_both_halves_when_there_are_two(contract, admin_client):
    """A total of 0,0300 explains a tan narx but not itself. Which of the two costs
    it is decides which screen to go and correct."""
    SupplierPayment.objects.create(contract=contract, date="2026-07-02",
                                   amount=Decimal("10000"),
                                   amount_uzs=Decimal("120000000"),
                                   commission_percent=Decimal("2"),
                                   exchange_rate=Decimal("12000"))
    shipment = _shipment(contract)
    _save(contract)
    html = admin_client.get(f"/shipments/{shipment.pk}/").content.decode()
    assert "vositachi 0,0100 + kelishuv xarajati 0,0200" in html


def test_a_kelishuv_that_adds_nothing_says_nothing(contract, admin_client):
    """Silent rather than a 0,0000 line: a kelishuv with no vositachi and no broker
    puts nothing on the tannarx, and a row saying so is one more figure to read."""
    shipment = _shipment(contract)
    html = admin_client.get(f"/shipments/{shipment.pk}/").content.decode()
    assert "Kelishuvdan" not in html


def test_the_tannarx_is_only_called_equal_when_it_is(contract, admin_client):
    """The claim was made off the YUK's xarajatlar alone, so a truck with none of
    its own made it under a kelishuv carrying a broker — a page stating a tan narx
    two cards above that plainly disagreed with it."""
    shipment = _shipment(contract)
    assert not shipment.expenses.exists()
    equal = "tan narx mahsulot narxiga teng"
    assert equal in admin_client.get(f"/shipments/{shipment.pk}/").content.decode()
    _save(contract)
    html = admin_client.get(f"/shipments/{shipment.pk}/").content.decode()
    assert equal not in html
    assert "Kelishuvdan 1 kg ga yana 0,0200 $" in html
