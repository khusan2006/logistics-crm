"""Boshqa chiqim — the "proche chiqim" that answers to no yuk and no counterparty.

Every other outflow in the app is tied to something and priced INTO something: a
hamkor to'lov settles a kelishuv, a yuk xarajati lands in a tannarx, a logist to'lov
funds a float. The office rent is none of those. Before this row it was either kept
off the books — so Kassada disagreed with the safe — or hung off whatever yuk happened
to be open, which quietly inflated that load's tannarx and every foyda behind it.

So the two things pinned here are: it DOES move the till, and it does NOT reach any
cost or foyda figure.
"""
from decimal import Decimal

import pytest

from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, OtherExpense,
    Partner, Shipment, ShipmentLine, ShipmentStatus, kassa_cash_by_currency,
    kassa_cash_by_method,
)

pytestmark = pytest.mark.django_db

RATE = Decimal("12000")


def _rows(*entries, note="Avgust ijarasi", date="2026-08-09"):
    """POST payload for the Boshqa chiqim modal: the shared izoh and sana, plus one
    row per way the money left. The twin of `supplier_payment_rows`."""
    data = {"note": note, "date": date,
            "form-TOTAL_FORMS": str(len(entries)), "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000"}
    defaults = {"currency": "usd", "amount": "0", "exchange_rate": str(RATE),
                "method": "cash", "fee_percent": ""}
    for i, entry in enumerate(entries):
        for key, value in {**defaults, **entry}.items():
            data[f"form-{i}-{key}"] = "" if value is None else str(value)
    return data


def _lot(kg="10000", price="1.00"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal(kg), price=Decimal(price),
        price_uzs=Decimal(price) * RATE)
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        arrived="2026-07-16")
    return ShipmentLine.objects.create(
        shipment=shipment, contract_line=line, kg=Decimal(kg))


class TestEnteringOne:
    def test_the_button_records_a_row_that_leaves_the_kassa(self, admin_client):
        resp = admin_client.post("/other-expenses/new/",
                                 _rows({"amount": "400"}, note="Avgust ijarasi"))
        assert resp.status_code in (200, 302)
        entry = OtherExpense.objects.get()
        assert entry.amount == Decimal("400.00")
        assert entry.note == "Avgust ijarasi"
        assert entry.total_out == Decimal("400.00")
        assert dict(kassa_cash_by_currency())[Currency.USD] == Decimal("-400.00")

    def test_one_payment_can_leave_by_two_usul_and_stays_one_payment(self, admin_client):
        """Half naqd and half by bank is one bill to the person paying it and two
        movements to the till — the same split every to'lov form in the app takes."""
        admin_client.post("/other-expenses/new/", _rows(
            {"amount": "300", "method": "cash"},
            {"amount": "100", "method": "transfer"}, note="Ish haqi"))
        rows = list(OtherExpense.objects.order_by("pk"))
        assert [r.amount for r in rows] == [Decimal("300.00"), Decimal("100.00")]
        # Shared izoh and sana, and one group id so a screen can draw them as one.
        assert {r.note for r in rows} == {"Ish haqi"}
        assert rows[0].group is not None and rows[0].group == rows[1].group

    def test_the_izoh_is_required_because_it_is_the_only_description(self, admin_client):
        """No turkum and no recipient, by request — so a blank izoh would leave a row
        that says only "$400 left on the 9th", which records nothing."""
        resp = admin_client.post("/other-expenses/new/",
                                 _rows({"amount": "400"}, note=""))
        assert resp.status_code in (200, 422)
        assert not OtherExpense.objects.exists()

    def test_a_bank_foiz_rides_on_top_like_every_other_chiqim(self, admin_client):
        """Money going OUT: the payee gets the full figure and the till is short by
        the foiz as well. `total_out` is what the kassa really loses."""
        admin_client.post("/other-expenses/new/", _rows(
            {"amount": "1000", "method": "transfer", "fee_percent": "2"}))
        entry = OtherExpense.objects.get()
        assert entry.fee_amount == Decimal("20.00")
        assert entry.total_out == Decimal("1020.00")
        assert dict(kassa_cash_by_currency())[Currency.USD] == Decimal("-1020.00")

    def test_a_foiz_is_ignored_on_a_cash_row(self, admin_client):
        admin_client.post("/other-expenses/new/", _rows(
            {"amount": "1000", "method": "cash", "fee_percent": "2"}))
        entry = OtherExpense.objects.get()
        assert entry.fee_amount == Decimal("0")
        assert entry.total_out == Decimal("1000.00")

    def test_a_som_row_stays_a_som_row(self, admin_client):
        """Per currency, never added across — the rule the whole board follows."""
        admin_client.post("/other-expenses/new/", _rows(
            {"amount": "5000000", "currency": "uzs", "exchange_rate": str(RATE)}))
        split = dict(kassa_cash_by_currency())
        assert split[Currency.UZS] == Decimal("-5000000.00")
        assert split.get(Currency.USD, Decimal("0")) == Decimal("0")


