"""QA audit: Mijoz to'lovlari + PaymentAllocation reconciliation.

Probes the four symptom families the product owner reported, against the real
views/forms:

  (a) round-trip   — the typed side must be bit-exact, the other derived once
  (b) no-drift     — re-saving a to'lov unchanged must move no money figure
  (c) stickiness   — currency=uzs must SAVE and RE-RENDER as uzs
  (d) aggregates   — Σ allocations must equal the parts, across mixed kursi

Every test here either passes, or is marked xfail with a BUG: reason that names
the defect it documents.
"""
from decimal import Decimal

import pytest
from django.db.models import Sum

from conftest import payment_rows
from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, Partner,
    PaymentAllocation, Sale, Shipment, ShipmentLine, ShipmentStatus,
    customer_advance_total, reconcile_customer_allocations,
    unspent_payment_amount,
)


# --- fixtures/helpers, same shape as tests/test_customer_payments.py ---------

def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="100000", brand="LLDPE", contract_price="0.50"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand, kg=Decimal(kg),
                                price=Decimal(contract_price))
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        eta="2026-07-15", arrived="2026-07-16", transport="01A111AA",
        container="MSCU-1")
    return ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg))


def _sale(customer, lot, kg, price, date, rate="12000"):
    """A dollar sotuv."""
    return Sale.objects.create(
        customer=customer, line=lot, kg=Decimal(kg), price=Decimal(price),
        price_uzs=Decimal(price) * Decimal(rate), currency=Currency.USD,
        exchange_rate=Decimal(rate), date=date)


def _som_sale(customer, lot, kg, price_uzs, rate, date):
    """A sotuv agreed in so'm: the so'm narx is exact, the dollar side derived at
    the sotuv's own kurs and quantised to the 4dp price quantum."""
    price_uzs, rate = Decimal(price_uzs), Decimal(rate)
    return Sale.objects.create(
        customer=customer, line=lot, kg=Decimal(kg),
        price=(price_uzs / rate).quantize(Decimal("0.0001")), price_uzs=price_uzs,
        currency=Currency.UZS, exchange_rate=rate, date=date)


def _create_payment(admin_client, customer, **row):
    """One to'lov through the real create view. Returns the saved row."""
    before = set(CustomerPayment.objects.values_list("pk", flat=True))
    resp = admin_client.post("/customer-payments/new/",
                             payment_rows(row, customer=customer))
    assert resp.status_code == 302, resp.status_code
    return CustomerPayment.objects.exclude(pk__in=before).get()


def _rendered_payload(admin_client, payment):
    """Exactly what the edit modal puts in the boxes — i.e. what an operator
    re-submits when they open the form and press Saqlash without touching it."""
    resp = admin_client.get(f"/customer-payments/{payment.pk}/edit/")
    assert resp.status_code == 200
    form = resp.context["form"]
    payload = {}
    for name in form.fields:
        value = form[name].value()
        payload[name] = "" if value is None else str(value)
    return payload


def _money(payment):
    payment.refresh_from_db()
    return (payment.currency, payment.amount, payment.amount_uzs,
            payment.exchange_rate)


def _alloc_sum(**filters):
    return (PaymentAllocation.objects.filter(**filters)
            .aggregate(s=Sum("amount"))["s"] or Decimal("0"))


# =============================================================================
# (a) ROUND-TRIP — the typed side is stored exact, the other derived once
# =============================================================================

def test_roundtrip_the_typed_side_is_bit_exact_in_both_directions(admin_client, db):
    """Type it in so'm, then type it in dollars, at the same ugly kurs. Whichever
    side was typed is the agreed figure and must be stored verbatim; only the
    other side is derived."""
    som_payer, usd_payer = _customer("So'm to'lovchi"), _customer("Dollar to'lovchi")

    som = _create_payment(admin_client, som_payer, currency="uzs",
                          amount="1265000", exchange_rate="12650")
    assert som.currency == Currency.UZS
    assert som.amount_uzs == Decimal("1265000.00")         # typed, untouched
    assert som.amount == Decimal("100.00")                 # derived
    assert som.exchange_rate == Decimal("12650")

    usd = _create_payment(admin_client, usd_payer, currency="usd",
                          amount="100", exchange_rate="12650")
    assert usd.currency == Currency.USD
    assert usd.amount == Decimal("100.00")                 # typed, untouched
    assert usd.amount_uzs == Decimal("1265000.00")         # derived


