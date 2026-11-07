from decimal import Decimal

from crm.models import Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus


def _contract(kg="1000", brand="LLDPE"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    _contract_obj = Contract.objects.create(partner=partner, created="2026-07-01")
    _contract_obj_line = ContractLine.objects.create(
        contract=_contract_obj, brand=brand, kg=Decimal(kg), price=Decimal("1.00"))
    return _contract_obj


def _arrived_shipment(kg="400", brand="LLDPE"):
    c = _contract(brand=brand)
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.arrival(), sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal(kg))
    return _ship_obj_line


def _non_arrived_shipment(kg="200", brand="HDPE"):
    c = _contract(brand=brand)
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first(), sent="2026-07-05", eta="2026-08-01")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal(kg))
    return _ship_obj


def test_arrived_shipment_is_lot_with_full_available_kg(db):
    s = _arrived_shipment(kg="400")
    assert s.is_lot is True
    assert s.sold_kg == Decimal("0")
    # No reserved_kg on a lot: a bron is a claim on a MARKA across every kelishuv,
    # and it holds nothing back anyway — it is reported, never subtracted.
    assert s.returned_kg == Decimal("0")
    assert s.available_kg == s.kg == Decimal("400")


def test_non_arrived_shipment_is_not_a_lot(db):
    s = _non_arrived_shipment()
    assert s.is_lot is False


def test_ombor_lists_only_arrived_lots(admin_client, db):
    lot = _arrived_shipment(kg="400", brand="LLDPE")
    not_lot = _non_arrived_shipment(kg="200", brand="HDPE-Excluded")
    html = admin_client.get("/ombor/").content.decode()
    assert lot.brand in html
    assert not_lot.contract.brand_summary not in html


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/ombor/").status_code == 403


def test_admin_sees_lot_brand(admin_client, db):
    lot = _arrived_shipment(kg="400")
    resp = admin_client.get("/ombor/")
    assert resp.status_code == 200
    assert lot.brand in resp.content.decode()


def _lot(brand="LLDPE", kg="400", price=None, arrived="2026-07-16", partner="Pars"):
    """One arrived lot of `brand`, optionally at its own USD/kg (its landed cost)."""
    p = Partner.objects.create(name=partner, phone="1", city="T")
    c = Contract.objects.create(partner=p, created="2026-07-01")
    c_line = ContractLine.objects.create(
        contract=c, brand=brand, kg=Decimal("100000"), price=Decimal("1.00"))
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.arrival(), arrived=arrived)
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal(kg), price=Decimal(price) if price else None)
    return _ship_obj_line


def test_ombor_groups_lots_of_one_marka_into_a_single_row(admin_client, db):
    """The same granula arriving twice at different prices is ONE ombor row —
    the differing landed costs live on the lots inside it, not in the table."""
    cheap = _lot(brand="2102 kampaund", kg="48000", price="1.20", arrived="2026-07-19")
    dear = _lot(brand="2102 kampaund", kg="72000", price="1.30", arrived="2026-07-23")
    other = _lot(brand="7000 kampaund", kg="24000", price="1.36")

    resp = admin_client.get("/ombor/")
    groups = resp.context["page"].object_list
    by_brand = {g["brand"]: g for g in groups}
    assert set(by_brand) == {"2102 kampaund", "7000 kampaund"}

    merged = by_brand["2102 kampaund"]
    assert [lot.pk for lot in merged["lots"]] == [cheap.pk, dear.pk]   # FIFO order
    assert merged["kirim"] == Decimal("120000")
    assert merged["on_hand"] == Decimal("120000")
    assert merged["cost_min"] == Decimal("1.2000")
    assert merged["cost_max"] == Decimal("1.3000")
    assert by_brand["7000 kampaund"]["lots"] == [other]


def test_ombor_row_carries_every_lot_for_selling_separately(admin_client, db):
    """Each lot inside the row keeps its own Sotish link, locked to that lot."""
    cheap = _lot(brand="2102 kampaund", kg="48000", price="1.20", arrived="2026-07-19")
    dear = _lot(brand="2102 kampaund", kg="72000", price="1.30", arrived="2026-07-23")
    html = admin_client.get("/ombor/").content.decode()
    assert f"/sales/new/?lot={cheap.pk}" in html
    assert f"/sales/new/?lot={dear.pk}" in html
    assert "1.2" in html and "1.3" in html           # per-lot tan narx, no trailing zeros


