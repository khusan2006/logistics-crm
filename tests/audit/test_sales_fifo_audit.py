"""QA audit — Sotuv (sales) and the FIFO lot split.

Probes the four symptom families the product owner reported, against the real
views/forms: round-trip of the typed money side, idempotence of an edit-resave,
currency stickiness through the form, and aggregate consistency across lots that
were bought at different kurs values.

Read-only diagnosis: nothing outside tests/audit/ is touched.
"""
from decimal import ROUND_HALF_UP, Decimal

import pytest

from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, PaymentAllocation,
    Partner, Sale, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus,
    allocate_customer_payment, convert_pair,
)

CENT = Decimal("0.01")
PRICE_Q = Decimal("0.0001")


# --- local factories -------------------------------------------------------

def _customer(name="Anvar Plastik"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(brand="LLDPE", kg="10000", contract_price="1.00", expense=None,
         arrived="2026-07-16", lot_price=None, currency="usd", rate="12000",
         partner_name=None):
    """An arrived lot: one product on one truck, already in the ombor.

    landed cost/kg = (lot price or kelishuv price) + expenses/kg + vositachi/kg.
    """
    partner = Partner.objects.create(
        name=partner_name or f"P-{Partner.objects.count() + 1}", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand,
                                kg=Decimal(kg), price=Decimal(contract_price))
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(),
        sent="2026-07-05", arrived=arrived)
    line = ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg),
        price=None if lot_price is None else Decimal(lot_price),
        currency=currency, exchange_rate=Decimal(rate))
    if expense:
        ShipmentExpense.objects.create(shipment=shipment, amount=Decimal(expense),
                                       date=arrived, exchange_rate=Decimal(rate))
    return ShipmentLine.objects.get(pk=line.pk)


def _sale_post(customer, brand, kg, price, currency="usd", rate="12000",
               date="2026-07-18", deadline="", note=""):
    return {
        "customer": customer.pk, "brand": brand, "kg": str(kg),
        "currency": currency, "price": str(price), "exchange_rate": str(rate),
        "date": date, "debt_deadline": deadline, "note": note,
    }


def _edit_post(sale, **overrides):
    """The payload the Sotuvni tahrirlash form posts back for an unchanged sale,
    built from the values the form actually renders (see `_rendered`)."""
    data = {
        "customer": sale.customer_id, "line": sale.line_id, "kg": str(sale.kg),
        "currency": sale.currency, "price": str(sale.price),
        "exchange_rate": str(sale.exchange_rate), "date": str(sale.date),
        "debt_deadline": str(sale.debt_deadline or ""), "note": sale.note,
    }
    data.update({k: str(v) for k, v in overrides.items()})
    return data


def _rendered(admin_client, sale):
    """What the real edit form puts in its fields — the values an operator would
    post straight back by pressing Saqlash without touching anything."""
    from crm.forms import SaleForm
    form = SaleForm(instance=Sale.objects.get(pk=sale.pk))
    return {name: form[name].value() for name in form.fields}


def _money_snapshot(sale):
    return (sale.currency, sale.exchange_rate, sale.price, sale.price_uzs,
            sale.kg, sale.total, sale.total_uzs)


# =====================================================================
# (a) ROUND-TRIP — the typed side must be stored bit-exact
# =====================================================================

def test_usd_typed_price_is_exact_and_som_side_is_derived(admin_client, db):
    """Type $/kg: the dollar column is what was typed, the so'm one is price×kurs."""
    lot = _lot(brand="A-USD", kg="1000", contract_price="1.00")
    c = _customer()
    resp = admin_client.post("/sales/new/",
                             _sale_post(c, "A-USD", "400", "1.6789", rate="12345.67"))
    assert resp.status_code == 302
    sale = Sale.objects.get()
    assert sale.currency == Currency.USD
    assert sale.price == Decimal("1.6789")                       # bit-exact typed side
    assert sale.price_uzs == (Decimal("1.6789") * Decimal("12345.67")).quantize(
        CENT, rounding=ROUND_HALF_UP)
    # ...and never re-derived from its own conversion
    assert sale.price == (sale.price_uzs / sale.exchange_rate).quantize(
        PRICE_Q, rounding=ROUND_HALF_UP)


