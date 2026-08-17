"""Logist: the outside party who arranges transport and pays the drivers.

The rule the whole feature turns on is that one payment must not be spent twice.
Money leaves the kassa when we fund a logist; what they later hand a driver prices
that yuk but must NOT appear in the kassa again.
"""

from datetime import date as _date
from decimal import Decimal

from crm.models import (
    Contract, ContractLine, CustomerPayment, Customer, Logist, LogistPayment, Partner,
    Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus, logist_positions,
)


def _logist(name="Sardor"):
    return Logist.objects.create(name=name, phone="+998901112233")


def _shipment(logist=None, kg="24000"):
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


def _send(logist, amount="10000", method="cash", **kw):
    return LogistPayment.objects.create(
        logist=logist, date=kw.pop("date", "2026-07-01"), amount=Decimal(amount),
        amount_uzs=Decimal(amount) * 12000, method=method, **kw)


def _advance(shipment, logist, amount="500", category="transport", **kw):
    return ShipmentExpense.objects.create(
        shipment=shipment, date=kw.pop("date", "2026-07-03"), category=category,
        amount=Decimal(amount), amount_uzs=Decimal(amount) * 12000,
        method="cash", logist=logist, **kw)


class TestBalance:
    def test_balance_is_sent_minus_handed_to_drivers(self, db):
        logist = _logist()
        shipment = _shipment(logist)
        _send(logist, "10000")
        _advance(shipment, logist, "500")
        assert logist.received_total == Decimal("10000.00")
        assert logist.paid_total == Decimal("500.00")
        assert logist.balance == Decimal("9500.00")
        assert logist.balance_uzs == Decimal("114000000.00")

    def test_balance_may_go_negative_when_they_front_their_own_cash(self, db):
        logist = _logist()
        shipment = _shipment(logist)
        _send(logist, "2000")
        _advance(shipment, logist, "3200")
        assert logist.balance == Decimal("-1200.00")

    def test_a_foiz_we_carry_still_funds_them_in_full(self, db):
        """By default the bank's cut is ours: the logist is funded the whole figure
        and the kassa is out that plus the cut. Only when the cut is charged to THEM
        does less than the figure become theirs to spend."""
        logist = _logist()
        payment = _send(logist, "1000", method="transfer", fee_percent=Decimal("2"))
        assert logist.received_total == Decimal("1000.00")
        assert logist.balance == Decimal("1000.00")
        assert payment.total_out == Decimal("1020.00")

        payment.fee_bearer = "counterparty"
        payment.save(update_fields=["fee_bearer"])
        logist = Logist.objects.get(pk=logist.pk)
        assert logist.received_total == Decimal("980.00")
        assert payment.total_out == Decimal("1000.00")

    def test_positions_keep_the_two_directions_apart(self, db):
        """Per currency now, and both sides positive: what a logist still holds and
        what one fronted himself are two facts, and so are a dollar heap and a so'm
        heap."""
        from crm.models import Currency
        holder, ower = _logist("Pulimiz turgan"), _logist("Qarzdormiz")
        _send(holder, "5000")
        _advance(_shipment(ower), ower, "800")
        held, owed = logist_positions()
        assert held == [(Currency.USD, Decimal("5000.00"))]
        assert owed == [(Currency.USD, Decimal("800.00"))]


