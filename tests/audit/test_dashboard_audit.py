"""Dashboard audit — diagnosis only, no fixes.

Every headline the dashboard prints is cross-checked against a value rebuilt
independently from the ORM in the test, on a data set that deliberately mixes
currencies and kurslar and includes a cancelled bron, a deleted sotuv, a
fully-paid and an over-paid mijoz, and a yuk with no product lines.

Probe families, mapped onto the reported symptoms:
  (a) round-trip   — the side the operator typed survives exact
  (b) idempotence  — re-saving unchanged through the REAL view must move nothing
  (c) stickiness   — a so'm row stays a so'm row, in the DB and on the edit form
  (d) aggregates   — every KPI equals the sum of its parts, in USD and in so'm

Tests marked xfail carry a BUG: reason and are the findings. Every xfail here has
been triaged against the application source and its docstrings: the two so'm-edit
ones are the already-established MoneyEntryFormMixin defect (root cause proven in
tests/audit/test_som_edit_dataloss.py) surfacing on this board, and the third is the
dashboard netting a negative payable_left into 'Hamkor qarzi'. Nothing was left as a
plain failure and no marker was kept without a named file:line and a concrete figure.
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from django.utils import timezone

from conftest import make_contract, make_shipment
from crm.models import (
    ContractLine, Currency, Customer, CustomerPayment, Partner, Reservation,
    Return, Sale, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus,
    SupplierPayment, arrived_lots, brand_on_hand_kg, customer_receivable_total,
    partner_positions, stock_value,
)

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()

#: Everything the dashboard prints as a number, so a whole board can be snapshotted.
FIGURES = ("total_kg", "shipped_kg", "arrived_kg", "stock_kg",
           "paid_total", "paid_total_uzs", "debt_total", "debt_total_uzs",
           "customer_debt_total", "customer_debt_total_uzs",
           "sales_profit_total", "sales_profit_total_uzs")


# --- helpers ---------------------------------------------------------------

def _dash(client):
    resp = client.get("/")
    assert resp.status_code == 200
    return resp


def _figures(client):
    ctx = _dash(client).context
    return {key: Decimal(ctx[key]) for key in FIGURES}


def _months(client):
    return {row["month"]: row for row in _dash(client).context["monthly"]}


def _lot(contract=None, kg="1000", price="1.00", arrived=date(2026, 7, 10),
         sent=date(2026, 7, 1), **kw):
    """An arrived yuk's single product line — the unit the ombor deals in."""
    ship = make_shipment(contract=contract, kg=kg, price=price,
                         status=ShipmentStatus.arrival(), sent=sent, arrived=arrived, **kw)
    return ship.lines.first()


def _customer(name="Olim"):
    return Customer.objects.create(name=name, phone="1", address="T")


def _pay_supplier(contract, amount, uzs, rate="12000", currency=Currency.USD,
                  day="2026-07-05"):
    return SupplierPayment.objects.create(
        contract=contract, date=day, amount=Decimal(amount), amount_uzs=Decimal(uzs),
        currency=currency, exchange_rate=Decimal(rate), method="cash")


def _form_data(form):
    """Exactly what the browser would POST back if the operator opened this form
    and pressed Save without touching a thing."""
    data = {}
    for name in form.fields:
        value = form[name].value()
        data[f"{form.prefix}-{name}" if form.prefix else name] = (
            "" if value is None else str(value))
    return data


def _formset_data(formset):
    data = {
        f"{formset.prefix}-TOTAL_FORMS": str(formset.total_form_count()),
        f"{formset.prefix}-INITIAL_FORMS": str(formset.initial_form_count()),
        f"{formset.prefix}-MIN_NUM_FORMS": "0",
        f"{formset.prefix}-MAX_NUM_FORMS": "1000",
    }
    for form in formset.forms:
        data.update(_form_data(form))
    return data


def _resave(client, url, extra_forms=(), formsets=(), changes=None):
    """Open an edit screen and press Save with what it rendered, optionally editing
    one unrelated field first — which is what an operator actually does."""
    page = client.get(url)
    assert page.status_code == 200
    data = _form_data(page.context["form"])
    for key in extra_forms:
        data.update(_form_data(page.context[key]))
    for key in formsets:
        data.update(_formset_data(page.context[key]))
    data.update(changes or {})
    resp = client.post(url, data)
    assert resp.status_code in (200, 302), resp.status_code
    return resp


# --- (d) the kg KPIs -------------------------------------------------------

def test_kg_kpis_equal_an_independent_orm_walk(admin_client):
    """Kelishilgan / Yuborilgan / Omborga kelgan kg, rebuilt row by row."""
    a = make_contract(brand="LLDPE", kg="1000", price="1.00")
    b = make_contract(brand="HDPE", kg="2500.5", price="0.9")
    make_shipment(contract=a, kg="400", sent=date(2026, 7, 1))
    make_shipment(contract=b, kg="1000.25", sent=date(2026, 7, 2),
                  arrived=date(2026, 7, 9), status=ShipmentStatus.arrival())

    ctx = _dash(admin_client).context
    assert ctx["total_kg"] == sum(
        (ln.kg for ln in ContractLine.objects.all()), Decimal("0"))
    assert ctx["shipped_kg"] == sum(
        (ln.kg for ln in ShipmentLine.objects.all()), Decimal("0"))
    assert ctx["arrived_kg"] == sum(
        (ln.kg for ln in ShipmentLine.objects.filter(shipment__arrived__isnull=False)),
        Decimal("0"))
    assert ctx["arrived_kg"] == Decimal("1000.250")


