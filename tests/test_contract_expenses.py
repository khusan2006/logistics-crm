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
    """The single-xarajat form's payload — still what `contract_expense_edit` takes,
    and what the ContractExpenseForm tests below bind directly."""
    return {"contract": contract.pk, "date": "2026-07-10", "category": "transport",
            "rate_per_kg": rate, "percent": "", "amount": "", "currency": "uzs",
            "exchange_rate": "12000", "method": "cash", "fee_percent": "0",
            "note": ""}


def _grid(contract, **over):
    """What the Xarajatlar modal posts: a box per turkum, filled in one pass.

    Every box is blank unless the caller names it — a blank box adds nothing, and
    on transport it also leaves the arrangement exactly where it is."""
    data = {"contract": contract.pk, "date": "2026-07-10", "currency": "uzs",
            "method": "cash", "exchange_rate": "12000", "fee_percent": "0",
            "note": "", "amount_broker": "", "amount_transport": "",
            "amount_other": "", "row_broker": "", "row_other": ""}
    data.update(over)
    return data


def test_transport_writes_the_kelishuv_and_no_row(birja, admin_client):
    admin_client.post("/contract-expenses/new/", _grid(birja, amount_transport="500"))
    birja.refresh_from_db()
    assert birja.transport_rate_per_kg == Decimal("500")
    assert not ContractExpense.objects.exists()


def test_the_audit_line_records_the_rate_that_was_saved(birja, admin_client):
    """Off the instance save() returns, not the copy the view loaded from the URL —
    that one still carries the OLD rate, and a line written from it records what the
    rate used to be while reading as a record of the change."""
    from crm.models import AuditLog
    admin_client.post("/contract-expenses/new/", _grid(birja, amount_transport="500"))
    admin_client.post("/contract-expenses/new/", _grid(birja, amount_transport="700"))
    latest = AuditLog.objects.filter(target_type="Kelishuv").order_by("-id").first()
    assert "700" in latest.summary


def test_saving_it_reaches_the_yuklar_that_already_landed(birja, admin_client):
    shipment = _landed(birja)
    admin_client.post("/contract-expenses/new/", _grid(birja, amount_transport="500"))
    row = shipment.expenses.filter(is_auto_transport=True).first()
    assert row is not None and row.amount_uzs == Decimal("5000") * 500


def test_it_is_not_in_the_kelishuvs_xarajat_total(birja, admin_client):
    """Its money is booked per yuk. Counting it here as well would state it twice —
    once as a kelishuv cost and again on every truck."""
    _landed(birja)
    admin_client.post("/contract-expenses/new/", _grid(birja, amount_transport="500"))
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
    admin_client.post("/contract-expenses/new/", _grid(birja, amount_transport="500"))
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


# --- the grid ---------------------------------------------------------------
#
# The Xarajatlar modal is the yuk's grid now: a box per turkum, filled in one pass,
# opening on whatever the kelishuv already carries. Three boxes that are not three
# of a kind — a foiz, a per-kg rate and a plain summa — which is what these pin.

@pytest.fixture
def birja_valued(db):
    """A birja kelishuv worth something, so a broker foiz has a base to multiply."""
    from crm.models import birja_partner
    c = Contract.objects.create(partner=birja_partner(), created="2026-07-01",
                                currency=Currency.USD)
    ContractLine.objects.create(contract=c, brand="LLDPE", kg=KG, price=PRICE)
    return c


def test_one_submission_writes_every_box(birja_valued, admin_client):
    """The whole point of the grid. A birja kelishuv routinely carries a broker AND
    a transport narx AND something agreed on the side; that was three trips through
    a form whose fields moved under the turkum picker each time."""
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_broker="2",
        amount_transport="500", amount_other="150"))
    birja_valued.refresh_from_db()
    assert birja_valued.transport_rate_per_kg == Decimal("500")
    rows = {row.category: row for row in birja_valued.expenses.all()}
    assert set(rows) == {"broker", "other"}
    assert rows["broker"].percent == Decimal("2")
    assert rows["broker"].amount == VALUE * Decimal("0.02")   # $400
    assert rows["other"].amount == Decimal("150")


def test_the_broker_box_takes_a_foiz_not_a_sum(birja_valued, admin_client):
    """`sync_contract_expenses` re-multiplies the foiz when the kelishuv's value
    moves. A box that stored a flat sum would be silently overwritten by that."""
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_broker="2"))
    row = birja_valued.expenses.get()
    assert row.is_percent
    ContractLine.objects.create(contract=birja_valued, brand="HDPE",
                                kg=KG, price=PRICE)
    sync_contract_expenses(birja_valued)
    row.refresh_from_db()
    assert row.amount == (VALUE * 2) * Decimal("0.02")        # $800