def test_uzs_typed_price_is_exact_and_usd_side_is_derived(admin_client, db):
    """Type so'm/kg: the so'm column holds the typed figure untouched and the
    dollar column is the derived one — not the other way round."""
    lot = _lot(brand="A-UZS", kg="1000", contract_price="1.00")
    c = _customer()
    resp = admin_client.post("/sales/new/", _sale_post(
        c, "A-UZS", "400", "20777.77", currency="uzs", rate="13456.78"))
    assert resp.status_code == 302
    sale = Sale.objects.get()
    assert sale.currency == Currency.UZS
    assert sale.price_uzs == Decimal("20777.77")                 # bit-exact typed side
    assert sale.price == (Decimal("20777.77") / Decimal("13456.78")).quantize(
        PRICE_Q, rounding=ROUND_HALF_UP)
    # the exact typed so'm figure survives — it is NOT price×kurs
    assert sale.price_uzs != (sale.price * sale.exchange_rate).quantize(CENT)


def test_uzs_price_rounds_the_dollar_side_at_the_4dp_quantum(admin_client, db):
    """Per-kg prices carry four decimals; the so'm→USD half-up rounding must land
    on that quantum exactly, with the so'm side untouched."""
    _lot(brand="Q", kg="1000", contract_price="1.00")
    c = _customer()
    # 12 000.60 / 12 000 = 1.00005 → half-up at 4dp → 1.0001
    admin_client.post("/sales/new/", _sale_post(
        c, "Q", "100", "12000.60", currency="uzs", rate="12000"))
    sale = Sale.objects.get()
    assert sale.price == Decimal("1.0001")
    assert sale.price_uzs == Decimal("12000.60")
    assert convert_pair("12000.60", Currency.UZS, Decimal("12000"), "0.0001") == (
        Decimal("1.0001"), Decimal("12000.60"))


def test_sale_typed_in_som_at_a_huge_and_a_tiny_kurs(admin_client, db):
    """Boundary kursi: a 1 000 000 so'm/$ rate and a 0.01 so'm/$ one both have to
    keep the typed side exact rather than collapsing it."""
    _lot(brand="H", kg="1000", contract_price="1.00")
    _lot(brand="T", kg="1000", contract_price="1.00", arrived="2026-07-17")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(
        c, "H", "10", "1000000", currency="uzs", rate="1000000"))
    huge = Sale.objects.get(line__contract_line__brand="H")
    assert huge.price_uzs == Decimal("1000000.00") and huge.price == Decimal("1.0000")

    admin_client.post("/sales/new/", _sale_post(
        c, "T", "10", "1.5", currency="usd", rate="0.01"))
    tiny = Sale.objects.get(line__contract_line__brand="T")
    assert tiny.price == Decimal("1.5000")
    assert tiny.price_uzs == Decimal("0.02")   # 1.5 × 0.01 = 0.015 → half-up


# =====================================================================
# (b) IDEMPOTENCE / NO-DRIFT — re-saving must move nothing
# =====================================================================

def test_resaving_a_usd_sale_unchanged_twice_moves_no_money(admin_client, db):
    _lot(brand="ID1", kg="1000", contract_price="1.00", expense="200")
    c = _customer()
    admin_client.post("/sales/new/",
                      _sale_post(c, "ID1", "400", "1.6789", rate="12345.67"))
    sale = Sale.objects.get()
    before = _money_snapshot(sale)

    for _ in range(2):
        resp = admin_client.post(f"/sales/{sale.pk}/edit/", _edit_post(sale))
        assert resp.status_code == 302
        sale.refresh_from_db()
        assert _money_snapshot(sale) == before