def test_roundtrip_som_is_never_re_derived_from_its_own_dollar_side(admin_client, db):
    """The whole point of storing both sides: 1 000 000 so'm at 12 345 is $81.00
    on the dollar column, but $81.00 back at 12 345 is 999 945 so'm. The stored
    so'm value must be the 1 000 000 that was actually handed over."""
    customer = _customer()
    payment = _create_payment(admin_client, customer, currency="uzs",
                              amount="1000000", exchange_rate="12345")
    assert payment.amount == Decimal("81.00")
    assert payment.amount_uzs == Decimal("1000000.00")
    assert payment.amount_uzs != payment.amount * payment.exchange_rate


def test_roundtrip_refuses_a_bad_kurs_or_a_bad_amount(admin_client, db):
    """Boundaries. Money with no kurs has only one of its two values and could
    never join a total of the other currency, so convert_pair refuses — and the
    form must refuse before it ever gets there. Same for zero/negative/blank."""
    customer = _customer()
    for rate in ("0", "", "-1"):
        resp = admin_client.post(
            "/customer-payments/new/",
            payment_rows({"currency": "uzs", "amount": "1265000",
                          "exchange_rate": rate}, customer=customer),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        assert resp.status_code == 422, f"kurs={rate!r} was accepted"
    for amount in ("0", "-500", ""):
        resp = admin_client.post(
            "/customer-payments/new/",
            payment_rows({"amount": amount}, customer=customer),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        assert resp.status_code == 422, f"amount={amount!r} was accepted"
    assert not CustomerPayment.objects.exists()


def test_roundtrip_at_a_tiny_kurs(admin_client, db):
    """A kurs below 1 is nonsense for so'm/$ but must still convert cleanly
    rather than blow up or zero a side."""
    payment = _create_payment(admin_client, _customer(), currency="usd",
                              amount="100", exchange_rate="0.01")
    assert payment.amount == Decimal("100.00")
    assert payment.amount_uzs == Decimal("1.00")


def test_a_row_whose_dollar_column_rounds_to_zero_is_worth_nothing_anywhere(admin_client, db):
    """Risk boundary, at the far end of the kurs range: once the DERIVED dollar
    column quantises to 0.00, the row is worth nothing on the so'm screens too —
    `net_amount_uzs`, `uzs_slice` and `PaymentAllocation.amount_uzs` are all
    computed as a FRACTION of `amount`, and every one of them short-circuits to
    zero when `amount` is falsy. The so'm column still holds the real figure."""
    payment = _create_payment(admin_client, _customer(), currency="uzs",
                              amount="1000000", exchange_rate="9999999999.99")
    assert payment.amount_uzs == Decimal("1000000.00")   # the so'm side is intact
    assert payment.amount == Decimal("0.00")             # the dollar side rounded away
    assert payment.net_amount == Decimal("0")
    assert payment.net_amount_uzs == Decimal("0")        # ...and so is its so'm twin
    assert unspent_payment_amount(payment) == Decimal("0")
    assert customer_advance_total() == (Decimal("0"), Decimal("0"))


# =============================================================================
# (b) IDEMPOTENCE / NO-DRIFT — a re-save must move nothing
# =============================================================================

def _resave_unchanged_twice_then_touch_the_note(admin_client, payment):
    """Open the edit modal and press Saqlash without touching a box — twice —
    then once more having changed only the Izoh. No money figure may move."""
    before = _money(payment)
    for attempt in ("first re-save", "second re-save"):
        resp = admin_client.post(f"/customer-payments/{payment.pk}/edit/",
                                 _rendered_payload(admin_client, payment))
        assert resp.status_code == 302
        assert _money(payment) == before, f"money drifted on the {attempt}"

    payload = _rendered_payload(admin_client, payment)
    payload["note"] = "kvitansiya raqami"
    assert admin_client.post(
        f"/customer-payments/{payment.pk}/edit/", payload).status_code == 302
    payment.refresh_from_db()
    assert payment.note == "kvitansiya raqami"
    assert _money(payment) == before, "money drifted on an Izoh-only edit"


def test_resaving_a_dollar_payment_moves_nothing(admin_client, db):
    """The control case — it holds, which is what isolates the defect below to
    the so'm side rather than to the edit view in general."""
    payment = _create_payment(admin_client, _customer(), currency="usd",
                              amount="1000", exchange_rate="12650")
    _resave_unchanged_twice_then_touch_the_note(admin_client, payment)


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_payment_moves_nothing(admin_client, db):
    payment = _create_payment(admin_client, _customer(), currency="uzs",
                              amount="1265000", exchange_rate="12650")
    assert payment.amount_uzs == Decimal("1265000.00")
    _resave_unchanged_twice_then_touch_the_note(admin_client, payment)


@pytest.mark.xfail(reason="BUG: customer_payment_edit deletes ALL allocations and "
                          "re-runs FIFO with no picks, so a manual Taqsimlash choice "
                          "is silently thrown away by an edit that only touched the "
                          "Izoh — the money jumps to another sotuv",
                   strict=False)
def test_a_manual_pick_survives_an_unrelated_edit(admin_client, db):
    """The operator deliberately put the money on the NEWER sotuv (the older one
    is disputed). Fixing a typo in the Izoh must not move it."""
    customer = _customer()
    lot = _lot()
    older = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    newer = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    resp = admin_client.post("/customer-payments/new/", {
        **payment_rows({"amount": "2000"}, customer=customer),
        f"alloc_{newer.pk}": "2000",
    })
    assert resp.status_code == 302
    payment = CustomerPayment.objects.get()
    assert _alloc_sum(sale=newer) == Decimal("2000.00")
    assert _alloc_sum(sale=older) == Decimal("0")

    payload = _rendered_payload(admin_client, payment)
    payload["note"] = "kvitansiya raqami"
    assert admin_client.post(
        f"/customer-payments/{payment.pk}/edit/", payload).status_code == 302

    assert _alloc_sum(sale=newer) == Decimal("2000.00")
    assert _alloc_sum(sale=older) == Decimal("0")


def test_reconcile_is_idempotent_and_never_double_places_money(admin_client, db):
    """The sweep is called after every to'lov/sotuv change; running it repeatedly
    must not keep stacking allocations on the same sotuv."""
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "1000", "1.00", "2026-07-17")
    _create_payment(admin_client, customer, amount="600")

    snapshots = []
    for _ in range(3):
        reconcile_customer_allocations(customer)
        sale.refresh_from_db()
        snapshots.append((_alloc_sum(sale=sale), sale.remaining))
    assert snapshots == [(Decimal("600.00"), Decimal("400.00"))] * 3


# =============================================================================
# (c) CURRENCY STICKINESS
# =============================================================================

def test_a_som_row_saves_as_som_with_the_typed_som_figure(admin_client, db):
    """Symptom 4, on the create path: currency=uzs must stick and the so'm column
    must hold the typed figure, not a USD-interpreted one."""
    customer = _customer()
    payment = _create_payment(admin_client, customer, currency="uzs",
                              amount="12650000", exchange_rate="12650")
    assert payment.currency == Currency.UZS
    assert payment.is_som is True
    assert payment.amount_uzs == Decimal("12650000.00")
    assert payment.amount == Decimal("1000.00")


def test_switching_a_payment_to_som_sticks_and_rerenders_bound_to_som(admin_client, db):
    """The reported wording — "I change the currency but it stays set to a
    different currency". The picker itself does stick."""
    customer = _customer()
    payment = _create_payment(admin_client, customer, currency="usd",
                              amount="1000", exchange_rate="12000")

    payload = _rendered_payload(admin_client, payment)
    payload.update({"currency": "uzs", "amount": "12650000",
                    "exchange_rate": "12650"})
    assert admin_client.post(
        f"/customer-payments/{payment.pk}/edit/", payload).status_code == 302

    payment.refresh_from_db()
    assert payment.currency == Currency.UZS
    assert payment.amount_uzs == Decimal("12650000.00")
    assert payment.amount == Decimal("1000.00")

    resp = admin_client.get(f"/customer-payments/{payment.pk}/edit/")
    assert resp.context["form"]["currency"].value() == Currency.UZS


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_form_shows_the_figure_the_operator_typed(admin_client, db):
    customer = _customer()
    payment = _create_payment(admin_client, customer, currency="uzs",
                              amount="12650000", exchange_rate="12650")
    form = admin_client.get(
        f"/customer-payments/{payment.pk}/edit/").context["form"]
    assert form["currency"].value() == Currency.UZS
    assert Decimal(str(form["amount"].value())) == Decimal("12650000.00")


def test_each_row_of_one_settlement_keeps_its_own_currency_and_kurs(admin_client, db):
    """A 10 000$ settlement half in naqd dollars, half in so'm at another kurs.
    Neither row may be re-read in the other's currency."""
    customer = _customer()
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "5000", "exchange_rate": "12000"},
        {"currency": "uzs", "amount": "63250000", "exchange_rate": "12650"},
        customer=customer))
    assert resp.status_code == 302
    usd_row, uzs_row = CustomerPayment.objects.order_by("id")
    assert (usd_row.currency, usd_row.amount, usd_row.amount_uzs) == (
        Currency.USD, Decimal("5000.00"), Decimal("60000000.00"))
    assert (uzs_row.currency, uzs_row.amount, uzs_row.amount_uzs) == (
        Currency.UZS, Decimal("5000.00"), Decimal("63250000.00"))


