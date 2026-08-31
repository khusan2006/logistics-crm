"""Birja — granula bought on the exchange here, rather than agreed with a hamkor in
Eron.

The whole feature rests on one decision: a birja kelishuv is an ordinary `Contract`
and a birja yuk an ordinary `Shipment`, both hanging off a singleton hamkor row
flagged `is_birja`. That is what lets the ombor, the sotuvlar and the kassa stay one
set of books — and it is what these tests are mostly checking: that the two lists
separate cleanly in BOTH directions, and that the Eron-road facts a birja load has
no business carrying (a QR kod, a bojxona) stay off it.
"""
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from conftest import line_data, make_shipment, supplier_payment_rows

from crm.models import (
    Contract, ContractLine, Currency, Partner, Shipment, ShipmentExpense,
    ShipmentStatus, birja_partner,
)

pytestmark = pytest.mark.django_db


# --- helpers ---------------------------------------------------------------

def _birja_contract(brand="LLDPE", kg="1000", price="1.00", **kw):
    contract = Contract.objects.create(partner=birja_partner(),
                                       created=kw.pop("created", "2026-07-01"), **kw)
    ContractLine.objects.create(contract=contract, brand=brand,
                                kg=Decimal(kg), price=Decimal(price))
    return contract


def _hamkor_contract(brand="HDPE", kg="1000", price="1.00"):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand,
                                kg=Decimal(kg), price=Decimal(price))
    return contract


def _first_birja_status():
    return ShipmentStatus.for_kind(birja=True).first()


def _post_birja_shipment(client, contract, **extra):
    row = {"contract_line": contract.lines.first().pk, "kg": extra.pop("kg", "400")}
    data = {"contract": contract.pk, "status": _first_birja_status().pk,
            "sent": "2026-07-05", "eta": "2026-07-20", "transport": "01A111AA",
            "note": "", **line_data(row)}
    data.update(extra)
    return client.post("/birja/yuklar/new/", data)


# --- the counterparty and the kod ------------------------------------------

def test_the_birja_hamkor_is_one_row_created_on_first_use():
    """Lazily, and never twice. It is not seeded by a migration on purpose:
    `wipe_business_data()` deletes every Partner, so a seeded row would vanish the
    first time starting data is reloaded and the numbering would restart at
    birja-1 beside codes that already exist."""
    assert not Partner.objects.filter(is_birja=True).exists()
    first = birja_partner()
    assert birja_partner().pk == first.pk
    assert Partner.objects.filter(is_birja=True).count() == 1


def test_kelishuvlar_are_coded_birja_1_birja_2():
    """The whole reason the counterparty is a Partner: `Contract.save()` already
    slugifies the hamkor's name and counts on `Partner.code_counter`, so the birja
    codes fall out of the existing machinery rather than a second number line."""
    assert _birja_contract(brand="A").code == "birja-1"
    assert _birja_contract(brand="B").code == "birja-2"


def test_a_deleted_birja_kelishuv_does_not_hand_its_number_back():
    """Same no-recycle rule a hamkor's codes follow — a kod that has been read out
    loud must never come back meaning something else."""
    _birja_contract(brand="A")
    _birja_contract(brand="B").delete()
    assert _birja_contract(brand="C").code == "birja-3"


def test_the_birja_hamkor_is_not_offered_as_one():
    """It is the exchange standing in as a counterparty, not somebody to strike a
    deal with — so it is off the Hamkorlar list and off the kelishuv form's picker."""
    from crm.forms import ContractForm
    birja = birja_partner()
    assert birja not in ContractForm().fields["partner"].queryset


def test_the_hamkorlar_list_does_not_show_it(admin_client):
    _birja_contract()
    Partner.objects.create(name="Pars", phone="1", city="Tehron")
    names = [p.name for p in admin_client.get("/partners/").context["page"]]
    assert names == ["Pars"]


