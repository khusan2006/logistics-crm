"""Audit pass — Qarzlar (debt list, per-customer page, to'lov muddati).

Diagnosis only: nothing outside tests/audit/ is touched. Tests that fail are
marked xfail with the defect they document, so the file stays runnable.

Run:
    TEST_DB_SUFFIX=_debts .venv/bin/python -m pytest tests/audit/test_debts_audit.py -q
"""
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from conftest import payment_rows
from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, Partner, Return,
    Sale, Shipment, ShipmentLine, ShipmentStatus,
)
from crm.templatetags.crm_extras import NBSP


# ── helpers ──────────────────────────────────────────────────────────────────

def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="100000", brand="LLDPE", contract_price="1.00"):
    """One arrived lot, big enough that every sale in a test fits inside it."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand, kg=Decimal(kg),
                                price=Decimal(contract_price))
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    return ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg))


def _sale_payload(lot, customer, kg="1000", price="1.60", currency="usd",
                  rate="12000", date="2026-07-17", deadline="", note=""):
    return {"lot": lot.pk, "customer": customer.pk, "kg": kg, "currency": currency,
            "price": price, "exchange_rate": rate, "date": date,
            "debt_deadline": deadline, "note": note}


def _post_sale(client, lot, customer, **kw):
    """Create a sotuv through the real one-lot view (/sales/new/?lot=)."""
    resp = client.post("/sales/new/", _sale_payload(lot, customer, **kw))
    assert resp.status_code in (200, 302), resp.status_code
    return Sale.objects.filter(customer=customer).order_by("-id").first()


def _edit_payload(sale, **overrides):
    """What SaleForm renders for an existing sotuv — the values a human would
    re-submit by opening the edit modal and pressing Saqlash."""
    data = {"customer": sale.customer_id, "line": sale.line_id,
            "kg": str(sale.kg), "currency": sale.currency,
            "price": str(sale.price), "exchange_rate": str(sale.exchange_rate),
            "date": str(sale.date), "debt_deadline": str(sale.debt_deadline or ""),
            "note": sale.note}
    data.update(overrides)
    return data


def _input_value(html, name):
    """The value= of the first <input name="..."> in a rendered form."""
    tag = re.search(r'<input[^>]*\bname="%s"[^>]*>' % re.escape(name), html)
    assert tag, f"no <input name={name}> in the rendered form"
    value = re.search(r'\bvalue="([^"]*)"', tag.group(0))
    return value.group(1) if value else ""


def _selected_option(html, name):
    """The selected <option> of a <select name="...">."""
    block = re.search(r'<select[^>]*\bname="%s".*?</select>' % re.escape(name),
                      html, re.S)
    assert block, f"no <select name={name}> in the rendered form"
    picked = re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', block.group(0))
    return picked.group(1) if picked else ""


def _money(sale):
    """Every money figure a qarz screen can draw for one sotuv."""
    sale.refresh_from_db()
    return {"currency": sale.currency, "rate": sale.exchange_rate,
            "price": sale.price, "price_uzs": sale.price_uzs,
            "total": sale.total, "total_uzs": sale.total_uzs,
            "net": sale.net_total, "net_uzs": sale.net_total_uzs,
            "paid": sale.paid, "paid_uzs": sale.paid_uzs,
            "remaining": sale.remaining, "remaining_uzs": sale.remaining_uzs}


# ── (a) ROUND-TRIP ───────────────────────────────────────────────────────────

def test_roundtrip_sale_typed_in_uzs_keeps_the_typed_som_bit_exact(admin_client, db):
    """12 500 so'm/kg at 12 000 → the so'm side is stored untouched and the dollar
    side is the only thing derived (convert_pair's contract)."""
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000",
                      currency="uzs", price="12500", rate="12000")

    assert sale.currency == Currency.UZS
    assert sale.price_uzs == Decimal("12500.00")            # typed, bit-exact
    assert sale.price == Decimal("1.0417")                  # 12500/12000 → 4dp HALF_UP
    assert sale.total_uzs == Decimal("12500000.00")
    assert sale.net_total_uzs == Decimal("12500000.00")
    assert customer.balance_uzs == Decimal("12500000.00")