def test_ombor_group_totals_net_out_sales(admin_client, db):
    from crm.models import Customer, Sale
    lot = _lot(brand="2102 kampaund", kg="1000", price="1.20")
    customer = Customer.objects.create(name="Ali")
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("400"),
                        price=Decimal("2"), date="2026-07-20")

    group = admin_client.get("/ombor/").context["page"].object_list[0]
    assert group["sold"] == Decimal("400") and group["on_hand"] == Decimal("600")


def test_ombor_search_still_matches_by_marka(admin_client, db):
    _lot(brand="2102 kampaund")
    _lot(brand="7000 kampaund")
    groups = admin_client.get("/ombor/", {"q": "2102"}).context["page"].object_list
    assert [g["brand"] for g in groups] == ["2102 kampaund"]


def test_marka_row_sotish_preselects_that_marka(admin_client, db):
    """The row-level Sotish is still the FIFO path — it just arrives with the marka
    already chosen; the per-lot Sotish links live inside the row."""
    _lot(brand="2102 kampaund", kg="1000", price="1.20")
    html = admin_client.get("/ombor/").content.decode()
    assert "/sales/new/?brand=2102%20kampaund" in html or "/sales/new/?brand=2102+kampaund" in html

    # The marka is a Mahsulot ROW now, so the shortcut prefills the first row
    # rather than the header.
    lines = admin_client.get("/sales/new/", {"brand": "2102 kampaund"}).context["lines"]
    assert lines.forms[0].initial["brand"] == "2102 kampaund"


