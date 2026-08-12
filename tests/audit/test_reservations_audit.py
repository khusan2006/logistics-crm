"""Audit pass over Bron (reservations): create, edit, cancel, convert to sotuv.

Probe families, mapped onto the symptoms the product owner reported:
  (a) round-trip   — the typed side must survive bit-exact at the given kurs
  (b) idempotence  — re-saving the same bron must not move a single figure
  (c) stickiness   — currency=uzs must stay uzs, and the so'm column must hold the
                     figure that was actually typed
  (d) aggregates   — a bron's total must equal the sotuvlar it became, across
                     mixed currencies and mixed kursi

Nothing here edits crm/ — it only observes.
"""
import re
from decimal import Decimal

import pytest

from conftest import line_data

from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, Partner,
    Reservation, Sale, Shipment, ShipmentLine, ShipmentStatus, brand_on_hand_kg,
    brand_reserved_kg,
)


# --- arrangement helpers (same shapes as tests/test_reservations.py) ----------

def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _arrived_lot(kg="10000", brand="LLDPE", partner_name="Pars",
                 contract_price="1.00", arrived="2026-07-16"):
    partner = Partner.objects.create(name=partner_name, phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand,
                                kg=Decimal(kg), price=Decimal(contract_price))
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        eta="2026-07-15", arrived=arrived, transport="01A111AA", container="MSCU-1")
    return ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg))


def _reserve(client, brand, customer, kg="5000", price="", currency="usd",
             exchange_rate="12000", note=""):
    return client.post("/reservations/new/", {
        "customer": customer.pk, "brand": brand, "kg": kg, "currency": currency,
        "price": price, "exchange_rate": exchange_rate, "note": note,
    })


def _edit_payload(reservation, **overrides):
    """Exactly what the edit screen posts back for an untouched bron: the values the
    form renders, not the values a test wishes were there."""
    data = {
        "customer": reservation.customer_id,
        "brand": reservation.brand,
        "kg": str(reservation.kg),
        "currency": reservation.currency,
        "price": "" if reservation.price is None else str(reservation.price),
        "exchange_rate": str(reservation.exchange_rate),
        "note": reservation.note,
    }
    data.update(overrides)
    return data


def _edit(client, reservation, **overrides):
    return client.post(f"/reservations/{reservation.pk}/edit/",
                       _edit_payload(reservation, **overrides))


def _convert(client, reservation, price=None, kg=None):
    """Hand kg over from a bron, through the ordinary sotuv form with Brondan
    ushlansin ticked — the one-click Berish it used to post to is gone. The bron's
    currency, kurs and agreed narx ride along, as that button used to carry them."""
    from crm.models import brand_on_hand_kg

    reservation.refresh_from_db()
    give = kg if kg is not None else min(reservation.remaining_kg,
                                         brand_on_hand_kg(reservation.brand))
    if price is not None:
        narx = price
    elif reservation.is_som:
        narx = reservation.price_uzs
    else:
        narx = reservation.price
    return client.post("/sales/new/", {
        "customer": reservation.customer_id,
        "currency": reservation.currency,
        "exchange_rate": str(reservation.exchange_rate),
        "date": "2026-07-20", "debt_deadline": "", "note": "",
        "draw_from_bron_asked": "1", "draw_from_bron": "on",
        # The marka and its narx are a Mahsulot ROW now.
        **line_data({"brand": reservation.brand, "kg": str(give),
                     "price": "" if narx is None else str(narx)}),
    })


def _money(reservation):
    """The four facts that must never move on their own."""
    return (reservation.currency, reservation.exchange_rate,
            reservation.price, reservation.price_uzs)


def _rendered_value(html, name):
    """The value= a rendered form field carries, so a re-save test posts what the
    operator would actually be posting."""
    match = re.search(r'<input[^>]*name="%s"[^>]*>' % name, html)
    assert match, f"no {name} input in the rendered form"
    value = re.search(r'value="([^"]*)"', match.group(0))
    return value.group(1) if value else ""


# =============================================================================
# (a) ROUND-TRIP — the typed side is stored exact, the other side is derived once
# =============================================================================

