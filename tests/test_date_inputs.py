"""Sana maydonlari tahrirlashda qiymatini yo'qotmasligi kerak.

<input type="date"> faqat ISO (yyyy-mm-dd) qiymatni tushunadi. Django esa mahalliy
formatda chiqarardi ("08.07.2026"), brauzer uni rad etib maydonni bo'sh
ko'rsatardi — va shu holda saqlansa, sana butunlay o'chib ketardi.
"""
import re
from datetime import date
from decimal import Decimal

import pytest

from conftest import make_contract, make_shipment
from crm.models import ShipmentStatus, SupplierPayment


def _date_inputs(html):
    return dict(re.findall(r'<input[^>]*type="date"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html))


def _all_date_inputs(html):
    """name -> value for every type=date input, whatever the attribute order."""
    out = {}
    for tag in re.findall(r"<input[^>]*>", html):
        if 'type="date"' not in tag:
            continue
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            out[name.group(1)] = value.group(1) if value else ""
    return out


ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_yuk_edit_keeps_its_dates(admin_client, db):
    c = make_contract(kg="2000")
    s = make_shipment(contract=c, kg="100", sent=date(2026, 7, 8), eta=date(2026, 7, 19))
    values = _all_date_inputs(admin_client.get(f"/shipments/{s.pk}/edit/").content.decode())
    assert values["sent"] == "2026-07-08"
    assert values["eta"] == "2026-07-19"


def test_kelishuv_edit_keeps_its_date(admin_client, db):
    c = make_contract(kg="1000", created="2026-03-04")
    values = _all_date_inputs(admin_client.get(f"/contracts/{c.pk}/edit/").content.decode())
    assert values["created"] == "2026-03-04"


def test_tolov_edit_keeps_its_date(admin_client, db):
    c = make_contract(kg="1000", price="1.00")
    p = SupplierPayment.objects.create(contract=c, date=date(2026, 5, 6),
                                       amount=Decimal("100"), method="cash")
    values = _all_date_inputs(admin_client.get(f"/supplier-payments/{p.pk}/edit/").content.decode())
    assert values["date"] == "2026-05-06"


@pytest.mark.parametrize("iso", ["2026-01-31", "2026-12-01"])
def test_every_rendered_date_is_iso(admin_client, db, iso):
    """Brauzer faqat ISO ni qabul qiladi — mahalliy format bo'sh maydon demak."""
    c = make_contract(kg="2000")
    y, m, d = (int(x) for x in iso.split("-"))
    s = make_shipment(contract=c, kg="100", sent=date(y, m, d), eta=date(y, m, d))
    values = _all_date_inputs(admin_client.get(f"/shipments/{s.pk}/edit/").content.decode())
    for name, value in values.items():
        if value:
            assert ISO.match(value), f"{name}={value!r} ISO emas"
