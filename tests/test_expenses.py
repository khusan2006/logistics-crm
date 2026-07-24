from decimal import Decimal

import pytest

from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus,
)


def rows(*entries, shipment, date="2026-07-10"):
    """POST payload for the xarajat modal: one shared sana plus the rows."""
    data = {"shipment": shipment.pk, "date": date,
            "form-TOTAL_FORMS": str(len(entries)), "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000"}
    defaults = {"category": "customs", "currency": "usd",
                "amount": "0", "exchange_rate": "", "method": "cash", "note": ""}
    for i, entry in enumerate(entries):
        for key, value in {**defaults, **entry}.items():
            data[f"form-{i}-{key}"] = str(value)
    return data


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal("20000"), price=Decimal("1.00"))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal("10000"))
    return _ship_obj


def test_landed_cost(admin_client, shipment):
    admin_client.post("/expenses/new/", rows(
        {"amount": "1200"}, {"amount": "800"}, shipment=shipment))
    assert shipment.expenses_total == Decimal("2000.00")
    # 1.00 + 2000/10000 = 1.20 per kg
    assert shipment.lines.first().landed_cost_per_kg == Decimal("1.2000")


def test_no_expenses_landed_cost_is_contract_price(shipment):
    assert shipment.lines.first().landed_cost_per_kg == Decimal("1.0000")


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
        rows({"amount": "500"}, shipment=shipment),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    # no X-Redirect: the modal reloads whichever page it was opened from
    # (loads list or load detail), so inline expense-adding stays in place
    assert "X-Redirect" not in resp
    assert ShipmentExpense.objects.filter(shipment=shipment).exists()


def test_uzs_converted_to_usd(admin_client, shipment):
    admin_client.post("/expenses/new/", rows(
        {"category": "transport", "currency": "uzs", "amount": "1265000",
         "exchange_rate": "12650"}, shipment=shipment))
    e = ShipmentExpense.objects.get()
    assert e.amount == Decimal("100.00")
    assert e.amount_original == Decimal("1265000")
    assert e.exchange_rate == Decimal("12650")


def test_several_xarajatlar_save_from_one_modal(admin_client, shipment):
    """Bitta yukka bir nechta xarajat — transport, bojxona — bir modalda."""
    resp = admin_client.post("/expenses/new/", rows(
        {"category": "transport", "amount": "500"},
        {"category": "customs", "amount": "120", "note": "bojxona"},
        shipment=shipment))
    assert resp.status_code == 302
    assert ShipmentExpense.objects.count() == 2
    assert shipment.expenses_total == Decimal("620.00")


def test_an_empty_xarajat_modal_is_rejected(admin_client, shipment):
    resp = admin_client.post("/expenses/new/", rows({"amount": ""}, shipment=shipment))
    assert resp.status_code == 200 and not ShipmentExpense.objects.exists()


def test_the_shared_sana_lands_on_every_row(admin_client, shipment):
    """Sana bir marta so'raladi va barcha qatorlarga yoziladi."""
    admin_client.post("/expenses/new/", rows(
        {"category": "transport", "amount": "500"},
        {"category": "customs", "amount": "120"},
        shipment=shipment, date="2026-08-02"))
    assert {e.date.isoformat() for e in ShipmentExpense.objects.all()} == {"2026-08-02"}


def test_the_sana_is_required(admin_client, shipment):
    resp = admin_client.post("/expenses/new/", rows(
        {"amount": "500"}, shipment=shipment, date=""))
    assert resp.status_code == 200 and not ShipmentExpense.objects.exists()


def test_each_row_converts_its_own_currency(admin_client, shipment):
    """Har qator o'z valyutasi bo'yicha alohida hisoblanadi."""
    admin_client.post("/expenses/new/", rows(
        {"category": "transport", "amount": "100"},
        {"category": "customs", "currency": "uzs", "amount": "1265000",
         "exchange_rate": "12650"},
        shipment=shipment))
    amounts = sorted(e.amount for e in ShipmentExpense.objects.all())
    assert amounts == [Decimal("100.00"), Decimal("100.00")]   # 1 265 000 / 12 650