# =============================================================================
# (d) AGGREGATE CONSISTENCY + reconciliation invariants
# =============================================================================

def test_allocations_never_exceed_the_payment_or_the_sale(admin_client, db):
    """The two hard invariants, under a settlement that mixes currencies, kursi
    and a perechisleniya foiz."""
    customer = _customer()
    lot = _lot()
    sales = [
        _sale(customer, lot, "1000", "1.00", "2026-07-10"),
        _som_sale(customer, lot, "500", "12000", "12000", "2026-07-11"),
        _sale(customer, lot, "2000", "1.5", "2026-07-12", rate="12650"),
    ]
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "1500", "exchange_rate": "12000"},
        {"currency": "uzs", "amount": "25300000", "exchange_rate": "12650"},
        {"currency": "usd", "amount": "1000", "method": "transfer",
         "fee_percent": "2", "exchange_rate": "12500"},
        customer=customer))
    assert resp.status_code == 302

    for payment in CustomerPayment.objects.all():
        assert _alloc_sum(payment=payment) <= payment.net_amount, payment.pk
        assert unspent_payment_amount(payment) >= 0
    for sale in sales:
        sale.refresh_from_db()
        assert _alloc_sum(sale=sale) <= sale.net_total, sale.pk
        assert sale.remaining >= 0


