from decimal import Decimal

from crm.models import Contract, Partner, SupplierPayment


def _contract(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    return Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("1000"),
                                   price=Decimal("1.00"), created="2026-07-01",
                                   deadline="2026-07-28")


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
