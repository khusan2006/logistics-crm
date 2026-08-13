"""The one ‹ davr › bar: every list narrows by the same ?from&to window.

Each screen used to spell the period its own way — the kassa and hisobotlar on
?from&to, the two to'lov ro'yxati on ?date_from&date_to, and the rest not at all. A
window picked on one screen therefore meant nothing on the next, and a link copied
between them narrowed nothing while still looking filtered. These tests pin the shared
window down: one spelling, one control, on every list that has a date to filter by.
"""
import re
from datetime import date
from decimal import Decimal

import pytest
from conftest import make_contract, make_lot
from crm.models import (
    AuditLog,
    Customer,
    CustomerPayment,
    Sale,
    Shipment,
    ShipmentStatus,
    SupplierPayment,
)

# Every screen carrying the bar, and the key its rows arrive under.
PAGES = [
    ("/kassa/", None),
    ("/sales/", "page"),
    ("/shipments/", None),
    ("/contracts/", "rows"),
    ("/audit/", "page"),
    ("/customer-payments/", "page"),
    ("/supplier-payments/", "page"),
    ("/reports/", None),
]


@pytest.mark.parametrize("url,_rows_key", PAGES)
def test_every_list_carries_the_same_bar(admin_client, db, url, _rows_key):
    """One control, one markup, everywhere — not a pair of bare date inputs on some
    screens and a calendar on others."""
    html = admin_client.get(url).content.decode()
    assert "daterange-bar" in html
    # With no period at all the bar reads "Hammasi" and drops its arrows.
    assert "daterange--bare" in html

    named = admin_client.get(url, {"from": "2026-07-01", "to": "2026-07-31"})
    assert named.status_code == 200
    # A whole month is named by the month, on every screen alike.
    label = re.search(r'daterange-text">(.*?)</span>',
                      named.content.decode(), re.S).group(1).strip()
    assert label == "Iyul"


@pytest.mark.parametrize("url,_rows_key", PAGES)
def test_a_mistyped_period_narrows_nothing_instead_of_500ing(admin_client, db, url, _rows_key):
    """A querystring is typed by hand and lives in bookmarks; a stale one must not
    take the page down."""
    assert admin_client.get(url, {"from": "kecha", "to": "2026-13-45"}).status_code == 200


def _customer():
    return Customer.objects.create(name="Alisher", phone="1")


def test_sotuvlar_narrow_to_the_window(admin_client, db):
    customer = _customer()
    lot = make_lot(kg="5000")
    july = Sale.objects.create(customer=customer, line=lot, kg=Decimal("10"),
                               price=Decimal("1"), date=date(2026, 7, 10))
    august = Sale.objects.create(customer=customer, line=lot, kg=Decimal("10"),
                                 price=Decimal("1"), date=date(2026, 8, 10))

    listed = admin_client.get("/sales/", {"from": "2026-07-01", "to": "2026-07-31"})
    assert [s.pk for s in listed.context["page"].object_list] == [july.pk]
    assert august.pk not in [s.pk for s in listed.context["page"].object_list]


def test_kelishuvlar_narrow_by_kelishuv_sanasi(admin_client, db):
    july = make_contract(created="2026-07-05")
    august = make_contract(created="2026-08-05")

    rows = admin_client.get("/contracts/", {
        "from": "2026-07-01", "to": "2026-07-31", "state": ""}).context["rows"]
    assert [c.pk for c in rows] == [july.pk]
    assert august.pk not in [c.pk for c in rows]


def test_yuklar_narrow_on_the_date_the_row_prints(admin_client, db):
    """The load's own sana: the day it arrived, or the day it is expected while it is
    still moving — which is exactly what the Sana ustuni shows."""
    arrived = Shipment.objects.create(
        contract=make_contract(), status=ShipmentStatus.arrival(),
        eta="2026-06-01", arrived="2026-07-20")
    moving = Shipment.objects.create(
        contract=make_contract(), status=ShipmentStatus.objects.first(), eta="2026-07-25")
    later = Shipment.objects.create(
        contract=make_contract(), status=ShipmentStatus.objects.first(), eta="2026-08-25")

    listed = admin_client.get("/shipments/", {
        "all": "1", "from": "2026-07-01", "to": "2026-07-31"}).context["shipments"]
    pks = {s.pk for s in listed}
    assert arrived.pk in pks and moving.pk in pks
    assert later.pk not in pks


def test_a_load_with_no_date_at_all_is_not_invented_into_the_window(admin_client, db):
    """It has no place on a calendar, so a narrowed window leaves it out — with the
    filter off it is still there."""
    undated = Shipment.objects.create(
        contract=make_contract(), status=ShipmentStatus.objects.first())

    narrowed = admin_client.get("/shipments/", {
        "all": "1", "from": "2026-07-01", "to": "2026-07-31"}).context["shipments"]
    assert undated.pk not in {s.pk for s in narrowed}
    everything = admin_client.get("/shipments/", {"all": "1"}).context["shipments"]
    assert undated.pk in {s.pk for s in everything}


def test_audit_is_cut_by_calendar_day_not_by_midnight(admin_client, db):
    """`created_at__gte="2026-07-31"` reads as that day's midnight and would drop
    everything written during the last day of the window."""
    AuditLog.objects.create(action=AuditLog.Action.CREATE, target_type="Sotuv",
                            summary="ertalab")
    entry = AuditLog.objects.get()
    AuditLog.objects.filter(pk=entry.pk).update(
        created_at="2026-07-31 18:30:00+05:00")

    listed = admin_client.get("/audit/", {"from": "2026-07-01", "to": "2026-07-31"})
    assert [row.pk for row in listed.context["page"].object_list] == [entry.pk]


def test_an_old_date_from_link_still_means_what_it_meant(admin_client, db):
    """The two to'lov ro'yxati used to spell the window ?date_from&date_to. Those
    names are still read, so a bookmark saved before the rename keeps working."""
    customer = _customer()
    july = CustomerPayment.objects.create(customer=customer, date=date(2026, 7, 10),
                                          amount=Decimal("100"))
    CustomerPayment.objects.create(customer=customer, date=date(2026, 8, 10),
                                   amount=Decimal("100"))

    old = admin_client.get("/customer-payments/",
                           {"date_from": "2026-07-01", "date_to": "2026-07-31"})
    assert [p.pk for p in old.context["page"].object_list] == [july.pk]
    # …and the bar it draws is the same one the new spelling draws.
    assert old.context["daterange"]["is_month"] is True


def test_the_arrows_keep_the_other_filters_and_drop_the_page_number(admin_client, db):
    """Stepping to last month must not clear the hamkor you are looking at, and page 3
    of the old window is a page nobody asked for."""
    contract = make_contract()
    SupplierPayment.objects.create(contract=contract, date=date(2026, 7, 5),
                                   amount=Decimal("100"))

    bar = admin_client.get("/supplier-payments/", {
        "from": "2026-07-01", "to": "2026-07-31",
        "method": "cash", "q": "pars", "page": "3"}).context["daterange"]

    assert bar["prev_url"].startswith("/supplier-payments/?")
    for url in (bar["prev_url"], bar["next_url"], bar["all_url"]):
        assert "method=cash" in url and "q=pars" in url
        assert "page=" not in url
        assert "date_from=" not in url and "date_to=" not in url
    assert "from=2026-06-01" in bar["prev_url"] and "to=2026-06-30" in bar["prev_url"]
    # "Hammasi" removes the period rather than setting one that covers everything.
    assert "from=" not in bar["all_url"] and "to=" not in bar["all_url"]