def test_customer_balance_equals_sales_minus_allocations_plus_advance(admin_client, db):
    """Qarzlar and the Sotuvlar list read the same money two different ways:
    balance walks net_amount, a sotuv's qoldiq walks allocations. They must
    reconcile exactly — Σ remaining − Σ unspent == balance."""
    customer = _customer()
    lot = _lot()
    _sale(customer, lot, "1000", "1.00", "2026-07-10")
    _sale(customer, lot, "2000", "1.00", "2026-07-11")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "1200", "exchange_rate": "12000"},
        {"currency": "uzs", "amount": "24000000", "exchange_rate": "12000"},
        customer=customer))

    remaining = sum((s.remaining for s in customer.sales.all()), Decimal("0"))
    unspent = sum((unspent_payment_amount(p)
                   for p in customer.customer_payments.all()), Decimal("0"))
    assert remaining - unspent == customer.balance


def test_a_payment_larger_than_the_debt_becomes_an_advance(admin_client, db):
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "1000", "1.00", "2026-07-10")
    payment = _create_payment(admin_client, customer, amount="2500")

    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")
    assert _alloc_sum(payment=payment) == Decimal("1000.00")
    assert unspent_payment_amount(payment) == Decimal("1500.00")
    assert customer.balance == Decimal("-1500.00")
    advance_usd, advance_uzs = customer_advance_total()
    assert advance_usd == Decimal("1500.00")
    assert advance_uzs == Decimal("18000000.00")