def test_roundtrip_sale_typed_in_usd_keeps_the_typed_dollar_bit_exact(admin_client, db):
    """The mirror: the dollar side survives at its full 4dp and the so'm side is
    derived once at the row's kurs."""
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000",
                      currency="usd", price="1.0417", rate="12000")

    assert sale.currency == Currency.USD
    assert sale.price == Decimal("1.0417")                  # typed, bit-exact
    assert sale.price_uzs == Decimal("12500.40")            # 1.0417*12000
    assert sale.total == Decimal("1041.70")
    assert customer.balance == Decimal("1041.70")


def test_roundtrip_payment_typed_in_uzs_lands_on_the_som_side(admin_client, db):
    """A to'lov typed in so'm settles the qarz with its own so'm figure exact."""
    customer, lot = _customer(), _lot()
    _post_sale(admin_client, lot, customer, kg="1000", currency="uzs",
               price="12500", rate="12000")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "6000000", "exchange_rate": "12000"},
        customer=customer, date="2026-07-18"))

    payment = CustomerPayment.objects.get(customer=customer)
    assert payment.currency == Currency.UZS
    assert payment.amount_uzs == Decimal("6000000.00")      # typed, bit-exact
    assert payment.amount == Decimal("500.00")
    assert customer.balance_uzs == Decimal("6500000.00")


# ── (c) CURRENCY STICKINESS ──────────────────────────────────────────────────

def test_uzs_sale_saves_with_currency_uzs_and_not_a_usd_reading(admin_client, db):
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", currency="uzs",
                      price="12500", rate="12000")

    assert sale.currency == "uzs"
    assert sale.price_uzs == Decimal("12500.00")
    # the typed 12 500 was NOT read as dollars
    assert sale.price != Decimal("12500.0000")
    html = admin_client.get(f"/debts/{customer.pk}/").content.decode()
    assert f"12{NBSP}500{NBSP}000 so&#x27;m" in html   # the Jami cell, in so'm


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_uzs_sale_edit_form_reopens_bound_to_the_som_figure(admin_client, db):
    """Re-opening the edit modal must show the operator the number they typed.

    ReturnForm does exactly this (`self.initial["price"] = self.sale.price_uzs`
    when the sale is in so'm); SaleForm has no such branch, so the narx box shows
    1.0417 next to a Valyuta picker that reads So'm."""
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", currency="uzs",
                      price="12500", rate="12000")

    html = admin_client.get(f"/sales/{sale.pk}/edit/").content.decode()
    assert _selected_option(html, "currency") == "uzs"      # this half is fine
    assert _input_value(html, "price") == "12500.00"        # this half is not


# ── (b) IDEMPOTENCE / NO DRIFT ───────────────────────────────────────────────

def test_resaving_a_usd_sale_unchanged_twice_moves_nothing(admin_client, db):
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", price="1.60",
                      currency="usd", rate="12000")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "600"}, customer=customer, date="2026-07-18"))
    before = _money(sale)
    before_balance = (customer.balance, customer.balance_uzs)

    for _ in range(2):
        resp = admin_client.post(f"/sales/{sale.pk}/edit/", _edit_payload(sale))
        assert resp.status_code in (200, 302)
        sale.refresh_from_db()
        assert _money(sale) == before
        assert (customer.balance, customer.balance_uzs) == before_balance


def test_resaving_a_usd_sale_with_only_the_izoh_changed_moves_no_money(admin_client, db):
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", price="1.60",
                      currency="usd", rate="12000")
    before = _money(sale)

    for note in ("birinchi izoh", "ikkinchi izoh"):
        admin_client.post(f"/sales/{sale.pk}/edit/", _edit_payload(sale, note=note))
        sale.refresh_from_db()
        assert sale.note == note
        assert _money(sale) == before


