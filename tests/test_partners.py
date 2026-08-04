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


def test_partner_phone_accepts_uz_ir_and_tr(db):
    from crm.forms import PartnerForm
    for phone in ["+998 90 123 45 67", "+98 912 345 6789", "+90 532 123 45 67"]:
        f = PartnerForm({"name": "X", "phone": phone, "city": "", "note": ""})
        assert f.is_valid(), (phone, f.errors)


def test_partner_phone_rejects_other_countries(db):
    from crm.forms import PartnerForm
    f = PartnerForm({"name": "X", "phone": "+82343905395034355", "city": "", "note": ""})
    assert not f.is_valid() and "phone" in f.errors


# --- qolgan to'lov, per currency -------------------------------------------

def test_a_partner_row_keeps_its_two_currencies_apart(admin_client, db):
    """One hamkor, a dollar kelishuv and a so'm kelishuv. The row carries both
    figures side by side: they are two different debts, and adding them would need a
    kurs neither side agreed on."""
    from decimal import Decimal

    from conftest import make_contract
    from crm.models import Currency
    from crm.templatetags.crm_extras import som, usd

    partner = Partner.objects.create(name="Sobir", phone="1", city="Tehron")
    make_contract(partner=partner, kg="1000", price="2.00")          # 2 000 $
    make_contract(partner=partner, kg="1000", price="1.00",
                  price_uzs="12650", currency=Currency.UZS)          # 12 650 000 so'm

    row = admin_client.get("/partners/").context["page"].object_list[0]
    assert row.payable == [(Currency.USD, Decimal("2000.00")),
                           (Currency.UZS, Decimal("12650000.00"))]

    html = admin_client.get("/partners/").content.decode()
    from django.utils.html import escape
    assert escape(usd(Decimal("2000.00"))) in html
    assert escape(som(Decimal("12650000.00"))) in html


def test_a_partner_with_nothing_outstanding_reads_as_qarzsiz(admin_client, db):
    from decimal import Decimal

    from conftest import make_contract
    from crm.models import SupplierPayment

    partner = Partner.objects.create(name="Arya", phone="1", city="Shiroz")
    contract = make_contract(partner=partner, kg="1000", price="2.00")
    SupplierPayment.objects.create(contract=contract, date="2026-07-23",
                                   amount=Decimal("2000"), method="cash")

    resp = admin_client.get("/partners/")
    assert resp.context["page"].object_list[0].payable == []
    assert "Qarzsiz" in resp.content.decode()
