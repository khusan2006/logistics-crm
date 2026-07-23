from datetime import date, timedelta
from decimal import Decimal

from conftest import line_data, make_shipment
from crm.models import Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus


def _contract(kg="1000"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    _contract_obj = Contract.objects.create(partner=partner, created="2026-07-01")
    _contract_obj_line = ContractLine.objects.create(
        contract=_contract_obj, brand="LLDPE", kg=Decimal(kg), price=Decimal("1.00"))
    return _contract_obj


def _post_shipment(client, contract, **extra):
    """A yuk carrying one product of the kelishuv. `kg`/`price` address that row."""
    row = {"contract_line": contract.lines.first().pk, "kg": extra.pop("kg", "400")}
    if "price" in extra:
        row["price"] = extra.pop("price")
    data = {"contract": contract.pk,
            "status": ShipmentStatus.objects.first().pk, "sent": "2026-07-05",
            "eta": "2026-07-20", "transport": "01A111AA", "container": "MSCU-1",
            "note": "", **line_data(row)}
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
    s = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first(), eta=date.today() - timedelta(days=3))
    s_line = ShipmentLine.objects.create(
        shipment=s, contract_line=c.lines.first(), kg=Decimal("100"))
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
    _ship_obj = Shipment.objects.create(contract=c, status=yolda)
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"))
    _ship_obj = Shipment.objects.create(contract=c, status=yolda)
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"))
    _ship_obj = Shipment.objects.create(contract=c, status=bojxona)
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"))

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
    _ship_obj = Shipment.objects.create(contract=c, status=first, transport="TRUCK-XYZ")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"))
    _ship_obj = Shipment.objects.create(contract=c, status=first, transport="OTHER-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"))
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
        {"contract": c.pk, "status": ShipmentStatus.objects.first().pk,
         "sent": "2026-07-05", "eta": "2026-07-20", "transport": "01A222BB",
         "container": "MSCU-2", "note": "",
         **line_data({"contract_line": c.lines.first().pk, "kg": "400"})},
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
    """Each product option carries its qolgan kg and narx, so the form JS can
    filter the list by kelishuv and prefill the row."""
    from crm.forms import ShipmentForm, ShipmentLineForm
    _contract()
    head, row = str(ShipmentForm()), str(ShipmentLineForm())
    assert "data-contract-source" in head
    assert "data-remaining" in row and "data-line-source" in row


def test_translator_sees_no_price_on_loads(translator_client, admin_client, db):
    """The Narx (price) column + expense shortcut are admin-only — the loads page is
    translator-visible and must stay money-free for them. Scope to the page content
    (the shared base.html JS mentions '$' in an unrelated preview helper)."""
    c = _contract()  # price 1.00/kg
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"))

    def content(html):
        return html.split('class="content"', 1)[1].split("</main>", 1)[0]

    tr = content(translator_client.get("/shipments/").content.decode())
    assert "Qiymati" not in tr and "$" not in tr and "Xarajat" not in tr
    ad = content(admin_client.get("/shipments/").content.decode())
    assert "Qiymati" in ad and "$100.00" in ad and "Xarajat" in ad


def test_yuklar_list_has_inline_legs_panel(admin_client, db):
    """Each load row on the Yuklar list has an expand control and an inline legs
    panel so the route can be managed without opening the detail page."""
    from crm.models import ShipmentLeg
    c = _contract()
    s = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    s_line = ShipmentLine.objects.create(
        shipment=s, contract_line=c.lines.first(), kg=Decimal("100"))
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
    own = make_shipment(contract=c, kg="100", price="2.50").lines.first()
    dflt = make_shipment(contract=c, kg="100").lines.first()
    assert own.unit_price == Decimal("2.50") and own.goods_value == Decimal("250.00")
    assert dflt.unit_price == Decimal("1.00") and dflt.goods_value == Decimal("100.00")
    assert own.landed_cost_per_kg == Decimal("2.5000")


def test_shipment_form_price_prefills_from_contract(db):
    """Each product option carries data-price so the form JS can prefill that row's
    1 kg narxi from the kelishuv."""
    from crm.forms import ShipmentLineForm
    _contract()
    assert "data-price" in str(ShipmentLineForm())


