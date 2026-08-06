"""QA audit: Hamkorlar (Partner) and Mijozlar (Customer).

Partner/Customer carry no money columns of their own — their money is entirely
DERIVED (Customer.sales_total / paid_total / balance and the _uzs twins). So the
four symptom families land here as:

  (a) round-trip   — a so'm-typed sotuv/to'lov must reach the mijoz total with its
                     typed side bit-exact, never re-derived from its conversion
  (b) no-drift     — re-saving a hamkor/mijoz through the REAL view must move no
                     stored field and no derived money figure
  (c) stickiness   — these two forms have no Valyuta picker; what has to "stick"
                     instead is the phone/contact normalisation and the code_slug /
                     code_counter machinery a rename touches
  (d) aggregates   — balance == Σ parts, including rows in MIXED currencies at
                     MIXED kursi, and the screen totals built on top of it

TRIAGE PASS (second session). The first tester left five UNVALIDATED xfail claims.
Two of them were wrong and have been rewritten to assert the behaviour the code
actually intends — each carries a "CLAIM WITHDRAWN" comment saying what the
original expectation got wrong. The remaining three are kept as xfail with the
figures and file:line that prove them.

Every test now either passes or is an xfail whose BUG: reason names a real defect.
"""
from decimal import Decimal

import pytest

from crm.formatting import normalize_container, phone_country, validate_intl_phone
from crm.forms import CustomerForm, PartnerForm
from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, Partner,
    PayMethod, Sale, Shipment, ShipmentLine, ShipmentStatus,
    allocate_customer_payment, customer_receivable_total,
)

pytestmark = pytest.mark.django_db

NBSP = " "   # the thousands separator every money filter emits


# --- factories ---------------------------------------------------------------

def _lot(kg="100000", brand="LLDPE", price="0.50"):
    """An arrived lot to hang sotuvlar off."""
    partner = Partner.objects.create(name="Pars", phone="", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand, kg=Decimal(kg),
                                price=Decimal(price))
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        eta="2026-07-15", arrived="2026-07-16", transport="01A111AA",
        container="MSKU 123456 7")
    return ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg))


def _usd_sale(customer, lot, kg, price, rate="12000", date="2026-07-18"):
    """A sotuv typed in DOLLARS: `price` exact, the so'm side derived at `rate`."""
    price, rate = Decimal(price), Decimal(rate)
    return Sale.objects.create(
        customer=customer, line=lot, kg=Decimal(kg), price=price,
        price_uzs=(price * rate).quantize(Decimal("0.01")),
        currency=Currency.USD, exchange_rate=rate, date=date)


def _som_sale(customer, lot, kg, price_uzs, rate, date="2026-07-18"):
    """A sotuv typed in SO'M: `price_uzs` exact, the dollar side derived at `rate`
    and quantised to the 4dp narx quantum (PriceEntryFormMixin.usd_places)."""
    price_uzs, rate = Decimal(price_uzs), Decimal(rate)
    return Sale.objects.create(
        customer=customer, line=lot, kg=Decimal(kg),
        price=(price_uzs / rate).quantize(Decimal("0.0001")), price_uzs=price_uzs,
        currency=Currency.UZS, exchange_rate=rate, date=date)


def _usd_payment(customer, amount, rate="12000", method=PayMethod.CASH, fee="0",
                 date="2026-07-20"):
    amount, rate = Decimal(amount), Decimal(rate)
    return CustomerPayment.objects.create(
        customer=customer, date=date, amount=amount,
        amount_uzs=(amount * rate).quantize(Decimal("0.01")),
        currency=Currency.USD, exchange_rate=rate, method=method,
        fee_percent=Decimal(fee))


def _som_payment(customer, amount_uzs, rate, method=PayMethod.CASH, fee="0",
                 date="2026-07-20"):
    amount_uzs, rate = Decimal(amount_uzs), Decimal(rate)
    return CustomerPayment.objects.create(
        customer=customer, date=date,
        amount=(amount_uzs / rate).quantize(Decimal("0.01")),
        amount_uzs=amount_uzs, currency=Currency.UZS, exchange_rate=rate,
        method=method, fee_percent=Decimal(fee))