def test_a_perechisleniya_only_settles_its_net(admin_client, db):
    """1000 sent at 2% pays off 980 of the qarz; the 20 never reached us."""
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "1000", "1.00", "2026-07-10")
    payment = _create_payment(admin_client, customer, amount="1000",
                              method="transfer", fee_percent="2",
                              exchange_rate="12000")
    sale.refresh_from_db()
    assert payment.net_amount == Decimal("980.00")
    assert _alloc_sum(payment=payment) == Decimal("980.00")
    assert sale.remaining == Decimal("20.00")
    assert customer.balance == Decimal("20.00")


def test_allocation_som_slices_sum_back_to_the_payments_som_value(admin_client, db):
    """An allocation's so'm worth is a slice of its parent's stored so'm value, so
    the slices plus the unspent remainder must add back up to the net — at most a
    tiyin of quantisation apart."""
    customer = _customer()
    lot = _lot()
    _sale(customer, lot, "333", "1.00", "2026-07-10")
    _sale(customer, lot, "333", "1.00", "2026-07-11")
    _sale(customer, lot, "333", "1.00", "2026-07-12")
    payment = _create_payment(admin_client, customer, currency="uzs",
                              amount="12345678", exchange_rate="12345")

    slices = sum((a.amount_uzs for a in payment.allocations.all()), Decimal("0"))
    unspent_uzs = (payment.amount_uzs * unspent_payment_amount(payment)
                   / payment.amount)
    assert abs(slices + unspent_uzs - payment.net_amount_uzs) <= Decimal("0.02")


def test_editing_a_payment_down_below_what_is_allocated_drops_the_excess(admin_client, db):
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "3000", "1.00", "2026-07-10")
    payment = _create_payment(admin_client, customer, amount="3000")
    assert _alloc_sum(payment=payment) == Decimal("3000.00")

    payload = _rendered_payload(admin_client, payment)
    payload["amount"] = "1000"
    assert admin_client.post(
        f"/customer-payments/{payment.pk}/edit/", payload).status_code == 302

    payment.refresh_from_db()
    sale.refresh_from_db()
    assert payment.amount == Decimal("1000.00")
    assert _alloc_sum(payment=payment) == Decimal("1000.00")
    assert _alloc_sum(payment=payment) <= payment.net_amount
    assert sale.remaining == Decimal("2000.00")


def test_deleting_a_sale_returns_its_allocations_to_the_other_sotuvlar(admin_client, db):
    """The sotuv others depend on: its allocations CASCADE away, and that money
    must land back on the mijoz's still-open sotuvlar, not vanish."""
    customer = _customer()
    lot = _lot()
    older = _sale(customer, lot, "1000", "1.00", "2026-07-10")
    newer = _sale(customer, lot, "1000", "1.00", "2026-07-11")
    payment = _create_payment(admin_client, customer, amount="1000")
    assert _alloc_sum(sale=older) == Decimal("1000.00")

    assert admin_client.post(f"/sales/{older.pk}/delete/").status_code == 302

    newer.refresh_from_db()
    assert not Sale.objects.filter(pk=older.pk).exists()
    assert _alloc_sum(payment=payment) == Decimal("1000.00")
    assert _alloc_sum(sale=newer) == Decimal("1000.00")
    assert newer.remaining == Decimal("0")
    assert unspent_payment_amount(payment) == Decimal("0")


def test_deleting_the_payment_reopens_the_sotuv_and_lets_other_avans_in(admin_client, db):
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "1000", "1.00", "2026-07-10")
    first = _create_payment(admin_client, customer, amount="1000")
    second = _create_payment(admin_client, customer, amount="400")
    assert unspent_payment_amount(second) == Decimal("400.00")

    assert admin_client.post(
        f"/customer-payments/{first.pk}/delete/").status_code == 302

    sale.refresh_from_db()
    assert sale.remaining == Decimal("600.00")           # 400 of avans stepped in
    assert _alloc_sum(sale=sale) == Decimal("400.00")
    assert unspent_payment_amount(second) == Decimal("0")


@pytest.mark.xfail(reason="BUG: a so'm sotuv settled with exactly the so'm qoldiq "
                          "the screen shows keeps a residue, because the sotuv's "
                          "dollar column is kg x a 4dp-rounded $/kg while the pick "
                          "converts the so'm total at the same kurs — the two "
                          "disagree by up to kg x 0.00005$, so the mijoz stays in "
                          "Qarzlar for a debt they paid to the tiyin",
                   strict=False)
