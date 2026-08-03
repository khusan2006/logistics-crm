"""Kassa (cash desk) audit — diagnosis only, no fixes.

Probe families, mapped onto the reported symptoms:
  (a) round-trip      — the typed side must survive bit-exact at the row's kurs
  (b) idempotence     — re-saving through the real view must move no figure
  (c) stickiness      — a so'm row must stay a so'm row, in the DB and on the form
  (d) aggregates      — every kassa total must equal the sum of its printed parts,
                        in USD and in so'm independently, across mixed kurslar

Tests marked xfail carry a BUG: reason and are the findings.
"""
from decimal import Decimal

import pytest

from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, Logist,
    LogistPayment, Partner, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus,
    SupplierPayment,
)

pytestmark = pytest.mark.django_db


# --- local factories -------------------------------------------------------

def _contract(name="Pars", kg="100000", price="1.00"):
    partner = Partner.objects.create(name=name, phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal(kg), price=Decimal(price),
                                price_uzs=Decimal(price) * 12000)
    return contract


def _shipment(contract, kg="500"):
    ship = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(),
                                   sent="2026-07-05", arrived="2026-07-16")
    ShipmentLine.objects.create(shipment=ship, contract_line=contract.lines.first(),
                                kg=Decimal(kg))
    return ship


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _logist(name="Sardor aka"):
    return Logist.objects.create(name=name, phone="1")


def _post(client, url, data):
    """POST as the modal does (AJAX): 204 = saved, 422 = the form said no."""
    return client.post(url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


def _bound_values(form, **overrides):
    """What the browser would resubmit from a freshly rendered edit form."""
    data = {}
    for name in form.fields:
        value = form[name].value()
        data[name] = "" if value is None else str(value)
    data.update({k: str(v) for k, v in overrides.items()})
    return data


def _money(obj):
    obj.refresh_from_db()
    return (obj.amount, obj.amount_uzs, obj.currency, obj.exchange_rate)


def _ctx(admin_client, **params):
    resp = admin_client.get("/kassa/", params)
    assert resp.status_code == 200
    return resp.context


# ===========================================================================
# (a) ROUND-TRIP
# ===========================================================================

def test_supplier_payment_typed_in_som_keeps_the_som_figure_exact(admin_client):
    """A so'm to'lov stores the typed so'm bit-exact; only USD is derived."""
    contract = _contract()
    assert _post(admin_client, "/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-10", "currency": "uzs",
        "amount": "12345678", "exchange_rate": "12345.67",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    }).status_code == 204

    p = SupplierPayment.objects.get()
    assert p.currency == Currency.UZS
    assert p.amount_uzs == Decimal("12345678.00")      # typed side untouched
    assert p.amount == Decimal("1000.00")              # 12345678 / 12345.67
    assert p.exchange_rate == Decimal("12345.67")


def test_supplier_payment_typed_in_dollars_keeps_the_dollar_figure_exact(admin_client):
    contract = _contract()
    assert _post(admin_client, "/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-10", "currency": "usd",
        "amount": "1234.56", "exchange_rate": "12650",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    }).status_code == 204

    p = SupplierPayment.objects.get()
    assert p.currency == Currency.USD
    assert p.amount == Decimal("1234.56")                    # typed side untouched
    assert p.amount_uzs == Decimal("1234.56") * Decimal("12650")


