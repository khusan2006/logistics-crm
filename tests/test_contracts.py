from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from conftest import line_data, make_contract, make_shipment
from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus, SupplierPayment,
)


def _contract(**kw):
    partner = kw.pop("partner", None) or Partner.objects.create(name="Pars", phone="1", city="Tehron")
    defaults = dict(brand="LLDPE 209AA", kg="50000", price="0.96",
                    created="2026-07-01", deadline="2026-07-28")
    defaults.update(kw)
    return make_contract(partner=partner, **defaults)


def _ship(contract, kg="100", price="1.00"):
    """One truck under the kelishuv, priced so its goods_value is easy to read."""
    return make_shipment(contract=contract, kg=kg, price=price)


def _pay(contract, amount):
    return SupplierPayment.objects.create(contract=contract, amount=Decimal(amount))


def _listed(client, **params):
    resp = client.get("/contracts/", params)
    assert resp.status_code == 200
    return resp, [c.pk for c in resp.context["page"].object_list]


def test_total_value(db):
    c = _contract()
    assert c.total_value == Decimal("48000.00")
    # nothing shipped yet → nothing owed (debt accrues per shipped truck)
    assert c.shipped_value == Decimal("0")
    assert c.debt == Decimal("0")
    assert c.remaining_kg == Decimal("50000")


def test_create_via_view(admin_client, admin_user):
    p = Partner.objects.create(name="Arya", phone="1", city="Shiroz")
    resp = admin_client.post("/contracts/new/", {
        "partner": p.pk, "created": "2026-07-04", "deadline": "2026-08-05", "note": "",
        **line_data({"brand": "HDPE 7000F", "kg": "30000", "price": "1.05"}),
    })
    assert resp.status_code == 302
    c = Contract.objects.get(lines__brand="HDPE 7000F")
    assert c.created_by == admin_user


def test_deadline_before_created_rejected(admin_client):
    p = Partner.objects.create(name="X", phone="1", city="Y")
    resp = admin_client.post("/contracts/new/", {
        "partner": p.pk, "created": "2026-07-10", "deadline": "2026-07-01", "note": "",
        **line_data({"brand": "B", "kg": "10", "price": "1"}),
    })
    assert resp.status_code == 200 and not Contract.objects.exists()