def test_resaving_a_uzs_sale_with_the_typed_som_figure_moves_nothing(admin_client, db):
    """The stable path: hand the form back the figure the operator actually typed
    (the so'm one) and the pair is reproduced exactly, twice over."""
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", currency="uzs",
                      price="12500", rate="12000")
    before = _money(sale)

    for _ in range(2):
        admin_client.post(f"/sales/{sale.pk}/edit/",
                          _edit_payload(sale, price=str(sale.price_uzs)))
        sale.refresh_from_db()
        assert _money(sale) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_uzs_sale_as_the_form_renders_it_moves_nothing(admin_client, db):
    """The operator's real path: open the sotuv, press Saqlash, change nothing."""
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", currency="uzs",
                      price="12500", rate="12000")
    before = _money(sale)

    html = admin_client.get(f"/sales/{sale.pk}/edit/").content.decode()
    rendered = {"customer": str(customer.pk), "line": str(sale.line_id),
                "kg": _input_value(html, "kg"),
                "currency": _selected_option(html, "currency"),
                "price": _input_value(html, "price"),
                "exchange_rate": _input_value(html, "exchange_rate"),
                "date": _input_value(html, "date"),
                "debt_deadline": _input_value(html, "debt_deadline"), "note": ""}
    for _ in range(2):
        admin_client.post(f"/sales/{sale.pk}/edit/", rendered)
        sale.refresh_from_db()
        assert _money(sale) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_uzs_payment_as_the_form_renders_it_moves_nothing(admin_client, db):
    customer, lot = _customer(), _lot()
    _post_sale(admin_client, lot, customer, kg="1000", currency="uzs",
               price="12500", rate="12000")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "6000000", "exchange_rate": "12000"},
        customer=customer, date="2026-07-18"))
    payment = CustomerPayment.objects.get(customer=customer)
    before = (payment.amount, payment.amount_uzs, customer.balance_uzs)

    html = admin_client.get(f"/customer-payments/{payment.pk}/edit/").content.decode()
    rendered = {"customer": str(customer.pk), "date": _input_value(html, "date"),
                "currency": _selected_option(html, "currency"),
                "amount": _input_value(html, "amount"),
                "exchange_rate": _input_value(html, "exchange_rate"),
                "method": _selected_option(html, "method"),
                "fee_percent": _input_value(html, "fee_percent"), "note": ""}
    for _ in range(2):
        admin_client.post(f"/customer-payments/{payment.pk}/edit/", rendered)
        payment.refresh_from_db()
        assert (payment.amount, payment.amount_uzs, customer.balance_uzs) == before


# ── (d) AGGREGATE CONSISTENCY ────────────────────────────────────────────────

def test_customer_page_dollar_header_equals_the_sum_of_its_rows(admin_client, db):
    """Mixed currencies AND mixed kursi: the qarz in the header is the sum of the
    Qoldiq column below it."""
    customer, lot = _customer(), _lot()
    _post_sale(admin_client, lot, customer, kg="1000", currency="usd",
               price="1.60", rate="12000", date="2026-07-10")
    _post_sale(admin_client, lot, customer, kg="500", currency="uzs",
               price="21000", rate="12600", date="2026-07-12")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "700", "exchange_rate": "12300"},
        {"currency": "uzs", "amount": "3000000", "exchange_rate": "12500"},
        customer=customer, date="2026-07-18"))

    resp = admin_client.get(f"/debts/{customer.pk}/")
    rows = resp.context["sales"]
    assert len(rows) >= 1
    assert sum((s.remaining for s in rows), Decimal("0")) == customer.balance


@pytest.mark.xfail(reason="BUG: Sale.paid_uzs re-rates the allocations at the "
                          "SOTUV's kurs (in_som) instead of using each to'lov's own "
                          "stored so'm value, so the so'm header and the so'm Qoldiq "
                          "column on the same page disagree", strict=False)