def test_a_yuk_with_no_product_lines_distorts_nothing(admin_client):
    """An empty truck is a real state (created before the mahsulot rows are known)."""
    contract = make_contract(kg="1000", price="1.00")
    Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(),
                            sent=date(2026, 7, 1), arrived=date(2026, 7, 5))

    ctx = _dash(admin_client).context
    assert ctx["shipped_kg"] == 0 and ctx["arrived_kg"] == 0
    assert ctx["stock_kg"] == Decimal("0")
    # It is still one truck in Yuk holatlari and one arrival in the oylik hisobot.
    assert sum(row["total"] for row in ctx["status_rows"]) == 1
    july = _months(admin_client)[date(2026, 7, 1)]
    assert (july["arrived"], july["kg"], july["value"]) == (1, Decimal("0"), Decimal("0"))


# --- (a)/(d) Jami to'langan ------------------------------------------------

def test_jami_tolangan_sums_both_stored_columns_across_mixed_kurslar(admin_client):
    """Neither side of the pair may be re-derived from the other: the dollar total
    is the sum of the dollar column and the so'm total the sum of the so'm one."""
    contract = make_contract(kg="100000", price="1.00")
    _pay_supplier(contract, "1000", "12000000", rate="12000")                    # typed $
    _pay_supplier(contract, "800", "10400000", rate="13000", currency=Currency.UZS)

    ctx = _dash(admin_client).context
    assert ctx["paid_total"] == Decimal("1800")
    assert ctx["paid_total_uzs"] == Decimal("22400000")
    # 22 400 000 is not 1800 at any single kurs — mixed rates must not collapse.
    assert ctx["paid_total_uzs"] != ctx["paid_total"] * Decimal("12000")
    assert ctx["paid_total_uzs"] != ctx["paid_total"] * Decimal("13000")


def test_hamkor_tolov_typed_in_som_reaches_the_dashboard_exact(admin_client):
    """(a) round-trip through the REAL create view: the so'm the operator typed is
    the so'm the headline shows, to the tiyin, at an awkward kurs."""
    contract = make_contract(kg="100000", price="10.00")
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-05", "currency": "uzs",
        "amount": "12345678", "exchange_rate": "12345.67",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    })
    assert resp.status_code == 302
    payment = SupplierPayment.objects.get()
    assert payment.currency == Currency.UZS
    assert payment.amount_uzs == Decimal("12345678.00")          # typed side exact
    assert payment.amount == (Decimal("12345678") / Decimal("12345.67")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)

    ctx = _dash(admin_client).context
    assert ctx["paid_total_uzs"] == Decimal("12345678.00")
    assert ctx["paid_total"] == payment.amount


def test_hamkor_tolov_typed_in_usd_reaches_the_dashboard_exact(admin_client):
    contract = make_contract(kg="100000", price="10.00")
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-05", "currency": "usd",
        "amount": "1234.56", "exchange_rate": "12345.67",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    })
    assert resp.status_code == 302
    payment = SupplierPayment.objects.get()
    assert payment.amount == Decimal("1234.56")                  # typed side exact
    assert payment.amount_uzs == (Decimal("1234.56") * Decimal("12345.67")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    ctx = _dash(admin_client).context
    assert (ctx["paid_total"], ctx["paid_total_uzs"]) == (payment.amount, payment.amount_uzs)


# --- (b)/(c) idempotence and stickiness through the real edit views ---------

def test_resaving_a_dollar_hamkor_tolov_unchanged_moves_nothing(admin_client):
    """The control case: a row typed in dollars survives an untouched re-save."""
    contract = make_contract(kg="100000", price="10.00")
    admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-05", "currency": "usd",
        "amount": "1000", "exchange_rate": "12500",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    })
    payment = SupplierPayment.objects.get()
    before = _figures(admin_client)

    _resave(admin_client, f"/supplier-payments/{payment.pk}/edit/")
    _resave(admin_client, f"/supplier-payments/{payment.pk}/edit/")

    payment.refresh_from_db()
    assert (payment.amount, payment.amount_uzs) == (Decimal("1000.00"), Decimal("12500000.00"))
    assert _figures(admin_client) == before


# TRIAGE: UPHELD, and it is the ALREADY-ESTABLISHED root cause (proven in
# tests/audit/test_som_edit_dataloss.py) surfacing on the dashboard, not a new defect.
# Regression guard. Was an xfail documenting the so'm-edit defect; it passes since
# MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing its
# so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_hamkor_tolov_unchanged_moves_nothing(admin_client):
    """(b) The 'values change by themselves' report, reproduced end to end."""
    contract = make_contract(kg="100000", price="10.00")
    admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-05", "currency": "uzs",
        "amount": "12500000", "exchange_rate": "12500",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    })
    payment = SupplierPayment.objects.get()
    assert (payment.amount, payment.amount_uzs) == (Decimal("1000.00"), Decimal("12500000.00"))
    before = _figures(admin_client)

    _resave(admin_client, f"/supplier-payments/{payment.pk}/edit/")

    payment.refresh_from_db()
    assert (payment.amount, payment.amount_uzs) == (Decimal("1000.00"), Decimal("12500000.00"))
    assert _figures(admin_client) == before