class TestKassaCountsTheMoneyOnce:
    """The trap: fund a logist $10 000, they advance a driver $500. The kassa must
    show $10 000 out — not $10 500, and not $500."""

    def test_funding_the_logist_is_the_outflow(self, admin_client, db):
        logist = _logist()
        shipment = _shipment(logist)
        _send(logist, "10000")
        _advance(shipment, logist, "500")
        ctx = admin_client.get("/kassa/?davr=all").context
        assert ctx["net_out"] == Decimal("10000.00")
        assert ctx["cash_total"] == Decimal("-10000.00")

    def test_a_driver_advance_alone_moves_no_cash(self, db):
        logist = _logist()
        advance = _advance(_shipment(logist), logist, "500")
        assert advance.from_kassa is False
        assert advance.total_out == Decimal("0")
        assert advance.total_out_uzs == Decimal("0")

    def test_an_expense_with_no_logist_still_leaves_the_kassa(self, db):
        expense = ShipmentExpense.objects.create(
            shipment=_shipment(), date="2026-07-03", category="customs",
            amount=Decimal("3200"), amount_uzs=Decimal("38400000"), method="cash")
        assert expense.from_kassa is True
        assert expense.total_out == Decimal("3200.00")

    def test_driver_advance_is_absent_from_the_chiqim_ledger(self, admin_client, db):
        logist = _logist()
        shipment = _shipment(logist)
        _send(logist, "10000")
        _advance(shipment, logist, "500")
        rows = admin_client.get("/kassa/?davr=all").context["outflow_page"].paginator.object_list
        kinds = {r["kind"] for r in rows}
        assert "logist" in kinds
        assert sum(r["amount"] for r in rows) == Decimal("10000.00")

    def test_the_waterfall_still_closes_on_the_cash_total(self, admin_client, db):
        logist = _logist()
        shipment = _shipment(logist)
        customer = Customer.objects.create(name="Mijoz", phone="1", address="T")
        CustomerPayment.objects.create(customer=customer, date="2026-07-02",
                                       amount=Decimal("4000"),
                                       amount_uzs=Decimal("48000000"), method="cash")
        _send(logist, "10000")
        _advance(shipment, logist, "500")
        ctx = admin_client.get("/kassa/?davr=all").context
        closing = ctx["waterfall"][-1]
        assert closing["running"] == ctx["cash_total"] == Decimal("-6000.00")
        assert closing["running_uzs"] == ctx["cash_total_uzs"]
        labels = {b["label"]: b["amount"] for b in ctx["waterfall"]}
        assert labels["Logistlarga"] == Decimal("-10000.00")

    def test_per_method_totals_include_the_logist_payment(self, admin_client, db):
        logist = _logist()
        _send(logist, "10000", method="transfer")
        balances = admin_client.get("/kassa/?davr=all").context["balances"]
        assert balances["transfer"]["out"] == Decimal("10000.00")
        assert balances["cash"]["out"] == Decimal("0")


class TestDriverAdvancePricesTheYuk:
    def test_it_lands_on_the_yuk_expenses_and_the_landed_cost(self, db):
        logist = _logist()
        shipment = _shipment(logist, kg="24000")
        _advance(shipment, logist, "480")
        shipment.refresh_from_db()
        assert shipment.expenses_total == Decimal("480.00")
        assert shipment.expense_per_kg == Decimal("0.02")
        lot = shipment.lines.first()
        # unit price 1.00 + 480/24000 freight share
        assert lot.landed_cost_per_kg == Decimal("1.0200")

    def test_who_paid_does_not_change_what_it_cost(self, db):
        """Same expense, one from the kassa and one from a logist — the granula
        cost the same either way."""
        from_kassa = _shipment()
        ShipmentExpense.objects.create(
            shipment=from_kassa, date="2026-07-03", category="transport",
            amount=Decimal("480"), amount_uzs=Decimal("5760000"), method="cash")
        logist = _logist()
        from_logist = _shipment(logist)
        _advance(from_logist, logist, "480")
        assert from_kassa.expenses_total == from_logist.expenses_total
        assert (from_kassa.lines.first().landed_cost_per_kg
                == from_logist.lines.first().landed_cost_per_kg)