def test_a_box_opens_on_the_row_it_stands_for_and_rewrites_it(birja_valued,
                                                              admin_client):
    """Not a second row beside it. Coming back to add a transport narx to a
    kelishuv that already carries a broker must not duplicate the broker."""
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_broker="2"))
    row = birja_valued.expenses.get()
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_broker="3", row_broker=row.pk))
    assert birja_valued.expenses.count() == 1
    row.refresh_from_db()
    assert row.percent == Decimal("3")
    assert row.amount == VALUE * Decimal("0.03")


def test_clearing_a_box_deletes_the_row_it_opened_with(birja_valued, admin_client):
    from crm.models import AuditLog
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_other="150"))
    row = birja_valued.expenses.get()
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_other="", row_other=row.pk))
    assert not birja_valued.expenses.exists()
    # The row is gone, so the line names none — Django clears the pk on delete, and
    # a target id of None recorded as if it were one is worse than no target at all.
    line = AuditLog.objects.filter(action=AuditLog.Action.DELETE,
                                   target_type="Kelishuv xarajati").get()
    assert birja_valued.code in line.summary and "1 ta" in line.summary


def test_a_row_added_while_the_modal_sat_open_is_left_alone(birja_valued,
                                                            admin_client):
    """The grid speaks for what the operator had in front of them, not for whatever
    the kelishuv holds by the time it is submitted. Without row_<turkum> a modal
    opened before a colleague added a xarajat would delete that xarajat on save."""
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_other="150"))
    added = birja_valued.expenses.get()
    # ...submitted from a modal drawn before `added` existed: its box is blank and
    # its row_ hidden is empty, which is not the same as a box that was cleared.
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_broker="2"))
    assert ContractExpense.objects.filter(pk=added.pk).exists()


def test_an_empty_transport_box_leaves_the_arrangement_alone(birja_valued,
                                                             admin_client):
    """The one box that does not clear on being emptied. Its rate has already
    written a xarajat onto every yuk that landed, and taking N of those out of the
    kassa is not something a blank box should do quietly — that is what
    `contract_transport_clear` is for, and it asks first."""
    landed = _landed(birja_valued)
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_transport="500"))
    assert landed.expenses.filter(is_auto_transport=True).exists()
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_transport="", amount_other="150"))
    birja_valued.refresh_from_db()
    assert birja_valued.transport_rate_per_kg == Decimal("500")
    assert landed.expenses.filter(is_auto_transport=True).exists()


def test_the_eron_road_gets_no_transport_box(contract, admin_client):
    """A logist quotes the run there and the yuk form asks for the haydovchi avansi
    — a per-kg box would be a second and contradictory answer to one question."""
    from crm.forms import ContractExpenseGridForm
    form = ContractExpenseGridForm(contract=contract)
    assert "amount_transport" not in form.fields
    assert "amount_broker" in form.fields and "amount_other" in form.fields


def test_an_empty_submission_is_refused(birja_valued, admin_client):
    from crm.forms import ContractExpenseGridForm
    form = ContractExpenseGridForm(_grid(birja_valued), contract=birja_valued)
    assert not form.is_valid()
    assert "Kamida bitta" in str(form.errors)


def test_a_broker_foiz_on_an_empty_kelishuv_is_refused(db):
    from crm.forms import ContractExpenseGridForm
    from crm.models import birja_partner
    empty = Contract.objects.create(partner=birja_partner(), created="2026-07-01",
                                    currency=Currency.USD)
    form = ContractExpenseGridForm(_grid(empty, amount_broker="2"), contract=empty)
    assert not form.is_valid()
    assert "mahsulot" in str(form.errors["amount_broker"])


def test_a_sum_box_keeps_its_own_valyuta(birja_valued, admin_client):
    """One box wired in so'm beside a broker booked in the kelishuv's dollars is one
    submission, the same override the yuk's grid carries."""
    admin_client.post("/contract-expenses/new/", _grid(
        birja_valued, currency="usd", amount_broker="2", amount_other="1200000",
        currency_other="uzs"))
    rows = {row.category: row for row in birja_valued.expenses.all()}
    assert rows["other"].currency == Currency.UZS
    assert rows["other"].amount_uzs == Decimal("1200000")
    # The broker follows the KELISHUV's money whatever the shared picker says — the
    # value it is a percentage of is the kelishuv's.
    assert rows["broker"].currency == Currency.USD


def test_the_modal_draws_the_grid(birja_valued, admin_client):
    html = admin_client.get(
        f"/contract-expenses/new/?contract={birja_valued.pk}").content.decode()
    assert 'name="amount_broker"' in html
    assert 'name="amount_transport"' in html
    assert 'name="amount_other"' in html
