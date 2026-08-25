import re
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


def test_the_same_container_can_come_back_on_a_later_yuk(admin_client, db):
    """Containers are reused; a repeat is normal, not an error. This used to be
    rejected as a duplicate, which blocked entering a perfectly real load."""
    c = _contract()
    _post_shipment(admin_client, c, kg="100", container="MSKU 123456 7")
    resp = _post_shipment(admin_client, c, kg="100", container="msku1234567")
    assert resp.status_code == 302
    assert Shipment.objects.count() == 2


def test_container_stored_normalized(admin_client, db):
    """Still tidied — uppercased and grouped when it looks like ISO 6346 — so the
    same container reads the same way however it was typed. Tidying is not
    rejecting: nothing is refused for failing to match."""
    c = _contract()
    _post_shipment(admin_client, c, kg="100", container="mscu1234567")
    assert Shipment.objects.get().container == "MSCU 123456 7"


def test_a_container_that_is_not_iso_is_kept_as_typed(admin_client, db):
    c = _contract()
    _post_shipment(admin_client, c, kg="100", container="konteyner yo'q")
    assert Shipment.objects.get().container == "KONTEYNER YO'Q"


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


def test_transport_is_free_text(db):
    """A plate-shaped regex used to reject anything that was not 5-12 alphanumerics
    containing a digit. Waybills say all sorts of things, and the operator copying
    one is not helped by being told it is the wrong shape."""
    from crm.forms import ShipmentForm
    c = _contract()
    st = ShipmentStatus.objects.first()
    f = ShipmentForm({"contract": c.pk, "kg": "100", "status": st.pk,
                      "sent": "2026-07-08", "transport": "hello world text",
                      "container": "", "note": ""})
    assert f.is_valid(), f.errors
    assert "transport" not in f.errors


def test_shipment_transport_accepts_uz_plate(db):
    from crm.forms import ShipmentForm
    c = _contract()
    st = ShipmentStatus.objects.first()
    f = ShipmentForm({"contract": c.pk, "kg": "100", "status": st.pk,
                      "sent": "2026-07-08", "transport": "01 777 AAA",
                      "container": "C1", "note": ""})
    assert f.is_valid(), f.errors


def test_a_raqam_from_any_country_is_stored_as_typed(admin_client, db):
    """Turkish, Kazakh, Russian — a yuk arrives behind whichever plate the waybill
    carries, and the field neither reshapes nor refuses it. The container beside it
    still tidies itself; this one does not, on purpose."""
    c = _contract()
    _post_shipment(admin_client, c, kg="100", transport="34 abc 123")
    assert Shipment.objects.get().transport == "34 abc 123"


def test_the_raqam_field_has_no_country_picker(admin_client, db):
    """It used to be wrapped in a UZ/IR picker that uppercased and re-spaced what
    was typed — two flags, and no room for a plate issued anywhere else."""
    html = admin_client.get("/shipments/new/").content.decode()
    assert 'name="transport"' in html
    assert "data-plate-intl" not in html


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
    assert "Qiymati" in ad and "$100" in ad and "Xarajat" in ad


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


def test_a_hamkor_kelishuvlari_run_consecutively(admin_client, db):
    """A hamkor's kelishuvlar sit in one block. Ordered by recency alone they
    interleaved, so reading everything going to one hamkor meant hunting the same
    name down a page it appeared on several separate times."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    sobir = Partner.objects.create(name="Sobir", phone="2", city="T")
    # Struck alternately, so recency alone would give Sobir/Pars/Sobir/Pars.
    for partner in (pars, sobir, pars, sobir):
        contract = Contract.objects.create(partner=partner, created="2026-07-01")
        ContractLine.objects.create(contract=contract, brand="LLDPE",
                                    kg=Decimal("1000"), price=Decimal("1.00"))
        make_shipment(contract=contract, kg="100")

    partners = [g["contract"].partner_id
                for g in admin_client.get("/shipments/").context["groups"]]
    assert len(partners) == 4
    # Two blocks of two, and Sobir leads on the strength of the newest kelishuv.
    assert partners == [sobir.pk, sobir.pk, pars.pk, pars.pk]


def test_arrived_loads_are_hidden_until_hammasi(admin_client, db):
    """Yetib kelgan yuklar faol ro'yxatda ko'rinmaydi, Hammasi da esa ko'rinadi."""
    c = _contract(kg="2000")
    active = make_shipment(contract=c, kg="100")
    done = make_shipment(contract=c, kg="200", arrived=date.today(),
                         status=ShipmentStatus.arrival())

    ids = [s.pk for s in admin_client.get("/shipments/").context["shipments"]]
    assert active.pk in ids and done.pk not in ids

    resp = admin_client.get("/shipments/", {"all": "1"})
    all_ids = [s.pk for s in resp.context["shipments"]]
    assert active.pk in all_ids and done.pk in all_ids
    assert resp.context["show_all"] is True
    assert resp.context["default_tab"] is None      # Hammasi filtrsiz ochiladi


