"""QA audit — the currency conversion core.

Covers convert_pair(), the MoneyEntry.save() backstop, is_som/in_som, the
CashEntry foiz pair, and the two form mixins that every money screen sits on.

Probe families, mapped onto the symptoms the product owner reported:
  (a) round-trip   — the typed side must come back bit-exact in both directions
  (b) no-drift     — re-saving a row unchanged through the real view must not
                     move a single figure ("values change by themselves")
  (c) stickiness   — currency=uzs must survive the round trip through the form,
                     including when the row is re-opened for editing
  (d) aggregates   — a page total must equal the sum of its parts across mixed
                     currencies and mixed kursi

Tests marked xfail carry a BUG: reason and are deliberate defect documentation.
"""
from decimal import Decimal

import pytest

from conftest import make_contract, make_shipment, payment_rows, supplier_payment_rows
from crm.models import (
    LEGACY_RATE, Currency, Customer, CustomerPayment, Reservation, Sale,
    ShipmentExpense, ShipmentStatus, SupplierPayment, convert_pair,
)

USD, UZS = Currency.USD, Currency.UZS


# --- helpers ---------------------------------------------------------------

def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _shipment():
    return make_shipment(kg="100", status=ShipmentStatus.objects.first())


def rendered_payload(form, **overrides):
    """The POST body a browser would send if the operator opened this edit form
    and pressed Save without touching anything.

    Every value is taken from the BOUND form exactly as it renders — which is the
    whole point: whatever the box shows is what comes back."""
    data = {}
    for name in form.fields:
        value = form[name].value()
        if value is None:
            value = ""
        if hasattr(value, "pk"):
            value = value.pk
        data[name] = str(value)
    data.update({key: str(value) for key, value in overrides.items()})
    return data


def money_snapshot(row):
    usd_field, uzs_field = row.money_fields
    return (row.currency, getattr(row, usd_field), getattr(row, uzs_field),
            row.exchange_rate)


def create_payment(client, customer, **row):
    """One mijoz to'lov through the real multi-row create modal."""
    resp = client.post("/customer-payments/new/",
                       payment_rows(row, customer=customer))
    assert resp.status_code == 302, getattr(resp, "context", None)
    return resp


def create_expense(client, shipment, category="declarant", **shared):
    """One xarajat through the real turkum grid.

    A deklarant by default rather than a bojxona: the bojxona box asks which tamojni
    paid (money we sent them beforehand), and these tests are about the kurs the
    grid stores, not about whose float a row comes off."""
    data = {"shipment": shipment.pk, "date": "2026-07-22", "currency": "usd",
            "method": "cash", "exchange_rate": "12000", "fee_percent": "0",
            "note": ""}
    amount = shared.pop("amount")
    data.update(shared)
    data[f"amount_{category}"] = str(amount)
    resp = client.post("/expenses/new/", data)
    assert resp.status_code == 302
    return resp


# ── (a) ROUND-TRIP ──────────────────────────────────────────────────────────

def test_a_som_figure_is_stored_bit_exact_and_never_re_derived():
    """12 345 678 so'm at 12 345 does not divide evenly. The so'm side must come
    back byte-for-byte; deriving it from its own rounded dollar twin would land on
    12 345 665.85 and move the agreed figure."""
    usd, uzs = convert_pair(Decimal("12345678"), UZS, Decimal("12345"))
    assert uzs == Decimal("12345678.00")
    assert usd == Decimal("1000.05")                     # 1000.0549… → 1000.05
    assert (usd * Decimal("12345")).quantize(Decimal("0.01")) != uzs


def test_a_dollar_figure_is_stored_bit_exact_and_its_som_twin_derived():
    usd, uzs = convert_pair(Decimal("1234.56"), USD, Decimal("12345"))
    assert usd == Decimal("1234.56")
    assert uzs == Decimal("15240643.20")