class TestBugungiKurs:
    """The ombor's so'm tannarx is an ESTIMATE at one kurs typed on the page.

    Everywhere else a so'm figure is stated at the kurs its own row was booked at,
    so a past figure cannot move after the fact. Here that is the wrong answer:
    nearly every kelishuv is struck in dollars, and the question the ombor is opened
    with is "what would this cost me in so'm today, to decide what to sell it for
    today". Lots booked at different kursi are otherwise not comparable to each
    other at all.

    Goods bought IN SO'M are the exception: nobody paid dollars for them, so there is
    no dollar figure to re-rate, and their so'm tannarx is the so'm they cost."""

    def _som_lot(self, kg="1000", price_uzs="16600", price="1.3833", **ship):
        """A lot bought in so'm, the way birja granula is: the so'm narx is the typed
        figure and the dollar one only its twin at the row's 12 000."""
        from conftest import make_contract, make_lot
        contract = make_contract(brand="LLDPE", kg="100000", price=price,
                                 price_uzs=price_uzs, currency="uzs")
        ship.setdefault("arrived", "2026-07-16")
        ship.setdefault("status", ShipmentStatus.arrival())
        return make_lot(contract=contract, kg=kg, **ship)

    def test_a_lot_bought_in_som_keeps_the_som_it_cost(self, admin_client, db):
        som_lot = self._som_lot()
        usd_lot = _lot(brand="LLDPE", kg="1000", price="1.40", partner="Basir")
        for kurs in ("12000", "12500"):
            g = admin_client.get("/ombor/", {"kurs": kurs}).context["page"].object_list[0]
            by_pk = {lot.pk: lot for lot in g["lots"]}
            # Not 1.3833 × kurs: 16 599,60 at 12 000, 17 291,25 at 12 500.
            assert by_pk[som_lot.pk].cost_uzs == Decimal("16600.00")
            # The dollar lot still follows the box, and its dollar column is untouched.
            assert by_pk[usd_lot.pk].cost_uzs == Decimal("1.40") * Decimal(kurs)
            assert by_pk[som_lot.pk].landed_cost_per_kg == Decimal("1.3833")

    def test_its_xarajatlar_are_added_in_the_som_they_were_paid(self, admin_client, db):
        from crm.models import Currency, ShipmentExpense
        lot = self._som_lot(kg="1000")
        ShipmentExpense.objects.create(
            shipment=lot.shipment, date="2026-07-16", amount=Decimal("229.17"),
            amount_uzs=Decimal("2750000"), currency=Currency.UZS,
            exchange_rate=Decimal("12000"))
        g = admin_client.get("/ombor/", {"kurs": "13000"}).context["page"].object_list[0]
        # 16 600 + 2 750 000 / 1 000 kg
        assert g["lots"][0].cost_uzs == Decimal("19350.00")
        assert g["avg_cost_uzs"] == Decimal("19350.00")

    def test_so_does_a_truck_of_it_on_the_road(self, admin_client, db):
        lot = self._som_lot(kg="1000")
        road = self._som_lot(kg="2000", arrived=None, sent="2026-07-20",
                             status=ShipmentStatus.objects.first())
        road.contract_line = lot.contract_line
        road.save()
        g = admin_client.get("/ombor/", {"kurs": "12500"}).context["page"].object_list[0]
        assert [t.pk for t in g["transit"]] == [road.pk]
        assert g["transit"][0].cost_uzs == Decimal("16600.00")
        assert g["avg_all_uzs"] == Decimal("16600.00")

    def test_every_lot_is_converted_at_the_kurs_that_was_typed(self, admin_client, db):
        cheap = _lot(brand="LLDPE", kg="1000", price="1.00")
        dear = _lot(brand="LLDPE", kg="1000", price="2.00", partner="Basir")
        # Booked at the columns' own default, which is NOT what the page is asked for
        assert cheap.exchange_rate != Decimal("13100")

        group = admin_client.get("/ombor/", {"kurs": "13100"}).context["page"].object_list[0]
        assert group["cost_min_uzs"] == Decimal("13100.00")
        assert group["cost_max_uzs"] == Decimal("26200.00")
        assert {lot.cost_uzs for lot in group["lots"]} == {
            Decimal("13100.00"), Decimal("26200.00")}
        assert dear.pk in {lot.pk for lot in group["lots"]}

    def test_the_dollar_column_beside_it_is_untouched(self, admin_client, db):
        """The kurs is presentation only: the recorded figure is the dollar one and
        no row is re-rated by typing in the box."""
        lot = _lot(brand="LLDPE", kg="1000", price="1.40")
        group = admin_client.get("/ombor/", {"kurs": "13100"}).context["page"].object_list[0]
        assert group["cost_min"] == Decimal("1.4000")
        lot.refresh_from_db()
        assert lot.exchange_rate == Decimal("12000")   # nothing was written

    def test_a_kurs_typed_as_the_operator_types_money_is_understood(
            self, admin_client, db):
        """The box is a data-money input: what comes back is space-grouped, and this
        keyboard's decimal key is a comma."""
        _lot(brand="LLDPE", kg="1000", price="1.00")
        resp = admin_client.get("/ombor/", {"kurs": "13 100,5"})
        assert resp.context["kurs"] == Decimal("13100.5")

    def test_rubbish_in_the_box_falls_back_instead_of_erroring(self, admin_client, db):
        _lot(brand="LLDPE", kg="1000", price="1.00")
        for bad in ("", "abc", "0", "-5"):
            resp = admin_client.get("/ombor/", {"kurs": bad})
            assert resp.status_code == 200
            assert resp.context["kurs"] > 0

    def test_the_page_opens_where_it_was_left(self, admin_client, db):
        """Remembered in the session, so the operator does not retype today's kurs
        on every visit — and the Excel button, which carries the querystring, shows
        the same figures the screen does either way."""
        _lot(brand="LLDPE", kg="1000", price="1.00")
        admin_client.get("/ombor/", {"kurs": "13100"})
        assert admin_client.get("/ombor/").context["kurs"] == Decimal("13100")

    def test_the_excel_is_written_at_the_same_kurs(self, admin_client, db):
        """The querystring rides along with the download, so the file and the screen
        cannot disagree about what the stock is worth."""
        from io import BytesIO

        import openpyxl

        _lot(brand="LLDPE", kg="1000", price="1.00")
        resp = admin_client.get("/ombor/export.xlsx", {"kurs": "13100"})
        assert resp.status_code == 200
        sheet = openpyxl.load_workbook(BytesIO(resp.content)).worksheets[0]
        headers = [c.value for c in sheet[1]]
        row = [c.value for c in sheet[2]]
        assert row[headers.index("Tan narx eng past (so\'m)")] == 13100

    def test_a_skladchi_is_not_offered_the_box(self, client, django_user_model, db):
        """Tan narx is not a skladchi's, so neither is the kurs that draws it."""
        _lot(brand="LLDPE", kg="1000", price="1.00")
        user = django_user_model.objects.create_user(
            username="sklad", password="x", role=django_user_model.Role.SKLADCHI)
        client.force_login(user)
        assert 'name="kurs"' not in client.get("/ombor/").content.decode()