# NOT xfail, despite the same underlying defect being real elsewhere. A formset row
# that is posted back completely unchanged is never written: Django's has_changed()
# skips it, so the corrupt narx in the box never reaches the database. The damage
# needs the operator to touch SOMETHING on the kelishuv — which is precisely why the
# report is "sometimes the figures move" rather than "always".
# The drift itself is proven in tests/audit/test_contracts_audit.py (edit variants)
# and, at its root, in tests/audit/test_som_edit_dataloss.py.
def test_resaving_a_som_kelishuv_unchanged_moves_nothing(admin_client):
    """(b) Same probe on the other input into 'Hamkor qarzi'."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    resp = admin_client.post("/contracts/new/", {
        "partner": partner.pk, "created": "2026-07-01", "note": "",
        "planned_trucks": "",
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-brand": "LLDPE", "lines-0-kg": "1000",
        "lines-0-currency": "uzs", "lines-0-price": "15000",
        "lines-0-exchange_rate": "12500",
    })
    assert resp.status_code == 302
    line = ContractLine.objects.get()
    assert (line.price, line.price_uzs) == (Decimal("1.2000"), Decimal("15000.00"))
    before = _figures(admin_client)
    assert before["debt_total"] == Decimal("1200.00")
    assert before["debt_total_uzs"] == Decimal("15000000.00")

    _resave(admin_client, f"/contracts/{line.contract_id}/edit/",
            extra_forms=("lines_after",), formsets=("lines",))

    line.refresh_from_db()
    assert (line.price, line.price_uzs) == (Decimal("1.2000"), Decimal("15000.00"))
    assert _figures(admin_client) == before


# TRIAGE: UPHELD — the render half of the SAME established so'm-edit root cause as the
# test above, kept because it pins where the corruption starts (the rendered form),
# which is what a fix has to change. Not an independent finding.
# Regression guard. Was an xfail documenting the so'm-edit defect; it passes since
# MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing its
# so'm figure. Kept as a test so the defect cannot come back.
def test_a_som_row_reopens_with_the_som_figure_in_the_summa_box(admin_client):
    contract = make_contract(kg="100000", price="10.00")
    admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-05", "currency": "uzs",
        "amount": "12500000", "exchange_rate": "12500",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    })
    payment = SupplierPayment.objects.get()

    page = admin_client.get(f"/supplier-payments/{payment.pk}/edit/")
    form = page.context["form"]
    assert form["currency"].value() == Currency.UZS        # the picker is sticky
    assert Decimal(str(form["amount"].value())) == payment.amount_uzs


# --- (d) Hamkor qarzi ------------------------------------------------------

def test_hamkor_qarzi_equals_the_sum_of_each_kelishuv_payable_left(admin_client):
    a = make_contract(kg="1000", price="1.00")
    b = make_contract(kg="2000", price="0.75")
    _pay_supplier(a, "300", "3600000", rate="12000")
    _pay_supplier(b, "500", "6500000", rate="13000", currency=Currency.UZS)

    ctx = _dash(admin_client).context
    assert ctx["debt_total"] == a.payable_left + b.payable_left
    assert ctx["debt_total_uzs"] == a.payable_left_uzs + b.payable_left_uzs
    assert ctx["debt_total"] == Decimal("1000") - Decimal("300") + Decimal("1500") - Decimal("500")


# TRIAGE: UPHELD. Not a documented decision — crm/views.py:54-56 only documents summing
# across every kelishuv rather than only the goods sent; it says nothing about keeping
# negative rows in. The mijoz KPI eleven lines below (crm/views.py:100) drops negative
# balances for precisely this reason, so the same dashboard treats the two sides of the
# ledger differently.
@pytest.mark.xfail(reason="BUG: 'Hamkor qarzi' NETS an over-paid kelishuv against the "
                          "debt owed on unrelated ones, and across DIFFERENT hamkorlar. "
                          "crm/views.py:57-58 sums Contract.payable_left / "
                          "payable_left_uzs over every kelishuv without dropping the "
                          "negative ones. A kelishuv paid $1 000 / 12 000 000 so'm whose "
                          "truck then goes out at half the agreed narx has its "
                          "expected_value fall to $500 (crm/models.py:600 "
                          "ContractLine.expected_value: 'a truck may be priced up or "
                          "down against it'), contributing -500.00 / -6 000 000. The "
                          "headline then reads 500,00 $ / 6 000 000 so'm while 1 000 $ / "
                          "12 000 000 so'm is genuinely owed to another hamkor. "
                          "crm/models.py:1126 customer_receivable_total() filters "
                          "balance > 0 for the mijoz side ('a mijoz sitting in avans "
                          "does not net off another mijoz's qarz') and "
                          "crm/models.py:1144 partner_positions() names this exact "
                          "netting as the thing that 'hid $203 030.5 of prepayment "
                          "behind a $50 480 payable'. The dashboard does neither.",
                   strict=False)
def test_hamkor_qarzi_does_not_net_a_prepaid_kelishuv_against_an_owed_one(admin_client):
    owing = make_contract(kg="1000", price="1.00")            # $1 000 still to pay
    prepaid = make_contract(kg="1000", price="1.00")
    _pay_supplier(prepaid, "1000", "12000000")                # paid in full up front
    # the truck then goes out at half the agreed narx — expected value falls to $500
    make_shipment(contract=prepaid, kg="1000", price="0.50",
                  sent=date(2026, 7, 2))

    # Two different hamkorlar: make_contract mints a Partner per call, so this is not
    # one counterparty's own running account being netted.
    assert owing.partner_id != prepaid.partner_id
    assert prepaid.payable_left == Decimal("-500.00")
    assert prepaid.payable_left_uzs == Decimal("-6000000.00")
    assert owing.payable_left == Decimal("1000.00")
    position = partner_positions()
    ctx = _dash(admin_client).context
    assert ctx["debt_total"] == Decimal("1000.00"), (
        f"netted to {ctx['debt_total']}; kassa reports owed={position['owed']} "
        f"prepaid={position['prepaid']} separately")
    assert ctx["debt_total_uzs"] == Decimal("12000000.00")


# --- (d) Mijoz qarzi -------------------------------------------------------

def test_mijoz_qarzi_drops_settled_and_overpaid_customers(admin_client):
    """A fully-paid and an over-paid mijoz must not net against a real debtor."""
    contract = make_contract(kg="9000", price="0.50")
    lot = _lot(contract=contract, kg="3000", price="0.50")

    owing, settled, overpaid = _customer("Owing"), _customer("Settled"), _customer("Over")
    for customer, paid in ((owing, "0"), (settled, "500"), (overpaid, "800")):
        Sale.objects.create(customer=customer, line=lot, kg=Decimal("500"),
                            price=Decimal("1.0000"), price_uzs=Decimal("12000"),
                            currency=Currency.USD, exchange_rate=Decimal("12000"),
                            date=date(2026, 7, 15))
        if paid != "0":
            CustomerPayment.objects.create(
                customer=customer, date=date(2026, 7, 16), amount=Decimal(paid),
                amount_uzs=Decimal(paid) * 12000, currency=Currency.USD,
                exchange_rate=Decimal("12000"), method="cash")

    assert settled.balance == Decimal("0")
    assert overpaid.balance == Decimal("-300")
    ctx = _dash(admin_client).context
    assert ctx["customer_debt_total"] == Decimal("500.00")     # only the real debtor
    assert ctx["customer_debt_total_uzs"] == Decimal("6000000.00")


def test_mijoz_qarzi_matches_the_kassa_receivable_across_mixed_kurslar(admin_client):
    """Two screens, one figure: the dashboard KPI and the kassa's 'Mijozlar qarzi'
    must agree even when a sotuv and its to'lov were booked at different kurslar."""
    contract = make_contract(kg="9000", price="0.50")
    lot = _lot(contract=contract, kg="4000", price="0.50")

    som_buyer, usd_buyer = _customer("Somchi"), _customer("Dollarchi")
    Sale.objects.create(customer=som_buyer, line=lot, kg=Decimal("1000"),
                        price=Decimal("1.2000"), price_uzs=Decimal("15000"),
                        currency=Currency.UZS, exchange_rate=Decimal("12500"),
                        date=date(2026, 7, 15))
    Sale.objects.create(customer=usd_buyer, line=lot, kg=Decimal("1000"),
                        price=Decimal("1.0000"), price_uzs=Decimal("12000"),
                        currency=Currency.USD, exchange_rate=Decimal("12000"),
                        date=date(2026, 7, 15))
    CustomerPayment.objects.create(
        customer=som_buyer, date=date(2026, 7, 20), amount=Decimal("500"),
        amount_uzs=Decimal("6500000"), currency=Currency.UZS,
        exchange_rate=Decimal("13000"), method="cash")

    total, total_uzs, count = customer_receivable_total()
    ctx = _dash(admin_client).context
    assert (ctx["customer_debt_total"], ctx["customer_debt_total_uzs"]) == (total, total_uzs)
    assert count == 2
    # And it is the sum of the parts, each at its OWN kurs — not a re-rated total.
    expected_uzs = sum((c.balance_uzs for c in Customer.objects.all() if c.balance > 0),
                       Decimal("0"))
    assert ctx["customer_debt_total_uzs"] == expected_uzs == Decimal("20500000.00")


