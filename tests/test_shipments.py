from datetime import date, timedelta
from decimal import Decimal

from crm.models import Contract, Partner, Shipment, ShipmentStatus


def _contract(kg="1000"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    return Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal(kg),
                                   price=Decimal("1.00"), created="2026-07-01",
                                   deadline="2026-08-01")


def _post_shipment(client, contract, **extra):
    data = {"contract": contract.pk, "kg": "400",
            "status": ShipmentStatus.objects.first().pk, "sent": "2026-07-05",
            "eta": "2026-07-20", "transport": "01A111AA", "container": "MSCU-1",
            "note": ""}
    data.update(extra)
    return client.post("/shipments/new/", data)


def test_create_and_contract_progress(admin_client, db):
    c = _contract()
    assert _post_shipment(admin_client, c).status_code == 302
    assert c.shipped_kg == Decimal("400.000")
    assert c.remaining_kg == Decimal("600.000")


def test_kg_over_contract_blocked(admin_client, db):
    c = _contract(kg="300")
    resp = _post_shipment(admin_client, c)
    assert resp.status_code == 200 and not Shipment.objects.exists()


def test_container_unique(admin_client, db):
    c = _contract()
    _post_shipment(admin_client, c)
    resp = _post_shipment(admin_client, c, kg="100", container="mscu-1")
    assert Shipment.objects.count() == 1 and resp.status_code == 200


def test_overdue(db, admin_user):
    c = _contract()
    s = Shipment.objects.create(contract=c, kg=Decimal("100"),
                                status=ShipmentStatus.objects.first(),
                                eta=date.today() - timedelta(days=3))
    assert s.is_overdue and s.days_late == 3


def test_translator_sees_list_but_cannot_create(translator_client, db):
    assert translator_client.get("/shipments/").status_code == 200
    c = _contract()
    assert _post_shipment(translator_client, c).status_code == 403


def test_kanban_groups_shipments_by_status_column(admin_client, db):
    """The board has one column per status (in order) and each load sits under its
    own status column with prev/next stage ids wired for the move arrows."""
    c = _contract(kg="5000")
    yolda = ShipmentStatus.objects.get(name="Yo'lda")
    bojxona = ShipmentStatus.objects.get(name="Bojxona")
    s1 = Shipment.objects.create(contract=c, kg=Decimal("100"), status=yolda)
    s2 = Shipment.objects.create(contract=c, kg=Decimal("100"), status=bojxona)

    resp = admin_client.get("/shipments/")
    assert resp.status_code == 200
    columns = resp.context["columns"]
    # one column per status, ordered
    names = [col["status"].name for col in columns]
    assert names == list(ShipmentStatus.objects.values_list("name", flat=True))
    by_name = {col["status"].name: col for col in columns}
    assert s1 in by_name["Yo'lda"]["shipments"]
    assert s2 in by_name["Bojxona"]["shipments"]
    assert s1 not in by_name["Bojxona"]["shipments"]
    # move arrows wired to adjacent stages
    assert by_name["Yo'lda"]["next_id"] == ShipmentStatus.objects.get(name="Chegarada").pk
    assert by_name["Yo'lda"]["prev_id"] == ShipmentStatus.objects.get(name="Yuklanmoqda").pk


def test_kanban_search_filters_cards(admin_client, db):
    c = _contract(kg="5000")
    first = ShipmentStatus.objects.first()
    Shipment.objects.create(contract=c, kg=Decimal("100"), status=first, transport="TRUCK-XYZ")
    Shipment.objects.create(contract=c, kg=Decimal("100"), status=first, transport="OTHER-1")
    resp = admin_client.get("/shipments/", {"q": "XYZ"})
    all_cards = [s for col in resp.context["columns"] for s in col["shipments"]]
    assert len(all_cards) == 1 and all_cards[0].transport == "TRUCK-XYZ"


def test_create_shipment_modal_get_returns_partial(admin_client, db):
    resp = admin_client.get("/shipments/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_shipment_modal_post_valid_returns_204_with_redirect(admin_client, db):
    c = _contract()
    resp = admin_client.post(
        "/shipments/new/",
        {"contract": c.pk, "kg": "400", "status": ShipmentStatus.objects.first().pk,
         "sent": "2026-07-05", "eta": "2026-07-20", "transport": "01A222BB",
         "container": "MSCU-2", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/shipments/"
    assert Shipment.objects.filter(container="MSCU-2").exists()


def test_create_shipment_modal_post_invalid_returns_422(admin_client, db):
    c = _contract(kg="300")
    resp = admin_client.post(
        "/shipments/new/",
        {"contract": c.pk, "kg": "400", "status": ShipmentStatus.objects.first().pk,
         "sent": "2026-07-05", "eta": "2026-07-20", "transport": "01A111AA",
         "container": "MSCU-3", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html
    assert not Shipment.objects.exists()


def test_shipment_transport_rejects_non_plate(db):
    from crm.forms import ShipmentForm
    c = _contract()
    st = ShipmentStatus.objects.first()
    f = ShipmentForm({"contract": c.pk, "kg": "100", "status": st.pk,
                      "transport": "hello world text", "container": "", "note": ""})
    assert not f.is_valid() and "transport" in f.errors


def test_shipment_transport_accepts_uz_plate(db):
    from crm.forms import ShipmentForm
    c = _contract()
    st = ShipmentStatus.objects.first()
    f = ShipmentForm({"contract": c.pk, "kg": "100", "status": st.pk,
                      "transport": "01 777 AAA", "container": "C1", "note": ""})
    assert f.is_valid(), f.errors


def test_shipment_contract_select_carries_prefill_data(db):
    """The contract <select> options expose remaining kg + deadline so the form JS
    can prefill Yuboriladigan kg and Taxminiy kelish."""
    from crm.forms import ShipmentForm
    _contract()  # has kg (remaining) and a deadline
    html = str(ShipmentForm())
    assert "data-remaining" in html and "data-deadline" in html
    assert 'data-contract-source' in html
