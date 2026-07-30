from decimal import Decimal

from conftest import payment_rows
from crm.templatetags.crm_extras import NBSP

from crm.models import (
    Contract, ContractLine, Customer, Partner, Sale, Shipment, ShipmentLine, ShipmentStatus,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="10000", brand="LLDPE", contract_price="1.00"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal(contract_price))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal(kg))
    return _ship_obj_line


def _sale(customer, lot, kg, price, date, debt_deadline=None):
    return Sale.objects.create(
        customer=customer, line=lot, kg=Decimal(kg), price=Decimal(price),
        date=date, debt_deadline=debt_deadline,
    )


def test_customer_with_unpaid_sale_appears_with_correct_total(admin_client, db):
    customer = _customer()
    lot = _lot()
    _sale(customer, lot, "1000", "1.60", "2026-07-17")

    html = admin_client.get("/debts/").content.decode()
    assert customer.name in html
    assert f"1{NBSP}600" in html


def test_fully_paid_customer_not_in_debt_list(admin_client, db):
    customer = _customer(name="Paid Customer")
    lot = _lot()
    sale = _sale(customer, lot, "1000", "1.60", "2026-07-17")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1600"}, customer=customer, date="2026-07-18"))
    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")

    html = admin_client.get("/debts/").content.decode()
    assert "Paid Customer" not in html


def test_advance_customer_not_in_debt_list(admin_client, db):
    customer = _customer(name="Avans Customer")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "500"}, customer=customer, date="2026-07-18"))
    customer.refresh_from_db()
    assert customer.balance < 0

    html = admin_client.get("/debts/").content.decode()
    assert "Avans Customer" not in html


def test_debt_customer_lists_outstanding_sales_and_excludes_paid(admin_client, db):
    customer = _customer()
    lot = _lot()
    unpaid = _sale(customer, lot, "1000", "1.60", "2026-07-17")
    paid = _sale(customer, lot, "500", "1.00", "2026-07-16")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "500"}, customer=customer, date="2026-07-18"))
    paid.refresh_from_db()
    unpaid.refresh_from_db()
    assert paid.remaining == Decimal("0")
    assert unpaid.remaining > Decimal("0")

    resp = admin_client.get(f"/debts/{customer.pk}/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert f"#{lot.pk}" in html
    # Exactly one outstanding sale row: the unpaid one is listed, the fully-paid
    # sale is excluded. (Dates render localized, so count rows rather than date strings.)
    assert html.count('class="row-actions"') == 1
    assert f"$1{NBSP}600" in html          # the outstanding sale's total is shown


def test_overdue_sale_shows_overdue_indicator(admin_client, db):
    customer = _customer(name="Overdue Customer")
    lot = _lot()
    _sale(customer, lot, "1000", "1.60", "2026-07-01", debt_deadline="2026-07-10")

    list_html = admin_client.get("/debts/").content.decode()
    assert "Overdue Customer" in list_html
    assert ">1<" in list_html or "muddati o'tgan" in list_html.lower() or "kechikkan" in list_html.lower()

    detail_html = admin_client.get(f"/debts/{customer.pk}/").content.decode()
    assert "muddati o'tgan" in detail_html.lower() or "kechikkan" in detail_html.lower()


def test_sale_without_muddat_is_due_the_day_it_was_sold(admin_client, db):
    """A blank To'lov muddati means "pay now", not "never due" — otherwise an unpaid
    sotuv sits outside every Qarzlar signal."""
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "1000", "1.60", "2026-07-17", debt_deadline=None)

    sale.refresh_from_db()
    assert str(sale.debt_deadline) == "2026-07-17"


def test_due_customers_sort_above_bigger_debts_not_yet_owed(admin_client, db):
    """Whoever has to pay now leads the table, oldest muddat first — even when a
    mijoz further down owes far more on a muddat that has not arrived."""
    from django.utils import timezone
    from datetime import timedelta

    today = timezone.localdate()
    lot = _lot(kg="100000")
    small_and_due = _customer(name="AAA Bugun")
    big_but_later = _customer(name="ZZZ Keyin")
    oldest_due = _customer(name="MMM Eski")

    _sale(small_and_due, lot, "100", "1.00", str(today), debt_deadline=str(today))
    _sale(big_but_later, lot, "50000", "1.00", str(today),
          debt_deadline=str(today + timedelta(days=30)))
    _sale(oldest_due, lot, "200", "1.00", str(today - timedelta(days=10)),
          debt_deadline=str(today - timedelta(days=10)))

    html = admin_client.get("/debts/").content.decode()
    order = [html.index(c.name) for c in (oldest_due, small_and_due, big_but_later)]
    assert order == sorted(order), "due-first, oldest muddat at the top"
    assert "bugun to'lash kerak" in html
    assert "muddati o'tgan" in html


def test_translator_forbidden(translator_client, db):
    customer = _customer()
    assert translator_client.get("/debts/").status_code == 403
    assert translator_client.get(f"/debts/{customer.pk}/").status_code == 403
