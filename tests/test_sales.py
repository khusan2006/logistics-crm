from decimal import Decimal

from crm.models import AuditLog, Contract, Customer, Partner, Sale, Shipment, ShipmentExpense, ShipmentStatus


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="10000", brand="LLDPE", contract_price="1.00", expense="2000.00"):
    """An arrived 10,000 kg lot @ contract price $1.00/kg + $2,000 expenses
    => landed cost = 1.00 + 2000/10000 = $1.20/kg."""
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(
        partner=partner, brand=brand, kg=Decimal(kg), price=Decimal(contract_price),
        created="2026-07-01", deadline="2026-08-01",
    )
    shipment = Shipment.objects.create(
        contract=contract, kg=Decimal(kg), status=ShipmentStatus.arrival(),
        sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16",
        transport="01A111AA", container="MSCU-1",
    )
    if expense:
        ShipmentExpense.objects.create(shipment=shipment, amount=Decimal(expense), date="2026-07-16")
    return shipment


def _non_arrived_lot(kg="1000", brand="HDPE"):
    partner = Partner.objects.create(name="Basir", phone="1", city="T")
    contract = Contract.objects.create(
        partner=partner, brand=brand, kg=Decimal(kg), price=Decimal("1.00"),
        created="2026-07-01", deadline="2026-08-01",
    )
    return Shipment.objects.create(
        contract=contract, kg=Decimal(kg), status=ShipmentStatus.objects.first(),
        sent="2026-07-05", eta="2026-08-01",
    )


def test_sale_snapshots_cost_and_computes_total_profit(admin_client, db):
    lot = _lot()
    assert lot.landed_cost_per_kg == Decimal("1.2000")
    customer = _customer()

    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "4000",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale = Sale.objects.get(shipment=lot)
    assert sale.kg == Decimal("4000")
    assert sale.cost_price == Decimal("1.2000")
    assert sale.total == Decimal("6400.00")
    assert sale.profit == Decimal("1600.00")
    assert AuditLog.objects.filter(target_type="Sotuv").exists()

    lot.refresh_from_db()
    assert lot.available_kg == Decimal("6000")

    customer.refresh_from_db()
    assert customer.balance == Decimal("6400.00")


def test_cost_price_stays_frozen_after_later_expense_change(admin_client, db):
    lot = _lot(expense="2000.00")
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "4000",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(shipment=lot)
    assert sale.cost_price == Decimal("1.2000")

    # Add a big new expense — landed cost changes, but the snapshot must not.
    ShipmentExpense.objects.create(shipment=lot, amount=Decimal("8000.00"), date="2026-07-19")
    lot.refresh_from_db()
    assert lot.landed_cost_per_kg != Decimal("1.2000")

    sale.refresh_from_db()
    assert sale.cost_price == Decimal("1.2000")


def test_selling_more_than_available_kg_rejected(admin_client, db):
    lot = _lot(kg="1000", expense="0")
    customer = _customer()
    resp = admin_client.post("/sales/new/", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "1500",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 200
    assert not Sale.objects.filter(shipment=lot).exists()


def test_selling_from_non_arrived_shipment_rejected(admin_client, db):
    lot = _non_arrived_lot()
    customer = _customer()
    resp = admin_client.post("/sales/new/", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "100",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 200
    assert not Sale.objects.filter(shipment=lot).exists()


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/sales/").status_code == 403
    assert translator_client.get("/sales/new/").status_code == 403


def test_list_and_search(admin_client, db):
    lot = _lot(brand="LLDPE-Findme")
    customer = _customer(name="Findable Customer")
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "500",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    html = admin_client.get("/sales/?q=Findable").content.decode()
    assert "Findable Customer" in html


def test_sale_create_modal_get_returns_partial(admin_client, db):
    lot = _lot()
    resp = admin_client.get(f"/sales/new/?lot={lot.pk}", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_sale_create_modal_post_valid_returns_204_with_redirect(admin_client, db):
    lot = _lot()
    customer = _customer()
    resp = admin_client.post(
        "/sales/new/",
        {"customer": customer.pk, "shipment": lot.pk, "kg": "100",
         "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/sales/"
    assert Sale.objects.filter(shipment=lot).exists()


def test_sale_create_modal_post_invalid_returns_422(admin_client, db):
    lot = _lot()
    customer = _customer()
    resp = admin_client.post(
        "/sales/new/",
        {"customer": customer.pk, "shipment": lot.pk, "kg": "99999",
         "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html


def test_sale_edit_re_snapshots_cost_from_current_shipment(admin_client, db):
    lot = _lot(expense="2000.00")
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "1000",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(shipment=lot)
    assert sale.cost_price == Decimal("1.2000")

    ShipmentExpense.objects.create(shipment=lot, amount=Decimal("8000.00"), date="2026-07-19")
    lot.refresh_from_db()
    new_landed = lot.landed_cost_per_kg
    assert new_landed != Decimal("1.2000")

    resp = admin_client.post(f"/sales/{sale.pk}/edit/", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "1000",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale.refresh_from_db()
    assert sale.cost_price == new_landed


def test_sale_delete(admin_client, db):
    lot = _lot()
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "500",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(shipment=lot)
    resp = admin_client.post(f"/sales/{sale.pk}/delete/")
    assert resp.status_code == 302
    assert not Sale.objects.filter(pk=sale.pk).exists()


def test_sale_detail(admin_client, db):
    lot = _lot()
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "shipment": lot.pk, "kg": "500",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(shipment=lot)
    resp = admin_client.get(f"/sales/{sale.pk}/")
    assert resp.status_code == 200


def test_ombor_sotish_button_present_for_available_lot(admin_client, db):
    lot = _lot()
    html = admin_client.get("/ombor/").content.decode()
    assert f"/sales/new/?lot={lot.pk}" in html
