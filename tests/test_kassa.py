from decimal import Decimal

from crm.models import (
    Contract, ContractLine, Customer, CustomerPayment, Partner, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus, SupplierPayment,
)


def _contract(partner_name="Pars"):
    partner = Partner.objects.create(name=partner_name, phone="1", city="T")
    _contract_obj = Contract.objects.create(partner=partner, created="2026-07-01")
    _contract_obj_line = ContractLine.objects.create(
        contract=_contract_obj, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"))
    return _contract_obj


def _arrived_shipment(contract):
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal("500"))
    return _ship_obj


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

    income_dates = [p.date.isoformat() for p in resp.context["income_rows"]]
    assert "2026-05-01" not in income_dates
    assert "2026-07-10" in income_dates


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/kassa/").status_code == 403


def test_kassa_shows_partner_payables_from_shipped_trucks(admin_client, db):
    """The Kassa surfaces what we owe hamkorlar right now: Σ per contract of
    shipped value − paid, grouped by partner."""
    from crm.models import Contract, Partner, Shipment, ShipmentStatus, SupplierPayment

    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    c = Contract.objects.create(partner=partner, created="2026-07-01")
    c_line = ContractLine.objects.create(
        contract=c, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1.00"))
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("600"))   # owe 600
    SupplierPayment.objects.create(contract=c, date="2026-07-02",
                                   amount=Decimal("250"), amount_original=Decimal("250"),
                                   method="cash")                    # paid 250
    resp = admin_client.get("/kassa/")
    assert resp.context["payable_total"] == Decimal("350.00")
    debts = {p.name: d for p, d in resp.context["partner_debts"]}
    assert debts == {"Pars": Decimal("350.00")}
    assert "Hamkorlarga qarzimiz" in resp.content.decode()


def test_kassa_kirim_chiqim_ledgers_and_cash_hero(admin_client, db):
    """Client-crm style: an all-time Kassadagi pul hero plus separate Kirim and
    Chiqim ledgers (customer payments in; hamkor payments + yuk expenses out)."""
    contract = _contract()
    shipment = _arrived_shipment(contract)
    customer = _customer()
    CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                   amount=Decimal("500.00"), method="cash")
    SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                   amount=Decimal("200.00"), method="cash")
    ShipmentExpense.objects.create(shipment=shipment, date="2026-07-12",
                                   category="transport", amount=Decimal("100.00"),
                                   method="cash")
    # the hero is all-time even when the filter excludes some rows
    resp = admin_client.get("/kassa/", {"from": "2026-07-11", "to": "2026-07-12"})
    assert resp.context["cash_total"] == Decimal("200.00")   # 500 - 200 - 100
    assert [p.amount for p in resp.context["income_rows"]] == []      # 07-10 outside range
    kinds = [(r["kind"], r["amount"]) for r in resp.context["outflow_rows"]]
    assert kinds == [("expense", Decimal("100.00")), ("supplier", Decimal("200.00"))]
    html = resp.content.decode()
    assert "Kirim" in html and "Chiqim" in html and "Kassadagi pul" in html
