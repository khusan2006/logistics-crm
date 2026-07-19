from decimal import Decimal

from crm.models import (
    Contract, Customer, CustomerPayment, Partner, Sale, Shipment, ShipmentStatus, SupplierPayment,
)


def _contract(partner=None, brand="LLDPE", created="2026-07-01"):
    partner = partner or Partner.objects.create(name="Pars", phone="1", city="T")
    return Contract.objects.create(
        partner=partner, brand=brand, kg=Decimal("1000"), price=Decimal("1"),
        created=created, deadline="2026-08-01",
    )


def _arrived_shipment(contract, kg=Decimal("500"), eta="2026-07-15", arrived="2026-07-16"):
    return Shipment.objects.create(
        contract=contract, kg=kg, status=ShipmentStatus.arrival(),
        sent="2026-07-05", eta=eta, arrived=arrived,
        transport="01A111AA", container="MSCU-1",
    )


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _sale(customer, shipment, kg=Decimal("100"), price=Decimal("2"), cost_price=Decimal("1"),
          date="2026-07-17"):
    return Sale.objects.create(
        customer=customer, shipment=shipment, kg=kg, price=price, cost_price=cost_price, date=date,
    )


def test_reports_page_renders_kpis(admin_client, db):
    contract = _contract()
    shipment = _arrived_shipment(contract)
    customer = _customer()
    _sale(customer, shipment)
    SupplierPayment.objects.create(
        contract=contract, date="2026-07-11", amount=Decimal("200.00"), method="cash",
    )
    CustomerPayment.objects.create(
        customer=customer, date="2026-07-17", amount=Decimal("100.00"), method="cash",
    )

    resp = admin_client.get("/reports/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Sotuvdan foyda" in html
    # profit = (2 - 1) * 100 = 100.00
    assert resp.context["profit_total"] == Decimal("100.00")
    assert "100.00" in html


def test_partner_filter_narrows_per_partner_table(admin_client, db):
    partner1 = Partner.objects.create(name="Pars", phone="1", city="T")
    partner2 = Partner.objects.create(name="Kaveh", phone="2", city="S")
    contract1 = _contract(partner=partner1)
    _contract(partner=partner2)

    resp = admin_client.get("/reports/", {"partner": partner1.pk})
    assert resp.status_code == 200
    partner_names = [row["partner"].name for row in resp.context["partner_rows"]]
    assert "Pars" in partner_names
    assert "Kaveh" not in partner_names


def test_date_filter_excludes_out_of_range_sale_from_profit(admin_client, db):
    contract = _contract()
    shipment = _arrived_shipment(contract)
    customer = _customer()
    _sale(customer, shipment, date="2026-05-01")
    _sale(customer, shipment, date="2026-07-17")

    resp = admin_client.get("/reports/", {"from": "2026-07-01", "to": "2026-07-31"})
    assert resp.status_code == 200
    # only the in-range sale (kg=100, price=2, cost=1) contributes profit = 100.00
    assert resp.context["profit_total"] == Decimal("100.00")


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/reports/").status_code == 403