# --- the two lists separate, both ways -------------------------------------

def test_the_two_kelishuvlar_lists_hold_only_their_own(admin_client):
    birja, hamkor = _birja_contract(), _hamkor_contract()
    assert [c.pk for c in admin_client.get("/birja/kelishuvlar/").context["rows"]] \
        == [birja.pk]
    assert [c.pk for c in admin_client.get("/contracts/").context["rows"]] \
        == [hamkor.pk]


def test_the_two_yuklar_lists_hold_only_their_own(admin_client):
    birja = make_shipment(contract=_birja_contract(), status=_first_birja_status())
    hamkor = make_shipment(contract=_hamkor_contract())
    assert [s.pk for s in admin_client.get("/birja/yuklar/").context["shipments"]] \
        == [birja.pk]
    assert [s.pk for s in admin_client.get("/shipments/").context["shipments"]] \
        == [hamkor.pk]


def _sheet_text(response):
    ws = openpyxl.load_workbook(BytesIO(response.content)).active
    return {str(cell.value) for row in ws.iter_rows() for cell in row}


def test_the_excel_button_exports_the_list_it_was_pressed_on(admin_client):
    """The flag is threaded through `_filter_contracts` / `_filter_shipments` rather
    than read off the path, so the file cannot end up holding the other list."""
    _birja_contract(brand="BIRJAMARKA")
    _hamkor_contract(brand="ERONMARKA")
    birja_file = _sheet_text(admin_client.get("/birja/kelishuvlar/export.xlsx"))
    hamkor_file = _sheet_text(admin_client.get("/contracts/export.xlsx"))
    assert "BIRJAMARKA" in birja_file and "ERONMARKA" not in birja_file
    assert "ERONMARKA" in hamkor_file and "BIRJAMARKA" not in hamkor_file


# --- valyuta ---------------------------------------------------------------
#
# Birja purchases are struck in so'm, which is a DEFAULT and not a rule. Everything
# below is the app's existing dual-currency machinery — `own_side`, `convert_pair`,
# the locked picker — asked of a birja kelishuv, because a screen that quietly
# assumed so'm would be wrong the first time a lot is quoted in dollars.

def test_a_birja_kelishuv_can_still_be_struck_in_dollars(admin_client):
    """The so'm is where the picker STARTS, not where it is stuck."""
    resp = admin_client.post("/birja/kelishuvlar/new/", {
        "currency": "usd", "created": "2026-07-04", "note": "",
        **line_data({"brand": "7000F", "kg": "1000", "price": "1.05"})})
    assert resp.status_code == 302
    contract = Contract.objects.get()
    assert contract.currency == Currency.USD and not contract.is_som
    assert contract.lines.first().price == Decimal("1.0500")


def test_a_som_narx_is_stored_on_both_sides_with_the_typed_one_exact(admin_client):
    """`convert_pair` keeps the figure that was actually agreed untouched and
    computes only the other half, so a so'm narx never drifts by a tiyin — the
    dollar twin is the derivation, at the last kurs somebody really typed."""
    admin_client.post("/birja/kelishuvlar/new/", {
        "currency": "uzs", "created": "2026-07-04", "note": "",
        **line_data({"brand": "2102 repak", "kg": "40000", "price": "13500"})})
    line = Contract.objects.get().lines.first()
    assert line.currency == Currency.UZS
    assert line.price_uzs == Decimal("13500.00")          # exactly as typed
    assert line.price == (Decimal("13500") / line.exchange_rate).quantize(
        Decimal("0.0001"))                                 # derived, not typed


