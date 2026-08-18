"""Audit pass over Ombor (warehouse stock) and Qaytarish (returns).

Diagnosis, not repair: every test either PASSES because the behaviour matches what
the docstrings promise, or is marked xfail with the defect it documents.

Probe families, mapped onto the product owner's symptoms:
  (a) round-trip   — the side the operator typed must survive bit-exact
  (b) idempotence  — re-saving an untouched row must not move any figure
  (c) stickiness   — a row entered in so'm must stay so'm through save + reopen
  (d) aggregates   — a page total must equal the sum of its parts, mixed kursi too
"""
from decimal import Decimal

import pytest

from conftest import return_rows

from crm.models import (
    Contract, ContractLine, Currency, Customer, Partner, Reservation, Return, Sale,
    Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus,
    brand_on_hand_kg, convert_pair, stock_value,
)

pytestmark = pytest.mark.django_db

USD = Currency.USD
UZS = Currency.UZS


# --- builders ---------------------------------------------------------------
# Fixtures are built the way the real forms build them: the operator types one
# side, convert_pair fills the other, and BOTH are stored.

def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(brand="LLDPE", kg="1000", partner="Pars",
         contract_typed="1.00", contract_currency=USD, contract_rate="12000",
         lot_typed=None, lot_currency=None, lot_rate="12000",
         expense=None, arrived="2026-07-16", contract_kg="100000"):
    """One arrived lot (a ShipmentLine) — the unit the ombor deals in.

    `lot_typed=None` leaves the truck line unpriced so it inherits the kelishuv
    narx, which is the common shape in the real data.

    A lot is priced in its kelishuv's currency, so `lot_currency` follows
    `contract_currency` — asking for the other one is not a shape the app can hold.
    """
    lot_currency = contract_currency
    p = Partner.objects.create(name=partner, phone="1", city="T")
    c = Contract.objects.create(partner=p, created="2026-07-01",
                                currency=contract_currency)
    c_usd, c_uzs = convert_pair(Decimal(contract_typed), contract_currency,
                                Decimal(contract_rate), "0.0001")
    line = ContractLine.objects.create(
        contract=c, brand=brand, kg=Decimal(contract_kg), price=c_usd,
        price_uzs=c_uzs, currency=contract_currency,
        exchange_rate=Decimal(contract_rate))
    ship = Shipment.objects.create(contract=c, status=ShipmentStatus.arrival(),
                                   sent="2026-07-05", eta="2026-07-15",
                                   arrived=arrived, transport="01A111AA")
    price = price_uzs = None
    if lot_typed is not None:
        price, price_uzs = convert_pair(Decimal(lot_typed), lot_currency,
                                        Decimal(lot_rate), "0.0001")
    lot = ShipmentLine.objects.create(
        shipment=ship, contract_line=line, kg=Decimal(kg), price=price,
        price_uzs=price_uzs, currency=lot_currency,
        exchange_rate=Decimal(lot_rate))
    if expense:
        ShipmentExpense.objects.create(shipment=ship, amount=Decimal(expense),
                                       amount_uzs=Decimal(expense) * Decimal("12000"),
                                       date=arrived)
    return lot


def _sell(client, lot, customer, kg="400", price="2.00", currency=USD,
          rate="12000", date="2026-07-18"):
    """A sotuv from ONE lot, through the real view the Ombor row links to."""
    resp = client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "kg": str(kg), "currency": currency,
        "price": str(price), "exchange_rate": str(rate), "date": date,
        "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302, resp.content.decode()[:800]
    return Sale.objects.filter(line=lot).order_by("-pk").first()


def _scrap(sale, kg="100"):
    """A vazvrat whose goods did NOT come back on the shelf.

    Written straight to the model on purpose: a vazvrat entered through the app
    always restocks, because that is the rule the business works to. The row still
    exists in the schema, so what it does to the ombor and the foyda is still worth
    pinning down — it just cannot be reached through a form any more."""
    return Return.objects.create(
        sale=sale, kg=Decimal(kg), price=sale.price, price_uzs=sale.price_uzs,
        currency=sale.currency, exchange_rate=sale.exchange_rate,
        date="2026-07-19", restock=False)