def test_hammasi_paginates_and_searches_across_every_load(admin_client, db):
    """Hammasi pages by KELISHUV: a page is N of them and all of their yuklar."""
    c = _contract(kg="200000")
    for i in range(3):
        make_shipment(contract=c, kg="100", arrived=date.today(), transport=f"01 77{i} AAA",
                      status=ShipmentStatus.arrival())
    wanted = make_shipment(contract=c, kg="100", arrived=date.today(),
                           transport="01 999 ZZZ", status=ShipmentStatus.arrival())

    page = admin_client.get("/shipments/", {"all": "1"}).context["page"]
    assert page is not None and page.paginator.count == 1          # one kelishuv…
    assert len(page.object_list[0]["shipments"]) == 4              # …carrying all 4

    hit = admin_client.get("/shipments/", {"all": "1", "q": "999"})
    assert [s.pk for s in hit.context["shipments"]] == [wanted.pk]


def test_hammasi_never_splits_a_kelishuv_across_pages(admin_client, db):
    """Paging the flat list cut a kelishuv wherever its 20th load fell, so the rest
    of its yuklar sat under a second copy of the same header a page later — and
    since the list runs newest-first, that cut ran along the status line: the moving
    loads on one page, the arrived ones on the next."""
    for i in range(12):
        contract = _contract(kg="200000")
        make_shipment(contract=contract, kg="100", transport=f"01 {i:03d} AAA")
        make_shipment(contract=contract, kg="100", arrived=date.today(),
                      transport=f"01 {i:03d} BBB", status=ShipmentStatus.arrival())

    seen = set()
    for number in (1, 2):
        groups = admin_client.get(
            "/shipments/", {"all": "1", "page": number}).context["groups"]
        for g in groups:
            assert g["contract"].pk not in seen        # never a second header
            seen.add(g["contract"].pk)
            # both of the kelishuv's yuklar, whatever holat they are in
            assert len(g["shipments"]) == 2
    assert len(seen) == 12