class TestLogistScreens:
    def test_list_shows_the_balance_and_both_position_figures(self, admin_client, db):
        logist = _logist("Sardor aka")
        shipment = _shipment(logist)
        _send(logist, "10000")
        _advance(shipment, logist, "500")
        resp = admin_client.get("/logists/")
        assert resp.status_code == 200
        from crm.models import Currency
        assert resp.context["held"] == [(Currency.USD, Decimal("9500.00"))]
        assert resp.context["owed"] == []
        html = resp.content.decode()
        assert "Sardor aka" in html and "Logistlarda turgan pulimiz" in html

    def test_state_filter_splits_holders_from_creditors(self, admin_client, db):
        holder, ower = _logist("Ushlab turgan"), _logist("Qarzdor")
        _send(holder, "5000")
        _advance(_shipment(ower), ower, "800")
        names = lambda url: [x.name for x in admin_client.get(url).context["page"]]
        assert names("/logists/?state=holding") == ["Ushlab turgan"]
        assert names("/logists/?state=owed") == ["Qarzdor"]

    def test_the_two_daftar_are_kept_apart_each_newest_first(self, admin_client, db):
        """Yuborilgan pul and Sarflangan pul are two lists, not two columns of one.

        They were one timeline ordered by date, which reads as a bank statement — and
        answering either question it holds ("have we funded them enough", "what has
        that money bought") meant reading past every row belonging to the other, on a
        line where one of the two money columns was blank by construction."""
        logist = _logist()
        shipment = _shipment(logist)
        _send(logist, "10000", date="2026-07-01")
        _send(logist, "4000", date="2026-07-09")
        _advance(shipment, logist, "500", date="2026-07-08")

        ctx = admin_client.get(f"/logists/{logist.pk}/").context
        sent, spent = list(ctx["sent_page"]), list(ctx["spent_page"])
        assert [r["date"] for r in sent] == [_date(2026, 7, 9), _date(2026, 7, 1)]
        assert [r["amount"] for r in sent] == [Decimal("4000.00"), Decimal("10000.00")]
        assert [r["date"] for r in spent] == [_date(2026, 7, 8)]
        assert [r["amount"] for r in spent] == [Decimal("500.00")]

    def test_neither_daftar_carries_the_other_side_s_rows(self, admin_client, db):
        """The whole point of the split: nothing appears on both, and nothing that
        moved money is missing from both."""
        logist = _logist()
        shipment = _shipment(logist)
        payment = _send(logist, "10000", date="2026-07-01")
        advance = _advance(shipment, logist, "500", date="2026-07-08")

        ctx = admin_client.get(f"/logists/{logist.pk}/").context
        assert [r["obj"].pk for r in ctx["sent_page"]] == [payment.pk]
        assert [r["obj"].pk for r in ctx["spent_page"]] == [advance.pk]

    def test_a_load_with_no_advance_yet_is_still_on_the_page(self, admin_client, db):
        """A yuk moves no money of ours on its own, so it is no longer a row on a
        money daftar — a zero summa would read as a payment of nothing. It belongs to
        Qaysi yuklarga, which now seeds from every load assigned to them so the one
        still WAITING for an advance is not the single load missing from the page."""
        logist = _logist()
        shipment = _shipment(logist)

        ctx = admin_client.get(f"/logists/{logist.pk}/").context
        assert list(ctx["sent_page"]) == [] and list(ctx["spent_page"]) == []
        assert [r["shipment"].pk for r in ctx["loads"]] == [shipment.pk]
        # Nothing paid on it yet, and the table says so rather than printing a zero.
        assert ctx["loads"][0]["paid"] == []
        assert "hali yo'q" in admin_client.get(f"/logists/{logist.pk}/").content.decode()

    def test_a_logist_with_no_loads_still_shows_only_their_money(self, admin_client, db):
        logist = _logist()
        payment = _send(logist, "10000", date="2026-07-01")
        ctx = admin_client.get(f"/logists/{logist.pk}/").context
        assert [r["obj"].pk for r in ctx["sent_page"]] == [payment.pk]
        assert list(ctx["spent_page"]) == [] and ctx["loads"] == []

    def test_each_daftar_pages_on_its_own(self, admin_client, db):
        """?ipage and ?opage, the two names the kassa's pair use: paging the to'lovlar
        must not scroll the advances out from under the reader."""
        logist = _logist()
        shipment = _shipment(logist)
        for i in range(25):
            _send(logist, "100", date=f"2026-07-{i % 28 + 1:02d}")
        _advance(shipment, logist, "500", date="2026-07-08")

        ctx = admin_client.get(f"/logists/{logist.pk}/", {"ipage": 2}).context
        assert ctx["sent_page"].number == 2 and len(ctx["sent_page"].object_list) == 5
        # The other daftar stayed where it was.
        assert ctx["spent_page"].number == 1
        assert [r["amount"] for r in ctx["spent_page"]] == [Decimal("500.00")]

    def test_the_money_they_are_holding_is_read_on_their_own_screen(self, admin_client, db):
        logist = _logist()
        _send(logist, "10000")
        from crm.models import Currency
        assert admin_client.get("/logists/").context["held"] == [
            (Currency.USD, Decimal("10000.00"))]

    def test_a_logist_with_money_behind_them_cannot_be_deleted(self, admin_client, db):
        logist = _logist()
        _send(logist, "10000")
        admin_client.post(f"/logists/{logist.pk}/delete/", {})
        assert Logist.objects.filter(pk=logist.pk).exists()

    def test_expense_form_defaults_to_the_yuk_own_logist(self, admin_client, db):
        """Picking the wrong one silently moves money between two people's accounts."""
        from crm.forms import ShipmentExpenseForm
        logist = _logist()
        shipment = _shipment(logist)
        form = ShipmentExpenseForm(initial={"shipment": shipment})
        assert form.initial.get("logist") == logist.pk

    def test_translator_forbidden(self, translator_client, db):
        assert translator_client.get("/logists/").status_code == 403
        assert translator_client.get("/logists/new/").status_code == 403
        assert translator_client.get("/logist-payments/new/").status_code == 403