def test_a_per_kg_narx_keeps_four_decimals_and_rounds_half_up():
    """A $/kg rounded to cents moves a 24-tonne lot by dollars, so prices carry a
    finer quantum than lump sums — and the quantum rounds half up, not half even."""
    usd, uzs = convert_pair(Decimal("14040"), UZS, Decimal("12000"), "0.0001")
    assert usd == Decimal("1.1700") and uzs == Decimal("14040.00")

    # 1.00005 sits exactly on the 4dp quantum; HALF_UP must climb, not bank.
    usd, _ = convert_pair(Decimal("1.00005"), USD, Decimal("12000"), "0.0001")
    assert usd == Decimal("1.0001")

    # And on the so'm side, a sub-tiyin narx still records something rather than 0.
    usd, _ = convert_pair(Decimal("1"), UZS, Decimal("12000"), "0.0001")
    assert usd == Decimal("0.0001")


def test_a_missing_zero_or_negative_kurs_is_refused_in_both_directions():
    """Money with no kurs has only one of its two values and could never appear in
    a total of the other currency — so it is refused rather than stored half-blind."""
    for bad in [None, Decimal("0"), Decimal("-1"), 0, ""]:
        with pytest.raises(ValueError):
            convert_pair(Decimal("100"), USD, bad)
        with pytest.raises(ValueError):
            convert_pair(Decimal("100"), UZS, bad)


def test_a_huge_and_a_tiny_kurs_both_convert_without_losing_the_typed_side():
    huge = Decimal("9999999.99")
    usd, uzs = convert_pair(Decimal("100"), USD, huge)
    assert usd == Decimal("100.00") and uzs == Decimal("999999999.00")

    tiny = Decimal("0.01")
    usd, uzs = convert_pair(Decimal("100"), USD, tiny)
    assert usd == Decimal("100.00") and uzs == Decimal("1.00")
    usd, uzs = convert_pair(Decimal("100"), UZS, tiny)
    assert uzs == Decimal("100.00") and usd == Decimal("10000.00")


# ── the save() backstop ─────────────────────────────────────────────────────

def test_the_backstop_fills_a_missing_som_twin_at_the_rows_own_kurs(db):
    """Rows built in code (importer, seeders, shell) set only the dollar column;
    without the fill they read as 0 so'm on every so'm screen."""
    payment = SupplierPayment.objects.create(
        contract=make_contract(), date="2026-07-02", amount=Decimal("250"),
        exchange_rate=Decimal("12650"), method="cash")
    payment.refresh_from_db()
    assert payment.amount_uzs == Decimal("3162500.00")


def test_the_backstop_never_overwrites_a_typed_som_value_on_a_som_row(db):
    """A so'm-typed row has a truthy dollar side too (it was derived on the way
    in). The backstop must still leave the typed so'm figure alone — recomputing
    it from the rounded dollar twin is exactly the drift convert_pair avoids."""
    payment = CustomerPayment.objects.create(
        customer=_customer(), date="2026-07-20", currency=UZS,
        amount=Decimal("1000.05"), amount_uzs=Decimal("12345678"),
        exchange_rate=Decimal("12345"), method="cash")
    for _ in range(3):                                   # repeated re-saves
        payment.note = payment.note + "."
        payment.save()
        payment.refresh_from_db()
    assert payment.amount_uzs == Decimal("12345678.00")
    assert payment.amount == Decimal("1000.05")


def test_the_backstop_is_one_directional_so_a_som_row_can_lose_its_dollar_side(db):
    """Documented asymmetry, and a real trap for code-built rows: a bron entered
    in so'm with only `price_uzs` set keeps price = None. It then shows a so'm
    total and NO dollar total — the same "reads as a missing figure" gap the
    backstop exists to close, just on the other side."""
    bron = Reservation.objects.create(
        customer=_customer(), brand="LLDPE", kg=Decimal("1000"), currency=UZS,
        price=None, price_uzs=Decimal("14040"), exchange_rate=Decimal("12000"))
    bron.refresh_from_db()
    assert bron.price is None                            # never back-filled
    assert bron.total is None                            # invisible to $ totals
    assert bron.total_uzs == Decimal("14040000.00")      # …but visible to so'm