def test_a_huge_and_a_tiny_kurs_both_round_trip(admin_client):
    """Boundary kurslar: the typed side is still exact, the derived side rounds
    once at its own quantum (2dp for a lump sum)."""
    customer = _customer()
    logist = _logist()
    # tiny kurs — a dollar row whose so'm twin is small
    assert _post(admin_client, "/logist-payments/new/", {
        "logist": logist.pk, "date": "2026-07-10", "currency": "usd",
        "amount": "100", "exchange_rate": "0.01", "method": "cash",
        "fee_percent": "0", "note": "",
    }).status_code == 204
    tiny = LogistPayment.objects.get()
    assert (tiny.amount, tiny.amount_uzs) == (Decimal("100.00"), Decimal("1.00"))

    # huge kurs — a so'm row whose dollar twin rounds to cents
    assert _post(admin_client, "/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-10",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
        "form-0-currency": "uzs", "form-0-amount": "1000000",
        "form-0-exchange_rate": "999999999.99", "form-0-method": "cash",
        "form-0-fee_percent": "0", "form-0-note": "",
    }).status_code == 204
    huge = CustomerPayment.objects.get()
    assert huge.amount_uzs == Decimal("1000000.00")   # typed side exact
    assert huge.amount == Decimal("0.00")             # 1e6 / 1e9 → below a cent


def test_a_kurs_of_zero_is_refused_rather_than_stored(admin_client):
    """convert_pair raises without a kurs; the form must catch it as a field error,
    not a 500 and not a silently-zero so'm row."""
    contract = _contract()
    resp = _post(admin_client, "/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-10", "currency": "uzs",
        "amount": "12000000", "exchange_rate": "0",
        "commission_percent": "0", "method": "cash", "fee_percent": "0", "note": "",
    })
    assert resp.status_code == 422
    assert not SupplierPayment.objects.exists()


def test_zero_and_negative_sums_are_refused(admin_client):
    logist = _logist()
    for amount in ("0", "-500"):
        resp = _post(admin_client, "/logist-payments/new/", {
            "logist": logist.pk, "date": "2026-07-10", "currency": "usd",
            "amount": amount, "exchange_rate": "12000", "method": "cash",
            "fee_percent": "0", "note": "",
        })
        assert resp.status_code == 422, amount
    assert not LogistPayment.objects.exists()


# ===========================================================================
# (b) IDEMPOTENCE / NO DRIFT
# ===========================================================================

def _resave_twice(admin_client, url, obj, **overrides):
    """Open the edit modal, resubmit exactly what it rendered, twice.

    Returns the money tuple after each save so a caller can see WHEN it moved."""
    seen = []
    for _ in range(2):
        form = admin_client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").context["form"]
        assert _post(admin_client, url, _bound_values(form, **overrides)).status_code == 204
        seen.append(_money(obj))
    return seen


def test_resaving_a_dollar_supplier_payment_moves_nothing(admin_client):
    contract = _contract()
    _post(admin_client, "/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-10", "currency": "usd",
        "amount": "1234.56", "exchange_rate": "12650", "commission_percent": "2",
        "method": "transfer", "fee_percent": "1", "note": "asl",
    })
    p = SupplierPayment.objects.get()
    before = _money(p)
    after = _resave_twice(admin_client, f"/supplier-payments/{p.pk}/edit/", p)
    assert after == [before, before]


def test_changing_only_the_note_moves_no_dollar_figure(admin_client):
    contract = _contract()
    _post(admin_client, "/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-10", "currency": "usd",
        "amount": "999.99", "exchange_rate": "12345.67", "commission_percent": "0",
        "method": "cash", "fee_percent": "0", "note": "asl",
    })
    p = SupplierPayment.objects.get()
    before = _money(p)
    after = _resave_twice(admin_client, f"/supplier-payments/{p.pk}/edit/", p,
                          note="boshqa izoh")
    assert after == [before, before]


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_supplier_payment_moves_nothing(admin_client):
    contract = _contract()
    _post(admin_client, "/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-10", "currency": "uzs",
        "amount": "12000000", "exchange_rate": "12000", "commission_percent": "0",
        "method": "cash", "fee_percent": "0", "note": "asl",
    })
    p = SupplierPayment.objects.get()
    before = _money(p)
    assert before[:2] == (Decimal("1000.00"), Decimal("12000000.00"))
    after = _resave_twice(admin_client, f"/supplier-payments/{p.pk}/edit/", p)
    assert after == [before, before]