class TestKelishSanasiSort:
    """Hammasi, saralangan: bugun kelgan yuklar eng tepada, keyin kecha kelganlari,
    va shu tartibda pastga.

    The sort is on `arrived` and not on the row date the davr filter uses — see
    `_filter_shipments`. That is what these hold: a load whose taxminiy kelish is
    next week must not sit above one that actually landed this morning."""

    def _fleet(self):
        """One kelishuv, four yuklar: landed today, kecha, a week ago — plus one
        still on the road. Entered in none of those orders, so a passing assertion
        cannot be the insertion order wearing a disguise."""
        today = date.today()
        c = _contract(kg="200000")
        week = make_shipment(contract=c, kg="100", transport="01 007 AAA",
                             arrived=today - timedelta(days=7),
                             status=ShipmentStatus.arrival())
        # ETA in the future: sorting on Coalesce(arrived, eta) would put this first.
        moving = make_shipment(contract=c, kg="100", transport="01 000 YOL",
                               eta=today + timedelta(days=3))
        bugun = make_shipment(contract=c, kg="100", transport="01 001 AAA",
                              arrived=today, status=ShipmentStatus.arrival())
        kecha = make_shipment(contract=c, kg="100", transport="01 002 AAA",
                              arrived=today - timedelta(days=1),
                              status=ShipmentStatus.arrival())
        return c, bugun, kecha, week, moving

    def test_the_days_run_backwards_from_bugun(self, admin_client, db):
        _c, bugun, kecha, week, moving = self._fleet()
        rows = admin_client.get(
            "/shipments/", {"all": "1", "sort": "kelish"}).context["shipments"]
        assert [s.pk for s in rows] == [bugun.pk, kecha.pk, week.pk, moving.pk]

    def test_yoldagi_yuklar_come_last_and_are_not_dropped(self, admin_client, db):
        """Hammasi means hammasi — a sort must not quietly become a filter. A yuk
        with no kelgan kun has nowhere on the calendar, so it goes to the end."""
        _c, _bugun, _kecha, _week, moving = self._fleet()
        soon = make_shipment(contract=_c, kg="100", transport="01 111 YOL",
                             eta=date.today() + timedelta(days=1))
        rows = admin_client.get(
            "/shipments/", {"all": "1", "sort": "kelish"}).context["shipments"]
        # Both are there, behind everything that has landed, nearest ETA first.
        assert [s.pk for s in rows[-2:]] == [soon.pk, moving.pk]

    def test_the_blocks_are_days_and_bugun_and_kecha_are_named(self, admin_client, db):
        _c, bugun, kecha, week, moving = self._fleet()
        resp = admin_client.get("/shipments/", {"all": "1", "sort": "kelish"})
        groups = resp.context["groups"]
        assert [g["label"] for g in groups] == ["Bugun", "Kecha", "", "Hali kelmagan"]
        assert [[s.pk for s in g["shipments"]] for g in groups] == [
            [bugun.pk], [kecha.pk], [week.pk], [moving.pk]]
        # An older day carries no word, so the header prints the date itself.
        assert groups[2]["day"] == date.today() - timedelta(days=7)
        assert "Bugun" in resp.content.decode()

    def test_the_kelishuv_blocks_step_aside(self, admin_client, db):
        """The day is the grouping in this view, so the kelishuv header is not drawn
        — sorting the kelishuv blocks themselves would answer "which kelishuv had
        something land recently", which is a different question."""
        c, *_rest = self._fleet()
        html = admin_client.get(
            "/shipments/", {"all": "1", "sort": "kelish"}).content.decode()
        # The header band is a day now. The kelishuv is still named on each row and
        # inside the yuk's own panel — that is where the assertion has to stop.
        assert f'kelishuv-title">Kelishuv {c.code}' not in html
        assert "kelishuv-row--day" in html

    def test_hammasi_still_groups_by_kelishuv_when_the_sort_is_off(self, admin_client, db):
        c, *_rest = self._fleet()
        groups = admin_client.get("/shipments/", {"all": "1"}).context["groups"]
        assert [g["contract"].pk for g in groups] == [c.pk]

    def test_it_pages_by_yuk_rather_than_by_kelishuv(self, admin_client, db):
        c = _contract(kg="900000")
        for i in range(55):
            make_shipment(contract=c, kg="100", transport=f"01 {i:03d} AAA",
                          arrived=date.today() - timedelta(days=i),
                          status=ShipmentStatus.arrival())
        page = admin_client.get(
            "/shipments/", {"all": "1", "sort": "kelish"}).context["page"]
        assert page.paginator.count == 55 and len(page.object_list) == 50

    def test_a_search_keeps_the_sort(self, admin_client, db):
        _c, bugun, *_rest = self._fleet()
        html = admin_client.get(
            "/shipments/", {"all": "1", "sort": "kelish"}).content.decode()
        assert '<input type="hidden" name="sort" value="kelish">' in html
        # …and the sort keeps the search.
        rows = admin_client.get(
            "/shipments/", {"all": "1", "sort": "kelish", "q": "001"}).context["shipments"]
        assert [s.pk for s in rows] == [bugun.pk]

    def test_the_active_view_cannot_be_sorted_by_a_day_none_of_it_has_reached(
            self, admin_client, db):
        """Every arrived load is dropped before the sort could reach it, so there
        the param means nothing — and it is refused rather than ignored quietly,
        which is what keeps the kelishuv grouping on that view."""
        c, *_rest = self._fleet()
        resp = admin_client.get("/shipments/", {"sort": "kelish"})
        assert resp.context["sort"] == ""
        assert [g["contract"].pk for g in resp.context["groups"]] == [c.pk]

    def test_bojxona_tolanmagan_keeps_its_own_row_set(self, admin_client, db):
        self._fleet()
        resp = admin_client.get("/shipments/", {"all": "1", "customs": "1", "sort": "kelish"})
        assert resp.context["sort"] == ""

    def test_the_pill_is_drawn_on_hammasi_alone(self, admin_client, db):
        """Pressing it is what puts `sort=kelish` on the URL, so the link's own href
        is what says the button is there to press."""
        self._fleet()
        assert "sort=kelish" in admin_client.get(
            "/shipments/", {"all": "1"}).content.decode()
        assert "sort=kelish" not in admin_client.get("/shipments/").content.decode()
        assert "sort=kelish" not in admin_client.get(
            "/shipments/", {"all": "1", "customs": "1"}).content.decode()

    def test_leaving_hammasi_drops_the_sort(self, admin_client, db):
        self._fleet()
        html = admin_client.get(
            "/shipments/", {"all": "1", "sort": "kelish"}).content.decode()
        # The way out carries no sort — the active view would only throw it away.
        out = re.search(r'href="([^"]+)"[^>]*>\s*Faol yuklar', html)
        assert out and "sort" not in out.group(1)

    def test_a_typo_leaves_the_page_in_kelishuv_order(self, admin_client, db):
        c, *_rest = self._fleet()
        resp = admin_client.get("/shipments/", {"all": "1", "sort": "kelisj"})
        assert resp.context["sort"] == ""
        assert [g["contract"].pk for g in resp.context["groups"]] == [c.pk]


def test_the_old_done_url_now_lands_on_hammasi(admin_client, db):
    resp = admin_client.get("/shipments/done/")
    assert resp.status_code == 302 and resp.url == "/shipments/?all=1"