def _row_html(client, url, name):
    """The one <tr>…</tr> of the list page that mentions `name`. Cut at the closing
    tag too — base.html's own chrome contains the word "qarz", and a chunk that ran
    to the end of the document would match it."""
    html = client.get(url).content.decode()
    rows = [r.split("</tr>")[0] for r in html.split("<tr>") if name in r.split("</tr>")[0]]
    assert rows, f"{name} not on {url}"
    assert len(rows) == 1, f"{name} matched {len(rows)} rows"
    return rows[0]


def _balance_cell(client, url, name):
    """Just the Balans <td> of that mijoz's row.

    Narrower than `_row_html` on purpose: the row also carries the action buttons,
    one of which is titled "Avans", so a row-wide search for that word answers a
    different question than "what does this mijoz's balance say"."""
    cell = _row_html(client, url, name).split('<td class="cell-balance">')
    assert len(cell) == 2, f"no balance cell for {name} on {url}"
    return cell[1].split("</td>")[0]


# ── phone / container normalisation (crm/formatting.py) ─────────────────────────

def test_phone_accepts_uz_ir_tr_with_and_without_punctuation():
    """Only the digits are checked, so the same number in either shape passes."""
    for pretty, raw in [("+998 90 123 45 67", "998901234567"),
                        ("+98 912 345 6789", "989123456789"),
                        ("+90 532 123 45 67", "905321234567")]:
        assert validate_intl_phone(pretty) == pretty
        assert validate_intl_phone(raw) == raw
    assert validate_intl_phone("") == ""
    assert validate_intl_phone(None) == ""


def test_phone_country_never_confuses_the_three_prefixes():
    """UZ/IR/TR all come to 12 digits; fullmatch + the second digit keeps them
    apart. The nasty case is an Iranian number whose national part starts 998."""
    assert phone_country("+998 90 123 45 67") == "UZ"
    assert phone_country("+98 912 345 6789") == "IR"
    assert phone_country("+90 532 123 45 67") == "TR"
    # IR national part 9987654321 — contains "998" but not at position 0
    assert phone_country("+98 998 765 4321") == "IR"


@pytest.mark.parametrize("bad", [
    "+998 90 123 45 6",       # one digit short of UZ
    "+998 90 123 45 678",     # one digit long
    "+998901234",             # far too short
    "+7 903 123 45 67",       # RU
    "+82 10 1234 5678",       # KR
    "+1 202 555 0143",        # US
    "998",                    # bare prefix
    "abc",
])
def test_phone_rejects_nearly_valid_numbers(bad):
    with pytest.raises(Exception):
        validate_intl_phone(bad)


def test_partner_form_stores_the_phone_exactly_as_typed():
    """(a) round-trip: validate_intl_phone returns the value untouched, so the
    stored string is the operator's own formatting — nothing is re-derived."""
    f = PartnerForm({"name": "Pars Polymer", "phone": "+998 90 123 45 67",
                     "city": "Tehron", "note": ""})
    assert f.is_valid(), f.errors
    partner = f.save()
    partner.refresh_from_db()
    assert partner.phone == "+998 90 123 45 67"


@pytest.mark.parametrize("country,phone", [("uz", "+998 90 123 45 67"),
                                           ("ir", "+98 912 345 6789"),
                                           ("tr", "+90 532 123 45 67"),
                                           ("none", "")])
def test_phone_survives_a_round_trip_through_the_real_hamkor_view(admin_client,
                                                                  country, phone):
    """(a) The same round-trip through the REAL POST path, for every country the
    picker offers — the view must store the canonical string byte for byte, and
    re-reading the edit form must hand the same string back."""
    name = f"Round {country}"
    resp = admin_client.post("/partners/new/", {"name": name, "phone": phone,
                                                "city": "", "note": ""})
    assert resp.status_code == 302, resp.status_code
    partner = Partner.objects.get(name=name)
    assert partner.phone == phone
    form = admin_client.get(f"/partners/{partner.pk}/edit/").context["form"]
    assert form.initial["phone"] == phone


