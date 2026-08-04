from decimal import Decimal

import pytest
from django.utils.html import escape

from crm.forms import ExpenseGridForm
from crm.models import (
    Contract, ContractLine, Logist, Partner, Shipment, ShipmentExpense, ShipmentLine,
    ShipmentStatus,
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


def opened(shipment, **over):
    """Exactly what the browser would POST if the operator opened the modal on this
    yuk and pressed Saqlash — every box as it was rendered, hidden row anchors and
    all. Built from the bound fields rather than by hand, because the point of these
    tests is what a re-submitted grid does to rows it is already showing."""
    form = ExpenseGridForm(shipment=shipment)
    data = {name: "" if form[name].value() is None else str(form[name].value())
            for name in form.fields}
    data["shipment"] = shipment.pk
    data.update({k: ("" if v is None else str(v)) for k, v in over.items()})
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


# --- the yuk's xarajatlar, not an entry queue -------------------------------

def test_the_modal_opens_showing_what_the_yuk_already_has(admin_client, shipment):
    """Coming back to add a bojxona used to mean seven empty boxes, with no way to
    tell from here what was already in the books."""
    admin_client.post("/expenses/new/", grid(shipment, loader="65", declarant="175"))
    html = admin_client.get("/expenses/new/?shipment=%d" % shipment.pk,
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    assert 'name="amount_loader" value="65.00"' in html
    assert 'name="amount_declarant" value="175.00"' in html
    assert 'name="amount_customs" value=' not in html          # nothing recorded yet


def test_a_so_m_row_opens_showing_so_m(admin_client, shipment):
    """The box is labelled in the valyuta beside it: a 1 265 000 so'm bojxona coming
    back as 100 would be re-read as 100 so'm and saved as $0.008."""
    admin_client.post("/expenses/new/", grid(
        shipment, currency="uzs", exchange_rate="12650", customs="1265000"))
    html = admin_client.get("/expenses/new/?shipment=%d" % shipment.pk,
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    assert 'name="amount_customs" value="1265000.00"' in html


def test_resubmitting_an_untouched_grid_changes_nothing(admin_client, shipment):
    """Including the kurs, which is one field above seven boxes: rows entered on two
    days at two kursi must not both be re-rated by a submit that touched nothing."""
    admin_client.post("/expenses/new/", grid(shipment, exchange_rate="12000",
                                             loader="65", declarant="175"))
    admin_client.post("/expenses/new/", grid(shipment, date="2026-07-24",
                                             exchange_rate="12500", customs="3200"))
    before = {e.pk: (e.amount, e.amount_uzs, e.exchange_rate, e.date)
              for e in shipment.expenses.all()}

    admin_client.post("/expenses/new/", opened(shipment))

    assert {e.pk: (e.amount, e.amount_uzs, e.exchange_rate, e.date)
            for e in shipment.expenses.all()} == before


def test_adding_a_turkum_does_not_duplicate_the_ones_on_show(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(shipment, loader="65", declarant="175"))

    admin_client.post("/expenses/new/", opened(shipment, amount_customs="3200"))

    saved = {e.category: e.amount for e in shipment.expenses.all()}
    assert saved == {"loader": Decimal("65.00"), "declarant": Decimal("175.00"),
                     "customs": Decimal("3200.00")}


def test_a_changed_box_rewrites_its_own_row(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(shipment, loader="65"))
    row = shipment.expenses.get()

    admin_client.post("/expenses/new/", opened(shipment, amount_loader="70"))

    assert shipment.expenses.count() == 1
    row.refresh_from_db()
    assert row.amount == Decimal("70.00")
    # the sana it was recorded with, not today: the Sana above dates new rows
    assert str(row.date) == "2026-07-10"


def test_a_cleared_box_deletes_its_row(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(shipment, loader="65", declarant="175"))

    admin_client.post("/expenses/new/", opened(shipment, amount_declarant=""))

    assert [e.category for e in shipment.expenses.all()] == ["loader"]


def test_clearing_every_box_is_a_delete_not_an_empty_submission(admin_client, shipment):
    """A blank grid on a yuk whose boxes opened filled is every xarajat cleared —
    the "kamida bitta" guard is for a grid that never showed anything."""
    admin_client.post("/expenses/new/", grid(shipment, loader="65"))

    resp = admin_client.post("/expenses/new/", opened(shipment, amount_loader=""),
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    assert resp.status_code == 204
    assert not shipment.expenses.exists()


def test_a_grid_that_never_showed_a_row_leaves_it_alone(admin_client, shipment):
    """The stale-modal case: opened before a colleague entered the gruzchi, so its
    box is blank here for the innocent reason that it did not exist yet."""
    admin_client.post("/expenses/new/", grid(shipment, loader="65"))

    admin_client.post("/expenses/new/", grid(shipment, customs="3200"))

    saved = {e.category: e.amount for e in shipment.expenses.all()}
    assert saved == {"loader": Decimal("65.00"), "customs": Decimal("3200.00")}


def test_a_turkum_recorded_twice_stays_additive(admin_client, shipment):
    """Two yo'l xarajati on two different days have no single figure to show, and
    prefilling one of them would rewrite that one and silently leave the other."""
    admin_client.post("/expenses/new/", grid(shipment, road="40"))
    admin_client.post("/expenses/new/", grid(shipment, date="2026-07-12", road="30"))

    payload = opened(shipment)
    assert payload["amount_road"] == "" and payload["row_road"] == ""

    admin_client.post("/expenses/new/", dict(payload, amount_road="25"))
    assert [e.amount for e in shipment.expenses.filter(category="road")] \
        == [Decimal("25.00"), Decimal("30.00"), Decimal("40.00")]


def test_the_haydovchi_avansi_is_never_one_of_the_boxes(admin_client, shipment):
    """The yuk form owns that row and rewrites it on every save, so a figure typed
    over it here would be undone the next time the yuk is edited."""
    logist = Logist.objects.create(name="Sardor aka")
    advance = ShipmentExpense.objects.create(
        shipment=shipment, category="transport", amount=Decimal("500"),
        amount_uzs=Decimal("6000000"), exchange_rate=Decimal("12000"),
        logist=logist, is_driver_advance=True, date="2026-07-09")

    payload = opened(shipment)
    assert payload["amount_transport"] == "" and payload["row_transport"] == ""

    admin_client.post("/expenses/new/", dict(payload, amount_transport="300"))
    advance.refresh_from_db()
    assert advance.amount == Decimal("500")                     # untouched
    assert shipment.expenses.filter(category="transport").count() == 2


def test_the_shared_date_and_kurs_are_not_overridable(admin_client, shipment):
    """Sana and kurs describe the trip, not the line — one truck, one rate."""
    html = admin_client.get("/expenses/new/?shipment=%d" % shipment.pk,
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    for category, _ in ShipmentExpense.Category.choices:
        assert f'name="date_{category}"' not in html
        assert f'name="exchange_rate_{category}"' not in html