def test_a_targeted_update_that_omits_the_money_column_does_not_persist_the_fill(db):
    """save(update_fields=[...]) that does not name the dollar column leaves the
    backstop's fill on the in-memory object only. The row still reads 0 so'm in
    the database while the object in hand says otherwise."""
    payment = SupplierPayment.objects.create(
        contract=make_contract(), date="2026-07-02", amount=Decimal("250"),
        amount_uzs=Decimal("3000000"), exchange_rate=Decimal("12000"), method="cash")
    SupplierPayment.objects.filter(pk=payment.pk).update(amount_uzs=Decimal("0"))

    payment = SupplierPayment.objects.get(pk=payment.pk)
    payment.note = "izoh"
    payment.save(update_fields=["note"])
    assert payment.amount_uzs == Decimal("3000000.00")   # filled in memory…
    payment.refresh_from_db()
    assert payment.amount_uzs == Decimal("0.00")         # …but not on the row


# ── is_som / in_som / the foiz pair ─────────────────────────────────────────

def test_is_som_reads_the_row_not_a_sitewide_switch(db):
    customer = _customer()
    som_row = CustomerPayment.objects.create(
        customer=customer, date="2026-07-20", currency=UZS, amount=Decimal("100"),
        amount_uzs=Decimal("1200000"), exchange_rate=Decimal("12000"), method="cash")
    usd_row = CustomerPayment.objects.create(
        customer=customer, date="2026-07-20", currency=USD, amount=Decimal("100"),
        amount_uzs=Decimal("1200000"), exchange_rate=Decimal("12000"), method="cash")
    assert som_row.is_som and not usd_row.is_som


def test_in_som_rates_a_derived_figure_at_the_rows_own_kurs_not_todays(db):
    payment = SupplierPayment.objects.create(
        contract=make_contract(), date="2026-07-02", amount=Decimal("1000"),
        amount_uzs=Decimal("12650000"), exchange_rate=Decimal("12650"),
        method="transfer", commission_percent=Decimal("3"))
    assert payment.commission_amount == Decimal("30.00")
    assert payment.commission_amount_uzs == Decimal("379500.00")   # 30 × 12 650
    assert payment.in_som(None) == Decimal("0.00")                 # blank is 0


def test_the_incoming_foiz_som_value_is_a_slice_of_the_stored_som(db):
    """CashEntry's rule: the cut was charged at the kurs of the day the money
    moved, so it is a share of the row's OWN so'm value, never a reconversion."""
    payment = CustomerPayment.objects.create(
        customer=_customer(), date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12650000"), exchange_rate=Decimal("12650"),
        method="transfer", fee_percent=Decimal("2"))
    assert payment.fee_amount == Decimal("20.00")
    assert payment.fee_amount_uzs == Decimal("253000.00")
    assert payment.net_amount_uzs == Decimal("12397000.00")
    # the slice and its parent agree about the kurs, whatever exchange_rate says
    assert payment.fee_amount_uzs + payment.net_amount_uzs == payment.amount_uzs


@pytest.mark.xfail(reason="BUG (low): SupplierPayment.fee_amount_uzs shadows "
                          "CashEntry's slice rule with in_som() — a reconversion "
                          "at exchange_rate. Immaterial while the stored pair and "
                          "the kurs agree; on a row whose exchange_rate is the "
                          "LEGACY_RATE default (importer/seeder rows) the chiqim "
                          "so'm figure is rated at a kurs the row never used",
                   strict=False)
def test_the_outgoing_foiz_som_value_follows_the_same_slice_rule(db):
    """Same shaped row as the incoming one above, on the chiqim side. CashEntry
    documents one rule for fee_amount_uzs; SupplierPayment quietly uses another,
    so a row whose stored so'm value was typed at a kurs other than
    `exchange_rate` reports a fee its own amount_uzs does not contain."""
    payment = SupplierPayment.objects.create(
        contract=make_contract(kg="100000", price="1.00"), date="2026-07-02",
        amount=Decimal("1000"), amount_uzs=Decimal("12650000"),
        exchange_rate=LEGACY_RATE, method="transfer", fee_percent=Decimal("2"))
    assert payment.fee_amount == Decimal("20.00")
    # slice rule → 12 650 000 × 20 / 1000 = 253 000. in_som() gives 240 000.
    assert payment.fee_amount_uzs == Decimal("253000.00")
    assert payment.total_out_uzs == Decimal("12903000.00")


