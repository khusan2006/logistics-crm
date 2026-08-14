"""Kapital: the ta'sischi's own money going into or out of the kassa.

The kassa had four models that took money out and one that brought it in, so the
money that FUNDED the business had nowhere to be recorded and "Kassadagi pul" read
as how much had been sunk into it rather than what is on hand. These tests pin the
two things that makes true: the till moves by what was put in, and a kapital row
never pretends to be a mijoz to'lov.
"""

from decimal import Decimal

from conftest import customs_payment_rows, kapital_rows, logist_payment_rows
from crm.models import (
    Customer, CustomerPayment, Kapital, KapitalKind, kapital_total_by_currency,
    kassa_cash_by_currency,
)


def _customer(name="Komoliddin"):
    return Customer.objects.create(name=name, phone="+998901112233")


def _kapital(amount="50000", kind=KapitalKind.IN, currency="usd", **kw):
    """A kapital row. so'm rows carry their own stored twin so nothing is inferred
    from a kurs the test did not choose."""
    amount = Decimal(amount)
    rate = Decimal(kw.pop("exchange_rate", "12000"))
    usd = amount if currency == "usd" else (amount / rate).quantize(Decimal("0.01"))
    return Kapital.objects.create(
        kind=kind, date=kw.pop("date", "2026-07-01"), currency=currency,
        exchange_rate=rate, amount=usd,
        amount_uzs=amount if currency == "uzs" else usd * rate,
        method=kw.pop("method", "cash"), **kw)


def _split(pairs):
    return dict(pairs)


class TestTheTillMoves:
    def test_money_put_in_raises_the_kassa(self, db):
        _kapital("50000")
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("50000.00")

    def test_money_taken_out_lowers_it(self, db):
        _kapital("50000")
        _kapital("20000", kind=KapitalKind.OUT)
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("30000.00")

    def test_kapital_and_mijoz_money_land_in_the_same_till(self, db):
        _kapital("50000")
        CustomerPayment.objects.create(customer=_customer(), date="2026-07-02",
                                       amount=Decimal("500.00"), method="cash")
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("50500.00")

    def test_taking_out_more_than_was_put_in_goes_negative(self, db):
        """Nothing blocks it and nothing should: the model records what happened, and
        a ta'sischi can draw against money the business earned rather than money they
        put in."""
        _kapital("1000")
        _kapital("2500", kind=KapitalKind.OUT)
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("-1500.00")


class TestCurrenciesStayApart:
    def test_a_som_kapital_never_joins_the_dollar_heap(self, db):
        _kapital("600000000", currency="uzs")
        split = _split(kassa_cash_by_currency())
        assert split["uzs"] == Decimal("600000000.00")
        assert "usd" not in split

    def test_each_side_moves_on_its_own(self, db):
        _kapital("50000")
        _kapital("600000000", currency="uzs")
        _kapital("120000000", kind=KapitalKind.OUT, currency="uzs")
        split = _split(kassa_cash_by_currency())
        assert split["usd"] == Decimal("50000.00")
        assert split["uzs"] == Decimal("480000000.00")


class TestBankFoiz:
    def test_only_what_landed_counts(self, db):
        """1000 sent by perechisleniya at 2% puts 980 in the till — the bank's cut
        never arrived, so counting the full figure would invent 20 dollars."""
        _kapital("1000", method="transfer", fee_percent=Decimal("2"))
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("980.00")

    def test_naqd_is_never_charged_one(self, db):
        _kapital("1000", method="cash", fee_percent=Decimal("2"))
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("1000.00")

    def test_a_withdrawal_nets_too(self, db):
        """Taking 1000 out by perechisleniya at 2% costs the till 980: the cut is
        the ta'sischi's loss on the way, exactly as on the way in."""
        _kapital("5000")
        _kapital("1000", kind=KapitalKind.OUT, method="transfer",
                 fee_percent=Decimal("2"))
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("4020.00")