def test_resaving_a_usd_sale_with_only_the_note_changed_moves_no_money(admin_client, db):
    _lot(brand="ID2", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/",
                      _sale_post(c, "ID2", "400", "1.2345", rate="12500"))
    sale = Sale.objects.get()
    before = _money_snapshot(sale)

    for note in ("birinchi izoh", "ikkinchi izoh"):
        resp = admin_client.post(f"/sales/{sale.pk}/edit/", _edit_post(sale, note=note))
        assert resp.status_code == 302
        sale.refresh_from_db()
        assert sale.note == note
        assert _money_snapshot(sale) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_uzs_sale_from_the_rendered_form_moves_no_money(admin_client, db):
    """The "values change by themselves" symptom, end to end.

    Open a so'm sotuv's edit modal, press Saqlash without touching anything, and
    the narx must be exactly what it was. The form renders `price` (the derived
    DOLLAR column) while `currency` still says so'm, so the resave reads 1.6642
    as 1.6642 so'm/kg.
    """
    _lot(brand="ID3", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(
        c, "ID3", "400", "20777.77", currency="uzs", rate="12485"))
    sale = Sale.objects.get()
    before = _money_snapshot(sale)

    for _ in range(2):
        shown = _rendered(admin_client, sale)
        resp = admin_client.post(f"/sales/{sale.pk}/edit/", {
            "customer": shown["customer"], "line": shown["line"],
            "kg": str(shown["kg"]), "currency": shown["currency"],
            "price": str(shown["price"]), "exchange_rate": str(shown["exchange_rate"]),
            "date": str(shown["date"]), "debt_deadline": str(shown["debt_deadline"] or ""),
            "note": shown["note"] or "",
        })
        assert resp.status_code == 302
        sale.refresh_from_db()
        assert _money_snapshot(sale) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_uzs_sale_edit_form_prefills_the_som_figure(admin_client, db):
    """A so'm sotuv reopened for editing must show the so'm narx in the narx box —
    that is the figure the currency picker beside it claims the box is in."""
    _lot(brand="ID4", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(
        c, "ID4", "400", "20777.77", currency="uzs", rate="12485"))
    sale = Sale.objects.get()

    shown = _rendered(admin_client, sale)
    assert shown["currency"] == "uzs"
    assert Decimal(str(shown["price"])) == sale.price_uzs


def test_resaving_a_uzs_sale_with_the_som_figure_retyped_moves_no_money(admin_client, db):
    """Control for the two xfails above: when the so'm figure IS the one posted
    back, the row is stable — so the defect is the prefill, not the save path."""
    _lot(brand="ID5", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(
        c, "ID5", "400", "20777.77", currency="uzs", rate="12485"))
    sale = Sale.objects.get()
    before = _money_snapshot(sale)

    for _ in range(2):
        resp = admin_client.post(f"/sales/{sale.pk}/edit/",
                                 _edit_post(sale, price=sale.price_uzs))
        assert resp.status_code == 302
        sale.refresh_from_db()
        assert _money_snapshot(sale) == before


# =====================================================================
# (c) CURRENCY STICKINESS
# =====================================================================

def test_currency_uzs_sticks_through_the_create_view(admin_client, db):
    """currency=uzs posted through the real view must land as uzs, with the so'm
    column holding the typed figure — not a USD-interpreted one."""
    _lot(brand="ST1", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(
        c, "ST1", "250", "19500", currency="uzs", rate="13000"))
    sale = Sale.objects.get()
    assert sale.currency == "uzs"
    assert sale.is_som is True
    assert sale.price_uzs == Decimal("19500.00")
    assert sale.price == Decimal("1.5000")
    assert sale.total_uzs == Decimal("4875000.00")     # 250 × 19 500