def _give_back(client, sale, kg="100", date="2026-07-19", settle="advance",
               due_date=""):
    """A vazvrat through the real view.

    No narx and no valyuta: both are read off the sotuv the kg are booked against,
    which is what stops a vazvrat being worth more per kg than the sotuv it
    reverses. Goods always go back on the shelf — that is the rule the business
    works to, so there is no restock flag to pass either."""
    return client.post("/returns/new/", return_rows(
        (sale, kg), customer=sale.customer, date=date, settle=settle,
        due_date=due_date))


def _reserve(client, brand, customer, kg="500", price="", rate="12000",
             currency=USD):
    resp = client.post("/reservations/new/", {
        "customer": customer.pk, "brand": brand, "kg": str(kg),
        "currency": currency, "price": price, "exchange_rate": rate, "note": "",
    })
    assert resp.status_code == 302, resp.content.decode()[:800]
    return Reservation.objects.order_by("-pk").first()


def _group(client, brand, **params):
    """The Ombor row for one marka, straight out of the view's context."""
    page = client.get("/ombor/", params).context["page"]
    return next(g for g in page.object_list if g["brand"] == brand)


def _rendered_post(form):
    """The POST body a browser would send back from an untouched rendered form.

    Every field carries the value Django painted into the widget — which is the
    whole point of the no-drift probe: pressing Save without touching anything
    must not move a figure.
    """
    data = {}
    for name in form.fields:
        bound = form[name]
        value = bound.value()
        if value is None:
            data[bound.html_name] = ""
        elif isinstance(value, bool):
            data[bound.html_name] = "on" if value else ""
        else:
            data[bound.html_name] = str(value)
    return data


def _rendered_formset_post(formset):
    data = _rendered_post(formset.management_form)
    for form in formset.forms:
        data.update(_rendered_post(form))
    return data


# =============================================================================
# 1. Stock arithmetic: remaining = shipped − sold + returned, never negative
# =============================================================================

def test_lot_remaining_is_arrived_minus_sold(admin_client):
    lot = _lot(kg="1000")
    _sell(admin_client, lot, _customer(), kg="400")

    lot.refresh_from_db()
    assert lot.sold_kg == Decimal("400.000")
    assert lot.returned_kg == Decimal("0")
    assert lot.available_kg == Decimal("600.000")

    g = _group(admin_client, "LLDPE")
    assert g["kirim"] == Decimal("1000.000")
    assert g["sold"] == Decimal("400.000")
    assert g["on_hand"] == Decimal("600.000")