def test_usd_bron_keeps_the_typed_dollar_narx_exact(admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="1.2345", currency="usd", exchange_rate="12650")
    bron = Reservation.objects.get()
    assert bron.currency == "usd"
    assert bron.price == Decimal("1.2345")                 # typed, untouched
    assert bron.price_uzs == Decimal("15616.43")           # 1.2345 × 12650, HALF_UP


def test_uzs_bron_keeps_the_typed_som_narx_exact(admin_client, db):
    """The operator agreed 18 000 so'm/kg. That figure — not a re-derivation of it —
    is what has to come back out of the database."""
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="18000", currency="uzs", exchange_rate="12650")
    bron = Reservation.objects.get()
    assert bron.currency == "uzs"
    assert bron.price_uzs == Decimal("18000.00")           # typed, untouched
    assert bron.price == Decimal("1.4229")                 # 18000/12650 at 4dp


def test_the_narx_quantum_is_four_decimals_not_two(admin_client, db):
    """A per-kg narx rounded to cents moves a 5-tonne bron by dollars."""
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="1", currency="uzs", exchange_rate="12000")
    bron = Reservation.objects.get()
    assert bron.price == Decimal("0.0001")                 # 1/12000 → HALF_UP at 4dp
    assert bron.price_uzs == Decimal("1.00")


def test_a_tiny_kurs_still_round_trips(admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="1000",
             price="100", currency="uzs", exchange_rate="0.01")
    bron = Reservation.objects.get()
    assert bron.price_uzs == Decimal("100.00")
    assert bron.price == Decimal("10000.0000")


def test_a_huge_kurs_still_round_trips(admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="1000",
             price="1.0000", currency="usd", exchange_rate="99999999.99")
    bron = Reservation.objects.get()
    assert bron.price == Decimal("1.0000")
    assert bron.price_uzs == Decimal("99999999.99")


# =============================================================================
# (c) CURRENCY STICKINESS
# =============================================================================

def test_a_som_bron_saves_as_som_and_the_som_column_holds_the_typed_figure(
        admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="20000", currency="uzs", exchange_rate="12500")
    bron = Reservation.objects.get()
    assert bron.currency == Currency.UZS
    assert bron.is_som is True
    # NOT the USD reading of 20000 (which would be 20000 dollars → 250 000 000 so'm)
    assert bron.price_uzs == Decimal("20000.00")
    assert bron.total_uzs == Decimal("100000000.00")       # 5000 × 20 000


def test_the_edit_form_comes_back_bound_to_som(admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="20000", currency="uzs", exchange_rate="12500")
    bron = Reservation.objects.get()
    html = admin_client.get(f"/reservations/{bron.pk}/edit/").content.decode()
    option = re.search(r'<option value="uzs"[^>]*>', html)
    assert option and "selected" in option.group(0), \
        "the Valyuta picker did not come back on So'm"


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_form_shows_the_som_narx_that_was_typed(admin_client, db):
    """What the operator typed was 20 000 so'm. Re-opening the bron must offer 20 000
    back, because the currency picker beside it says So'm and the server will read
    whatever is in that box as so'm on submit."""
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="20000", currency="uzs", exchange_rate="12500")
    bron = Reservation.objects.get()
    html = admin_client.get(f"/reservations/{bron.pk}/edit/").content.decode()
    assert Decimal(_rendered_value(html, "price")) == Decimal("20000.00")


def test_a_usd_bron_stays_usd_through_an_edit(admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="2.5000", currency="usd", exchange_rate="12000")
    bron = Reservation.objects.get()
    _edit(admin_client, bron, note="izoh")
    bron.refresh_from_db()
    assert bron.currency == Currency.USD
    assert bron.price == Decimal("2.5000")
    assert bron.price_uzs == Decimal("30000.00")


def test_switching_the_currency_on_an_edit_reinterprets_the_typed_narx(
        admin_client, db):
    """Deliberate change of mind: the operator picks So'm and types a so'm figure.
    The row must follow them rather than keep the old dollar reading."""
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="2.0000", currency="usd", exchange_rate="12000")
    bron = Reservation.objects.get()
    resp = _edit(admin_client, bron, currency="uzs", price="24000")
    assert resp.status_code == 302
    bron.refresh_from_db()
    assert bron.currency == Currency.UZS
    assert bron.price_uzs == Decimal("24000.00")
    assert bron.price == Decimal("2.0000")


