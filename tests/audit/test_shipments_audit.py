"""Audit pass over Yuk (shipment), ShipmentLine, legs, extend and status.

Written as a diagnosis, not a fix: every test either passes (the behaviour is what
the docstrings say it should be) or is marked xfail with the defect it documents.

The four probe families the product owner's symptoms map onto:
  (a) round-trip     — the typed side of a money pair must survive bit-exact
  (b) idempotence    — re-saving an untouched row must not move any figure
  (c) stickiness     — a row entered in so'm must stay so'm through save + reopen
  (d) aggregates     — a total must equal the sum of its parts, mixed kursi included
"""
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import pytest
from crm.models import (
    LEGACY_RATE,
    Contract, ContractLine, Currency, Customer, Logist, LogistPayment, Partner, Sale,
    Shipment, ShipmentDelay, ShipmentExpense, ShipmentLeg, ShipmentLine, ShipmentStatus,
    convert_pair,
)
from crm.templatetags.crm_extras import NBSP

pytestmark = pytest.mark.django_db


# --- fixtures/builders ------------------------------------------------------

def _partner(name="Pars"):
    return Partner.objects.create(name=name, phone="1", city="Tehron")


def _contract_line(contract, brand="LLDPE", kg="10000", typed="1.00",
                   currency=Currency.USD, rate="12000"):
    """A kelishuv product priced the way the real form prices it: the operator
    types `typed` in `currency`, both sides are stored at `rate`."""
    usd, uzs = convert_pair(Decimal(typed), currency, Decimal(rate), "0.0001")
    return ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=usd, price_uzs=uzs,
        currency=currency, exchange_rate=Decimal(rate))


def _contract(**kw):
    """A kelishuv and its single product, both struck in the same currency — the
    product cannot pick its own any more."""
    contract = Contract.objects.create(
        partner=_partner(), created="2026-07-01",
        currency=kw.get("currency", Currency.USD))
    _contract_line(contract, **kw)
    return contract


