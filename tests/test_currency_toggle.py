"""P3.7: the money-entry modals expose data-money-currency/-rate/-amount hooks
so the base.html JS enhancer can show/hide the exchange-rate field and render
a live USD preview. This only asserts the server-side widget wiring; the
actual show/hide + live preview behavior must be eyeballed in a browser."""
from decimal import Decimal

from crm.models import Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus


def _contract(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    _contract_obj = Contract.objects.create(partner=partner, created="2026-07-01", deadline="2026-07-28")
    _contract_obj_line = ContractLine.objects.create(
        contract=_contract_obj, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1.00"))
    return _contract_obj


def test_supplier_payment_modal_has_currency_toggle_hooks(admin_client, db):
    resp = admin_client.get("/supplier-payments/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "data-money-currency" in html
    assert "data-money-rate" in html
    assert "data-money-amount" in html


def test_customer_payment_modal_has_currency_toggle_hooks(admin_client, db):
    resp = admin_client.get("/customer-payments/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "data-money-currency" in html
    assert "data-money-rate" in html
    assert "data-money-amount" in html


def test_expense_modal_has_currency_toggle_hooks(admin_client, db):
    c = _contract(db)
    shipment = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    shipment_line = ShipmentLine.objects.create(
        shipment=shipment, contract_line=c.lines.first(), kg=Decimal("500"))
    resp = admin_client.get(
        "/expenses/new/?shipment=%d" % shipment.pk, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "data-money-currency" in html
    assert "data-money-rate" in html
    assert "data-money-amount" in html