# --- (d) Omborda qoldiq ----------------------------------------------------

def test_omborda_qoldiq_matches_the_ombor_and_the_kassa(admin_client):
    """Sold kg leave the shelf, restocked qaytarish comes back, and the dashboard,
    stock_value() and the per-marka walk must all land on the same kg."""
    contract = make_contract(kg="9000", price="0.50")
    lot = _lot(contract=contract, kg="3000", price="0.50")
    customer = _customer()
    sale = Sale.objects.create(customer=customer, line=lot, kg=Decimal("1200"),
                               price=Decimal("1.0000"), price_uzs=Decimal("12000"),
                               currency=Currency.USD, exchange_rate=Decimal("12000"),
                               date=date(2026, 7, 15))
    Return.objects.create(sale=sale, kg=Decimal("200"), price=Decimal("1.0000"),
                          price_uzs=Decimal("12000"), currency=Currency.USD,
                          exchange_rate=Decimal("12000"), date=date(2026, 7, 18),
                          restock=True)
    Return.objects.create(sale=sale, kg=Decimal("100"), price=Decimal("1.0000"),
                          price_uzs=Decimal("12000"), currency=Currency.USD,
                          exchange_rate=Decimal("12000"), date=date(2026, 7, 19),
                          restock=False)

    ctx = _dash(admin_client).context
    assert ctx["stock_kg"] == Decimal("2000.000")             # 3000 - 1200 + 200
    assert ctx["stock_kg"] == stock_value()[2]
    assert ctx["stock_kg"] == brand_on_hand_kg("LLDPE")
    assert ctx["stock_kg"] == sum((lot.available_kg for lot in arrived_lots()),
                                  Decimal("0"))


