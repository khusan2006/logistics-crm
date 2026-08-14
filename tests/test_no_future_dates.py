"""Pul harakati kelajak sanasi bilan yozilmaydi.

Money moves when it moves; it cannot move next month. A future-dated row splits the
books in two: the kassa page counts up to the day you are looking at, while every
"how much is in the till" figure counts every row there is — so one to'lov dated
tomorrow makes the page show money another part of the app insists is already there.

Backdating stays allowed. Entering an old daftar with its real dates is the ordinary
case, and refusing it would be the worse bug.

A plain (non-AJAX) POST of an invalid form re-renders the page with 200 and the error
on it — the 422 belongs to the modal path (see crm/utils.py). So each test here asks
the question that actually matters: did the row get written?
"""
from datetime import timedelta

import pytest
from conftest import line_data, make_contract, make_lot, make_shipment, payment_rows
from django.utils import timezone

from crm.models import (
    Customer,
    CustomerPayment,
    Kapital,
    Logist,
    LogistPayment,
    Sale,
    Shipment,
    ShipmentExpense,
    ShipmentStatus,
    SupplierPayment,
)

ERROR = "kelajakda"


def tomorrow():
    return (timezone.localdate() + timedelta(days=1)).isoformat()


def yesterday():
    return (timezone.localdate() - timedelta(days=1)).isoformat()


def _customer():
    return Customer.objects.create(name="Alisher", phone="1")


def _arrived_lot(kg="1000", brand="LLDPE"):
    """A lot on the shelf — only an arrived yuk can be sold from."""
    return make_lot(kg=kg, brand=brand, status=ShipmentStatus.arrival(),
                    sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16")


def test_a_hamkor_tolovi_cannot_be_dated_tomorrow(admin_client, db):
    contract = make_contract(kg="9000")
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": tomorrow(), "currency": "usd", "amount": "100",
        "exchange_rate": "12000", "commission_percent": "", "method": "cash", "note": "",
    })
    assert not SupplierPayment.objects.exists()
    assert ERROR in resp.content.decode()


def test_the_same_tolov_dated_yesterday_goes_through(admin_client, db):
    """The guard is about tomorrow, not about "today only" — an old daftar is entered
    with the dates it really has."""
    contract = make_contract(kg="9000")
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": contract.pk, "date": yesterday(), "currency": "usd", "amount": "100",
        "exchange_rate": "12000", "commission_percent": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    assert SupplierPayment.objects.count() == 1


def test_a_mijoz_tolovi_cannot_be_dated_tomorrow(admin_client, db):
    customer = _customer()
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "100", "exchange_rate": "12000"},
        customer=customer, date=tomorrow()))
    assert not CustomerPayment.objects.exists()


def test_kapital_cannot_be_dated_tomorrow(admin_client, db):
    resp = admin_client.post("/kapital/new/", {
        "kind": "in", "date": tomorrow(), "currency": "usd", "amount": "50000",
        "exchange_rate": "12000", "method": "cash", "fee_percent": ""})
    assert not Kapital.objects.exists()
    assert ERROR in resp.content.decode()


def test_a_yuk_xarajati_cannot_be_dated_tomorrow(admin_client, db):
    shipment = make_shipment(kg="400")
    resp = admin_client.post("/expenses/new/", {
        "shipment": shipment.pk, "date": tomorrow(), "category": "transport",
        "currency": "usd", "amount": "50", "exchange_rate": "12000",
        "method": "cash", "fee_percent": "", "note": ""})
    assert not ShipmentExpense.objects.exists()
    assert ERROR in resp.content.decode()


def test_a_logist_tolovi_cannot_be_dated_tomorrow(admin_client, db):
    logist = Logist.objects.create(name="Bek", phone="1")
    resp = admin_client.post("/logist-payments/new/", {
        "logist": logist.pk, "date": tomorrow(), "currency": "usd", "amount": "100",
        "exchange_rate": "12000", "method": "cash", "fee_percent": "", "note": ""})
    assert not LogistPayment.objects.exists()
    assert ERROR in resp.content.decode()


