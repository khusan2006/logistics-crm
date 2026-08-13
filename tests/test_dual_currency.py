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
from crm.forms import SaleCreateForm
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


def test_a_sale_never_asks_for_a_kurs_and_inherits_the_last_one(admin_client, db):
    """A sotuv is agreed, owed and settled in one currency, so its kurs moves
    nothing the operator can see — the form stopped asking. The row still gets a
    rate, inherited, because the so'm column has to hold something."""
    lot = _lot()
    assert SaleCreateForm()["exchange_rate"].is_hidden
    CustomerPayment.objects.create(
        customer=_customer("Kurs bergan"), date="2026-07-17", amount=Decimal("100"),
        amount_uzs=Decimal("1300000"), exchange_rate=Decimal("13000"),
        currency=Currency.USD, method="cash")

    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer().pk, "brand": lot.brand, "kg": "1000",
        "currency": "uzs", "price": "13000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale = Sale.objects.get()
    # The typed so'm side is exact; the dollar twin is derived at the inherited kurs.
    assert sale.exchange_rate == Decimal("13000")
    assert sale.price_uzs == Decimal("13000.0000")
    assert sale.price == Decimal("1.0000")


def test_every_fifo_slice_inherits_the_agreed_currency(admin_client, db):
    """A brand-level sotuv splits across lots; each slice must carry the one narx
    that was agreed, in the currency it was agreed in."""
    _lot(kg="600")
    _lot(kg="600")
    resp = admin_client.post("/sales/new/", {
        "customer": _customer().pk,
        "currency": "uzs", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
        **line_data({"brand": "LLDPE", "kg": "1000", "price": "14040"}),
    })
    assert resp.status_code == 302
    slices = Sale.objects.all()
    assert slices.count() == 2
    assert all(s.currency == Currency.UZS and s.price_uzs == Decimal("14040.00")
               for s in slices)


# --- the kelishuv narx -----------------------------------------------------

def test_a_contract_price_can_be_agreed_in_som(admin_client, db):
    """The currency is the kelishuv's, not the row's, and no kurs is typed: the row
    inherits the last one entered (LEGACY_RATE in an empty book)."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    resp = admin_client.post("/contracts/new/", {
        "partner": partner.pk, "currency": "uzs", "created": "2026-07-01",
        "note": "", "planned_trucks": "1",
        **line_data({"brand": "LLDPE", "kg": "1000", "price": "9768"}),
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


def test_the_foiz_carries_its_own_som_value(db):
    """The cut is a slice of the row, so it is worth the same slice of the row's
    stored so'm value — not a reconversion at some other kurs."""
    payment = CustomerPayment.objects.create(
        customer=_customer(), date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12650000"), exchange_rate=Decimal("12650"),
        method="transfer", fee_percent=Decimal("2"))
    assert payment.fee_amount_uzs == Decimal("253000.00")
    assert payment.net_amount_uzs == Decimal("12397000.00")


def test_a_foiz_over_100_is_refused(admin_client, db):
    """A typo'd foiz is not a small error: 200% turns a to'lov into a negative one.
    The arithmetic accepts it happily, so the form has to be the one to say no."""
    customer = _customer()
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1000", "method": "transfer", "fee_percent": "200"},
        customer=customer))
    assert resp.status_code == 200                   # re-rendered, not saved
    assert not CustomerPayment.objects.exists()
    assert "100 dan oshmasligi" in resp.content.decode()


def test_the_list_shows_what_arrived_beside_what_was_sent(admin_client, db):
    """The screen the client reads: 1000 sent, 980 in hand. Showing only the 1000
    is what made the foiz look like it was being ignored — the qarz fell by 980
    while every to'lov row still read 1000."""
    CustomerPayment.objects.create(
        customer=_customer(), date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), method="transfer", fee_percent=Decimal("2"))
    html = admin_client.get("/customer-payments/").content.decode()
    assert f"$1{NBSP}000" in html                     # to'lagan summa
    assert "$980" in html                            # qo'lga tegdi
    assert "bank foizi 2% · −$20" in html


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


# --- a row is drawn in the currency it was booked in ------------------------