def test_hammasi_hides_money_from_translator(translator_client, admin_client, db):
    c = _contract()  # price 1.00/kg
    make_shipment(contract=c, kg="100", arrived=date.today(), status=ShipmentStatus.arrival())

    def content(html):
        return html.split('class="content"', 1)[1].split("</main>", 1)[0]

    tr = content(translator_client.get("/shipments/", {"all": "1"}).content.decode())
    assert "$" not in tr and "Qiymati" not in tr
    ad = content(admin_client.get("/shipments/", {"all": "1"}).content.decode())
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
    data = resp.json()
    assert data["status_id"] == other.pk and data["arrived"] is False
    # The re-rendered date cell drives the in-place swap; not arrived yet.
    assert "Yetib keldi" not in data["date_html"]
    s.refresh_from_db()
    assert s.status_id == other.pk

    arrival = ShipmentStatus.arrival()
    resp = admin_client.post(f"/shipments/{s.pk}/status/", {"status": arrival.pk},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    data = resp.json()
    assert data["status_id"] == arrival.pk and data["arrived"] is True
    assert "Yetib keldi" in data["date_html"]
    s.refresh_from_db()
    assert s.arrived is not None


def _expense_cell(html):
    """The Xarajat cell of the first load row (the inline panel below it also
    mentions expenses, so assertions have to target the column itself)."""
    return html.split('class="num load-expense"', 1)[1].split("</td>", 1)[0]


def test_loads_table_totals_expenses_after_transport(admin_client, translator_client, db):
    """Yuklar carries the load's xarajat total in its own column, right after
    the Transport and Haydovchi columns. It is money, so translators never see it."""
    from crm.models import ShipmentExpense
    c = _contract()
    s = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first(), transport="01A111AA", container="MSCU-1")
    s_line = ShipmentLine.objects.create(
        shipment=s, contract_line=c.lines.first(), kg=Decimal("100"))
    ShipmentExpense.objects.create(shipment=s, amount=Decimal("120.50"), category="road")
    ShipmentExpense.objects.create(shipment=s, amount=Decimal("79.50"), category="customs")

    html = admin_client.get("/shipments/").content.decode()
    assert (html.index("Transport</th>") < html.index("Haydovchi</th>")
            < html.index("Xarajat</th>") < html.index("Kelish</th>"))
    assert "$200" in _expense_cell(html) and "2 ta" in _expense_cell(html)

    tr = translator_client.get("/shipments/").content.decode()
    assert "Xarajat</th>" not in tr and "$200" not in tr


def test_loads_table_shows_driver_name_and_phone(admin_client, db):
    """The transport cell surfaces the driver + phone (as on the dashboard's
    Kechikayotgan yuklar), so the logist can call without opening the load."""
    c = _contract()
    s = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first(),
                                driver_name="Alisher Karimov", driver_phone="+998 90 123 45 67")
    ShipmentLine.objects.create(shipment=s, contract_line=c.lines.first(), kg=Decimal("100"))
    html = admin_client.get("/shipments/").content.decode()
    assert "Alisher Karimov" in html and "+998 90 123 45 67" in html


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


def test_progress_bar_only_appears_when_a_truck_plan_exists(admin_client, db):
    """Rejasiz kelishuvda bar bo'sh qutida yolg'iz raqam ko'rsatardi — yonidagi
    "1 yuk" chipi buni allaqachon aytadi, shuning uchun bar butunlay chiqmaydi."""
    c = _contract(kg="2000")
    make_shipment(contract=c, kg="100")
    assert "kelishuv-progress" not in admin_client.get("/shipments/").content.decode()

    ContractLine.objects.filter(contract=c).update(planned_trucks=2)
    html = admin_client.get("/shipments/").content.decode()
    assert "kelishuv-progress" in html and ">1/2<" in html


def _codes(field):
    return sorted(c.code for c in field.queryset)


def test_closed_kelishuv_is_not_offered_when_creating_a_yuk(db):
    """Hamma kg yo'lga chiqqan kelishuv yangi yukda tanlovda ko'rinmaydi."""
    from crm.forms import ShipmentForm

    open_c = _contract(kg="1000")
    closed = _contract(kg="1000")
    make_shipment(contract=closed, kg="1000")          # to'liq yuborilgan → yopiq

    codes = _codes(ShipmentForm().fields["contract"])
    assert open_c.code in codes and closed.code not in codes


def test_editing_a_yuk_keeps_its_own_kelishuv_selectable(db):
    """Tahrirlashda o'z kelishuvi yopiq bo'lsa ham tanlovda qoladi."""
    from crm.forms import ShipmentForm

    c = _contract(kg="1000")
    s = make_shipment(contract=c, kg="1000")           # kelishuvni yopadi
    assert c.code in _codes(ShipmentForm(instance=s).fields["contract"])


def test_fully_shipped_product_is_not_offered_as_a_lot(db):
    """Mahsulot qatorida ham to'liq yuborilgan mahsulot ko'rinmaydi."""
    from crm.forms import ShipmentLineForm

    c = _contract(kg="1000")
    ContractLine.objects.create(contract=c, brand="Bor", kg=Decimal("500"), price=Decimal("1"))
    make_shipment(contract=c, kg="1000",
                  contract_line=c.lines.first())        # birinchi mahsulot tugadi

    brands = [ln.brand for ln in ShipmentLineForm().fields["contract_line"].queryset]
    assert "Bor" in brands and "LLDPE" not in brands


def test_edit_modal_prefills_every_saved_value(admin_client, db):
    """Tahrirlash oynasi saqlangan qiymatlarni ko'rsatishi kerak."""
    c = _contract(kg="2000")
    s = make_shipment(contract=c, kg="100", sent=date(2026, 7, 8), eta=date(2026, 7, 19),
                      transport="01 777 AAA", container="MSKU 123456 7",
                      responsible="Otabek", driver_name="Akmal aka",
                      driver_phone="+998901112233", note="Izoh matni")

    html = admin_client.get(f"/shipments/{s.pk}/edit/").content.decode()
    for label, needle in [
        ("sent", 'value="2026-07-08"'), ("eta", 'value="2026-07-19"'),
        ("responsible", "Otabek"), ("driver_name", "Akmal aka"),
        ("driver_phone", "998901112233"), ("transport", "01 777 AAA"),
        ("container", "MSKU 123456 7"), ("note", "Izoh matni"),
    ]:
        assert needle in html, f"{label} ko'rinmadi"