def test_active_list_groups_by_contract_and_shows_price_per_kg(admin_client, db):
    """Rows are grouped under kelishuv header rows, and the Narx column shows the
    per-kg unit price."""
    c = _contract()
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"), price=Decimal("2.5"))
    resp = admin_client.get("/shipments/")
    html = resp.content.decode()
    assert f'class="kelishuv-row" data-contract="{c.pk}"' in html
    assert f"Kelishuv {c.code}" in html
    assert "$/kg" in html and "2,5" in html or "2.5" in html
    groups = resp.context["groups"]
    assert len(groups) == 1 and groups[0]["contract"].pk == c.pk


def test_arrived_loads_move_to_done_page(admin_client, db):
    """Arrived loads leave the active Yuklar list and appear on Yakunlangan."""
    c = _contract()
    active = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    active_line = ShipmentLine.objects.create(
        shipment=active, contract_line=c.lines.first(), kg=Decimal("100"))
    done = Shipment.objects.create(contract=c, status=ShipmentStatus.arrival(), arrived=date.today())
    done_line = ShipmentLine.objects.create(
        shipment=done, contract_line=c.lines.first(), kg=Decimal("200"))
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
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.arrival(), arrived=date.today())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"))

    def content(html):
        return html.split('class="content"', 1)[1].split("</main>", 1)[0]

    tr = content(translator_client.get("/shipments/done/").content.decode())
    assert "$" not in tr and "Qiymati" not in tr
    ad = content(admin_client.get("/shipments/done/").content.decode())
    assert "$" in ad and "Qiymati" in ad


def test_set_status_ajax_returns_json_in_place_update(admin_client, db):
    """The list saves status changes via fetch: JSON back, no redirect — the row
    updates in place and an arrival answer tells the JS to drop the row."""
    c = _contract()
    s = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    s_line = ShipmentLine.objects.create(
        shipment=s, contract_line=c.lines.first(), kg=Decimal("100"))
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


def _expense_cell(html):
    """The Xarajat cell of the first load row (the inline panel below it also
    mentions expenses, so assertions have to target the column itself)."""
    return html.split('class="num load-expense"', 1)[1].split("</td>", 1)[0]


def test_loads_table_totals_expenses_after_transport(admin_client, translator_client, db):
    """Yuklar carries the load's xarajat total in its own column, right after
    Transport / Konteyner. It is money, so translators never see it."""
    from crm.models import ShipmentExpense
    c = _contract()
    s = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first(), transport="01A111AA", container="MSCU-1")
    s_line = ShipmentLine.objects.create(
        shipment=s, contract_line=c.lines.first(), kg=Decimal("100"))
    ShipmentExpense.objects.create(shipment=s, amount=Decimal("120.50"), category="road")
    ShipmentExpense.objects.create(shipment=s, amount=Decimal("79.50"), category="customs")

    html = admin_client.get("/shipments/").content.decode()
    assert html.index("Transport / Konteyner") < html.index("Xarajat</th>") < html.index("Kelish</th>")
    assert "$200.00" in _expense_cell(html) and "2 ta" in _expense_cell(html)

    tr = translator_client.get("/shipments/").content.decode()
    assert "Xarajat</th>" not in tr and "$200.00" not in tr


def test_loads_table_shows_a_dash_when_no_expenses(admin_client, db):
    """An expense-free load reads as — , not $0.00: nothing was spent on it yet."""
    c = _contract()
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"))
    cell = _expense_cell(admin_client.get("/shipments/").content.decode())
    assert "—" in cell and "$" not in cell


def test_shipment_form_has_no_route_fields(db):
    """Yuk qo'shish never asks qayerdan/qayerga — the run is always Eron →
    O'zbekiston, so the route is a constant, not a question."""
    from crm.forms import ShipmentForm
    form = ShipmentForm()
    assert "origin" not in form.fields and "destination" not in form.fields


def test_new_shipment_gets_the_iran_uzbekistan_route(admin_client, db):
    c = _contract()
    assert _post_shipment(admin_client, c).status_code == 302
    s = Shipment.objects.get()
    assert s.origin == "Eron" and s.destination == "O'zbekiston"


