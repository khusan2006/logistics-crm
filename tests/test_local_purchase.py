"""Mahalliy xarid — granula bought here from a third party (a Telegram seller, say) to
sell on.

The feature rests on the birja's decision: a purchase is an ordinary `Contract` under a
placeholder hamkor flagged `is_local`, with one `Shipment` that has already landed. So
the lot is stock the ombor, the sotuv form and FIFO already know how to sell, and what
is still owed is an ordinary kelishuv qarz. These tests check that the purchase really
does behave as stock and as a qarz, and that it stays off the Eron and birja screens.
"""
from decimal import Decimal

import pytest
from conftest import line_data, supplier_payment_rows

from crm.models import (
    AuditLog, Contract, Customer, LocalPurchase, Sale, Shipment, ShipmentStatus, SupplierPayment,
    brand_on_hand_kg, local_partner,
)

pytestmark = pytest.mark.django_db


# --- helpers ---------------------------------------------------------------

def _post(client, **overrides):
    data = {"created": "2026-09-10", "brand": "LLDPE", "kg": "5000", "currency": "usd",
            "price": "1.10", "paid_now": "", "seller_name": "", "seller_phone": "",
            "note": ""}
    data.update(overrides)
    return client.post("/mahalliy-xaridlar/new/", data)


def _edit(client, purchase, **overrides):
    data = {"created": "2026-09-10", "brand": "LLDPE", "kg": "5000", "currency": "usd",
            "price": "1.10", "paid_now": "", "seller_name": "", "seller_phone": "",
            "note": ""}
    data.update(overrides)
    return client.post(f"/mahalliy-xaridlar/{purchase.pk}/edit/", data)


def _sell(client, kg, brand="LLDPE", date="2026-09-12"):
    customer = Customer.objects.create(name="Alisher Mebel", phone="1", address="T")
    return client.post("/sales/new/", {
        "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
        "date": date, "debt_deadline": "", "note": "",
        **line_data({"brand": brand, "kg": kg, "price": "1.50"})})


# --- it is stock -----------------------------------------------------------

def test_a_purchase_lands_in_the_ombor_as_one_lot(admin_client):
    assert _post(admin_client).status_code == 302
    purchase = LocalPurchase.objects.get()
    assert purchase.contract.partner == local_partner()
    assert purchase.contract.code == "mahalliy-1"
    assert purchase.shipment.arrived.isoformat() == "2026-09-10"
    assert purchase.lot.kg == Decimal("5000")
    assert brand_on_hand_kg("LLDPE") == Decimal("5000")
    assert "mahalliy-1" in admin_client.get("/ombor/").content.decode()


def test_it_is_sold_through_the_ordinary_sotuv_form(admin_client):
    _post(admin_client)
    assert _sell(admin_client, "1200").status_code == 302
    purchase = LocalPurchase.objects.get()
    assert Sale.objects.get().line == purchase.lot
    assert purchase.lot.available_kg == Decimal("3800")


def test_the_seller_is_optional_and_kept_when_given(admin_client):
    _post(admin_client)
    _post(admin_client, seller_name="Akmal (Telegram)", seller_phone="+998901112233")
    anonymous, named = LocalPurchase.objects.order_by("pk")
    assert anonymous.seller_name == ""
    assert named.seller_name == "Akmal (Telegram)"
    assert named.seller_phone == "+998901112233"
    # One placeholder hamkor for all of them, whoever sold it.
    assert anonymous.contract.partner == named.contract.partner


def test_a_typed_marka_joins_the_one_already_on_the_books(admin_client):
    _post(admin_client, brand="LLDPE")
    _post(admin_client, brand="lldpe")
    assert {p.line.brand for p in LocalPurchase.objects.all()} == {"LLDPE"}
    assert brand_on_hand_kg("LLDPE") == Decimal("10000")


# --- nasiya and to'lov -----------------------------------------------------