def test_jonatilgan_sana_is_required(admin_client, db):
    """Sanasiz yuk oylik hisobotda 'jo'natilgan' bo'lib sanalmay qolardi."""
    c = _contract(kg="2000")
    resp = _post_shipment(admin_client, c, sent="")
    assert resp.status_code == 200 and not Shipment.objects.exists()

    assert _post_shipment(admin_client, c, sent="2026-07-08").status_code == 302
    assert Shipment.objects.get().sent == date(2026, 7, 8)


def test_an_old_yuk_without_a_sana_still_opens_for_editing(admin_client, db):
    """Model hali ham bo'sh sanaga ruxsat beradi — eski qatorlar tahrirlanadi,
    lekin saqlash uchun sana kiritilishi kerak."""
    c = _contract(kg="2000")
    s = make_shipment(contract=c, kg="100")            # sanasiz, to'g'ridan-to'g'ri
    assert s.sent is None
    assert admin_client.get(f"/shipments/{s.pk}/edit/").status_code == 200


class TestArrivalDateIsEditable:
    """Yetib kelgan sana is stamped as `today` by the status change, which is wrong
    every time a truck is marked in a day or two late — and until now there was no way
    to correct it: `arrived` was on no form at all.

    It is not a cosmetic date. `arrived_lots()` filters on it and nothing else, so it
    decides what is in the ombor, what a lot's kg count under in a month, and the FIFO
    order sales draw from."""

    def _arrived(self, admin_client):
        contract = _contract()
        _post_shipment(admin_client, contract)
        shipment = Shipment.objects.get()
        admin_client.post(f"/shipments/{shipment.pk}/status/",
                          {"status": ShipmentStatus.arrival().pk})
        shipment.refresh_from_db()
        assert shipment.arrived == date.today()      # stamped as today
        return shipment

    def _edit(self, admin_client, shipment, **extra):
        line = shipment.lines.first()
        data = {"contract": shipment.contract_id, "status": shipment.status_id,
                "sent": "2026-07-05", "eta": "2026-07-20", "transport": "01A111AA",
                "container": "MSCU-1", "note": "",
                **line_data({"id": line.pk, "contract_line": line.contract_line_id,
                             "kg": "400"}, initial=1)}
        data.update(extra)
        return admin_client.post(f"/shipments/{shipment.pk}/edit/", data)

    def test_the_field_appears_only_once_the_yuk_has_arrived(self, admin_client, db):
        """A date box beside a load still on the road is an invitation to type one,
        and a yuk carrying an arrival date while its holat says otherwise would sit in
        the ombor with its tannarx already counted."""
        contract = _contract()
        _post_shipment(admin_client, contract)
        shipment = Shipment.objects.get()
        html = admin_client.get(f"/shipments/{shipment.pk}/edit/").content.decode()
        assert 'name="arrived"' not in html

        admin_client.post(f"/shipments/{shipment.pk}/status/",
                          {"status": ShipmentStatus.arrival().pk})
        html = admin_client.get(f"/shipments/{shipment.pk}/edit/").content.decode()
        assert 'name="arrived"' in html

    def test_editing_it_moves_the_real_arrival_date(self, admin_client, db):
        shipment = self._arrived(admin_client)
        real = date.today() - timedelta(days=3)          # marked in three days late
        assert self._edit(admin_client, shipment,
                          arrived=real.isoformat()).status_code == 302
        shipment.refresh_from_db()
        assert shipment.arrived == real
        assert shipment.eta == date(2026, 7, 20)         # the plan is untouched

    def test_the_lot_stays_in_the_ombor_at_its_corrected_date(self, admin_client, db):
        """The date is what `arrived_lots` filters on, so a correction must not drop
        the lot off the shelf on its way through."""
        from crm.models import arrived_lots
        shipment = self._arrived(admin_client)
        real = date.today() - timedelta(days=3)
        self._edit(admin_client, shipment, arrived=real.isoformat())
        assert arrived_lots().filter(shipment=shipment).exists()

    def test_a_future_arrival_is_refused(self, admin_client, db):
        shipment = self._arrived(admin_client)
        was = shipment.arrived
        resp = self._edit(admin_client, shipment,
                          arrived=(date.today() + timedelta(days=1)).isoformat())
        assert resp.status_code == 200                   # re-rendered with the error
        shipment.refresh_from_db()
        assert shipment.arrived == was

    def test_arriving_before_departing_is_refused(self, admin_client, db):
        shipment = self._arrived(admin_client)
        was = shipment.arrived
        resp = self._edit(admin_client, shipment, sent="2026-07-05",
                          arrived="2026-07-01")
        assert resp.status_code == 200
        shipment.refresh_from_db()
        assert shipment.arrived == was

    def test_clearing_it_is_refused_while_the_holat_says_arrived(self, admin_client, db):
        shipment = self._arrived(admin_client)
        was = shipment.arrived
        assert self._edit(admin_client, shipment, arrived="").status_code == 200
        shipment.refresh_from_db()
        assert shipment.arrived == was


