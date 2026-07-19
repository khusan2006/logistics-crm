from decimal import Decimal

from crm.models import Contract, Partner


def _contract(**kw):
    partner = kw.pop("partner", None) or Partner.objects.create(name="Pars", phone="1", city="Tehron")
    defaults = dict(partner=partner, brand="LLDPE 209AA", kg=Decimal("50000"),
                    price=Decimal("0.96"), created="2026-07-01", deadline="2026-07-28")
    defaults.update(kw)
    return Contract.objects.create(**defaults)


def test_total_value(db):
    c = _contract()
    assert c.total_value == Decimal("48000.00")
    # nothing shipped yet → nothing owed (debt accrues per shipped truck)
    assert c.shipped_value == Decimal("0")
    assert c.debt == Decimal("0")
    assert c.remaining_kg == Decimal("50000")


def test_create_via_view(admin_client, admin_user):
    p = Partner.objects.create(name="Arya", phone="1", city="Shiroz")
    resp = admin_client.post("/contracts/new/", {
        "partner": p.pk, "brand": "HDPE 7000F", "kg": "30000", "price": "1.05",
        "created": "2026-07-04", "deadline": "2026-08-05", "note": "",
    })
    assert resp.status_code == 302
    c = Contract.objects.get(brand="HDPE 7000F")
    assert c.created_by == admin_user


def test_deadline_before_created_rejected(admin_client):
    p = Partner.objects.create(name="X", phone="1", city="Y")
    resp = admin_client.post("/contracts/new/", {
        "partner": p.pk, "brand": "B", "kg": "10", "price": "1",
        "created": "2026-07-10", "deadline": "2026-07-01", "note": "",
    })
    assert resp.status_code == 200 and not Contract.objects.exists()


def test_create_contract_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/contracts/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_contract_modal_post_valid_returns_204_with_redirect(admin_client):
    p = Partner.objects.create(name="Zamin", phone="1", city="Buxoro")
    resp = admin_client.post(
        "/contracts/new/",
        {
            "partner": p.pk, "brand": "LDPE 2100TN00", "kg": "20000", "price": "1.10",
            "created": "2026-07-05", "deadline": "2026-08-01", "note": "",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/contracts/"
    assert Contract.objects.filter(brand="LDPE 2100TN00").exists()


def test_create_contract_modal_post_invalid_returns_422(admin_client):
    p = Partner.objects.create(name="Zamin", phone="1", city="Buxoro")
    resp = admin_client.post(
        "/contracts/new/",
        {
            "partner": p.pk, "brand": "B", "kg": "10", "price": "1",
            "created": "2026-07-10", "deadline": "2026-07-01", "note": "",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html
