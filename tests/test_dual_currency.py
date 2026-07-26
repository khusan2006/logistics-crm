"""Sitewide dual currency: every money row carries a dollar value, a so'm value and
the kurs linking them, and perechisleniya carries a bank foiz.

The rules these lock down, in the operator's words: enter in either currency and the
other is derived; the typed side is kept exact; a foiz on money going OUT rides on
top of what the hamkor receives, while a foiz on money coming IN is the mijoz's
loss — 1000 sent at 2% pays off 980 of their qarz and puts 980 in the kassa.
"""
from decimal import Decimal

import pytest

from conftest import line_data, payment_rows
from crm.templatetags.crm_extras import NBSP
from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, Partner, Sale, Shipment,
    ShipmentExpense, ShipmentLine, ShipmentStatus, SupplierPayment, convert_pair,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _contract(price="1.00", kg="10000"):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal(kg), price=Decimal(price))
    return contract


def _lot(kg="10000"):
    contract = _contract(kg=kg)
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        eta="2026-07-15", arrived="2026-07-16")
    return ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg))


# --- the conversion itself -------------------------------------------------

def test_the_typed_side_is_kept_exact_in_both_directions():
    """Whichever currency was typed is stored verbatim. Deriving it back from the
    rounded counter-value would drift a tiyin on every edit."""
    usd, uzs = convert_pair(Decimal("1265000"), Currency.UZS, Decimal("12650"))
    assert usd == Decimal("100.00") and uzs == Decimal("1265000.00")

    usd, uzs = convert_pair(Decimal("100"), Currency.USD, Decimal("12650"))
    assert usd == Decimal("100.00") and uzs == Decimal("1265000.00")


def test_a_price_keeps_four_decimals_where_a_sum_keeps_two():
    """A per-kg narx rounded to cents would move a 24-tonne lot by dollars."""
    usd, _ = convert_pair(Decimal("14040"), Currency.UZS, Decimal("12000"), "0.0001")
    assert usd == Decimal("1.1700")


def test_converting_without_a_kurs_is_refused():
    with pytest.raises(ValueError):
        convert_pair(Decimal("100"), Currency.USD, Decimal("0"))


# --- sotuv, the modal that started this ------------------------------------

def test_a_sale_can_be_entered_in_som(admin_client, db):
    lot = _lot()
    customer = _customer()
    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "1000",
        "currency": "uzs", "price": "14040", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale = Sale.objects.get()
    assert sale.currency == Currency.UZS
    assert sale.price_uzs == Decimal("14040.00")     # exactly what was agreed
    assert sale.price == Decimal("1.1700")           # derived at 12,000
    assert sale.total == Decimal("1170.00")
    assert sale.total_uzs == Decimal("14040000.00")


def test_a_dollar_sale_still_records_its_som_value(admin_client, db):
    lot = _lot()
    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer().pk, "brand": lot.brand, "kg": "1000",
        "currency": "usd", "price": "1.17", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale = Sale.objects.get()
    assert sale.price == Decimal("1.1700") and sale.price_uzs == Decimal("14040.00")


def test_a_sale_without_a_kurs_is_rejected(admin_client, db):
    lot = _lot()
    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer().pk, "brand": lot.brand, "kg": "1000",
        "currency": "uzs", "price": "14040", "exchange_rate": "",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 200
    assert not Sale.objects.exists()