def test_the_qarz_on_a_som_kelishuv_is_read_and_settled_in_som(admin_client):
    """The rule every qarz in the app follows (`own_side`): a kelishuv struck in
    so'm is owed in so'm, and a so'm to'lov closes it. Reading the dollar twin
    would leave a settled birja kelishuv permanently unfinished, because that half
    was derived at a kurs neither side agreed to."""
    contract = _birja_contract(kg="1000", price="1.00", currency="uzs")
    line = contract.lines.first()
    line.currency, line.exchange_rate = Currency.UZS, Decimal("12800")
    line.price, line.price_uzs = Decimal("1.0000"), Decimal("12800.00")
    line.save()
    make_shipment(contract=contract, kg="1000")

    assert contract.is_som
    assert contract.payable_left_uzs == Decimal("12800000.00")
    admin_client.post("/supplier-payments/new/", supplier_payment_rows(
        {"currency": "uzs", "amount": "12800000", "exchange_rate": "12800",
         "commission_percent": "", "method": "cash", "note": ""},
        contract=contract, date="2026-07-21"))
    contract = Contract.objects.get(pk=contract.pk)
    assert contract.payable_left_uzs == Decimal("0.00")
    assert contract.is_settled


def test_the_yuklar_list_leads_with_the_kelishuvs_own_currency(admin_client):
    """Qiymati is not a blended figure — the goods on a truck are priced in the one
    currency their kelishuv was struck in — so it has to lead with that side, the
    way the kelishuvlar list and every qarz already do. It led with the dollar for
    every load, which on a so'm kelishuv put a figure nobody agreed to at the front
    of the row. (A truck's XARAJAT still leads in dollars: a so'm transport bill
    beside a dollar bojxona has no agreed side.)"""
    som_contract = _birja_contract(kg="1000", price="1.00", currency="uzs")
    line = som_contract.lines.first()
    line.currency, line.exchange_rate = Currency.UZS, Decimal("12800")
    line.price, line.price_uzs = Decimal("1.0000"), Decimal("12800.00")
    line.save()
    make_shipment(contract=som_contract, kg="1000")

    html = admin_client.get("/birja/yuklar/").content.decode()
    lead = html.index("money-pair")
    alt = html.index("money-alt", lead)
    # "so'm" comes back HTML-escaped, hence the entity rather than the apostrophe.
    assert "so&#x27;m" in html[lead:alt]     # the agreed side leads
    assert "$" in html[alt:alt + 200]        # the derived twin sits beneath it


def test_a_dollar_kelishuvs_yuk_still_leads_with_the_dollar(admin_client):
    """The mirror — nothing changes for the loads that were always dollar."""
    contract = _birja_contract(kg="1000", price="1.00")   # usd by default here
    make_shipment(contract=contract, kg="1000")
    html = admin_client.get("/birja/yuklar/").content.decode()
    lead = html.index("money-pair")
    alt = html.index("money-alt", lead)
    assert "$" in html[lead:alt] and "so&#x27;m" not in html[lead:alt]


def test_the_valyuta_is_frozen_once_money_or_goods_are_on_it(admin_client):
    """Same lock a hamkor kelishuv carries: re-striking it would re-read every
    figure already booked — a 13 500 so'm narx becoming $13 500."""
    from crm.forms import ContractForm
    contract = _birja_contract(currency="uzs")
    assert not ContractForm(instance=contract, birja=True).fields["currency"].disabled
    make_shipment(contract=contract)
    assert ContractForm(instance=contract, birja=True).fields["currency"].disabled


# --- creating through the views --------------------------------------------

def test_creating_a_kelishuv_pins_the_hamkor_and_asks_nothing(admin_client):
    """No hamkor picker: there is one counterparty a birja kelishuv can have, and a
    disabled select offering it would read as a choice somebody might get wrong."""
    from crm.forms import ContractForm
    assert "partner" not in ContractForm(birja=True).fields

    resp = admin_client.post("/birja/kelishuvlar/new/", {
        "currency": "uzs", "created": "2026-07-04", "note": "",
        **line_data({"brand": "7000F", "kg": "30000", "price": "13000"})})
    assert resp.status_code == 302
    contract = Contract.objects.get()
    assert contract.partner.is_birja and contract.code == "birja-1"
    assert contract.currency == Currency.UZS


