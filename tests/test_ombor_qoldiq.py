"""Ombor qoldig'i at the end of a past day, opened from a Sotuvlar day band."""
from decimal import Decimal

from crm.models import Return, Sale
from tests.test_lot_detail import _customer, _lot, _sell


def _qoldiq(client, day):
    resp = client.get(f"/ombor/qoldiq/{day}/")
    assert resp.status_code == 200
    return {r["brand"]: r for r in resp.context["rows"]}


def test_qoldiq_walks_forward_day_by_day(admin_client, db):
    _lot("LDPE", "1000", "1.00", "2026-07-10")
    _lot("LDPE", "500", "1.00", "2026-07-20")           # arrives later
    _sell(admin_client, "LDPE", "400", "2026-07-12", _customer())
    sale = Sale.objects.get()
    Return.objects.create(sale=sale, kg=Decimal("100"), price=Decimal("3.00"),
                          date="2026-07-14")
    Return.objects.create(sale=sale, kg=Decimal("50"), price=Decimal("3.00"),
                          date="2026-07-14", restock=False)

    assert _qoldiq(admin_client, "2026-07-09") == {}
    assert _qoldiq(admin_client, "2026-07-10")["LDPE"]["qoldiq"] == Decimal("1000")
    assert _qoldiq(admin_client, "2026-07-12")["LDPE"]["qoldiq"] == Decimal("600")
    # only the restocked vazvrat flows back onto the shelf
    assert _qoldiq(admin_client, "2026-07-14")["LDPE"]["qoldiq"] == Decimal("700")
    assert _qoldiq(admin_client, "2026-07-20")["LDPE"]["qoldiq"] == Decimal("1200")


def test_sale_list_band_links_to_its_days_qoldiq(admin_client, db):
    _lot("PP", "1000", "1.00", "2026-07-10")
    _sell(admin_client, "PP", "400", "2026-07-12", _customer())
    html = admin_client.get("/sales/").content.decode()
    assert 'href="/ombor/qoldiq/2026-07-12/"' in html


def test_bad_day_is_404(admin_client, db):
    assert admin_client.get("/ombor/qoldiq/not-a-day/").status_code == 404


def test_skladchi_may_read_it_translator_may_not(skladchi_client, translator_client, db):
    assert skladchi_client.get("/ombor/qoldiq/2026-07-12/").status_code == 200
    assert translator_client.get("/ombor/qoldiq/2026-07-12/").status_code == 403