# =============================================================================
# (b) IDEMPOTENCE / NO-DRIFT
# =============================================================================

def test_resaving_a_usd_bron_unchanged_moves_nothing_twice(admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="1.2345", currency="usd", exchange_rate="12650")
    bron = Reservation.objects.get()
    before = _money(bron)
    for _ in range(2):
        assert _edit(admin_client, bron).status_code == 302
        bron.refresh_from_db()
        assert _money(bron) == before


def test_editing_only_the_note_leaves_the_usd_money_alone(admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="1.2345", currency="usd", exchange_rate="12650")
    bron = Reservation.objects.get()
    before = _money(bron)
    _edit(admin_client, bron, note="mijoz keyin oladi")
    bron.refresh_from_db()
    assert bron.note == "mijoz keyin oladi"
    assert _money(bron) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_bron_unchanged_moves_nothing_twice(admin_client, db):
    """The reported symptom, reproduced end to end: open the bron, change nothing,
    press Save. Twice."""
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="20000", currency="uzs", exchange_rate="12500")
    bron = Reservation.objects.get()
    before = _money(bron)
    for round_no in (1, 2):
        html = admin_client.get(f"/reservations/{bron.pk}/edit/").content.decode()
        # post back exactly what the screen offered — the operator touched nothing
        admin_client.post(f"/reservations/{bron.pk}/edit/", {
            "customer": bron.customer_id, "brand": bron.brand,
            "kg": _rendered_value(html, "kg") or str(bron.kg),
            "currency": "uzs",
            "price": _rendered_value(html, "price"),
            "exchange_rate": _rendered_value(html, "exchange_rate"),
            "note": bron.note,
        })
        bron.refresh_from_db()
        assert _money(bron) == before, f"money drifted on no-op save #{round_no}"


def test_editing_the_kg_of_a_som_bron_does_not_touch_the_narx_columns(
        admin_client, db):
    """A kg change is not a narx change. Posting the so'm narx back (what the picker
    says the box means) must leave both money columns exactly where they were."""
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="20000", currency="uzs", exchange_rate="12500")
    bron = Reservation.objects.get()
    before = _money(bron)
    resp = admin_client.post(f"/reservations/{bron.pk}/edit/", {
        "customer": bron.customer_id, "brand": bron.brand, "kg": "6000",
        "currency": "uzs", "price": "20000", "exchange_rate": "12500", "note": "",
    })
    assert resp.status_code == 302
    bron.refresh_from_db()
    assert bron.kg == Decimal("6000.000")
    assert _money(bron) == before


def test_a_priceless_bron_resaves_without_growing_a_narx(admin_client, db):
    _arrived_lot()
    _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="")
    bron = Reservation.objects.get()
    assert bron.price is None and bron.price_uzs is None
    for _ in range(2):
        assert _edit(admin_client, bron).status_code == 302
        bron.refresh_from_db()
        assert bron.price is None and bron.price_uzs is None
        assert bron.total is None and bron.total_uzs is None


# =============================================================================
# Boundaries on create
# =============================================================================

def test_a_narx_with_no_kurs_is_refused(admin_client, db):
    _arrived_lot()
    resp = _reserve(admin_client, "LLDPE", _customer(), kg="5000",
                    price="2.00", exchange_rate="")
    assert resp.status_code == 200
    assert not Reservation.objects.exists()
    assert "Dollar kursini kiriting" in resp.content.decode()


def test_a_narx_at_kurs_zero_is_refused(admin_client, db):
    _arrived_lot()
    resp = _reserve(admin_client, "LLDPE", _customer(), kg="5000",
                    price="2.00", exchange_rate="0")
    assert resp.status_code == 200
    assert not Reservation.objects.exists()


def test_a_priceless_bron_keeps_the_default_kurs_when_the_field_is_absent(
        admin_client, db):
    """The kurs is genuinely optional on an unpriced bron, so an omitted field falls
    back to the model default rather than erroring."""
    _arrived_lot()
    resp = admin_client.post("/reservations/new/", {
        "customer": _customer().pk, "brand": "LLDPE", "kg": "5000",
        "currency": "usd", "price": "", "note": "",
    })
    assert resp.status_code == 302
    bron = Reservation.objects.get()
    assert bron.price is None
    assert bron.exchange_rate == Decimal("12000.00")