class TestALogistWhoFrontedTheirOwnCash:
    """A debt nobody can see is a debt nobody pays."""

    def test_it_is_counted_as_money_we_owe(self, admin_client, db):
        logist = _logist()
        _advance(_shipment(logist), logist, "800")      # no funding sent first
        from crm.models import Currency, logist_positions
        # Positive, like every other qarzimiz figure: which direction it goes is said
        # by the side it is on, not by a minus sign on one figure out of three.
        assert logist_positions()[1] == [(Currency.USD, Decimal("800.00"))]
        owed = admin_client.get("/logists/?state=owed").context["page"]
        assert [row.name for row in owed] == [logist.name]

    def test_nobody_is_owed_when_the_money_went_out_first(self, admin_client, db):
        from crm.models import Currency, logist_positions
        _send(_logist(), "5000")
        held, owed = logist_positions()
        assert held == [(Currency.USD, Decimal("5000.00"))]
        assert owed == []


class TestDriverAdvanceOnDispatch:
    """The advance is handed over as the truck leaves, so it is entered on the yuk
    form — not remembered later as an xarajat."""

    def _contract(self):
        partner = Partner.objects.create(name="Pars", phone="1", city="T")
        contract = Contract.objects.create(partner=partner, created="2026-07-01")
        ContractLine.objects.create(contract=contract, brand="LLDPE",
                                    kg=Decimal("24000"), price=Decimal("1.00"),
                                    price_uzs=Decimal("12000"))
        return contract

    def _post(self, admin_client, contract, logist=None, advance="", url="/shipments/new/"):
        line = contract.lines.first()
        body = {
            "contract": contract.pk, "status": ShipmentStatus.objects.first().pk,
            "sent": "2026-07-05", "eta": "2026-07-15", "responsible": "",
            "driver_name": "Akmal aka", "driver_phone": "", "transport": "",
            "container": "", "note": "",
            "driver_advance": advance,
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "10",
            "lines-0-contract_line": line.pk, "lines-0-kg": "24000",
            "lines-0-price": "", "lines-0-currency": "usd",
            "lines-0-exchange_rate": "12000", "lines-0-id": "",
        }
        if logist:
            body["logist"] = logist.pk
        return admin_client.post(url, body)

    def test_creating_a_yuk_records_the_advance(self, admin_client, db):
        logist = _logist()
        resp = self._post(admin_client, self._contract(), logist, advance="500")
        assert resp.status_code == 302
        advance = ShipmentExpense.objects.get()
        assert advance.is_driver_advance is True
        assert advance.category == "transport"
        assert advance.logist_id == logist.pk
        assert advance.amount == Decimal("500.00")
        assert advance.from_kassa is False
        logist.refresh_from_db()
        assert logist.balance == Decimal("-500.00")

    def test_it_prices_the_yuk_but_not_the_kassa(self, admin_client, db):
        logist = _logist()
        _send(logist, "10000")
        self._post(admin_client, self._contract(), logist, advance="480")
        shipment = Shipment.objects.get()
        assert shipment.expenses_total == Decimal("480.00")
        assert shipment.expense_per_kg == Decimal("0.02")
        ctx = admin_client.get("/kassa/?davr=all").context
        assert ctx["net_out"] == Decimal("10000.00")     # only the funding

    def test_an_advance_with_no_logist_is_refused(self, admin_client, db):
        """Otherwise it silently becomes an ordinary kassa expense — the exact
        double-count this feature exists to prevent."""
        resp = self._post(admin_client, self._contract(), logist=None, advance="500")
        assert resp.status_code == 200                   # re-rendered, invalid
        assert not ShipmentExpense.objects.exists()
        assert "Logistni tanlang" in resp.content.decode()

    def test_no_advance_creates_no_row(self, admin_client, db):
        self._post(admin_client, self._contract(), _logist(), advance="")
        assert not ShipmentExpense.objects.exists()

    def test_the_som_side_uses_the_kurs_we_funded_the_logist_at(self, admin_client, db):
        """Advances are dollars out of money we already sent, so re-rating them at
        today's kurs would give them a so'm value that money never had."""
        logist = _logist()
        _send(logist, "10000", date="2026-07-01")          # at 12 000
        LogistPayment.objects.create(logist=logist, date="2026-07-20",
                                     amount=Decimal("5000"),
                                     amount_uzs=Decimal("64000000"),
                                     exchange_rate=Decimal("12800"), method="cash")
        assert logist.latest_rate == Decimal("12800.00")
        self._post(admin_client, self._contract(), logist, advance="500")
        advance = ShipmentExpense.objects.get(is_driver_advance=True)
        assert advance.currency == "usd"
        assert advance.amount == Decimal("500.00")
        assert advance.exchange_rate == Decimal("12800.00")
        assert advance.amount_uzs == Decimal("6400000.00")   # 500 × 12 800

    def test_an_unfunded_logist_still_takes_an_advance(self, admin_client, db):
        """They may have paid the driver before we sent them anything."""
        logist = _logist()
        self._post(admin_client, self._contract(), logist, advance="500")
        advance = ShipmentExpense.objects.get(is_driver_advance=True)
        assert advance.amount == Decimal("500.00")
        assert logist.balance == Decimal("-500.00")

    def test_editing_the_yuk_rewrites_the_same_row(self, admin_client, db):
        logist = _logist()
        contract = self._contract()
        self._post(admin_client, contract, logist, advance="500")
        shipment = Shipment.objects.get()
        line = shipment.lines.first()
        body_extra = {"lines-INITIAL_FORMS": "1", "lines-0-id": line.pk}
        resp = self._post(admin_client, contract, logist, advance="800",
                          url=f"/shipments/{shipment.pk}/edit/")
        # the formset needs the existing line's id on edit
        admin_client.post(f"/shipments/{shipment.pk}/edit/", {
            **{k: v for k, v in [("contract", contract.pk),
                                 ("status", ShipmentStatus.objects.first().pk),
                                 ("sent", "2026-07-05"), ("eta", "2026-07-15"),
                                 ("logist", logist.pk), ("responsible", ""),
                                 ("driver_name", "Akmal aka"), ("driver_phone", ""),
                                 ("transport", ""), ("container", ""), ("note", ""),
                                 ("driver_advance", "800"), ("advance_currency", "usd"),
                                 ("advance_rate", "12000"),
                                 ("lines-TOTAL_FORMS", "1"), ("lines-MIN_NUM_FORMS", "0"),
                                 ("lines-MAX_NUM_FORMS", "10"),
                                 ("lines-0-contract_line", contract.lines.first().pk),
                                 ("lines-0-kg", "24000"), ("lines-0-price", ""),
                                 ("lines-0-currency", "usd"),
                                 ("lines-0-exchange_rate", "12000")]},
            **body_extra})
        assert ShipmentExpense.objects.count() == 1      # rewritten, not duplicated
        assert ShipmentExpense.objects.get().amount == Decimal("800.00")

    def test_the_advance_shows_when_reopening_the_yuk(self, admin_client, db):
        from crm.forms import ShipmentForm
        logist = _logist()
        self._post(admin_client, self._contract(), logist, advance="500")
        shipment = Shipment.objects.get()
        form = ShipmentForm(instance=shipment)
        assert form.initial["driver_advance"] == Decimal("500.00")

    def test_it_never_touches_an_unrelated_expense_the_logist_paid(self, admin_client, db):
        """A logist can pay this load's bojxona too — editing the yuk must rewrite
        only the advance, which is why the row carries its own flag."""
        logist = _logist()
        contract = self._contract()
        self._post(admin_client, contract, logist, advance="500")
        shipment = Shipment.objects.get()
        from crm.forms import ShipmentForm
        bojxona = ShipmentExpense.objects.create(
            shipment=shipment, date="2026-07-06", category="customs",
            amount=Decimal("3200"), amount_uzs=Decimal("38400000"),
            method="cash", logist=logist)
        form = ShipmentForm(instance=shipment)
        form.cleaned_data = {"driver_advance": Decimal("900"), "logist": logist}
        form.sync_driver_advance(shipment, None)
        bojxona.refresh_from_db()
        assert bojxona.amount == Decimal("3200.00")      # untouched
        assert shipment.expenses.filter(is_driver_advance=True).get().amount \
            == Decimal("900.00")