def test_a_som_sotuv_paid_with_its_exact_som_qoldiq_closes(admin_client, db):
    customer = _customer()
    lot = _lot()
    sale = _som_sale(customer, lot, "24000", "12345", "12650", "2026-07-10")
    # what the Taqsimlash table prints beside the box
    shown_uzs = sale.remaining_uzs
    assert shown_uzs == Decimal("296280000.00")

    resp = admin_client.post("/customer-payments/new/", {
        **payment_rows({"currency": "uzs", "amount": str(shown_uzs),
                        "exchange_rate": "12650"}, customer=customer),
        f"alloc_{sale.pk}": str(shown_uzs),
    })
    assert resp.status_code == 302

    sale.refresh_from_db()
    assert customer.balance_uzs == Decimal("0")      # in so'm the mijoz is square
    assert sale.remaining == Decimal("0"), (
        f"residue of {sale.remaining}$ on a fully paid so'm sotuv")
    assert sale.is_paid


def test_a_som_pick_is_capped_by_the_sotuv_and_by_the_payment(admin_client, db):
    """Over-allocation probe: type a wildly too-large so'm figure into the
    Taqsimlash box. Neither cap may be breached."""
    customer = _customer()
    lot = _lot()
    sale = _som_sale(customer, lot, "1000", "12000", "12000", "2026-07-10")
    resp = admin_client.post("/customer-payments/new/", {
        **payment_rows({"currency": "usd", "amount": "400",
                        "exchange_rate": "12000"}, customer=customer),
        f"alloc_{sale.pk}": "999999999",          # far more than either side
    })
    assert resp.status_code == 302
    payment = CustomerPayment.objects.get()
    sale.refresh_from_db()
    assert _alloc_sum(sale=sale) == Decimal("400.00")
    assert _alloc_sum(payment=payment) <= payment.net_amount
    assert sale.remaining == Decimal("600.00")


def test_a_return_on_a_paid_sotuv_frees_the_excess_instead_of_over_capping(admin_client, db):
    """A qaytarish shrinks net_total under what is already allocated. The excess
    must be trimmed — Σ allocations ≤ net_total — and the freed money must reach
    the mijoz's other open sotuv rather than strand in a dead over-cap row."""
    customer = _customer()
    lot = _lot()
    paid = _sale(customer, lot, "1000", "1.00", "2026-07-10")
    other = _sale(customer, lot, "1000", "1.00", "2026-07-11")
    payment = _create_payment(admin_client, customer, amount="1000")
    assert _alloc_sum(sale=paid) == Decimal("1000.00")

    resp = admin_client.post(f"/returns/new/?sale={paid.pk}", {
        "sale": paid.pk, "kg": "400", "currency": "usd", "price": "1.00",
        "exchange_rate": "12000", "date": "2026-07-12", "restock": "on", "note": "",
    })
    assert resp.status_code == 302

    paid.refresh_from_db()
    other.refresh_from_db()
    assert paid.net_total == Decimal("600.00")
    assert _alloc_sum(sale=paid) <= paid.net_total          # the hard cap holds
    assert _alloc_sum(sale=paid) == Decimal("600.00")
    assert _alloc_sum(sale=other) == Decimal("400.00")      # freed money re-homed
    assert _alloc_sum(payment=payment) == payment.net_amount
    assert unspent_payment_amount(payment) == Decimal("0")


def test_a_pick_for_another_mijoz_sale_is_ignored_not_mis_applied(admin_client, db):
    """A stale/tampered pick id belonging to somebody else must not move money
    across mijozlar; the to'lov FIFOs onto its own owner's sotuv instead."""
    payer = _customer("To'lovchi")
    stranger = _customer("Begona")
    lot = _lot()
    mine = _sale(payer, lot, "1000", "1.00", "2026-07-10")
    theirs = _sale(stranger, lot, "1000", "1.00", "2026-07-10")

    resp = admin_client.post("/customer-payments/new/", {
        **payment_rows({"amount": "1000"}, customer=payer),
        f"alloc_{theirs.pk}": "1000",
    })
    assert resp.status_code == 302
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert _alloc_sum(sale=theirs) == Decimal("0")
    assert mine.remaining == Decimal("0")