@pytest.mark.xfail(reason="BUG: clearing the Dollar kursi box on a bron with no "
                          "agreed narx crashes the save. MoneyEntryFormMixin marks "
                          "exchange_rate not-required (crm/forms.py:128-131) so that "
                          "this exact entry is possible, but the column is NOT NULL "
                          "with a default, so Django drops it from validation, "
                          "construct_instance writes None, and the INSERT raises an "
                          "unhandled IntegrityError (500) instead of a form error",
                   strict=False)
def test_a_priceless_bron_needs_no_kurs(admin_client, db):
    """crm/forms.py:128 says exactly this: a bron with no narx agreed yet has nothing
    to convert, so the kurs must not be demanded."""
    _arrived_lot()
    resp = _reserve(admin_client, "LLDPE", _customer(), kg="5000",
                    price="", exchange_rate="")
    assert resp.status_code == 302, resp.content.decode()[:600]
    bron = Reservation.objects.get()
    assert bron.price is None


@pytest.mark.xfail(reason="BUG: MoneyEntryFormMixin.clean() returns early when the "
                          "narx is blank (crm/forms.py:152-155), so the kurs is "
                          "never checked on an unpriced bron and exchange_rate=0 is "
                          "stored — the very state convert_pair() exists to refuse",
                   strict=False)
def test_a_priceless_bron_will_not_take_a_kurs_of_zero(admin_client, db):
    _arrived_lot()
    resp = _reserve(admin_client, "LLDPE", _customer(), kg="5000",
                    price="", exchange_rate="0")
    assert resp.status_code == 200 or Reservation.objects.get().exchange_rate > 0


@pytest.mark.xfail(reason="BUG: handing over a bron whose stored kurs is 0 raises "
                          "convert_pair's ValueError straight out of "
                          "reservation_convert (crm/views.py:1745) — an unhandled "
                          "500 on the hand-over button instead of a form message. "
                          "Reachable because the create form accepts kurs=0 on an "
                          "unpriced bron",
                   strict=False)
