"""The deliberate exception: a COST blends both currencies, a QARZ never does.

Everything else in the app now keeps the two apart. What somebody owes is measured
in the currency it was agreed in and is never converted, because a converted qarz is
a figure neither side signed up to and it moves on its own as the kurs does.

A cost cannot work that way. One kg of granula has one price, and the money that put
it on the shelf arrives in both currencies at once: the mol bought in dollars, the
transport paid in so'm, the bojxona in whichever was to hand. 12 of the 35 real
trucks carry expenses in both. Refusing to convert here would not produce two honest
figures, it would produce no tannarx at all — and with it no foyda, no ombor
qiymati and no hisobot.

So these tests exist to stop the currency rule from being "finished" onto the cost
side by a later change. Every assertion here is a place where mixing is correct.
The blend is always at each row's OWN entry-day kurs, never at today's, so a cost
still cannot drift after the fact.
"""
from decimal import Decimal

import pytest

from conftest import make_contract, make_shipment
from crm.models import (
    Currency, Customer, Sale, ShipmentExpense, ShipmentLine, ShipmentStatus,
    stock_value, transit_value,
)

pytestmark = pytest.mark.django_db


def _arrived_lot(contract, kg="1000", rate="12000"):
    """One arrived truck carrying the whole kelishuv, priced off it."""
    shipment = make_shipment(contract=contract, kg=kg, status=ShipmentStatus.arrival(),
                             sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16")
    lot = shipment.lines.get()
    ShipmentLine.objects.filter(pk=lot.pk).update(exchange_rate=Decimal(rate))
    return ShipmentLine.objects.get(pk=lot.pk)


def _spend(shipment, usd, uzs, rate, date="2026-07-10"):
    """One expense typed in dollars and one typed in so'm, on the same truck."""
    ShipmentExpense.objects.create(
        shipment=shipment, date=date, amount=Decimal(usd),
        amount_uzs=Decimal(usd) * Decimal("12000"),
        currency=Currency.USD, exchange_rate=Decimal("12000"))
    ShipmentExpense.objects.create(
        shipment=shipment, date=date, amount=(Decimal(uzs) / Decimal(rate)).quantize(Decimal("0.01")),
        amount_uzs=Decimal(uzs), currency=Currency.UZS, exchange_rate=Decimal(rate))


def test_a_trucks_expenses_total_across_both_currencies():
    """$200 of bojxona and 2 600 000 so'm of transport on one truck is $400 of cost,
    each side converted at the kurs of the day it was actually spent."""
    contract = make_contract(kg="1000", price="1.00")
    lot = _arrived_lot(contract)
    _spend(lot.shipment, usd="200", uzs="2600000", rate="13000")

    shipment = lot.shipment
    assert shipment.expenses_total == Decimal("400.00")     # 200 + 2 600 000/13 000
    assert shipment.expense_per_kg == Decimal("0.4")


def test_the_landed_cost_of_a_kg_is_one_figure_built_from_both():
    contract = make_contract(kg="1000", price="1.00")
    lot = _arrived_lot(contract)
    _spend(lot.shipment, usd="200", uzs="2600000", rate="13000")

    lot = ShipmentLine.objects.get(pk=lot.pk)
    assert lot.landed_cost_per_kg == Decimal("1.4000")      # 1.00 mol + 0.40 yo'l
    # and the so'm face of that cost is the blend restated at the lot's own kurs,
    # not a sum of so'm columns — the one figure that cannot be kept independent
    assert lot.landed_cost_per_kg_uzs == Decimal("16800.00")


def test_a_som_kelishuv_owes_som_but_still_costs_in_dollars():
    """The two rules side by side on ONE kelishuv. What we owe the hamkor is so'm and
    only so'm; what the granula cost us is a dollar figure with so'm expenses folded
    into it, because the ombor and the foyda cannot be told two numbers."""
    contract = make_contract(kg="1000", price="1.00", price_uzs="12650",
                             currency=Currency.UZS)
    contract.lines.update(exchange_rate=Decimal("12650"))
    lot = _arrived_lot(contract, rate="12650")
    _spend(lot.shipment, usd="50", uzs="632500", rate="12650")

    lot = ShipmentLine.objects.get(pk=lot.pk)
    # the qarz side: one currency, untouched by any conversion
    assert contract.currency == Currency.UZS
    assert contract.payable_left_own == Decimal("12650000.00")
    # the cost side: 1.00 mol + (50 + 50)/1000 yo'l, both expenses folded in
    assert lot.landed_cost_per_kg == Decimal("1.1000")


def test_stock_value_costs_a_som_priced_lot_in_both_columns():
    """Ombor qiymati has to be one figure per currency across every lot, whatever
    each was agreed in — a shelf holding so'm-bought and dollar-bought granula is
    still one shelf worth one amount."""
    som_contract = make_contract(kg="1000", price="1.00", price_uzs="12650",
                                 currency=Currency.UZS)
    som_contract.lines.update(exchange_rate=Decimal("12650"))
    _arrived_lot(som_contract, rate="12650")

    dollar_contract = make_contract(kg="1000", price="2.00")
    _arrived_lot(dollar_contract)

    total, total_uzs, kg = stock_value()
    assert kg == Decimal("2000.000")
    assert total == Decimal("3000.00")                     # 1 000×1.00 + 1 000×2.00
    assert total_uzs > 0                                   # each lot at its own kurs


def test_transit_value_does_the_same_for_goods_still_moving():
    contract = make_contract(kg="1000", price="1.00", price_uzs="12650",
                             currency=Currency.UZS)
    contract.lines.update(exchange_rate=Decimal("12650"))
    make_shipment(contract=contract, kg="1000", status=ShipmentStatus.objects.first(),
                  sent="2026-07-05", eta="2026-07-20")

    total, total_uzs, kg, loads = transit_value()
    assert (kg, loads) == (Decimal("1000.000"), 1)
    assert total == Decimal("1000.00")
    assert total_uzs == Decimal("12650000.00")


def test_profit_is_measured_against_the_blended_cost():
    """A sotuv's foyda is its narx minus the landed cost — and that cost carries the
    so'm expenses. Split the currencies here and a truck whose freight was paid in
    so'm would look more profitable than one whose freight was paid in dollars."""
    contract = make_contract(kg="1000", price="1.00")
    lot = _arrived_lot(contract)
    _spend(lot.shipment, usd="200", uzs="2600000", rate="13000")
    customer = Customer.objects.create(name="Alisher Mebel", phone="1", address="T")

    lot = ShipmentLine.objects.get(pk=lot.pk)
    sale = Sale.objects.create(
        line=lot, customer=customer, kg=Decimal("500"), price=Decimal("2.00"),
        price_uzs=Decimal("24000"), currency=Currency.USD,
        exchange_rate=Decimal("12000"), date="2026-07-18")

    assert sale.cost_price == Decimal("1.4000")            # the blend, not just the mol
    assert sale.profit == Decimal("300.00")                # (2.00 − 1.40) × 500


def test_dropping_the_som_expense_moves_the_cost():
    """The guard with teeth: if a later change ever stops folding so'm expenses into
    the dollar cost, this figure falls and the test says so."""
    contract = make_contract(kg="1000", price="1.00")
    lot = _arrived_lot(contract)
    _spend(lot.shipment, usd="200", uzs="2600000", rate="13000")
    with_som = ShipmentLine.objects.get(pk=lot.pk).landed_cost_per_kg

    ShipmentExpense.objects.filter(currency=Currency.UZS).delete()
    without_som = ShipmentLine.objects.get(pk=lot.pk).landed_cost_per_kg

    assert with_som == Decimal("1.4000")
    assert without_som == Decimal("1.2000")
    assert with_som != without_som
