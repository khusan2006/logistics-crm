"""Audit pass over Kelishuv (Contract) + ContractLine.

Diagnosis only — nothing outside tests/audit/ is touched. The probes follow the
symptoms the product owner reported:

  (a) ROUND-TRIP    — a narx typed in so'm and one typed in dollars must both come
                      back bit-exact on the side that was actually typed.
  (b) NO-DRIFT      — re-saving a kelishuv through the real view must not move a
                      single money figure, however many times it is done.
  (c) STICKINESS    — currency=uzs in must be currency=uzs out, and the edit form
                      must re-open showing the figure that was agreed.
  (d) AGGREGATES    — Jami / Qolgan to'lov must equal the sum of their parts even
                      when the products mix currencies and mix kurs values.

Tests marked xfail carry a "BUG:" reason and document a defect that is still live.
"""
from decimal import Decimal

import pytest
from django import forms as djforms
from django.db.models import ProtectedError

from conftest import line_data, make_shipment
from crm.models import (
    Contract, ContractLine, Currency, Partner, ShipmentLine, SupplierPayment,
)


# --- helpers ---------------------------------------------------------------

def _partner(name="Pars"):
    return Partner.objects.create(name=name, phone="1", city="Tehron")


def _create(client, *rows, partner=None, created="2026-07-01", note="", **extra):
    """Open a kelishuv through the real view and hand back the saved Contract."""
    partner = partner or _partner()
    payload = {"partner": partner.pk, "created": created, "note": note,
               "planned_trucks": "", **line_data(*rows), **extra}
    resp = client.post("/contracts/new/", payload)
    assert resp.status_code == 302, resp.context["lines"].errors if resp.context else resp
    return Contract.objects.order_by("-pk").first()


def _bound_values(form, data):
    """Copy a bound/unbound form's rendered values into a POST dict, the way the
    browser would submit the page it was rendered into."""
    for bf in form:
        value = bf.value()
        if isinstance(bf.field, djforms.BooleanField):
            if value:                      # a browser omits an unticked checkbox
                data[bf.html_name] = "on"
            continue
        data[bf.html_name] = "" if value is None else str(value)
    return data


def _reopen(client, contract):
    """GET the edit form and return the POST body the browser would send back if
    the operator pressed Saqlash without touching anything."""
    resp = client.get(f"/contracts/{contract.pk}/edit/")
    assert resp.status_code == 200
    data = {}
    _bound_values(resp.context["form"], data)
    _bound_values(resp.context["lines_after"], data)
    _bound_values(resp.context["lines"].management_form, data)
    for form in resp.context["lines"].forms:
        _bound_values(form, data)
    return data


def _resave(client, contract, **overrides):
    data = _reopen(client, contract)
    data.update(overrides)
    resp = client.post(f"/contracts/{contract.pk}/edit/", data)
    assert resp.status_code == 302, resp.context["lines"].errors if resp.context else resp
    return resp


def _money(line):
    line.refresh_from_db()
    return line.currency, line.price, line.price_uzs, line.exchange_rate


# --- (a) round-trip --------------------------------------------------------

def test_a_som_narx_is_stored_exactly_as_it_was_typed(admin_client, db):
    """12 650 so'm/kg at 12 650 so'm/$ is one dollar — and the so'm side must be
    the operator's own figure, never a value re-derived from that dollar."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    assert line.currency == Currency.UZS
    assert line.price_uzs == Decimal("12650.00")     # typed side, bit-exact
    assert line.price == Decimal("1.0000")           # derived side
    assert line.exchange_rate == Decimal("12650.00")


def test_a_dollar_narx_is_stored_exactly_as_it_was_typed(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.17",
                                      "exchange_rate": "12000"})
    line = contract.lines.get()
    assert line.currency == Currency.USD
    assert line.price == Decimal("1.1700")           # typed side, bit-exact
    assert line.price_uzs == Decimal("14040.00")     # derived side


def test_the_derived_dollar_side_of_a_som_narx_keeps_four_decimals(admin_client, db):
    """A narx carries 4dp where a lump sum carries 2 — rounding 12 345 so'm/kg to
    cents would move a 24-tonne truck by dollars."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "24000",
                                      "currency": "uzs", "price": "12345",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    assert line.price_uzs == Decimal("12345.00")
    assert line.price == Decimal("0.9759")           # 12345/12650 = 0.975889…
    assert line.total_value_uzs == Decimal("296280000.00")   # 24000 × 12 345


