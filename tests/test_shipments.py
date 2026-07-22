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


def test_container_unique_ignores_spacing(admin_client, db):
    c = _contract()
    _post_shipment(admin_client, c, kg="100", container="MSKU 123456 7")
    resp = _post_shipment(admin_client, c, kg="100", container="msku1234567")
    assert Shipment.objects.count() == 1 and resp.status_code == 200
    assert Shipment.objects.first().container == "MSKU 123456 7"


def test_container_stored_normalized(admin_client, db):
    c = _contract()
    _post_shipment(admin_client, c, kg="100", container="mscu1234567")
    assert Shipment.objects.get().container == "MSCU 123456 7"


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


def test_status_tabs_have_per_status_counts(admin_client, db):
    """The page offers one tab per status (in order) with a live count, and every
    load is rendered as a row tagged with its status for client-side tab filtering."""
    c = _contract(kg="5000")
    yolda = ShipmentStatus.objects.get(name="Yo'lda")
    bojxona = ShipmentStatus.objects.get(name="Bojxona")
    Shipment.objects.create(contract=c, kg=Decimal("100"), status=yolda)
    Shipment.objects.create(contract=c, kg=Decimal("100"), status=yolda)
    Shipment.objects.create(contract=c, kg=Decimal("100"), status=bojxona)

    resp = admin_client.get("/shipments/")
    assert resp.status_code == 200
    tabs = resp.context["tabs"]
    names = [t["status"].name for t in tabs]
    # no tab for the arrival status — those loads live on the Yakunlangan page
    assert names == list(ShipmentStatus.objects.filter(is_arrival=False)
                         .values_list("name", flat=True))
    by_name = {t["status"].name: t["count"] for t in tabs}
    assert by_name["Yo'lda"] == 2 and by_name["Bojxona"] == 1
    assert resp.context["total"] == 3
    # rows carry their status id so the tab JS can filter them
    html = resp.content.decode()
    assert f'data-status="{yolda.pk}"' in html


def test_shipment_search_filters_rows(admin_client, db):
    c = _contract(kg="5000")
    first = ShipmentStatus.objects.first()
    Shipment.objects.create(contract=c, kg=Decimal("100"), status=first, transport="TRUCK-XYZ")
    Shipment.objects.create(contract=c, kg=Decimal("100"), status=first, transport="OTHER-1")
    resp = admin_client.get("/shipments/", {"q": "XYZ"})
    rows = resp.context["shipments"]
    assert len(rows) == 1 and rows[0].transport == "TRUCK-XYZ"


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


def test_translator_sees_no_price_on_loads(translator_client, admin_client, db):
    """The Narx (price) column + expense shortcut are admin-only — the loads page is
    translator-visible and must stay money-free for them. Scope to the page content
    (the shared base.html JS mentions '$' in an unrelated preview helper)."""
    c = _contract()  # price 1.00/kg
    Shipment.objects.create(contract=c, kg=Decimal("100"), status=ShipmentStatus.objects.first())

    def content(html):
        return html.split('class="content"', 1)[1].split("</main>", 1)[0]

    tr = content(translator_client.get("/shipments/").content.decode())
    assert "Narx" not in tr and "$" not in tr and "Xarajat" not in tr
    ad = content(admin_client.get("/shipments/").content.decode())
    assert "Narx" in ad and "$100.00" in ad and "Xarajat" in ad


def test_yuklar_list_has_inline_legs_panel(admin_client, db):
    """Each load row on the Yuklar list has an expand control and an inline legs
    panel so the route can be managed without opening the detail page."""
    from crm.models import ShipmentLeg
    c = _contract()
    s = Shipment.objects.create(contract=c, kg=Decimal("100"), status=ShipmentStatus.objects.first())
    ShipmentLeg.objects.create(shipment=s, order=1, from_location="Tehron",
                               to_location="Chegara", transport="12 A 345")
    html = admin_client.get("/shipments/").content.decode()
    assert "leg-expand" in html and "legs-detail" in html
    assert "Tehron" in html and "Chegara" in html          # legs rendered inline
    assert f"/legs/new/?shipment={s.pk}" in html            # inline "+ Bosqich"
    # admin also sees the load's expenses inside the panel
    assert "Xarajatlar" in html and f"/expenses/new/?shipment={s.pk}" in html


