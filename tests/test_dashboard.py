from datetime import date, timedelta
from decimal import Decimal

from conftest import make_contract, make_shipment
from crm.models import SupplierPayment, Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus


def test_dashboard_kpis(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    c = Contract.objects.create(partner=partner, created="2026-07-01")
    c_line = ContractLine.objects.create(
        contract=c, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"))
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first(), eta=date.today() - timedelta(days=2))
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("400"))
    html = admin_client.get("/").content.decode()
    assert "kechikdi" in html.lower()
    assert "LLDPE" in html


def test_translator_redirected(translator_client):
    resp = translator_client.get("/")
    assert resp.status_code == 302 and resp.url == "/shipments/"


def _lot(contract, kg, sent, arrived=None, price=None):
    return make_shipment(contract=contract, kg=kg, price=price, sent=sent, arrived=arrived,
                         status=ShipmentStatus.arrival() if arrived else ShipmentStatus.objects.first())


def test_monthly_rows_count_trucks_sent_and_arrived(admin_client, db):
    """Oylik hisobot: jo'natilgan oy bo'yicha, yetib kelgan esa kelgan oy bo'yicha
    sanaladi — bitta yuk ikki xil oyga tushishi mumkin."""
    c = make_contract(kg="100000", price="1.00")
    _lot(c, "1000", sent="2026-06-20", arrived="2026-07-02")   # iyunda ketdi, iyulda keldi
    _lot(c, "2000", sent="2026-07-05", arrived="2026-07-20")
    _lot(c, "3000", sent="2026-07-28")                         # hali yo'lda

    rows = {r["month"]: r for r in admin_client.get("/").context["monthly"]}
    june, july = rows[date(2026, 6, 1)], rows[date(2026, 7, 1)]
    assert (june["sent"], june["arrived"]) == (1, 0)
    assert (july["sent"], july["arrived"]) == (2, 2)
    assert july["kg"] == Decimal("3000")                       # faqat kelganlari
    assert july["value"] == Decimal("3000.00")


def test_monthly_rows_are_newest_first_and_skip_empty_months(admin_client, db):
    c = make_contract(kg="100000", price="1.00")
    _lot(c, "1000", sent="2026-05-10", arrived="2026-05-15")
    _lot(c, "1000", sent="2026-07-10", arrived="2026-07-15")

    months = [r["month"] for r in admin_client.get("/").context["monthly"]]
    assert months == [date(2026, 7, 1), date(2026, 5, 1)]       # iyun umuman yo'q


def test_monthly_table_renders(admin_client, db):
    c = make_contract(kg="100000", price="1.00")
    _lot(c, "1000", sent="2026-07-10", arrived="2026-07-15")
    html = admin_client.get("/").content.decode()
    assert "Oylik" in html


def test_hamkor_qarzi_covers_every_kelishuv_not_just_shipped_goods(admin_client, db):
    """Dashboarddagi Hamkor qarzi butun kelishuv bo'yicha qoladigan to'lovni
    ko'rsatadi — faqat yo'lga chiqqan yuklarni emas."""
    c = make_contract(kg="1000", price="1.00")          # jami 1 000$
    make_shipment(contract=c, kg="200")                 # 200$ yuborildi

    resp = admin_client.get("/")
    assert c.debt == Decimal("200")                     # yuborilgani bo'yicha
    assert resp.context["debt_total"] == Decimal("1000")   # butun kelishuv bo'yicha


def test_hamkor_qarzi_drops_as_payments_land(admin_client, db):
    c = make_contract(kg="1000", price="1.00")
    SupplierPayment.objects.create(contract=c, date="2026-07-20",
                                   amount=Decimal("300"), method="cash")
    assert admin_client.get("/").context["debt_total"] == Decimal("700")


def test_kelishuvlar_chart_labels_every_marka(admin_client, db):
    """Grafik yorlig'i c.brand ni o'qirdi — u endi mahsulot qatorlarida."""
    c = make_contract(brand="2102 repak", kg="1000", price="1.00")
    ContractLine.objects.create(contract=c, brand="ftor oq", kg=Decimal("500"),
                                price=Decimal("1"))
    html = admin_client.get("/").content.decode()
    assert "2102 repak, ftor oq" in html