def test_a_cancelled_bron_leaves_every_figure_alone(admin_client):
    """A bron is a claim on a marka, not on a lot (crm/models.py:1008), so neither
    an active nor a cancelled one may move a dashboard number."""
    contract = make_contract(kg="9000", price="0.50")
    _lot(contract=contract, kg="3000", price="0.50")
    customer = _customer()
    before = _figures(admin_client)

    Reservation.objects.create(customer=customer, brand="LLDPE", kg=Decimal("500"),
                               price=Decimal("1.2000"), price_uzs=Decimal("15000"),
                               currency=Currency.UZS, exchange_rate=Decimal("12500"),
                               status=Reservation.Status.CANCELLED)
    assert _figures(admin_client) == before

    Reservation.objects.create(customer=customer, brand="LLDPE", kg=Decimal("500"),
                               price=Decimal("1.2000"), price_uzs=Decimal("15000"),
                               currency=Currency.UZS, exchange_rate=Decimal("12500"),
                               status=Reservation.Status.ACTIVE)
    assert _figures(admin_client) == before


def test_deleting_a_sotuv_returns_its_kg_and_its_foyda(admin_client):
    """A row other rows depend on: the lot must go back to what it was."""
    contract = make_contract(kg="9000", price="0.50")
    lot = _lot(contract=contract, kg="3000", price="0.50")
    customer = _customer()
    before = _figures(admin_client)

    sale = Sale.objects.create(customer=customer, line=lot, kg=Decimal("800"),
                               price=Decimal("1.0000"), price_uzs=Decimal("12000"),
                               currency=Currency.USD, exchange_rate=Decimal("12000"),
                               date=date(2026, 7, 15))
    mid = _figures(admin_client)
    assert mid["stock_kg"] == before["stock_kg"] - Decimal("800")
    assert mid["sales_profit_total"] == Decimal("400.00")      # (1.00 - 0.50) * 800

    resp = admin_client.post(f"/sales/{sale.pk}/delete/")
    assert resp.status_code in (200, 302)
    assert not Sale.objects.filter(pk=sale.pk).exists()
    assert _figures(admin_client) == before


# --- (d) Sotuvdan foyda ----------------------------------------------------

def test_sotuvdan_foyda_equals_the_sum_of_each_sotuv_profit(admin_client):
    """Mixed-currency sotuvlar off one lot, with a shipment xarajat blended in."""
    contract = make_contract(kg="9000", price="0.50")
    lot = _lot(contract=contract, kg="3000", price="0.50")
    ShipmentExpense.objects.create(
        shipment=lot.shipment, category="transport", date=date(2026, 7, 11),
        amount=Decimal("300"), amount_uzs=Decimal("3600000"),
        currency=Currency.USD, exchange_rate=Decimal("12000"), method="cash")
    customer = _customer()
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"),
                        price=Decimal("1.0000"), price_uzs=Decimal("12000"),
                        currency=Currency.USD, exchange_rate=Decimal("12000"),
                        date=date(2026, 7, 15))
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("500"),
                        price=Decimal("1.2000"), price_uzs=Decimal("15000"),
                        currency=Currency.UZS, exchange_rate=Decimal("12500"),
                        date=date(2026, 7, 16))

    ctx = _dash(admin_client).context
    assert ctx["sales_profit_total"] == sum((s.profit for s in Sale.objects.all()),
                                            Decimal("0"))
    assert ctx["sales_profit_total_uzs"] == sum((s.profit_uzs for s in Sale.objects.all()),
                                                Decimal("0"))
    # cost = 0.50 narx + 300/3000 freight = 0.60 $/kg
    assert ctx["sales_profit_total"] == Decimal("400.00") + Decimal("300.00")
    # each sotuv's so'm foyda is rated at ITS OWN kurs, never a blended one
    assert ctx["sales_profit_total_uzs"] == (Decimal("400") * 12000
                                             + Decimal("300") * 12500)


# --- (d) Oylik hisobot -----------------------------------------------------

