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


# ── Hamkor tarixi ────────────────────────────────────────────────────────────

def _history(admin_client, partner):
    """The Hamkor tarixi rows, as (sana, voqea, tafsilot, summa)."""
    import re

    html = admin_client.get(f"/partners/{partner.pk}/").content.decode()
    assert "Hamkor tarixi" in html, "tarix bo'limi yo'q"
    section = html.split("Hamkor tarixi")[1]
    rows = re.findall(r"<tr>(.*?)</tr>", section, re.S)[1:]   # drop the header row

    def text(cell):
        # ASCII whitespace only: the NBSP thousands separator is the convention
        # being asserted, so it has to survive being read back out.
        return re.sub(r"[ \t\r\n]+", " ", re.sub(r"<[^>]+>", "", cell)).strip()

    return [tuple(text(c) for c in re.findall(r"<td.*?>(.*?)</td>", row, re.S))
            for row in rows]


def _book(kg="24000", price="1.00"):
    """A hamkor with a kelishuv, a yuk against it and a to'lov on it."""
    from decimal import Decimal

    from crm.models import (Contract, ContractLine, Shipment, ShipmentLine,
                            ShipmentStatus, SupplierPayment)

    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    line = ContractLine.objects.create(contract=contract, brand="LLDPE",
                                       kg=Decimal(kg), price=Decimal(price))
    shipment = Shipment.objects.create(contract=contract,
                                       status=ShipmentStatus.arrival(),
                                       sent="2026-07-05", eta="2026-07-15")
    ShipmentLine.objects.create(shipment=shipment, contract_line=line, kg=Decimal(kg))
    SupplierPayment.objects.create(contract=contract, date="2026-07-10",
                                   amount=Decimal("10000"),
                                   amount_uzs=Decimal("120000000"), method="cash")
    return partner


def test_the_history_carries_kelishuv_tolov_and_yuk_newest_first(admin_client, db):
    from datetime import datetime

    partner = _book()
    rows = _history(admin_client, partner)
    assert [r[1] for r in rows] == ["To&#x27;lov", "Yuk", "Kelishuv"]
    dates = [datetime.strptime(r[0], "%d.%m.%Y").date() for r in rows]
    assert dates == sorted(dates, reverse=True)


def test_a_yuk_shows_kg_and_no_figure(admin_client, db):
    """A yuk carries goods, not money — a sum there would be inventing one."""
    partner = _book(kg="24000")
    yuk = next(r for r in _history(admin_client, partner) if r[1] == "Yuk")
    assert "24 000 kg" in yuk[2].replace("\xa0", " ")
    assert yuk[3] == "—"


def test_the_tolov_row_is_what_the_hamkor_received(admin_client, db):
    """So the page reconciles with the qolgan to'lov printed above it: kelishuv
    value less the to'lov rows IS that figure. The vositachi cut and the bank's
    foiz are money spent on the transfer, not paid to the hamkor, so they are named
    in the detail instead of inflating the column."""
    from decimal import Decimal

    from crm.models import Contract, ContractLine, SupplierPayment, payable_by_currency

    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("10000"), price=Decimal("1.00"))
    SupplierPayment.objects.create(contract=contract, date="2026-07-10",
                                   amount=Decimal("4000"),
                                   amount_uzs=Decimal("48000000"), method="cash",
                                   commission_percent=Decimal("2"))
    rows = {r[1]: r for r in _history(admin_client, partner)}
    assert rows["To&#x27;lov"][3] == "$4\xa0000"                 # what they received
    assert "ustiga $80 xarajat" in rows["To&#x27;lov"][2]        # the cut, named
    # 10 000 agreed − 4 000 received = 6 000 still owed, exactly as the header says
    assert payable_by_currency(partner.contracts.all()) == [("usd", Decimal("6000.00"))]


def test_a_hamkor_with_no_dealings_gets_an_empty_history(admin_client, db):
    partner = Partner.objects.create(name="Yangi", phone="", city="")
    html = admin_client.get(f"/partners/{partner.pk}/").content.decode()
    assert "Bu hamkor bilan hali hech qanday amaliyot bo'lmagan" in html


def test_the_hamkorlar_list_links_the_name_to_that_hamkor_page(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    html = admin_client.get("/partners/").content.decode()
    assert f'href="/partners/{partner.pk}/">{partner.name}</a>' in html


def test_translator_cannot_open_a_hamkor_page(translator_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    assert translator_client.get(f"/partners/{partner.pk}/").status_code == 403