def test_the_dollar_control_for_the_same_round_trip_holds(admin_client):
    """Same open-and-save on a USD row: proves the drift above is the currency
    handling, not the edit path itself."""
    contract = _contract()
    _post(admin_client, "/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-10", "currency": "usd",
        "amount": "1000", "exchange_rate": "12000", "commission_percent": "0",
        "method": "cash", "fee_percent": "0", "note": "",
    })
    p = SupplierPayment.objects.get()
    form = admin_client.get(f"/supplier-payments/{p.pk}/edit/",
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").context["form"]
    assert form["currency"].value() == Currency.USD
    assert Decimal(str(form["amount"].value())) == Decimal("1000.00")
    expected = (Decimal("1000.00"), Decimal("12000000.00"), "usd", Decimal("12000.00"))
    assert _resave_twice(admin_client, f"/supplier-payments/{p.pk}/edit/", p) == [
        expected, expected]


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_customer_payment_moves_nothing(admin_client):
    customer = _customer()
    _post(admin_client, "/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-10",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
        "form-0-currency": "uzs", "form-0-amount": "12650000",
        "form-0-exchange_rate": "12650", "form-0-method": "cash",
        "form-0-fee_percent": "0", "form-0-note": "",
    })
    p = CustomerPayment.objects.get()
    before = _money(p)
    assert before[:2] == (Decimal("1000.00"), Decimal("12650000.00"))
    after = _resave_twice(admin_client, f"/customer-payments/{p.pk}/edit/", p)
    assert after == [before, before]


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_logist_payment_moves_nothing(admin_client):
    logist = _logist()
    _post(admin_client, "/logist-payments/new/", {
        "logist": logist.pk, "date": "2026-07-10", "currency": "uzs",
        "amount": "24000000", "exchange_rate": "12000", "method": "cash",
        "fee_percent": "0", "note": "",
    })
    p = LogistPayment.objects.get()
    before = _money(p)
    after = _resave_twice(admin_client, f"/logist-payments/{p.pk}/edit/", p)
    assert after == [before, before]


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_expense_moves_nothing_and_the_kassa_holds(admin_client):
    contract = _contract()
    ship = _shipment(contract)
    _post(admin_client, "/expenses/new/", {
        "shipment": ship.pk, "date": "2026-07-12", "currency": "uzs",
        "method": "cash", "exchange_rate": "12000", "fee_percent": "0", "note": "",
        "amount_customs": "6000000",
    })
    e = ShipmentExpense.objects.get()
    before = _money(e)
    assert before[:2] == (Decimal("500.00"), Decimal("6000000.00"))
    cash_before = _ctx(admin_client)["cash_total_uzs"]
    after = _resave_twice(admin_client, f"/expenses/{e.pk}/edit/", e)
    assert after == [before, before]
    assert _ctx(admin_client)["cash_total_uzs"] == cash_before


# ===========================================================================
# (c) CURRENCY STICKINESS
# ===========================================================================

def test_a_som_customer_payment_is_stored_as_som_not_reinterpreted(admin_client):
    customer = _customer()
    assert _post(admin_client, "/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-10",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
        "form-0-currency": "uzs", "form-0-amount": "12000000",
        "form-0-exchange_rate": "12000", "form-0-method": "cash",
        "form-0-fee_percent": "0", "form-0-note": "",
    }).status_code == 204

    p = CustomerPayment.objects.get()
    assert p.currency == Currency.UZS and p.is_som
    assert p.amount_uzs == Decimal("12000000.00")     # the typed figure, as so'm
    assert p.amount == Decimal("1000.00")             # NOT 12 000 000 read as $
    ctx = _ctx(admin_client)
    assert ctx["cash_total_uzs"] == Decimal("12000000.00")
    assert ctx["cash_total"] == Decimal("1000.00")