def test_oylik_hisobot_reconciles_with_the_kpi_row(admin_client):
    contract = make_contract(kg="90000", price="1.00")
    _lot(contract=contract, kg="1000", price="1.00",
         sent=date(2026, 6, 20), arrived=date(2026, 7, 2))
    lot = _lot(contract=contract, kg="2000", price="1.00",
               sent=date(2026, 7, 5), arrived=date(2026, 7, 20))
    make_shipment(contract=contract, kg="3000", price="1.00", sent=date(2026, 7, 28))
    customer = _customer()
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("500"),
                        price=Decimal("2.0000"), price_uzs=Decimal("24000"),
                        currency=Currency.USD, exchange_rate=Decimal("12000"),
                        date=date(2026, 7, 25))

    ctx = _dash(admin_client).context
    rows = ctx["monthly"]
    assert sum((r["kg"] for r in rows), Decimal("0")) == ctx["arrived_kg"]
    assert sum((r["sales"] for r in rows), Decimal("0")) == sum(
        (s.net_total for s in Sale.objects.all()), Decimal("0"))
    assert sum((r["profit"] for r in rows), Decimal("0")) == ctx["sales_profit_total"]
    assert sum((r["profit_uzs"] for r in rows), Decimal("0")) == ctx["sales_profit_total_uzs"]
    assert sum((r["sent"] for r in rows)) == Shipment.objects.filter(
        sent__isnull=False).count()
    assert sum((r["arrived"] for r in rows)) == Shipment.objects.filter(
        arrived__isnull=False).count()


def test_oylik_hisobot_month_boundaries_are_not_off_by_one(admin_client):
    """The last day of a month and the first day of the next must land in their own
    rows — a truck that leaves on 31 July and lands on 1 August is one of each."""
    contract = make_contract(kg="90000", price="1.00")
    _lot(contract=contract, kg="100", price="1.00",
         sent=date(2026, 7, 31), arrived=date(2026, 8, 1))
    _lot(contract=contract, kg="200", price="1.00",
         sent=date(2026, 8, 31), arrived=date(2026, 8, 31))
    _lot(contract=contract, kg="400", price="1.00",
         sent=date(2026, 9, 1), arrived=date(2026, 9, 1))

    rows = _months(admin_client)
    assert (rows[date(2026, 7, 1)]["sent"], rows[date(2026, 7, 1)]["arrived"]) == (1, 0)
    assert (rows[date(2026, 8, 1)]["sent"], rows[date(2026, 8, 1)]["arrived"]) == (1, 2)
    assert rows[date(2026, 8, 1)]["kg"] == Decimal("300")     # 100 + 200
    assert (rows[date(2026, 9, 1)]["sent"], rows[date(2026, 9, 1)]["arrived"]) == (1, 1)
    assert rows[date(2026, 9, 1)]["kg"] == Decimal("400")


def test_oylik_hisobot_keeps_the_twelve_newest_months(admin_client):
    """Documented cap (crm/views.py:121). Beyond it the table stops being a
    reconciliation of the KPI row, which is why the KPI test above stays inside it."""
    contract = make_contract(kg="900000", price="1.00")
    for month in range(1, 15):                                # 14 months of arrivals
        year, real = (2025, month) if month <= 12 else (2026, month - 12)
        _lot(contract=contract, kg="100", price="1.00",
             sent=date(year, real, 5), arrived=date(year, real, 6))

    rows = _dash(admin_client).context["monthly"]
    assert len(rows) == 12
    assert rows[0]["month"] == date(2026, 2, 1)               # newest first
    assert rows[-1]["month"] == date(2025, 3, 1)


# --- Kechikkan yuklar ------------------------------------------------------

def test_kechikkan_yuklar_boundary_is_strictly_before_today(admin_client):
    contract = make_contract(kg="90000", price="1.00")
    today = make_shipment(contract=contract, kg="100", eta=TODAY)
    tomorrow = make_shipment(contract=contract, kg="100", eta=TODAY + timedelta(days=1))
    late = make_shipment(contract=contract, kg="100", eta=TODAY - timedelta(days=1))
    landed = make_shipment(contract=contract, kg="100", eta=TODAY - timedelta(days=5),
                           arrived=TODAY, status=ShipmentStatus.arrival())

    overdue = _dash(admin_client).context["overdue"]
    assert [s.pk for s in overdue] == [late.pk]
    assert late.days_late == 1
    assert today.days_left == 0 and tomorrow.days_left == 1
    assert landed.days_late == 0


# --- boundary values -------------------------------------------------------

def test_a_kelishuv_with_no_movement_and_a_zero_paid_row_still_renders(admin_client):
    """Zero, blank and a kelishuv nothing has happened to."""
    contract = make_contract(kg="1000", price="1.00")
    SupplierPayment.objects.create(contract=contract, date="2026-07-05",
                                   amount=Decimal("0"), amount_uzs=Decimal("0"),
                                   currency=Currency.USD, exchange_rate=Decimal("12000"),
                                   method="cash")
    ctx = _dash(admin_client).context
    assert ctx["paid_total"] == Decimal("0") and ctx["paid_total_uzs"] == Decimal("0")
    assert ctx["debt_total"] == Decimal("1000.00")
    assert ctx["stock_kg"] == Decimal("0")
    assert ctx["monthly"] == []                    # nothing sent, nothing sold
    assert ctx["truck_plan_rows"] == []


def test_a_legacy_rateless_row_is_counted_in_dollars_and_missing_in_som(admin_client):
    """rate = 0 rows predate dual currency (crm/models.py:31). They cannot have a
    so'm value, so the so'm headline is SHORT by them while the dollar one is not —
    documented, but it is what makes the two headlines disagree on old data."""
    contract = make_contract(kg="100000", price="1.00")
    SupplierPayment.objects.create(contract=contract, date="2026-07-05",
                                   amount=Decimal("500"), amount_uzs=Decimal("0"),
                                   currency=Currency.USD, exchange_rate=Decimal("0"),
                                   method="cash")
    _pay_supplier(contract, "500", "6000000", rate="12000")

    ctx = _dash(admin_client).context
    assert ctx["paid_total"] == Decimal("1000.00")
    assert ctx["paid_total_uzs"] == Decimal("6000000.00")     # half the rows only