def test_a_narx_with_no_kurs_is_refused(admin_client, db):
    partner = _partner()
    resp = admin_client.post("/contracts/new/", {
        "partner": partner.pk, "created": "2026-07-01", "note": "", "planned_trucks": "",
        **line_data({"brand": "LLDPE", "kg": "1000", "currency": "uzs",
                     "price": "12650", "exchange_rate": ""})})
    assert resp.status_code == 200
    assert not Contract.objects.exists()


def test_a_narx_at_a_zero_kurs_is_refused(admin_client, db):
    partner = _partner()
    resp = admin_client.post("/contracts/new/", {
        "partner": partner.pk, "created": "2026-07-01", "note": "", "planned_trucks": "",
        **line_data({"brand": "LLDPE", "kg": "1000", "currency": "usd",
                     "price": "1.00", "exchange_rate": "0"})})
    assert resp.status_code == 200
    assert not Contract.objects.exists()


@pytest.mark.parametrize("bad", [{"price": "0"}, {"price": "-1"}, {"kg": "0"},
                                 {"kg": "-5"}, {"price": ""}])
def test_a_blank_zero_or_negative_figure_is_refused(admin_client, db, bad):
    partner = _partner()
    row = {"brand": "LLDPE", "kg": "1000", "currency": "usd", "price": "1.00",
           "exchange_rate": "12000", **bad}
    resp = admin_client.post("/contracts/new/", {
        "partner": partner.pk, "created": "2026-07-01", "note": "",
        "planned_trucks": "", **line_data(row)})
    assert resp.status_code == 200
    assert not Contract.objects.exists()


# --- (c) currency stickiness ----------------------------------------------