def test_a_per_box_som_override_sticks_on_the_expense_grid(admin_client):
    """The grid shares one Valyuta but each box may override it."""
    contract = _contract()
    ship = _shipment(contract)
    assert _post(admin_client, "/expenses/new/", {
        "shipment": ship.pk, "date": "2026-07-12", "currency": "usd",
        "method": "cash", "exchange_rate": "12000", "fee_percent": "0", "note": "",
        "amount_customs": "100", "amount_transport": "2400000",
        "currency_transport": "uzs",
    }).status_code == 204

    rows = {e.category: e for e in ShipmentExpense.objects.all()}
    assert rows["customs"].currency == Currency.USD
    assert (rows["customs"].amount, rows["customs"].amount_uzs) == (
        Decimal("100.00"), Decimal("1200000.00"))
    assert rows["transport"].currency == Currency.UZS
    assert (rows["transport"].amount, rows["transport"].amount_uzs) == (
        Decimal("200.00"), Decimal("2400000.00"))


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_modal_of_a_som_row_shows_the_som_figure(admin_client):
    contract = _contract()
    _post(admin_client, "/supplier-payments/new/", {
        "contract": contract.pk, "date": "2026-07-10", "currency": "uzs",
        "amount": "12000000", "exchange_rate": "12000", "commission_percent": "0",
        "method": "cash", "fee_percent": "0", "note": "",
    })
    p = SupplierPayment.objects.get()
    form = admin_client.get(f"/supplier-payments/{p.pk}/edit/",
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").context["form"]
    assert form["currency"].value() == Currency.UZS          # radio does stick
    assert Decimal(str(form["amount"].value())) == Decimal("12000000.00")


# ===========================================================================
# (d) AGGREGATE CONSISTENCY — mixed currencies, mixed kurslar
# ===========================================================================

def _mixed_book(admin_client):
    """One period holding every kind of movement, in both currencies at three
    different kurslar, so no total can be right by coincidence."""
    contract = _contract()
    ship = _shipment(contract)
    customer = _customer()
    logist = _logist()

    CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                   amount=Decimal("1000.00"),
                                   amount_uzs=Decimal("12000000"),
                                   exchange_rate=Decimal("12000"), method="cash")
    CustomerPayment.objects.create(customer=customer, date="2026-07-11",
                                   currency=Currency.UZS, amount=Decimal("200.00"),
                                   amount_uzs=Decimal("2530000"),
                                   exchange_rate=Decimal("12650"), method="transfer",
                                   fee_percent=Decimal("2"))
    SupplierPayment.objects.create(contract=contract, date="2026-07-12",
                                   amount=Decimal("300.00"),
                                   amount_uzs=Decimal("3900000"),
                                   exchange_rate=Decimal("13000"), method="transfer",
                                   commission_percent=Decimal("2"),
                                   fee_percent=Decimal("1"))
    SupplierPayment.objects.create(contract=contract, date="2026-07-13",
                                   currency=Currency.UZS, amount=Decimal("100.00"),
                                   amount_uzs=Decimal("1200000"),
                                   exchange_rate=Decimal("12000"), method="card")
    ShipmentExpense.objects.create(shipment=ship, date="2026-07-14",
                                   category="customs", amount=Decimal("50.00"),
                                   amount_uzs=Decimal("632500"),
                                   exchange_rate=Decimal("12650"), method="cash")
    ShipmentExpense.objects.create(shipment=ship, date="2026-07-15",
                                   currency=Currency.UZS, category="transport",
                                   amount=Decimal("40.00"), amount_uzs=Decimal("520000"),
                                   exchange_rate=Decimal("13000"), method="transfer",
                                   fee_percent=Decimal("3"))
    LogistPayment.objects.create(logist=logist, date="2026-07-16",
                                 currency=Currency.UZS, amount=Decimal("80.00"),
                                 amount_uzs=Decimal("960000"),
                                 exchange_rate=Decimal("12000"), method="cash")
    return contract, ship, customer, logist