def test_edit_form_renders_bound_to_uzs(admin_client, db):
    """Re-opening the edit modal keeps the Valyuta picker on So'm."""
    _lot(brand="ST2", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(
        c, "ST2", "250", "19500", currency="uzs", rate="13000"))
    sale = Sale.objects.get()

    html = admin_client.get(f"/sales/{sale.pk}/edit/",
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    assert 'value="uzs"' in html
    uzs_at = html.index('value="uzs"')
    # the uzs option is the chosen one (a <select> here, radios in the lineset modals)
    assert any(flag in html[uzs_at:uzs_at + 40] for flag in ("selected", "checked"))
    assert _rendered(admin_client, sale)["currency"] == "uzs"


def test_switching_a_sale_from_usd_to_uzs_actually_changes_it(admin_client, db):
    """"I change the currency but it stays set to a different currency" — the
    switch itself has to take, and re-derive the other side."""
    _lot(brand="ST3", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(c, "ST3", "100", "1.50", rate="13000"))
    sale = Sale.objects.get()
    assert sale.currency == "usd"

    resp = admin_client.post(f"/sales/{sale.pk}/edit/",
                             _edit_post(sale, currency="uzs", price="19500"))
    assert resp.status_code == 302
    sale.refresh_from_db()
    assert sale.currency == "uzs"
    assert sale.price_uzs == Decimal("19500.00")
    assert sale.price == Decimal("1.5000")


def test_fifo_slices_all_carry_the_som_currency_rate_and_price(admin_client, db):
    """The FIFO split builds Sale rows BY HAND via form.money_kwargs(). Every slice
    has to carry the one agreed narx with its currency, kurs and so'm side —
    including slices taken from lots bought at a completely different kurs."""
    _lot(brand="F1", kg="300", contract_price="1.00", arrived="2026-07-10",
         currency="usd", rate="11000")
    _lot(brand="F1", kg="300", contract_price="1.20", arrived="2026-07-12",
         currency="uzs", rate="14000", lot_price="1.20")
    _lot(brand="F1", kg="300", contract_price="1.40", arrived="2026-07-14",
         currency="usd", rate="12500")
    c = _customer()

    resp = admin_client.post("/sales/new/", _sale_post(
        c, "F1", "750", "20777.77", currency="uzs", rate="12485"))
    assert resp.status_code == 302

    slices = list(Sale.objects.order_by("id"))
    assert len(slices) == 3
    assert [s.kg for s in slices] == [Decimal("300.000"), Decimal("300.000"),
                                      Decimal("150.000")]
    for s in slices:
        assert s.currency == "uzs"
        assert s.exchange_rate == Decimal("12485.00")
        assert s.price_uzs == Decimal("20777.77")           # typed side, every slice
        assert s.price == (Decimal("20777.77") / Decimal("12485")).quantize(PRICE_Q)


# =====================================================================
# (d) AGGREGATE CONSISTENCY
# =====================================================================

def test_fifo_slices_sum_to_the_kg_and_money_that_was_entered(admin_client, db):
    """Nothing may be lost or invented by the split: Σ kg and Σ jami over the
    slices are exactly the sale that was typed."""
    _lot(brand="AG1", kg="333.333", contract_price="1.00", arrived="2026-07-10")
    _lot(brand="AG1", kg="333.333", contract_price="1.10", arrived="2026-07-11")
    _lot(brand="AG1", kg="333.334", contract_price="1.20", arrived="2026-07-12")
    c = _customer()

    admin_client.post("/sales/new/",
                      _sale_post(c, "AG1", "1000", "1.6789", rate="12345.67"))
    slices = list(Sale.objects.all())
    assert len(slices) == 3
    assert sum(s.kg for s in slices) == Decimal("1000.000")
    expected = (Decimal("1000") * Decimal("1.6789")).quantize(CENT)
    # per-slice quantize may shed a cent or two, but never more than one per slice
    assert abs(sum(s.total for s in slices) - expected) <= CENT * len(slices)
    assert Customer.objects.get(pk=c.pk).balance == sum(s.net_total for s in slices)


def test_customer_balance_matches_sum_of_slices_in_both_currencies(admin_client, db):
    """The mijoz's qarz on screen is Σ net_total over their sotuvlar, in each
    currency column, when the sotuv was agreed in so'm."""
    _lot(brand="AG2", kg="500", contract_price="1.00", arrived="2026-07-10")
    _lot(brand="AG2", kg="500", contract_price="1.30", arrived="2026-07-11")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(
        c, "AG2", "800", "20500", currency="uzs", rate="12800"))

    c.refresh_from_db()
    slices = list(Sale.objects.all())
    assert c.balance == sum(s.net_total for s in slices)
    assert c.balance_uzs == sum(s.net_total_uzs for s in slices)
    # and the so'm side is the exact typed narx × kg, not a reconversion
    assert c.balance_uzs == Decimal("800") * Decimal("20500")


def test_mixed_currency_sales_aggregate_without_double_counting(admin_client, db):
    """One mijoz, one sotuv in dollars and one in so'm at a different kurs: both
    totals must be plain sums of the stored pairs."""
    _lot(brand="MX1", kg="500", contract_price="1.00")
    _lot(brand="MX2", kg="500", contract_price="1.00", arrived="2026-07-17")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(c, "MX1", "100", "2.00", rate="12000"))
    admin_client.post("/sales/new/", _sale_post(
        c, "MX2", "100", "26000", currency="uzs", rate="13000"))

    c.refresh_from_db()
    assert c.balance == Decimal("200.00") + Decimal("200.00")
    assert c.balance_uzs == Decimal("2400000.00") + Decimal("2600000.00")