def test_customer_page_som_header_equals_the_sum_of_its_rows(admin_client, db):
    """Sotuv booked at 12 000, to'lov received at 13 000.

    The mijoz owed 12 000 000 so'm and handed over 6 500 000 so'm, so 5 500 000
    is left — which is what `balance_uzs` says. The Qoldiq column re-rates the
    500$ allocation at the sotuv's 12 000 and prints 6 000 000 instead."""
    customer, lot = _customer(), _lot()
    _post_sale(admin_client, lot, customer, kg="1000", currency="usd",
               price="1.00", rate="12000", date="2026-07-10")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "6500000", "exchange_rate": "13000"},
        customer=customer, date="2026-07-18"))

    rows = admin_client.get(f"/debts/{customer.pk}/").context["sales"]
    assert customer.balance_uzs == Decimal("5500000.00")           # what was owed
    assert sum((s.remaining_uzs for s in rows), Decimal("0")) == customer.balance_uzs


@pytest.mark.xfail(reason="BUG (root cause): Sale.paid_uzs = in_som(paid) re-rates "
                          "the allocations at the sotuv's kurs, while "
                          "PaymentAllocation.amount_uzs — the value sale_detail.html "
                          "actually prints for the very same rows — carries the "
                          "to'lov's kurs. Jami − Σ taqsimot ≠ Qoldiq, in so'm",
                    strict=False)
def test_sale_som_arithmetic_closes_against_its_own_allocation_rows(admin_client, db):
    """The narrowest statement of the defect, with no page in the way."""
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", currency="usd",
                      price="1.00", rate="12000", date="2026-07-10")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "6500000", "exchange_rate": "13000"},
        customer=customer, date="2026-07-18"))
    sale.refresh_from_db()

    allocated_uzs = sum((a.amount_uzs for a in sale.allocations.all()), Decimal("0"))
    assert allocated_uzs == Decimal("6500000.00")       # the so'm that really arrived
    assert sale.paid_uzs == allocated_uzs
    assert sale.net_total_uzs - allocated_uzs == sale.remaining_uzs


def test_debt_list_row_and_customer_page_show_the_same_qarz(admin_client, db):
    customer, lot = _customer(name="Bir Xil"), _lot()
    _post_sale(admin_client, lot, customer, kg="1000", currency="uzs",
               price="12500", rate="12000", date="2026-07-10")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "2500000", "exchange_rate": "12000"},
        customer=customer, date="2026-07-18"))

    listed = admin_client.get("/debts/").context["page"].object_list
    row = next(r for r in listed if r["customer"].pk == customer.pk)
    page_customer = admin_client.get(f"/debts/{customer.pk}/").context["customer"]
    assert row["customer"].balance == page_customer.balance
    assert row["customer"].balance_uzs == page_customer.balance_uzs


@pytest.mark.xfail(reason="BUG: the Jami column prints Sale.total (gross) while "
                          "Qoldiq prints net_total - paid, so after a qaytarish the "
                          "three money columns on the qarz page no longer add up",
                    strict=False)
def test_customer_page_columns_add_up_after_a_return(admin_client, db):
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", price="1.60",
                      currency="usd", rate="12000")
    Return.objects.create(sale=sale, kg=Decimal("250"), price=Decimal("1.6000"),
                          price_uzs=Decimal("19200.00"), currency="usd",
                          exchange_rate=Decimal("12000"), date="2026-07-20")

    row = admin_client.get(f"/debts/{customer.pk}/").context["sales"][0]
    assert row.total - row.paid == row.remaining


# ── deadlines ────────────────────────────────────────────────────────────────

def test_sale_with_no_muddat_is_chased_from_its_sale_date(admin_client, db):
    """Documented intent: blank muddat means "naqd, pay now"."""
    customer, lot = _customer(name="Muddatsiz"), _lot()
    today = timezone.localdate()
    sale = _post_sale(admin_client, lot, customer, kg="100", price="1.00",
                      date=str(today - timedelta(days=3)), deadline="")

    assert sale.debt_deadline == today - timedelta(days=3)
    assert sale.is_due and sale.is_overdue
    row = next(r for r in admin_client.get("/debts/").context["page"].object_list
               if r["customer"].pk == customer.pk)
    assert row["earliest_due"] == sale.debt_deadline
    assert row["overdue_count"] == 1