def _rows(*rows, initial=0):
    """Mahsulotlar formset payload; carries `id` for edits. Nothing is defaulted in —
    a yuk row has no valyuta or kurs box of its own, both come off the kelishuv."""
    data = {"lines-TOTAL_FORMS": str(len(rows)), "lines-INITIAL_FORMS": str(initial),
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000"}
    for i, row in enumerate(rows):
        for key, value in row.items():
            data[f"lines-{i}-{key}"] = "" if value is None else str(value)
    return data


@contextmanager
def _inherited_kurs(kurs):
    """Pin what a fresh row inherits as its kurs — the probes used to type it into a
    box that no longer exists, so they say it here instead. Blank, zero or negative
    means "whatever the app would have found", which is LEGACY_RATE in an empty book."""
    if kurs is None or Decimal(str(kurs or 0)) <= 0:
        yield
        return
    with patch("crm.forms.latest_exchange_rate", return_value=Decimal(str(kurs))):
        yield


def _header(contract, **extra):
    data = {"contract": contract.pk, "status": ShipmentStatus.objects.first().pk,
            "sent": "2026-07-05", "eta": "2026-07-20", "logist": "",
            "driver_advance": "", "responsible": "", "driver_name": "",
            "driver_phone": "", "transport": "01 A 111", "container": "", "note": ""}
    data.update(extra)
    return data


def _create(client, contract, *rows, **extra):
    """A row's `currency` is dropped (it is the kelishuv's) and its `exchange_rate`
    becomes the rate the row inherits, so the probes read as they always did."""
    rows = [dict(row) for row in rows]
    kurs = None
    for row in rows:
        row.pop("currency", None)
        kurs = row.pop("exchange_rate", kurs)
    with _inherited_kurs(kurs):
        return client.post("/shipments/new/",
                           {**_header(contract, **extra), **_rows(*rows)})


def _one_line_yuk(client, contract, **row):
    """Create a yuk with a single product row through the real view."""
    row = {"contract_line": contract.lines.first().pk, "kg": "1000", **row}
    resp = _create(client, contract, row)
    assert resp.status_code == 302, resp.context["form"].errors if resp.context else resp
    return Shipment.objects.latest("pk")


def _echo_edit_payload(client, shipment, **overrides):
    """Exactly what the browser would POST back if the operator opened the edit
    modal and pressed Save without touching anything.

    Built from the bound fields the template renders ({{ field }} → field.value()),
    so this is a faithful "untouched re-save" rather than a payload we invented.
    """
    resp = client.get(f"/shipments/{shipment.pk}/edit/")
    assert resp.status_code == 200
    data = {}

    def collect(form):
        for name in form.fields:
            bound = form[name]
            value = bound.value()
            if value is None or value is False:
                data[bound.html_name] = ""
            elif value is True:
                data[bound.html_name] = "on"
            else:
                data[bound.html_name] = str(value)

    collect(resp.context["form"])
    lines = resp.context["lines"]
    collect(lines.management_form)
    for sub in lines.forms:
        collect(sub)
    data.update(overrides)
    return data


def _money_snapshot(shipment):
    shipment.refresh_from_db()
    return [(ln.pk, ln.kg, ln.price, ln.price_uzs, ln.currency, ln.exchange_rate)
            for ln in shipment.lines.order_by("pk")]


# =====================================================================
# (a) ROUND-TRIP — the typed side is stored bit-exact, the other derived
# =====================================================================

def test_a_som_line_stores_the_typed_som_bit_exact(admin_client):
    """14 040 so'm/kg typed at 12 000 → the so'm side is the agreed figure, the
    dollar side is derived once at four decimals."""
    contract = _contract(currency=Currency.UZS, typed="14040", rate="12000")
    yuk = _one_line_yuk(admin_client, contract,
                        currency="uzs", price="14040", exchange_rate="12000")
    line = yuk.lines.get()
    assert line.currency == Currency.UZS
    assert line.price_uzs == Decimal("14040.00")   # typed, untouched
    assert line.price == Decimal("1.1700")         # derived
    assert line.exchange_rate == Decimal("12000.00")
    # and each side of the value is built from its own stored column, never from
    # a conversion of the other
    assert line.goods_value == Decimal("1170.00")
    assert line.goods_value_uzs == Decimal("14040000.00")


def test_a_dollar_line_stores_the_typed_dollar_bit_exact(admin_client):
    contract = _contract()
    yuk = _one_line_yuk(admin_client, contract,
                        currency="usd", price="1.17", exchange_rate="12000")
    line = yuk.lines.get()
    assert line.currency == Currency.USD
    assert line.price == Decimal("1.1700")
    assert line.price_uzs == Decimal("14040.00")


def test_a_som_narx_that_does_not_divide_keeps_four_decimals(admin_client):
    """A per-kg narx carries four decimals, not two — 12 345 so'm at 10 850 is
    1.1378 $/kg, and rounding that to 1.14 would move a 24-tonne lot by $53."""
    contract = _contract(kg="30000", currency=Currency.UZS, typed="12345", rate="10850")
    yuk = _one_line_yuk(admin_client, contract, kg="24000",
                        currency="uzs", price="12345", exchange_rate="10850")
    line = yuk.lines.get()
    assert line.price_uzs == Decimal("12345.00")
    assert line.price == Decimal("1.1378")         # 1.137788… half-up at 4dp
    # and the so'm value of the load is the typed narx times the kg, undiluted
    assert line.goods_value_uzs == Decimal("296280000.00")


# =====================================================================
#     boundaries: blank kurs, zero kurs, zero/negative narx, extremes
# =====================================================================

def test_a_priced_line_always_has_a_usable_kurs_to_inherit(admin_client):
    """A narx with no kurs has only one of its two values and could never join a
    so'm total. The operator is no longer asked for one, so the guarantee moved: the
    row inherits the last rate entered, and an empty book still yields LEGACY_RATE —
    there is no path left that reaches convert_pair without one."""
    contract = _contract(currency=Currency.UZS, typed="14040", rate="12000")
    resp = _create(admin_client, contract,
                   {"contract_line": contract.lines.first().pk, "kg": "1000",
                    "price": "14040"})
    assert resp.status_code == 302
    line = Shipment.objects.latest("pk").lines.get()
    assert line.exchange_rate == LEGACY_RATE
    assert line.price_uzs == Decimal("14040.00")
    assert line.price == Decimal("1.1700")           # 14040 / 12000


@pytest.mark.parametrize("price", ["0", "-1.5"])
def test_a_zero_or_negative_narx_is_refused(admin_client, price):
    contract = _contract()
    resp = _create(admin_client, contract,
                   {"contract_line": contract.lines.first().pk, "kg": "1000",
                    "currency": "usd", "price": price, "exchange_rate": "12000"})
    assert resp.status_code == 200
    assert not Shipment.objects.exists()


def test_an_extreme_kurs_still_round_trips_the_typed_side(admin_client):
    """A tiny kurs and a huge one are both storable; the typed side must survive
    either, because it is never re-derived from the conversion."""
    huge = _contract(kg="20000")
    yuk = _one_line_yuk(admin_client, huge, kg="1000",
                        currency="usd", price="1.17", exchange_rate="999999.99")
    line = yuk.lines.get()
    assert line.price == Decimal("1.1700")
    assert line.price_uzs == Decimal("1169999.99")

    tiny = _contract(kg="20000", currency=Currency.UZS, typed="5", rate="0.01")
    yuk2 = _one_line_yuk(admin_client, tiny, kg="1000",
                         currency="uzs", price="5", exchange_rate="0.01")
    line2 = yuk2.lines.get()
    assert line2.price_uzs == Decimal("5.00")      # typed side untouched
    assert line2.price == Decimal("500.0000")      # 5 / 0.01


# =====================================================================
# (c) CURRENCY STICKINESS
# =====================================================================

def test_a_som_line_saves_as_som_and_reopens_bound_to_som(admin_client):
    contract = _contract(currency=Currency.UZS, typed="14040", rate="12000")
    yuk = _one_line_yuk(admin_client, contract,
                        currency="uzs", price="14040", exchange_rate="12000")
    line = yuk.lines.get()
    assert line.currency == "uzs"
    # the so'm column holds the typed figure, NOT a dollar-interpreted one
    assert line.price_uzs == Decimal("14040.00")
    assert line.price_uzs != Decimal("14040") * Decimal("12000")

    resp = admin_client.get(f"/shipments/{yuk.pk}/edit/")
    sub = resp.context["lines"].forms[0]
    # No pickers on the row any more — it reads so'm because its kelishuv does, and
    # the kurs it was booked at stays on the row without being typed.
    assert "currency" not in sub.fields and "exchange_rate" not in sub.fields
    assert Decimal(str(sub["price"].value())) == Decimal("14040.00")
    assert line.exchange_rate == Decimal("12000.00")


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_form_shows_the_som_figure_in_the_narx_box(admin_client):
    contract = _contract(currency=Currency.UZS, typed="14040", rate="12000")
    yuk = _one_line_yuk(admin_client, contract,
                        currency="uzs", price="14040", exchange_rate="12000")
    resp = admin_client.get(f"/shipments/{yuk.pk}/edit/")
    sub = resp.context["lines"].forms[0]
    assert Decimal(str(sub["price"].value())) == Decimal("14040.00")


def test_a_lot_cannot_be_switched_to_the_other_currency_on_its_own(admin_client):
    """The old complaint was that a row would not stay on the currency it was set to.
    It cannot be set at all now: a lot is priced in its kelishuv's currency, so a
    posted valyuta is ignored rather than half-applied to one row of a truck whose
    qarz is measured on the other side."""
    contract = _contract()
    yuk = _one_line_yuk(admin_client, contract,
                        currency="usd", price="1.17", exchange_rate="12000")
    payload = _echo_edit_payload(admin_client, yuk, **{
        "lines-0-currency": "uzs", "lines-0-price": "1.30",
        "lines-0-exchange_rate": "13000"})
    assert admin_client.post(f"/shipments/{yuk.pk}/edit/", payload).status_code == 302
    line = yuk.lines.get()
    line.refresh_from_db()
    assert line.currency == Currency.USD == contract.currency
    assert line.price == Decimal("1.3000")           # the narx did land
    assert line.exchange_rate == Decimal("12000.00")  # at the kurs it was booked with


# =====================================================================
# (b) IDEMPOTENCE / NO-DRIFT
# =====================================================================

def test_resaving_an_untouched_dollar_lot_moves_nothing(admin_client):
    contract = _contract()
    yuk = _one_line_yuk(admin_client, contract,
                        currency="usd", price="1.17", exchange_rate="12000")
    before = _money_snapshot(yuk)
    for _ in range(2):
        payload = _echo_edit_payload(admin_client, yuk)
        assert admin_client.post(f"/shipments/{yuk.pk}/edit/", payload).status_code == 302
        assert _money_snapshot(yuk) == before


def test_resaving_an_untouched_som_lot_moves_nothing(admin_client):
    """Passes, but only by luck: the re-post is byte-identical to what the form
    rendered, so Django's `has_changed()` skips the row and never writes it. The
    row's money survives because nothing touched it — see the kg test below for
    what happens the moment anything on the row does change."""
    contract = _contract(currency=Currency.UZS, typed="14040", rate="12000")
    yuk = _one_line_yuk(admin_client, contract,
                        currency="uzs", price="14040", exchange_rate="12000")
    before = _money_snapshot(yuk)
    for _ in range(2):
        payload = _echo_edit_payload(admin_client, yuk)
        assert admin_client.post(f"/shipments/{yuk.pk}/edit/", payload).status_code == 302
        assert _money_snapshot(yuk) == before


def test_editing_only_the_transport_leaves_a_som_lots_money_alone(admin_client):
    """A header-only edit is safe — the Mahsulot row is unchanged, so it is not
    rewritten."""
    contract = _contract(currency=Currency.UZS, typed="14040", rate="12000")
    yuk = _one_line_yuk(admin_client, contract,
                        currency="uzs", price="14040", exchange_rate="12000")
    before = _money_snapshot(yuk)
    payload = _echo_edit_payload(admin_client, yuk, transport="02 B 222")
    assert admin_client.post(f"/shipments/{yuk.pk}/edit/", payload).status_code == 302
    yuk.refresh_from_db()
    assert yuk.transport == "02 B 222"
    assert _money_snapshot(yuk) == before


def test_correcting_the_kg_of_a_dollar_lot_leaves_its_narx_alone(admin_client):
    """The control for the test below: on a dollar lot, correcting the kg rewrites
    the row and the narx comes back out unchanged."""
    contract = _contract()
    yuk = _one_line_yuk(admin_client, contract,
                        currency="usd", price="1.17", exchange_rate="12000")
    payload = _echo_edit_payload(admin_client, yuk, **{"lines-0-kg": "1100"})
    assert admin_client.post(f"/shipments/{yuk.pk}/edit/", payload).status_code == 302
    line = yuk.lines.get()
    assert line.kg == Decimal("1100.000")
    assert line.price == Decimal("1.1700") and line.price_uzs == Decimal("14040.00")


# Regression guard. Was an xfail documenting the so'm-edit defect; it passes since
# MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing its
# so'm figure. Kept as a test so the defect cannot come back.
def test_correcting_the_kg_of_a_som_lot_leaves_its_narx_alone(admin_client):
    contract = _contract(currency=Currency.UZS, typed="14040", rate="12000")
    yuk = _one_line_yuk(admin_client, contract,
                        currency="uzs", price="14040", exchange_rate="12000")
    payload = _echo_edit_payload(admin_client, yuk, **{"lines-0-kg": "1100"})
    assert admin_client.post(f"/shipments/{yuk.pk}/edit/", payload).status_code == 302
    line = yuk.lines.get()
    assert line.kg == Decimal("1100.000")
    assert line.currency == "uzs"
    assert line.price_uzs == Decimal("14040.00")
    assert line.price == Decimal("1.1700")


# Regression guard. Was an xfail documenting the same defect the logist audit pins:
# an advance already handed over kept being re-rated at whatever the logist's newest
# funding kurs happened to be, on a save that had nothing to do with it.
def test_a_driver_advance_is_not_re_rated_when_the_yuk_is_resaved(admin_client, admin_user):
    logist = Logist.objects.create(name="Sardor", phone="1")
    LogistPayment.objects.create(logist=logist, date="2026-07-01", amount=Decimal("1000"),
                                 amount_uzs=Decimal("12000000"), currency=Currency.USD,
                                 exchange_rate=Decimal("12000"))
    contract = _contract()
    row = {"contract_line": contract.lines.first().pk, "kg": "1000",
           "currency": "usd", "price": "1.17", "exchange_rate": "12000"}
    resp = _create(admin_client, contract, row,
                   logist=logist.pk, driver_advance="100")
    assert resp.status_code == 302
    yuk = Shipment.objects.latest("pk")
    advance = yuk.expenses.get(is_driver_advance=True)
    assert (advance.amount, advance.amount_uzs) == (Decimal("100.00"),
                                                    Decimal("1200000.00"))

    # weeks later the logist is topped up again, at a different kurs
    LogistPayment.objects.create(logist=logist, date="2026-07-25", amount=Decimal("500"),
                                 amount_uzs=Decimal("6500000"), currency=Currency.USD,
                                 exchange_rate=Decimal("13000"))
    payload = _echo_edit_payload(admin_client, yuk, note="Izoh tuzatildi")
    assert admin_client.post(f"/shipments/{yuk.pk}/edit/", payload).status_code == 302

    advance.refresh_from_db()
    assert advance.amount_uzs == Decimal("1200000.00")
    assert advance.exchange_rate == Decimal("12000.00")


# =====================================================================
# (d) AGGREGATE CONSISTENCY — mixed currencies, mixed kursi
# =====================================================================

def test_a_yuk_total_equals_its_lines_across_two_brands(admin_client):
    """One truck, two so'm-priced brands. Each side of the yuk total is the plain sum
    of that side's rows — never a re-conversion of the other."""
    contract = Contract.objects.create(partner=_partner(), created="2026-07-01",
                                       currency=Currency.UZS)
    a = _contract_line(contract, brand="LLDPE", kg="10000", typed="13000",
                       currency=Currency.UZS, rate="13000")
    b = _contract_line(contract, brand="HDPE", kg="10000", typed="16901",
                       currency=Currency.UZS, rate="13000")
    resp = _create(
        admin_client, contract,
        {"contract_line": a.pk, "kg": "1000", "price": "16250",
         "exchange_rate": "13000"},
        {"contract_line": b.pk, "kg": "2000", "price": "16901"})
    assert resp.status_code == 302
    yuk = Shipment.objects.latest("pk")

    rows = list(yuk.lines.all())
    assert {r.currency for r in rows} == {Currency.UZS}
    assert (yuk.goods_value_uzs == sum(r.goods_value_uzs for r in rows)
            == Decimal("50052000.00"))         # 16 250 000 + 33 802 000
    assert yuk.goods_value == sum(r.goods_value for r in rows)
    assert yuk.kg == Decimal("3000.000")


def test_the_kelishuv_shipped_value_equals_the_sum_of_its_yuk_lines(admin_client):
    """What we owe the hamkor is built out of the trucks; both currency sides must
    reconcile to the same rows the Yuklar page shows."""
    contract = Contract.objects.create(partner=_partner(), created="2026-07-01")
    a = _contract_line(contract, brand="LLDPE", kg="10000", typed="1.00")
    b = _contract_line(contract, brand="HDPE", kg="10000", typed="1.00")
    _create(admin_client, contract,
            {"contract_line": a.pk, "kg": "1000", "price": "1.25",
             "exchange_rate": "12000"})
    _create(admin_client, contract,
            {"contract_line": b.pk, "kg": "2000", "price": "1.10",
             "exchange_rate": "13000"})
    contract.refresh_from_db()

    lines = [ln for yuk in contract.shipments.all() for ln in yuk.lines.all()]
    assert contract.shipped_value == sum(ln.goods_value for ln in lines)
    assert contract.shipped_value_uzs == sum(ln.goods_value_uzs for ln in lines)
    assert contract.shipped_value == Decimal("1250.00") + Decimal("2200.00")
    # each truck carries the kurs of the day it was booked, so the so'm side is not
    # the dollar side times any single rate
    assert contract.shipped_value_uzs == Decimal("15000000.00") + Decimal("28600000.00")
    assert contract.shipped_value_own == contract.shipped_value


def test_landed_cost_is_the_narx_plus_the_freight_share_plus_the_vositachi_cut(admin_client):
    """Freight is charged per truck, so it splits by kg across every brand on it;
    the yuk's per-kg figure and each lot's landed cost must agree."""
    contract = Contract.objects.create(partner=_partner(), created="2026-07-01")
    a = _contract_line(contract, brand="LLDPE", kg="10000", typed="1.00")
    b = _contract_line(contract, brand="HDPE", kg="10000", typed="2.00")
    resp = _create(admin_client, contract,
                   {"contract_line": a.pk, "kg": "1000", "currency": "usd",
                    "price": "1.25", "exchange_rate": "12000"},
                   {"contract_line": b.pk, "kg": "3000", "currency": "usd",
                    "price": "2.50", "exchange_rate": "12000"})
    assert resp.status_code == 302
    yuk = Shipment.objects.latest("pk")
    ShipmentExpense.objects.create(
        shipment=yuk, date="2026-07-06", category=ShipmentExpense.Category.CUSTOMS,
        amount=Decimal("800"), amount_uzs=Decimal("9600000"),
        currency=Currency.USD, exchange_rate=Decimal("12000"))

    yuk.refresh_from_db()
    assert yuk.expenses_total == Decimal("800.00")
    assert yuk.expense_per_kg == Decimal("800") / Decimal("4000.000")   # 0.20/kg
    lots = {ln.brand: ln for ln in yuk.lines.all()}
    assert lots["LLDPE"].landed_cost_per_kg == Decimal("1.4500")
    assert lots["HDPE"].landed_cost_per_kg == Decimal("2.7000")
    # total landed value == goods + freight, to the cent
    total = sum(ln.kg * ln.landed_cost_per_kg for ln in yuk.lines.all())
    assert total == yuk.goods_value + yuk.expenses_total


# =====================================================================
#     price inheritance: a blank narx means "use the kelishuv's"
# =====================================================================

def test_a_blank_narx_inherits_the_dollar_kelishuv_price_on_both_sides(admin_client):
    contract = _contract(typed="1.30", currency=Currency.USD, rate="12000")
    yuk = _one_line_yuk(admin_client, contract, kg="1000", price="")
    line = yuk.lines.get()
    assert line.price is None and line.price_uzs is None
    assert line.unit_price == Decimal("1.3000")
    assert line.unit_price_uzs == Decimal("15600.00")
    assert line.unit_currency == Currency.USD
    assert line.goods_value == Decimal("1300.00")
    assert line.goods_value_uzs == Decimal("15600000.00")


def test_a_blank_narx_inherits_a_som_kelishuv_narx_and_reads_in_som(admin_client):
    """A truck line that sets no narx of its own must report the kelishuv's narx in
    the kelishuv's currency — printing a so'm figure with a dollar sign was the
    documented reason `unit_currency` exists."""
    contract = _contract(typed="16900", currency=Currency.UZS, rate="13000")
    yuk = _one_line_yuk(admin_client, contract, kg="1000", price="",
                        currency="usd", exchange_rate="12000")
    line = yuk.lines.get()
    assert line.unit_currency == Currency.UZS
    assert line.unit_price_uzs == Decimal("16900.00")
    assert line.goods_value_uzs == Decimal("16900000.00")


@pytest.mark.xfail(reason="BUG: landed_cost_per_kg_uzs restates the cost through "
                          "in_som(), which uses the LINE's own exchange_rate. A line "
                          "with a blank narx has no kurs of its own (the field keeps "
                          "whatever the modal happened to post, default 12 000), so a "
                          "lot inheriting a 13 000-kurs so'm kelishuv narx is costed "
                          "in the ombor at 12 000 — ~7.7% off. crm/models.py "
                          "ShipmentLine.landed_cost_per_kg_uzs.",
                   strict=False)
def test_an_inherited_narx_is_costed_in_som_at_the_kelishuvs_own_kurs(admin_client):
    contract = _contract(typed="16900", currency=Currency.UZS, rate="13000")
    yuk = _one_line_yuk(admin_client, contract, kg="1000", price="",
                        currency="usd", exchange_rate="12000")
    line = yuk.lines.get()
    # no expenses and no vositachi cut, so tannarx == the narx itself
    assert line.landed_cost_per_kg == line.unit_price
    assert line.landed_cost_per_kg_uzs == line.unit_price_uzs


@pytest.mark.xfail(reason="BUG (same root, user-visible end): Ombor prints a lot's "
                          "tannarx with `lot.currency` (templates/crm/ombor.html:89, "
                          "shipment_detail.html:98, shipment_list.html:222) instead "
                          "of `lot.unit_currency`, which is the property that exists "
                          "precisely to answer 'which currency should this narx be "
                          "READ in'. A lot inheriting a so'm kelishuv narx is "
                          "therefore printed as $/kg — and the so'm figure behind it "
                          "is the wrong one anyway (see the kurs test above).",
                   strict=False)
def test_ombor_prints_an_inherited_som_tannarx_in_som(admin_client):
    contract = _contract(typed="16900", currency=Currency.UZS, rate="13000")
    yuk = _one_line_yuk(admin_client, contract, kg="1000", price="",
                        currency="usd", exchange_rate="12000")
    admin_client.post(f"/shipments/{yuk.pk}/status/", {"status": ShipmentStatus.arrival().pk})
    html = admin_client.get("/ombor/").content.decode()
    assert "so'm/kg" in html and "$/kg" not in html
    assert f"16{NBSP}900 so'm/kg" in html


def test_editing_the_kelishuv_narx_moves_inherited_lots_but_not_priced_ones(admin_client):
    """Documented design: `unit_price` reads the kelishuv price live, so an
    inheriting truck re-prices when the kelishuv does, while a truck that set its
    own narx is frozen at what it actually went at."""
    contract = Contract.objects.create(partner=_partner(), created="2026-07-01")
    a = _contract_line(contract, brand="LLDPE", kg="10000", typed="1.00")
    b = _contract_line(contract, brand="HDPE", kg="10000", typed="1.00")
    _create(admin_client, contract,
            {"contract_line": a.pk, "kg": "1000", "price": ""},
            {"contract_line": b.pk, "kg": "1000", "currency": "usd",
             "price": "1.00", "exchange_rate": "12000"})
    yuk = Shipment.objects.latest("pk")
    assert yuk.goods_value == Decimal("2000.00")

    a.price = Decimal("2.0000")
    a.price_uzs = Decimal("24000.00")
    a.save()
    b.price = Decimal("2.0000")
    b.price_uzs = Decimal("24000.00")
    b.save()

    yuk = Shipment.objects.get(pk=yuk.pk)
    lots = {ln.brand: ln for ln in yuk.lines.all()}
    assert lots["LLDPE"].unit_price == Decimal("2.0000")   # inherited → moved
    assert lots["HDPE"].unit_price == Decimal("1.0000")    # own narx → frozen
    assert yuk.goods_value == Decimal("3000.00")


# =====================================================================
#     status, extend, legs, and rows other rows depend on
# =====================================================================

def test_extend_records_the_delay_and_moves_the_eta(admin_client):
    contract = _contract()
    yuk = _one_line_yuk(admin_client, contract)
    resp = admin_client.post(f"/shipments/{yuk.pk}/extend/",
                             {"new_eta": "2026-08-10", "reason": "Chegarada navbat"})
    assert resp.status_code == 302
    yuk.refresh_from_db()
    assert str(yuk.eta) == "2026-08-10"
    delay = ShipmentDelay.objects.get()
    assert str(delay.old_eta) == "2026-07-20" and str(delay.new_eta) == "2026-08-10"


@pytest.mark.xfail(reason="BUG: ShipmentExtendForm (crm/forms.py:624) validates "
                          "nothing, so uzaytirish accepts an ETA before the "
                          "dispatch date. ShipmentForm.clean rejects eta < sent, so "
                          "the yuk lands in a state its own edit modal then refuses "
                          "to save — and days_left goes negative on a load nobody "
                          "flagged.",
                   strict=False)
def test_extend_refuses_an_eta_before_the_dispatch_date(admin_client):
    contract = _contract()
    yuk = _one_line_yuk(admin_client, contract)   # sent 2026-07-05
    resp = admin_client.post(f"/shipments/{yuk.pk}/extend/",
                             {"new_eta": "2026-07-01", "reason": "Xato"})
    yuk.refresh_from_db()

    # the state it leaves behind: the yuk's own edit modal now refuses to save it,
    # so the load can no longer be corrected without first fixing the ETA
    wedged = admin_client.post(f"/shipments/{yuk.pk}/edit/",
                               _echo_edit_payload(admin_client, yuk))
    assert wedged.status_code == 302, "the edit modal can no longer save this yuk"

    assert resp.status_code == 200
    assert str(yuk.eta) == "2026-07-20"


def test_the_status_button_marks_a_yuk_arrived_and_back(admin_client):
    contract = _contract()
    yuk = _one_line_yuk(admin_client, contract)
    arrival = ShipmentStatus.arrival()
    admin_client.post(f"/shipments/{yuk.pk}/status/", {"status": arrival.pk})
    yuk.refresh_from_db()
    assert yuk.arrived is not None and yuk.is_lot

    back = ShipmentStatus.objects.filter(is_arrival=False).first()
    admin_client.post(f"/shipments/{yuk.pk}/status/", {"status": back.pk})
    yuk.refresh_from_db()
    assert yuk.arrived is None and not yuk.is_lot


# CLAIM UPHELD AND NOW CLOSED. `shipment_edit` never synced `arrived` with the chosen
# status, unlike `shipment_create` and `shipment_set_status`, so picking the arrival
# status in the edit modal left `arrived` NULL: the load read as arrived on Yuklar and
# never became a lot in the Ombor. It now follows the same rule as the other two —
# entering arrival stamps a date, leaving it clears one — which is what the Yetib
# kelgan sana field on that modal rests on.
def test_choosing_the_arrival_status_in_the_edit_modal_makes_it_a_lot(admin_client):
    contract = _contract()
    yuk = _one_line_yuk(admin_client, contract)
    arrival = ShipmentStatus.arrival()
    payload = _echo_edit_payload(admin_client, yuk, status=str(arrival.pk))
    assert admin_client.post(f"/shipments/{yuk.pk}/edit/", payload).status_code == 302
    yuk.refresh_from_db()
    assert yuk.status_id == arrival.pk
    assert yuk.arrived is not None and yuk.is_lot


@pytest.mark.xfail(reason="BUG: the Mahsulotlar formset only caps kg against the "
                          "KELISHUV's remaining kg (BaseShipmentLineFormSet.clean, "
                          "crm/forms.py:579). Nothing checks what has already been "
                          "SOLD off the lot, so shrinking an arrived lot below its "
                          "sold kg is accepted and available_kg goes negative — the "
                          "shelf silently loses the difference and the hamkor's debt "
                          "drops with it.",
                   strict=False)
def test_shrinking_an_arrived_lot_below_what_is_sold_is_refused(admin_client, admin_user):
    contract = _contract(kg="10000", typed="1.00")
    yuk = _one_line_yuk(admin_client, contract, kg="1000",
                        currency="usd", price="1.00", exchange_rate="12000")
    admin_client.post(f"/shipments/{yuk.pk}/status/", {"status": ShipmentStatus.arrival().pk})
    lot = yuk.lines.get()
    customer = Customer.objects.create(name="Alisher", phone="1", address="Toshkent")
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("800"),
                        price=Decimal("1.5000"), price_uzs=Decimal("18000.00"),
                        currency=Currency.USD, exchange_rate=Decimal("12000"),
                        date="2026-07-21")

    payload = _echo_edit_payload(admin_client, yuk, **{"lines-0-kg": "100"})
    resp = admin_client.post(f"/shipments/{yuk.pk}/edit/", payload)
    lot.refresh_from_db()
    # the harm first: the shelf must never hold less than nothing
    assert lot.available_kg >= 0, f"available_kg went to {lot.available_kg}"
    assert lot.kg == Decimal("1000.000")
    assert resp.status_code == 200          # refused, with an error on kg