def test_cash_total_equals_the_sum_of_its_parts_in_both_currencies(admin_client):
    _mixed_book(admin_client)
    ctx = _ctx(admin_client)

    ins = sum((p.net_amount for p in CustomerPayment.objects.all()), Decimal("0"))
    outs = (sum((p.total_out for p in SupplierPayment.objects.all()), Decimal("0"))
            + sum((e.total_out for e in ShipmentExpense.objects.all()), Decimal("0"))
            + sum((p.total_out for p in LogistPayment.objects.all()), Decimal("0")))
    ins_uzs = sum((p.net_amount_uzs for p in CustomerPayment.objects.all()), Decimal("0"))
    outs_uzs = (sum((p.total_out_uzs for p in SupplierPayment.objects.all()), Decimal("0"))
                + sum((e.total_out_uzs for e in ShipmentExpense.objects.all()), Decimal("0"))
                + sum((p.total_out_uzs for p in LogistPayment.objects.all()), Decimal("0")))

    assert ctx["cash_total"] == ins - outs
    assert ctx["cash_total_uzs"] == ins_uzs - outs_uzs
    # the two currencies are independent figures, not one re-rated at one kurs
    assert ctx["cash_total_uzs"] != ctx["cash_total"] * Decimal("12000")


def test_method_balances_sum_to_the_period_totals_in_both_currencies(admin_client):
    _mixed_book(admin_client)
    ctx = _ctx(admin_client)
    balances = ctx["balances"]

    assert sum(b["in"] for b in balances.values()) == ctx["net_in"]
    assert sum(b["out"] for b in balances.values()) == ctx["net_out"]
    assert sum(b["in_uzs"] for b in balances.values()) == ctx["net_in_uzs"]
    assert sum(b["out_uzs"] for b in balances.values()) == ctx["net_out_uzs"]
    assert ctx["net_total"] == ctx["net_in"] - ctx["net_out"]
    # unfiltered, the period IS all time, so the hero must agree with the ledgers
    assert ctx["net_total"] == ctx["cash_total"]
    assert ctx["net_total_uzs"] == ctx["cash_total_uzs"]


def test_the_kirim_ledger_rows_add_up_to_the_kirim_total(admin_client):
    _mixed_book(admin_client)
    ctx = _ctx(admin_client)
    rows = list(ctx["income_page"].object_list)
    assert sum((p.net_amount for p in rows), Decimal("0")) == ctx["net_in"]
    assert sum((p.net_amount_uzs for p in rows), Decimal("0")) == ctx["net_in_uzs"]


@pytest.mark.xfail(reason="BUG: the Chiqim so'm total does not equal the so'm figures "
                          "printed in its own rows — the vositachi/foiz rows are "
                          "rendered with uzs_slice (a share of the stored so'm value) "
                          "while total_out_uzs re-derives them with in_som "
                          "(USD x kurs), and the two disagree for a so'm row",
                   strict=False)
def test_the_chiqim_ledger_rows_add_up_to_the_chiqim_total(admin_client):
    """A so'm-typed hamkor to'lov whose USD twin had to round: the commission is
    then worth a different so'm figure depending on which rule you use."""
    contract = _contract()
    # 500 so'm at 12 000 → $0.04 (0.041666… rounded up), so amount x kurs is 480,
    # not the 500 that was actually typed.
    SupplierPayment.objects.create(contract=contract, date="2026-07-12",
                                   currency=Currency.UZS, amount=Decimal("0.04"),
                                   amount_uzs=Decimal("500"),
                                   exchange_rate=Decimal("12000"), method="transfer",
                                   commission_percent=Decimal("50"),
                                   fee_percent=Decimal("50"))
    ctx = _ctx(admin_client)
    rows = list(ctx["outflow_page"].object_list)
    assert sum((r["amount"] for r in rows), Decimal("0")) == ctx["net_out"]
    assert sum((r["amount_uzs"] for r in rows), Decimal("0")) == ctx["net_out_uzs"]


def test_the_chiqim_ledger_rows_add_up_in_dollars_across_mixed_kurslar(admin_client):
    _mixed_book(admin_client)
    ctx = _ctx(admin_client)
    rows = list(ctx["outflow_page"].object_list)
    assert sum((r["amount"] for r in rows), Decimal("0")) == ctx["net_out"]