def test_a_huge_and_a_tiny_kurs_both_survive_the_round_trip(admin_client):
    contract = make_contract(kg="1000000", price="1.00")
    admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-05", "currency": "uzs",
        "amount": "999999999.99", "exchange_rate": "999999.99",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    })
    admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-06", "currency": "usd",
        "amount": "100", "exchange_rate": "0.01",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    })
    payments = list(SupplierPayment.objects.order_by("date"))
    assert len(payments) == 2
    assert payments[0].amount_uzs == Decimal("999999999.99")   # typed so'm exact
    assert payments[1].amount == Decimal("100.00")             # typed dollars exact
    assert payments[1].amount_uzs == Decimal("1.00")           # 100 * 0.01

    ctx = _dash(admin_client).context
    assert ctx["paid_total_uzs"] == Decimal("1000000000.99")
    assert ctx["paid_total"] == payments[0].amount + Decimal("100.00")


# --- (a)/(c) the Mijoz qarzi KPI, entered the way an operator enters it ------

def test_a_som_sotuv_reaches_mijoz_qarzi_exact_and_stays_a_som_sotuv(admin_client):
    """(a)+(c) through the REAL sotuv view: the so'm narx the operator typed is the
    so'm the KPI shows, the row stays currency=uzs, and the dollar side is derived
    once at the sotuv's own kurs rather than back-rated from the so'm total."""
    contract = make_contract(kg="9000", price="0.50")
    _lot(contract=contract, kg="3000", price="0.50")
    customer = _customer("Somchi")

    resp = admin_client.post("/sales/new/", {
        "customer": customer.pk, "brand": "LLDPE", "kg": "1000",
        "currency": "uzs", "price": "15000", "exchange_rate": "12500",
        "date": "2026-07-15", "debt_deadline": "", "note": "",
    })
    assert resp.status_code in (200, 302), resp.status_code
    sale = Sale.objects.get()
    assert sale.currency == Currency.UZS                       # (c) stickiness
    assert sale.price_uzs == Decimal("15000.00")               # (a) typed side exact
    assert sale.price == Decimal("1.2000")                     # 15000 / 12500

    ctx = _dash(admin_client).context
    assert ctx["customer_debt_total_uzs"] == Decimal("15000000.00")
    assert ctx["customer_debt_total"] == Decimal("1200.00")
    # and the KPI is the kassa's figure, not a second opinion
    total, total_uzs, count = customer_receivable_total()
    assert (ctx["customer_debt_total"], ctx["customer_debt_total_uzs"]) == (total, total_uzs)
    assert count == 1


# --- (d) Yuk holatlari, Yuklar qarzi, the progress chart --------------------

def test_yuk_holatlari_counts_trucks_per_hamkor_busiest_first(admin_client):
    """The card answers "whose trucks are on the road" (crm/views.py:62-64): a count
    per hamkor, busiest first, ties broken by name."""
    busy = Partner.objects.create(name="Zomin", phone="1", city="Tehron")
    quiet = Partner.objects.create(name="Alfa", phone="2", city="Tehron")
    other = Partner.objects.create(name="Bahor", phone="3", city="Tehron")
    for partner, trucks in ((busy, 2), (quiet, 1), (other, 1)):
        contract = make_contract(partner=partner, kg="9000", price="1.00")
        for _ in range(trucks):
            make_shipment(contract=contract, kg="100", sent=date(2026, 7, 1))

    rows = _dash(admin_client).context["status_rows"]
    assert len(rows) == 1                                   # all four in one holat
    assert rows[0]["total"] == 4
    assert rows[0]["partners"] == [("Zomin", 2), ("Alfa", 1), ("Bahor", 1)]


def test_yuklar_qarzi_counts_only_kelishuvlar_that_are_behind_their_plan(admin_client):
    """crm/views.py:78-85: only a kelishuv that SET a plan and has not met it yet
    owes trucks, and a hamkor's kelishuvlar add up into one row."""
    behind = Partner.objects.create(name="Alfa", phone="1", city="Tehron")
    met = Partner.objects.create(name="Bahor", phone="2", city="Tehron")
    unplanned = Partner.objects.create(name="Chinor", phone="3", city="Tehron")
    tied = Partner.objects.create(name="Zomin", phone="4", city="Tehron")

    first = make_contract(partner=behind, kg="9000", price="1.00", planned_trucks=3)
    make_shipment(contract=first, kg="100", sent=date(2026, 7, 1))          # 2 left
    make_contract(partner=behind, kg="9000", price="1.00", planned_trucks=1)  # 1 left
    done = make_contract(partner=met, kg="9000", price="1.00", planned_trucks=1)
    make_shipment(contract=done, kg="100", sent=date(2026, 7, 1))           # met
    make_contract(partner=unplanned, kg="9000", price="1.00")               # no plan
    make_contract(partner=tied, kg="9000", price="1.00", planned_trucks=3)  # 3 left

    rows = _dash(admin_client).context["truck_plan_rows"]
    assert rows == [("Alfa", 3), ("Zomin", 3)]      # tie broken by name, not by pk


