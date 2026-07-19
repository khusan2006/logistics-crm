from decimal import Decimal

import pytest

from crm.models import Contract, Partner, Shipment, ShipmentExpense, ShipmentStatus


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("20000"),
                                       price=Decimal("1.00"), created="2026-07-01",
                                       deadline="2026-08-01")
    return Shipment.objects.create(contract=contract, kg=Decimal("10000"),
                                   status=ShipmentStatus.objects.first())


def test_landed_cost(admin_client, shipment):
    for amount in ("1200", "800"):
        admin_client.post("/expenses/new/?shipment=%d" % shipment.pk, {
            "shipment": shipment.pk, "date": "2026-07-10", "category": "customs",
            "currency": "usd", "amount": amount, "exchange_rate": "",
            "method": "cash", "note": "",
        })
    assert shipment.expenses_total == Decimal("2000.00")
    # 1.00 + 2000/10000 = 1.20 per kg
    assert shipment.landed_cost_per_kg == Decimal("1.2000")


def test_no_expenses_landed_cost_is_contract_price(shipment):
    assert shipment.landed_cost_per_kg == Decimal("1.0000")


def test_translator_forbidden(translator_client, shipment):
    resp = translator_client.get("/expenses/new/?shipment=%d" % shipment.pk)
    assert resp.status_code == 403


def test_create_modal_get_returns_partial(admin_client, shipment):
    resp = admin_client.get(
        "/expenses/new/?shipment=%d" % shipment.pk, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_modal_post_valid_returns_204_with_redirect(admin_client, shipment):
    resp = admin_client.post(
        "/expenses/new/?shipment=%d" % shipment.pk,
        {
            "shipment": shipment.pk, "date": "2026-07-10", "category": "customs",
            "currency": "usd", "amount": "500", "exchange_rate": "",
            "method": "cash", "note": "",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    # no X-Redirect: the modal reloads whichever page it was opened from
    # (loads list or load detail), so inline expense-adding stays in place
    assert "X-Redirect" not in resp
    assert ShipmentExpense.objects.filter(shipment=shipment).exists()


def test_uzs_converted_to_usd(admin_client, shipment):
    admin_client.post("/expenses/new/?shipment=%d" % shipment.pk, {
        "shipment": shipment.pk, "date": "2026-07-10", "category": "transport",
        "currency": "uzs", "amount": "1265000", "exchange_rate": "12650",
        "method": "cash", "note": "",
    })
    e = ShipmentExpense.objects.get()
    assert e.amount == Decimal("100.00")
    assert e.amount_original == Decimal("1265000")
    assert e.exchange_rate == Decimal("12650")