def test_restocked_return_puts_the_kg_back_on_the_lot(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400")
    assert _give_back(admin_client, sale, kg="100").status_code == 302

    lot.refresh_from_db()
    assert lot.returned_kg == Decimal("100.000")
    assert lot.available_kg == Decimal("700.000")
    # and the marka-level figure the sotuv forms police follows it
    assert brand_on_hand_kg("LLDPE") == Decimal("700.000")
    assert _group(admin_client, "LLDPE")["on_hand"] == Decimal("700.000")


def test_non_restocked_return_leaves_the_shelf_alone(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400")
    _scrap(sale, kg="100")

    lot.refresh_from_db()
    assert lot.returned_kg == Decimal("0")
    assert lot.available_kg == Decimal("600.000")
    # the mijoz is still credited — the goods just did not come back
    sale.refresh_from_db()
    assert sale.returned_amount == Decimal("200.00")     # 100 kg × $2.00
    assert sale.net_total == Decimal("600.00")           # 800 − 200


def test_stock_never_goes_negative_and_the_shortfall_is_named(admin_client):
    """Bronning more than has landed is legitimate; a negative shelf is not. The
    bron takes nothing off the shelf, so the gap is reported as `short` instead."""
    _lot(kg="1000")
    _reserve(admin_client, "LLDPE", _customer(), kg="2500")

    g = _group(admin_client, "LLDPE")
    assert g["on_hand"] == Decimal("1000.000")         # every kg still sellable
    assert g["reserved"] == Decimal("2500.000")
    assert g["short"] == Decimal("1500.000")           # clamped, not −1500
    assert brand_on_hand_kg("LLDPE") == Decimal("1000.000")


def test_deleting_a_sotuv_releases_its_stock(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400")
    assert _group(admin_client, "LLDPE")["on_hand"] == Decimal("600.000")

    assert admin_client.post(f"/sales/{sale.pk}/delete/").status_code == 302
    lot.refresh_from_db()
    assert lot.available_kg == Decimal("1000.000")
    g = _group(admin_client, "LLDPE")
    assert g["sold"] == Decimal("0") and g["on_hand"] == Decimal("1000.000")


def test_deleting_a_sotuv_that_has_a_return_restores_the_whole_lot(admin_client):
    """The return is a row that hangs off the sotuv — deleting the parent must not
    leave its kg counted twice (once released, once still 'returned')."""
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400")
    _give_back(admin_client, sale, kg="100")
    assert Return.objects.count() == 1

    assert admin_client.post(f"/sales/{sale.pk}/delete/").status_code == 302
    assert not Return.objects.exists()                   # cascaded with the sotuv
    lot.refresh_from_db()
    assert lot.available_kg == Decimal("1000.000")       # not 1100


def test_deleting_a_bron_drops_the_promise(admin_client):
    lot = _lot(kg="1000")
    bron = _reserve(admin_client, "LLDPE", _customer(), kg="400")
    g = _group(admin_client, "LLDPE")
    assert g["reserved"] == Decimal("400.000")
    assert g["on_hand"] == Decimal("1000.000")           # promised, not held

    assert admin_client.post(f"/reservations/{bron.pk}/delete/", {}).status_code == 302
    g = _group(admin_client, "LLDPE")
    assert g["reserved"] == Decimal("0")
    assert g["on_hand"] == Decimal("1000.000")
    lot.refresh_from_db()
    assert lot.available_kg == Decimal("1000.000")       # a bron never touched it


@pytest.mark.xfail(reason="BUG: Ombor prints Kirim / Sotilgan / Sotish mumkin but no "
                          "Qaytgan column, and Sotilgan is gross of restocked "
                          "returns — after a return the three stop reconciling "
                          "(1000 − 400 = 600, but the page says 700 sellable)",
                   strict=False)
def test_ombor_columns_reconcile_after_a_restocked_return(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400")
    _give_back(admin_client, sale, kg="100")

    g = _group(admin_client, "LLDPE")
    # Nothing is bronned, so the columns on screen must add up on their own.
    assert g["kirim"] - g["sold"] == g["on_hand"]


# =============================================================================
# 2. (a) ROUND-TRIP — the typed side of a qaytarish must be bit-exact
# =============================================================================

def test_return_typed_in_som_keeps_the_som_side_exact(admin_client):
    """20 000 so'm/kg at 12 345 is 1.620097… $/kg. The so'm side must come back
    exactly as typed; only the dollar side is derived (4dp, HALF_UP)."""
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="20000",
                 currency=UZS, rate="12345")
    assert _give_back(admin_client, sale, kg="100").status_code == 302

    ret = Return.objects.get()
    assert ret.currency == UZS
    assert ret.price_uzs == Decimal("20000.00")          # the sotuv's own, untouched
    assert ret.price == Decimal("1.6201")                # derived
    assert ret.amount_uzs == Decimal("2000000.00")       # 100 kg × 20 000
    assert ret.amount == Decimal("162.01")


def test_return_takes_the_dollar_narx_off_its_sotuv(admin_client):
    """The narx is never typed. It is copied off the sotuv the kg came from, which is
    what makes it impossible for a vazvrat to be worth more per kg than the sotuv it
    reverses — the hole a free-typed narx box left open."""
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="1.6667",
                 rate="12345")
    assert _give_back(admin_client, sale, kg="100").status_code == 302

    ret = Return.objects.get()
    assert ret.currency == USD
    assert ret.price == Decimal("1.6667")                # the sotuv's, exactly
    assert ret.exchange_rate == Decimal("12345.00")      # and its kurs, exactly
    assert ret.price_uzs == Decimal("20575.41")          # 1.6667 × 12345
    assert ret.amount == Decimal("166.67")


def test_som_price_rounds_at_the_4dp_quantum_not_the_2dp_one(admin_client):
    """A per-kg narx keeps four decimals — rounding it to cents would move a
    24-tonne lot by dollars. 1 so'm at 12 000 is 0.0000833…$ → 0.0001."""
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="1", currency=UZS,
                 rate="12000")
    assert _give_back(admin_client, sale, kg="100").status_code == 302

    ret = Return.objects.get()
    assert ret.price == Decimal("0.0001")
    assert ret.price_uzs == Decimal("1.00")


# =============================================================================
# 3. (c) CURRENCY STICKINESS
# =============================================================================

def test_som_qaytarish_saves_as_som_and_the_form_reopens_in_som(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="20000",
                 currency=UZS, rate="12500")
    _give_back(admin_client, sale, kg="100")

    ret = Return.objects.get()
    assert ret.currency == UZS and ret.is_som is True
    assert ret.price_uzs == Decimal("20000.00")
    assert ret.exchange_rate == Decimal("12500.00")

    # The modal asks no currency and no narx at all: it prints the sotuv's own, in
    # the sotuv's own money, and takes only kg. There is nowhere for the two to
    # drift apart any more.
    html = admin_client.get(f"/returns/new/?sale={sale.pk}").content.decode()
    assert "20 000" in html.replace("\u00a0", " ")
    assert 'name="price"' not in html
    assert 'name="currency"' not in html


def test_som_sale_returned_in_full_zeroes_both_sides(admin_client):
    """Crediting a so'm sotuv back at its own narx must leave nothing owed in
    EITHER currency — a leftover so'm balance is the classic conversion bug."""
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="20000",
                 currency=UZS, rate="12345")
    assert sale.total_uzs == Decimal("8000000.00")

    assert _give_back(admin_client, sale, kg="400").status_code == 302
    sale.refresh_from_db()
    assert sale.net_total_uzs == Decimal("0.00")
    assert sale.net_total == Decimal("0.00")


