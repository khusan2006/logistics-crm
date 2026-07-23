from datetime import date, timedelta
from decimal import Decimal

import pytest

from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentDelay, ShipmentLine, ShipmentStatus,
)


@pytest.fixture
def late_shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.first(), eta=date.today() - timedelta(days=2))
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal("500"))
    return _ship_obj


def test_extend_requires_reason(translator_client, late_shipment):
    new_eta = (date.today() + timedelta(days=5)).isoformat()
    resp = translator_client.post(f"/shipments/{late_shipment.pk}/extend/",
                                  {"new_eta": new_eta, "reason": ""})
    assert resp.status_code == 200 and not ShipmentDelay.objects.exists()


def test_extend_saves_history_and_updates_eta(translator_client, late_shipment):
    old_eta = late_shipment.eta
    new_eta = date.today() + timedelta(days=5)
    resp = translator_client.post(f"/shipments/{late_shipment.pk}/extend/",
                                  {"new_eta": new_eta.isoformat(), "reason": "Chegarada navbat"})
    assert resp.status_code == 302
    late_shipment.refresh_from_db()
    assert late_shipment.eta == new_eta and not late_shipment.is_overdue
    delay = late_shipment.delays.get()
    assert delay.old_eta == old_eta and delay.reason == "Chegarada navbat"


def test_detail_shows_history(admin_client, late_shipment):
    admin_client.post(f"/shipments/{late_shipment.pk}/extend/",
                      {"new_eta": (date.today() + timedelta(days=3)).isoformat(),
                       "reason": "Bojxona tekshiruvi"})
    html = admin_client.get(f"/shipments/{late_shipment.pk}/").content.decode()
    assert "Bojxona tekshiruvi" in html


def test_extend_modal_get_returns_partial(admin_client, late_shipment):
    resp = admin_client.get(f"/shipments/{late_shipment.pk}/extend/",
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_extend_modal_post_valid_returns_204_no_redirect(admin_client, late_shipment):
    # form_reload (not form_success): the extend modal is often opened from
    # shipment_detail, so a successful AJAX submit reloads whatever page opened
    # it in place, rather than bouncing to the list via X-Redirect.
    new_eta = (date.today() + timedelta(days=5)).isoformat()
    resp = admin_client.post(
        f"/shipments/{late_shipment.pk}/extend/",
        {"new_eta": new_eta, "reason": "Bojxona tekshiruvi"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert "X-Redirect" not in resp


def test_translator_detail_has_no_money(translator_client, late_shipment):
    # Task 10: expenses/landed cost are admin-only. Translators must see none of it.
    html = translator_client.get(f"/shipments/{late_shipment.pk}/").content.decode()
    content = html.split('class="content"', 1)[1].split("</main>", 1)[0]
    lowered = content.lower()
    for term in ("qarz", "debt", "narx", "to'lov", "price", "expense", "xarajat"):
        assert term not in lowered


def test_both_roles_can_view_detail(translator_client, admin_client, late_shipment):
    assert translator_client.get(f"/shipments/{late_shipment.pk}/").status_code == 200
    assert admin_client.get(f"/shipments/{late_shipment.pk}/").status_code == 200
