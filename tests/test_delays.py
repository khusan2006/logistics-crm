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


def test_extend_requires_reason(admin_client, late_shipment):
    new_eta = (date.today() + timedelta(days=5)).isoformat()
    resp = admin_client.post(f"/shipments/{late_shipment.pk}/extend/",
                             {"new_eta": new_eta, "reason": ""})
    assert resp.status_code == 200 and not ShipmentDelay.objects.exists()


def test_a_tarjimon_cannot_move_the_muddat(translator_client, late_shipment):
    """Extending an ETA writes a delay reason onto the load's record and moves what
    counts as overdue. A tarjimon may keep the haydovchi and konteyner current and
    nothing else."""
    new_eta = (date.today() + timedelta(days=5)).isoformat()
    resp = translator_client.post(f"/shipments/{late_shipment.pk}/extend/",
                                  {"new_eta": new_eta, "reason": "Chegarada navbat"})
    assert resp.status_code == 403
    assert not ShipmentDelay.objects.exists()
    late_shipment.refresh_from_db()
    assert late_shipment.is_overdue


def test_extend_saves_history_and_updates_eta(admin_client, late_shipment):
    old_eta = late_shipment.eta
    new_eta = date.today() + timedelta(days=5)
    resp = admin_client.post(f"/shipments/{late_shipment.pk}/extend/",
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


# ── uzaytirishni tuzatish ──────────────────────────────────────────────────────
#
# Uzaytirishlar zanjir: har qatorning `old_eta` si oldingisining `new_eta` si, va
# yukning o'z eta si — oxirgi `new_eta`. Shuning uchun sanani faqat oxirgi qatorda
# siljitish mumkin; sabab esa har qatorda tuzatiladi.

def _extend(admin_client, shipment, days, reason):
    eta = date.today() + timedelta(days=days)
    admin_client.post(f"/shipments/{shipment.pk}/extend/",
                      {"new_eta": eta.isoformat(), "reason": reason})
    return eta


def test_editing_the_latest_delay_moves_the_eta_with_it(admin_client, late_shipment):
    _extend(admin_client, late_shipment, 5, "Chegarada navbat")
    delay = late_shipment.delays.get()
    fixed = date.today() + timedelta(days=9)

    resp = admin_client.post(f"/delays/{delay.pk}/edit/",
                             {"new_eta": fixed.isoformat(), "reason": "Yo'l yopiq"})
    assert resp.status_code == 302

    late_shipment.refresh_from_db()
    delay.refresh_from_db()
    assert late_shipment.eta == fixed          # yuk sanasi ergashdi
    assert delay.new_eta == fixed
    assert delay.reason == "Yo'l yopiq"


def test_an_older_delay_can_only_have_its_reason_fixed(admin_client, late_shipment):
    """Oradagi qatorning sanasi keyingisining boshlanish nuqtasi — uni siljitish
    zanjirni uzadi. Forma o'sha maydonni umuman ko'rsatmaydi."""
    first_eta = _extend(admin_client, late_shipment, 5, "Chegarada navbat")
    second_eta = _extend(admin_client, late_shipment, 9, "Yo'l yopiq")
    older = late_shipment.delays.last()
    assert older.new_eta == first_eta

    page = admin_client.get(f"/delays/{older.pk}/edit/").content.decode()
    assert 'name="reason"' in page
    assert 'name="new_eta"' not in page

    resp = admin_client.post(f"/delays/{older.pk}/edit/", {"reason": "Bojxonada"})
    assert resp.status_code == 302

    older.refresh_from_db()
    late_shipment.refresh_from_db()
    assert older.reason == "Bojxonada"
    assert older.new_eta == first_eta          # sana tegilmadi
    assert late_shipment.eta == second_eta     # yuk sanasi ham


def test_undoing_the_latest_delay_puts_the_eta_back(admin_client, late_shipment):
    was = late_shipment.eta
    _extend(admin_client, late_shipment, 5, "Chegarada navbat")
    delay = late_shipment.delays.get()

    resp = admin_client.post(f"/delays/{delay.pk}/delete/")
    assert resp.status_code == 302

    late_shipment.refresh_from_db()
    assert late_shipment.eta == was
    assert not ShipmentDelay.objects.exists()
    assert late_shipment.is_overdue            # yana kechikkan holatga qaytdi


def test_undo_walks_back_one_step_at_a_time(admin_client, late_shipment):
    """Ikki marta uzaytirilgan yukda oxirgisini bekor qilish birinchisining
    sanasiga qaytaradi — boshiga emas."""
    first_eta = _extend(admin_client, late_shipment, 5, "Chegarada navbat")
    _extend(admin_client, late_shipment, 9, "Yo'l yopiq")
    latest = late_shipment.delays.first()

    admin_client.post(f"/delays/{latest.pk}/delete/")

    late_shipment.refresh_from_db()
    assert late_shipment.eta == first_eta
    assert late_shipment.delays.count() == 1


def test_an_older_delay_cannot_be_undone(admin_client, late_shipment):
    _extend(admin_client, late_shipment, 5, "Chegarada navbat")
    second_eta = _extend(admin_client, late_shipment, 9, "Yo'l yopiq")
    older = late_shipment.delays.last()

    resp = admin_client.post(f"/delays/{older.pk}/delete/")

    assert ShipmentDelay.objects.count() == 2
    late_shipment.refresh_from_db()
    assert late_shipment.eta == second_eta
    assert "eng oxirgi" in resp.content.decode()


def test_a_tarjimon_cannot_touch_the_history(translator_client, admin_client,
                                             late_shipment):
    """Uzaytirish adminniki — uni tuzatish ham shunday."""
    _extend(admin_client, late_shipment, 5, "Chegarada navbat")
    delay = late_shipment.delays.get()

    assert translator_client.post(
        f"/delays/{delay.pk}/edit/",
        {"new_eta": date.today().isoformat(), "reason": "x"}).status_code == 403
    assert translator_client.post(f"/delays/{delay.pk}/delete/").status_code == 403
    delay.refresh_from_db()
    assert delay.reason == "Chegarada navbat"