class TestShelfAverageTannarx:
    """The ombor's tannarx headline is the AVERAGE of what is on the shelf, weighted
    by each lot's qoldiq kg. The spread across every lot the marka ever came on is
    history — on a marka with a long run of trucks it says little about the kg still
    there to sell — so the average is read off the open lots only."""

    def _sell(self, lot, kg):
        from crm.models import Customer, Sale
        customer = Customer.objects.create(name=f"Mijoz {lot.pk}")
        Sale.objects.create(customer=customer, line=lot, kg=Decimal(kg),
                            price=Decimal("2"), date="2026-07-25")

    def test_weighted_by_qoldiq_and_blind_to_sold_out_lots(self, admin_client, db):
        gone = _lot(brand="LLDPE", kg="1000", price="1.00", arrived="2026-07-10")
        self._sell(gone, "1000")                       # sold out — not on the shelf
        _lot(brand="LLDPE", kg="3000", price="1.40", arrived="2026-07-12", partner="B")
        part = _lot(brand="LLDPE", kg="2000", price="1.60", arrived="2026-07-14",
                    partner="C")
        self._sell(part, "1000")                       # 1 000 kg left at 1.60

        g = admin_client.get("/ombor/", {"kurs": "12000"}).context["page"].object_list[0]
        # (3000 × 1.40 + 1000 × 1.60) / 4000 — not (1.40 + 1.60) / 2, and the
        # sold-out 1.00 lot plays no part.
        assert g["shelf_kg"] == Decimal("4000")
        assert g["avg_cost"] == Decimal("1.4500")
        assert g["avg_cost_uzs"] == Decimal("17400.00")
        # The history spread is still there for the Excel and the sold-out case.
        assert g["cost_min"] == Decimal("1.0000")

    def test_the_marka_row_prints_the_average(self, admin_client, db):
        _lot(brand="LLDPE", kg="3000", price="1.40")
        _lot(brand="LLDPE", kg="1000", price="1.60", partner="B")
        html = admin_client.get("/ombor/", {"kurs": "12000"}).content.decode()
        assert "1.45 $/kg" in html
        assert "17\u00a0400 so&#x27;m/kg" in html     # format_html escapes the '
        # The column says "o'rtacha" once, in its header, not again under each
        # figure (Sarvar, 2026-11-07).
        assert ">O'rtacha tan narx</th>" in html
        assert "<div class=\"muted load-sub\">o'rtacha" not in html
        # Nor the cheapest – dearest lot under it: "keremas" (Sarvar, 2026-11-07).
        assert "1.4 – 1.6 $/kg" not in html

    def test_a_sold_out_marka_averages_everything_it_came_in_at(self, admin_client, db):
        """Nothing on the shelf to average, so the row reads every lot the marka came
        on, weighted by kirim kg, not the min – max spread it printed before."""
        a = _lot(brand="LLDPE", kg="3000", price="1.00")
        b = _lot(brand="LLDPE", kg="1000", price="1.40", partner="B")
        self._sell(a, "3000")
        self._sell(b, "1000")
        resp = admin_client.get("/ombor/", {"kurs": "12000"})
        g = resp.context["page"].object_list[0]
        assert g["avg_cost"] is None
        # (3000 × 1.00 + 1000 × 1.40) / 4000, not (1.00 + 1.40) / 2.
        assert g["avg_kirim"] == Decimal("1.1000")
        assert g["avg_kirim_uzs"] == Decimal("13200.00")
        html = resp.content.decode()
        assert "1.1 $/kg" in html
        assert "barcha kirim bo'yicha" in html
        assert "1 – 1.4 $/kg" not in html

    def test_the_excel_carries_the_same_average(self, admin_client, db):
        from io import BytesIO

        import openpyxl

        _lot(brand="LLDPE", kg="3000", price="1.40")
        _lot(brand="LLDPE", kg="1000", price="1.60", partner="B")
        gone = _lot(brand="HDPE", kg="1000", price="1.00", partner="C")
        self._sell(gone, "1000")
        resp = admin_client.get("/ombor/export.xlsx", {"kurs": "12000"})
        sheet = openpyxl.load_workbook(BytesIO(resp.content)).worksheets[0]
        headers = [c.value for c in sheet[1]]
        rows = {r[0]: r for r in ([c.value for c in row] for row in sheet.iter_rows(min_row=2))}
        usd = headers.index("O'rtacha tan narx ($)")
        uzs = headers.index("O'rtacha tan narx (so'm)")
        assert (rows["LLDPE"][usd], rows["LLDPE"][uzs]) == (1.45, 17400)
        assert (rows["HDPE"][usd], rows["HDPE"][uzs]) == (1, 12000)


