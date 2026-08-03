from decimal import Decimal

import pytest
from django.utils.html import escape

from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus,
)


def grid(shipment, date="2026-07-10", currency="usd", method="cash",
         exchange_rate="12000", note="", fee_percent="0", **amounts):
    """POST payload for the xarajat modal: shared settings plus one amount per
    turkum. `amounts` is keyed by category — grid(customs="3200", loader="65")."""
    data = {"shipment": shipment.pk, "date": date, "currency": currency,
            "method": method, "exchange_rate": exchange_rate, "note": note,
            "fee_percent": fee_percent}
    for category, value in amounts.items():
        data[f"amount_{category}"] = str(value)
    return data


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal("20000"), price=Decimal("1.00"))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal("10000"))
    return _ship_obj


def test_landed_cost(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(shipment, transport="500", customs="300"))
    lot = shipment.lines.first()
    # 800 spread over 10 000 kg = 0.08 $/kg on top of the 1.00 kelishuv narx
    assert lot.landed_cost_per_kg == Decimal("1.0800")


def test_no_expenses_landed_cost_is_contract_price(shipment):
    assert shipment.lines.first().landed_cost_per_kg == Decimal("1.0000")


def test_translator_forbidden(translator_client, shipment):
    resp = translator_client.get("/expenses/new/?shipment=%d" % shipment.pk)
    assert resp.status_code == 403


def test_create_modal_get_returns_partial(admin_client, shipment):
    resp = admin_client.get(
        "/expenses/new/?shipment=%d" % shipment.pk, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_modal_post_valid_returns_204_with_redirect(admin_client, shipment):
    resp = admin_client.post("/expenses/new/", grid(shipment, customs="250"),
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 204
    assert ShipmentExpense.objects.get().amount == Decimal("250.00")


def test_every_turkum_has_its_own_input(admin_client, shipment):
    """The turkumlar are the form — no dropdown to pick one from."""
    html = admin_client.get("/expenses/new/?shipment=%d" % shipment.pk,
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    for category, label in ShipmentExpense.Category.choices:
        assert f'name="amount_{category}"' in html, category
        # escape(): labels like "Yo'l xarajati" reach the page as Yo&#x27;l
        assert escape(label) in html, label
    assert 'name="category"' not in html          # the old dropdown is gone


def test_one_pass_saves_every_filled_box(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(
        shipment, customs="3200", declarant="175", loader="65"))
    saved = {e.category: e.amount for e in ShipmentExpense.objects.all()}
    assert saved == {"customs": Decimal("3200.00"),
                     "declarant": Decimal("175.00"),
                     "loader": Decimal("65.00")}


def test_blank_boxes_save_nothing(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(
        shipment, customs="100", transport="", loader="", road=""))
    assert ShipmentExpense.objects.count() == 1
    assert ShipmentExpense.objects.get().category == "customs"


def test_an_empty_xarajat_modal_is_rejected(admin_client, shipment):
    """Every box blank would otherwise save nothing and close as if it worked."""
    resp = admin_client.post("/expenses/new/", grid(shipment),
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 422
    assert not ShipmentExpense.objects.exists()


def test_a_negative_amount_is_rejected(admin_client, shipment):
    resp = admin_client.post("/expenses/new/", grid(shipment, customs="-5"),
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 422
    assert not ShipmentExpense.objects.exists()


def test_shared_settings_land_on_every_row(admin_client, shipment):
    """Sana, valyuta, kurs and usul are asked once and apply to all of them."""
    admin_client.post("/expenses/new/", grid(
        shipment, date="2026-07-14", method="transfer", customs="100", loader="50"))
    rows = ShipmentExpense.objects.all()
    assert {str(e.date) for e in rows} == {"2026-07-14"}
    assert {e.method for e in rows} == {"transfer"}
    assert {e.exchange_rate for e in rows} == {Decimal("12000.00")}


def test_uzs_converted_to_usd(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(
        shipment, currency="uzs", exchange_rate="12650", customs="1265000"))
    expense = ShipmentExpense.objects.get()
    assert expense.amount == Decimal("100.00")          # 1 265 000 / 12 650
    assert expense.amount_uzs == Decimal("1265000.00")


def test_the_sana_is_required(admin_client, shipment):
    resp = admin_client.post("/expenses/new/", grid(shipment, date="", customs="100"),
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 422
    assert not ShipmentExpense.objects.exists()


def test_the_fee_rides_on_each_row(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(
        shipment, method="transfer", fee_percent="2", customs="1000"))
    expense = ShipmentExpense.objects.get()
    assert expense.fee_amount == Decimal("20.00")
    assert expense.total_out == Decimal("1020.00")


def test_a_box_may_override_the_shared_currency_and_method(admin_client, shipment):
    """A bojxona wired in so'm next to a cash gruzchi — one submission, each row
    converting at its own currency."""
    admin_client.post("/expenses/new/", dict(
        grid(shipment, currency="usd", method="cash", exchange_rate="12000",
             customs="1200000", loader="65"),
        currency_customs="uzs", method_customs="transfer"))
    rows = {e.category: e for e in ShipmentExpense.objects.all()}

    boj = rows["customs"]
    assert boj.currency == "uzs" and boj.method == "transfer"
    assert boj.amount == Decimal("100.00")            # 1 200 000 / 12 000
    assert boj.amount_uzs == Decimal("1200000.00")

    gruz = rows["loader"]
    assert gruz.currency == "usd" and gruz.method == "cash"   # fell back to shared
    assert gruz.amount == Decimal("65.00")


def test_a_blank_override_means_use_the_shared_one(admin_client, shipment):
    admin_client.post("/expenses/new/", dict(
        grid(shipment, currency="uzs", method="transfer", exchange_rate="12000",
             customs="120000"),
        currency_customs="", method_customs=""))
    expense = ShipmentExpense.objects.get()
    assert expense.currency == "uzs" and expense.method == "transfer"


def test_the_shared_date_and_kurs_are_not_overridable(admin_client, shipment):
    """Sana and kurs describe the trip, not the line — one truck, one rate."""
    html = admin_client.get("/expenses/new/?shipment=%d" % shipment.pk,
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    for category, _ in ShipmentExpense.Category.choices:
        assert f'name="date_{category}"' not in html
        assert f'name="exchange_rate_{category}"' not in html