class TestTheTotalHelper:
    def test_it_nets_the_two_directions(self, db):
        _kapital("50000")
        _kapital("20000", kind=KapitalKind.OUT)
        assert _split(kapital_total_by_currency())["usd"] == Decimal("30000.00")

    def test_it_ignores_mijoz_money(self, db):
        """The whole point of the figure: it answers "how much of the till was put
        in rather than earned", so a mijoz to'lov must not show up in it."""
        _kapital("50000")
        CustomerPayment.objects.create(customer=_customer(), date="2026-07-02",
                                       amount=Decimal("500.00"), method="cash")
        assert _split(kapital_total_by_currency())["usd"] == Decimal("50000.00")

    def test_nothing_entered_is_an_empty_answer(self, db):
        assert kapital_total_by_currency() == []


class TestTheKassaScreen:
    def _ctx(self, client):
        return client.get("/kassa/?davr=all").context

    def _tiles(self, client):
        return {t["label"]: t for t in self._ctx(client)["tiles"]}

    def test_kapital_is_not_a_tile(self, admin_client, db):
        """A tile answers "where is money sitting right now". Kapital is not a place
        money sits, it is where some of it came from — it reads in the kirim daftar
        below, and the tile list must not grow."""
        _kapital("50000")
        assert "Kapital" not in self._tiles(admin_client)

    def test_the_hero_card_carries_no_explaining_lines(self, admin_client, db):
        """The Kassada card is the one the page is opened for, so it is label and the
        three heaps and nothing else. The "shundan kapital / shundan mijoz avansi"
        lines that used to sit here pushed the only figures that matter down the card;
        both facts stay readable where they are acted on."""
        _kapital("50000")
        CustomerPayment.objects.create(customer=_customer(), date="2026-07-02",
                                       amount=Decimal("500.00"), method="cash")
        hero = self._tiles(admin_client)["Kassada"]
        assert hero["meta"] == ""
        assert hero["note"] == ""

    def test_the_hero_figure_includes_it(self, admin_client, db):
        _kapital("50000")
        ctx = self._ctx(admin_client)
        assert ctx["cash_total"] == Decimal("50000.00")

    def test_money_in_lands_in_the_kirim_ledger(self, admin_client, db):
        _kapital("50000")
        rows = list(self._ctx(admin_client)["income_page"].object_list)
        assert [r["kind"] for r in rows] == ["kapital"]
        assert rows[0]["title"] == "Ta'sischi kapitali"
        assert rows[0]["amount"] == Decimal("50000.00")

    def test_money_out_lands_in_the_chiqim_ledger(self, admin_client, db):
        """On the other side, not as a negative kirim: a ledger total that mixed the
        two would net them and report neither."""
        _kapital("20000", kind=KapitalKind.OUT)
        ctx = self._ctx(admin_client)
        assert list(ctx["income_page"].object_list) == []
        rows = [r for r in ctx["outflow_page"].object_list if r["kind"] == "kapital"]
        assert rows[0]["amount"] == Decimal("20000.00")
        assert ctx["net_out"] == Decimal("20000.00")

    def test_the_note_carries_into_the_row(self, db, admin_client):
        _kapital("50000", note="birinchi ulush")
        rows = list(self._ctx(admin_client)["income_page"].object_list)
        assert rows[0]["title"] == "Ta'sischi kapitali · birinchi ulush"

    def test_the_ledger_rows_still_add_up_to_the_ledger_total(self, admin_client, db):
        _kapital("50000")
        _kapital("20000", kind=KapitalKind.OUT)
        CustomerPayment.objects.create(customer=_customer(), date="2026-07-02",
                                       amount=Decimal("500.00"), method="cash")
        ctx = self._ctx(admin_client)
        income = sum((r["amount"] for r in ctx["income_page"].object_list),
                     Decimal("0"))
        assert income == ctx["net_in"]
        assert ctx["net_total"] == ctx["cash_total"]

    def test_the_date_filter_narrows_the_ledger_but_not_the_tile(self, admin_client, db):
        _kapital("50000", date="2026-05-01")
        _kapital("10000", date="2026-07-10")
        ctx = admin_client.get("/kassa/", {"from": "2026-07-01",
                                           "to": "2026-07-31"}).context
        assert [r["amount"] for r in ctx["income_page"].object_list] == [
            Decimal("10000.00")]
        # The hero is all-time on purpose — the till holds what it holds.
        tiles = {t["label"]: t for t in ctx["tiles"]}
        assert dict(tiles["Kassada"]["split"])["usd"] == Decimal("60000.00")


