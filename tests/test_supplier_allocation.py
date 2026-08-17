"""A hamkor to'lov spreads across the kelishuv's products, and what is left is the
hamkor's avans.

The two rules the kelishuv owner asked for:

* Name a marka and overpay it — the extra goes on the NEXT product of the same
  kelishuv rather than overpaying the one that was named, and only becomes an avans
  once every product is covered.
* Hand over a zaklad naming nothing — it splits across the products by mashina
  count, five trucks against five splitting a payment in half.

An avans belongs to the HAMKOR, not to the kelishuv it happened to be paid on.
"""
from decimal import Decimal

import pytest

from conftest import make_contract, make_shipment
from crm.models import (
    ContractLine, Partner, SupplierPayment, allocate_supplier_payment,
    partner_advance_total, reconcile_supplier_allocations,
    unspent_supplier_payment_amount,
)

pytestmark = pytest.mark.django_db


def _contract_two_markalar(price_a="1.00", price_b="1.00", kg_a="5000", kg_b="5000",
                           trucks_a=5, trucks_b=5, ship=True):
    """A kelishuv covering two markalar, each with its own truck plan."""
    contract = make_contract(brand="209 campaund", kg=kg_a, price=price_a)
    first = contract.lines.get()
    first.planned_trucks = trucks_a
    first.save(update_fields=["planned_trucks"])
    second = ContractLine.objects.create(
        contract=contract, brand="7000 campaund", kg=Decimal(kg_b),
        price=Decimal(price_b), planned_trucks=trucks_b)
    if ship:
        make_shipment(contract=contract, kg=kg_a)
        make_shipment(contract=contract, kg=kg_b, contract_line=second)
    return contract, first, second


def _pay(contract, amount, line=None, date="2026-07-02"):
    payment = SupplierPayment.objects.create(
        contract=contract, contract_line=line, date=date,
        amount=Decimal(amount), amount_uzs=Decimal(amount) * 12000,
        exchange_rate=Decimal("12000"), method="cash")
    allocate_supplier_payment(payment)
    return payment


def _spread(payment):
    """{marka: summa} — where this to'lov actually landed, summed per marka."""
    out = {}
    for a in payment.allocations.select_related("line"):
        out[a.line.brand] = out.get(a.line.brand, Decimal("0")) + a.amount
    return out


# ── 1. Nomlangan marka: ortiqchasi keyingisiga ─────────────────────────────────

def test_paying_a_marka_exactly_puts_it_all_on_that_marka(db):
    contract, first, _second = _contract_two_markalar()
    payment = _pay(contract, "5000", line=first)
    assert _spread(payment) == {"209 campaund": Decimal("5000.00")}
    assert unspent_supplier_payment_amount(payment) == Decimal("0.00")


def test_overpaying_a_marka_spills_onto_the_next_one(db):
    """The owner's own example: 209 owes 5 000, we send 7 000, and the 2 000 buys
    the next product rather than overpaying the one we named."""
    contract, first, second = _contract_two_markalar()
    payment = _pay(contract, "7000", line=first)
    assert _spread(payment) == {"209 campaund": Decimal("5000.00"),
                                "7000 campaund": Decimal("2000.00")}
    assert first.paid_total == Decimal("5000.00")
    assert second.paid_total == Decimal("2000.00")


def test_what_no_marka_can_take_becomes_the_hamkor_s_avans(db):
    """Both products together are worth 10 000; 12 000 sent leaves 2 000 that is not
    a payment for anything yet."""
    contract, first, _second = _contract_two_markalar()
    payment = _pay(contract, "12000", line=first)
    assert sum(_spread(payment).values()) == Decimal("10000.00")
    assert unspent_supplier_payment_amount(payment) == Decimal("2000.00")
    assert partner_advance_total(contract.partner)[0] == Decimal("2000.00")


def test_naming_the_last_marka_wraps_round_to_the_first(db):
    """Money has to find every product that is still owed before it is called an
    avans — naming the last one must not send the remainder straight past the rest."""
    contract, first, second = _contract_two_markalar()
    payment = _pay(contract, "7000", line=second)
    assert _spread(payment) == {"7000 campaund": Decimal("5000.00"),
                                "209 campaund": Decimal("2000.00")}