def test_a_new_kelishuv_defaults_to_som(admin_client):
    """Goods bought here are priced and settled in so'm — still a choice, not a
    lock, so a lot quoted in dollars stays recordable."""
    from crm.forms import ContractForm
    assert ContractForm(birja=True).fields["currency"].initial == Currency.UZS


def test_creating_a_yuk_routes_it_from_the_birja(admin_client):
    contract = _birja_contract()
    assert _post_birja_shipment(admin_client, contract).status_code == 302
    shipment = Shipment.objects.get()
    assert shipment.is_birja
    assert shipment.origin == "Birja"
    assert shipment.destination == "O'zbekiston"


def test_a_birja_yuk_cannot_be_loaded_against_an_eron_kelishuv(admin_client):
    """The kelishuv picker is narrowed to one side, and it is the form field that
    enforces it — posting the other side's pk is rejected, not quietly accepted."""
    hamkor = _hamkor_contract()
    resp = _post_birja_shipment(admin_client, hamkor)
    assert resp.status_code == 200 and not Shipment.objects.exists()


def test_an_eron_yuk_cannot_be_loaded_against_a_birja_kelishuv(admin_client):
    """The mirror, which is the half that is easy to leave out."""
    contract = _birja_contract()
    resp = admin_client.post("/shipments/new/", {
        "contract": contract.pk, "status": ShipmentStatus.objects.first().pk,
        "sent": "2026-07-05", "eta": "2026-07-20", "transport": "01A111AA", "note": "",
        **line_data({"contract_line": contract.lines.first().pk, "kg": "400"})})
    assert resp.status_code == 200 and not Shipment.objects.exists()


# --- the holat chains ------------------------------------------------------

def test_each_list_offers_only_its_own_chain(admin_client):
    make_shipment(contract=_birja_contract(), status=_first_birja_status())
    make_shipment(contract=_hamkor_contract())
    birja_tabs = [t["status"].name
                  for t in admin_client.get("/birja/yuklar/").context["tabs"]]
    hamkor_tabs = [t["status"].name
                   for t in admin_client.get("/shipments/").context["tabs"]]
    assert "Sotib olindi" in birja_tabs and "Chegarada" not in birja_tabs
    assert "Chegarada" in hamkor_tabs and "Sotib olindi" not in hamkor_tabs


def test_the_birja_list_opens_on_the_first_step_of_its_own_chain(admin_client):
    """There is no "Yo'lda" to open on — the operator has not named the birja steps
    yet — so it opens on whichever step is currently first."""
    make_shipment(contract=_birja_contract(), status=_first_birja_status())
    context = admin_client.get("/birja/yuklar/").context
    assert context["default_tab"] == _first_birja_status().pk


def test_a_yuk_cannot_be_moved_into_the_other_chains_holat(admin_client):
    """Posting an Eron holat onto a birja yuk would leave the row in a bosqich its
    own list cannot draw a tab for — invisible on every view of the list it
    belongs to."""
    shipment = make_shipment(contract=_birja_contract(), status=_first_birja_status())
    chegarada = ShipmentStatus.objects.get(name="Chegarada")
    resp = admin_client.post(f"/shipments/{shipment.pk}/status/",
                             {"status": chegarada.pk})
    shipment.refresh_from_db()
    assert resp.status_code == 404
    assert shipment.status.scope == ShipmentStatus.Scope.BIRJA


def test_both_chains_end_on_the_same_arrival_holat(admin_client):
    """The one step they share. It is what turns a yuk into an ombor loti, so a
    birja load reaching it lands on the shelf exactly like an Eron one."""
    arrival = ShipmentStatus.arrival()
    assert arrival in ShipmentStatus.for_kind(birja=True)
    assert arrival in ShipmentStatus.for_kind(birja=False)

    contract = _birja_contract()
    shipment = make_shipment(contract=contract, status=_first_birja_status())
    admin_client.post(f"/shipments/{shipment.pk}/status/", {"status": arrival.pk})
    shipment.refresh_from_db()
    assert shipment.arrived is not None
    assert admin_client.get("/ombor/").status_code == 200