@pytest.mark.xfail(reason="BUG: deleting a Mahsulot row that a sotuv hangs off "
                          "raises ProtectedError out of _save_lines "
                          "(crm/views.py:421) — an unhandled 500 rather than a form "
                          "error. shipment_delete catches exactly this case; the "
                          "line formset does not.",
                   strict=False)
def test_deleting_a_lot_a_sotuv_depends_on_fails_gracefully(admin_client, admin_user):
    """Two products on the truck so the min-one-product rule does not mask the
    delete; the first has already been sold from."""
    contract = Contract.objects.create(partner=_partner(), created="2026-07-01")
    a = _contract_line(contract, brand="LLDPE", kg="10000", typed="1.00")
    b = _contract_line(contract, brand="HDPE", kg="10000", typed="1.00")
    assert _create(admin_client, contract,
                   {"contract_line": a.pk, "kg": "1000", "currency": "usd",
                    "price": "1.00", "exchange_rate": "12000"},
                   {"contract_line": b.pk, "kg": "1000", "currency": "usd",
                    "price": "1.00", "exchange_rate": "12000"}).status_code == 302
    yuk = Shipment.objects.latest("pk")
    admin_client.post(f"/shipments/{yuk.pk}/status/", {"status": ShipmentStatus.arrival().pk})
    lot = yuk.lines.order_by("pk").first()
    customer = Customer.objects.create(name="Alisher", phone="1", address="Toshkent")
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("100"),
                        price=Decimal("1.5000"), price_uzs=Decimal("18000.00"),
                        currency=Currency.USD, exchange_rate=Decimal("12000"),
                        date="2026-07-21")

    payload = _echo_edit_payload(admin_client, yuk, **{"lines-0-DELETE": "on"})
    resp = admin_client.post(f"/shipments/{yuk.pk}/edit/", payload)
    assert resp.status_code in (200, 302)
    assert ShipmentLine.objects.filter(pk=lot.pk).exists()