def test_container_normalisation_is_idempotent():
    """normalize_container must be a fixed point after one pass — otherwise a value
    re-saved through the form drifts a little every time it is edited."""
    for value in ["msku1234567", "MSKU 123456 7", "  mskU1234567 ", "MSKU-1234567",
                  "no  container", ""]:
        once = normalize_container(value)
        assert normalize_container(once) == once, value


def test_container_iso_grouping_and_non_iso_passthrough():
    assert normalize_container("msku1234567") == "MSKU 123456 7"
    assert normalize_container("MSKU 123456 7") == "MSKU 123456 7"
    # not ISO 6346 → uppercased and space-collapsed, but otherwise verbatim
    assert normalize_container("MSKU-1234567") == "MSKU-1234567"
    assert normalize_container("  tk  9 ") == "TK 9"


# ── (b) no-drift through the REAL views ────────────────────────────────────────

def test_partner_edit_twice_moves_nothing(admin_client):
    """Re-submitting the hamkor form unchanged, twice, must not move a single
    stored field — including the frozen code_counter the kelishuv codes hang off."""
    partner = Partner.objects.create(name="Sobir", phone="+998 90 123 45 67",
                                     city="Toshkent", note="izoh")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("1000"), price=Decimal("1"))
    partner.refresh_from_db()
    before = (partner.name, partner.phone, partner.city, partner.note,
              partner.code_slug, partner.code_counter)
    code_before = Contract.objects.get(pk=contract.pk).code

    payload = {"name": partner.name, "phone": partner.phone,
               "city": partner.city, "note": partner.note}
    for _ in range(2):
        resp = admin_client.post(f"/partners/{partner.pk}/edit/", payload)
        assert resp.status_code in (200, 302)
        partner.refresh_from_db()
        assert (partner.name, partner.phone, partner.city, partner.note,
                partner.code_slug, partner.code_counter) == before
        assert Contract.objects.get(pk=contract.pk).code == code_before


def test_renaming_a_hamkor_never_recycles_a_kelishuv_number(admin_client):
    """(c) stickiness of the code machinery: Partner.save() documents that the
    counter only ever climbs and the slug tracks the current name. So a rename must
    leave the OLD kelishuv's code frozen and make the NEXT one continue the
    sequence under the new slug — never hand out -1 a second time."""
    partner = Partner.objects.create(name="Sobir", phone="", city="")
    first = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=first, brand="LLDPE",
                                kg=Decimal("10"), price=Decimal("1"))
    partner.refresh_from_db()
    assert (first.code, partner.code_counter) == ("sobir-1", 1)

    resp = admin_client.post(f"/partners/{partner.pk}/edit/",
                             {"name": "Sobir Aka", "phone": "", "city": "", "note": ""})
    assert resp.status_code in (200, 302)
    partner.refresh_from_db()
    assert partner.code_slug == "sobir-aka"
    assert partner.code_counter == 1, "a rename must not reset the high-water mark"
    assert Contract.objects.get(pk=first.pk).code == "sobir-1", "issued codes are frozen"

    second = Contract.objects.create(partner=partner, created="2026-07-02")
    assert second.code == "sobir-aka-2"


def test_customer_edit_twice_moves_no_money(admin_client):
    """(b) The mijoz balance is derived, so a no-op edit of the CONTACT card must
    leave every derived money figure bit-identical."""
    customer = Customer.objects.create(name="Alisher Mebel",
                                       phone="+998 90 111 22 33",
                                       address="Toshkent", note="")
    lot = _lot()
    _usd_sale(customer, lot, "1000", "1.2345", rate="12500")
    _som_sale(customer, lot, "1000", "15000.55", rate="12700")
    _usd_payment(customer, "500", rate="12800")
    _som_payment(customer, "7000000", rate="12900")

    def figures():
        c = Customer.objects.get(pk=customer.pk)
        return (c.sales_total, c.sales_total_uzs, c.paid_total, c.paid_total_uzs,
                c.balance, c.balance_uzs)

    before = figures()
    payload = {"name": customer.name, "phone": customer.phone,
               "address": customer.address, "note": ""}
    for _ in range(2):
        resp = admin_client.post(f"/customers/{customer.pk}/edit/", payload)
        assert resp.status_code in (200, 302)
        assert figures() == before