def test_create_contract_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/contracts/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_contract_modal_post_valid_returns_204_with_redirect(admin_client):
    p = Partner.objects.create(name="Zamin", phone="1", city="Buxoro")
    resp = admin_client.post(
        "/contracts/new/",
        {
            "partner": p.pk, "created": "2026-07-05", "deadline": "2026-08-01", "note": "",
            **line_data({"brand": "LDPE 2100TN00", "kg": "20000", "price": "1.10"}),
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/contracts/"
    assert Contract.objects.filter(lines__brand="LDPE 2100TN00").exists()


def test_create_contract_modal_post_invalid_returns_422(admin_client):
    p = Partner.objects.create(name="Zamin", phone="1", city="Buxoro")
    resp = admin_client.post(
        "/contracts/new/",
        {
            "partner": p.pk, "created": "2026-07-10", "deadline": "2026-07-01", "note": "",
            **line_data({"brand": "B", "kg": "10", "price": "1"}),
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html


def _mixed_book(partner):
    """Four kelishuvlar covering every to'lov holati, incl. the not-yet-shipped case."""
    paid = _contract(partner=partner)
    _ship(paid), _pay(paid, "100")            # 100$ shipped, 100$ paid → qarz yo'q
    partial = _contract(partner=partner)
    _ship(partial), _pay(partial, "40")       # 60$ qarz
    unpaid = _contract(partner=partner)
    _ship(unpaid)                             # 100$ qarz, hech to'lov yo'q
    idle = _contract(partner=partner)         # yuk yuborilmagan → qarz ham yo'q
    return paid, partial, unpaid, idle


def test_filter_by_payment_status(admin_client, db):
    """To'lov holati comes from debt = shipped_value − paid_total. A kelishuv with
    nothing shipped owes nothing yet, so it matches none of the three chips — it
    only shows under Hammasi."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    paid, partial, unpaid, idle = _mixed_book(partner)

    assert _listed(admin_client, pay="paid")[1] == [paid.pk]
    assert _listed(admin_client, pay="partial")[1] == [partial.pk]
    assert _listed(admin_client, pay="unpaid")[1] == [unpaid.pk]
    assert set(_listed(admin_client)[1]) == {paid.pk, partial.pk, unpaid.pk, idle.pk}


def test_payment_chips_carry_counts(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    _mixed_book(partner)
    resp, _ = _listed(admin_client)
    counts = {t["key"]: t["count"] for t in resp.context["pay_tabs"]}
    assert counts == {"": 4, "paid": 1, "partial": 1, "unpaid": 1}


def test_chip_counts_reflect_the_other_filters(admin_client, db):
    """Counts are faceted: they narrow with partner/delivery/overdue/search, but the
    payment filter itself never shrinks its own chips."""
    a = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    b = Partner.objects.create(name="Arya", phone="2", city="Shiroz")
    _mixed_book(a)
    other = _contract(partner=b)
    _ship(other)                               # b: bitta to'lanmagan kelishuv

    resp, _ = _listed(admin_client, partner=b.pk, pay="unpaid")
    counts = {t["key"]: t["count"] for t in resp.context["pay_tabs"]}
    assert counts == {"": 1, "paid": 0, "partial": 0, "unpaid": 1}


def test_filter_by_partner(admin_client, db):
    a = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    b = Partner.objects.create(name="Arya", phone="2", city="Shiroz")
    mine = _contract(partner=a)
    _contract(partner=b)
    assert _listed(admin_client, partner=a.pk)[1] == [mine.pk]


def test_filter_by_delivery_state(admin_client, db):
    """Yakunlangan = hamma kg yuborilgan VA hamkorga qarz qolmagan. To'liq
    yuborilgan, lekin to'lanmagan kelishuv hali ham qolganlar orasida."""
    done = _contract(kg=Decimal("100"))
    _ship(done, kg="100"), _pay(done, "100")
    owed = _contract(kg=Decimal("100"))
    _ship(owed, kg="100")                      # yuborilgan, lekin qarz bor
    part = _contract(kg=Decimal("100"))
    _ship(part, kg="40")

    assert _listed(admin_client, delivery="sent")[1] == [done.pk]
    assert set(_listed(admin_client, delivery="open")[1]) == {owed.pk, part.pk}


def test_finished_kelishuvlar_are_hidden_by_default(admin_client, db):
    """Filtrsiz kirilganda faqat qolganlar ko'rinadi — `Hammasi` ataylab tanlanadi."""
    done = _contract(kg=Decimal("100"))
    _ship(done, kg="100"), _pay(done, "100")
    open_one = _contract(kg=Decimal("100"))

    assert _listed(admin_client)[1] == [open_one.pk]
    assert set(_listed(admin_client, delivery="")[1]) == {done.pk, open_one.pk}


def test_filter_overdue_needs_undelivered_kg(admin_client, db):
    """Muddati o'tgan chases what is still owed in goods — a kelishuv whose trucks
    all went out is not late even if its deadline has passed."""
    today = timezone.localdate()
    late = _contract(kg=Decimal("100"), created=today - timedelta(days=30),
                     deadline=today - timedelta(days=1))
    _ship(late, kg="40")
    done = _contract(kg=Decimal("100"), created=today - timedelta(days=30),
                     deadline=today - timedelta(days=1))
    _ship(done, kg="100")
    _contract(kg=Decimal("100"), created=today, deadline=today + timedelta(days=5))

    assert _listed(admin_client, overdue="1")[1] == [late.pk]


def test_filters_combine_with_search(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    hit = _contract(partner=partner, brand="LLDPE 209AA")
    _ship(hit)
    _pay(_contract(partner=partner, brand="LLDPE 100AA"), "0.01")   # boshqa holat
    other = _contract(partner=partner, brand="HDPE 7000F")
    _ship(other)

    assert _listed(admin_client, q="LLDPE", pay="unpaid")[1] == [hit.pk]


def test_filtered_list_does_not_query_per_contract(admin_client, db,
                                                   django_assert_max_num_queries):
    """The pay/delivery filters read shipments + payments off the prefetch, so the
    page cost stays flat instead of growing two queries per kelishuv."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    for _ in range(6):
        c = _contract(partner=partner)
        _ship(c), _pay(c, "10")
    with django_assert_max_num_queries(12):
        admin_client.get("/contracts/", {"pay": "partial"})