class TestEditKeepsHolatAndArrivalTogether:
    """The holat decides WHETHER a yuk has arrived, the date says WHEN — and the edit
    screen used to sync neither, so the two could disagree. `arrived_lots()` reads the
    date alone, which is what made the disagreement expensive."""

    def _payload(self, shipment, **extra):
        line = shipment.lines.first()
        data = {"contract": shipment.contract_id, "status": shipment.status_id,
                "sent": "2026-07-05", "eta": "2026-07-20", "transport": "", 
                "container": "", "note": "",
                **line_data({"id": line.pk, "contract_line": line.contract_line_id,
                             "kg": "400"}, initial=1)}
        data.update(extra)
        return data

    def test_setting_the_holat_to_arrival_stamps_a_date(self, admin_client, db):
        """Without this the load claimed to have landed and never reached the ombor."""
        from crm.models import arrived_lots
        contract = _contract()
        _post_shipment(admin_client, contract)
        shipment = Shipment.objects.get()

        admin_client.post(f"/shipments/{shipment.pk}/edit/",
                          self._payload(shipment, status=ShipmentStatus.arrival().pk))
        shipment.refresh_from_db()
        assert shipment.arrived == date.today()
        assert arrived_lots().filter(shipment=shipment).exists()

    def test_moving_off_arrival_clears_the_date(self, admin_client, db):
        """And without this the lot stayed on the shelf after going back on the road."""
        from crm.models import arrived_lots
        contract = _contract()
        _post_shipment(admin_client, contract)
        shipment = Shipment.objects.get()
        admin_client.post(f"/shipments/{shipment.pk}/status/",
                          {"status": ShipmentStatus.arrival().pk})
        shipment.refresh_from_db()

        on_road = ShipmentStatus.objects.exclude(is_arrival=True).first()
        admin_client.post(f"/shipments/{shipment.pk}/edit/",
                          self._payload(shipment, status=on_road.pk))
        shipment.refresh_from_db()
        assert shipment.arrived is None
        assert not arrived_lots().filter(shipment=shipment).exists()


class TestQrKod:
    """A QR kod is handed to SOME drivers as they leave Eron; those trucks land
    earlier. The yuklar table has to say which at a glance, so every row carries a
    marker: green once the kod is given, yellow while it is not."""

    def _shipment(self):
        contract = _contract()
        return make_shipment(contract=contract, kg="100")

    def test_qr_day_is_saved_from_the_yuk_form(self, admin_client, db):
        """The planned day is entered when the load is dispatched — it is known then,
        and nobody comes back to a saved yuk to add it."""
        contract = _contract()
        assert _post_shipment(admin_client, contract, qr_date="2026-07-08").status_code == 302
        assert Shipment.objects.get().qr_date == date(2026, 7, 8)

    def test_qr_day_is_optional(self, admin_client, db):
        """Most loads never get one, so leaving it blank has to be an ordinary save."""
        contract = _contract()
        assert _post_shipment(admin_client, contract).status_code == 302
        shipment = Shipment.objects.get()
        assert shipment.qr_date is None and shipment.has_qr is False

    def test_the_modal_asks_for_the_date_rather_than_assuming_today(self, admin_client, db):
        """The mark is often entered a day or two after the fact. A press that wrote
        today recorded a date that was simply wrong, and `qr_given` is read as the
        fact of WHEN it happened, so the real day has to be asked for."""
        shipment = self._shipment()
        admin_client.post(f"/shipments/{shipment.pk}/qr/", {"qr_given": "2026-08-07"})
        shipment.refresh_from_db()
        assert shipment.qr_given == date(2026, 8, 7)
        assert shipment.has_qr is True

    def test_the_field_opens_on_today_for_an_unmarked_load(self, admin_client, db):
        """The common case is still "this happened today", so that stays one click."""
        shipment = self._shipment()
        form = admin_client.get(f"/shipments/{shipment.pk}/qr/").context["form"]
        assert form.initial["qr_given"] == date.today()

    def test_the_field_opens_on_the_stored_date_once_marked(self, admin_client, db):
        """Reopening it is how a wrong date gets corrected, so it must show what is
        actually saved rather than resetting to today."""
        shipment = self._shipment()
        shipment.qr_given = date(2026, 8, 3)
        shipment.save(update_fields=["qr_given"])
        form = admin_client.get(f"/shipments/{shipment.pk}/qr/").context["form"]
        assert form.initial["qr_given"] == date(2026, 8, 3)

    def test_an_empty_date_takes_the_mark_back(self, admin_client, db):
        """The way back from a QR marked on the wrong yuk, without a full edit."""
        shipment = self._shipment()
        admin_client.post(f"/shipments/{shipment.pk}/qr/", {"qr_given": "2026-08-07"})
        admin_client.post(f"/shipments/{shipment.pk}/qr/", {"qr_given": ""})
        shipment.refresh_from_db()
        assert shipment.qr_given is None and shipment.has_qr is False

    def _row(self, client, shipment):
        """The opening <tr> of that load's row — where the marker classes live.
        No `qr` param, so the row is on screen whichever side it belongs to."""
        html = client.get("/shipments/?all=1").content.decode()
        return html.split(f'data-load="{shipment.pk}"', 1)[0].rsplit("<tr", 1)[1]

    def test_the_row_stays_yellow_until_the_kod_is_given(self, admin_client, db):
        """`has-qr` is what turns the left bar green; without it the CSS leaves it
        yellow, which is the ordinary case rather than a warning."""
        shipment = self._shipment()
        assert "load-row" in self._row(admin_client, shipment)
        assert "has-qr" not in self._row(admin_client, shipment)

        admin_client.post(f"/shipments/{shipment.pk}/qr/", {"qr_given": "2026-08-07"})
        assert "has-qr" in self._row(admin_client, shipment)

    def test_a_late_load_carries_both_marks_at_once(self, admin_client, db):
        """Late AND no QR is exactly the truck worth finding, so one colour must not
        replace the other — the bar splits and shows both."""
        shipment = self._shipment()
        shipment.eta = date.today() - timedelta(days=3)
        shipment.save(update_fields=["eta"])
        row = self._row(admin_client, shipment)
        assert "is-overdue" in row and "has-qr" not in row

        admin_client.post(f"/shipments/{shipment.pk}/qr/", {"qr_given": "2026-08-07"})
        row = self._row(admin_client, shipment)
        assert "is-overdue" in row and "has-qr" in row

    def test_a_translator_cannot_mark_a_qr(self, translator_client, db):
        """They read the marker; changing it is admin work, like holat beside it."""
        shipment = self._shipment()
        resp = translator_client.post(f"/shipments/{shipment.pk}/qr/")
        assert resp.status_code in (302, 403)
        shipment.refresh_from_db()
        assert shipment.qr_given is None

    def test_a_translator_sees_no_qr_button_but_still_reads_the_marker(
            self, translator_client, db):
        """A button that 403s on click is worse than no button. The bar is CSS on the
        row, so a tarjimon still sees which trucks carry a kod — they just cannot
        change it."""
        shipment = self._shipment()
        html = translator_client.get("/shipments/?all=1").content.decode()
        assert "act-qr" not in html
        assert "load-toggle" in html

        shipment.qr_given = date.today()
        shipment.save(update_fields=["qr_given"])
        assert "has-qr" in self._row(translator_client, shipment)