def test_the_saved_row_keeps_the_som_currency_it_was_entered_in(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    assert line.currency == "uzs"
    assert line.is_som
    # the so'm column holds the typed figure, not a dollar-interpreted one
    assert line.price_uzs == Decimal("12650.00")
    assert line.price != Decimal("12650.0000")


def test_the_edit_form_reopens_with_som_selected(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    resp = admin_client.get(f"/contracts/{contract.pk}/edit/")
    form = resp.context["lines"].forms[0]
    assert form["currency"].value() == "uzs"
    assert form["exchange_rate"].value() == Decimal("12650.00")


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_form_reopens_showing_the_som_narx_that_was_agreed(admin_client, db):
    """ReturnForm already does this right (crm/forms.py:917 seeds `price` with
    `sale.price_uzs` for a so'm sale). ContractLineForm never does, so the box the
    operator reads — and re-submits — is in the wrong currency."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    resp = admin_client.get(f"/contracts/{contract.pk}/edit/")
    form = resp.context["lines"].forms[0]
    assert Decimal(str(form["price"].value())) == Decimal("12650")


# --- (b) idempotence / no drift -------------------------------------------

def test_resaving_an_untouched_dollar_kelishuv_twice_moves_nothing(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.17",
                                      "exchange_rate": "12000"})
    line = contract.lines.get()
    before = _money(line)
    _resave(admin_client, contract)
    assert _money(line) == before
    _resave(admin_client, contract)
    assert _money(line) == before


def test_resaving_an_untouched_som_kelishuv_twice_moves_nothing(admin_client, db):
    """Survives only because Django skips a formset row whose fields did not
    change — see the next test for what happens the moment one of them does."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    before = _money(line)
    _resave(admin_client, contract)
    assert _money(line) == before
    _resave(admin_client, contract)
    assert _money(line) == before


def test_editing_an_unrelated_field_on_a_dollar_line_moves_no_money(admin_client, db):
    """Control for the so'm case below: the dollar side round-trips cleanly."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.17",
                                      "exchange_rate": "12000"})
    line = contract.lines.get()
    before = _money(line)
    _resave(admin_client, contract, **{"lines-0-brand": "LLDPE 209AA"})
    assert _money(line) == before
    _resave(admin_client, contract, **{"lines-0-kg": "1200"})
    assert _money(line) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_editing_the_marka_of_a_som_line_must_not_move_its_narx(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    before = _money(line)
    _resave(admin_client, contract, **{"lines-0-brand": "LLDPE 209AA"})
    assert _money(line) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_som_narx_does_not_decay_further_on_a_second_edit(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    before = _money(line)
    _resave(admin_client, contract, **{"lines-0-kg": "1100"})
    _resave(admin_client, contract, **{"lines-0-kg": "1200"})
    assert _money(line) == before


def test_editing_the_kg_of_a_som_line_leaves_its_narx_alone(admin_client, db):
    """The regression guard for the so'm-edit defect, on the kelishuv.

    This test used to pin the DAMAGE — it asserted the narx collapsing to 1.00
    so'm and the Jami to 1 200 — because that is what the code did. Now that
    MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
    its so'm figure, it asserts the opposite: correcting the kg is a kg edit and
    must leave the money exactly where it was."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    assert contract.total_value_uzs == Decimal("12650000.00")
    _resave(admin_client, contract, **{"lines-0-kg": "1200"})
    line.refresh_from_db()
    assert line.price_uzs == Decimal("12650.00")      # the narx that was agreed
    assert line.price == Decimal("1.0000")            # its dollar twin, unmoved
    # Jami follows the new kg only: 1 200 kg x 12 650 so'm.
    assert Contract.objects.get(pk=contract.pk).total_value_uzs == Decimal("15180000.00")


# --- (d) aggregates over mixed currencies and mixed kurs values ------------

def test_jami_is_the_sum_of_lines_that_mix_currency_and_kurs(admin_client, db):
    contract = _create(
        admin_client,
        {"brand": "LLDPE", "kg": "1000", "currency": "usd", "price": "1.20",
         "exchange_rate": "12000"},
        {"brand": "HDPE", "kg": "500", "currency": "uzs", "price": "13000",
         "exchange_rate": "13000"},
    )
    usd_line = contract.lines.get(brand="LLDPE")
    som_line = contract.lines.get(brand="HDPE")

    assert usd_line.total_value == Decimal("1200.00")
    assert usd_line.total_value_uzs == Decimal("14400000.00")
    assert som_line.total_value == Decimal("500.00")          # 500 × (13000/13000)
    assert som_line.total_value_uzs == Decimal("6500000.00")

    assert contract.total_value == usd_line.total_value + som_line.total_value
    assert contract.total_value_uzs == (usd_line.total_value_uzs
                                        + som_line.total_value_uzs)
    assert contract.kg == Decimal("1500.000")


def test_the_list_page_prints_the_same_jami_the_model_computes(admin_client, db):
    # `money_both` goes through format_html, so the apostrophe in "so'm" reaches the
    # page escaped — the expectation has to be escaped too, not the tag un-escaped.
    from django.utils.html import escape

    from crm.templatetags.crm_extras import som, usd

    contract = _create(
        admin_client,
        {"brand": "LLDPE", "kg": "1000", "currency": "usd", "price": "1.20",
         "exchange_rate": "12000"},
        {"brand": "HDPE", "kg": "500", "currency": "uzs", "price": "13000",
         "exchange_rate": "13000"},
    )
    html = admin_client.get("/contracts/", {"state": ""}).content.decode()
    assert escape(usd(contract.total_value)) in html
    assert escape(som(contract.total_value_uzs)) in html


@pytest.mark.xfail(reason="BUG: the Kelishuvlar Narx column renders a per-kg narx with "
                          "the lump-sum {% money %} tag instead of {% rate %}, so a 4dp "
                          "$/kg is shown rounded to cents (0.9759 reads as $0.98) and "
                          "loses its /kg unit — narx x kg no longer matches Jami",
                   strict=False)
def test_the_list_page_prints_a_per_kg_narx_at_its_full_precision(admin_client, db):
    from django.utils.html import escape

    from crm.templatetags.crm_extras import rate

    contract = _create(admin_client, {"brand": "LLDPE", "kg": "24000",
                                      "currency": "usd", "price": "0.9759",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    assert line.price == Decimal("0.9759")
    html = admin_client.get("/contracts/", {"state": ""}).content.decode()
    assert escape(rate(line.price, line.price_uzs, line.currency)) in html


def test_the_list_page_narx_column_rounds_to_cents_today(admin_client, db):
    """The measured half: what the Narx cell actually says, and the gap it opens
    against the Jami cell right beside it."""
    from django.utils.html import escape

    from crm.templatetags.crm_extras import usd

    contract = _create(admin_client, {"brand": "LLDPE", "kg": "24000",
                                      "currency": "usd", "price": "0.9759",
                                      "exchange_rate": "12650"})
    html = admin_client.get("/contracts/", {"state": ""}).content.decode()
    assert "$0.98" in html                                  # the narx as drawn
    assert escape(usd(contract.total_value)) in html        # 24000 x 0.9759 = 23 421.6
    # a reader multiplying the two columns is out by $98.40 on one kelishuv
    assert Decimal("24000") * Decimal("0.98") - contract.total_value == Decimal("98.40")

def test_expected_value_blends_shipped_prices_with_the_agreed_remainder(admin_client, db):
    """A truck may go at its own narx; what is left is still valued at the agreed
    one. Both currencies must follow the same rule."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.00",
                                      "exchange_rate": "12000"})
    line = contract.lines.get()
    shipment = make_shipment(contract=contract, kg="400", price="1.10")
    ShipmentLine.objects.filter(shipment=shipment).update(
        price=Decimal("1.10"), price_uzs=Decimal("14300.00"),
        exchange_rate=Decimal("13000"))

    assert line.shipped_kg == Decimal("400.000")
    assert line.remaining_kg == Decimal("600.000")
    assert line.shipped_value == Decimal("440.00")
    assert line.shipped_value_uzs == Decimal("5720000.00")
    # 440 shipped + 600 kg still to come at the agreed $1.00
    assert contract.expected_value == Decimal("1040.00")
    # 5 720 000 shipped (at the truck's 13 000 kurs) + 600 × 12 000 agreed
    assert contract.expected_value_uzs == Decimal("12920000.00")
    assert contract.payable_left == contract.expected_value - contract.paid_total
    assert contract.payable_left_uzs == (contract.expected_value_uzs
                                         - contract.paid_total_uzs)


def test_payable_left_nets_payments_that_arrived_in_different_currencies(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.00",
                                      "exchange_rate": "12000"})
    SupplierPayment.objects.create(contract=contract, amount=Decimal("400.00"),
                                   amount_uzs=Decimal("4800000.00"),
                                   currency=Currency.USD,
                                   exchange_rate=Decimal("12000"))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("200.00"),
                                   amount_uzs=Decimal("2600000.00"),
                                   currency=Currency.UZS,
                                   exchange_rate=Decimal("13000"))
    contract = Contract.objects.get(pk=contract.pk)

    assert contract.paid_total == Decimal("600.00")
    assert contract.paid_total_uzs == Decimal("7400000.00")
    assert contract.payable_left == Decimal("400.00")
    assert contract.payable_left_uzs == Decimal("4600000.00")


@pytest.mark.xfail(reason="BUG: a so'm kelishuv settled in full in so'm still shows a "
                          "dollar Qolgan to'lov and never leaves Tugallanmagan — "
                          "is_settled/payable_left read only the USD side, which was "
                          "derived at a different day's kurs than the payment",
                   strict=False)
def test_a_som_kelishuv_paid_in_full_in_som_counts_as_settled(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    line = contract.lines.get()
    make_shipment(contract=contract, kg="1000")          # every kg goes out
    # paid to the tiyin, in the currency the kelishuv was struck in, a week later
    SupplierPayment.objects.create(contract=contract, amount=Decimal("988.28"),
                                   amount_uzs=Decimal("12650000.00"),
                                   currency=Currency.UZS,
                                   exchange_rate=Decimal("12800"))
    contract = Contract.objects.get(pk=contract.pk)

    assert line.contract.total_value_uzs == Decimal("12650000.00")
    assert contract.payable_left_uzs == Decimal("0.00")   # nothing owed in so'm
    assert contract.payable_left <= 0                     # but the USD side says 11.72
    assert contract.is_settled


def test_a_som_kelishuv_settled_in_som_still_shows_a_dollar_payable(admin_client, db):
    """The measured half of the test above: the two halves of Qolgan to'lov
    disagree by the kurs move between the kelishuv and the to'lov."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "12650",
                                      "exchange_rate": "12650"})
    make_shipment(contract=contract, kg="1000")
    SupplierPayment.objects.create(contract=contract, amount=Decimal("988.28"),
                                   amount_uzs=Decimal("12650000.00"),
                                   currency=Currency.UZS,
                                   exchange_rate=Decimal("12800"))
    contract = Contract.objects.get(pk=contract.pk)

    assert contract.payable_left_uzs == Decimal("0.00")
    assert contract.payable_left == Decimal("11.72")
    assert contract.is_settled is False
    # and so it stays on the working list for ever
    resp = admin_client.get("/contracts/", {"state": "open"})
    assert [c.pk for c in resp.context["page"].object_list] == [contract.pk]


def test_the_to_lov_chips_can_leave_a_kelishuv_in_no_bucket(admin_client, db):
    """`paid` is measured against expected_value while `partial`/`unpaid` are
    measured against total_value. A truck sent above the agreed narx pulls the two
    apart, and a kelishuv paid exactly its agreed total matches none of the three
    chips — the faceted counts no longer add up to Hammasi."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.00",
                                      "exchange_rate": "12000"})
    shipment = make_shipment(contract=contract, kg="1000", price="1.10")
    ShipmentLine.objects.filter(shipment=shipment).update(
        price=Decimal("1.10"), price_uzs=Decimal("13200.00"))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("1000.00"),
                                   amount_uzs=Decimal("12000000.00"),
                                   currency=Currency.USD, exchange_rate=Decimal("12000"))
    contract = Contract.objects.get(pk=contract.pk)
    assert contract.total_value == Decimal("1000.00")
    assert contract.expected_value == Decimal("1100.00")

    resp = admin_client.get("/contracts/", {"state": ""})
    counts = {t["key"]: t["count"] for t in resp.context["pay_tabs"]}
    assert counts[""] == 1
    assert counts["paid"] + counts["partial"] + counts["unpaid"] == 0


# --- boundaries ------------------------------------------------------------

def test_shrinking_a_marka_below_what_already_shipped_is_refused(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.00",
                                      "exchange_rate": "12000"})
    make_shipment(contract=contract, kg="400")
    data = _reopen(admin_client, contract)
    data["lines-0-kg"] = "300"
    resp = admin_client.post(f"/contracts/{contract.pk}/edit/", data)
    assert resp.status_code == 200
    assert contract.lines.get().kg == Decimal("1000.000")


def test_the_same_marka_twice_is_refused(admin_client, db):
    partner = _partner()
    resp = admin_client.post("/contracts/new/", {
        "partner": partner.pk, "created": "2026-07-01", "note": "", "planned_trucks": "",
        **line_data({"brand": "LLDPE", "kg": "1000", "price": "1.00"},
                    {"brand": "lldpe", "kg": "500", "price": "1.10"})})
    assert resp.status_code == 200
    assert not Contract.objects.exists()


def test_a_kelishuv_cannot_lose_its_last_marka(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.00",
                                      "exchange_rate": "12000"})
    data = _reopen(admin_client, contract)
    data["lines-0-DELETE"] = "on"
    resp = admin_client.post(f"/contracts/{contract.pk}/edit/", data)
    assert resp.status_code == 200
    assert contract.lines.count() == 1


@pytest.mark.xfail(reason="BUG: removing a Mahsulot that already has trucks booked "
                          "against it raises an uncaught ProtectedError out of "
                          "_save_lines — a 500, where the kelishuv delete view "
                          "catches the same error and shows a message",
                   strict=False)
def test_removing_a_marka_that_has_trucks_is_refused_gracefully(admin_client, db):
    contract = _create(
        admin_client,
        {"brand": "LLDPE", "kg": "1000", "currency": "usd", "price": "1.00",
         "exchange_rate": "12000"},
        {"brand": "HDPE", "kg": "500", "currency": "usd", "price": "1.10",
         "exchange_rate": "12000"},
    )
    make_shipment(contract_line=contract.lines.get(brand="LLDPE"), kg="400")
    data = _reopen(admin_client, contract)
    data["lines-0-DELETE"] = "on"
    resp = admin_client.post(f"/contracts/{contract.pk}/edit/", data)
    assert resp.status_code in (200, 302)
    assert contract.lines.filter(brand="LLDPE").exists()


def test_removing_a_marka_that_has_trucks_raises_today(admin_client, db):
    """The measured half of the test above — pinned so the 500 is not a surprise."""
    contract = _create(
        admin_client,
        {"brand": "LLDPE", "kg": "1000", "currency": "usd", "price": "1.00",
         "exchange_rate": "12000"},
        {"brand": "HDPE", "kg": "500", "currency": "usd", "price": "1.10",
         "exchange_rate": "12000"},
    )
    make_shipment(contract_line=contract.lines.get(brand="LLDPE"), kg="400")
    data = _reopen(admin_client, contract)
    data["lines-0-DELETE"] = "on"
    with pytest.raises(ProtectedError):
        admin_client.post(f"/contracts/{contract.pk}/edit/", data)


def test_deleting_a_kelishuv_with_trucks_is_refused_gracefully(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.00",
                                      "exchange_rate": "12000"})
    make_shipment(contract=contract, kg="400")
    resp = admin_client.post(f"/contracts/{contract.pk}/delete/")
    assert resp.status_code == 302
    assert Contract.objects.filter(pk=contract.pk).exists()


def test_a_kurs_so_large_the_dollar_side_rounds_to_nothing(admin_client, db):
    """Boundary: at 100 000 000 so'm/$ a 1 000 so'm/kg narx is worth 0.00001 $/kg,
    which does not survive the 4dp column. The kelishuv then reads $0 in Jami while
    its so'm value is intact — the USD aggregates silently lose the line."""
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "uzs", "price": "1000",
                                      "exchange_rate": "100000000"})
    line = contract.lines.get()
    assert line.price_uzs == Decimal("1000.00")
    assert line.price == Decimal("0.0000")
    assert contract.total_value == Decimal("0.00")
    assert contract.total_value_uzs == Decimal("1000000.00")


def test_a_tiny_kurs_still_converts(admin_client, db):
    contract = _create(admin_client, {"brand": "LLDPE", "kg": "1000",
                                      "currency": "usd", "price": "1.00",
                                      "exchange_rate": "0.01"})
    line = contract.lines.get()
    assert line.price == Decimal("1.0000")
    assert line.price_uzs == Decimal("0.01")


@pytest.mark.xfail(reason="BUG: ContractLine.total_value quantizes with the default "
                          "ROUND_HALF_EVEN while every other money path in the app "
                          "(convert_pair, in_som) uses ROUND_HALF_UP, so a half-cent "
                          "line total rounds the other way",
                   strict=False)
def test_a_line_total_rounds_half_up_like_the_rest_of_the_money(db):
    line = ContractLine(contract=None, brand="LLDPE", kg=Decimal("2.5"),
                        price=Decimal("0.0100"))
    assert line.total_value == Decimal("0.03")       # 0.025 → HALF_UP
