"""Transport and gruzchi leave the kassa when the truck is UNLOADED, not when the
bill is written down.

The driver is settled with when he delivers and the loaders when they have carried it
in, so booking those out of the till the moment somebody typed the row showed money
gone that was still in the safe — sometimes for weeks, while the load was on the road.
Bojxona and deklarant are deliberately untouched: those are paid AT the border, long
before the warehouse, and waiting for arrival would misstate the till in the other
direction and for longer.

What must NOT move is any figure about what the goods COST. A tannarx is settled when
the obligation exists; when the cash happens to leave the safe is a fact about the
till, not about the price of a kg. Both halves are pinned here.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from crm.models import (
    Contract, ContractLine, CustomsAgent, Currency, Logist, Partner, Shipment,
    ShipmentExpense, ShipmentLine, ShipmentStatus, cash_date_expression,
    kassa_cash_by_currency, pending_expenses_by_currency,
)

pytestmark = pytest.mark.django_db

RATE = Decimal("12000")


def _shipment(arrived=None, kg="10000"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal(kg), price=Decimal("1.00"),
        price_uzs=RATE)
    status = ShipmentStatus.arrival() if arrived else ShipmentStatus.objects.first()
    shipment = Shipment.objects.create(
        contract=contract, status=status, sent="2026-07-05", arrived=arrived)
    ShipmentLine.objects.create(shipment=shipment, contract_line=line, kg=Decimal(kg))
    return shipment


def _expense(shipment, category="transport", amount="100", date="2026-07-10", **kw):
    return ShipmentExpense.objects.create(
        shipment=shipment, date=date, category=category, amount=Decimal(amount),
        amount_uzs=Decimal(amount) * RATE, method=kw.pop("method", "cash"), **kw)


class TestWhichRowsWait:
    def test_transport_and_gruzchi_wait_for_the_ombor(self):
        moving = _shipment()
        for category in ("transport", "loader"):
            expense = _expense(moving, category)
            assert expense.waits_for_arrival is True
            assert expense.is_pending is True
            assert expense.total_out == Decimal("0")
            assert expense.cash_date is None

    def test_bojxona_and_the_rest_still_leave_when_written(self):
        """They are paid AT the border, long before the warehouse. Waiting for
        arrival would misstate the till in the other direction and for longer."""
        moving = _shipment()
        for category in ("customs", "declarant", "road", "cert", "other"):
            expense = _expense(moving, category)
            assert expense.waits_for_arrival is False
            assert expense.is_pending is False
            assert expense.total_out == Decimal("100.00")
            assert expense.cash_date == date(2026, 7, 10)

    def test_a_row_a_holder_funded_never_waits(self):
        """That cash left when we topped the logist or the bojxonachi up, which has
        already happened and cannot be waited for."""
        moving = _shipment()
        logist = Logist.objects.create(name="Sardor")
        agent = CustomsAgent.objects.create(name="Buxoro")
        by_logist = _expense(moving, "transport", logist=logist)
        by_agent = _expense(moving, "loader", customs_agent=agent)
        for expense in (by_logist, by_agent):
            assert expense.waits_for_arrival is False
            assert expense.is_pending is False
            # Zero because somebody else paid it, which was already true.
            assert expense.total_out == Decimal("0")
            assert expense.pending_out == Decimal("0")


class TestWhenTheMoneyLeaves:
    def test_arriving_is_what_releases_it(self):
        shipment = _shipment()
        expense = _expense(shipment, "transport", "100", date="2026-07-10")
        assert expense.total_out == Decimal("0")

        shipment.status = ShipmentStatus.arrival()
        shipment.arrived = date(2026, 7, 20)
        shipment.save()

        expense = ShipmentExpense.objects.select_related("shipment").get(pk=expense.pk)
        assert expense.is_pending is False
        assert expense.total_out == Decimal("100.00")
        assert expense.total_out_uzs == Decimal("1200000.00")
        # Paid the day it landed, not the day the bill was written.
        assert expense.cash_date == date(2026, 7, 20)

    def test_a_bill_written_after_arrival_leaves_that_same_day(self):
        """Money cannot leave before anybody has recorded that it is owed. This is
        the half that lets a xarajat be added at ANY point in a load's life."""
        shipment = _shipment(arrived=date(2026, 7, 20))
        expense = _expense(shipment, "loader", "100", date="2026-07-28")
        assert expense.is_pending is False
        assert expense.total_out == Decimal("100.00")
        assert expense.cash_date == date(2026, 7, 28)

    def test_moving_a_load_back_off_omborda_puts_the_money_back(self):
        """Derived, not booked, so a holat correction cannot leave the till holding a
        payment that never happened."""
        shipment = _shipment(arrived=date(2026, 7, 20))
        expense = _expense(shipment, "transport")
        assert expense.total_out == Decimal("100.00")

        shipment.status = ShipmentStatus.objects.exclude(is_arrival=True).first()
        shipment.arrived = None
        shipment.save()

        expense = ShipmentExpense.objects.select_related("shipment").get(pk=expense.pk)
        assert expense.is_pending is True and expense.total_out == Decimal("0")

    def test_exactly_one_of_the_two_figures_is_ever_non_zero(self):
        """`total_out` and `pending_out` are twins: "already spent" and "still in the
        safe" must never both claim the same bill, or the board double-counts it."""
        for arrived in (None, date(2026, 7, 20)):
            expense = _expense(_shipment(arrived=arrived), "transport")
            assert bool(expense.total_out) != bool(expense.pending_out)
            assert (expense.total_out + expense.pending_out) == Decimal("100.00")


