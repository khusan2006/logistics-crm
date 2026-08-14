"""Kassada — pul qayerda turibdi, va kassa bo'yicha universal qidiruv.

Two things the till page was not saying. "Kassada $12 000" answers how much we have;
it does not answer how much can be handed over this minute — cash in the safe, money
on a card and a bank balance are three different answers, and only the first is
immediate. And the two daftar under it had no search at all: a to'lov was found by
paging.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

import openpyxl
from conftest import make_contract, make_shipment
from crm.models import (
    Currency,
    Customer,
    CustomerPayment,
    Kapital,
    PayMethod,
    SupplierPayment,
    kassa_cash_by_currency,
    kassa_cash_by_method,
)


def _customer(name="Alisher"):
    return Customer.objects.create(name=name, phone="1")


def _in(amount, method, currency="usd", day=10, customer=None, rate="12000"):
    return CustomerPayment.objects.create(
        customer=customer or _customer(), date=date(2026, 7, day),
        amount=Decimal(amount), currency=currency, exchange_rate=Decimal(rate),
        amount_uzs=Decimal(amount) * Decimal(rate) if currency == "usd" else Decimal(amount),
        method=method)


def _methods(client):
    return {m["label"]: dict(m["split_full"])
            for m in client.get("/kassa/?davr=all").context["cash_by_method"]}


class TestWhereTheMoneyIsHeld:
    def test_each_method_is_its_own_heap(self, admin_client, db):
        customer = _customer()
        _in("1000", PayMethod.CASH, customer=customer)
        _in("2500", PayMethod.CARD, customer=customer)
        _in("400", PayMethod.TRANSFER, customer=customer)

        rows = _methods(admin_client)
        assert rows["Naqd"][Currency.USD] == Decimal("1000.00")
        assert rows["Karta"][Currency.USD] == Decimal("2500.00")
        assert rows["Bank o'tkazmasi"][Currency.USD] == Decimal("400.00")

    def test_the_three_add_up_to_the_till(self, admin_client, db):
        """If they did not, one of the two figures on this page would be a lie — and
        the operator has no way to tell which."""
        customer = _customer()
        _in("1000", PayMethod.CASH, customer=customer)
        _in("500", PayMethod.CARD, customer=customer)
        _in("12000000", PayMethod.TRANSFER, currency="uzs", customer=customer)
        SupplierPayment.objects.create(contract=make_contract(kg="9000"),
                                       date=date(2026, 7, 12), amount=Decimal("300"),
                                       method=PayMethod.CASH)

        per_method = kassa_cash_by_method()
        for currency, total in kassa_cash_by_currency():
            summed = sum((dict(m["split"]).get(currency, Decimal("0"))
                          for m in per_method), Decimal("0"))
            assert summed == total, currency

    def test_som_and_dollar_stay_apart_inside_one_method(self, admin_client, db):
        """A card holding so'm and a card holding dollars are two heaps, exactly as
        everywhere else on this page."""
        customer = _customer()
        _in("500", PayMethod.CARD, customer=customer)
        _in("6000000", PayMethod.CARD, currency="uzs", customer=customer)

        card = _methods(admin_client)["Karta"]
        assert card[Currency.USD] == Decimal("500.00")
        assert card[Currency.UZS] == Decimal("6000000.00")

    def test_an_empty_method_is_drawn_as_a_zero_not_left_out(self, admin_client, db):
        """The line is checked against what is in the safe; a missing one reads as
        "not counted" rather than as "none"."""
        _in("1000", PayMethod.CASH)
        rows = _methods(admin_client)
        assert set(rows) == {"Naqd", "Karta", "Bank o'tkazmasi"}
        assert rows["Karta"] == {Currency.USD: Decimal("0"), Currency.UZS: Decimal("0")}

    def test_money_taken_out_lowers_the_method_it_left_by(self, admin_client, db):
        _in("1000", PayMethod.CASH)
        Kapital.objects.create(kind="out", date=date(2026, 7, 15), amount=Decimal("400"),
                               exchange_rate=Decimal("12000"), method=PayMethod.CASH)
        assert _methods(admin_client)["Naqd"][Currency.USD] == Decimal("600.00")


class TestKassaSearch:
    def _counts(self, client, **params):
        # Searching is asked over ALL the money unless a test names a davr — the
        # screen opens on bugun (see `_kassa_date_window`) and these rows are July's.
        resp = client.get("/kassa/", {"davr": "all", **params})
        return (resp.context["income_page"].paginator.count,
                resp.context["outflow_page"].paginator.count)

    def test_search_finds_a_row_by_who_it_was_with(self, admin_client, db):
        _in("1000", PayMethod.CASH, customer=Customer.objects.create(name="Alisher", phone="1"))
        _in("2000", PayMethod.CASH, customer=Customer.objects.create(name="Bekzod", phone="2"))

        resp = admin_client.get("/kassa/", {"davr": "all", "q": "alisher"})
        assert [r["title"] for r in resp.context["income_page"].object_list] == ["Alisher"]

    def test_search_reaches_both_daftar(self, admin_client, db):
        """One box over Kirim AND Chiqim: money is remembered as a fragment, not as a
        side of the page."""
        _in("1000", PayMethod.CASH, customer=Customer.objects.create(name="Pars mijoz", phone="1"))
        SupplierPayment.objects.create(
            contract=make_contract(kg="9000"), date=date(2026, 7, 12),
            amount=Decimal("300"), method=PayMethod.CASH)

        assert self._counts(admin_client, q="pars") == (1, 1)

    def test_search_matches_the_usul_and_the_turi(self, admin_client, db):
        _in("1000", PayMethod.CASH)
        _in("2000", PayMethod.CARD)

        assert self._counts(admin_client, q="naqd")[0] == 1
        assert self._counts(admin_client, q="karta")[0] == 1

    def test_a_sum_is_matched_however_it_is_typed(self, admin_client, db):
        """28800, 28 800 and 28,800.00 are one figure to the person searching."""
        _in("28800", PayMethod.CASH)
        _in("500", PayMethod.CASH)

        for typed in ("28800", "28 800", "28,800.00"):
            assert self._counts(admin_client, q=typed)[0] == 1, typed

    def test_search_matches_the_day(self, admin_client, db):
        _in("1000", PayMethod.CASH, day=10)
        _in("2000", PayMethod.CASH, day=11)

        assert self._counts(admin_client, q="10.07.2026")[0] == 1

    def test_the_search_and_the_davr_narrow_together(self, admin_client, db):
        customer = _customer("Alisher")
        _in("1000", PayMethod.CASH, day=10, customer=customer)
        _in("2000", PayMethod.CASH, day=25, customer=customer)

        assert self._counts(admin_client, q="alisher")[0] == 2
        assert self._counts(admin_client, q="alisher",
                            **{"from": "2026-07-01", "to": "2026-07-15"})[0] == 1

    def test_the_headline_totals_follow_the_search(self, admin_client, db):
        """The +/- above each daftar totals the rows under it. Left unsearched they
        would contradict the list they sit on top of."""
        customer = _customer("Alisher")
        _in("1000", PayMethod.CASH, customer=customer)
        _in("500", PayMethod.CASH, customer=Customer.objects.create(name="Bekzod", phone="2"))

        resp = admin_client.get("/kassa/", {"davr": "all", "q": "alisher"})
        assert dict(resp.context["income_split"])[Currency.USD] == Decimal("1000.00")

    def test_the_board_above_is_not_touched_by_the_search(self, admin_client, db):
        """Those tiles are the state of things today, not a slice of this list."""
        _in("1000", PayMethod.CASH)
        searched = admin_client.get("/kassa/", {"davr": "all", "q": "zzz"}).context
        plain = admin_client.get("/kassa/?davr=all").context
        assert searched["cash_by_method"] == plain["cash_by_method"]
        assert [t["split"] for t in searched["tiles"]] == [t["split"] for t in plain["tiles"]]

    def test_nothing_found_says_what_was_looked_for(self, admin_client, db):
        _in("1000", PayMethod.CASH)
        html = admin_client.get("/kassa/", {"davr": "all", "q": "yoq-narsa"}).content.decode()
        assert "kirim topilmadi" in html and "chiqim topilmadi" in html

    def test_the_excel_button_downloads_what_the_search_left(self, admin_client, db):
        _in("1000", PayMethod.CASH, customer=Customer.objects.create(name="Alisher", phone="1"))
        _in("500", PayMethod.CASH, customer=Customer.objects.create(name="Bekzod", phone="2"))

        resp = admin_client.get("/kassa/export.xlsx", {"davr": "all", "q": "alisher"})
        wb = openpyxl.load_workbook(BytesIO(resp.content))
        assert len(list(wb["Kirim"].iter_rows(min_row=2))) == 1


def test_the_avans_tiles_no_longer_claim_the_money_is_coming_back(admin_client, db):
    """An avans at a logist or a bojxonachi is not handed back — it is spent on the
    next yuk. The heading says what is actually true of all four rows: the money is
    ours and it is somewhere else."""
    make_shipment(kg="400")
    ctx = admin_client.get("/kassa/?davr=all").context
    assert [g["title"] for g in ctx["tile_groups"]] == [
        "Mol — tannarxda", "Boshqa qo'ldagi pulimiz", "Qarzlarimiz"]
    labels = {t["label"] for t in ctx["tiles"]}
    assert {"Logistlarda avansimiz", "Bojxonada avansimiz"} <= labels
    assert "Bizga qaytadigan pul" not in admin_client.get("/kassa/?davr=all").content.decode()
