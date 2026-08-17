"""Bojxona: the outside party we send clearing money to, per load, in advance.

Two rules carry the whole feature.

The first is the logist rule, and it is the same one: money leaves the kassa when we
fund the bojxonachi; what they later pay at bojxona prices that yuk but must NOT
appear in the kassa again.

The second is what makes this its own feature. The money goes out as an ESTIMATE —
~40 mln so a truck clears — and the real figure only lands afterwards. So every load
carries a gap between what was sent for it and what it cost, in both directions, and
that gap is the thing nobody could see before.
"""

import re
from decimal import Decimal

import pytest

from crm.models import (
    Contract, ContractLine, Currency, CustomsAgent, CustomsPayment, Logist,
    Partner, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus,
    customs_positions,
)

#: Read constantly below — every figure here is a (currency, amount) bucket, never
#: a converted total. USD is its twin on the mixed-currency tests.
UZS, USD = Currency.UZS, Currency.USD

#: A round kurs so a so'm figure and its dollar twin both stay readable:
#: 40 mln so'm is exactly $4 000, and a 3 mln gap is exactly $300.
RATE = Decimal("10000")


def _agent(name="Bahrom aka"):
    return CustomsAgent.objects.create(name=name, phone="+998901112233")


def _shipment(kg="24000", logist=None):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal(kg), price=Decimal("1.00"),
        price_uzs=Decimal("12000"))
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        eta="2026-07-15", arrived="2026-07-16", logist=logist)
    ShipmentLine.objects.create(shipment=shipment, contract_line=line, kg=Decimal(kg))
    return shipment


def _send(agent, uzs="40000000", shipment=None, **kw):
    """What we send ahead of the truck, before anybody knows the real figure."""
    amount_uzs = Decimal(uzs)
    return CustomsPayment.objects.create(
        agent=agent, shipment=shipment, date=kw.pop("date", "2026-07-01"),
        currency="uzs", exchange_rate=RATE,
        amount=amount_uzs / RATE, amount_uzs=amount_uzs,
        method=kw.pop("method", "cash"), **kw)


def _cleared(shipment, agent, uzs="37000000", category="customs", **kw):
    """What clearing it actually cost, out of the money already sent."""
    amount_uzs = Decimal(uzs)
    return ShipmentExpense.objects.create(
        shipment=shipment, date=kw.pop("date", "2026-07-08"), category=category,
        currency="uzs", exchange_rate=RATE,
        amount=amount_uzs / RATE, amount_uzs=amount_uzs,
        method="cash", customs_agent=agent, **kw)


# The dollar twins of the two above. Every one of these rows also stores a so'm
# column — that is the whole trap: at RATE, $4 000 stores 40 000 000, exactly the
# figure a real so'm to'lov stores, so a total that adds the columns cannot tell
# the two apart and reports twice the money.

def _send_usd(agent, usd="4000", shipment=None, **kw):
    amount = Decimal(usd)
    return CustomsPayment.objects.create(
        agent=agent, shipment=shipment, date=kw.pop("date", "2026-07-01"),
        currency="usd", exchange_rate=RATE,
        amount=amount, amount_uzs=amount * RATE,
        method=kw.pop("method", "cash"), **kw)


def _cleared_usd(shipment, agent, usd="3700", category="customs", **kw):
    amount = Decimal(usd)
    return ShipmentExpense.objects.create(
        shipment=shipment, date=kw.pop("date", "2026-07-08"), category=category,
        currency="usd", exchange_rate=RATE,
        amount=amount, amount_uzs=amount * RATE,
        method="cash", customs_agent=agent, **kw)