def test_the_waterfall_closes_on_the_cash_total_in_both_currencies(admin_client):
    _mixed_book(admin_client)
    ctx = _ctx(admin_client)
    closing = ctx["waterfall"][-1]
    assert closing["label"] == "Qoldiq"
    assert closing["running"] == ctx["cash_total"]
    assert closing["running_uzs"] == ctx["cash_total_uzs"]


# ===========================================================================
# BANK FOIZ — the transfer-only charge
# ===========================================================================

def test_naqd_and_karta_never_pay_a_foiz_even_when_one_is_stored(admin_client):
    """fee_percent left over from a method change must be ignored, not trusted."""
    contract = _contract()
    customer = _customer()
    CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                   amount=Decimal("1000.00"),
                                   amount_uzs=Decimal("12000000"), method="cash",
                                   fee_percent=Decimal("5"))
    SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                   amount=Decimal("400.00"),
                                   amount_uzs=Decimal("4800000"), method="card",
                                   fee_percent=Decimal("5"))
    ctx = _ctx(admin_client)
    assert ctx["balances"]["cash"]["in"] == Decimal("1000.00")     # not 950
    assert ctx["balances"]["card"]["out"] == Decimal("400.00")     # not 420
    assert ctx["cash_total"] == Decimal("600.00")
    assert ctx["cash_total_uzs"] == Decimal("7200000.00")
    # and no foiz row is drawn for either of them
    assert not [r for r in ctx["outflow_page"].object_list
                if r["kind"].startswith("fee")]
    assert "Bank foizi" not in [b["label"] for b in ctx["waterfall"]]


def test_an_incoming_foiz_is_carved_out_and_an_outgoing_one_rides_on_top(admin_client):
    """Direction matters: the mijoz's bank cut never reaches us (kirim shrinks),
    ours is an extra charge (chiqim grows)."""
    contract = _contract()
    customer = _customer()
    CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                   amount=Decimal("1000.00"),
                                   amount_uzs=Decimal("12000000"), method="transfer",
                                   fee_percent=Decimal("2"))
    SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                   amount=Decimal("500.00"),
                                   amount_uzs=Decimal("6000000"), method="transfer",
                                   fee_percent=Decimal("2"))
    ctx = _ctx(admin_client)
    assert ctx["net_in"] == Decimal("980.00")            # 1000 − 20
    assert ctx["net_in_uzs"] == Decimal("11760000.00")
    assert ctx["net_out"] == Decimal("510.00")           # 500 + 10
    assert ctx["net_out_uzs"] == Decimal("6120000.00")
    assert ctx["cash_total"] == Decimal("470.00")
    # only the outgoing one becomes a waterfall step
    steps = {b["label"]: b["amount"] for b in ctx["waterfall"]}
    assert steps["Bank foizi"] == Decimal("-10.00")


@pytest.mark.xfail(reason="BUG: the Oqim waterfall bills the bank foiz of a "
                          "logist-funded xarajat, whose cash never left the kassa "
                          "(total_out is 0 for it) — the run stops closing on the "
                          "Kassada figure",
                   strict=False)
def test_a_logist_funded_expense_does_not_leak_its_foiz_into_the_waterfall(admin_client):
    contract = _contract()
    ship = _shipment(contract)
    logist = _logist()
    LogistPayment.objects.create(logist=logist, date="2026-07-10",
                                 amount=Decimal("1000.00"),
                                 amount_uzs=Decimal("12000000"), method="cash")
    # paid by the logist out of the float we already sent them: the kassa loses
    # nothing here, foiz included
    ShipmentExpense.objects.create(shipment=ship, date="2026-07-12", logist=logist,
                                   category="customs", amount=Decimal("200.00"),
                                   amount_uzs=Decimal("2400000"), method="transfer",
                                   fee_percent=Decimal("10"))
    ctx = _ctx(admin_client)
    assert ctx["cash_total"] == Decimal("-1000.00")
    assert ctx["waterfall"][-1]["running"] == ctx["cash_total"]
    assert ctx["waterfall"][-1]["running_uzs"] == ctx["cash_total_uzs"]