def test_only_a_tarjimon_gets_the_haydovchi_button(admin_client, translator_client, db):
    """The full dispatch modal already carries those four fields for an admin, so a
    second door to them only crowded a row that has five other actions. The view
    still serves both roles — this is the row saying whose job it is."""
    shipment = make_shipment(contract=_contract(), kg="100")
    href = f"/shipments/{shipment.pk}/driver/"
    assert href in translator_client.get("/shipments/").content.decode()
    assert href not in admin_client.get("/shipments/").content.decode()

    detail = f"/shipments/{shipment.pk}/"
    assert href in translator_client.get(detail).content.decode()
    assert href not in admin_client.get(detail).content.decode()


class TestQrFilter:
    """QR bor / QR yo'q on the yuklar page. Server-side: it has to combine with the
    holat tabs rather than replace whichever is active, and it has to reach past the
    Hammasi pager, which a client-side filter never could."""

    def _loads(self):
        contract = _contract(kg="5000")
        with_qr = make_shipment(contract=contract, kg="100")
        with_qr.qr_given = date.today()
        with_qr.save(update_fields=["qr_given"])
        without = make_shipment(contract=contract, kg="100")
        return with_qr, without

    def _rows(self, client, query=""):
        return [s.pk for s in client.get(f"/shipments/{query}").context["shipments"]]

    def test_bor_keeps_only_the_loads_carrying_a_kod(self, admin_client, db):
        with_qr, without = self._loads()
        assert self._rows(admin_client, "?qr=bor") == [with_qr.pk]

    def test_yoq_keeps_only_the_loads_without_one(self, admin_client, db):
        with_qr, without = self._loads()
        assert self._rows(admin_client, "?qr=yoq") == [without.pk]

    def test_no_filter_shows_both(self, admin_client, db):
        with_qr, without = self._loads()
        assert set(self._rows(admin_client)) == {with_qr.pk, without.pk}

    def test_an_unknown_value_filters_nothing(self, admin_client, db):
        """A typo in the URL should show every yuk, not silently drop half of them."""
        with_qr, without = self._loads()
        resp = admin_client.get("/shipments/?qr=nonsense")
        assert set(s.pk for s in resp.context["shipments"]) == {with_qr.pk, without.pk}
        assert resp.context["qr"] == ""

    def test_the_holat_counts_follow_the_filter(self, admin_client, db):
        """The tabs sit right under these buttons — counting the whole set there
        while the table below shows one row is the two disagreeing on screen."""
        with_qr, without = self._loads()
        status = with_qr.status
        resp = admin_client.get("/shipments/?qr=bor")
        counts = {t["status"].pk: t["count"] for t in resp.context["tabs"]}
        assert counts[status.pk] == 1
        assert resp.context["total"] == 1

    def test_it_combines_with_the_search_term(self, admin_client, db):
        """Both are server-side, so one must not throw the other away."""
        with_qr, without = self._loads()
        with_qr.transport = "TRUCK-XYZ"
        with_qr.save(update_fields=["transport"])
        assert self._rows(admin_client, "?qr=bor&q=XYZ") == [with_qr.pk]
        assert self._rows(admin_client, "?qr=yoq&q=XYZ") == []

    def test_the_active_pill_links_back_to_no_filter(self, admin_client, db):
        """Clicking the active one clears it, the way the holat tabs behave."""
        self._loads()
        html = admin_client.get("/shipments/?qr=bor&all=1").content.decode()
        # Active pill is not a ghost, and its href drops qr while keeping all=1.
        assert 'class="btn qr-filter"' in html
        assert 'href="?all=1"' in html

    def test_searching_keeps_the_filter_on(self, admin_client, db):
        """The search box posts as a GET form, so the filter rides along as a hidden
        field — otherwise typing a plate would quietly widen the set again."""
        self._loads()
        html = admin_client.get("/shipments/?qr=yoq").content.decode()
        assert '<input type="hidden" name="qr" value="yoq">' in html

    def test_the_empty_state_says_the_filter_is_why(self, admin_client, db):
        """Otherwise it reads "Faol yuklar yo'q" on a screen where the loads are one
        click away."""
        make_shipment(contract=_contract(), kg="100")     # exists, but has no kod
        html = admin_client.get("/shipments/?qr=bor").content.decode()
        assert "QR kod berilganlari orasida" in html

    def test_a_translator_gets_the_filter_too(self, translator_client, db):
        """They read the marker on every row, so they can narrow by it."""
        with_qr, without = self._loads()
        assert self._rows(translator_client, "?qr=bor") == [with_qr.pk]