class TestTannarxCalculator:
    """The lot panel's tick boxes: every open lot, ticked, carrying its qoldiq kg and
    tannarx for the bar to average; trucks still on the road listed beside them,
    unticked, so the operator can see what the average becomes once they land."""

    def test_open_lots_carry_their_figures_and_start_ticked(self, admin_client, db):
        lot = _lot(brand="LLDPE", kg="3000", price="1.40")
        html = admin_client.get("/ombor/", {"kurs": "12000"}).content.decode()
        assert "data-pick-bar" in html
        assert 'data-kg="3000.000"' in html
        assert 'data-usd="1.4000"' in html
        assert 'data-uzs="16800.00"' in html
        assert f'aria-label="#{lot.pk} — tan narx' in html

    def test_a_finished_lot_has_no_box(self, admin_client, db):
        from crm.models import Customer, Sale
        live = _lot(brand="LLDPE", kg="1000", price="1.40")
        done = _lot(brand="LLDPE", kg="500", price="1.20", partner="B")
        Sale.objects.create(customer=Customer.objects.create(name="Ali"), line=done,
                            kg=Decimal("500"), price=Decimal("2"), date="2026-07-25")
        html = admin_client.get("/ombor/").content.decode()
        assert f'aria-label="#{live.pk} — tan narx' in html
        assert f'aria-label="#{done.pk} — tan narx' not in html

    def test_trucks_on_the_road_are_offered_unticked(self, admin_client, db):
        lot = _lot(brand="LLDPE", kg="1000", price="1.40")
        c = lot.contract_line.contract
        road = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first(),
                                       sent="2026-07-20", eta="2026-08-05")
        ShipmentLine.objects.create(shipment=road, contract_line=lot.contract_line,
                                    kg=Decimal("2000"), price=Decimal("1.50"))
        resp = admin_client.get("/ombor/", {"kurs": "12000"})
        g = resp.context["page"].object_list[0]
        assert [t.shipment_id for t in g["transit"]] == [road.pk]
        assert g["transit_trucks"] == 1
        assert g["transit"][0].cost_uzs == Decimal("18000.00")
        html = resp.content.decode()
        assert "1 yuk yo&#x27;lda" in html or "1 yuk yo'lda" in html
        assert 'data-transit' in html and 'data-kg="2000.000"' in html
        # The shelf average does not take the road into account on its own.
        assert g["avg_cost"] == Decimal("1.4000")

    def test_the_marka_row_can_count_the_road_in(self, admin_client, db):
        """The row opens on the shelf average; "+ yo'ldagi N yuk" swaps in the one
        with every truck on the road counted in, each by all it carries."""
        lot = _lot(brand="LLDPE", kg="1000", price="1.40")
        road = Shipment.objects.create(contract=lot.contract_line.contract,
                                       status=ShipmentStatus.objects.first(),
                                       sent="2026-07-20")
        ShipmentLine.objects.create(shipment=road, contract_line=lot.contract_line,
                                    kg=Decimal("2000"), price=Decimal("1.50"))
        resp = admin_client.get("/ombor/", {"kurs": "12000"})
        g = resp.context["page"].object_list[0]
        assert g["avg_cost"] == Decimal("1.4000")
        assert (g["road_kg"], g["all_kg"]) == (Decimal("2000"), Decimal("3000"))
        # (1 000 × 1.40 + 2 000 × 1.50) / 3 000
        assert g["avg_all"] == Decimal("1.4667")
        assert g["avg_all_uzs"] == Decimal("17600.00")
        html = resp.content.decode()
        assert 'data-road-toggle="1"' in html
        assert "+ yo'ldagi 1 yuk</button>" in html
        assert '<div data-avg-view="road" hidden>' in html
        assert "1.4667 $/kg" in html and "17 600 so&#x27;m/kg" in html

    def test_no_button_without_a_truck_on_the_road(self, admin_client, db):
        _lot(brand="LLDPE", kg="1000", price="1.40")
        html = admin_client.get("/ombor/").content.decode()
        assert 'data-road-toggle="' not in html       # the script names it; no button
        assert '<div data-avg-view="road"' not in html

    def test_a_skladchi_gets_no_calculator(self, client, django_user_model, db):
        lot = _lot(brand="LLDPE", kg="1000", price="1.40")
        road = Shipment.objects.create(contract=lot.contract_line.contract,
                                       status=ShipmentStatus.objects.first(),
                                       sent="2026-07-20")
        ShipmentLine.objects.create(shipment=road, contract_line=lot.contract_line,
                                    kg=Decimal("2000"))
        user = django_user_model.objects.create_user(
            username="sklad", password="x", role=django_user_model.Role.SKLADCHI)
        client.force_login(user)
        html = client.get("/ombor/").content.decode()
        assert "data-pick" not in html                 # no boxes, no bar, no script
        assert "yo'lda" not in html and "yo&#x27;lda" not in html