def test_clearing_the_muddat_on_an_edit_falls_back_to_the_sale_date(admin_client, db):
    """Characterisation of Sale.save(): a muddat wiped on an edit does not become
    "never due", it snaps back to the sana. Deliberate, per the docstring — but it
    IS a figure that moves without the operator typing it, so it is pinned here."""
    customer, lot = _customer(), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="100", price="1.00",
                      date="2026-07-10", deadline="2026-09-01")
    assert sale.debt_deadline.isoformat() == "2026-09-01"

    admin_client.post(f"/sales/{sale.pk}/edit/", _edit_payload(sale, debt_deadline=""))
    sale.refresh_from_db()
    assert sale.debt_deadline.isoformat() == "2026-07-10"


def test_debt_list_orders_due_first_then_biggest_and_is_stable(admin_client, db):
    """Due-first, oldest muddat at the top; not-yet-due behind, biggest first. The
    same request twice must produce the same order (no unstable tie-break)."""
    today = timezone.localdate()
    lot = _lot()
    old_due = _customer(name="ZZZ Eski")          # oldest muddat, smallest qarz
    due_today = _customer(name="AAA Bugun")
    later_big = _customer(name="MMM Katta Keyin")
    later_small = _customer(name="NNN Kichik Keyin")

    _post_sale(admin_client, lot, old_due, kg="100", price="1.00",
               date=str(today - timedelta(days=9)), deadline=str(today - timedelta(days=9)))
    _post_sale(admin_client, lot, due_today, kg="300", price="1.00",
               date=str(today), deadline=str(today))
    _post_sale(admin_client, lot, later_big, kg="9000", price="1.00",
               date=str(today), deadline=str(today + timedelta(days=30)))
    _post_sale(admin_client, lot, later_small, kg="200", price="1.00",
               date=str(today), deadline=str(today + timedelta(days=30)))

    def order():
        return [r["customer"].name for r in
                admin_client.get("/debts/").context["page"].object_list]

    assert order() == ["ZZZ Eski", "AAA Bugun", "MMM Katta Keyin", "NNN Kichik Keyin"]
    assert order() == order()


# ── boundaries, deletion, edge balances ──────────────────────────────────────

@pytest.mark.parametrize("bad", [{"exchange_rate": "0"}, {"price": "0"},
                                 {"price": "-5"}, {"exchange_rate": ""}])
def test_sale_form_refuses_money_it_cannot_convert(admin_client, db, bad):
    """rate=0, zero and negative narx, and a blank kurs all have to be rejected —
    convert_pair raises on them, and a row that slipped through would carry only
    one of its two values into every qarz total."""
    customer, lot = _customer(), _lot()
    payload = _sale_payload(lot, customer, kg="100", price="1.00", rate="12000")
    payload.update(bad)
    admin_client.post("/sales/new/", payload)
    assert not Sale.objects.filter(customer=customer).exists()


def test_extreme_kursi_round_trip_without_drifting(admin_client, db):
    """A kurs of 1 and a kurs of 9 999 999.99 both have to survive the pair."""
    lot = _lot()
    tiny, huge = _customer(name="Kichik Kurs"), _customer(name="Katta Kurs")

    tiny_sale = _post_sale(admin_client, lot, tiny, kg="100", price="1.5000",
                           currency="usd", rate="1")
    assert (tiny_sale.price, tiny_sale.price_uzs) == (Decimal("1.5000"), Decimal("1.50"))
    assert tiny.balance_uzs == Decimal("150.00")

    huge_sale = _post_sale(admin_client, lot, huge, kg="100", price="99999999.99",
                           currency="uzs", rate="9999999.99")
    assert huge_sale.price_uzs == Decimal("99999999.99")   # typed side untouched
    assert huge_sale.price == Decimal("10.0000")
    assert huge.balance_uzs == Decimal("9999999999.00")