class TestItReachesTheRightFiguresAndNoOthers:
    def test_it_shows_in_the_chiqim_daftar(self, admin_client):
        admin_client.post("/other-expenses/new/",
                          _rows({"amount": "400"}, note="Avgust ijarasi",
                                date="2026-08-09"))
        resp = admin_client.get("/kassa/?davr=all")
        rows = [r for r in resp.context["outflow_page"] if r["kind"] == "other"]
        assert [r["title"] for r in rows] == ["Avgust ijarasi"]
        assert [r["amount"] for r in rows] == [Decimal("400.00")]
        assert "Avgust ijarasi" in resp.content.decode()

    def test_it_lands_in_the_period_total_and_the_oqim(self, admin_client):
        admin_client.post("/other-expenses/new/",
                          _rows({"amount": "400"}, date="2026-08-09"))
        resp = admin_client.get("/kassa/", {"from": "2026-08-01", "to": "2026-08-31"})
        assert resp.context["net_out"] == Decimal("400.00")
        labels = [bar["label"] for bar in resp.context["waterfall"]]
        assert "Boshqa chiqimlar" in labels
        # The Oqim has to close on the same figure the daftar beside it totals.
        assert resp.context["waterfall"][-1]["running"] == Decimal("-400.00")

    def test_it_is_split_by_usul_like_every_other_row(self, admin_client):
        admin_client.post("/other-expenses/new/", _rows(
            {"amount": "300", "method": "cash"},
            {"amount": "100", "method": "card"}))
        by_method = {m["code"]: dict(m["split"]) for m in kassa_cash_by_method()}
        assert by_method["cash"][Currency.USD] == Decimal("-300.00")
        assert by_method["card"][Currency.USD] == Decimal("-100.00")

    def test_it_touches_no_tannarx_and_no_foyda(self, admin_client):
        """It is a cost with no cost object. "Sotuvdan foyda" is a gross margin —
        sotuv less tannarx — and quietly netting the office rent into it would change
        what that figure has always meant on every screen it appears on."""
        lot = _lot(kg="10000", price="1.00")
        before_cost = lot.landed_cost_per_kg
        before = admin_client.get("/?davr=all").context["sales_profit_total"]

        admin_client.post("/other-expenses/new/", _rows({"amount": "400"}))

        lot.refresh_from_db()
        assert lot.landed_cost_per_kg == before_cost
        assert lot.shipment.expenses_total == Decimal("0")
        assert admin_client.get("/?davr=all").context["sales_profit_total"] == before

    def test_it_is_found_by_the_kassa_search(self, admin_client):
        admin_client.post("/other-expenses/new/",
                          _rows({"amount": "400"}, note="Avgust ijarasi"))
        rows = admin_client.get("/kassa/", {"davr": "all", "q": "ijara"}) \
            .context["outflow_page"]
        assert [r["title"] for r in rows] == ["Avgust ijarasi"]


class TestEditingAndDeleting:
    def _one(self, admin_client, **kw):
        admin_client.post("/other-expenses/new/", _rows({"amount": "400"}, **kw))
        return OtherExpense.objects.get()

    def test_the_row_edits_from_the_daftar(self, admin_client):
        entry = self._one(admin_client)
        resp = admin_client.post(f"/other-expenses/{entry.pk}/edit/", {
            "date": "2026-08-10", "note": "Sentabr ijarasi", "currency": "usd",
            "amount": "450", "exchange_rate": str(RATE), "method": "cash",
            "fee_percent": ""})
        assert resp.status_code in (200, 302)
        entry.refresh_from_db()
        assert entry.amount == Decimal("450.00")
        assert entry.note == "Sentabr ijarasi"

    def test_a_correction_carries_across_the_whole_payment(self, admin_client):
        """The izoh and the sana describe the payment, not one movement of it, so a
        split entered in one go cannot end up half-renamed."""
        admin_client.post("/other-expenses/new/", _rows(
            {"amount": "300", "method": "cash"},
            {"amount": "100", "method": "transfer"}, note="Ish haqi"))
        first, second = OtherExpense.objects.order_by("pk")
        admin_client.post(f"/other-expenses/{first.pk}/edit/", {
            "date": "2026-08-11", "note": "Avgust ish haqi", "currency": "usd",
            "amount": "300", "exchange_rate": str(RATE), "method": "cash",
            "fee_percent": ""})
        second.refresh_from_db()
        assert second.note == "Avgust ish haqi"
        assert str(second.date) == "2026-08-11"

    def test_deleting_it_puts_the_money_back(self, admin_client):
        entry = self._one(admin_client)
        assert dict(kassa_cash_by_currency())[Currency.USD] == Decimal("-400.00")
        admin_client.post(f"/other-expenses/{entry.pk}/delete/", {})
        assert not OtherExpense.objects.exists()
        assert dict(kassa_cash_by_currency()).get(Currency.USD, Decimal("0")) \
            == Decimal("0")

    def test_the_daftar_links_to_its_own_edit_url_not_a_yuk_xarajat(self, admin_client):
        """Every stored kind is matched by name in the template; anything unlisted
        falls through to expense_edit and hands ITS pk to a ShipmentExpense URL — a
        different row, or a 404. See the note in crm/kassa.html."""
        entry = self._one(admin_client)
        html = admin_client.get("/kassa/?davr=all").content.decode()
        assert f"/other-expenses/{entry.pk}/edit/" in html
        assert f"/expenses/{entry.pk}/edit/" not in html


def test_a_founder_withdrawal_is_still_its_own_thing(admin_client):
    """Kapital OUT and a boshqa chiqim both leave the till and mean opposite things:
    one is the owner taking their money back, the other is the business spending it.
    Kept apart so the Oqim can say which."""
    customer = Customer.objects.create(name="Alisher", phone="1")
    CustomerPayment.objects.create(customer=customer, date="2026-08-01",
                                   amount=Decimal("1000"), method="cash")
    admin_client.post("/other-expenses/new/", _rows({"amount": "400"}))

    resp = admin_client.get("/kassa/?davr=all")
    labels = [bar["label"] for bar in resp.context["waterfall"]]
    assert "Boshqa chiqimlar" in labels
    assert resp.context["cash_total"] == Decimal("600.00")
