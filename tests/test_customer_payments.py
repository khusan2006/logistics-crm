from decimal import Decimal

from crm.models import (
    Contract, ContractLine, Customer, CustomerPayment, Partner, PaymentAllocation, Sale, Shipment, ShipmentLine, ShipmentStatus,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="10000", brand="LLDPE", contract_price="1.00"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01", deadline="2026-08-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal(contract_price))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal(kg))
    return _ship_obj


def _sale(customer, lot, kg, price, date):
    return Sale.objects.create(
        customer=customer, shipment=lot, kg=Decimal(kg), price=Decimal(price),
        cost_price=lot.landed_cost_per_kg, date=date,
    )


def test_uzs_converted_to_usd(admin_client, db):
    customer = _customer()
    admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "uzs", "amount": "1265000",
        "exchange_rate": "12650", "method": "cash", "note": "",
    })
    p = CustomerPayment.objects.get()
    assert p.amount == Decimal("100.00")
    assert p.amount_original == Decimal("1265000")
    assert p.exchange_rate == Decimal("12650")


def test_usd_payment_passes_with_blank_rate(admin_client, db):
    customer = _customer()
    resp = admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "400",
        "exchange_rate": "", "method": "transfer", "note": "",
    })
    assert resp.status_code == 302
    assert CustomerPayment.objects.filter(customer=customer, amount=Decimal("400.00")).exists()


def test_payment_fifo_allocates_across_customer_sales(admin_client, db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    s2 = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    resp = admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "4000",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    s1.refresh_from_db()
    s2.refresh_from_db()
    assert s1.remaining == Decimal("0")
    assert s2.remaining == Decimal("1000.00")


def test_manual_pick_via_view(admin_client, db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    s2 = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    resp = admin_client.post(f"/customer-payments/new/?customer={customer.pk}", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "2000",
        "exchange_rate": "", "method": "cash", "note": "",
        f"alloc_{s2.pk}": "2000",
    })
    assert resp.status_code == 302
    s1.refresh_from_db()
    s2.refresh_from_db()
    assert s1.remaining == Decimal("3000.00")
    assert s2.remaining == Decimal("0")


def test_create_preselects_customer_and_shows_alloc_table(admin_client, db):
    customer = _customer()
    lot = _lot()
    _sale(customer, lot, "500", "1.00", "2026-07-17")
    resp = admin_client.get(f"/customer-payments/new/?customer={customer.pk}")
    assert resp.status_code == 200
    assert resp.context["form"].initial.get("customer") == customer.pk
    html = resp.content.decode()
    assert "alloc_" in html


def test_create_without_customer_has_no_alloc_table(admin_client, db):
    resp = admin_client.get("/customer-payments/new/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "alloc_" not in html


def test_create_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/customer-payments/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_modal_post_valid_returns_204_with_redirect(admin_client, db):
    customer = _customer()
    resp = admin_client.post(
        "/customer-payments/new/",
        {"customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "400",
         "exchange_rate": "", "method": "transfer", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/customer-payments/"
    assert CustomerPayment.objects.filter(customer=customer).exists()


def test_create_modal_post_invalid_returns_422(admin_client, db):
    resp = admin_client.post(
        "/customer-payments/new/",
        {"customer": "", "date": "2026-07-20", "currency": "usd", "amount": "400",
         "exchange_rate": "", "method": "transfer", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html
    assert not CustomerPayment.objects.exists()


def test_edit_reallocates_after_amount_change(admin_client, db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "1000",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    payment = CustomerPayment.objects.get()
    s1.refresh_from_db()
    assert s1.remaining == Decimal("2000.00")

    resp = admin_client.post(f"/customer-payments/{payment.pk}/edit/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "3000",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    s1.refresh_from_db()
    assert s1.remaining == Decimal("0")
    payment.refresh_from_db()
    assert payment.amount == Decimal("3000.00")


def test_delete_removes_allocations(admin_client, db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "1000",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    payment = CustomerPayment.objects.get()
    resp = admin_client.post(f"/customer-payments/{payment.pk}/delete/")
    assert resp.status_code == 302
    assert not CustomerPayment.objects.filter(pk=payment.pk).exists()
    assert not PaymentAllocation.objects.filter(sale=s1).exists()
    s1.refresh_from_db()
    assert s1.remaining == Decimal("3000.00")


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/customer-payments/").status_code == 403
    assert translator_client.get("/customer-payments/new/").status_code == 403


def test_list_shows_payment(admin_client, db):
    customer = _customer()
    admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "400",
        "exchange_rate": "", "method": "transfer", "note": "",
    })
    html = admin_client.get("/customer-payments/").content.decode()
    assert customer.name in html


def test_customer_list_has_payment_action(admin_client, db):
    customer = _customer()
    html = admin_client.get("/customers/").content.decode()
    assert f"/customer-payments/new/?customer={customer.pk}" in html