def test_shipment_own_price_drives_value_and_landed_cost(admin_client, db):
    """A truck may carry its own USD/kg price; value, landed cost and the sale
    cost snapshot all follow it. Blank price falls back to the contract price."""
    c = _contract()  # price 1.00/kg
    own = Shipment.objects.create(contract=c, kg=Decimal("100"), price=Decimal("2.50"),
                                  status=ShipmentStatus.objects.first())
    dflt = Shipment.objects.create(contract=c, kg=Decimal("100"),
                                   status=ShipmentStatus.objects.first())
    assert own.unit_price == Decimal("2.50") and own.goods_value == Decimal("250.00")
    assert dflt.unit_price == Decimal("1.00") and dflt.goods_value == Decimal("100.00")
    assert own.landed_cost_per_kg == Decimal("2.5000")


def test_shipment_form_price_prefills_from_contract(db):
    """The contract <select> carries data-price so the form JS can prefill the
    truck's 1 kg narxi from the chosen kelishuv."""
    from crm.forms import ShipmentForm
    _contract()
    assert "data-price" in str(ShipmentForm())


def test_active_list_groups_by_contract_and_shows_price_per_kg(admin_client, db):
    """Rows are grouped under kelishuv header rows, and the Narx column shows the
    per-kg unit price."""
    c = _contract()
    Shipment.objects.create(contract=c, kg=Decimal("100"), price=Decimal("2.5"),
                            status=ShipmentStatus.objects.first())
    resp = admin_client.get("/shipments/")
    html = resp.content.decode()
    assert f'class="kelishuv-row" data-contract="{c.pk}"' in html
    assert "Kelishuv #%d" % c.pk in html
    assert "$/kg" in html and "2,5" in html or "2.5" in html
    groups = resp.context["groups"]
    assert len(groups) == 1 and groups[0]["contract"].pk == c.pk


def test_arrived_loads_move_to_done_page(admin_client, db):
    """Arrived loads leave the active Yuklar list and appear on Yakunlangan."""
    c = _contract()
    active = Shipment.objects.create(contract=c, kg=Decimal("100"),
                                     status=ShipmentStatus.objects.first())
    done = Shipment.objects.create(contract=c, kg=Decimal("200"),
                                   status=ShipmentStatus.arrival(),
                                   arrived=date.today())
    main = admin_client.get("/shipments/")
    ids = [s.pk for s in main.context["shipments"]]
    assert active.pk in ids and done.pk not in ids
    assert main.context["done_count"] == 1
    assert "/shipments/done/" in main.content.decode()

    done_page = admin_client.get("/shipments/done/")
    assert done_page.status_code == 200
    done_ids = [s.pk for s in done_page.context["page"].object_list]
    assert done_ids == [done.pk]
    # translator may see it too, but with no money on the page
    assert "Yakunlangan" in done_page.content.decode()


def test_done_page_hides_money_from_translator(translator_client, admin_client, db):
    c = _contract()  # price 1.00/kg
    Shipment.objects.create(contract=c, kg=Decimal("100"),
                            status=ShipmentStatus.arrival(), arrived=date.today())

    def content(html):
        return html.split('class="content"', 1)[1].split("</main>", 1)[0]

    tr = content(translator_client.get("/shipments/done/").content.decode())
    assert "$" not in tr and "Narx" not in tr
    ad = content(admin_client.get("/shipments/done/").content.decode())
    assert "$/kg" in ad


def test_set_status_ajax_returns_json_in_place_update(admin_client, db):
    """The list saves status changes via fetch: JSON back, no redirect — the row
    updates in place and an arrival answer tells the JS to drop the row."""
    c = _contract()
    s = Shipment.objects.create(contract=c, kg=Decimal("100"),
                                status=ShipmentStatus.objects.first())
    other = ShipmentStatus.objects.filter(is_arrival=False).exclude(pk=s.status_id).first()
    resp = admin_client.post(f"/shipments/{s.pk}/status/", {"status": other.pk},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 200
    assert resp.json() == {"status_id": other.pk, "arrived": False}
    s.refresh_from_db()
    assert s.status_id == other.pk

    arrival = ShipmentStatus.arrival()
    resp = admin_client.post(f"/shipments/{s.pk}/status/", {"status": arrival.pk},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.json() == {"status_id": arrival.pk, "arrived": True}
    s.refresh_from_db()
    assert s.arrived is not None