def test_the_foiz_is_zero_on_naqd_and_on_a_zero_row(db):
    """Boundaries: a method that charges nothing, and a row with no money in it —
    the so'm slice must not divide by zero."""
    naqd = CustomerPayment.objects.create(
        customer=_customer(), date="2026-07-20", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), method="cash", fee_percent=Decimal("2"))
    assert naqd.fee_amount == Decimal("0") and naqd.fee_amount_uzs == Decimal("0")

    empty = CustomerPayment.objects.create(
        customer=_customer("Bo'sh"), date="2026-07-20", amount=Decimal("0"),
        amount_uzs=Decimal("0"), method="transfer", fee_percent=Decimal("2"))
    assert empty.fee_amount == Decimal("0") and empty.fee_amount_uzs == Decimal("0")


# ── (c) CURRENCY STICKINESS, through the real views ─────────────────────────

def test_a_som_payment_saves_as_som_with_the_typed_figure_in_the_som_column(admin_client, db):
    create_payment(admin_client, _customer(), currency="uzs",
                   amount="12345678", exchange_rate="12345")
    payment = CustomerPayment.objects.get()
    assert payment.currency == UZS
    assert payment.amount_uzs == Decimal("12345678.00")   # typed, untouched
    assert payment.amount == Decimal("1000.05")           # derived


def test_the_edit_form_of_a_som_payment_still_says_som(admin_client, db):
    """The picker itself sticks — this is the half that works, and the contrast
    that makes the next test a real defect rather than a misreading."""
    create_payment(admin_client, _customer(), currency="uzs",
                   amount="12345678", exchange_rate="12345")
    payment = CustomerPayment.objects.get()
    resp = admin_client.get(f"/customer-payments/{payment.pk}/edit/")
    assert resp.status_code == 200
    assert resp.context["form"]["currency"].value() == UZS


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_form_of_a_som_payment_offers_the_som_figure_in_the_typed_box(
        admin_client, db):
    create_payment(admin_client, _customer(), currency="uzs",
                   amount="12345678", exchange_rate="12345")
    payment = CustomerPayment.objects.get()
    resp = admin_client.get(f"/customer-payments/{payment.pk}/edit/")
    form = resp.context["form"]
    assert form["currency"].value() == UZS
    assert Decimal(str(form["amount"].value())) == Decimal("12345678.00")


# ── (b) IDEMPOTENCE / NO-DRIFT ──────────────────────────────────────────────

def test_resaving_a_dollar_payment_unchanged_moves_nothing_twice_over(admin_client, db):
    create_payment(admin_client, _customer(), currency="usd",
                   amount="1234.56", exchange_rate="12345")
    payment = CustomerPayment.objects.get()
    before = money_snapshot(payment)
    assert before == (USD, Decimal("1234.56"), Decimal("15240643.20"), Decimal("12345.00"))

    for round_trip in range(2):
        form = admin_client.get(
            f"/customer-payments/{payment.pk}/edit/").context["form"]
        resp = admin_client.post(f"/customer-payments/{payment.pk}/edit/",
                                 rendered_payload(form))
        assert resp.status_code == 302, resp.context["form"].errors
        payment.refresh_from_db()
        assert money_snapshot(payment) == before, f"drifted on re-save {round_trip + 1}"


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_payment_unchanged_moves_nothing_twice_over(admin_client, db):
    create_payment(admin_client, _customer(), currency="uzs",
                   amount="12345678", exchange_rate="12345")
    payment = CustomerPayment.objects.get()
    before = money_snapshot(payment)

    for round_trip in range(2):
        form = admin_client.get(
            f"/customer-payments/{payment.pk}/edit/").context["form"]
        resp = admin_client.post(f"/customer-payments/{payment.pk}/edit/",
                                 rendered_payload(form))
        assert resp.status_code == 302
        payment.refresh_from_db()
        assert money_snapshot(payment) == before, f"drifted on re-save {round_trip + 1}"


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_editing_only_the_note_on_a_som_expense_leaves_the_money_alone(
        admin_client, db):
    shipment = _shipment()
    create_expense(admin_client, shipment, amount="24690000",
                   currency="uzs", exchange_rate="12345")
    expense = ShipmentExpense.objects.get()
    before = money_snapshot(expense)
    assert before == (UZS, Decimal("2000.00"), Decimal("24690000.00"), Decimal("12345.00"))

    form = admin_client.get(f"/expenses/{expense.pk}/edit/").context["form"]
    resp = admin_client.post(f"/expenses/{expense.pk}/edit/",
                             rendered_payload(form, note="tekshirildi"))
    assert resp.status_code == 302
    expense.refresh_from_db()
    assert expense.note == "tekshirildi"
    assert money_snapshot(expense) == before