def test_a_second_to_lov_starts_where_the_first_one_stopped(db):
    """Each product remembers what is already on it, so two to'lovlar do not both
    fill the same one."""
    contract, first, second = _contract_two_markalar()
    _pay(contract, "5000", line=first)
    later = _pay(contract, "5000", line=first, date="2026-07-03")
    assert _spread(later) == {"7000 campaund": Decimal("5000.00")}
    assert first.paid_total == Decimal("5000.00")
    assert second.paid_total == Decimal("5000.00")


# ── 2. Zaklad: mashina soniga qarab ────────────────────────────────────────────

def test_a_zaklad_splits_by_truck_count(db):
    """Five trucks against five: 10 000 splits in half. Nothing has shipped yet —
    a zaklad is paid BEFORE the trucks go, which is the whole point of it."""
    contract, _first, _second = _contract_two_markalar(ship=False)
    payment = _pay(contract, "10000")
    assert _spread(payment) == {"209 campaund": Decimal("5000.00"),
                                "7000 campaund": Decimal("5000.00")}


def test_an_uneven_truck_plan_splits_in_its_own_proportion(db):
    """Six trucks against two is 3:1, so 8 000 goes 6 000 / 2 000."""
    contract, _first, _second = _contract_two_markalar(
        kg_a="6000", kg_b="2000", trucks_a=6, trucks_b=2, ship=False)
    payment = _pay(contract, "8000")
    assert _spread(payment) == {"209 campaund": Decimal("6000.00"),
                                "7000 campaund": Decimal("2000.00")}


def test_a_share_a_marka_cannot_take_falls_through_to_the_other(db):
    """The split is a starting point, not a straitjacket: a product already covered
    passes its share on rather than swallowing money it does not owe."""
    contract, first, second = _contract_two_markalar(ship=False)
    _pay(contract, "5000", line=first)                      # 209 fully covered
    zaklad = _pay(contract, "6000", date="2026-07-03")
    # 3 000 was its share, but 209 has nothing left to take — it all lands on 7000.
    assert _spread(zaklad) == {"7000 campaund": Decimal("5000.00")}
    assert unspent_supplier_payment_amount(zaklad) == Decimal("1000.00")


def test_a_kelishuv_with_no_truck_plan_still_places_the_money(db):
    """No plan and nothing sent: there is nothing to weigh by, so the money simply
    runs the kelishuv in order instead of refusing to place itself."""
    contract, _first, _second = _contract_two_markalar(trucks_a=None, trucks_b=None,
                                                       ship=False)
    payment = _pay(contract, "7000")
    assert _spread(payment) == {"209 campaund": Decimal("5000.00"),
                                "7000 campaund": Decimal("2000.00")}


# ── 3. Avans hamkorniki, kelishuvniki emas ─────────────────────────────────────

def test_the_avans_is_read_across_all_of_a_hamkor_s_kelishuvlar(db):
    """Money over on one kelishuv is credit with the hamkor, so it is counted
    wherever that hamkor is asked about."""
    contract, first, _second = _contract_two_markalar()
    partner = contract.partner
    other = make_contract(brand="ftor", kg="1000", price="1.00", partner=partner)
    _pay(contract, "12000", line=first)                     # 2 000 over
    _pay(other, "1500")                                     # 500 over
    assert partner_advance_total(partner)[0] == Decimal("2500.00")


def test_another_hamkor_s_avans_is_not_ours(db):
    contract, first, _second = _contract_two_markalar()
    _pay(contract, "12000", line=first)
    stranger = Partner.objects.create(name="Boshqa", phone="2", city="Tehron")
    assert partner_advance_total(stranger)[0] == Decimal("0")


# ── 4. Qayta yuritish ──────────────────────────────────────────────────────────

def test_re_running_does_not_stack_a_second_set_of_slices(db):
    """Called again — after an edit, a new truck, another to'lov — it re-answers
    rather than adding to yesterday's answer."""
    contract, first, _second = _contract_two_markalar()
    payment = _pay(contract, "7000", line=first)
    allocate_supplier_payment(payment)
    allocate_supplier_payment(payment)
    assert payment.allocations.count() == 2
    assert sum(a.amount for a in payment.allocations.all()) == Decimal("7000.00")


def test_reconcile_places_every_to_lov_of_a_kelishuv_oldest_first(db):
    contract, first, second = _contract_two_markalar()
    _pay(contract, "4000", line=first, date="2026-07-02")
    _pay(contract, "4000", line=first, date="2026-07-03")
    reconcile_supplier_allocations(contract)
    assert first.paid_total == Decimal("5000.00")
    assert second.paid_total == Decimal("3000.00")