class TestQrOverdue:
    """`qr_date` is the day the kod was MEANT to reach the driver, and nothing makes
    it so. Once that day is behind us with the kod still not handed over, the load is
    no longer "a truck without a kod" — it is a promise that was missed, and it needs
    saying, because on screen the two look identical."""

    def _load(self, qr_date=None, qr_given=None):
        shipment = make_shipment(contract=_contract(), kg="100")
        shipment.qr_date = qr_date
        shipment.qr_given = qr_given
        shipment.save(update_fields=["qr_date", "qr_given"])
        return shipment

    def test_a_passed_day_with_no_kod_is_late(self, db):
        assert self._load(qr_date=date.today() - timedelta(days=2)).qr_overdue is True

    def test_no_planned_day_is_never_late(self, db):
        """Most loads were never meant to get one. Without a plan there is nothing to
        miss, and calling those late would flag the whole fleet."""
        assert self._load().qr_overdue is False

    def test_today_is_not_yet_late(self, db):
        """The day itself is still the day it can happen — warning on the morning of
        would cry wolf on every load the moment it was dispatched."""
        assert self._load(qr_date=date.today()).qr_overdue is False

    def test_a_kod_that_arrived_is_not_late_however_late_it_was(self, db):
        """Once it is in the driver's hands the plan stops mattering. Marking a load
        given has to clear the warning even when it is marked days after the plan —
        which is the ordinary case, not the exception."""
        shipment = self._load(qr_date=date.today() - timedelta(days=5),
                              qr_given=date.today())
        assert shipment.qr_overdue is False

    def test_it_counts_the_days_since_the_promise(self, db):
        assert self._load(qr_date=date.today() - timedelta(days=3)).qr_days_late == 3

    def test_a_load_that_is_not_late_counts_zero(self, db):
        """Templates read this unconditionally, so it must not blow up on a load with
        no qr_date at all."""
        assert self._load().qr_days_late == 0

    def test_the_row_says_so(self, admin_client, db):
        """The bar is already how "no kod" is read, so the missed plan is said there
        and again in words beside the kechikdi badge — a colour alone cannot carry
        the difference between "never getting one" and "should have had one".
        """
        self._load(qr_date=date.today() - timedelta(days=4))
        html = admin_client.get("/shipments/?all=1").content.decode()
        assert "qr-late" in html
        assert "QR 4 kun kechikdi" in html

    def test_a_load_still_within_its_plan_says_nothing(self, db, admin_client):
        self._load(qr_date=date.today() + timedelta(days=2))
        html = admin_client.get("/shipments/?all=1").content.decode()
        assert "qr-late" not in html

    def test_the_pill_carries_the_count(self, admin_client, db):
        """The number is what makes the warning reachable from the default screen:
        the loads still waiting are on the other side of a filter nobody has pressed
        yet."""
        self._load(qr_date=date.today() - timedelta(days=1))
        self._load(qr_date=date.today() - timedelta(days=6))
        self._load(qr_date=date.today() + timedelta(days=1))     # still in plan
        self._load()                                             # never planned
        assert admin_client.get("/shipments/").context["qr_waiting_count"] == 2

    def test_the_count_survives_standing_on_qr_bor(self, admin_client, db):
        """Counted before the filter narrows anything. Filtering to the loads that
        HAVE a kod must not report zero still waiting — that is precisely the screen
        on which the number is the only thing speaking for the hidden rows."""
        self._load(qr_date=date.today() - timedelta(days=2))
        assert admin_client.get("/shipments/?qr=bor").context["qr_waiting_count"] == 1