def test_customer_edit_form_reopens_bound_to_the_stored_values(admin_client):
    """(c) stickiness, contact-card flavour: what the edit modal puts back in the
    boxes must be exactly what is stored, so pressing Saqlash changes nothing."""
    customer = Customer.objects.create(name="Bekzod Savdo",
                                       phone="+90 532 123 45 67",
                                       address="Farg'ona", note="eslatma")
    form = admin_client.get(f"/customers/{customer.pk}/edit/").context["form"]
    assert form.initial["phone"] == "+90 532 123 45 67"
    assert form.initial["name"] == "Bekzod Savdo"
    assert form.initial["address"] == "Farg'ona"
    # and the round-trip of that payload is a no-op
    resp = admin_client.post(f"/customers/{customer.pk}/edit/", {
        "name": form.initial["name"], "phone": form.initial["phone"],
        "address": form.initial["address"], "note": form.initial["note"]})
    assert resp.status_code in (200, 302)
    customer.refresh_from_db()
    assert customer.phone == "+90 532 123 45 67"


# ── (a) + (d) the balance properties ───────────────────────────────────────────

def test_som_typed_sale_reaches_the_mijoz_total_exactly():
    """(a) The so'm side the operator typed must survive into sales_total_uzs
    unchanged — never re-derived from the dollar side it was converted to."""
    customer = Customer.objects.create(name="Zarina Plast")
    lot = _lot()
    sale = _som_sale(customer, lot, "24000", "15000.55", rate="12000")
    # dollar side is a derived 4dp figure; so'm side is exactly what was typed
    assert sale.price_uzs == Decimal("15000.55")
    assert sale.price == Decimal("1.2500")
    assert customer.sales_total_uzs == Decimal("24000") * Decimal("15000.55")
    # and the dollar total is NOT the so'm total put back through the kurs
    assert customer.sales_total == Decimal("24000") * Decimal("1.2500")


def test_som_typed_tolov_reaches_paid_total_uzs_exactly():
    """(a) The to'lov half of the same rule: 12 345 678 so'm handed over is
    12 345 678 so'm in the mijoz total, not 1 028,81 $ × kurs (= 12 345 720)."""
    customer = Customer.objects.create(name="So'm To'lovchi")
    payment = _som_payment(customer, "12345678", rate="12000")
    assert payment.amount_uzs == Decimal("12345678")
    assert payment.amount == Decimal("1028.81")           # derived, 2dp
    assert customer.paid_total_uzs == Decimal("12345678")
    assert customer.paid_total_uzs != payment.amount * Decimal("12000")


def test_balance_is_the_exact_sum_of_its_parts_across_mixed_currencies():
    """(d) Both sides of the balance must equal Σ of the rows' own sides, with
    sotuvlar in both currencies at three different kursi."""
    customer = Customer.objects.create(name="Aralash Mijoz")
    lot = _lot()
    sales = [
        _usd_sale(customer, lot, "1000", "1.10", rate="12000"),
        _som_sale(customer, lot, "1000", "13500", rate="12500"),
        _usd_sale(customer, lot, "500", "1.25", rate="13000"),
    ]
    payments = [
        _usd_payment(customer, "800", rate="12100"),
        _som_payment(customer, "9000000", rate="12600"),
    ]
    c = Customer.objects.get(pk=customer.pk)
    assert c.sales_total == sum(s.net_total for s in sales)
    assert c.sales_total_uzs == sum(s.net_total_uzs for s in sales)
    assert c.paid_total == sum(p.net_amount for p in payments)
    assert c.paid_total_uzs == sum(p.net_amount_uzs for p in payments)
    assert c.balance == c.sales_total - c.paid_total
    assert c.balance_uzs == c.sales_total_uzs - c.paid_total_uzs


def test_paid_total_is_net_of_the_bank_foiz_on_both_sides():
    """A perechisleniya foiz never reached us, so it settles nothing — and the so'm
    twin has to be the same slice of the row's own so'm value, not a reconversion."""
    customer = Customer.objects.create(name="Foizli Mijoz")
    payment = _som_payment(customer, "12600000", rate="12600",
                           method=PayMethod.TRANSFER, fee="2")
    assert payment.amount == Decimal("1000.00")
    assert payment.net_amount == Decimal("980.00")
    assert customer.paid_total == Decimal("980.00")
    # 2% off the exact so'm figure the operator typed
    assert payment.net_amount_uzs == Decimal("12348000.00")
    assert customer.paid_total_uzs == Decimal("12348000.00")