def test_yuk_holatlari_counts_trucks_per_hamkor(admin_client, db):
    """Har holat ostida qaysi hamkorning nechta mashinasi shu holatda ekani —
    yuklarni birma-bir sanab chiqish o'rniga."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    arya = Partner.objects.create(name="Arya", phone="2", city="S")
    loading = ShipmentStatus.objects.first()
    a = make_contract(partner=pars, kg="9000")
    b = make_contract(partner=arya, kg="9000")
    for _ in range(4):
        make_shipment(contract=a, kg="100", status=loading)
    make_shipment(contract=b, kg="100", status=loading)

    resp = admin_client.get("/")
    row = {r["status"].name: r for r in resp.context["status_rows"]}[loading.name]
    assert row["total"] == 5
    # eng ko'pi yuqorida, tenglashsa nom bo'yicha
    assert row["partners"] == [("Pars", 4), ("Arya", 1)]
    assert "4 ta" in resp.content.decode()


def test_yuk_holatlari_skips_statuses_with_no_yuk(admin_client, db):
    c = make_contract(kg="9000")
    used = ShipmentStatus.objects.first()
    make_shipment(contract=c, kg="100", status=used)
    names = [r["status"].name for r in admin_client.get("/").context["status_rows"]]
    assert names == [used.name]


def test_truck_plan_totals_per_hamkor(admin_client, db):
    """Hamkor bo'yicha jamlanadi: bir hamkorning bir necha kelishuvidagi qolgan
    mashinalar bitta qatorda."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    arya = Partner.objects.create(name="Arya", phone="2", city="S")
    a = make_contract(partner=pars, kg="9000")
    b = make_contract(partner=pars, kg="9000")
    c = make_contract(partner=arya, kg="9000")
    Contract.objects.filter(pk=a.pk).update(planned_trucks=3)
    Contract.objects.filter(pk=b.pk).update(planned_trucks=2)
    Contract.objects.filter(pk=c.pk).update(planned_trucks=1)
    make_shipment(contract=a, kg="100")                 # 3 dan 1 tasi ketdi

    resp = admin_client.get("/")
    assert resp.context["truck_plan_rows"] == [("Pars", 4), ("Arya", 1)]
    assert "4 ta" in resp.content.decode()


def test_truck_plan_skips_kelishuvlar_that_are_done_or_unplanned(admin_client, db):
    done = make_contract(kg="9000")
    Contract.objects.filter(pk=done.pk).update(planned_trucks=1)
    make_shipment(contract=done, kg="100")          # rejasi bajarildi
    make_contract(kg="9000")                        # rejasi yo'q

    assert admin_client.get("/").context["truck_plan_rows"] == []


def test_progress_chart_shows_the_kelishuvlar_that_actually_moved(admin_client, db):
    """Chart eng yangi 8 tasini emas, harakatdagilarini ko'rsatadi — aks holda
    yangi kelishuvlar bo'sh chiziqlar bilan chartni to'ldirib, yuk ketayotgan
    kelishuvlarni pastga surib yuboradi."""
    moving = make_contract(brand="Ketgan", kg="1000", price="1.00",
                           created="2026-01-01")          # eng eskisi
    make_shipment(contract=moving, kg="400")
    for i in range(9):                                     # 9 ta yangi, bo'sh
        make_contract(brand=f"Bo'sh {i}", kg="1000", price="1.00", created="2026-09-09")

    shown = admin_client.get("/").context["contracts"]
    assert moving.pk in [c.pk for c in shown]
    assert shown[0].pk == moving.pk                        # harakatdagisi birinchi


def test_progress_chart_says_when_it_is_showing_a_subset(admin_client, db):
    for i in range(10):
        make_contract(brand=f"K{i}", kg="1000", price="1.00")
    resp = admin_client.get("/")
    assert resp.context["contracts_shown"] == 8
    assert resp.context["contracts_total"] == 10
    assert "10 tadan 8 tasi" in resp.content.decode()


def test_monthly_sent_counts_every_load_with_a_date_in_that_month(admin_client, db):
    """To'qqizta yuk iyulda jo'natilsa, iyul qatorida 9 turishi kerak."""
    c = make_contract(kg="90000")
    for day in range(1, 10):
        make_shipment(contract=c, kg="100", sent=date(2026, 7, day))

    rows = {r["month"]: r for r in admin_client.get("/").context["monthly"]}
    assert rows[date(2026, 7, 1)]["sent"] == 9


def test_a_load_sent_in_another_month_lands_in_that_month(admin_client, db):
    """Iyunda jo'natilib iyulda kelgan yuk iyul 'jo'natilgan' iga kirmaydi —
    hisobot kamaygandek ko'rinishining eng ehtimolli sababi shu."""
    c = make_contract(kg="90000")
    for day in range(1, 9):
        make_shipment(contract=c, kg="100", sent=date(2026, 7, day))
    make_shipment(contract=c, kg="100", sent=date(2026, 6, 28), arrived=date(2026, 7, 3),
                  status=ShipmentStatus.arrival())

    rows = {r["month"]: r for r in admin_client.get("/").context["monthly"]}
    assert rows[date(2026, 7, 1)]["sent"] == 8      # iyulda jo'natilganlar
    assert rows[date(2026, 6, 1)]["sent"] == 1      # to'qqizinchisi iyunda
    assert rows[date(2026, 7, 1)]["arrived"] == 1   # lekin iyulda yetib kelgan
