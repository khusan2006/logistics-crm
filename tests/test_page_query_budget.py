"""The screens that price granula must not query per row.

A tannarx is live: one lot's `landed_cost_per_kg` reaches into its truck's
xarajatlar, its kelishuv's kg and that kelishuv's hamkor to'lovlari, and one sotuv's
`cost_price` reaches through the SLICES it was costed against into all of the same.
Left unprefetched, a page asks those questions again for every row it draws — and
every figure still comes out RIGHT, so nothing else in this suite notices.

That is exactly how it broke once: `sold_kg` moved from the sotuv onto its slices
(`SaleLot`), the prefetch lists in views.py kept naming the old path, and three
screens quietly went to ten queries a row — ombor 476, doska 321, sotuvlar 976, on a
database of 89 sotuvlar. These budgets are the alarm for the next time that path
moves. They are ceilings with room in them, not measurements: a change that adds a
query is fine, one that adds a query PER ROW is what they catch.
"""
from decimal import Decimal

import pytest

from conftest import make_contract, make_shipment
from crm.models import Customer, Sale, ShipmentLine

pytestmark = pytest.mark.django_db


def _world(lots=6, sales_per_lot=2):
    """Enough rows that a per-row query is unmistakable: N lots, each sold from
    twice, each on its own kelishuv with a to'lov behind it."""
    customer = Customer.objects.create(name="Alisher", phone="+998901234567")
    for i in range(lots):
        contract = make_contract(brand=f"marka-{i}", kg="1000", price="1.00")
        make_shipment(contract=contract, kg="1000", arrived="2026-07-01")
        line = ShipmentLine.objects.filter(contract_line__contract=contract).first()
        for _ in range(sales_per_lot):
            Sale.objects.create(customer=customer, line=line, date="2026-07-10",
                                kg=Decimal("100"), price=Decimal("2.00"),
                                price_uzs=Decimal("24000"),
                                exchange_rate=Decimal("12000"))
    return customer


def test_the_ombor_does_not_query_per_lot(admin_client, django_assert_max_num_queries):
    """Every row prints a tannarx; the truck's xarajatlar and the kelishuv's
    to'lovlar behind it have to be loaded once for the page, not once per lot."""
    _world()
    with django_assert_max_num_queries(20):
        assert admin_client.get("/ombor/").status_code == 200


def test_the_sotuvlar_list_does_not_query_per_sotuv(admin_client,
                                                    django_assert_max_num_queries):
    """The list prints a tan narx and a foyda per row, both of which walk the
    sotuv's slices into their yuklar and kelishuvlar."""
    _world()
    with django_assert_max_num_queries(25):
        assert admin_client.get("/sales/").status_code == 200


#: The doska over EVERYTHING. It opens on this month now (`_dashboard_window`), and
#: `_world` dates its sotuvlar into Iyul 2026 — so plain "/" walks nought sotuvlar and
#: the Sotuvdan foyda path these budgets exist to guard is never entered at all. The
#: page still answers 200 in 51 queries and the test still passes, which is precisely
#: the way this file's docstring says the regression hid the first time. Asking for
#: hammasi is what puts the rows back under the ceiling.
DOSKA = "/?davr=all"


def test_the_doska_does_not_query_per_lot(admin_client, django_assert_max_num_queries):
    """Ombordagi qoldiq reads what is left on every lot — kg sold off it and kg
    returned to it — and both of those are read through the sotuv's slices."""
    _world()
    with django_assert_max_num_queries(75):
        assert admin_client.get(DOSKA).status_code == 200


def test_the_doska_budget_is_measuring_the_sotuvlar_it_thinks_it_is(admin_client):
    """The guard on the guard: a board reporting nought foyda is a board that never
    walked a sotuv, and a ceiling held by an empty page is not a ceiling."""
    _world()
    assert admin_client.get(DOSKA).context["sales_profit_total"] > 0


def test_the_budget_holds_as_the_table_grows(admin_client, django_assert_max_num_queries):
    """The real guard: doubling the rows must not double the queries. A budget that
    only ever sees six lots would pass just as happily on a per-row page."""
    _world(lots=12, sales_per_lot=2)
    with django_assert_max_num_queries(20):
        assert admin_client.get("/ombor/").status_code == 200
    with django_assert_max_num_queries(25):
        assert admin_client.get("/sales/").status_code == 200
    with django_assert_max_num_queries(75):
        assert admin_client.get(DOSKA).status_code == 200
