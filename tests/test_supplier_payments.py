from decimal import Decimal

from crm.models import Contract, Partner, Shipment, ShipmentStatus, SupplierPayment


def _contract(db, ship_kg="1000"):
    """Contract with (by default) its full kg already on a truck — the payable
    to the partner accrues per shipped truck, so tests that pay need shipped value."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    c = Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("1000"),
                                price=Decimal("1.00"), created="2026-07-01",
                                deadline="2026-07-28")
    if ship_kg:
        Shipment.objects.create(contract=c, kg=Decimal(ship_kg),
                                status=ShipmentStatus.objects.first())
    return c


def test_payment_blocked_before_anything_ships(admin_client, db):
    """Debt accrues per shipped truck — with no trucks sent there is nothing owed,
    so a payment (prepayment) is rejected."""
    c = _contract(db, ship_kg=None)
    assert c.debt == Decimal("0")
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "100",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_debt_accrues_per_truck_at_its_own_price(admin_client, db):
    """Two trucks under one kelishuv, one at its own price: owed = Σ kg × unit
    price, not the contract total."""
    c = _contract(db, ship_kg="400")                       # 400 kg @ 1.00 (contract)
    Shipment.objects.create(contract=c, kg=Decimal("100"), price=Decimal("2.00"),
                            status=ShipmentStatus.objects.first())
    assert c.shipped_value == Decimal("600.00")            # 400 + 200
    assert c.debt == Decimal("600.00")
    resp = admin_client.post("/supplier-payments/new/", {  # 601 > 600 → blocked
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "601",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_payment_reduces_debt(admin_client, db):
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "400",
        "exchange_rate": "", "method": "transfer", "note": "",
    })
    assert resp.status_code == 302
    assert c.debt == Decimal("600.00")


def test_overpay_blocked(admin_client, db):
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "1500",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_uzs_converted_to_usd(admin_client, db):
    c = _contract(db)
    admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "uzs", "amount": "1265000",
        "exchange_rate": "12650", "method": "cash", "note": "",
    })
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("100.00")
    assert p.amount_original == Decimal("1265000")
    assert p.exchange_rate == Decimal("12650")


def test_edit_excludes_own_amount_from_debt_check(admin_client, db):
    c = _contract(db)
    p = SupplierPayment.objects.create(contract=c, date="2026-07-02", amount=Decimal("1000"),
                                       amount_original=Decimal("1000"), method="cash")
    resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "900",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.amount == Decimal("900.00")


def test_create_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/supplier-payments/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_modal_post_valid_returns_204_with_redirect(admin_client, db):
    c = _contract(db)
    resp = admin_client.post(
        "/supplier-payments/new/",
        {
            "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "400",
            "exchange_rate": "", "method": "transfer", "note": "",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/supplier-payments/"
    assert SupplierPayment.objects.filter(contract=c).exists()


def test_create_modal_post_invalid_returns_422(admin_client, db):
    c = _contract(db)
    resp = admin_client.post(
        "/supplier-payments/new/",
        {
            "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "1500",
            "exchange_rate": "", "method": "cash", "note": "",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html
    assert not SupplierPayment.objects.exists()


def test_create_preselects_contract_from_query_param(admin_client, db):
    c = _contract(db)
    resp = admin_client.get(f"/supplier-payments/new/?contract={c.pk}")
    assert resp.status_code == 200
    assert resp.context["form"].initial.get("contract") == c.pk