def test_profit_across_lots_bought_at_different_kurs(admin_client, db):
    """Foyda per slice is (narx − that lot's live tannarx) × kg, and the total is
    their sum. Cost is booked on the LOT (its own currency/kurs), revenue on the
    sotuv — the dollar side is the common ground, which is what makes the sum add
    up at all."""
    old = _lot(brand="PF", kg="400", contract_price="1.00", expense="80",
               arrived="2026-07-10", currency="uzs", rate="11000")   # 1.00 + 0.20
    new = _lot(brand="PF", kg="400", contract_price="1.50", arrived="2026-07-12",
               currency="usd", rate="13000")                          # 1.50
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(c, "PF", "600", "2.00", rate="12500"))

    s_old = Sale.objects.get(line=old)
    s_new = Sale.objects.get(line=new)
    assert s_old.cost_price == Decimal("1.2000") and s_old.kg == Decimal("400.000")
    assert s_new.cost_price == Decimal("1.5000") and s_new.kg == Decimal("200.000")
    assert s_old.profit == Decimal("320.00")      # (2.00 − 1.20) × 400
    assert s_new.profit == Decimal("100.00")      # (2.00 − 1.50) × 200
    assert s_old.profit + s_new.profit == Decimal("420.00")
    # tannarx in so'm is rated at THIS SALE's kurs, not the lot's (documented)
    assert s_old.cost_price_uzs == (Decimal("1.2000") * Decimal("12500")).quantize(CENT)


def test_som_profit_agrees_with_som_revenue_minus_som_cost(admin_client, db):
    """profit_uzs is in_som(profit) while total_uzs is the exact typed so'm — the
    two bases can only differ by the 4dp rounding of the dollar side. Anything
    bigger would be a real so'm miscalculation."""
    _lot(brand="PU", kg="2000", contract_price="1.00", expense="200")   # tannarx 1.10
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(
        c, "PU", "1100", "20777.77", currency="uzs", rate="13500"))
    sale = Sale.objects.get()

    som_revenue = sale.total_uzs
    som_cost = (sale.cost_price_uzs * sale.kg).quantize(CENT)
    slack = (sale.kg * PRICE_Q / 2 * sale.exchange_rate).quantize(CENT) + CENT
    assert abs(sale.profit_uzs - (som_revenue - som_cost)) <= slack


def test_restocked_return_reverses_both_revenue_and_profit(admin_client, db):
    """Control for the xfail below. Goods come back onto the shelf, so revenue AND
    cost reverse and foyda falls by the margin on the returned kg."""
    lot = _lot(brand="RR", kg="1000", contract_price="1.00", expense="200")  # cost 1.20
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(c, "RR", "400", "2.00"))
    sale = Sale.objects.get()
    assert sale.profit == Decimal("320.00")            # (2.00 − 1.20) × 400

    admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "400", "currency": "usd", "exchange_rate": "12000", "price": "2.00",
        "date": "2026-07-19", "restock": "on", "note": ""})
    sale.refresh_from_db()
    assert sale.net_total == Decimal("0.00")
    assert sale.profit == Decimal("0.00")
    assert ShipmentLine.objects.get(pk=lot.pk).available_kg == Decimal("1000.000")


@pytest.mark.xfail(reason="BUG: Sale._returned_profit only counts restocked returns, so a "
                          "scrapped (restock=False) return credits the mijoz in full while "
                          "foyda keeps the whole margin AND the cost of the lost goods",
                   strict=False)
def test_non_restocked_return_removes_the_profit_it_credited(admin_client, db):
    """A qaytarish that is NOT put back on the shelf: the mijoz is credited the full
    kg × narx (net_total → 0) but the granula is gone, so the cost was still
    incurred. Foyda must drop by at least the revenue that was handed back — it
    cannot stay at the full margin of a sale that no longer has any revenue."""
    lot = _lot(brand="RN", kg="1000", contract_price="1.00", expense="200")  # cost 1.20
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(c, "RN", "400", "2.00"))
    sale = Sale.objects.get()
    assert sale.profit == Decimal("320.00")

    admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "400", "currency": "usd", "exchange_rate": "12000", "price": "2.00",
        "date": "2026-07-19", "restock": "", "note": ""})
    sale.refresh_from_db()
    assert sale.net_total == Decimal("0.00")           # every dollar credited back
    assert ShipmentLine.objects.get(pk=lot.pk).available_kg == Decimal("600.000")
    # 400 kg were paid for at 1.20 and never came back → −480, certainly not +320
    assert sale.profit == Decimal("-480.00")