def test_transport_and_container_are_optional(admin_client, db):
    """Yuk ochilayotganda mashina va konteyner raqami hali ma'lum bo'lmasligi
    mumkin — ikkalasini ham bo'sh qoldirib saqlash mumkin."""
    c = _contract()
    resp = _post_shipment(admin_client, c, transport="", container="")
    assert resp.status_code == 302
    s = Shipment.objects.get()
    assert s.transport == "" and s.container == ""


def test_two_yuklar_can_both_have_no_container(admin_client, db):
    """Bo'sh konteyner raqami takrorlanish tekshiruviga tushmasligi kerak."""
    c = _contract(kg="2000")
    _post_shipment(admin_client, c, transport="", container="")
    resp = _post_shipment(admin_client, c, transport="", container="")
    assert resp.status_code == 302 and Shipment.objects.count() == 2


def test_transport_and_container_stay_editable_after_saving(admin_client, db):
    """Keyin ma'lum bo'lganda qo'shib qo'yish mumkin."""
    c = _contract()
    _post_shipment(admin_client, c, transport="", container="")
    s = Shipment.objects.get()
    resp = admin_client.post(f"/shipments/{s.pk}/edit/", {
        "contract": c.pk, "status": s.status_id, "sent": "2026-07-05",
        "eta": "2026-07-20", "transport": "01 777 AAA", "container": "MSKU 123456 7",
        "note": "",
        **line_data({"id": s.lines.first().pk, "contract_line": c.lines.first().pk,
                     "kg": "400"}, initial=1),
    })
    assert resp.status_code in (204, 302)
    s.refresh_from_db()
    assert s.transport == "01 777 AAA" and s.container


def test_driver_name_and_phone_are_optional_and_saved(admin_client, db):
    """Haydovchi ismi va telefoni — ixtiyoriy, lekin kiritilsa saqlanadi."""
    c = _contract(kg="2000")
    assert _post_shipment(admin_client, c, transport="", container="").status_code == 302
    assert Shipment.objects.get().driver_name == ""

    Shipment.objects.all().delete()
    resp = _post_shipment(admin_client, c, driver_name="Akmal aka",
                          driver_phone="+998901112233")
    assert resp.status_code == 302
    s = Shipment.objects.get()
    assert s.driver_name == "Akmal aka" and s.driver_phone == "+998901112233"


def test_driver_shows_on_the_yuk_page(admin_client, db):
    c = _contract(kg="2000")
    _post_shipment(admin_client, c, driver_name="Akmal aka", driver_phone="+998901112233")
    s = Shipment.objects.get()
    html = admin_client.get(f"/shipments/{s.pk}/").content.decode()
    assert "Akmal aka" in html and "998901112233" in html


def test_responsible_person_is_saved_and_shown(admin_client, db):
    """Mas'ul shaxs — yuk uchun javobgar xodim; ixtiyoriy, lekin kiritilsa
    yuk sahifasida ko'rinadi."""
    c = _contract(kg="2000")
    assert _post_shipment(admin_client, c).status_code == 302
    assert Shipment.objects.get().responsible == ""      # ixtiyoriy

    Shipment.objects.all().delete()
    assert _post_shipment(admin_client, c, responsible="Otabek").status_code == 302
    s = Shipment.objects.get()
    assert s.responsible == "Otabek"
    assert "Otabek" in admin_client.get(f"/shipments/{s.pk}/").content.decode()


def test_yuklar_opens_on_yolda(admin_client, db):
    """Logist yo'ldagi yuklarni kuzatadi — sahifa o'sha tabda ochiladi."""
    make_shipment(contract=_contract(), kg="100")   # tabs only render with yuklar
    resp = admin_client.get("/shipments/")
    yolda = ShipmentStatus.objects.get(name="Yo'lda")
    assert resp.context["default_tab"] == yolda.pk
    html = resp.content.decode()
    assert f'class="status-tab is-active" data-tab="{yolda.pk}" data-default' in html


def test_no_default_tab_when_yolda_was_renamed(admin_client, db):
    """Holatlar tahrirlanadi — nomi o'zgarsa sahifa avvalgidek Hammasi bilan ochiladi."""
    ShipmentStatus.objects.filter(name="Yo'lda").update(name="Harakatda")
    assert admin_client.get("/shipments/").context["default_tab"] is None
