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
    other at all."""

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