# =============================================================================
# 4. (b) IDEMPOTENCE — nothing may move on an untouched re-save
# =============================================================================

def _ombor_snapshot(client, brand):
    g = _group(client, brand)
    return {k: g[k] for k in ("kirim", "sold", "on_hand", "reserved", "short",
                              "cost_min", "cost_max", "cost_min_uzs", "cost_max_uzs")}


def test_resaving_an_untouched_yuk_twice_moves_no_ombor_figure(admin_client):
    """Open the yuk, press Save, twice. Stock and tan narx must not budge."""
    lot = _lot(kg="1000", lot_typed="1.20", expense="2000")
    _sell(admin_client, lot, _customer(), kg="400")
    before = _ombor_snapshot(admin_client, "LLDPE")
    money_before = (lot.price, lot.price_uzs, lot.kg, lot.currency)

    for _ in range(2):
        page = admin_client.get(f"/shipments/{lot.shipment_id}/edit/")
        body = _rendered_post(page.context["form"])
        body.update(_rendered_formset_post(page.context["lines"]))
        resp = admin_client.post(f"/shipments/{lot.shipment_id}/edit/", body)
        assert resp.status_code == 302, resp.content.decode()[:1500]

        lot.refresh_from_db()
        assert (lot.price, lot.price_uzs, lot.kg, lot.currency) == money_before
        assert _ombor_snapshot(admin_client, "LLDPE") == before


