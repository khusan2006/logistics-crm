from decimal import Decimal

from crm.models import AuditLog, Contract, Customer, Partner, Return, Sale, Shipment, ShipmentExpense, ShipmentStatus


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


def _sale(admin_client, lot, customer, kg="4000", price="1.60"):
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "shipment": lot.pk, "kg": kg,
        "price": price, "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    return Sale.objects.get(shipment=lot, kg=Decimal(kg))


def test_return_credits_debt_and_restocks_lot(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")

    assert sale.net_total == Decimal("6400.00")
    assert sale.remaining == Decimal("6400.00")
    customer.refresh_from_db()
    assert customer.balance == Decimal("6400.00")
    lot.refresh_from_db()
    assert lot.available_kg == Decimal("6000")
    assert sale.profit == Decimal("1600.00")  # (1.60 − 1.20) × 4000

    resp = admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "1000", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": "",
    })
    assert resp.status_code == 302

    assert Return.objects.filter(sale=sale).exists()
    ret = Return.objects.get(sale=sale)
    assert ret.amount == Decimal("1600.00")

    sale.refresh_from_db()
    assert sale.net_total == Decimal("4800.00")
    assert sale.remaining == Decimal("4800.00")

    customer.refresh_from_db()
    assert customer.balance == Decimal("4800.00")

    lot.refresh_from_db()
    assert lot.returned_kg == Decimal("1000")
    assert lot.available_kg == Decimal("7000")
    assert sale.profit == Decimal("1200.00")  # 1600 − (1.60 − 1.20) × 1000 restocked

    assert AuditLog.objects.filter(action=AuditLog.Action.RETURN, target_type="Qaytarish").exists()


def test_return_without_restock_does_not_flow_kg_back(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    lot.refresh_from_db()
    available_before = lot.available_kg

    admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "1000", "price": "1.60", "date": "2026-07-19", "restock": "", "note": "",
    })

    sale.refresh_from_db()
    assert sale.net_total == Decimal("4800.00")

    lot.refresh_from_db()
    assert lot.returned_kg == Decimal("0")
    assert lot.available_kg == available_before  # no kg flowed back


def test_return_kg_cannot_exceed_sold_kg(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")

    resp = admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "1500", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": "",
    })
    assert resp.status_code == 200
    assert not Return.objects.filter(sale=sale).exists()


def test_return_kg_cannot_exceed_remaining_after_prior_return(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")

    resp1 = admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "600", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": "",
    })
    assert resp1.status_code == 302

    # only 400 kg left un-returned; asking for 500 must fail
    resp2 = admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "500", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": "",
    })
    assert resp2.status_code == 200
    assert Return.objects.filter(sale=sale).count() == 1


def test_return_delete(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "1000", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": "",
    })
    ret = Return.objects.get(sale=sale)

    resp = admin_client.post(f"/returns/{ret.pk}/delete/")
    assert resp.status_code == 302
    assert not Return.objects.filter(pk=ret.pk).exists()

    sale.refresh_from_db()
    assert sale.net_total == Decimal("6400.00")
    assert AuditLog.objects.filter(action=AuditLog.Action.DELETE, target_type="Qaytarish").exists()


def test_translator_forbidden(translator_client, admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    assert translator_client.get(f"/returns/new/?sale={sale.pk}").status_code == 403
    assert translator_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "100", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": "",
    }).status_code == 403
    # the delete path is admin-only too
    ret = Return.objects.create(sale=sale, kg=Decimal("100"), price=Decimal("1.60"), date="2026-07-19")
    assert translator_client.post(f"/returns/{ret.pk}/delete/").status_code == 403
    assert Return.objects.filter(pk=ret.pk).exists()


def test_return_create_modal_get_returns_partial(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    resp = admin_client.get(f"/returns/new/?sale={sale.pk}", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_return_create_modal_post_valid_returns_204_with_redirect(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    resp = admin_client.post(
        f"/returns/new/?sale={sale.pk}",
        {"kg": "100", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == f"/sales/{sale.pk}/"
    assert Return.objects.filter(sale=sale).exists()


def test_return_create_modal_post_invalid_returns_422(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    resp = admin_client.post(
        f"/returns/new/?sale={sale.pk}",
        {"kg": "99999", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html


def test_sale_detail_shows_returns_section(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    admin_client.post(f"/returns/new/?sale={sale.pk}", {
        "kg": "200", "price": "1.60", "date": "2026-07-19", "restock": "on", "note": "sifat",
    })
    html = admin_client.get(f"/sales/{sale.pk}/").content.decode()
    assert "320" in html or "320.00" in html  # 200 * 1.60
    assert "sifat" in html
