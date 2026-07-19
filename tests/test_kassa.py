from decimal import Decimal

from crm.models import (
    Contract, Customer, CustomerPayment, Partner, Shipment, ShipmentExpense, ShipmentStatus, SupplierPayment,
)


def _contract(partner_name="Pars"):
    partner = Partner.objects.create(name=partner_name, phone="1", city="T")
    return Contract.objects.create(
        partner=partner, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"),
        created="2026-07-01", deadline="2026-08-01",
    )


def _arrived_shipment(contract):
    return Shipment.objects.create(
        contract=contract, kg=Decimal("500"), status=ShipmentStatus.arrival(),
        sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16",
        transport="01A111AA", container="MSCU-1",
    )


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def test_cash_balance_nets_in_and_out(admin_client, db):
    contract = _contract()
    shipment = _arrived_shipment(contract)
    customer = _customer()

    CustomerPayment.objects.create(
        customer=customer, date="2026-07-10", amount=Decimal("500.00"), method="cash",
    )
    SupplierPayment.objects.create(
        contract=contract, date="2026-07-11", amount=Decimal("200.00"), method="cash",
    )
    ShipmentExpense.objects.create(
        shipment=shipment, date="2026-07-12", category="transport", amount=Decimal("100.00"), method="cash",
    )

    resp = admin_client.get("/kassa/")
    assert resp.status_code == 200
    balances = resp.context["balances"]
    assert balances["cash"]["balance"] == Decimal("200.00")
    assert resp.context["net_total"] == Decimal("200.00")

    html = resp.content.decode()
    assert "200.00" in html


def test_bank_payment_shows_under_bank_and_cash_unaffected(admin_client, db):
    contract = _contract()
    customer = _customer()

    CustomerPayment.objects.create(
        customer=customer, date="2026-07-10", amount=Decimal("300.00"), method="transfer",
    )

    resp = admin_client.get("/kassa/")
    balances = resp.context["balances"]
    assert balances["transfer"]["in"] == Decimal("300.00")
    assert balances["transfer"]["balance"] == Decimal("300.00")
    assert balances["cash"]["balance"] == Decimal("0.00")


def test_date_filter_excludes_out_of_range_payment(admin_client, db):
    customer = _customer()

    CustomerPayment.objects.create(
        customer=customer, date="2026-05-01", amount=Decimal("150.00"), method="cash",
    )
    CustomerPayment.objects.create(
        customer=customer, date="2026-07-10", amount=Decimal("500.00"), method="cash",
    )

    resp = admin_client.get("/kassa/", {"from": "2026-07-01", "to": "2026-07-31"})
    balances = resp.context["balances"]
    assert balances["cash"]["in"] == Decimal("500.00")

    feed_dates = [row["date"].isoformat() for row in resp.context["feed"].object_list]
    assert "2026-05-01" not in feed_dates
    assert "2026-07-10" in feed_dates


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/kassa/").status_code == 403