def test_the_progress_chart_shows_the_kelishuvlar_that_have_actually_moved(admin_client):
    """crm/views.py:87-96 caps the chart at 8 and sorts by shipped kg so a run of
    fresh kelishuvlar cannot push the shipping ones off it."""
    moving = []
    for kg in ("300", "100", "200"):
        contract = make_contract(kg="9000", price="1.00")
        make_shipment(contract=contract, kg=kg, sent=date(2026, 7, 1))
        moving.append(contract)
    idle = [make_contract(kg="9000", price="1.00") for _ in range(7)]

    ctx = _dash(admin_client).context
    assert ctx["contracts_total"] == len(moving) + len(idle) == 10
    assert ctx["contracts_shown"] == 8                       # CHART_LIMIT
    shown = list(ctx["contracts"])
    assert [c.pk for c in shown[:3]] == [moving[0].pk, moving[2].pk, moving[1].pk]
    assert all(c.shipped_kg == 0 for c in shown[3:])         # the idle ones fill up
    assert [c.shipped_kg for c in shown[:3]] == [
        Decimal("300.000"), Decimal("200.000"), Decimal("100.000")]


# --- (d) Oylik hisobot: the value columns ----------------------------------

def test_oylik_hisobot_value_columns_are_the_sum_of_each_yuk_at_its_own_narx(admin_client):
    """Both currency columns of the monthly table, rebuilt from the yuklar. A truck
    priced in so'm must contribute its OWN so'm figure, not a re-rating of the
    dollar one."""
    contract = make_contract(kg="90000", price="1.00")
    _lot(contract=contract, kg="1000", price="1.00",
         sent=date(2026, 7, 1), arrived=date(2026, 7, 10))
    som_ship = make_shipment(contract=contract, kg="500", price="1.25",
                             status=ShipmentStatus.arrival(),
                             sent=date(2026, 7, 2), arrived=date(2026, 7, 12))
    line = som_ship.lines.first()
    line.currency = Currency.UZS
    line.exchange_rate = Decimal("12500")
    line.price_uzs = Decimal("15625.00")
    line.save()

    july = _months(admin_client)[date(2026, 7, 1)]
    arrived = list(Shipment.objects.filter(arrived__isnull=False))
    assert july["value"] == sum((s.goods_value for s in arrived), Decimal("0"))
    assert july["value_uzs"] == sum((s.goods_value_uzs for s in arrived), Decimal("0"))
    # 1000 * 1.00 + 500 * 1.25 = 1625 $; 1000 * 12000 + 500 * 15625 = 19 812 500 so'm
    assert july["value"] == Decimal("1625.00")
    assert july["value_uzs"] == Decimal("19812500.00")
    assert july["value_uzs"] != july["value"] * Decimal("12000")


def test_oylik_hisobot_sotuv_columns_carry_both_currencies(admin_client):
    """`sales_uzs` has no KPI to reconcile against, so it is checked against the
    sotuvlar directly — one dollar sotuv and one so'm sotuv at different kurslar."""
    contract = make_contract(kg="9000", price="0.50")
    lot = _lot(contract=contract, kg="3000", price="0.50")
    customer = _customer()
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"),
                        price=Decimal("1.0000"), price_uzs=Decimal("12000"),
                        currency=Currency.USD, exchange_rate=Decimal("12000"),
                        date=date(2026, 7, 15))
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("500"),
                        price=Decimal("1.2000"), price_uzs=Decimal("15000"),
                        currency=Currency.UZS, exchange_rate=Decimal("12500"),
                        date=date(2026, 7, 16))

    july = _months(admin_client)[date(2026, 7, 1)]
    sales = list(Sale.objects.all())
    assert july["sales"] == sum((s.net_total for s in sales), Decimal("0"))
    assert july["sales_uzs"] == sum((s.net_total_uzs for s in sales), Decimal("0"))
    assert july["sales"] == Decimal("1600.00")               # 1000 + 600
    assert july["sales_uzs"] == Decimal("19500000.00")       # 12 000 000 + 7 500 000


# --- (b) idempotence of the whole board ------------------------------------

def test_reopening_the_dashboard_twice_reports_the_same_board(admin_client):
    """Nothing on the board may be computed from state that reading it changes:
    two GETs over a mixed-currency data set must be byte-identical."""
    contract = make_contract(kg="9000", price="0.50")
    lot = _lot(contract=contract, kg="3000", price="0.50")
    _pay_supplier(contract, "800", "10400000", rate="13000", currency=Currency.UZS)
    customer = _customer()
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"),
                        price=Decimal("1.2000"), price_uzs=Decimal("15000"),
                        currency=Currency.UZS, exchange_rate=Decimal("12500"),
                        date=date(2026, 7, 15))

    first, second = _figures(admin_client), _figures(admin_client)
    assert first == second
    assert _months(admin_client) == _months(admin_client)


# --- the board is admin-only ------------------------------------------------

def test_a_tarjimon_never_reaches_the_board(translator_client):
    """crm/views.py:44: the money headlines are admin-only, so a tarjimon is sent to
    the Yuklar list instead of seeing a partial board."""
    resp = translator_client.get("/")
    assert resp.status_code == 302
    assert resp.url == "/shipments/"