def test_a_logist_funded_expense_costs_the_kassa_nothing(admin_client):
    """The cash left as the LogistPayment; charging it again would bill us twice."""
    contract = _contract()
    ship = _shipment(contract)
    logist = _logist()
    LogistPayment.objects.create(logist=logist, date="2026-07-10",
                                 amount=Decimal("1000.00"),
                                 amount_uzs=Decimal("12000000"), method="cash")
    ShipmentExpense.objects.create(shipment=ship, date="2026-07-12", logist=logist,
                                   category="customs", amount=Decimal("200.00"),
                                   amount_uzs=Decimal("2400000"), method="cash")
    ctx = _ctx(admin_client)
    assert ctx["cash_total"] == Decimal("-1000.00")
    assert ctx["cash_total_uzs"] == Decimal("-12000000.00")
    assert not [r for r in ctx["outflow_page"].object_list if r["kind"] == "expense"]


# ===========================================================================
# DATE RANGE — boundaries, double counting, dropped rows
# ===========================================================================

def test_both_boundary_days_are_inside_the_range_and_counted_once(admin_client):
    customer = _customer()
    for day in ("2026-06-30", "2026-07-01", "2026-07-15", "2026-07-31", "2026-08-01"):
        CustomerPayment.objects.create(customer=customer, date=day,
                                       amount=Decimal("100.00"),
                                       amount_uzs=Decimal("1200000"), method="cash")
    ctx = _ctx(admin_client, **{"from": "2026-07-01", "to": "2026-07-31"})
    assert ctx["net_in"] == Decimal("300.00")            # 01, 15, 31 — once each
    assert ctx["net_in_uzs"] == Decimal("3600000.00")
    assert ctx["income_page"].paginator.count == 3
    # and the opening balance carries exactly the one day before the window
    assert ctx["waterfall"][0]["amount"] == Decimal("100.00")
    assert ctx["waterfall"][0]["amount_uzs"] == Decimal("1200000.00")
    # opening + period == balance at the end of the window (07-31), all-time is more
    assert ctx["waterfall"][-1]["running"] == Decimal("400.00")
    assert ctx["cash_total"] == Decimal("500.00")


def test_a_one_day_window_neither_drops_nor_duplicates_its_row(admin_client):
    customer = _customer()
    CustomerPayment.objects.create(customer=customer, date="2026-07-15",
                                   amount=Decimal("250.00"),
                                   amount_uzs=Decimal("3000000"), method="cash")
    ctx = _ctx(admin_client, **{"from": "2026-07-15", "to": "2026-07-15"})
    assert ctx["net_in"] == Decimal("250.00")
    assert ctx["income_page"].paginator.count == 1
    assert ctx["waterfall"][0]["amount"] == Decimal("0")


# Regression guard. This was an xfail documenting a malformed ?from/?to crashing the
# view; it passes since _date_param (crm/views.py) drops a value that is not a real
# ISO date. Kept as a test so the crash cannot come back.
def test_a_malformed_date_filter_does_not_500_the_kassa(admin_client):
    """The screen itself prints dates as d.m.Y, so that is the shape an operator
    retypes into the URL."""
    _customer()
    assert admin_client.get("/kassa/", {"from": "15.07.2026"}).status_code == 200


# ===========================================================================
# ROUNDING QUANTA
# ===========================================================================

def test_the_usd_side_rounds_half_up_at_the_cent(admin_client):
    """60 so'm at 12 000 is exactly half a cent — the documented rule is HALF_UP."""
    logist = _logist()
    assert _post(admin_client, "/logist-payments/new/", {
        "logist": logist.pk, "date": "2026-07-10", "currency": "uzs",
        "amount": "60", "exchange_rate": "12000", "method": "cash",
        "fee_percent": "0", "note": "",
    }).status_code == 204
    p = LogistPayment.objects.get()
    assert (p.amount, p.amount_uzs) == (Decimal("0.01"), Decimal("60.00"))
    assert _ctx(admin_client)["cash_total_uzs"] == Decimal("-60.00")