def test_a_sotuv_cannot_be_dated_tomorrow(admin_client, db):
    """A sotuv books money AND takes granula off the shelf; neither happens tomorrow.

    Both sotuv forms are guarded — the marka-level one here, the single-lot one below,
    since they are two different classes and only one of them inherits the money mixin.
    """
    _arrived_lot(kg="1000", brand="LLDPE")
    resp = admin_client.post("/sales/new/", {
        "customer": _customer().pk, "currency": "usd", "exchange_rate": "12000",
        "date": tomorrow(), "debt_deadline": "", "note": "",
        **line_data({"brand": "LLDPE", "kg": "10", "price": "2"}),
    })
    assert not Sale.objects.exists()
    assert ERROR in resp.content.decode()


def test_a_sotuv_from_one_lot_cannot_be_dated_tomorrow(admin_client, db):
    lot = _arrived_lot(kg="1000")
    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer().pk, "kg": "100", "currency": "usd",
        "exchange_rate": "12000", "price": "2", "date": tomorrow(),
        "debt_deadline": "", "note": "",
    })
    assert not Sale.objects.exists()
    assert ERROR in resp.content.decode()


def test_a_muddat_is_still_allowed_to_be_in_the_future(admin_client, db):
    """`debt_deadline` is a future date by definition — guarding it would refuse the
    only kind of value it ever holds."""
    lot = _arrived_lot(kg="1000")
    deadline = (timezone.localdate() + timedelta(days=30)).isoformat()
    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": _customer().pk, "kg": "100", "currency": "usd",
        "exchange_rate": "12000", "price": "2", "date": yesterday(),
        "debt_deadline": deadline, "note": "",
    })
    assert resp.status_code == 302, resp.content[:400]
    assert Sale.objects.get().debt_deadline.isoformat() == deadline


def test_a_yuk_may_still_be_expected_next_month(admin_client, db):
    """An ETA is a plan. The only yuk date that cannot be in the future is `arrived`,
    and that guard was already there (crm/forms.py, ShipmentForm.clean)."""
    contract = make_contract(kg="9000")
    eta = (timezone.localdate() + timedelta(days=20)).isoformat()
    resp = admin_client.post("/shipments/new/", {
        "contract": contract.pk, "status": ShipmentStatus.objects.first().pk,
        "sent": yesterday(), "eta": eta, "responsible": "", "driver_name": "",
        "driver_phone": "", "transport": "01A111AA", "container": "", "note": "",
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "10",
        "lines-0-contract_line": contract.lines.first().pk, "lines-0-kg": "400",
        "lines-0-price": "", "lines-0-currency": "usd",
        "lines-0-exchange_rate": "12000", "lines-0-id": "",
    })
    assert resp.status_code == 302, resp.content[:400]
    assert Shipment.objects.get().eta.isoformat() == eta


@pytest.mark.parametrize("url", [
    "/supplier-payments/new/",
    "/customer-payments/new/",
    "/kapital/new/",
])
def test_the_date_picker_itself_stops_at_today(admin_client, db, url):
    """`max` is what makes the wrong date unpickable; the validator is what makes the
    rule true for anything that never went through a picker."""
    html = admin_client.get(url).content.decode()
    assert f'max="{timezone.localdate().isoformat()}"' in html


def test_the_guard_is_on_the_money_date_only(admin_client, db):
    """Sanity: the yuk form's own dates keep their meaning. `eta` is a plan and takes
    no `max`; a sotuv's `debt_deadline` is a muddat and takes none either."""
    html = admin_client.get("/shipments/new/").content.decode()
    assert 'name="eta"' in html
    assert f'name="eta" max="{timezone.localdate().isoformat()}"' not in html