class TestTheForm:
    def test_admin_can_book_money_in(self, admin_client, db):
        resp = admin_client.post(
            "/kapital/new/",
            kapital_rows({"currency": "usd", "amount": "50000",
                          "exchange_rate": "12000", "method": "cash",
                          "fee_percent": "0", "note": "ta'sischi ulushi"},
                         kind="in", date="2026-07-01"))
        assert resp.status_code in (200, 302), resp.content[:400]
        entry = Kapital.objects.get()
        assert entry.amount == Decimal("50000.00")
        assert entry.kind == KapitalKind.IN
        # The so'm twin is never left empty — a row with none reads as 0 so'm on
        # every so'm screen, which looks like a figure rather than a gap.
        assert entry.amount_uzs == Decimal("600000000.00")

    def test_it_records_who_entered_it(self, admin_client, db, django_user_model):
        admin_client.post(
            "/kapital/new/",
            kapital_rows({"currency": "usd", "amount": "1000",
                          "exchange_rate": "12000", "method": "cash",
                          "fee_percent": "0"},
                         kind="in", date="2026-07-01"))
        assert Kapital.objects.get().created_by is not None

    def test_a_negative_amount_is_refused(self, admin_client, db):
        admin_client.post(
            "/kapital/new/",
            kapital_rows({"currency": "usd", "amount": "-5000",
                          "exchange_rate": "12000", "method": "cash",
                          "fee_percent": "0"},
                         kind="in", date="2026-07-01"))
        assert not Kapital.objects.exists()

    def test_a_foiz_over_a_hundred_is_refused(self, admin_client, db):
        admin_client.post(
            "/kapital/new/",
            kapital_rows({"currency": "usd", "amount": "5000",
                          "exchange_rate": "12000", "method": "transfer",
                          "fee_percent": "200"},
                         kind="in", date="2026-07-01"))
        assert not Kapital.objects.exists()

    def test_an_edit_keeps_the_kurs_the_row_was_booked_at(self, admin_client, db):
        """A figure already on the books must not move on its own when somebody
        reopens the modal to fix a note."""
        entry = _kapital("50000", exchange_rate="11500")
        admin_client.post(f"/kapital/{entry.pk}/edit/", {
            "kind": "in", "date": "2026-07-01", "currency": "usd",
            "amount": "50000", "exchange_rate": "", "method": "cash",
            "fee_percent": "0", "note": "izoh"})
        entry.refresh_from_db()
        assert entry.exchange_rate == Decimal("11500.00")
        assert entry.note == "izoh"

    def test_deleting_takes_the_money_back_out_of_the_till(self, admin_client, db):
        entry = _kapital("50000")
        admin_client.post(f"/kapital/{entry.pk}/delete/")
        assert not Kapital.objects.exists()
        assert kassa_cash_by_currency() == []

    def test_the_modal_renders(self, admin_client, db):
        """A form whose template blows up 500s only when somebody opens it, long
        after the page linking to it was called done."""
        assert admin_client.get("/kapital/new/").status_code == 200

    def test_the_kassa_offers_the_button(self, admin_client, db):
        assert "/kapital/new/" in admin_client.get("/kassa/?davr=all").content.decode()


class TestPermissions:
    def test_a_tarjimon_cannot_book_kapital(self, translator_client, db):
        assert translator_client.get("/kapital/new/").status_code == 403
        assert translator_client.post(
            "/kapital/new/",
            kapital_rows({"currency": "usd", "amount": "50000",
                          "exchange_rate": "12000", "method": "cash",
                          "fee_percent": "0"},
                         kind="in", date="2026-07-01")).status_code == 403
        assert not Kapital.objects.exists()

    def test_a_tarjimon_cannot_edit_or_delete_one(self, translator_client, db):
        entry = _kapital("50000")
        assert translator_client.get(f"/kapital/{entry.pk}/edit/").status_code == 403
        assert translator_client.post(f"/kapital/{entry.pk}/delete/").status_code == 403
        assert Kapital.objects.exists()