def test_paid_and_remaining_agree_with_the_allocations(admin_client, db):
    """Qoldiq on the sotuv row = jami − Σ taqsimlangan, in both columns."""
    _lot(brand="PA", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(c, "PA", "500", "2.00", rate="12000"))
    sale = Sale.objects.get()

    payment = CustomerPayment.objects.create(
        customer=c, date="2026-07-19", amount=Decimal("400.00"),
        amount_uzs=Decimal("4800000.00"), currency="usd",
        exchange_rate=Decimal("12000"), method="cash")
    allocate_customer_payment(payment)

    sale.refresh_from_db()
    allocated = sum(a.amount for a in PaymentAllocation.objects.filter(sale=sale))
    assert sale.paid == allocated == Decimal("400.00")
    assert sale.remaining == sale.net_total - Decimal("400.00") == Decimal("600.00")
    assert sale.remaining_uzs == sale.net_total_uzs - sale.paid_uzs


# =====================================================================
# BOUNDARIES
# =====================================================================

@pytest.mark.parametrize("price", ["0", "-1.5"])
def test_zero_or_negative_price_is_rejected(admin_client, db, price):
    _lot(brand="B1", kg="1000", contract_price="1.00")
    c = _customer()
    resp = admin_client.post("/sales/new/", _sale_post(c, "B1", "100", price))
    assert resp.status_code == 200
    assert not Sale.objects.exists()


@pytest.mark.parametrize("kg", ["0", "-10"])
def test_zero_or_negative_kg_is_rejected(admin_client, db, kg):
    _lot(brand="B2", kg="1000", contract_price="1.00")
    c = _customer()
    resp = admin_client.post("/sales/new/", _sale_post(c, "B2", kg, "1.50"))
    assert resp.status_code == 200
    assert not Sale.objects.exists()


@pytest.mark.parametrize("rate", ["0", ""])
def test_missing_or_zero_kurs_is_rejected(admin_client, db, rate):
    """A row with no kurs has only one of its two values and could never join a
    so'm total — convert_pair refuses it, and so must the form."""
    _lot(brand="B3", kg="1000", contract_price="1.00")
    c = _customer()
    resp = admin_client.post("/sales/new/",
                             _sale_post(c, "B3", "100", "1.50", rate=rate))
    assert resp.status_code == 200
    assert not Sale.objects.exists()
    with pytest.raises(ValueError):
        convert_pair("1.50", Currency.USD, Decimal("0"))


def test_blank_price_is_rejected_on_a_sale(admin_client, db):
    """A bron may have no narx yet; a sotuv may not — it is what the mijoz owes."""
    _lot(brand="B4", kg="1000", contract_price="1.00")
    c = _customer()
    resp = admin_client.post("/sales/new/", _sale_post(c, "B4", "100", ""))
    assert resp.status_code == 200
    assert not Sale.objects.exists()


def test_deleting_a_lot_that_a_sale_depends_on_is_blocked(admin_client, db):
    """Sale.line is PROTECT: the ombor row a sotuv was taken from cannot vanish
    under it and leave the sotuv costing nothing."""
    from django.db.models import ProtectedError

    lot = _lot(brand="B5", kg="1000", contract_price="1.00")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(c, "B5", "100", "1.50"))
    assert Sale.objects.count() == 1
    with pytest.raises(ProtectedError):
        lot.delete()


def test_deleting_a_fifo_slice_returns_only_its_own_kg(admin_client, db):
    """Deleting one slice of a split sotuv frees that lot's kg and no other's, and
    the mijoz's qarz drops by exactly that slice."""
    old = _lot(brand="B6", kg="400", contract_price="1.00", arrived="2026-07-10")
    new = _lot(brand="B6", kg="400", contract_price="1.00", arrived="2026-07-12")
    c = _customer()
    admin_client.post("/sales/new/", _sale_post(c, "B6", "600", "2.00"))
    s_old, s_new = Sale.objects.get(line=old), Sale.objects.get(line=new)
    c.refresh_from_db()
    before = c.balance

    resp = admin_client.post(f"/sales/{s_new.pk}/delete/")
    assert resp.status_code == 302
    assert ShipmentLine.objects.get(pk=new.pk).available_kg == Decimal("400.000")
    assert ShipmentLine.objects.get(pk=old.pk).available_kg == Decimal("0.000")
    c.refresh_from_db()
    assert c.balance == before - s_new.net_total
    assert Sale.objects.filter(pk=s_old.pk).exists()