class TestAdvanceFieldPresentation:
    def test_the_advance_sits_with_the_logist_not_at_the_end(self, db):
        """Fields about the logist stranded six fields away from the logist picker
        is the kind of thing declaration order does silently."""
        from crm.forms import ShipmentForm
        order = list(ShipmentForm().fields)
        assert order.index("driver_advance") == order.index("logist") + 1

    def test_no_currency_or_kurs_is_asked_for(self, db):
        """Advances are dollars, and the so'm side comes off the logist's own
        funding — asking again would let the two disagree."""
        from crm.forms import ShipmentForm
        fields = ShipmentForm().fields
        assert "advance_currency" not in fields
        assert "advance_rate" not in fields
        assert "$" in str(fields["driver_advance"].label)

    def test_the_advance_fields_are_boxed_together(self, admin_client, db):
        """The modal carries a Valyuta and a kurs on every Mahsulot row too, so the
        advance's pair has to be visibly part of the logist block."""
        html = admin_client.get("/shipments/new/",
                                headers={"X-Requested-With": "XMLHttpRequest"}).content.decode()
        assert '<fieldset class="fieldgroup">' in html
        box = html.split('<fieldset class="fieldgroup">')[1].split("</fieldset>")[0]
        for name in ("logist", "driver_advance"):
            assert f'name="{name}"' in box, name
        # the Mahsulot row's own currency stays outside the box
        assert 'name="lines-0-currency"' not in box

    def test_forms_without_groups_still_render_every_field(self, admin_client, db):
        """Regression: the shared partial called an attribute only the mixin has,
        and a missing attribute resolves to empty — so every ungrouped form in the
        app rendered nothing at all."""
        html = admin_client.get("/supplier-payments/new/",
                                headers={"X-Requested-With": "XMLHttpRequest"}).content.decode()
        # The shared header, then the fields of the first to'lov row: this modal takes
        # several ways one payment moved, so the money boxes are formset-prefixed.
        for name in ("contract", "date", "form-0-amount", "form-0-method"):
            assert f'name="{name}"' in html, name
        assert "fieldgroup" not in html