class TestTheTill:
    def test_a_pending_bill_is_still_in_the_kassa(self):
        shipment = _shipment()
        _expense(shipment, "transport", "100")
        assert dict(kassa_cash_by_currency()).get(Currency.USD, Decimal("0")) \
            == Decimal("0")          # nothing in, nothing out

        # …and the same bill on a landed truck HAS left.
        landed = _shipment(arrived=date(2026, 7, 20))
        _expense(landed, "transport", "100")
        assert dict(kassa_cash_by_currency())[Currency.USD] == Decimal("-100.00")

    def test_the_kassa_says_what_is_committed(self, admin_client):
        shipment = _shipment()
        _expense(shipment, "transport", "100")
        _expense(shipment, "loader", "40")
        _expense(shipment, "customs", "70")          # not a waiting turkum

        resp = admin_client.get("/kassa/?davr=all")
        assert dict(resp.context["pending_split"])[Currency.USD] == Decimal("140.00")
        html = resp.content.decode()
        assert "Kutilayotgan xarajatlar" in html

    def test_no_pending_line_when_nothing_is_waiting(self, admin_client):
        _expense(_shipment(arrived=date(2026, 7, 20)), "transport")
        resp = admin_client.get("/kassa/?davr=all")
        assert resp.context["pending_split"] == []
        assert "Kutilayotgan xarajatlar" not in resp.content.decode()

    def test_pending_money_is_never_netted_off_kassada(self, admin_client):
        """Kassada is checked against what is physically in the safe, and this money
        IS in the safe. The note beside it is a caveat, never a subtraction."""
        before = admin_client.get("/kassa/?davr=all").context["cash_total"]
        _expense(_shipment(), "transport", "100")
        after = admin_client.get("/kassa/?davr=all")
        assert after.context["cash_total"] == before
        assert dict(after.context["pending_split"])[Currency.USD] == Decimal("100.00")