def rendered_formset_payload(formset):
    """The POST body for a re-submitted Mahsulotlar formset, taken from the forms
    exactly as they render (management form included)."""
    data = {}
    for name in formset.management_form.fields:
        data[f"{formset.prefix}-{name}"] = str(formset.management_form[name].value())
    for index, form in enumerate(formset.forms):
        for name in form.fields:
            value = form[name].value()
            if value is None or value is False:
                value = ""
            elif value is True:
                value = "on"
            elif hasattr(value, "pk"):
                value = value.pk
            data[f"{formset.prefix}-{index}-{name}"] = str(value)
    return data


def _som_priced_contract():
    contract = make_contract(kg="1000", price="0.8140", currency=UZS)
    line = contract.lines.get()
    line.price_uzs = Decimal("9768")
    line.exchange_rate = Decimal("12000")
    line.save()
    line.refresh_from_db()
    return contract, line


def _post_contract_edit(client, contract, **line_overrides):
    resp = client.get(f"/contracts/{contract.pk}/edit/")
    # Nechta mashina is a column of the product rows now, so it rides along in
    # `rendered_formset_payload` rather than needing a line of its own here.
    payload = {"partner": contract.partner_id, "currency": contract.currency,
               "created": "2026-07-01", "note": ""}
    formset = resp.context["lines"]
    payload.update(rendered_formset_payload(formset))
    for key, value in line_overrides.items():
        payload[f"{formset.prefix}-0-{key}"] = str(value)
    return client.post(f"/contracts/{contract.pk}/edit/", payload)


def test_an_untouched_kelishuv_narx_row_is_not_rewritten_at_all(admin_client, db):
    """The formset only writes rows that changed, so re-saving a kelishuv with
    nothing touched leaves the narx alone. This is what hides the defect below —
    not a guard against it."""
    contract, line = _som_priced_contract()
    before = money_snapshot(line)
    assert before == (UZS, Decimal("0.8140"), Decimal("9768.00"), Decimal("12000.00"))

    assert _post_contract_edit(admin_client, contract).status_code == 302
    line.refresh_from_db()
    assert money_snapshot(line) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_editing_the_kg_of_a_som_kelishuv_row_leaves_its_narx_alone(admin_client, db):
    contract, line = _som_priced_contract()
    before = money_snapshot(line)

    assert _post_contract_edit(admin_client, contract, kg="1200").status_code == 302
    line.refresh_from_db()
    assert line.kg == Decimal("1200.000")
    assert money_snapshot(line) == before


def test_changing_only_the_kurs_re_derives_only_the_untyped_side(admin_client, db):
    """The kurs is part of what was typed, so moving it is a real edit — but it
    must move only the DERIVED column. A dollar row keeps its dollars."""
    create_payment(admin_client, _customer(), currency="usd",
                   amount="1000", exchange_rate="12000")
    payment = CustomerPayment.objects.get()

    form = admin_client.get(f"/customer-payments/{payment.pk}/edit/").context["form"]
    resp = admin_client.post(f"/customer-payments/{payment.pk}/edit/",
                             rendered_payload(form, exchange_rate="13000"))
    assert resp.status_code == 302
    payment.refresh_from_db()
    assert payment.amount == Decimal("1000.00")           # typed side untouched
    assert payment.amount_uzs == Decimal("13000000.00")   # derived side follows


def test_a_zero_or_negative_or_blank_sum_is_refused_by_the_form(admin_client, db):
    customer = _customer()
    for bad in ["0", "-100", ""]:
        resp = admin_client.post("/customer-payments/new/",
                                 payment_rows({"amount": bad}, customer=customer))
        assert resp.status_code == 200, f"{bad!r} was accepted"
    assert not CustomerPayment.objects.exists()