def test_promoting_a_holat_to_arrival_makes_it_shared(admin_client):
    """Otherwise the other chain silently loses its ending and its loads could
    never reach the ombor."""
    chegarada = ShipmentStatus.objects.get(name="Chegarada")
    admin_client.post(f"/statuses/{chegarada.pk}/edit/",
                      {"name": "Chegarada", "scope": "hamkor", "is_arrival": "on"})
    chegarada.refresh_from_db()
    assert chegarada.is_arrival and chegarada.scope == ShipmentStatus.Scope.BOTH
    assert chegarada in ShipmentStatus.for_kind(birja=True)


def test_a_new_holat_lands_at_the_end_of_its_own_chain_not_after_arrival(admin_client):
    """The arrival row sits at a deliberately high `order` so both chains end on
    it. Taking the table-wide maximum would park every new step after it."""
    admin_client.post("/statuses/new/",
                      {"name": "Birjada tekshirildi", "scope": "birja"})
    names = [s.name for s in ShipmentStatus.for_kind(birja=True)]
    assert names[-1] == "Omborga yetib keldi"
    assert names[-2] == "Birjada tekshirildi"


# --- what a birja yuk does not carry ---------------------------------------

def test_an_arrived_birja_lot_is_never_bojxonasi_tolanmagan(admin_client):
    """It crossed no border, so it owes no clearing — and since it will never carry
    a bojxona xarajat, "not recorded yet" would be permanent. The Python property
    and its SQL twin both have to say so: the row's badge comes from one and the
    pill's count from the other."""
    birja = make_shipment(contract=_birja_contract(), status=ShipmentStatus.arrival(),
                          arrived="2026-07-20")
    hamkor = make_shipment(contract=_hamkor_contract(), status=ShipmentStatus.arrival(),
                           arrived="2026-07-20")
    assert birja.customs_pending is False
    assert hamkor.customs_pending is True

    rows = admin_client.get("/shipments/", {"customs": "1"}).context["shipments"]
    assert [s.pk for s in rows] == [hamkor.pk]
    assert admin_client.get("/shipments/").context["customs_pending_count"] == 1


def test_the_birja_list_has_no_bojxona_group_and_no_qr(admin_client):
    make_shipment(contract=_birja_contract(), status=_first_birja_status())
    context = admin_client.get("/birja/yuklar/").context
    assert context["customs_pending_count"] == 0
    assert context["qr_waiting_count"] == 0
    html = admin_client.get("/birja/yuklar/").content.decode()
    assert "Bojxona to'lanmagan" not in html
    assert "QR bor" not in html


def test_the_qr_and_customs_params_narrow_nothing_on_the_birja_list(admin_client):
    """Enforced in `_filter_shipments` rather than by hiding the pills, so a
    hand-typed querystring cannot narrow a list where the answer never differs."""
    shipment = make_shipment(contract=_birja_contract(), status=_first_birja_status())
    rows = admin_client.get("/birja/yuklar/",
                            {"qr": "yoq", "customs": "1"}).context["shipments"]
    assert [s.pk for s in rows] == [shipment.pk]


def test_the_yuk_form_drops_the_qr_and_bojxonachi_boxes():
    from crm.forms import ShipmentForm
    birja_fields = ShipmentForm(birja=True).fields
    assert "qr_date" not in birja_fields and "customs_agent" not in birja_fields
    hamkor_fields = ShipmentForm(birja=False).fields
    assert "qr_date" in hamkor_fields and "customs_agent" in hamkor_fields


