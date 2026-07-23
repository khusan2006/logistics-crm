from datetime import date, timedelta
from decimal import Decimal

from conftest import make_contract, make_shipment
from crm.models import Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus


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
