import pytest

from crm.models import AuditLog, Partner


def test_create_partner(admin_client):
    resp = admin_client.post("/partners/new/", {
        "name": "Pars Polymer", "phone": "+98 912 440 1122", "city": "Tehron", "note": "",
    })
    assert resp.status_code == 302
    assert Partner.objects.filter(name="Pars Polymer").exists()
    assert AuditLog.objects.filter(target_type="Hamkor").exists()


def test_list_and_search(admin_client):
    Partner.objects.create(name="Arya Petrochem", phone="1", city="Shiroz")
    Partner.objects.create(name="Toshkent Polimer", phone="2", city="Toshkent")
    html = admin_client.get("/partners/?q=arya").content.decode()
    assert "Arya" in html and "Toshkent Polimer" not in html


def test_translator_forbidden(translator_client):
    assert translator_client.get("/partners/").status_code == 403


def test_create_partner_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/partners/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_partner_modal_post_valid_returns_204_with_redirect(admin_client):
    resp = admin_client.post(
        "/partners/new/",
        {"name": "Zamin Kimyo", "phone": "+998 90 123 45 67", "city": "Buxoro", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/partners/"
    assert Partner.objects.filter(name="Zamin Kimyo").exists()


def test_create_partner_modal_post_invalid_returns_422(admin_client):
    resp = admin_client.post(
        "/partners/new/",
        {"name": "", "phone": "", "city": "", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html