def test_a_kurs_zero_bron_reports_instead_of_crashing_on_handover(admin_client, db):
    _arrived_lot(kg="5000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="", exchange_rate="0")
    bron = Reservation.objects.get()
    resp = _convert(admin_client, bron, price="2.00")       # raises today
    assert resp.status_code in (200, 302)
    assert not Sale.objects.exists()


def test_the_agreed_narx_is_carried_onto_the_handover_form(admin_client, db):
    """Serving a bron goes through the ordinary sotuv form now, so the agreed narx
    has to travel with the link — retyping it from memory is how an agreed price
    quietly becomes a different one. The valyuta rides along for the same reason:
    the narx box is read as whichever currency the picker says."""
    _arrived_lot(kg="5000")
    customer = _customer()
    _reserve(admin_client, "LLDPE", customer, kg="5000",
             price="18000", currency="uzs", exchange_rate="12650")
    bron = Reservation.objects.get()
    assert bron.price_own == Decimal("18000.00")

    # the link the Bronlar row renders
    html = admin_client.get("/reservations/").content.decode()
    assert f"price={bron.price_own}" in html
    assert "currency=uzs" in html

    # and opening it puts the agreed figures in the boxes
    resp = admin_client.get(
        f"/sales/new/?customer={customer.pk}&brand=LLDPE"
        f"&currency=uzs&price={bron.price_own}")
    assert resp.context["form"].initial["currency"] == "uzs"
    # The marka and its agreed narx prefill the first Mahsulot row.
    row = resp.context["lines"].forms[0].initial
    assert row["brand"] == "LLDPE"
    assert row["price"] == Decimal("18000.00")

    _convert(admin_client, bron)
    sale = Sale.objects.get()
    assert sale.price_uzs == Decimal("18000.00")
    assert sale.price == Decimal("1.4229")


@pytest.mark.parametrize("price", ["0", "-1.5"])
def test_a_zero_or_negative_narx_is_refused(admin_client, db, price):
    _arrived_lot()
    resp = _reserve(admin_client, "LLDPE", _customer(), kg="5000", price=price)
    assert resp.status_code == 200
    assert not Reservation.objects.exists()


@pytest.mark.parametrize("kg", ["0", "-100"])
def test_a_zero_or_negative_kg_is_refused(admin_client, db, kg):
    _arrived_lot()
    resp = _reserve(admin_client, "LLDPE", _customer(), kg=kg, price="2.00")
    assert resp.status_code == 200
    assert not Reservation.objects.exists()


# =============================================================================
# Convert: the money shape must carry into the sotuv unchanged
# =============================================================================

def test_converting_a_usd_bron_carries_currency_kurs_and_narx(admin_client, db):
    _arrived_lot(kg="5000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="1.2345", currency="usd", exchange_rate="12650")
    bron = Reservation.objects.get()
    _convert(admin_client, bron)
    sale = Sale.objects.get()
    assert sale.currency == "usd"
    assert sale.exchange_rate == Decimal("12650.00")
    assert sale.price == Decimal("1.2345")
    assert sale.price_uzs == Decimal("15616.43")


def test_converting_a_som_bron_stays_a_som_sotuv(admin_client, db):
    """The docstring's own promise: a bron struck in so'm must not become a dollar
    sotuv re-rated at today's kurs."""
    _arrived_lot(kg="5000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="18000", currency="uzs", exchange_rate="12650")
    bron = Reservation.objects.get()
    _convert(admin_client, bron)
    sale = Sale.objects.get()
    assert sale.currency == Currency.UZS
    assert sale.is_som is True
    assert sale.price_uzs == Decimal("18000.00")           # the agreed figure, exact
    assert sale.price == Decimal("1.4229")
    assert sale.exchange_rate == Decimal("12650.00")


def test_a_priceless_bron_settles_in_the_currency_it_was_taken_in(admin_client, db):
    """No narx agreed at bron time, currency So'm. The figure typed at hand-over is
    a so'm figure — reading it as dollars would multiply the invoice by 12 650."""
    _arrived_lot(kg="5000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="", currency="uzs", exchange_rate="12650")
    bron = Reservation.objects.get()
    _convert(admin_client, bron, price="18000")
    sale = Sale.objects.get()
    assert sale.currency == Currency.UZS
    assert sale.price_uzs == Decimal("18000.00")
    assert sale.price == Decimal("1.4229")


def test_a_priceless_bron_will_not_convert_without_a_narx(admin_client, db):
    _arrived_lot(kg="5000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="")
    bron = Reservation.objects.get()
    _convert(admin_client, bron)
    bron.refresh_from_db()
    assert not Sale.objects.exists()
    assert bron.fulfilled_kg == Decimal("0")
    assert bron.status == "active"


@pytest.mark.xfail(reason="BUG: the narx typed at hand-over is written onto the "
                          "Sale only — the bron keeps price=None, so its own total "
                          "reads 'kelishilmagan' forever and a second (partial) "
                          "conversion silently asks for the narx again, letting the "
                          "same bron be filled at two different prices",
                   strict=False)
def test_the_narx_typed_at_handover_sticks_to_the_bron(admin_client, db):
    _arrived_lot(kg="3000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="", currency="usd", exchange_rate="12000")
    bron = Reservation.objects.get()
    _convert(admin_client, bron, price="2.0000")           # 3 000 kg handed over
    bron.refresh_from_db()
    assert bron.price == Decimal("2.0000"), "the agreed narx was not recorded"
    # and the rest of the bron cannot then be sold at a different narx by accident
    _arrived_lot(kg="2000", partner_name="Keyingi", arrived="2026-07-20")
    _convert(admin_client, bron, price="1.0000")
    assert {s.price for s in Sale.objects.all()} == {Decimal("2.0000")}


def test_double_conversion_does_not_sell_the_same_kg_twice(admin_client, db):
    _arrived_lot(kg="5000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="2.00")
    bron = Reservation.objects.get()
    _convert(admin_client, bron)
    _convert(admin_client, bron)
    bron.refresh_from_db()
    assert Sale.objects.count() == 1
    assert bron.fulfilled_kg == Decimal("5000.000")
    assert bron.status == "converted"


def test_a_cancelled_bron_is_never_drawn_down_again(admin_client, db):
    """A cancelled bron is out of the queue, so a later sotuv to that same mijoz is
    an ordinary one — it goes through, and the dead promise stays at zero rather
    than quietly recording kg against a booking that was called off."""
    _arrived_lot(kg="5000")
    customer = _customer()
    _reserve(admin_client, "LLDPE", customer, kg="5000", price="2.00")
    bron = Reservation.objects.get()
    admin_client.post(f"/reservations/{bron.pk}/cancel/", {})

    _convert(admin_client, bron)
    bron.refresh_from_db()
    assert bron.status == "cancelled"
    assert bron.fulfilled_kg == Decimal("0")
    sale = Sale.objects.get()
    assert sale.reservation_id is None


def test_converting_after_the_stock_went_elsewhere_is_refused(admin_client, db):
    """The bron was taken while the granula was on the shelf; by hand-over time the
    shelf is empty. Nothing must be conjured."""
    lot = _arrived_lot(kg="5000")
    _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="5000", price="2.00")
    bron = Reservation.objects.get()
    Sale.objects.create(customer=_customer("Boshqa"), line=lot, kg=Decimal("5000"),
                        price=Decimal("2.0000"), price_uzs=Decimal("24000.00"),
                        currency="usd", exchange_rate=Decimal("12000"),
                        date="2026-07-18")
    _convert(admin_client, bron)
    bron.refresh_from_db()
    assert Sale.objects.filter(reservation=bron).count() == 0
    assert bron.fulfilled_kg == Decimal("0")
    assert bron.status == "active"


def test_a_partial_conversion_only_takes_what_landed(admin_client, db):
    _arrived_lot(kg="3000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="2.00")
    bron = Reservation.objects.get()
    _convert(admin_client, bron)
    bron.refresh_from_db()
    assert bron.fulfilled_kg == Decimal("3000.000")
    assert bron.remaining_kg == Decimal("2000.000")
    assert bron.status == "active"
    assert Sale.objects.get().kg == Decimal("3000.000")


# =============================================================================
# (d) AGGREGATE CONSISTENCY
# =============================================================================

def test_the_sotuv_slices_add_back_up_to_the_bron_total_in_both_currencies(
        admin_client, db):
    """One bron, two lots → two Sale rows. The pieces must add up to the whole in
    dollars AND in so'm."""
    _arrived_lot(kg="3000", partner_name="Pars", arrived="2026-07-16")
    _arrived_lot(kg="2000", partner_name="Boshqa", arrived="2026-07-18")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="1.2345", currency="usd", exchange_rate="12650")
    bron = Reservation.objects.get()
    _convert(admin_client, bron)
    bron.refresh_from_db()
    sales = list(Sale.objects.all())
    assert len(sales) == 2
    assert sum(s.kg for s in sales) == bron.kg
    assert sum(s.total for s in sales) == bron.total
    assert sum(s.total_uzs for s in sales) == bron.total_uzs


def test_a_som_bron_totals_the_same_across_slices(admin_client, db):
    _arrived_lot(kg="3000", partner_name="Pars", arrived="2026-07-16")
    _arrived_lot(kg="2000", partner_name="Boshqa", arrived="2026-07-18")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="18000", currency="uzs", exchange_rate="12650")
    bron = Reservation.objects.get()
    _convert(admin_client, bron)
    bron.refresh_from_db()
    sales = list(Sale.objects.all())
    assert sum(s.total_uzs for s in sales) == bron.total_uzs == Decimal("90000000.00")


def test_a_mijoz_balance_sums_brons_of_mixed_currency_and_mixed_kurs(
        admin_client, db):
    """Two brons for one mijoz — one in dollars at 12 000, one in so'm at 13 000 —
    become sotuvlar. The qarz must be the plain sum of both, on each side."""
    _arrived_lot(kg="4000", brand="LLDPE")
    _arrived_lot(kg="4000", brand="HDPE", partner_name="Ikkinchi")
    customer = _customer()
    _reserve(admin_client, "LLDPE", customer, kg="4000",
             price="2.0000", currency="usd", exchange_rate="12000")
    _reserve(admin_client, "HDPE", customer, kg="4000",
             price="26000", currency="uzs", exchange_rate="13000")
    for bron in Reservation.objects.order_by("created_at", "pk"):
        _convert(admin_client, bron)
    sales = list(Sale.objects.all())
    assert len(sales) == 2
    customer.refresh_from_db()
    assert customer.balance == sum(s.total for s in sales)
    assert customer.balance_uzs == sum(s.total_uzs for s in sales)
    # and each side is the figure that was actually agreed, not a re-rating
    assert customer.balance == Decimal("8000.00") + Decimal("8000.00")
    assert customer.balance_uzs == Decimal("96000000.00") + Decimal("104000000.00")


def test_the_list_page_reserved_kg_matches_what_the_ombor_reports(admin_client, db):
    _arrived_lot(kg="10000")
    _reserve(admin_client, "LLDPE", _customer("Bir"), kg="3000", price="2.00")
    _reserve(admin_client, "LLDPE", _customer("Ikki"), kg="2000", price="2.00")
    head = Reservation.objects.order_by("created_at", "pk").first()
    _convert(admin_client, head)                            # 3 000 kg handed over
    assert brand_on_hand_kg("LLDPE") == Decimal("7000.000")  # 10000 − 3000 sold
    assert brand_reserved_kg("LLDPE") == Decimal("2000.000")  # still promised
    rows = {r.customer.name: r
            for r in admin_client.get("/reservations/").context["page"]}
    assert rows["Ikki"].queue_pos == 1
    assert rows["Ikki"].servable_kg == Decimal("2000.000")


# =============================================================================
# Deleting / cancelling a row other rows depend on
# =============================================================================

def test_a_converted_bron_is_not_deletable(admin_client, db):
    _arrived_lot(kg="5000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="2.00")
    bron = Reservation.objects.get()
    _convert(admin_client, bron)
    admin_client.post(f"/reservations/{bron.pk}/delete/", {})
    assert Reservation.objects.filter(pk=bron.pk).exists()
    assert Sale.objects.get().reservation_id == bron.pk


@pytest.mark.xfail(reason="BUG: reservation_cancel has no status guard, so a "
                          "CONVERTED bron can be flipped to 'cancelled' — which "
                          "then walks straight past reservation_delete's "
                          "converted-only refusal and hard-deletes it, cutting the "
                          "sotuv (SET_NULL) and any earmarked to'lov loose from the "
                          "bron they came from",
                   strict=False)
def test_a_converted_bron_cannot_be_cancelled_into_deletability(admin_client, db):
    _arrived_lot(kg="5000")
    customer = _customer()
    _reserve(admin_client, "LLDPE", customer, kg="5000", price="2.00")
    bron = Reservation.objects.get()
    CustomerPayment.objects.create(customer=customer, date="2026-07-17",
                                   amount=Decimal("1000.00"), reservation=bron)
    _convert(admin_client, bron)
    bron.refresh_from_db()
    assert bron.status == "converted"

    admin_client.post(f"/reservations/{bron.pk}/cancel/", {})
    bron.refresh_from_db()
    assert bron.status == "converted", "a converted bron was cancelled"

    admin_client.post(f"/reservations/{bron.pk}/delete/", {})
    assert Reservation.objects.filter(pk=bron.pk).exists()
    assert Sale.objects.get().reservation_id is not None
    assert CustomerPayment.objects.get().reservation_id is not None


def test_cancelling_an_active_bron_drops_the_promise_and_moves_no_money(
        admin_client, db):
    _arrived_lot(kg="10000")
    _reserve(admin_client, "LLDPE", _customer(), kg="5000",
             price="18000", currency="uzs", exchange_rate="12650")
    bron = Reservation.objects.get()
    before = _money(bron)
    assert brand_reserved_kg("LLDPE") == Decimal("5000.000")
    admin_client.post(f"/reservations/{bron.pk}/cancel/", {})
    bron.refresh_from_db()
    assert bron.status == "cancelled"
    assert brand_reserved_kg("LLDPE") == Decimal("0")
    assert brand_on_hand_kg("LLDPE") == Decimal("10000.000")
    assert _money(bron) == before
