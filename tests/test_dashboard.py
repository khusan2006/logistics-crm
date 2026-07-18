from datetime import date, timedelta
from decimal import Decimal

from crm.models import Contract, Partner, Shipment, ShipmentStatus


def test_dashboard_kpis(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    c = Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("1000"),
                                price=Decimal("1"), created="2026-07-01", deadline="2026-08-01")
    Shipment.objects.create(contract=c, kg=Decimal("400"),
                            status=ShipmentStatus.objects.first(),
                            eta=date.today() - timedelta(days=2))
    html = admin_client.get("/").content.decode()
    assert "kechikdi" in html.lower()
    assert "LLDPE" in html


def test_translator_redirected(translator_client):
    resp = translator_client.get("/")
    assert resp.status_code == 302 and resp.url == "/shipments/"