class TestBalance:
    def test_balance_is_sent_minus_actually_spent(self, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        assert agent.received_by_currency() == [(UZS, Decimal("40000000.00"))]
        assert agent.spent_by_currency() == [(UZS, Decimal("37000000.00"))]
        assert agent.balance_by_currency() == [(UZS, Decimal("3000000.00"))]

    def test_the_leftover_carries_and_funds_the_next_load(self, db):
        """The whole reason the balance is a running float: 3 mln left over from a
        truck that came in under is 3 mln less to send for the next one."""
        agent = _agent()
        first, second = _shipment(), _shipment()
        _send(agent, "40000000", first)
        _cleared(first, agent, "37000000")
        _send(agent, "40000000", second)
        _cleared(second, agent, "40000000")
        assert agent.balance_by_currency() == [(UZS, Decimal("3000000.00"))]

    def test_balance_may_go_negative_when_they_cover_a_load_themselves(self, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "37000000", shipment)
        _cleared(shipment, agent, "40000000")
        assert agent.balance_by_currency() == [(UZS, Decimal("-3000000.00"))]
        assert agent.owed_by_currency() == [(UZS, Decimal("3000000.00"))]

    def test_a_foiz_we_carry_still_funds_them_in_full(self, db):
        """By default the bank's cut is ours: they are funded the whole figure and
        the kassa is out that plus the cut. Charged to THEM, less than the figure
        becomes theirs to spend."""
        agent = _agent()
        payment = _send(agent, "40000000", method="transfer",
                        fee_percent=Decimal("2"))
        assert agent.received_by_currency() == [(UZS, Decimal("40000000.00"))]
        assert payment.total_out_uzs == Decimal("40800000.00")

        payment.fee_bearer = "counterparty"
        payment.save(update_fields=["fee_bearer"])
        agent = CustomsAgent.objects.get(pk=agent.pk)
        assert agent.received_by_currency() == [(UZS, Decimal("39200000.00"))]
        assert payment.total_out_uzs == Decimal("40000000.00")

    def test_positions_keep_the_two_directions_apart(self, db):
        holder, ower = _agent("Pulimiz turgan"), _agent("Qarzdormiz")
        _send(holder, "40000000")
        _cleared(_shipment(), ower, "8000000")
        held, owed = customs_positions()
        assert held == [(UZS, Decimal("40000000.00"))]
        assert owed == [(UZS, Decimal("8000000.00"))]


class TestTheTwoHeapsAreNeverAdded:
    """The rule every total in this app follows, and the one this feature got wrong
    first: a figure is bucketed by the currency the money actually moved in, and the
    buckets are never summed. Adding each row's so'm column would take a $4 000
    to'lov's derived twin — 40 mln at that day's kurs — and pile it on real so'm."""

    def test_a_dollar_top_up_does_not_inflate_the_som_heap(self, db):
        agent = _agent()
        _send(agent, "40000000")                       # real so'm
        _send_usd(agent, "4000")                       # its twin is ANOTHER 40 mln
        assert agent.received_by_currency() == [
            (USD, Decimal("4000.00")), (UZS, Decimal("40000000.00"))]
        assert agent.balance_by_currency() == [
            (USD, Decimal("4000.00")), (UZS, Decimal("40000000.00"))]

    def test_a_dollar_clearing_comes_off_the_dollar_heap(self, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _send_usd(agent, "4000", shipment)
        _cleared(shipment, agent, "37000000")          # so'm clearing
        _cleared_usd(shipment, agent, "3700")          # dollar clearing
        assert agent.balance_by_currency() == [
            (USD, Decimal("300.00")), (UZS, Decimal("3000000.00"))]

    def test_a_load_can_be_over_in_one_currency_and_under_in_the_other(self, db):
        """Netting these would report a settled load twice over — and the operator
        has two different conversations to have about it."""
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")          # 3 mln left over
        _send_usd(agent, "1000", shipment)
        _cleared_usd(shipment, agent, "1500")          # $500 short
        assert shipment.customs_diff_by_currency() == [
            (USD, Decimal("-500.00")), (UZS, Decimal("3000000.00"))]
        assert shipment.customs_is_open is True

    def test_such_an_agent_is_both_a_holder_and_a_creditor(self, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        _send_usd(agent, "1000", shipment)
        _cleared_usd(shipment, agent, "1500")
        assert agent.held_by_currency() == [(UZS, Decimal("3000000.00"))]
        assert agent.owed_by_currency() == [(USD, Decimal("500.00"))]

    def test_the_kassa_positions_split_the_same_way(self, db):
        agent = _agent()
        _send(agent, "40000000")
        _send_usd(agent, "4000")
        held, owed = customs_positions()
        assert held == [(USD, Decimal("4000.00")), (UZS, Decimal("40000000.00"))]
        assert owed == []

    def test_both_lists_show_the_agent_under_either_filter(self, admin_client, db):
        agent = _agent("Ikki tomonlama")
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        _send_usd(agent, "1000", shipment)
        _cleared_usd(shipment, agent, "1500")
        names = lambda url: [x.name for x in admin_client.get(url).context["page"]]
        assert names("/customs/?state=holding") == ["Ikki tomonlama"]
        assert names("/customs/?state=owed") == ["Ikki tomonlama"]

    def test_the_loads_page_flags_it_both_ways(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        _send_usd(agent, "1000", shipment)
        _cleared_usd(shipment, agent, "1500")
        pks = lambda url: [r["shipment"].pk
                           for r in admin_client.get(url).context["page"]]
        assert pks("/customs/loads/?state=left") == [shipment.pk]
        assert pks("/customs/loads/?state=over") == [shipment.pk]

    def test_the_position_carries_both_lines(self, admin_client, db):
        from crm.models import customs_positions
        agent = _agent()
        _send(agent, "40000000")
        _send_usd(agent, "4000")
        held, _owed = customs_positions()
        assert dict(held) == {"usd": Decimal("4000.00"),
                              "uzs": Decimal("40000000.00")}


class TestPerLoadReconciliation:
    """"We send about 40 mln, but it can be 37 or 39 or 40 — they don't know."
    This is the answer, and it is read off rows that were being stored anyway."""

    def test_a_load_that_came_in_under_leaves_money_with_them(self, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        assert shipment.customs_sent_by_currency() == [(UZS, Decimal("40000000.00"))]
        assert shipment.customs_spent_by_currency() == [(UZS, Decimal("37000000.00"))]
        assert shipment.customs_diff_by_currency() == [(UZS, Decimal("3000000.00"))]
        assert shipment.customs_is_open is True

    def test_a_load_that_ran_over_shows_a_negative_gap(self, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "41500000")
        assert shipment.customs_diff_by_currency() == [(UZS, Decimal("-1500000.00"))]

    def test_a_load_that_cost_exactly_what_was_sent_is_settled(self, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "40000000")
        assert shipment.customs_diff_by_currency() == []
        assert shipment.customs_is_open is False

    def test_a_load_nobody_sent_customs_money_for_is_not_in_the_ledger(self, db):
        """Not "settled" — simply not part of this. Otherwise every yuk in the
        system would sit on the reconciliation screen showing three zeros."""
        shipment = _shipment()
        assert shipment.customs_is_open is False
        assert shipment.customs_sent_by_currency() == []

    def test_a_general_top_up_lands_on_no_load(self, db):
        """Money sent ahead of the week with no truck named yet still funds the
        float, but it cannot make some load look overfunded."""
        agent = _agent()
        shipment = _shipment()
        _send(agent, "50000000", shipment=None)
        assert agent.balance_by_currency() == [(UZS, Decimal("50000000.00"))]
        assert shipment.customs_sent_by_currency() == []
        assert shipment.customs_is_open is False

    def test_a_kassa_paid_bojxona_row_is_not_counted_against_the_float(self, db):
        """It is a real cost of the yuk, but it is not money out of what we sent
        them — counting it here would read as an overspend that never happened."""
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        ShipmentExpense.objects.create(
            shipment=shipment, date="2026-07-09", category="customs",
            currency="uzs", exchange_rate=RATE, amount=Decimal("500"),
            amount_uzs=Decimal("5000000"), method="cash")
        assert shipment.customs_spent_by_currency() == []
        assert shipment.customs_diff_by_currency() == [(UZS, Decimal("40000000.00"))]

    def test_the_foiz_is_off_the_sent_side_too(self, db):
        """A cut the bank took never reached them, so it was never money that could
        clear anything — the load was funded by that much less."""
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment, method="transfer",
              fee_percent=Decimal("2"), fee_bearer="counterparty")
        assert shipment.customs_sent_by_currency() == [(UZS, Decimal("39200000.00"))]


class TestKassaCountsTheMoneyOnce:
    """The trap: send 40 mln, they spend 37 mln at bojxona. The kassa must show
    40 mln out — not 77, and not 37."""

    def test_funding_the_agent_is_the_outflow(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        ctx = admin_client.get("/kassa/?davr=all").context
        assert ctx["net_out_uzs"] == Decimal("40000000.00")
        assert ctx["cash_total_uzs"] == Decimal("-40000000.00")

    def test_a_clearing_alone_moves_no_cash(self, db):
        agent = _agent()
        expense = _cleared(_shipment(), agent, "37000000")
        assert expense.from_kassa is False
        assert expense.total_out == Decimal("0")
        assert expense.total_out_uzs == Decimal("0")

    def test_the_clearing_is_absent_from_the_chiqim_ledger(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        rows = admin_client.get("/kassa/?davr=all").context["outflow_page"].paginator.object_list
        assert {r["kind"] for r in rows} == {"customs"}
        assert sum(r["amount_uzs"] for r in rows) == Decimal("40000000.00")

    def test_the_waterfall_still_closes_on_the_cash_total(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        ctx = admin_client.get("/kassa/?davr=all").context
        closing = ctx["waterfall"][-1]
        assert closing["running_uzs"] == ctx["cash_total_uzs"]
        labels = {b["label"]: b["amount_uzs"] for b in ctx["waterfall"]}
        assert labels["Bojxonaga oldindan"] == Decimal("-40000000.00")

    def test_per_method_totals_include_the_customs_payment(self, admin_client, db):
        _send(_agent(), "40000000", method="transfer")
        balances = admin_client.get("/kassa/?davr=all").context["balances"]
        assert balances["transfer"]["out_uzs"] == Decimal("40000000.00")
        assert balances["cash"]["out_uzs"] == Decimal("0")


class TestTheClearingPricesTheYuk:
    def test_it_lands_on_the_yuk_expenses_and_the_landed_cost(self, db):
        agent = _agent()
        shipment = _shipment(kg="24000")
        _cleared(shipment, agent, "24000000")          # $2 400 at the test kurs
        shipment.refresh_from_db()
        assert shipment.expenses_total == Decimal("2400.00")
        assert shipment.expense_per_kg == Decimal("0.10")
        lot = shipment.lines.first()
        assert lot.landed_cost_per_kg == Decimal("1.1000")

    def test_who_paid_does_not_change_what_it_cost(self, db):
        """Same clearing, one from the kassa and one from the float we already sent
        — the granula cost the same either way."""
        from_kassa = _shipment()
        ShipmentExpense.objects.create(
            shipment=from_kassa, date="2026-07-08", category="customs",
            currency="uzs", exchange_rate=RATE, amount=Decimal("3700"),
            amount_uzs=Decimal("37000000"), method="cash")
        from_agent = _shipment()
        _cleared(from_agent, _agent(), "37000000")
        assert from_kassa.expenses_total == from_agent.expenses_total
        assert (from_kassa.lines.first().landed_cost_per_kg
                == from_agent.lines.first().landed_cost_per_kg)


class TestOnePaymentHasOnePayer:
    def test_a_row_cannot_be_paid_by_both(self, db):
        from django.core.exceptions import ValidationError
        logist = Logist.objects.create(name="Sardor")
        expense = ShipmentExpense(
            shipment=_shipment(), date="2026-07-08", category="customs",
            amount=Decimal("3700"), amount_uzs=Decimal("37000000"), method="cash",
            logist=logist, customs_agent=_agent())
        with pytest.raises(ValidationError):
            expense.clean()

    def test_the_database_refuses_it_too(self, db):
        """clean() is only reached through a form; the importer and the seeders
        build rows directly, and this must not be possible from there either."""
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            ShipmentExpense.objects.create(
                shipment=_shipment(), date="2026-07-08", category="customs",
                amount=Decimal("3700"), amount_uzs=Decimal("37000000"),
                method="cash", logist=Logist.objects.create(name="Sardor"),
                customs_agent=_agent())

    def test_the_form_says_so_on_the_field(self, db):
        from crm.forms import ShipmentExpenseForm
        shipment = _shipment()
        form = ShipmentExpenseForm(data={
            "shipment": shipment.pk, "date": "2026-07-08", "category": "customs",
            "logist": Logist.objects.create(name="Sardor").pk,
            "customs_agent": _agent().pk, "currency": "uzs", "amount": "37000000",
            "exchange_rate": str(RATE), "method": "cash", "fee_percent": "0",
            "note": ""})
        assert not form.is_valid()
        assert "customs_agent" in form.errors

    def test_the_expense_form_defaults_to_the_agent_funded_for_that_load(self, db):
        from crm.forms import ShipmentExpenseForm
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        form = ShipmentExpenseForm(initial={"shipment": shipment})
        assert form.initial.get("customs_agent") == agent.pk

    def test_a_load_with_a_logist_never_opens_holding_both(self, db):
        """Pre-filling the pair would open the form already carrying the one
        combination the same form refuses."""
        from crm.forms import ShipmentExpenseForm
        logist = Logist.objects.create(name="Sardor")
        shipment = _shipment(logist=logist)
        _send(_agent(), "40000000", shipment)
        form = ShipmentExpenseForm(initial={"shipment": shipment})
        assert form.initial.get("logist") == logist.pk
        assert form.initial.get("customs_agent") is None


class TestTheGridCanSayWhoPaid:
    """The grid is how xarajatlar are actually entered. Before it could name a
    payer, a bojxona typed here always became a kassa row — so a load whose clearing
    had already been funded had that same money leaving twice."""

    def _grid(self, shipment, **over):
        """The whole grid posted back. Payer boxes exist on Bojxona and Transport
        only, so a test that wants one passes payer_customs=/payer_transport=."""
        body = {"shipment": shipment.pk, "date": "2026-07-08", "currency": "uzs",
                "method": "cash", "exchange_rate": str(RATE), "fee_percent": "0",
                "note": ""}
        for category, _label in ShipmentExpense.Category.choices:
            body[f"amount_{category}"] = ""
            body[f"currency_{category}"] = ""
            body[f"method_{category}"] = ""
            body[f"fee_{category}"] = ""
            body[f"row_{category}"] = ""
        body["payer_customs"] = ""
        body["payer_transport"] = ""
        body.update(over)
        return body

    def test_a_clearing_entered_here_can_come_out_of_the_float(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        resp = admin_client.post("/expenses/new/", self._grid(
            shipment, payer_customs=f"customs:{agent.pk}", amount_customs="37000000"))
        assert resp.status_code == 302
        expense = ShipmentExpense.objects.get()
        assert expense.customs_agent_id == agent.pk
        assert expense.from_kassa is False
        assert shipment.customs_spent_by_currency() == [(UZS, Decimal("37000000.00"))]
        assert shipment.customs_diff_by_currency() == [(UZS, Decimal("3000000.00"))]

    def test_the_kassa_does_not_pay_for_it_a_second_time(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        admin_client.post("/expenses/new/", self._grid(
            shipment, payer_customs=f"customs:{agent.pk}", amount_customs="37000000"))
        ctx = admin_client.get("/kassa/?davr=all").context
        assert ctx["cash_total_uzs"] == Decimal("-40000000.00")

    def test_left_alone_it_still_bills_the_kassa(self, admin_client, db):
        """The default is the behaviour this form has always had, so a grid nobody
        touched differently keeps working exactly as before."""
        shipment = _shipment()
        admin_client.post("/expenses/new/",
                          self._grid(shipment, amount_customs="37000000"))
        expense = ShipmentExpense.objects.get()
        assert expense.from_kassa is True
        assert expense.customs_agent_id is None

    def test_it_reaches_bojxona_and_transport_only(self, admin_client, db):
        """One picker over a grid of seven. A gruzchi and a sertifikat come out of
        the kassa on the day, so choosing the bojxonachi for a 37 mln bojxona must
        not put the 65 beside it on his balance — money he never handled, and a
        qoldiq nobody could explain afterwards."""
        agent = _agent()
        logist = Logist.objects.create(name="Sardor")
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        admin_client.post("/expenses/new/", self._grid(
            shipment, payer_customs=f"customs:{agent.pk}",
            payer_transport=f"logist:{logist.pk}",
            amount_customs="37000000", amount_transport="12000000",
            amount_loader="780000", amount_cert="500000",
            amount_declarant="2100000"))
        paid_by = {e.category: e.customs_agent_id
                   for e in ShipmentExpense.objects.all()}
        assert paid_by == {"customs": agent.pk, "transport": None,
                           "loader": None, "cert": None, "declarant": None}
        assert ShipmentExpense.objects.get(category="transport").logist_id == logist.pk

    def test_the_bojxonachi_and_the_logist_can_be_on_one_submission(self, admin_client, db):
        """The ordinary case on a load that has both, and the reason the picker is
        per box rather than one control above the grid."""
        agent = _agent()
        logist = Logist.objects.create(name="Sardor")
        shipment = _shipment(logist=logist)
        _send(agent, "40000000", shipment)
        admin_client.post("/expenses/new/", self._grid(
            shipment, payer_customs=f"customs:{agent.pk}",
            payer_transport=f"logist:{logist.pk}",
            amount_customs="37000000", amount_transport="12000000"))
        bojxona = ShipmentExpense.objects.get(category="customs")
        transport = ShipmentExpense.objects.get(category="transport")
        assert (bojxona.customs_agent_id, bojxona.logist_id) == (agent.pk, None)
        assert (transport.logist_id, transport.customs_agent_id) == (logist.pk, None)

    def test_changing_a_box_moves_that_row_between_accounts(self, admin_client, db):
        """A per-turkum box opens showing that row's own payer, so changing it is a
        deliberate, visible edit — unlike one shared answer being applied to rows it
        was never asked about, which is why the shared version never rewrote."""
        agent = _agent()
        shipment = _shipment()
        existing = _cleared(shipment, agent, "37000000")
        admin_client.post("/expenses/new/", self._grid(
            shipment, payer_customs="", amount_customs="37000000",
            **{"row_customs": existing.pk}))
        existing.refresh_from_db()
        assert existing.customs_agent_id is None
        assert existing.from_kassa is True

    def test_the_untouched_turkumlar_still_leave_the_kassa(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        admin_client.post("/expenses/new/", self._grid(
            shipment, payer_customs=f"customs:{agent.pk}",
            amount_customs="37000000", amount_loader="780000"))
        loader = ShipmentExpense.objects.get(category="loader")
        assert loader.from_kassa is True
        # ...and only the bojxona came off the float, so the load reads 3 mln left
        # over rather than 3 mln less the gruzchi.
        assert shipment.customs_spent_by_currency() == [(UZS, Decimal("37000000.00"))]
        assert shipment.customs_diff_by_currency() == [(UZS, Decimal("3000000.00"))]

    def test_only_those_two_boxes_carry_a_picker(self, admin_client, db):
        _agent()
        shipment = _shipment()
        html = admin_client.get(
            f"/expenses/new/?shipment={shipment.pk}",
            headers={"X-Requested-With": "XMLHttpRequest"}).content.decode()
        # Split at each box's own marker and ask which segment holds a picker — a
        # regex spanning from one data-cat to a closing tag runs straight through
        # the next box and reports it as marked too.
        boxes = re.split(r'data-cat="([a-z]+)"', html)[1:]
        marked = {category for category, body in zip(boxes[::2], boxes[1::2])
                  if "xcell-payer" in body}
        assert marked == {"customs", "transport"}

    def test_each_box_offers_only_the_people_who_pay_that_turkum(self, db):
        """A bojxonachi clears loads and a logist pays drivers — the two roles do not
        overlap, so listing both in both boxes turns a two-item pick into a scan of
        every outside party in the books."""
        from crm.forms import ExpenseGridForm
        agent = _agent("Bahrom aka")
        logist = Logist.objects.create(name="Sardor aka")
        form = ExpenseGridForm(initial={})
        assert form.fields["payer_customs"].choices == [
            ("", "Kassadan to'landi"), (f"customs:{agent.pk}", "Bahrom aka")]
        assert form.fields["payer_transport"].choices == [
            ("", "Kassadan to'landi"), (f"logist:{logist.pk}", "Sardor aka")]

    def test_both_boxes_rest_on_the_kassa(self, db):
        from crm.forms import ExpenseGridForm
        _agent()
        Logist.objects.create(name="Sardor")
        form = ExpenseGridForm(initial={})
        assert form.fields["payer_customs"].choices[0] == ("", "Kassadan to'landi")
        assert form.fields["payer_transport"].choices[0] == ("", "Kassadan to'landi")
        assert form.fields["payer_customs"].initial == ""
        assert form.fields["payer_transport"].initial == ""

    def test_a_logist_who_once_paid_a_bojxona_stays_pickable(self, admin_client, db):
        """ShipmentExpense allows it and the single xarajat form still offers it.
        Dropped from this box's list, such a row would open showing nobody and be
        refused on submit — so correcting the figure beside it would mean first
        reassigning money the operator never touched."""
        from crm.forms import ExpenseGridForm
        logist = Logist.objects.create(name="Sardor")
        shipment = _shipment()
        row = ShipmentExpense.objects.create(
            shipment=shipment, date="2026-07-08", category="customs",
            currency="uzs", exchange_rate=RATE, amount=Decimal("3700"),
            amount_uzs=Decimal("37000000"), method="cash", logist=logist)
        form = ExpenseGridForm(shipment=shipment,
                               initial={"shipment": shipment.pk})
        assert form.initial["payer_customs"] == f"logist:{logist.pk}"
        assert (f"logist:{logist.pk}", "Sardor (avvalgi)") \
            in form.fields["payer_customs"].choices

        # ...and correcting the figure keeps it where it was.
        admin_client.post("/expenses/new/", self._grid(
            shipment, payer_customs=f"logist:{logist.pk}",
            amount_customs="39000000", **{"row_customs": row.pk}))
        row.refresh_from_db()
        assert row.amount_uzs == Decimal("39000000.00")
        assert row.logist_id == logist.pk

    def test_the_transport_box_charges_the_logist(self, admin_client, db):
        logist = Logist.objects.create(name="Sardor")
        shipment = _shipment()
        admin_client.post("/expenses/new/", self._grid(
            shipment, payer_transport=f"logist:{logist.pk}",
            amount_transport="12000000"))
        expense = ShipmentExpense.objects.get()
        assert expense.logist_id == logist.pk
        assert expense.customs_agent_id is None

    def test_correcting_a_figure_leaves_the_payer_where_it_was(self, admin_client, db):
        """The box comes back as it was drawn, so only the figure moves. Correcting
        a 37 mln bojxona to 39 must not quietly hand it back to the kassa."""
        agent = _agent()
        shipment = _shipment()
        existing = _cleared(shipment, agent, "37000000")
        admin_client.post("/expenses/new/", self._grid(
            shipment, payer_customs=f"customs:{agent.pk}",
            amount_customs="39000000", **{"row_customs": existing.pk}))
        existing.refresh_from_db()
        assert existing.amount_uzs == Decimal("39000000.00")
        assert existing.customs_agent_id == agent.pk

    def test_a_submit_that_touched_nothing_writes_nothing(self, admin_client, db):
        """The payer joins the as-drawn comparison, so a modal opened and saved
        without an edit does not count as a rewrite."""
        from crm.forms import ExpenseGridForm
        agent = _agent()
        shipment = _shipment()
        existing = _cleared(shipment, agent, "37000000")
        form = ExpenseGridForm(self._grid(
            shipment, payer_customs=f"customs:{agent.pk}",
            amount_customs="37000000", **{"row_customs": existing.pk}),
            shipment=shipment)
        assert form.is_valid(), form.errors
        created, updated, deleted = form.save(None)
        assert (created, updated, deleted) == ([], [], [])

    def test_the_bojxona_box_opens_on_the_agent_funded_for_that_load(self, db):
        """Entering that figure as a kassa row is the double-count the picker exists
        to stop, so the box opens already naming where the money went."""
        from crm.forms import ExpenseGridForm
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        form = ExpenseGridForm(shipment=shipment,
                               initial={"shipment": shipment.pk})
        assert form.initial["payer_customs"] == f"customs:{agent.pk}"
        assert form.initial.get("payer_transport", "") == ""

    def test_a_recorded_row_opens_showing_its_own_payer(self, db):
        """Unlike valyuta and usul, which fall back to a shared picker, this box has
        no shared answer to inherit — so it shows exactly what the row says."""
        from crm.forms import ExpenseGridForm
        agent = _agent()
        logist = Logist.objects.create(name="Sardor")
        shipment = _shipment()
        _cleared(shipment, agent, "37000000")
        ShipmentExpense.objects.create(
            shipment=shipment, date="2026-07-08", category="transport",
            currency="uzs", exchange_rate=RATE, amount=Decimal("1200"),
            amount_uzs=Decimal("12000000"), method="cash", logist=logist)
        form = ExpenseGridForm(shipment=shipment,
                               initial={"shipment": shipment.pk})
        assert form.initial["payer_customs"] == f"customs:{agent.pk}"
        assert form.initial["payer_transport"] == f"logist:{logist.pk}"

    def test_a_plain_load_opens_on_the_kassa(self, db):
        from crm.forms import ExpenseGridForm
        shipment = _shipment()
        form = ExpenseGridForm(shipment=shipment,
                               initial={"shipment": shipment.pk})
        assert form.initial["payer_customs"] == ""

    def test_a_logist_run_load_still_opens_on_the_kassa(self, db):
        """Every load in the books predates this box and the grid wrote kassa rows
        on all of them. Defaulting to the logist would start moving a gruzchi onto
        somebody's balance on loads whose entry never changed."""
        from crm.forms import ExpenseGridForm
        shipment = _shipment(logist=Logist.objects.create(name="Sardor"))
        form = ExpenseGridForm(shipment=shipment,
                               initial={"shipment": shipment.pk})
        assert form.initial.get("payer_transport", "") == ""


class TestPaymentForm:
    def _body(self, agent, **over):
        body = {"agent": agent.pk, "shipment": "", "date": "2026-07-01",
                "currency": "uzs", "amount": "40000000", "exchange_rate": "",
                "method": "cash", "fee_percent": "0", "note": ""}
        body.update(over)
        return body

    def test_neither_currency_needs_a_kurs(self, db):
        """The rule the hamkor and mijoz forms follow — ask only when the money
        crosses — taken to its end. A bojxonachi holds two heaps, so a so'm to'lov
        lands in the so'm heap and a dollar one in the dollar heap. Nothing crosses,
        so nothing is demanded and the row inherits a rate for its stored twin."""
        from crm.forms import CustomsPaymentForm
        for currency, amount in [("uzs", "40000000"), ("usd", "4000")]:
            form = CustomsPaymentForm(data=self._body(
                _agent(), currency=currency, amount=amount))
            assert form.is_valid(), (currency, form.errors)
            payment = form.save(commit=False)
            assert payment.crosses_currency is False
            assert payment.exchange_rate > 0        # inherited, never demanded

    def test_the_kurs_box_is_hidden_in_both_directions(self, admin_client, db):
        """Marked with an EMPTY settlement currency, which base.html reads as "this
        money crosses nothing, ever". The hamkor form uses the same attribute to
        hide the box when a kelishuv is paid in its own currency."""
        html = admin_client.get(
            "/customs-payments/new/",
            headers={"X-Requested-With": "XMLHttpRequest"}).content.decode()
        assert 'data-settled-against=""' in html

    def test_a_rate_the_operator_does_supply_still_stands(self, db):
        """Hidden rather than forbidden, exactly as on a hamkor to'lov."""
        from crm.forms import CustomsPaymentForm
        form = CustomsPaymentForm(data=self._body(
            _agent(), currency="usd", amount="4000", exchange_rate="12800"))
        assert form.is_valid(), form.errors
        payment = form.save(commit=False)
        assert payment.exchange_rate == Decimal("12800")
        assert payment.amount == Decimal("4000.00")
        assert payment.amount_uzs == Decimal("51200000.00")   # the stored twin

    def test_a_new_form_opens_in_som(self, db):
        """The column's default is dollars, which is right everywhere else in the
        app and wrong here — bojxona is paid in so'm."""
        from crm.forms import CustomsPaymentForm
        assert CustomsPaymentForm().initial.get("currency") == "uzs"

    def test_the_payment_can_name_the_load_it_is_for(self, db):
        from crm.forms import CustomsPaymentForm
        agent, shipment = _agent(), _shipment()
        form = CustomsPaymentForm(data=self._body(agent, shipment=shipment.pk))
        assert form.is_valid(), form.errors
        assert form.save(commit=False).shipment_id == shipment.pk


class TestCustomsScreens:
    def test_list_shows_the_balance_and_both_position_figures(self, admin_client, db):
        agent = _agent("Bahrom aka")
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        resp = admin_client.get("/customs/")
        assert resp.status_code == 200
        assert resp.context["held"] == [(UZS, Decimal("3000000.00"))]
        assert resp.context["owed"] == []
        html = resp.content.decode()
        assert "Bahrom aka" in html and "Bojxonachida turgan pulimiz" in html

    def test_the_list_headlines_what_is_still_unaccounted_for(self, admin_client, db):
        agent = _agent()
        under, over = _shipment(), _shipment()
        _send(agent, "40000000", under)
        _cleared(under, agent, "37000000")             # +3 mln
        _send(agent, "40000000", over)
        _cleared(over, agent, "41000000")              # −1 mln
        ctx = admin_client.get("/customs/").context
        assert ctx["open_diff"] == [(UZS, Decimal("2000000.00"))]
        # Two loads, not one: a net that nearly cancels is not "nothing to do".
        assert ctx["open_loads"] == 2

    def test_state_filter_splits_holders_from_creditors(self, admin_client, db):
        holder, ower = _agent("Ushlab turgan"), _agent("Qarzdor")
        _send(holder, "40000000")
        _cleared(_shipment(), ower, "8000000")
        names = lambda url: [x.name for x in admin_client.get(url).context["page"]]
        assert names("/customs/?state=holding") == ["Ushlab turgan"]
        assert names("/customs/?state=owed") == ["Qarzdor"]

    def test_loads_page_lists_sent_spent_and_the_gap(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        ctx = admin_client.get("/customs/loads/").context
        row = list(ctx["page"])[0]
        assert row["sent"] == [(UZS, Decimal("40000000.00"))]
        assert row["spent"] == [(UZS, Decimal("37000000.00"))]
        assert row["diff"] == [(UZS, Decimal("3000000.00"))]
        assert row["agents"] == agent.name
        assert ctx["totals"]["diff"] == [(UZS, Decimal("3000000.00"))]

    def test_loads_page_leaves_out_loads_no_money_touched(self, admin_client, db):
        agent = _agent()
        funded = _shipment()
        _shipment()                                     # never sent anything for
        _send(agent, "40000000", funded)
        rows = list(admin_client.get("/customs/loads/").context["page"])
        assert [r["shipment"].pk for r in rows] == [funded.pk]

    def test_loads_page_filters_by_direction(self, admin_client, db):
        agent = _agent()
        under, over = _shipment(), _shipment()
        _send(agent, "40000000", under)
        _cleared(under, agent, "37000000")
        _send(agent, "40000000", over)
        _cleared(over, agent, "41000000")
        pks = lambda url: [r["shipment"].pk
                           for r in admin_client.get(url).context["page"]]
        assert pks("/customs/loads/?state=left") == [under.pk]
        assert pks("/customs/loads/?state=over") == [over.pk]

    def test_the_two_daftar_are_kept_apart_each_newest_first(self, admin_client, db):
        """Yuborilgan pul and Sarflangan pul are two lists, not two columns of one.

        It matters more here than on a logist's page, and for the reason the feature
        exists: what we SEND for a truck is an estimate typed before anybody knows the
        price, and what clearing COSTS is only known afterwards. Those are the two
        figures being compared, and interleaved by date they were compared across a
        column that was blank on every other row."""
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment, date="2026-07-01")
        _send(agent, "5000000", shipment=None, date="2026-07-11")
        _cleared(shipment, agent, "37000000", date="2026-07-08")

        ctx = admin_client.get(f"/customs/{agent.pk}/").context
        sent, spent = list(ctx["sent_page"]), list(ctx["spent_page"])
        assert [r["amount_uzs"] for r in sent] == [
            Decimal("5000000.00"), Decimal("40000000.00")]
        assert [r["amount_uzs"] for r in spent] == [Decimal("37000000.00")]
        # A top-up against no yuk says so; one sent for a truck names it.
        assert "Umumiy to'ldirish" in sent[0]["title"]
        assert f"Yuk #{shipment.pk} uchun" in sent[1]["title"]

    def test_neither_daftar_carries_the_other_side_s_rows(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        payment = _send(agent, "40000000", shipment, date="2026-07-01")
        expense = _cleared(shipment, agent, "37000000", date="2026-07-08")

        ctx = admin_client.get(f"/customs/{agent.pk}/").context
        assert [r["obj"].pk for r in ctx["sent_page"]] == [payment.pk]
        assert [r["obj"].pk for r in ctx["spent_page"]] == [expense.pk]

    def test_each_daftar_totals_its_own_side_in_the_head(self, admin_client, db):
        """The head of each daftar repeats the tile above it, so the two cannot
        drift — and the Qoldiq tile is the difference between them."""
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment, date="2026-07-01")
        _cleared(shipment, agent, "37000000", date="2026-07-08")

        resp = admin_client.get(f"/customs/{agent.pk}/")
        assert dict(resp.context["agent"].received_by_currency())[UZS] \
            == Decimal("40000000.00")
        assert dict(resp.context["agent"].spent_by_currency())[UZS] \
            == Decimal("37000000.00")
        assert dict(resp.context["agent"].balance_by_currency())[UZS] \
            == Decimal("3000000.00")
        html = resp.content.decode()
        assert "Yuborilgan pul" in html and "Sarflangan pul" in html

    def test_the_detail_page_lists_the_loads_the_money_went_to(self, admin_client, db):
        """The hisob varaqasi says what moved; this says on WHAT — and a load is
        recognised by its kelishuv and marka, not by "Yuk #3"."""
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        resp = admin_client.get(f"/customs/{agent.pk}/")
        row = resp.context["loads"][0]
        assert row["shipment"].pk == shipment.pk
        assert row["sent"] == [(UZS, Decimal("40000000.00"))]
        assert row["paid"] == [(UZS, Decimal("37000000.00"))]
        assert row["diff"] == [(UZS, Decimal("3000000.00"))]
        html = resp.content.decode()
        assert shipment.contract.code in html
        assert "LLDPE" in html and "Qaysi yuklarga" in html

    def test_a_general_top_up_puts_no_load_on_that_table(self, admin_client, db):
        agent = _agent()
        _send(agent, "50000000", shipment=None)
        assert admin_client.get(f"/customs/{agent.pk}/").context["loads"] == []

    def test_the_logist_page_lists_theirs_without_the_sent_columns(self, admin_client, db):
        """A LogistPayment names no yuk — their funding is a lump against no load —
        so there is nothing to set what they paid against."""
        logist = Logist.objects.create(name="Sardor")
        shipment = _shipment(logist=logist)
        ShipmentExpense.objects.create(
            shipment=shipment, date="2026-07-08", category="transport",
            currency="uzs", exchange_rate=RATE, amount=Decimal("960"),
            amount_uzs=Decimal("12000000"), method="cash", logist=logist)
        resp = admin_client.get(f"/logists/{logist.pk}/")
        row = resp.context["loads"][0]
        assert row["shipment"].pk == shipment.pk
        assert row["paid"] == [(UZS, Decimal("12000000.00"))]
        assert row["sent"] == []
        html = resp.content.decode()
        assert shipment.contract.code in html
        # Aimed at the two columns themselves, not at the word: the page has a
        # Yuborilgan pul daftar of its own now, and a bare `"Yuborilgan" not in html`
        # would pass or fail on that instead of on the table it is about.
        assert 'title="Shu yuk uchun oldindan yuborilgan"' not in html
        assert ">Farq</th>" not in html

    def test_the_yuk_page_shows_that_load_own_reconciliation(self, admin_client, db):
        agent = _agent()
        shipment = _shipment()
        _send(agent, "40000000", shipment)
        _cleared(shipment, agent, "37000000")
        html = admin_client.get(f"/shipments/{shipment.pk}/").content.decode()
        assert "Bojxonaga oldindan yuborilgan" in html
        assert agent.name in html                       # the "Kim to'ladi" column

    def test_the_money_sent_ahead_shows_on_the_bojxona_screen(self, admin_client, db):
        _send(_agent(), "40000000")
        page = admin_client.get("/customs/")
        assert dict(page.context["held"])["uzs"] == Decimal("40000000.00")

    def test_a_bojxonachi_we_owe_is_only_listed_when_owed(self, admin_client, db):
        from crm.models import customs_positions
        _send(_agent(), "40000000")
        assert customs_positions()[1] == []
        assert admin_client.get("/customs/?state=owed").context["page"].object_list == []

        _cleared(_shipment(), _agent("Qarzdor"), "8000000")
        # Positive, like every other qarzimiz figure — see the logist's twin test.
        assert dict(customs_positions()[1])["uzs"] == Decimal("8000000.00")
        owed = admin_client.get("/customs/?state=owed").context["page"]
        assert [a.name for a in owed] == ["Qarzdor"]

    def test_the_modals_render_every_field_they_ask_for(self, admin_client, db):
        """A form whose template blows up 500s only when somebody opens it, which is
        long after the page that links to it was called done."""
        ajax = {"headers": {"X-Requested-With": "XMLHttpRequest"}}
        html = admin_client.get("/customs-payments/new/", **ajax).content.decode()
        # The shared header, then the fields of the first to'lov row: this modal takes
        # several ways one payment moved, so the money boxes are formset-prefixed.
        for name in ("agent", "shipment", "date",
                     "form-0-currency", "form-0-amount", "form-0-method"):
            assert f'name="{name}"' in html, name
        assert admin_client.get("/customs/new/", **ajax).status_code == 200

    def test_the_grid_modal_offers_everyone_who_could_have_paid(self, admin_client, db):
        agent = _agent("Bahrom aka")
        logist = Logist.objects.create(name="Sardor aka")
        shipment = _shipment()
        html = admin_client.get(
            f"/expenses/new/?shipment={shipment.pk}",
            headers={"X-Requested-With": "XMLHttpRequest"}).content.decode()
        assert 'name="payer_customs"' in html and 'name="payer_transport"' in html
        assert f'value="customs:{agent.pk}"' in html
        assert f'value="logist:{logist.pk}"' in html

    def test_the_single_expense_form_asks_who_paid_on_both_sides(self, admin_client, db):
        expense = _cleared(_shipment(), _agent(), "37000000")
        html = admin_client.get(
            f"/expenses/{expense.pk}/edit/",
            headers={"X-Requested-With": "XMLHttpRequest"}).content.decode()
        assert 'name="logist"' in html and 'name="customs_agent"' in html

    def test_an_agent_with_money_behind_them_cannot_be_deleted(self, admin_client, db):
        agent = _agent()
        _send(agent, "40000000")
        admin_client.post(f"/customs/{agent.pk}/delete/", {})
        assert CustomsAgent.objects.filter(pk=agent.pk).exists()

    def test_a_load_we_sent_customs_money_for_cannot_be_deleted(self, admin_client, db):
        """Money that really left the kassa for this truck must not vanish with it."""
        shipment = _shipment()
        _send(_agent(), "40000000", shipment)
        admin_client.post(f"/shipments/{shipment.pk}/delete/", {})
        assert Shipment.objects.filter(pk=shipment.pk).exists()

    def test_translator_forbidden(self, translator_client, db):
        assert translator_client.get("/customs/").status_code == 403
        assert translator_client.get("/customs/new/").status_code == 403
        assert translator_client.get("/customs/loads/").status_code == 403
        assert translator_client.get("/customs-payments/new/").status_code == 403