@pytest.mark.xfail(reason="BUG (low): CashEntry.fee_amount quantizes ROUND_HALF_UP "
                          "but SupplierPayment.commission_amount uses a bare "
                          "quantize(), i.e. Python's ROUND_HALF_EVEN — the same "
                          "10.10 at 5% is 0.51 as a bank foiz and 0.50 as a "
                          "vositachi cut",
                   strict=False)
def test_both_percentage_cuts_round_by_the_same_rule(admin_client):
    contract = _contract()
    p = SupplierPayment.objects.create(
        contract=contract, date="2026-07-12", amount=Decimal("10.10"),
        amount_uzs=Decimal("121200"), method="transfer",
        commission_percent=Decimal("5"), fee_percent=Decimal("5"))
    assert p.commission_amount == p.fee_amount == Decimal("0.51")


# ===========================================================================
# HELD vs OWNED — the till is not all ours
# ===========================================================================

def test_the_mijoz_avans_is_carved_off_the_till_in_both_currencies(admin_client):
    """A so'm to'lov sitting on no sotuv is held in so'm, not re-rated dollars."""
    customer = _customer()
    _post(admin_client, "/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-10",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
        "form-0-currency": "uzs", "form-0-amount": "12650000",
        "form-0-exchange_rate": "12650", "form-0-method": "cash",
        "form-0-fee_percent": "0", "form-0-note": "",
    })
    ctx = _ctx(admin_client)
    assert ctx["cash_total"] == Decimal("1000.00")
    assert ctx["cash_total_uzs"] == Decimal("12650000.00")
    assert ctx["advance"] == Decimal("1000.00")
    assert ctx["advance_uzs"] == Decimal("12650000.00")
    assert ctx["own_cash"] == ctx["cash_total"] - ctx["advance"] == Decimal("0.00")
    assert ctx["own_cash_uzs"] == ctx["cash_total_uzs"] - ctx["advance_uzs"]


# ===========================================================================
# DELETION — a row others depend on
# ===========================================================================

def test_deleting_a_yuk_takes_its_expenses_out_of_the_kassa(admin_client):
    contract = _contract()
    ship = _shipment(contract)
    customer = _customer()
    CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                   amount=Decimal("1000.00"),
                                   amount_uzs=Decimal("12000000"), method="cash")
    ShipmentExpense.objects.create(shipment=ship, date="2026-07-12",
                                   category="customs", amount=Decimal("150.00"),
                                   amount_uzs=Decimal("1800000"), method="cash")
    assert _ctx(admin_client)["cash_total"] == Decimal("850.00")

    ship.lines.all().delete()
    ship.delete()                                   # CASCADE takes the expenses
    ctx = _ctx(admin_client)
    assert not ShipmentExpense.objects.exists()
    assert ctx["cash_total"] == Decimal("1000.00")
    assert ctx["cash_total_uzs"] == Decimal("12000000.00")
    assert ctx["waterfall"][-1]["running"] == ctx["cash_total"]


def test_deleting_a_mijoz_tolov_returns_the_kassa_to_where_it_was(admin_client):
    customer = _customer()
    base = _ctx(admin_client)["cash_total"]
    _post(admin_client, "/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-10",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
        "form-0-currency": "uzs", "form-0-amount": "12000000",
        "form-0-exchange_rate": "12000", "form-0-method": "cash",
        "form-0-fee_percent": "0", "form-0-note": "",
    })
    p = CustomerPayment.objects.get()
    assert _ctx(admin_client)["cash_total_uzs"] == Decimal("12000000.00")
    assert _post(admin_client, f"/customer-payments/{p.pk}/delete/", {}).status_code == 204
    ctx = _ctx(admin_client)
    assert ctx["cash_total"] == base
    assert ctx["cash_total_uzs"] == Decimal("0")
    assert ctx["advance"] == Decimal("0")