def test_the_yuk_detail_page_says_nothing_about_a_qr_kod(admin_client):
    """The shared detail page draws the Eron-road facts off `shipment.is_birja`.
    Left alone it printed "QR kod · Berilmagan" and offered the mark-as-given
    button — which does not read as "none was needed", it reads as a truck somebody
    forgot."""
    birja = make_shipment(contract=_birja_contract(), status=_first_birja_status())
    hamkor = make_shipment(contract=_hamkor_contract())
    assert "QR" not in admin_client.get(f"/shipments/{birja.pk}/").content.decode()
    assert "QR" in admin_client.get(f"/shipments/{hamkor.pk}/").content.decode()


def test_editing_reads_the_kind_off_the_row(admin_client):
    """A yuk never changes sides, so `shipment_edit` is shared — the form asks the
    instance which chain it is on rather than the URL."""
    from crm.forms import ShipmentForm
    shipment = make_shipment(contract=_birja_contract(), status=_first_birja_status())
    assert "qr_date" not in ShipmentForm(instance=shipment).fields
    assert admin_client.get(f"/shipments/{shipment.pk}/edit/").status_code == 200


# --- the books stay together -----------------------------------------------

def test_a_birja_lot_reaches_the_ombor_and_carries_its_xarajatlar(admin_client):
    """"Others will be together": once a birja yuk has landed it is stock like any
    other, priced by the same landed-cost rule."""
    lot = make_shipment(contract=_birja_contract(), status=ShipmentStatus.arrival(),
                        arrived="2026-07-20", kg="400").lines.first()
    ShipmentExpense.objects.create(
        shipment=lot.shipment, category=ShipmentExpense.Category.TRANSPORT,
        amount=Decimal("200"), date="2026-07-20")
    assert lot.landed_cost_per_kg > lot.contract_line.price
    html = admin_client.get("/ombor/").content.decode()
    assert "birja-1" in html


def test_a_hamkor_tolov_can_be_made_against_a_birja_kelishuv(admin_client):
    """Money owed to the birja is owed on the same page as everything else — the
    feature adds two lists, not a second set of books."""
    contract = _birja_contract(kg="1000", price="1.00")
    make_shipment(contract=contract, kg="1000")
    resp = admin_client.post("/supplier-payments/new/", supplier_payment_rows(
        {"currency": "usd", "amount": "500", "exchange_rate": "12000",
         "commission_percent": "", "method": "cash", "note": ""},
        contract=contract, date="2026-07-21"))
    assert resp.status_code == 302
    contract.refresh_from_db()
    assert contract.paid_total == Decimal("500.00")


# --- who may look ----------------------------------------------------------

@pytest.mark.parametrize("url", ["/birja/kelishuvlar/", "/birja/yuklar/",
                                 "/birja/kelishuvlar/export.xlsx",
                                 "/birja/yuklar/export.xlsx"])
def test_a_tarjimon_cannot_reach_the_birja_pages(translator_client, url):
    """Their whole job is the Eron road. `role_required` is on the shared view and
    cannot see which of the two lists it was reached through, so the guard is
    inside it."""
    assert translator_client.get(url).status_code == 403


def test_a_tarjimon_cannot_reach_a_birja_yuk_by_typing_its_number(translator_client):
    """The two per-yuk screens a tarjimon may open are shared by both pipelines, so
    without this a role that can reach no birja LIST could still reach one birja
    ROW. Their own loads are untouched."""
    birja = make_shipment(contract=_birja_contract(), status=_first_birja_status())
    hamkor = make_shipment(contract=_hamkor_contract())
    assert translator_client.get(f"/shipments/{birja.pk}/").status_code == 403
    assert translator_client.get(f"/shipments/{birja.pk}/driver/").status_code == 403
    assert translator_client.get(f"/shipments/{hamkor.pk}/").status_code == 200
    assert translator_client.get(f"/shipments/{hamkor.pk}/driver/").status_code == 200
