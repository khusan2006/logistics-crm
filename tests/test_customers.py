import pytest

from crm.models import AuditLog, Customer


def test_create_customer(admin_client):
    resp = admin_client.post("/customers/new/", {
        "name": "Alisher Mebel", "phone": "+998 90 111 22 33", "address": "Toshkent, Chilonzor", "note": "",
    })
    assert resp.status_code == 302
    assert Customer.objects.filter(name="Alisher Mebel").exists()
    assert AuditLog.objects.filter(target_type="Mijoz").exists()


def test_list_and_search(admin_client):
    Customer.objects.create(name="Alisher Mebel", phone="1", address="Toshkent")
    Customer.objects.create(name="Zarina Plast", phone="2", address="Samarqand")
    html = admin_client.get("/customers/?q=alisher").content.decode()
    assert "Alisher" in html and "Zarina Plast" not in html


def test_translator_forbidden(translator_client):
    assert translator_client.get("/customers/").status_code == 403


def test_create_customer_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/customers/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_customer_modal_post_valid_returns_204_with_redirect(admin_client):
    resp = admin_client.post(
        "/customers/new/",
        {"name": "Bekzod Savdo", "phone": "+998 91 222 33 44", "address": "Farg'ona", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/customers/"
    assert Customer.objects.filter(name="Bekzod Savdo").exists()


def test_create_customer_modal_post_invalid_returns_422(admin_client):
    resp = admin_client.post(
        "/customers/new/",
        {"name": "", "phone": "", "address": "", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html


def test_customer_quick_create(admin_client, db):
    import json
    from crm.models import Customer
    resp = admin_client.post("/customers/quick/", {"name": "Yangi Mijoz", "phone": "+998 90 111 22 33"})
    assert resp.status_code == 200
    d = json.loads(resp.content)
    assert d["created"] is True
    assert d["id"] == Customer.objects.get(name="Yangi Mijoz").pk


def test_customer_quick_create_reuses_same_name(admin_client, db):
    import json
    from crm.models import Customer
    existing = Customer.objects.create(name="Bor Mijoz", phone="1")
    d = json.loads(admin_client.post("/customers/quick/", {"name": "bor mijoz"}).content)
    assert d["created"] is False and d["id"] == existing.pk
    assert Customer.objects.filter(name__iexact="bor mijoz").count() == 1


def test_customer_quick_create_translator_forbidden(translator_client, db):
    assert translator_client.post("/customers/quick/", {"name": "X"}).status_code == 403


def test_sale_form_customer_has_quick_add_hook(db):
    from crm.forms import SaleForm
    html = str(SaleForm())
    assert "data-quick-add-url" in html and "/customers/quick/" in html