def test_a_blank_or_zero_kurs_is_refused_by_the_form(admin_client, db):
    customer = _customer()
    for bad in ["", "0"]:
        resp = admin_client.post("/customer-payments/new/", payment_rows(
            {"currency": "uzs", "amount": "12000000", "exchange_rate": bad},
            customer=customer))
        assert resp.status_code == 200, f"kurs {bad!r} was accepted"
    assert not CustomerPayment.objects.exists()


# ── (d) AGGREGATE CONSISTENCY across mixed currencies and mixed kursi ───────

def test_the_kassa_total_is_the_sum_of_its_parts_across_mixed_rows(admin_client, db):
    """Three kirim rows in two currencies at three different kursi, one chiqim.
    Both headline figures must equal the sum of the rows behind them — and the
    so'm figure must be what was actually banked, never the dollar total re-rated."""
    customer = _customer()
    for kw in [{"currency": "usd", "amount": "1000", "exchange_rate": "12000"},
               {"currency": "uzs", "amount": "12650000", "exchange_rate": "12650"},
               {"currency": "uzs", "amount": "999999", "exchange_rate": "13000",
                "method": "transfer", "fee_percent": "2"}]:
        create_payment(admin_client, customer, **kw)
    resp = admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "uzs", "amount": "6500000", "exchange_rate": "13000",
                               "commission_percent": "", "method": "cash",
                               "fee_percent": "0", "note": ""},
                              contract=make_contract(kg="100000", price="1.00").pk,
                              date="2026-07-02"))
    assert resp.status_code == 302

    kirim = list(CustomerPayment.objects.all())
    chiqim = list(SupplierPayment.objects.all())
    expected = (sum(p.net_amount for p in kirim)
                - sum(p.total_out for p in chiqim))
    expected_uzs = (sum(p.net_amount_uzs for p in kirim)
                    - sum(p.total_out_uzs for p in chiqim))

    page = admin_client.get("/kassa/?davr=all")
    assert page.context["cash_total"] == expected
    assert page.context["cash_total_uzs"] == expected_uzs
    # …and the so'm figure is genuinely per-row, not the dollar total × one kurs
    assert expected_uzs != (expected * Decimal("12000")).quantize(Decimal("0.01"))


def test_deleting_a_row_takes_exactly_its_own_money_out_of_the_total(admin_client, db):
    """A total must survive the removal of one of its parts: what the page loses
    has to be that row's own pair, not a re-rated approximation of it."""
    customer = _customer()
    create_payment(admin_client, customer, currency="usd", amount="1000",
                   exchange_rate="12000")
    create_payment(admin_client, customer, currency="uzs", amount="12650000",
                   exchange_rate="12650")
    before = admin_client.get("/kassa/?davr=all").context
    doomed = CustomerPayment.objects.get(currency=UZS)
    lost, lost_uzs = doomed.net_amount, doomed.net_amount_uzs

    assert admin_client.post(
        f"/customer-payments/{doomed.pk}/delete/").status_code == 302
    after = admin_client.get("/kassa/?davr=all").context
    assert after["cash_total"] == before["cash_total"] - lost
    assert after["cash_total_uzs"] == before["cash_total_uzs"] - lost_uzs


def test_a_som_sotuv_totals_in_the_currency_it_was_agreed_in(admin_client, db):
    """The per-kg mixin end of the same rule: kg × the typed narx, in the typed
    currency, with the dollar side derived once."""
    lot = make_shipment(kg="10000", contract=make_contract(kg="10000"),
                        status=ShipmentStatus.arrival(), sent="2026-07-05",
                        eta="2026-07-15", arrived="2026-07-16").lines.first()
    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer().pk, "brand": lot.brand, "kg": "1000",
        "currency": "uzs", "price": "14040", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": ""})
    assert resp.status_code == 302
    sale = Sale.objects.get()
    assert sale.price_uzs == Decimal("14040.00") and sale.price == Decimal("1.1700")
    assert sale.total_uzs == sale.kg * sale.price_uzs
    assert sale.total == Decimal("1170.00")
