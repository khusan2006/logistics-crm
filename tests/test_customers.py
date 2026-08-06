import pytest

from crm.models import AuditLog, Customer


def test_create_customer(admin_client):
    resp = admin_client.post("/customers/new/", {
        "name": "Alisher Mebel", "phone": "+998 90 111 22 33", "address": "Toshkent, Chilonzor", "note": "",
    })
    assert resp.status_code == 302
    assert Customer.objects.filter(name="Alisher Mebel").exists()
    assert AuditLog.objects.filter(target_type="Mijoz").exists()


def test_list_and_search(admin_client):
    Customer.objects.create(name="Alisher Mebel", phone="1", address="Toshkent")
    Customer.objects.create(name="Zarina Plast", phone="2", address="Samarqand")
    html = admin_client.get("/customers/?q=alisher").content.decode()
    assert "Alisher" in html and "Zarina Plast" not in html


def test_translator_forbidden(translator_client):
    assert translator_client.get("/customers/").status_code == 403


def test_create_customer_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/customers/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_customer_modal_post_valid_returns_204_with_redirect(admin_client):
    resp = admin_client.post(
        "/customers/new/",
        {"name": "Bekzod Savdo", "phone": "+998 91 222 33 44", "address": "Farg'ona", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/customers/"
    assert Customer.objects.filter(name="Bekzod Savdo").exists()


def test_create_customer_modal_post_invalid_returns_422(admin_client):
    resp = admin_client.post(
        "/customers/new/",
        {"name": "", "phone": "", "address": "", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html


def test_customer_quick_create(admin_client, db):
    import json
    from crm.models import Customer
    resp = admin_client.post("/customers/quick/", {"name": "Yangi Mijoz", "phone": "+998 90 111 22 33"})
    assert resp.status_code == 200
    d = json.loads(resp.content)
    assert d["created"] is True
    assert d["id"] == Customer.objects.get(name="Yangi Mijoz").pk


def test_customer_quick_create_reuses_same_name(admin_client, db):
    import json
    from crm.models import Customer
    existing = Customer.objects.create(name="Bor Mijoz", phone="1")
    d = json.loads(admin_client.post("/customers/quick/", {"name": "bor mijoz"}).content)
    assert d["created"] is False and d["id"] == existing.pk
    assert Customer.objects.filter(name__iexact="bor mijoz").count() == 1


def test_customer_quick_create_translator_forbidden(translator_client, db):
    assert translator_client.post("/customers/quick/", {"name": "X"}).status_code == 403


def test_sale_form_customer_has_quick_add_hook(db):
    from crm.forms import SaleForm
    html = str(SaleForm())
    assert "data-quick-add-url" in html and "/customers/quick/" in html


# --- boshlang'ich avans -----------------------------------------------------

def test_a_new_mijoz_can_be_created_with_the_money_they_already_paid(admin_client, db):
    """A mijoz who paid up front, before any sotuv was written. The figure becomes an
    ordinary to'lov sitting on no sotuv — which is what an avans is — so it settles
    their first sotuv by itself instead of needing a second trip through the form."""
    from decimal import Decimal

    from crm.models import CustomerPayment, customer_balance_by_currency

    resp = admin_client.post("/customers/new/", {
        "name": "Avansli", "phone": "", "address": "", "note": "",
        "opening_avans": "12650000", "opening_avans_currency": "uzs"})
    assert resp.status_code == 302

    customer = Customer.objects.get(name="Avansli")
    payment = CustomerPayment.objects.get(customer=customer)
    assert payment.amount_uzs == Decimal("12650000.00")     # typed side, exact
    assert payment.currency == "uzs"
    assert payment.note == "Boshlang'ich avans"
    # negative = an avans we are holding, in the currency it arrived in
    assert customer_balance_by_currency(customer) == [("uzs", Decimal("-12650000.00"))]


def test_a_new_mijoz_without_an_avans_books_no_tolov(admin_client, db):
    from crm.models import CustomerPayment

    assert admin_client.post("/customers/new/", {
        "name": "Avanssiz", "phone": "", "address": "", "note": "",
        "opening_avans": "", "opening_avans_currency": "usd"}).status_code == 302
    assert not CustomerPayment.objects.filter(customer__name="Avanssiz").exists()


def test_the_avans_box_is_not_offered_when_editing_a_mijoz(admin_client, db):
    """Editing a mijoz is not the place to book money — the To'lov button beside
    them is, and it records a date and a usul."""
    customer = Customer.objects.create(name="Bor Mijoz", phone="", address="")
    form = admin_client.get(f"/customers/{customer.pk}/edit/").context["form"]
    assert "opening_avans" not in form.fields
    assert "opening_avans" in admin_client.get("/customers/new/").context["form"].fields


# ── Avans for a mijoz who already exists ─────────────────────────────────────

def test_an_existing_mijoz_can_be_given_an_avans_in_both_currencies(admin_client, db):
    """The gap the opening avans left: it only ever appears while a mijoz is being
    created, so from the second visit on there was no way to book money handed over
    ahead of an order. A mijoz commonly puts down dollars AND so'm at once, and both
    are booked in the currency they arrived in — neither is converted into the other."""
    from decimal import Decimal

    from crm.models import CustomerPayment, customer_balance_by_currency

    customer = Customer.objects.create(name="Eski Mijoz", phone="", address="")
    resp = admin_client.post(f"/customers/{customer.pk}/avans/", {
        "date": "2026-08-06", "amount_usd": "1000", "amount_uzs": "5000000",
        "method": "cash", "note": ""})
    assert resp.status_code == 302

    rows = CustomerPayment.objects.filter(customer=customer)
    assert rows.count() == 2
    usd_row = rows.get(currency="usd")
    uzs_row = rows.get(currency="uzs")
    assert usd_row.amount == Decimal("1000.00")          # typed side, exact
    assert uzs_row.amount_uzs == Decimal("5000000.00")   # typed side, exact
    assert usd_row.note == "Avans" and uzs_row.note == "Avans"
    # negative = money we are holding, one position per currency it arrived in
    assert customer_balance_by_currency(customer) == [
        ("usd", Decimal("-1000.00")), ("uzs", Decimal("-5000000.00"))]


def test_an_avans_fills_in_only_the_currency_that_was_typed(admin_client, db):
    from crm.models import CustomerPayment

    customer = Customer.objects.create(name="Faqat Dollar", phone="", address="")
    assert admin_client.post(f"/customers/{customer.pk}/avans/", {
        "date": "2026-08-06", "amount_usd": "250", "amount_uzs": "",
        "method": "cash", "note": ""}).status_code == 302
    rows = CustomerPayment.objects.filter(customer=customer)
    assert [r.currency for r in rows] == ["usd"]


def test_an_avans_with_neither_summa_is_rejected(admin_client, db):
    from crm.models import CustomerPayment

    customer = Customer.objects.create(name="Bo'sh Avans", phone="", address="")
    resp = admin_client.post(f"/customers/{customer.pk}/avans/", {
        "date": "2026-08-06", "amount_usd": "", "amount_uzs": "",
        "method": "cash", "note": ""})
    assert resp.status_code == 200
    assert not CustomerPayment.objects.filter(customer=customer).exists()


def test_an_avans_never_asks_for_a_kurs_but_the_rows_still_carry_one(admin_client, db):
    """Nothing here is being converted to know what it is worth — each side is money
    in hand. The rows still inherit a rate so the kassa's other column holds something."""
    from crm.models import CustomerPayment

    customer = Customer.objects.create(name="Kurssiz Avans", phone="", address="")
    form = admin_client.get(f"/customers/{customer.pk}/avans/").context["form"]
    assert "exchange_rate" not in form.fields

    admin_client.post(f"/customers/{customer.pk}/avans/", {
        "date": "2026-08-06", "amount_usd": "100", "amount_uzs": "",
        "method": "cash", "note": ""})
    assert CustomerPayment.objects.get(customer=customer).exchange_rate > 0


def test_an_avans_from_a_mijoz_who_owes_us_goes_onto_the_qarz(admin_client, db):
    """Not held money: it belongs to the sotuv that has been waiting for it. The
    same rule every other to'lov follows — money must not skip a queue it is in."""
    from decimal import Decimal

    from conftest import make_lot

    from crm.models import Sale, customer_balance_by_currency

    customer = Customer.objects.create(name="Qarzdor", phone="", address="")
    lot = make_lot(kg="1000", arrived="2026-07-16")
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"),
                        price=Decimal("1.00"), date="2026-08-01")  # $1 000 owed
    assert customer_balance_by_currency(customer) == [("usd", Decimal("1000.00"))]

    admin_client.post(f"/customers/{customer.pk}/avans/", {
        "date": "2026-08-06", "amount_usd": "400", "amount_uzs": "",
        "method": "cash", "note": ""})
    assert customer_balance_by_currency(customer) == [("usd", Decimal("600.00"))]


def test_the_avans_button_is_on_every_mijoz_row(admin_client, db):
    customer = Customer.objects.create(name="Tugmali", phone="", address="")
    html = admin_client.get("/customers/").content.decode()
    assert f'/customers/{customer.pk}/avans/' in html


def test_translator_cannot_book_an_avans(translator_client, db):
    customer = Customer.objects.create(name="Yopiq", phone="", address="")
    assert translator_client.get(f"/customers/{customer.pk}/avans/").status_code == 403