def test_resaving_an_untouched_som_priced_yuk_keeps_its_som_narx(admin_client):
    """A so'm lot survives an untouched Save — and now for the right reason.

    This used to pass by luck: the narx box was painted with the DERIVED dollar
    figure (1.6201) under a So'm picker, and nothing moved only because the row
    was byte-identical to its initial and Django's has_changed() kept _save_lines
    from writing it at all. Touching any other field removed that shield.

    Since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) the box holds the
    so'm narx the operator actually agreed, so the round trip is correct rather
    than merely skipped.
    """
    lot = _lot(kg="1000", lot_typed="20000", contract_currency=UZS, lot_rate="12345")
    assert lot.price_uzs == Decimal("20000.00")

    for _ in range(2):
        page = admin_client.get(f"/shipments/{lot.shipment_id}/edit/")
        # the box now shows the so'm side — there is no picker beside it any more,
        # the row reads so'm because the kelishuv it hangs off was struck in so'm
        row = page.context["lines"].forms[0]
        assert row["price"].value() == Decimal("20000.00")
        assert "currency" not in row.fields
        body = _rendered_post(page.context["form"])
        body.update(_rendered_formset_post(page.context["lines"]))
        resp = admin_client.post(f"/shipments/{lot.shipment_id}/edit/", body)
        assert resp.status_code == 302, resp.content.decode()[:1500]

        lot.refresh_from_db()
        assert lot.currency == UZS
        assert lot.price_uzs == Decimal("20000.00")
        assert lot.price == Decimal("1.6201")


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_correcting_the_kg_of_a_som_priced_lot_must_not_move_its_narx(admin_client):
    lot = _lot(kg="1000", lot_typed="20000", contract_currency=UZS, lot_rate="12345")

    page = admin_client.get(f"/shipments/{lot.shipment_id}/edit/")
    body = _rendered_post(page.context["form"])
    body.update(_rendered_formset_post(page.context["lines"]))
    body["lines-0-kg"] = "900"                      # the only thing being corrected
    resp = admin_client.post(f"/shipments/{lot.shipment_id}/edit/", body)
    assert resp.status_code == 302, resp.content.decode()[:1500]

    lot.refresh_from_db()
    assert lot.kg == Decimal("900.000")
    assert lot.price_uzs == Decimal("20000.00"), "the agreed so'm narx moved by itself"
    assert lot.price == Decimal("1.6201")


def test_editing_an_unrelated_field_on_the_yuk_moves_no_money(admin_client):
    """One field changed that has nothing to do with money (the plate) — every
    figure on the ombor row must be exactly where it was."""
    lot = _lot(kg="1000", lot_typed="1.20", expense="2000")
    _sell(admin_client, lot, _customer(), kg="400")
    before = _ombor_snapshot(admin_client, "LLDPE")

    page = admin_client.get(f"/shipments/{lot.shipment_id}/edit/")
    body = _rendered_post(page.context["form"])
    body.update(_rendered_formset_post(page.context["lines"]))
    body["transport"] = "90A999ZZ"
    resp = admin_client.post(f"/shipments/{lot.shipment_id}/edit/", body)
    assert resp.status_code == 302, resp.content.decode()[:1500]

    lot.refresh_from_db()
    assert lot.shipment.transport == "90A999ZZ"
    assert _ombor_snapshot(admin_client, "LLDPE") == before


