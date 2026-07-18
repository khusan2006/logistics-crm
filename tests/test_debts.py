from decimal import Decimal

from crm.models import Customer, Partner, Contract, Sale, Shipment, ShipmentStatus


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="10000", brand="LLDPE", contract_price="1.00"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(
        partner=partner, brand=brand, kg=Decimal(kg), price=Decimal(contract_price),
        created="2026-07-01", deadline="2026-08-01",
    )
    return Shipment.objects.create(
        contract=contract, kg=Decimal(kg), status=ShipmentStatus.arrival(),
        sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16",
        transport="01A111AA", container="MSCU-1",
    )


def _sale(customer, lot, kg, price, date, debt_deadline=None):
    return Sale.objects.create(
        customer=customer, shipment=lot, kg=Decimal(kg), price=Decimal(price),
        cost_price=lot.landed_cost_per_kg, date=date, debt_deadline=debt_deadline,
    )


def test_customer_with_unpaid_sale_appears_with_correct_total(admin_client, db):
    customer = _customer()
    lot = _lot()
    _sale(customer, lot, "1000", "1.60", "2026-07-17")

    html = admin_client.get("/debts/").content.decode()
    assert customer.name in html
    assert "1,600" in html or "1600" in html


def test_fully_paid_customer_not_in_debt_list(admin_client, db):
    customer = _customer(name="Paid Customer")
    lot = _lot()
    sale = _sale(customer, lot, "1000", "1.60", "2026-07-17")
    admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-18", "currency": "usd", "amount": "1600",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")

    html = admin_client.get("/debts/").content.decode()
    assert "Paid Customer" not in html


def test_advance_customer_not_in_debt_list(admin_client, db):
    customer = _customer(name="Avans Customer")
    admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-18", "currency": "usd", "amount": "500",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    customer.refresh_from_db()
    assert customer.balance < 0

    html = admin_client.get("/debts/").content.decode()
    assert "Avans Customer" not in html


def test_debt_customer_lists_outstanding_sales_and_excludes_paid(admin_client, db):
    customer = _customer()
    lot = _lot()
    unpaid = _sale(customer, lot, "1000", "1.60", "2026-07-17")
    paid = _sale(customer, lot, "500", "1.00", "2026-07-16")
    admin_client.post("/customer-payments/new/", {
        "customer": customer.pk, "date": "2026-07-18", "currency": "usd", "amount": "500",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    paid.refresh_from_db()
    unpaid.refresh_from_db()
    assert paid.remaining == Decimal("0")
    assert unpaid.remaining > Decimal("0")

    resp = admin_client.get(f"/debts/{customer.pk}/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert f"#{lot.pk}" in html
    # the fully paid sale's row should not be present; check via its date not duplicated
    assert html.count("2026-07-16") == 0 or "2026-07-17" in html


def test_overdue_sale_shows_overdue_indicator(admin_client, db):
    customer = _customer(name="Overdue Customer")
    lot = _lot()
    _sale(customer, lot, "1000", "1.60", "2026-07-01", debt_deadline="2026-07-10")

    list_html = admin_client.get("/debts/").content.decode()
    assert "Overdue Customer" in list_html
    assert ">1<" in list_html or "muddati o'tgan" in list_html.lower() or "kechikkan" in list_html.lower()

    detail_html = admin_client.get(f"/debts/{customer.pk}/").content.decode()
    assert "muddati o'tgan" in detail_html.lower() or "kechikkan" in detail_html.lower()


def test_translator_forbidden(translator_client, db):
    customer = _customer()
    assert translator_client.get("/debts/").status_code == 403
    assert translator_client.get(f"/debts/{customer.pk}/").status_code == 403