def test_legs_do_not_disturb_the_loads_money(admin_client):
    """Legs carry no money; adding, reordering and deleting them must leave every
    figure on the load exactly where it was."""
    contract = _contract(currency=Currency.UZS, typed="14040", rate="12000")
    yuk = _one_line_yuk(admin_client, contract,
                        currency="uzs", price="14040", exchange_rate="12000")
    before = _money_snapshot(yuk)
    admin_client.post(f"/legs/new/?shipment={yuk.pk}", {
        "from_location": "Tehron", "to_location": "Chegara", "transport": "D1",
        "container": "", "departed": "2026-07-05", "arrived": "2026-07-08", "note": ""})
    admin_client.post(f"/legs/new/?shipment={yuk.pk}", {
        "from_location": "Chegara", "to_location": "Toshkent", "transport": "D2",
        "container": "", "departed": "2026-07-09", "arrived": "", "note": ""})
    yuk.refresh_from_db()
    assert yuk.current_transport == "D2"

    second = ShipmentLeg.objects.get(transport="D2")
    admin_client.post(f"/legs/{second.pk}/move/", {"dir": "up"})
    admin_client.post(f"/legs/{second.pk}/delete/", {})
    yuk.refresh_from_db()
    assert yuk.current_transport == "D1"        # falls back to the last leg
    assert _money_snapshot(yuk) == before