def test_nasiya_leaves_the_whole_price_owed_until_a_tolov_pays_it_down(admin_client):
    _post(admin_client, kg="1000", price="2")
    contract = LocalPurchase.objects.get().contract
    assert contract.payable_left_own == Decimal("2000.00")
    assert not SupplierPayment.objects.exists()

    resp = admin_client.post("/supplier-payments/new/", supplier_payment_rows(
        {"currency": "usd", "amount": "500", "exchange_rate": "12000"},
        contract=contract, date="2026-09-11"))
    assert resp.status_code == 302
    contract = Contract.objects.get(pk=contract.pk)
    assert contract.payable_left_own == Decimal("1500.00")


def test_paid_on_the_spot_is_an_ordinary_hamkor_tolov(admin_client):
    _post(admin_client, kg="1000", price="2", paid_now="2000")
    contract = LocalPurchase.objects.get().contract
    payment = SupplierPayment.objects.get()
    assert payment.contract == contract
    assert payment.amount == Decimal("2000.00")
    assert payment.date.isoformat() == "2026-09-10"
    assert contract.payable_left_own == Decimal("0.00")


def test_a_som_purchase_is_priced_and_owed_in_som(admin_client):
    _post(admin_client, currency="uzs", kg="1000", price="16500", paid_now="5000000")
    purchase = LocalPurchase.objects.get()
    assert purchase.contract.currency == "uzs"
    assert purchase.line.price_uzs == Decimal("16500.00")
    assert purchase.contract.payable_left_own == Decimal("11500000.00")


def test_cannot_pay_more_than_the_purchase_costs(admin_client):
    resp = _post(admin_client, kg="1000", price="2", paid_now="2500")
    assert resp.status_code == 200
    assert not LocalPurchase.objects.exists()
    assert not Contract.objects.exists()


# --- it stays on its own page ----------------------------------------------

def test_it_stays_off_the_eron_and_birja_screens(admin_client):
    _post(admin_client)
    partner = local_partner()
    for url in ("/contracts/?state=", "/shipments/", "/birja/kelishuvlar/?state=",
                "/birja/yuklar/"):
        assert "mahalliy-1" not in admin_client.get(url).content.decode(), url
    assert f"/partners/{partner.pk}/" not in admin_client.get("/partners/").content.decode()
    assert admin_client.get("/").status_code == 200
    assert not LocalPurchase.objects.get().shipment.customs_pending


def test_it_is_listed_on_its_own_page_with_its_seller(admin_client):
    _post(admin_client, seller_name="Akmal")
    html = admin_client.get("/mahalliy-xaridlar/").content.decode()
    assert "mahalliy-1" in html
    assert "Akmal" in html
    assert admin_client.get("/mahalliy-xaridlar/export.xlsx").status_code == 200


def test_the_kelishuv_and_yuk_screens_send_it_back_to_its_own_page(admin_client):
    _post(admin_client)
    purchase = LocalPurchase.objects.get()
    for url in (f"/contracts/{purchase.contract.pk}/edit/",
                f"/contracts/{purchase.contract.pk}/delete/",
                f"/shipments/{purchase.shipment.pk}/edit/",
                f"/shipments/{purchase.shipment.pk}/delete/"):
        resp = admin_client.post(url, {})
        assert resp.status_code == 302, url
        assert resp["Location"] == "/mahalliy-xaridlar/", url
    assert LocalPurchase.objects.exists()


def test_its_yuk_cannot_be_moved_off_arrival(admin_client):
    """The lot page links to the yuk page, and the yuk page carries the holat buttons.
    Taken off arrival the lot would leave the ombor with its sotuvlar still on it."""
    _post(admin_client)
    shipment = LocalPurchase.objects.get().shipment
    other = ShipmentStatus.objects.exclude(is_arrival=True).first()
    resp = admin_client.post(f"/shipments/{shipment.pk}/status/", {"status": other.pk})
    assert resp.status_code == 302
    shipment.refresh_from_db()
    assert shipment.status.is_arrival
    assert shipment.arrived is not None
    assert brand_on_hand_kg("LLDPE") == Decimal("5000")