def test_balance_on_a_mijoz_with_nothing_is_a_clean_zero():
    customer = Customer.objects.create(name="Bo'sh Mijoz")
    assert customer.sales_total == customer.paid_total == Decimal("0")
    assert customer.balance == Decimal("0") and customer.balance_uzs == Decimal("0")


def test_advance_shows_as_a_negative_balance_on_both_sides():
    """Boundary: paying more than was sold is an avans, negative on both sides."""
    customer = Customer.objects.create(name="Avansli Mijoz")
    lot = _lot()
    _usd_sale(customer, lot, "100", "1.00", rate="12000")
    _usd_payment(customer, "250", rate="12000")
    assert customer.balance == Decimal("-150.00")
    assert customer.balance_uzs == Decimal("-1800000.00")


def test_a_dollar_sotuv_paid_in_dollars_is_square(admin_client):
    """A $1 000 sotuv paid with $1 000 is settled, whatever the kurs did between the
    two. Its so'm twin does NOT come out at zero — the sale was rated at 12 000 and
    the to'lov at 13 000 — and that is fine, because a dollar sotuv is not settled in
    so'm. The twin is kept for the blended figures (kassa, tannarx) that need it.

    This test used to argue the opposite: that settlement was dollar-denominated for
    every sotuv, so a so'm one had to be measured in dollars too. That is the rule
    this phase replaced.
    """
    customer = Customer.objects.create(name="Nol Dollar", phone="", address="")
    lot = _lot()
    sale = _usd_sale(customer, lot, "1000", "1.00", rate="12000")
    payment = _usd_payment(customer, "1000", rate="13000")
    allocate_customer_payment(payment)

    assert customer.balance == Decimal("0")
    sale.refresh_from_db()
    # square in the currency it was agreed in, which is the one that settles it
    assert sale.currency == Currency.USD
    assert sale.remaining_own == Decimal("0")
    assert sale.is_paid
    # the twin does not land on zero, and is not asked to
    assert sale.remaining_uzs != Decimal("0")

    cell = _balance_cell(admin_client, "/customers/", "Nol Dollar")
    assert "qarz" not in cell and "avans" not in cell, cell


@pytest.mark.xfail(reason="BUG: Customer.balance_uzs (crm/models.py:339) subtracts "
                          "two so'm figures struck at DIFFERENT kursi — "
                          "sales_total_uzs at the sotuv's kurs minus paid_total_uzs "
                          "at each to'lov's kurs — instead of following the house "
                          "rule MoneyEntry.in_som (:105) that Sale.paid_uzs (:1408) "
                          "uses. A mijoz who bought $10 000 at 12 000 and has paid "
                          "$9 800 at 12 600 still owes $200: the sotuv row on "
                          "debt_customer.html says Qoldiq 2 400 000 so'm, while the "
                          "header of that same page (customer.balance_uzs) says "
                          "-3 480 000 so'm — 5 880 000 so'm apart, and the wrong sign",
                  strict=False)
def test_customer_balance_uzs_disagrees_with_the_per_sale_qoldiq():
    """(d) aggregate consistency: the mijoz-level so'm qarz must be Σ of the so'm
    qoldiq of their own sotuvlar. Both figures are printed on debt_customer.html —
    the header from customer.balance_uzs, each row from sale.remaining_uzs."""
    customer = Customer.objects.create(name="Qoldiq Mijoz")
    lot = _lot()
    sale = _usd_sale(customer, lot, "10000", "1.00", rate="12000")
    payment = _usd_payment(customer, "9800", rate="12600")
    allocate_customer_payment(payment)

    sale.refresh_from_db()
    assert customer.balance == Decimal("200.00")
    assert sale.remaining == Decimal("200.00")
    assert sale.remaining_uzs == Decimal("2400000.00")     # 200 $ at the sotuv's kurs
    assert customer.balance_uzs == sale.remaining_uzs, (
        f"header says {customer.balance_uzs} so'm, the sotuv row says "
        f"{sale.remaining_uzs} so'm")