class TestSplitOutgoingPayments:
    """One payment, the several ways it actually left — on every chiqim modal.

    Half naqd and half perechisleniya is one settlement to the person making it and
    two movements to the kassa: the safe goes down by one figure, the bank by the
    other, and only the transfer carries a bank foiz. Rows are what let a single
    modal say all three of those things at once."""

    def test_a_logist_top_up_can_leave_two_ways_at_once(self, admin_client, db):
        from crm.models import Logist, LogistPayment
        logist = Logist.objects.create(name="Sardor aka", phone="1")
        resp = admin_client.post(
            "/logist-payments/new/",
            logist_payment_rows({"amount": "600", "method": "cash"},
                                {"amount": "400", "method": "transfer"},
                                logist=logist.pk))
        assert resp.status_code == 302
        rows = LogistPayment.objects.order_by("pk")
        assert [(r.method, r.amount) for r in rows] == [
            ("cash", Decimal("600.00")), ("transfer", Decimal("400.00"))]
        # One float, whichever way the money reached it.
        assert logist.balance == Decimal("1000.00")

    def test_a_bojxona_payment_can_leave_two_ways_at_once(self, admin_client, db):
        from crm.models import CustomsAgent, CustomsPayment
        agent = CustomsAgent.objects.create(name="Bojxonachi", phone="1")
        resp = admin_client.post(
            "/customs-payments/new/",
            customs_payment_rows({"amount": "20000000", "method": "cash"},
                                 {"amount": "20000000", "method": "transfer"},
                                 agent=agent.pk))
        assert resp.status_code == 302
        rows = CustomsPayment.objects.order_by("pk")
        assert [r.method for r in rows] == ["cash", "transfer"]
        assert sum(r.amount_uzs for r in rows) == Decimal("40000000.00")

    def test_kapital_can_be_put_in_two_ways_at_once(self, admin_client, db):
        resp = admin_client.post(
            "/kapital/new/",
            kapital_rows({"amount": "30000", "method": "cash"},
                         {"amount": "20000", "method": "transfer"},
                         kind=KapitalKind.IN))
        assert resp.status_code == 302
        rows = Kapital.objects.order_by("pk")
        assert [r.method for r in rows] == ["cash", "transfer"]
        # The direction is the settlement's, so both rows carry it.
        assert {r.kind for r in rows} == {KapitalKind.IN}
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("50000.00")

    def test_only_the_transfer_half_pays_a_bank_foiz(self, admin_client, db):
        """The reason these are rows and not one row with a breakdown: 2% off the
        perechisleniya and nothing off the naqd."""
        resp = admin_client.post(
            "/kapital/new/",
            kapital_rows({"amount": "1000", "method": "cash", "fee_percent": "2"},
                         {"amount": "1000", "method": "transfer", "fee_percent": "2"},
                         kind=KapitalKind.IN))
        assert resp.status_code == 302
        naqd, bank = Kapital.objects.order_by("pk")
        assert naqd.fee_amount == Decimal("0")
        assert bank.fee_amount == Decimal("20.00")
        # 1000 naqd + 980 that actually landed
        assert _split(kassa_cash_by_currency())["usd"] == Decimal("1980.00")

    def test_the_kassa_shows_each_half_where_it_actually_went(self, admin_client, db):
        resp = admin_client.post(
            "/kapital/new/",
            kapital_rows({"amount": "300", "method": "cash"},
                         {"amount": "400", "method": "card"},
                         kind=KapitalKind.IN))
        assert resp.status_code == 302
        balances = admin_client.get("/kassa/?davr=all").context["balances"]
        assert balances["cash"]["in"] == Decimal("300.00")
        assert balances["card"]["in"] == Decimal("400.00")

    def test_nothing_is_written_when_one_row_is_bad(self, admin_client, db):
        """All of it or none — a settlement half-written is worse than one refused."""
        resp = admin_client.post(
            "/kapital/new/",
            kapital_rows({"amount": "300", "method": "cash"},
                         {"amount": "-5", "method": "transfer"},
                         kind=KapitalKind.IN))
        assert resp.status_code == 200
        assert not Kapital.objects.exists()