def test_every_fifo_slice_inherits_the_agreed_currency(admin_client, db):
    """A brand-level sotuv splits across lots; each slice must carry the one narx
    that was agreed, in the currency it was agreed in."""
    _lot(kg="600")
    _lot(kg="600")
    resp = admin_client.post("/sales/new/", {
        "customer": _customer().pk, "brand": "LLDPE", "kg": "1000",
        "currency": "uzs", "price": "14040", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    slices = Sale.objects.all()
    assert slices.count() == 2
    assert all(s.currency == Currency.UZS and s.price_uzs == Decimal("14040.00")
               for s in slices)


# --- the kelishuv narx -----------------------------------------------------

def test_a_contract_price_can_be_agreed_in_som(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    resp = admin_client.post("/contracts/new/", {
        "partner": partner.pk, "created": "2026-07-01", "note": "", "planned_trucks": "1",
        **line_data({"brand": "LLDPE", "kg": "1000", "currency": "uzs",
                     "price": "9768", "exchange_rate": "12000"}),
    })
    assert resp.status_code == 302
    line = ContractLine.objects.get()
    assert line.currency == Currency.UZS
    assert line.price_uzs == Decimal("9768.00") and line.price == Decimal("0.8140")


# --- perechisleniya foizi --------------------------------------------------

def test_an_incoming_foiz_is_the_mijozs_loss(admin_client, db):
    """1000 sent by perechisleniya at 2% → we received 980, and the mijoz's qarz
    falls by 980, not 1000. The 20 never reached us, so it cannot pay anything."""
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1000", "method": "transfer", "fee_percent": "2"},
        customer=_customer()))
    assert resp.status_code == 302
    payment = CustomerPayment.objects.get()
    assert payment.amount == Decimal("1000.00")      # what they sent
    assert payment.fee_amount == Decimal("20.00")
    assert payment.net_amount == Decimal("980.00")   # what arrived


def test_an_incoming_foiz_only_pays_down_what_arrived(admin_client, db):
    lot = _lot()
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "1000",
        "currency": "usd", "price": "1.00", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1000", "method": "transfer", "fee_percent": "2"}, customer=customer))
    sale = Sale.objects.get()
    assert sale.paid == Decimal("980.00")
    assert sale.remaining == Decimal("20.00")        # still owes the fee's worth


def test_an_outgoing_foiz_rides_on_top(admin_client, db):
    """The hamkor is credited in full; the bank's cut costs the kassa extra."""
    contract = _contract()
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-02", "currency": "usd",
        "amount": "1000", "exchange_rate": "12000", "commission_percent": "",
        "method": "transfer", "fee_percent": "2", "note": "",
    })
    assert resp.status_code == 302
    payment = SupplierPayment.objects.get()
    assert payment.amount == Decimal("1000.00")      # what the hamkor receives
    assert payment.total_out == Decimal("1020.00")   # what the kassa loses
    contract.refresh_from_db()
    assert contract.paid_total == Decimal("1000.00")  # the foiz settles no qarz


def test_the_foiz_is_ignored_on_naqd(db):
    """Cash has no bank behind it. Stored rather than trusted, so a method changed
    after the fact can never quietly start charging."""
    payment = CustomerPayment.objects.create(
        customer=_customer(), date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), method="cash", fee_percent=Decimal("2"))
    assert payment.fee_amount == Decimal("0")
    assert payment.net_amount == Decimal("1000.00")


def test_both_foizlar_can_ride_the_same_payment(db):
    """The vositachi's cut and the bank's foiz are different money to different
    people, so they stack rather than replace one another."""
    payment = SupplierPayment.objects.create(
        contract=_contract(), date="2026-07-02", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), method="transfer",
        commission_percent=Decimal("3"), fee_percent=Decimal("2"))
    assert payment.commission_amount == Decimal("30.00")
    assert payment.fee_amount == Decimal("20.00")
    assert payment.total_out == Decimal("1050.00")


# --- the kassa ------------------------------------------------------------

def test_the_kassa_counts_net_in_and_gross_out(admin_client, db):
    customer = _customer()
    contract = _contract()
    CustomerPayment.objects.create(
        customer=customer, date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), method="transfer", fee_percent=Decimal("2"))
    SupplierPayment.objects.create(
        contract=contract, date="2026-07-21", amount=Decimal("500"),
        amount_uzs=Decimal("6000000"), method="transfer", fee_percent=Decimal("2"))
    ShipmentExpense.objects.create(
        shipment=Shipment.objects.create(contract=contract,
                                         status=ShipmentStatus.objects.first()),
        date="2026-07-22", category="road", amount=Decimal("100"),
        amount_uzs=Decimal("1200000"), method="cash")

    resp = admin_client.get("/kassa/")
    # in 980 (1000 − 2%) − out 510 (500 + 2%) − out 100 (naqd, no foiz) = 370
    assert resp.context["cash_total"] == Decimal("370.00")


# --- the dollar / so'm display toggle --------------------------------------

def test_the_app_shows_dollars_until_told_otherwise(admin_client, db):
    resp = admin_client.get("/kassa/")
    assert resp.context["display_currency"] == "usd"
    assert resp.context["showing_som"] is False


