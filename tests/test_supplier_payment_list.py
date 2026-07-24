"""To'lovlar sahifasidagi filtrlar va saralash."""
from datetime import date
from decimal import Decimal

from conftest import make_contract
from crm.models import Partner, SupplierPayment


def _pay(contract, amount, day, method="cash", percent="0", note=""):
    return SupplierPayment.objects.create(
        contract=contract, date=date(2026, 7, day), amount=Decimal(amount),
        commission_percent=Decimal(percent), method=method, note=note)


def _listed(client, **params):
    resp = client.get("/supplier-payments/", params)
    assert resp.status_code == 200
    return resp, [p.pk for p in resp.context["page"].object_list]


def test_search_matches_kod_hamkor_and_izoh(admin_client, db):
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    arya = Partner.objects.create(name="Arya", phone="2", city="S")
    a = _pay(make_contract(partner=pars, kg="9000"), "100", 5, note="avans")
    b = _pay(make_contract(partner=arya, kg="9000"), "200", 6)

    assert _listed(admin_client, q="Arya")[1] == [b.pk]
    assert _listed(admin_client, q="avans")[1] == [a.pk]
    assert _listed(admin_client, q="pars-1")[1] == [a.pk]


def test_filter_by_hamkor_and_usul(admin_client, db):
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    arya = Partner.objects.create(name="Arya", phone="2", city="S")
    cash = _pay(make_contract(partner=pars, kg="9000"), "100", 5, method="cash")
    card = _pay(make_contract(partner=arya, kg="9000"), "200", 6, method="card")

    assert _listed(admin_client, partner=pars.pk)[1] == [cash.pk]
    assert _listed(admin_client, method="card")[1] == [card.pk]


def test_filter_by_date_range(admin_client, db):
    c = make_contract(kg="90000")
    early, late = _pay(c, "100", 3), _pay(c, "200", 25)
    assert _listed(admin_client, date_from="2026-07-10")[1] == [late.pk]
    assert _listed(admin_client, date_to="2026-07-10")[1] == [early.pk]


def test_sorting(admin_client, db):
    c = make_contract(kg="90000")
    small_late = _pay(c, "100", 25)
    big_early = _pay(c, "900", 3)

    assert _listed(admin_client, sort="-date")[1] == [small_late.pk, big_early.pk]
    assert _listed(admin_client, sort="date")[1] == [big_early.pk, small_late.pk]
    assert _listed(admin_client, sort="-amount")[1] == [big_early.pk, small_late.pk]
    assert _listed(admin_client, sort="amount")[1] == [small_late.pk, big_early.pk]


def test_the_page_totals_what_the_filters_left(admin_client, db):
    """Filtrlangan to'lovlarning jami summasi ko'rinadi."""
    c = make_contract(kg="90000")
    _pay(c, "100", 5, percent="10")      # kassadan 110
    _pay(c, "200", 6)                    # kassadan 200
    resp, _ = _listed(admin_client)
    assert resp.context["total_paid"] == Decimal("300")
    assert resp.context["total_out"] == Decimal("310.00")


def test_a_payment_without_a_vositachi_shows_a_dash(admin_client, db):
    """Vositachi ustuni bo'sh qatorda chiziqcha ko'rsatadi."""
    _pay(make_contract(kg="9000"), "100", 5, percent="0")
    html = admin_client.get("/supplier-payments/").content.decode()
    assert "topilmadi" not in html          # bo'sh holat matni qatorga tushmasin
    assert "—" in html


def test_empty_result_explains_the_filters(admin_client, db):
    _pay(make_contract(kg="9000"), "100", 5)
    html = admin_client.get("/supplier-payments/", {"q": "yo'q"}).content.decode()
    assert "tanlangan filtrlar bo'yicha" in html