@pytest.mark.xfail(reason="BUG: customer_receivable_total() (crm/models.py:1126) "
                          "picks debtors by the DOLLAR balance and then adds up "
                          "Customer.balance_uzs (:339), which carries the kurs "
                          "difference between a sotuv and the to'lovlar against it. "
                          "A mijoz owing $200 on a $10 000 sotuv booked at 12 000 and "
                          "paid down at 12 600 contributes -3 480 000 so'm to the "
                          "'Mijozlar qarzi' KPI (dashboard, crm/views.py:100 and "
                          ":2339) — the so'm figure prints NEGATIVE beside a positive "
                          "dollar qarz. Correct contribution is 2 400 000 so'm",
                  strict=False)
def test_customer_receivable_som_total_is_never_negative():
    """(d) The kurs only ever climbs and to'lovlar always land after the sotuv, so a
    nearly-settled mijoz systematically flips the so'm side negative: here 98% of a
    $10 000 sotuv paid a year later at a 5% higher kurs."""
    customer = Customer.objects.create(name="Qarzdor")
    lot = _lot()
    sale = _usd_sale(customer, lot, "10000", "1.00", rate="12000")
    payment = _usd_payment(customer, "9800", rate="12600")
    allocate_customer_payment(payment)
    sale.refresh_from_db()

    assert customer.balance == Decimal("200.00")
    total, total_uzs, count = customer_receivable_total()
    assert (total, count) == (Decimal("200.00"), 1)
    assert total_uzs >= 0, f"debt KPI prints {total_uzs} so'm under a qarz label"
    assert total_uzs == sale.remaining_uzs


def test_receivable_total_keeps_avans_out_of_the_qarz_figure():
    """(d) Documented in customer_receivable_total's own docstring: one mijoz's avans
    must not net off another's qarz — the money is not fungible across customers."""
    lot = _lot()
    debtor = Customer.objects.create(name="Qarzli")
    _usd_sale(debtor, lot, "1000", "1.00", rate="12000")
    _usd_payment(debtor, "400", rate="12000")

    prepaid = Customer.objects.create(name="Avansli")
    _usd_sale(prepaid, lot, "100", "1.00", rate="12000")
    _usd_payment(prepaid, "900", rate="12000")

    total, total_uzs, count = customer_receivable_total()
    assert (total, count) == (Decimal("600.00"), 1)
    assert total_uzs == Decimal("7200000.00")


# ── deleting a hamkor / mijoz other rows depend on ─────────────────────────────

def test_partner_with_a_kelishuv_is_protected(admin_client):
    partner = Partner.objects.create(name="Bog'langan", phone="", city="")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("10"), price=Decimal("1"))
    resp = admin_client.post(f"/partners/{partner.pk}/delete/")
    assert resp.status_code in (200, 302)
    assert Partner.objects.filter(pk=partner.pk).exists()
    assert Contract.objects.filter(pk=contract.pk).exists()


def test_partner_without_kelishuv_deletes(admin_client):
    partner = Partner.objects.create(name="Yolg'iz", phone="", city="")
    resp = admin_client.post(f"/partners/{partner.pk}/delete/")
    assert resp.status_code in (200, 302)
    assert not Partner.objects.filter(pk=partner.pk).exists()


def test_customer_with_a_sotuv_is_protected(admin_client):
    customer = Customer.objects.create(name="Sotuvli Mijoz")
    _usd_sale(customer, _lot(), "10", "1.00")
    resp = admin_client.post(f"/customers/{customer.pk}/delete/")
    assert resp.status_code in (200, 302)
    assert Customer.objects.filter(pk=customer.pk).exists()
    assert Sale.objects.filter(customer=customer).exists()


def test_customer_with_only_a_tolov_is_protected_and_says_why(admin_client):
    """A mijoz who has only ever PAID is protected too (CustomerPayment.customer is
    PROTECT) — the operator has to be told which rows are holding them."""
    customer = Customer.objects.create(name="To'lovli Mijoz")
    _usd_payment(customer, "100")
    resp = admin_client.post(f"/customers/{customer.pk}/delete/", follow=True)
    assert Customer.objects.filter(pk=customer.pk).exists()
    assert CustomerPayment.objects.filter(customer=customer).exists()
    text = " ".join(str(m) for m in resp.context["messages"])
    assert "o'chirib bo'lmaydi" in text, text


