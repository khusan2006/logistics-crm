"""Mijoz to'lovlari sahifasidagi filtrlar va saralash."""
from datetime import date
from decimal import Decimal

from crm.models import Customer, CustomerPayment


def _customer(name="Alisher"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _pay(customer, amount, day, method="cash", percent="0", note=""):
    return CustomerPayment.objects.create(
        customer=customer, date=date(2026, 7, day), amount=Decimal(amount),
        fee_percent=Decimal(percent), method=method, note=note)


def _listed(client, **params):
    resp = client.get("/customer-payments/", params)
    assert resp.status_code == 200
    return resp, [p.pk for p in resp.context["page"].object_list]


def test_search_matches_mijoz_and_izoh(admin_client, db):
    a = _pay(_customer("Alisher Mebel"), "100", 5, note="avans")
    b = _pay(_customer("Bobur Plastik"), "200", 6)

    assert _listed(admin_client, q="Bobur")[1] == [b.pk]
    assert _listed(admin_client, q="avans")[1] == [a.pk]


def test_filter_by_mijoz_and_usul(admin_client, db):
    alisher, bobur = _customer("Alisher"), _customer("Bobur")
    cash = _pay(alisher, "100", 5, method="cash")
    card = _pay(bobur, "200", 6, method="card")

    assert _listed(admin_client, customer=alisher.pk)[1] == [cash.pk]
    assert _listed(admin_client, method="card")[1] == [card.pk]


def test_filter_by_date_range(admin_client, db):
    c = _customer()
    early, late = _pay(c, "100", 3), _pay(c, "200", 25)
    assert _listed(admin_client, date_from="2026-07-10")[1] == [late.pk]
    assert _listed(admin_client, date_to="2026-07-10")[1] == [early.pk]


def test_sorting(admin_client, db):
    c = _customer()
    small_late = _pay(c, "100", 25)
    big_early = _pay(c, "900", 3)

    assert _listed(admin_client, sort="-date")[1] == [small_late.pk, big_early.pk]
    assert _listed(admin_client, sort="date")[1] == [big_early.pk, small_late.pk]
    assert _listed(admin_client, sort="-amount")[1] == [big_early.pk, small_late.pk]
    assert _listed(admin_client, sort="amount")[1] == [small_late.pk, big_early.pk]


def test_a_mijoz_link_still_narrows_the_page(admin_client, db):
    """Mijoz sahifasidagi ?customer= havolasi filtr sifatida ishlaydi."""
    alisher, bobur = _customer("Alisher"), _customer("Bobur")
    mine = _pay(alisher, "100", 5)
    _pay(bobur, "200", 6)
    assert _listed(admin_client, customer=alisher.pk)[1] == [mine.pk]


def test_empty_result_explains_the_filters(admin_client, db):
    _pay(_customer(), "100", 5)
    html = admin_client.get("/customer-payments/", {"q": "yo'q"}).content.decode()
    assert "tanlangan filtrlar bo'yicha" in html