def test_a_tarjimon_cannot_reach_it(translator_client):
    assert translator_client.get("/mahalliy-xaridlar/").status_code == 403
    assert translator_client.get("/mahalliy-xaridlar/new/").status_code == 403


# --- correcting it ---------------------------------------------------------

def test_an_edit_moves_the_kelishuv_and_the_lot_together(admin_client):
    _post(admin_client)
    purchase = LocalPurchase.objects.get()
    resp = _edit(admin_client, purchase, kg="6000", price="1.20", seller_name="Akmal")
    assert resp.status_code == 302
    purchase = LocalPurchase.objects.get()
    assert purchase.lot.kg == Decimal("6000")
    assert purchase.line.kg == Decimal("6000")
    assert purchase.line.price == Decimal("1.2000")
    assert purchase.seller_name == "Akmal"
    assert Shipment.objects.count() == 1


def test_an_edit_cannot_take_kg_below_what_was_sold(admin_client):
    _post(admin_client)
    _sell(admin_client, "4000")
    purchase = LocalPurchase.objects.get()
    assert _edit(admin_client, purchase, kg="3000").status_code == 200
    assert LocalPurchase.objects.get().lot.kg == Decimal("5000")


def test_the_edit_form_is_the_create_form_filled_in(admin_client):
    _post(admin_client, kg="1000", price="2", paid_now="500")
    purchase = LocalPurchase.objects.get()
    form = admin_client.get(f"/mahalliy-xaridlar/{purchase.pk}/edit/").context["form"]
    create = admin_client.get("/mahalliy-xaridlar/new/").context["form"]
    assert list(form.fields) == list(create.fields)
    assert Decimal(form["paid_now"].value()) == Decimal("500")


def test_an_edit_corrects_the_spot_tolov_rather_than_adding_one(admin_client):
    _post(admin_client, kg="1000", price="2", paid_now="500")
    purchase = LocalPurchase.objects.get()
    assert _edit(admin_client, purchase, kg="1000", price="2",
                 paid_now="800").status_code == 302
    payment = SupplierPayment.objects.get()
    assert payment.amount == Decimal("800")
    assert LocalPurchase.objects.get().spot_payment == payment
    assert LocalPurchase.objects.get().contract.payable_left_own == Decimal("1200")
    assert "hozir to'landi" in AuditLog.objects.latest("pk").summary
    _edit(admin_client, purchase, kg="1000", price="2", paid_now="300")
    assert LocalPurchase.objects.get().contract.payable_left_own == Decimal("1700")


def test_emptying_hozir_tolandi_turns_the_purchase_into_nasiya(admin_client):
    _post(admin_client, kg="1000", price="2", paid_now="500")
    purchase = LocalPurchase.objects.get()
    assert _edit(admin_client, purchase, kg="1000", price="2").status_code == 302
    assert not SupplierPayment.objects.exists()
    assert LocalPurchase.objects.get().contract.payable_left_own == Decimal("2000")


def test_hozir_tolandi_can_be_added_on_an_edit(admin_client):
    _post(admin_client, kg="1000", price="2")
    purchase = LocalPurchase.objects.get()
    assert _edit(admin_client, purchase, kg="1000", price="2",
                 paid_now="300").status_code == 302
    assert LocalPurchase.objects.get().spot_payment.amount == Decimal("300")


def test_hozir_tolandi_leaves_room_for_the_tolovlar_made_since(admin_client):
    _post(admin_client, kg="1000", price="2", paid_now="500")
    purchase = LocalPurchase.objects.get()
    SupplierPayment.objects.create(
        contract=purchase.contract, contract_line=purchase.line, currency="usd",
        amount=Decimal("1200"), amount_uzs=Decimal("0"), exchange_rate=Decimal("12000"))
    assert _edit(admin_client, purchase, kg="1000", price="2",
                 paid_now="900").status_code == 200
    assert LocalPurchase.objects.get().spot_payment.amount == Decimal("500")
    assert _edit(admin_client, purchase, kg="1000", price="2",
                 paid_now="800").status_code == 302