# ── customer_quick_create (the inline "+ Yangi mijoz" in other modals) ──────────

def test_quick_create_returns_what_the_calling_modal_consumes(admin_client):
    """base.html's inline quick-add reads `d.id` and `d.text` off the JSON and
    calls addOption(sel, d.id, d.text); anything else leaves the select empty."""
    resp = admin_client.post("/customers/quick/",
                             {"name": "Yangi Mijoz", "phone": "+998 90 111 22 33"})
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/json")
    data = resp.json()
    assert set(data) >= {"id", "text"}
    customer = Customer.objects.get(name="Yangi Mijoz")
    assert data["id"] == customer.pk
    assert data["text"] == str(customer) == "Yangi Mijoz"
    # and the id is actually selectable in the sotuv form it was opened from
    from crm.forms import SaleForm
    assert SaleForm().fields["customer"].queryset.filter(pk=data["id"]).exists()


def test_quick_create_blank_name_is_a_400_the_modal_can_show(admin_client):
    resp = admin_client.post("/customers/quick/", {"name": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"]


def test_quick_create_accepts_a_blank_phone(admin_client):
    """The panel's phone box is labelled ixtiyoriy, so an empty one must not be a
    validation problem at either end."""
    resp = admin_client.post("/customers/quick/", {"name": "Telefonsiz", "phone": ""})
    assert resp.status_code == 200
    customer = Customer.objects.get(name="Telefonsiz")
    assert customer.phone == ""
    assert CustomerForm({"name": customer.name, "phone": customer.phone,
                         "address": "", "note": ""}, instance=customer).is_valid()


@pytest.mark.xfail(reason="BUG: customer_quick_create (crm/views.py:271) writes "
                          "request.POST['phone'] straight onto the row without "
                          "validate_intl_phone. base.html's window.canonicalPhone "
                          "(:902) has no minimum length, so a half-typed number is "
                          "submitted as the canonical-looking '+998 90 123 4' and "
                          "stored. CustomerForm.clean_phone (crm/forms.py:218) then "
                          "refuses that value, so every later edit of that mijoz is "
                          "rejected over a field the operator never filled in there",
                  strict=False)
def test_quick_create_phone_must_pass_the_same_rule_as_the_full_form(admin_client):
    # what the quick panel actually sends when the operator stops typing early
    partial = "+998 90 123 4"
    assert not CustomerForm({"name": "X", "phone": partial, "address": "",
                             "note": ""}).is_valid()

    admin_client.post("/customers/quick/", {"name": "Tez Mijoz", "phone": partial})
    customer = Customer.objects.get(name="Tez Mijoz")

    # the concrete harm: a later, unrelated edit of that mijoz cannot be saved
    resp = admin_client.post(f"/customers/{customer.pk}/edit/",
                             {"name": "Tez Mijoz", "phone": customer.phone,
                              "address": "Toshkent", "note": ""})
    assert resp.status_code in (200, 302)
    customer.refresh_from_db()
    assert customer.address == "Toshkent", "the edit was rejected over the quick phone"


def test_quick_create_reuses_a_homonym_without_touching_their_contact_card(admin_client):
    """CLAIM WITHDRAWN (was xfail "quick-create reuses by name__iexact and picks
    .first() … the typed phone that would have told them apart is dropped on the
    floor", asserting the reused row must come back carrying the newly typed phone).

    What the original claim got wrong: reuse-by-name is the documented contract on
    both sides — crm/views.py:263 ("Reuses a same-name customer instead of
    duplicating") and the live quick-add JS in base.html:1219 ("reuses a same-name
    record instead of duplicating"). The fix it demanded — writing the phone typed
    in a sotuv modal onto an EXISTING mijoz — would silently overwrite the contact
    details of a customer the operator was not editing, which is a data-loss bug of
    its own. The right behaviour is what the code does: reuse, and leave the stored
    card alone. Ambiguity between two homonyms is a consequence of a deliberate
    documented decision, not a value defect.
    """
    for phone in ["+998 90 111 11 11", "+998 90 222 22 22"]:
        resp = admin_client.post("/customers/new/",
                                 {"name": "Ali", "phone": phone,
                                  "address": "", "note": ""})
        assert resp.status_code == 302
    assert Customer.objects.filter(name="Ali").count() == 2

    data = admin_client.post("/customers/quick/",
                             {"name": "ali", "phone": "+998 90 333 33 33"}).json()
    assert data["created"] is False
    assert Customer.objects.filter(name="Ali").count() == 2, "no third row is minted"
    reused = Customer.objects.get(pk=data["id"])
    assert reused.name == "Ali"
    assert reused.phone in ["+998 90 111 11 11", "+998 90 222 22 22"], (
        "the stored contact card of an existing mijoz must not be overwritten")


def test_quick_create_reuse_is_case_insensitive_and_makes_no_second_row(admin_client):
    """Documented behaviour: a same-name mijoz is REUSED, not duplicated."""
    existing = Customer.objects.create(name="Bor Mijoz", phone="+998 90 111 22 33")
    data = admin_client.post("/customers/quick/", {"name": "  bor mijoz "}).json()
    assert data["created"] is False and data["id"] == existing.pk
    assert Customer.objects.filter(name__iexact="bor mijoz").count() == 1


# ── list screens ───────────────────────────────────────────────────────────────

@pytest.mark.xfail(reason="BUG: customer_list (crm/views.py:238) and partner_list "
                          "(:173) match the phone with a plain icontains on the "
                          "stored string, but base.html's phone widget (:921) always "
                          "submits the canonical spaced form, so every phone is "
                          "stored as '+998 90 123 45 67'. The search box advertises "
                          "'Ismi, telefon yoki manzil bo'yicha qidirish' yet the "
                          "number as it is written everywhere else — 901234567, or "
                          "+998901234567 pasted out of Telegram — returns 0 rows",
                  strict=False)
def test_list_search_finds_a_mijoz_by_their_phone_digits(admin_client):
    Customer.objects.create(name="Telefonli", phone="+998 90 123 45 67",
                            address="Toshkent")
    Partner.objects.create(name="Telefonli Hamkor", phone="+998 90 123 45 67",
                           city="Tehron")
    # the search itself is wired: the spaced form the screen displays does match
    assert "Telefonli" in admin_client.get(
        "/customers/", {"q": "90 123 45 67"}).content.decode()
    # …but the same number written as digits finds nobody
    assert "Telefonli" in admin_client.get(
        "/customers/", {"q": "901234567"}).content.decode()
    assert "Telefonli Hamkor" in admin_client.get(
        "/partners/", {"q": "998901234567"}).content.decode()


def test_list_search_still_matches_name_and_address(admin_client):
    """Guard for the phone fix above: it must not cost the other two columns."""
    Customer.objects.create(name="Telefonli", phone="+998 90 123 45 67",
                            address="Toshkent shahri")
    for query in ["Telefon", "telefonli", "Toshkent"]:
        html = admin_client.get(f"/customers/?q={query}").content.decode()
        assert "Telefonli" in html, query


def test_customer_list_keeps_a_two_currency_qarz_apart(admin_client):
    """(d) A mijoz who bought once in dollars and once in so'm owes two debts, not one
    converted total. The row carries both, each the figure that sotuv was agreed at —
    $1 000 and 13 000 000 so'm, never their blend."""
    customer = Customer.objects.create(name="Ikki Valyuta", phone="", address="")
    lot = _lot()
    _usd_sale(customer, lot, "1000", "1.00", rate="12000")
    _som_sale(customer, lot, "1000", "13000", rate="13000")

    cell = _balance_cell(admin_client, "/customers/", "Ikki Valyuta")
    assert "qarz" in cell
    # NBSP thousands separator, per the house convention in crm_extras
    assert f"$1{NBSP}000" in cell                       # the dollar sotuv, alone
    assert f"13{NBSP}000{NBSP}000 so&#x27;m" in cell    # the so'm sotuv, alone
    assert f"$2{NBSP}000" not in cell                   # the blend nobody agreed to
    assert f"25{NBSP}000{NBSP}000" not in cell