def test_a_row_is_drawn_in_the_currency_it_was_typed_in(admin_client, db):
    """No sitewide switch: the row itself says which of its two stored values is
    the one that was agreed, and that is the one printed."""
    customer = _customer()
    CustomerPayment.objects.create(
        customer=customer, date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12500000"), exchange_rate=Decimal("12500"),
        currency=Currency.USD, method="cash")
    CustomerPayment.objects.create(
        customer=customer, date="2026-07-21", amount=Decimal("200"),
        amount_uzs=Decimal("2500000"), exchange_rate=Decimal("12500"),
        currency=Currency.UZS, method="cash")

    html = admin_client.get("/customer-payments/").content.decode()
    # Both on one screen, each leading with its own currency. Qo'lga tegdi is the
    # headline (<strong>), green because it is money that actually reached us.
    assert f'<strong class="money-in">$1{NBSP}000</strong>' in html
    assert f'<strong class="money-in">2{NBSP}500{NBSP}000 so&#x27;m</strong>' in html
    assert f'<strong class="money-in">$200</strong>' not in html


def test_a_row_is_never_reconverted_beside_itself(admin_client, db):
    """A to'lov is printed in the currency it ARRIVED in and in no other. The twin
    that used to sit under it was a conversion nobody asked for — the same rule the
    rest of the app already follows, where only a TOTAL carries two figures."""
    CustomerPayment.objects.create(
        customer=_customer(), date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12500000"), exchange_rate=Decimal("12500"),
        currency=Currency.USD, method="cash")
    html = admin_client.get("/customer-payments/").content.decode()
    assert f"$1{NBSP}000" in html
    assert f"12{NBSP}500{NBSP}000 so&#x27;m" not in html


def test_a_kassa_total_never_restates_a_row_in_the_other_currency(admin_client, db):
    """A total on the kassa spans rows of both currencies, and it prints ONE LINE PER
    CURRENCY rather than picking a side or blending them.

    It used to publish a dollar figure that counted the so'm rows too, each restated
    at its own entry-day kurs, with the so'm total beneath it. That dollar figure is
    money nobody ever handed over — so a lone so'm to'lov now shows up in so'm and
    nowhere else, and no figure on this page carries a converted twin."""
    CustomerPayment.objects.create(
        customer=_customer(), date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12500000"), exchange_rate=Decimal("12500"),
        currency=Currency.UZS, method="cash")
    html = admin_client.get("/kassa/").content.decode()
    assert f"12{NBSP}500{NBSP}000 so&#x27;m" in html   # the heap it arrived in
    assert f"$1{NBSP}000" not in html                  # never its dollar restatement
    assert 'class="money-alt"' not in html


def test_the_display_switch_is_gone(admin_client, db):
    assert admin_client.post("/valyuta/", {"currency": "uzs"}).status_code == 404
    assert "currency-switch" not in admin_client.get("/kassa/").content.decode()


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


# --- per-kg rates follow the same rule -------------------------------------

def test_a_per_kg_narx_follows_its_own_row(admin_client, db):
    """Tannarx and sotuv narx are rendered as "1.17 $/kg", never as a bare sum, so
    they need their own tag — but the rule is the same: the sotuv's own currency."""
    lot = _lot()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer().pk, "brand": lot.brand, "kg": "1000",
        "currency": "usd", "price": "1.17", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert "1.17 $/kg" in admin_client.get("/sales/").content.decode()

    lot = _lot()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer("Zilola Mebel").pk, "brand": lot.brand, "kg": "1000",
        "currency": "uzs", "price": "14040", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    html = admin_client.get("/sales/").content.decode()
    assert f"14{NBSP}040 so&#x27;m/kg" in html
    assert "1.17 $/kg" in html          # the dollar sotuv is untouched by it


def test_a_narx_is_not_padded_out_to_four_decimals(db):
    from crm.templatetags.crm_extras import _trim
    assert _trim(Decimal("0.8140")) == "0.814"
    assert _trim(Decimal("1.0000")) == "1"
    assert _trim(Decimal("1.1700")) == "1.17"


def test_a_progress_caption_writes_the_som_unit_once(db):
    """Chiziq yonidagi "shuncha / shundan" — so'm so'zi oxirida bir marta, aks holda
    ikkita to'qqiz xonali raqam chiziqni siqib qo'yadi. Dollarda esa $ ikkalasida
    ham qoladi: u bir belgi va raqamning o'zi bilan o'qiladi."""
    from crm.templatetags.crm_extras import money_progress_in
    assert money_progress_in(Decimal("312500000"), Decimal("625000000"), "uzs") == (
        f"312{NBSP}500{NBSP}000 / 625{NBSP}000{NBSP}000 so'm")
    assert money_progress_in(Decimal("60000"), Decimal("296400"), "usd") == (
        f"$60{NBSP}000 / $296{NBSP}400")
    assert money_progress_in(None, Decimal("24000"), "usd") == f"$0 / $24{NBSP}000"