@pytest.mark.xfail(reason="BUG: debt_list/debt_customer gate on the USD balance "
                          "alone, so a mijoz who settled a so'm sotuv to the tiyin "
                          "keeps a rounding-residue qarz and never leaves Qarzlar",
                    strict=False)
def test_a_som_debt_settled_in_full_leaves_the_qarzlar_list(admin_client, db):
    """12 500 so'm/kg × 1000 kg = 12 500 000 so'm, paid in full in so'm at the same
    kurs. balance_uzs is 0; balance is $0.03, because the derived USD price was
    rounded up at 4dp and the derived USD payment down at 2dp."""
    customer, lot = _customer(name="To'la To'lagan"), _lot()
    _post_sale(admin_client, lot, customer, kg="1000", currency="uzs",
               price="12500", rate="12000")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "12500000", "exchange_rate": "12000"},
        customer=customer, date="2026-07-18"))

    assert customer.balance_uzs == Decimal("0.00")         # settled to the tiyin
    listed = [r["customer"].pk for r in
              admin_client.get("/debts/").context["page"].object_list]
    assert customer.pk not in listed


def test_overpaid_customer_is_an_avans_not_a_qarz(admin_client, db):
    customer, lot = _customer(name="Ortiqcha To'lagan"), _lot()
    _post_sale(admin_client, lot, customer, kg="1000", price="1.00",
               currency="usd", rate="12000")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1500"}, customer=customer, date="2026-07-18"))

    assert customer.balance == Decimal("-500.00")
    assert customer.balance_uzs == Decimal("-6000000.00")
    listed = [r["customer"].pk for r in
              admin_client.get("/debts/").context["page"].object_list]
    assert customer.pk not in listed
    html = admin_client.get(f"/debts/{customer.pk}/").content.decode()
    assert "avans" in html
    assert "to'lanmagan sotuvlari yo'q" in html.lower()


def test_deleting_a_payment_reopens_the_qarz_it_had_settled(admin_client, db):
    """The qarz screens depend on rows they do not own — dropping the to'lov has to
    put the mijoz back on the list at exactly the old figure."""
    customer, lot = _customer(name="O'chirilgan To'lov"), _lot()
    sale = _post_sale(admin_client, lot, customer, kg="1000", price="1.60",
                      currency="usd", rate="12000")
    before = (customer.balance, customer.balance_uzs)
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1600"}, customer=customer, date="2026-07-18"))
    assert customer.balance == Decimal("0")
    payment = CustomerPayment.objects.get(customer=customer)

    admin_client.post(f"/customer-payments/{payment.pk}/delete/")
    sale.refresh_from_db()
    assert (customer.balance, customer.balance_uzs) == before
    assert sale.paid == Decimal("0")
    listed = [r["customer"].pk for r in
              admin_client.get("/debts/").context["page"].object_list]
    assert customer.pk in listed


def test_deleting_one_of_two_sales_hands_its_money_to_the_other(admin_client, db):
    """Freed allocation is avans again and the mijoz's other open sotuv has first
    claim on it — the qarz must fall by the deleted sotuv, not by more or less."""
    customer, lot = _customer(name="Ikki Sotuv"), _lot()
    old = _post_sale(admin_client, lot, customer, kg="1000", price="1.00",
                     currency="usd", rate="12000", date="2026-07-05")
    new = _post_sale(admin_client, lot, customer, kg="1000", price="1.00",
                     currency="usd", rate="12000", date="2026-07-12")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1200"}, customer=customer, date="2026-07-18"))
    assert customer.balance == Decimal("800.00")

    admin_client.post(f"/sales/{old.pk}/delete/")
    new.refresh_from_db()
    assert customer.balance == Decimal("-200.00")          # 1000 sold, 1200 paid
    assert new.remaining == Decimal("0")
    assert new.paid == Decimal("1000.00")