def test_a_return_does_not_move_when_its_sotuv_is_resaved_untouched(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400")
    _give_back(admin_client, sale, kg="100")
    ret = Return.objects.get()
    frozen = (ret.kg, ret.price, ret.price_uzs, ret.currency, ret.amount)

    for _ in range(2):
        page = admin_client.get(f"/sales/{sale.pk}/edit/")
        body = _rendered_post(page.context["form"])
        resp = admin_client.post(f"/sales/{sale.pk}/edit/", body)
        assert resp.status_code == 302, resp.content.decode()[:1500]

        ret.refresh_from_db()
        assert (ret.kg, ret.price, ret.price_uzs, ret.currency, ret.amount) == frozen
        sale.refresh_from_db()
        assert sale.net_total == Decimal("600.00")
        lot.refresh_from_db()
        assert lot.available_kg == Decimal("700.000")


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_a_som_sotuv_with_a_qaytarish_survives_an_untouched_resave(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="20000",
                 currency=UZS, rate="12345")
    _give_back(admin_client, sale, kg="100")
    assert sale.total_uzs == Decimal("8000000.00")

    page = admin_client.get(f"/sales/{sale.pk}/edit/")
    body = _rendered_post(page.context["form"])
    resp = admin_client.post(f"/sales/{sale.pk}/edit/", body)
    assert resp.status_code == 302, resp.content.decode()[:1500]

    sale.refresh_from_db()
    assert sale.currency == UZS
    assert sale.price_uzs == Decimal("20000.00")
    assert sale.total_uzs == Decimal("8000000.00")
    assert sale.net_total_uzs == Decimal("6000000.00")   # 8 000 000 − 2 000 000
    assert sale.net_total_uzs >= 0


# =============================================================================
# 5. Boundaries: zero, negative, blank, rate = 0, huge and tiny kurs
# =============================================================================

@pytest.mark.parametrize("kg", ["0", "-100"])
def test_return_refuses_zero_and_negative_kg(admin_client, kg):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400")
    assert _give_back(admin_client, sale, kg=kg).status_code == 200
    assert not Return.objects.exists()


def test_a_posted_narx_or_kurs_is_ignored(admin_client):
    """The old form asked for a narx, a valyuta and a kurs, and had to refuse a blank
    kurs, a zero narx and a negative one. None of those questions exist now: all three
    are read off the sotuv, so a tampered POST carrying its own figures changes
    nothing. The refusals were guarding a door that has been walled up."""
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="2.00",
                 rate="12000")
    data = return_rows((sale, "100"), customer=sale.customer, date="2026-07-19")
    data.update({"price": "999", "currency": UZS, "exchange_rate": "0"})
    assert admin_client.post("/returns/new/", data).status_code == 302

    ret = Return.objects.get()
    assert ret.price == Decimal("2.0000")               # the sotuv's, not the POST's
    assert ret.currency == USD
    assert ret.exchange_rate == Decimal("12000.00")


def test_a_tiny_kurs_still_round_trips(admin_client):
    """A kurs of 0.01 is nonsense in the market but legal in the column. The vazvrat
    inherits it from the sotuv, and the sotuv's own side must survive the trip."""
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="1", currency=UZS,
                 rate="0.01")
    assert _give_back(admin_client, sale, kg="100").status_code == 302
    ret = Return.objects.get()
    assert ret.price_uzs == Decimal("1.00")
    assert ret.price == Decimal("100.0000")              # 1 / 0.01


@pytest.mark.xfail(reason="BUG: at an absurd kurs the DERIVED dollar side rounds to "
                          "0.0000 and is stored anyway, so the qaytarish credits "
                          "2 000 000 so'm and $0 — the sotuv's dollar balance never "
                          "moves. The form only checks that the TYPED side is "
                          "positive, never that the derived twin survived",
                   strict=False)
def test_a_huge_kurs_does_not_silently_store_a_zero_twin(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="20000",
                 currency=UZS, rate="12345")
    resp = _give_back(admin_client, sale, kg="100", price="20000", currency=UZS,
                      rate="999999999.99")
    ret = Return.objects.filter(sale=sale).first()
    if ret is not None:
        assert ret.price > 0, "a money row was stored with a zero dollar side"
    else:
        assert resp.status_code == 200


def test_return_cannot_exceed_what_is_left_of_the_sotuv(admin_client):
    lot = _lot(kg="1000")
    sale = _sell(admin_client, lot, _customer(), kg="400")
    assert _give_back(admin_client, sale, kg="300").status_code == 302
    resp = _give_back(admin_client, sale, kg="200")     # only 100 kg left
    assert resp.status_code == 200
    assert Return.objects.count() == 1
    lot.refresh_from_db()
    assert lot.available_kg == Decimal("900.000")       # 1000 − 400 + 300


def test_the_last_product_row_cannot_be_removed_at_all(admin_client):
    """A yuk must keep at least one product, so the single-lot case is refused by
    the formset before anything is deleted."""
    lot = _lot(kg="1000")
    _sell(admin_client, lot, _customer(), kg="400")

    page = admin_client.get(f"/shipments/{lot.shipment_id}/edit/")
    body = _rendered_post(page.context["form"])
    body.update(_rendered_formset_post(page.context["lines"]))
    body["lines-0-DELETE"] = "on"
    resp = admin_client.post(f"/shipments/{lot.shipment_id}/edit/", body)
    assert resp.status_code == 200
    assert ShipmentLine.objects.filter(pk=lot.pk).exists()