def test_a_wrong_valyuta_can_be_corrected_while_only_hozir_tolandi_is_paid(admin_client):
    # Typed in so'm figures with Dollar left selected: 17 000 "dollars" a kg.
    _post(admin_client, kg="1910", price="17000", paid_now="32470000")
    purchase = LocalPurchase.objects.get()
    form = admin_client.get(f"/mahalliy-xaridlar/{purchase.pk}/edit/").context["form"]
    assert not form.fields["currency"].disabled
    assert _edit(admin_client, purchase, kg="1910", price="17000", currency="uzs",
                 paid_now="32470000").status_code == 302
    purchase = LocalPurchase.objects.get()
    assert purchase.contract.currency == "uzs"
    assert purchase.line.price_uzs == Decimal("17000")
    payment = SupplierPayment.objects.get()
    assert (payment.currency, payment.amount_uzs) == ("uzs", Decimal("32470000"))
    assert purchase.contract.payable_left_own == Decimal("0")


def test_the_spot_tolov_shows_as_typed_even_under_the_wrong_valyuta(admin_client):
    _post(admin_client, kg="1910", price="17000", paid_now="1000")
    purchase = LocalPurchase.objects.get()
    SupplierPayment.objects.filter(pk=purchase.spot_payment_id).update(
        currency="uzs", amount_uzs=Decimal("32470000"))
    form = admin_client.get(f"/mahalliy-xaridlar/{purchase.pk}/edit/").context["form"]
    assert Decimal(form["paid_now"].value()) == Decimal("32470000")


def test_a_later_tolov_still_pins_the_valyuta(admin_client):
    _post(admin_client, kg="1000", price="2")
    purchase = LocalPurchase.objects.get()
    SupplierPayment.objects.create(
        contract=purchase.contract, contract_line=purchase.line, currency="usd",
        amount=Decimal("100"), amount_uzs=Decimal("0"), exchange_rate=Decimal("12000"))
    form = admin_client.get(f"/mahalliy-xaridlar/{purchase.pk}/edit/").context["form"]
    assert form.fields["currency"].disabled


def test_resaving_the_edit_form_untouched_moves_nothing(admin_client):
    _post(admin_client, currency="uzs", kg="1000", price="16500", paid_now="5000000")
    purchase = LocalPurchase.objects.get()
    url = f"/mahalliy-xaridlar/{purchase.pk}/edit/"
    for _ in range(2):
        form = admin_client.get(url).context["form"]
        shown = {name: form[name].value() for name in form.fields}
        body = {k: "" if v is None else str(v) for k, v in shown.items()}
        assert admin_client.post(url, body).status_code == 302
    payment = SupplierPayment.objects.get()
    assert payment.amount_uzs == Decimal("5000000")
    assert LocalPurchase.objects.get().line.price_uzs == Decimal("16500")


def test_delete_takes_the_purchase_and_its_tolov_away(admin_client):
    _post(admin_client, kg="1000", price="2", paid_now="500")
    purchase = LocalPurchase.objects.get()
    assert admin_client.post(f"/mahalliy-xaridlar/{purchase.pk}/delete/").status_code == 302
    assert not LocalPurchase.objects.exists()
    assert not Contract.objects.exists()
    assert not Shipment.objects.exists()
    assert not SupplierPayment.objects.exists()


def test_delete_is_refused_once_something_was_sold(admin_client):
    _post(admin_client)
    _sell(admin_client, "100")
    purchase = LocalPurchase.objects.get()
    admin_client.post(f"/mahalliy-xaridlar/{purchase.pk}/delete/")
    assert LocalPurchase.objects.exists()
    assert Sale.objects.exists()