class TestTheLedgerAndItsWindow:
    def test_a_pending_row_is_in_no_period_at_all(self, admin_client):
        """A chiqim in the Iyul daftar that the Iyul-end balance does not reflect is a
        kassa disagreeing with itself."""
        _expense(_shipment(), "transport", "100", date="2026-07-10")
        rows = admin_client.get("/kassa/", {"from": "2026-07-01", "to": "2026-07-31"}) \
            .context["outflow_page"]
        assert [r for r in rows if r["kind"] == "expense"] == []

    def test_it_files_under_the_day_the_till_moved(self, admin_client):
        """Written in Iyul, landed in Avgust: it belongs to Avgust's chiqim."""
        shipment = _shipment(arrived=date(2026, 8, 3))
        _expense(shipment, "transport", "100", date="2026-07-10")

        july = admin_client.get("/kassa/", {"from": "2026-07-01", "to": "2026-07-31"})
        assert [r for r in july.context["outflow_page"] if r["kind"] == "expense"] == []

        august = admin_client.get("/kassa/", {"from": "2026-08-01", "to": "2026-08-31"})
        rows = [r for r in august.context["outflow_page"] if r["kind"] == "expense"]
        assert [r["date"] for r in rows] == [date(2026, 8, 3)]
        assert [r["amount"] for r in rows] == [Decimal("100.00")]

    def test_the_opening_balance_reads_the_same_day(self, admin_client):
        """Otherwise the waterfall starts below the line it has to close on."""
        shipment = _shipment(arrived=date(2026, 8, 3))
        _expense(shipment, "transport", "100", date="2026-07-10")

        august = admin_client.get("/kassa/", {"from": "2026-08-01", "to": "2026-08-31"})
        opening, closing = (august.context["waterfall"][0],
                            august.context["waterfall"][-1])
        # The bill was written in Iyul but had not left the safe when Avgust opened,
        # so nothing is carried in — it is one of Avgust's own movements.
        assert opening["label"] == "Boshlang'ich qoldiq"
        assert opening["amount"] == Decimal("0")
        assert closing["running"] == Decimal("-100.00")

        # Read from Iyul instead and it is neither carried in NOR spent in the period.
        july = admin_client.get("/kassa/", {"from": "2026-07-01", "to": "2026-07-31"})
        assert july.context["waterfall"][0]["amount"] == Decimal("0")
        assert july.context["waterfall"][-1]["running"] == Decimal("0")


def test_the_sql_twin_agrees_with_the_property_row_by_row():
    """`cash_date_expression()` and `ShipmentExpense.cash_date` are one rule written
    twice — once for the database and once to be read. Two spellings of one rule is
    precisely the pair that drifts, so they are checked against each other over every
    shape that exists: waiting and not, funded by us and by a holder, landed before
    the bill and after it."""
    logist = Logist.objects.create(name="Sardor")
    moving, landed_early = _shipment(), _shipment(arrived=date(2026, 7, 1))
    landed_late = _shipment(arrived=date(2026, 8, 3))

    for shipment in (moving, landed_early, landed_late):
        for category, _label in ShipmentExpense.Category.choices:
            _expense(shipment, category, date="2026-07-10")
        _expense(shipment, "transport", date="2026-07-10", logist=logist)

    rows = (ShipmentExpense.objects.select_related("shipment", "logist")
            .annotate(_sql=cash_date_expression()))
    assert rows.count() == 24
    for row in rows:
        assert row._sql == row.cash_date, (
            f"#{row.pk} {row.category} arrived={row.shipment.arrived}: "
            f"SQL {row._sql} vs property {row.cash_date}")


def test_no_cost_figure_moved():
    """The tannarx is settled when the obligation exists. When the cash leaves the
    safe is a fact about the till and must change no price anywhere."""
    moving = _shipment(kg="10000")
    landed = _shipment(arrived=date(2026, 7, 20), kg="10000")
    for shipment in (moving, landed):
        _expense(shipment, "transport", "100")

    assert moving.expenses_total == landed.expenses_total == Decimal("100.00")
    assert moving.expense_per_kg == landed.expense_per_kg
    assert (moving.lines.first().landed_cost_per_kg
            == landed.lines.first().landed_cost_per_kg)
    # …and the one that has not arrived is the one still holding its money.
    assert moving.expenses.first().is_pending is True
    assert landed.expenses.first().is_pending is False


def test_pending_totals_are_bucketed_per_currency():
    """Never added across, like every other heap on that board."""
    shipment = _shipment()
    _expense(shipment, "transport", "100", currency="usd", exchange_rate=RATE)
    _expense(shipment, "loader", "50", currency="uzs", exchange_rate=RATE)
    split = dict(pending_expenses_by_currency())
    assert split[Currency.USD] == Decimal("100.00")
    assert split[Currency.UZS] == Decimal("600000.00")     # 50 × 12 000, so'm side