@pytest.mark.xfail(reason="BUG: removing ONE product row from a yuk that still has "
                          "another row raises ProtectedError out of _save_lines "
                          "(crm/views.py:426, Sale.line is PROTECT) instead of a form "
                          "error — a 500 on an ordinary yuk edit. shipment_delete "
                          "catches exactly this; the edit path does not",
                   strict=False)
def test_removing_a_lot_that_has_sotuvlar_fails_gracefully(admin_client):
    lot = _lot(kg="1000")
    # a second product on the same truck, so the "at least one row" rule cannot
    # mask the delete
    other_line = ContractLine.objects.create(
        contract=lot.contract_line.contract, brand="HDPE", kg=Decimal("100000"),
        price=Decimal("1.00"), price_uzs=Decimal("12000"))
    ShipmentLine.objects.create(shipment=lot.shipment, contract_line=other_line,
                                kg=Decimal("500"))
    _sell(admin_client, lot, _customer(), kg="400")

    page = admin_client.get(f"/shipments/{lot.shipment_id}/edit/")
    body = _rendered_post(page.context["form"])
    body.update(_rendered_formset_post(page.context["lines"]))
    body["lines-0-DELETE"] = "on"
    resp = admin_client.post(f"/shipments/{lot.shipment_id}/edit/", body)
    assert resp.status_code in (200, 302)
    assert ShipmentLine.objects.filter(pk=lot.pk).exists()


# =============================================================================
# 6. (d) AGGREGATE CONSISTENCY — mixed currencies, mixed kursi
# =============================================================================

def test_ombor_group_totals_equal_the_sum_of_their_lots(admin_client):
    """Two arrivals of one marka at different landed costs are ONE row; the row's
    figures must be the sum of the lots inside it."""
    cheap = _lot(brand="2102", kg="48000", lot_typed="1.20", arrived="2026-07-19")
    dear = _lot(brand="2102", kg="72000", lot_typed="1.30", partner="Boshqa",
                arrived="2026-07-23")
    _sell(admin_client, cheap, _customer(), kg="8000")

    g = _group(admin_client, "2102")
    assert [lot.pk for lot in g["lots"]] == [cheap.pk, dear.pk]      # FIFO order
    assert g["kirim"] == sum(lot.kg for lot in g["lots"])
    assert g["sold"] == sum(lot.sold_kg for lot in g["lots"])
    assert g["on_hand"] == sum(lot.available_kg for lot in g["lots"])
    assert g["cost_min"] == Decimal("1.2000") and g["cost_max"] == Decimal("1.3000")


def test_stock_value_equals_the_ombor_shelf_at_landed_cost(admin_client):
    """The dashboard's ombor qiymati and the Ombor page must be the same goods."""
    a = _lot(brand="2102", kg="1000", lot_typed="1.20", expense="200")   # 1.40/kg
    b = _lot(brand="7000", kg="2000", lot_typed="1.30", partner="Ikki")  # 1.30/kg
    _sell(admin_client, a, _customer(), kg="400")

    total, total_uzs, kg = stock_value()
    assert kg == Decimal("2600.000")                    # 600 + 2000
    assert total == Decimal("600") * a.landed_cost_per_kg + Decimal("2000") * b.landed_cost_per_kg
    page_kg = sum(g["on_hand"] for g in admin_client.get("/ombor/").context["page"])
    assert page_kg == kg


@pytest.mark.xfail(reason="BUG: the group's so'm tan narx range is taken from the "
                          "lot that is cheapest in DOLLARS (min() over (usd, uzs) "
                          "tuples, crm/views.py:1026). With lots booked at "
                          "different kursi the so'm pair comes out inverted — the "
                          "Ombor prints a range whose low end is above its high end",
                   strict=False)