def test_switching_to_som_redraws_the_figures(admin_client, db):
    """Same rows, different column — the toggle picks which stored value is read."""
    customer = _customer()
    CustomerPayment.objects.create(
        customer=customer, date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12500000"), exchange_rate=Decimal("12500"), method="cash")

    html = admin_client.get("/kassa/").content.decode()
    assert "$1,000.00" in html

    resp = admin_client.post("/valyuta/", {"currency": "uzs", "next": "/kassa/"})
    assert resp.status_code == 302
    html = admin_client.get("/kassa/").content.decode()
    # non-breaking spaces group the figure, and the apostrophe is HTML-escaped
    assert f"12{NBSP}500{NBSP}000 so&#x27;m" in html
    assert "$1,000.00" not in html


def test_the_choice_survives_the_next_page(admin_client, db):
    admin_client.post("/valyuta/", {"currency": "uzs", "next": "/kassa/"})
    for url in ["/kassa/", "/sales/", "/customer-payments/", "/"]:
        assert admin_client.get(url).context["showing_som"] is True


def test_a_som_total_is_the_sum_of_entry_time_values(admin_client, db):
    """Two payments booked at different kursi. The so'm total is what was actually
    banked — 12,500,000 + 13,000,000 — not the $2,000 dollar total re-rated at
    either one of them."""
    customer = _customer()
    for rate, uzs in [("12500", "12500000"), ("13000", "13000000")]:
        CustomerPayment.objects.create(
            customer=customer, date="2026-07-20", amount=Decimal("1000"),
            amount_uzs=Decimal(uzs), exchange_rate=Decimal(rate), method="cash")
    resp = admin_client.get("/kassa/")
    assert resp.context["cash_total"] == Decimal("2000.00")
    assert resp.context["cash_total_uzs"] == Decimal("25500000.00")


def test_an_unknown_currency_is_ignored(admin_client, db):
    admin_client.post("/valyuta/", {"currency": "eur", "next": "/kassa/"})
    assert admin_client.get("/kassa/").context["display_currency"] == "usd"


def test_the_switch_cannot_be_used_as_an_open_redirect(admin_client, db):
    resp = admin_client.post("/valyuta/", {"currency": "uzs",
                                           "next": "https://evil.example.com/"})
    assert resp.status_code == 302
    assert resp["Location"] == "/"


# --- rows built in code, not through a form --------------------------------

def test_a_row_saved_without_a_som_value_still_gets_one(db):
    """The importer and the seeders build rows directly. A row with a dollar value
    and a blank so'm one would read as 0 so'm on every so'm screen — which looks
    like a real figure rather than a missing one."""
    payment = SupplierPayment.objects.create(
        contract=_contract(), date="2026-07-02", amount=Decimal("250"),
        exchange_rate=Decimal("12000"), method="cash")
    assert payment.amount_uzs == Decimal("3000000.00")


def test_a_stored_som_value_is_never_recomputed(db):
    """The backstop fills gaps; it must not overwrite the figure the operator
    actually typed, even when it disagrees with amount x rate."""
    payment = SupplierPayment.objects.create(
        contract=_contract(), date="2026-07-02", amount=Decimal("250"),
        amount_uzs=Decimal("2999999"), exchange_rate=Decimal("12000"), method="cash")
    payment.refresh_from_db()
    assert payment.amount_uzs == Decimal("2999999.00")


# --- per-kg rates follow the toggle too ------------------------------------

def test_a_per_kg_narx_switches_with_everything_else(admin_client, db):
    """Tannarx and sotuv narx are rendered as "1.17 $/kg", never as a bare sum, so
    they need their own tag — but they must still follow the same switch."""
    lot = _lot()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer().pk, "brand": lot.brand, "kg": "1000",
        "currency": "usd", "price": "1.17", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    html = admin_client.get("/sales/").content.decode()
    assert "1.17 $/kg" in html

    admin_client.post("/valyuta/", {"currency": "uzs", "next": "/sales/"})
    html = admin_client.get("/sales/").content.decode()
    assert f"14{NBSP}040 so&#x27;m/kg" in html
    assert "$/kg" not in html


def test_a_narx_is_not_padded_out_to_four_decimals(db):
    from crm.templatetags.crm_extras import _trim
    assert _trim(Decimal("0.8140")) == "0.814"
    assert _trim(Decimal("1.0000")) == "1"
    assert _trim(Decimal("1.1700")) == "1.17"