def test_som_tannarx_range_is_a_real_range_when_kursi_differ(admin_client):
    # cheaper in dollars, dearer in so'm (booked when the kurs was high)
    _lot(brand="2102", kg="1000", lot_typed="18000", contract_currency=UZS,
         lot_rate="15000", arrived="2026-07-19")           # $1.20/kg · 18 000 so'm
    _lot(brand="2102", kg="1000", lot_typed="13000", contract_currency=UZS,
         lot_rate="10000", partner="Ikki", arrived="2026-07-23")  # $1.30 · 13 000

    g = _group(admin_client, "2102")
    assert g["cost_min"] < g["cost_max"]
    assert g["cost_min_uzs"] <= g["cost_max_uzs"], (
        f"so'm range printed backwards: {g['cost_min_uzs']} – {g['cost_max_uzs']}")


@pytest.mark.xfail(reason="BUG: a truck line with no narx of its own inherits the "
                          "kelishuv's so'm narx (ShipmentLine.unit_price_uzs) but "
                          "landed_cost_per_kg_uzs restates it at the LINE's own "
                          "default kurs of 12 000, and ombor.html reads it with "
                          "lot.currency (usd) instead of lot.unit_currency — so a "
                          "so'm kelishuv's stock shows a dollar tan narx that is "
                          "8% off",
                   strict=False)
def test_inherited_narx_lot_reports_the_kelishuv_som_tannarx(admin_client):
    lot = _lot(kg="1000", lot_typed=None, contract_typed="13000",
               contract_currency=UZS, contract_rate="13000")
    assert lot.unit_price == Decimal("1.0000")
    assert lot.unit_price_uzs == Decimal("13000.00")
    assert lot.unit_currency == UZS

    assert lot.landed_cost_per_kg == Decimal("1.0000")
    assert lot.landed_cost_per_kg_uzs == Decimal("13000.00")   # not 12 000
    g = _group(admin_client, "LLDPE")
    assert g["cost_min_uzs"] == Decimal("13000.00")


def test_profit_drops_by_the_credit_when_a_return_is_not_restocked(admin_client):
    lot = _lot(kg="1000", lot_typed="1.20")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="2.00")
    assert sale.profit == Decimal("320.00")             # (2.00 − 1.20) × 400

    _scrap(sale, kg="100")
    sale.refresh_from_db()
    # 400 kg cost us 480; we keep 600 of the 800 revenue → 120 earned.
    assert sale.net_total == Decimal("600.00")
    assert sale.profit == Decimal("120.00")


def test_profit_drops_by_the_margin_when_a_return_is_restocked(admin_client):
    """The mirror of the case above, and the one the code gets right: the goods
    came back, so only their margin is reversed."""
    lot = _lot(kg="1000", lot_typed="1.20")
    sale = _sell(admin_client, lot, _customer(), kg="400", price="2.00")
    _give_back(admin_client, sale, kg="100")

    sale.refresh_from_db()
    assert sale.profit == Decimal("240.00")             # (2.00 − 1.20) × 300
    assert sale.net_total == Decimal("600.00")


def test_mixed_currency_lots_of_one_marka_still_sum_in_both_currencies(admin_client):
    """A marka whose lots were booked in different currencies at different kursi:
    the shelf total in each currency must be the sum of the lots' own figures,
    never a re-rating of the other side."""
    a = _lot(brand="2102", kg="1000", lot_typed="1.20", lot_currency=USD,
             lot_rate="12000", arrived="2026-07-19")
    b = _lot(brand="2102", kg="2000", lot_typed="18000", contract_currency=UZS,
             lot_rate="15000", partner="Ikki", arrived="2026-07-23")

    total, total_uzs, kg = stock_value()
    assert kg == Decimal("3000.000")
    assert total == (Decimal("1000") * a.landed_cost_per_kg
                     + Decimal("2000") * b.landed_cost_per_kg)
    assert total_uzs == (Decimal("1000") * a.landed_cost_per_kg_uzs
                         + Decimal("2000") * b.landed_cost_per_kg_uzs)
    # each lot's so'm cost stands at ITS OWN kurs, not a blended one
    assert a.landed_cost_per_kg_uzs == Decimal("14400.00")   # 1.20 × 12 000
    assert b.landed_cost_per_kg_uzs == Decimal("18000.00")   # 1.20 × 15 000
