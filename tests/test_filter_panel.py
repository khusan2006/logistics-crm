"""Filtrlar paneli va faol filtr chiplari.

The selects used to sit in the search row: four boxes competing with the search input
and the davr bar, wrapping into a wall on a narrow screen — and once the page reloaded,
nothing said which of them was still on. Now the row carries one button with a count,
and what the list is currently narrowed BY is said as chips, each with its own ✕.

The rules worth pinning down: a chip names the label, not the raw id; a filter sitting
at its default is not a filter; and applying one never drops the search term or the
davr the user was already looking at.
"""
from decimal import Decimal

from conftest import make_contract
from crm.models import Partner, SupplierPayment


def _pay(contract, amount="100", day=5, method="cash"):
    return SupplierPayment.objects.create(
        contract=contract, date=f"2026-07-{day:02d}", amount=Decimal(amount),
        method=method)


def _panel(client, url, **params):
    resp = client.get(url, params)
    assert resp.status_code == 200
    return resp, resp.context["filters"]


def test_an_active_filter_becomes_a_chip_that_names_it(admin_client, db):
    """`?partner=7` says nothing to the person reading the page; "Hamkor: Pars" does."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    _pay(make_contract(partner=pars, kg="9000"))

    _resp, filters = _panel(admin_client, "/supplier-payments/", partner=pars.pk)
    assert [(c["label"], c["value"]) for c in filters["chips"]] == [("Hamkor", "Pars")]
    assert filters["count"] == 1


def test_a_filter_at_its_default_is_not_a_filter(admin_client, db):
    """Kelishuvlar open on Tugallanmagan and sort newest-first. Counting those as
    "2 filters" would make the badge meaningless — it must mean the list is narrower
    than it normally is."""
    make_contract(kg="9000")
    _resp, filters = _panel(admin_client, "/contracts/")
    assert filters["count"] == 0
    assert filters["chips"] == []

    _resp, narrowed = _panel(admin_client, "/contracts/", state="done")
    assert [(c["label"], c["value"]) for c in narrowed["chips"]] == [("Holat", "Tugallangan")]


def test_the_chips_x_removes_only_that_one(admin_client, db):
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    _pay(make_contract(partner=pars, kg="9000"))

    _resp, filters = _panel(admin_client, "/supplier-payments/",
                            partner=pars.pk, method="cash", q="pars", page="3")
    remove = {c["label"]: c["remove_url"] for c in filters["chips"]}
    assert "partner=" not in remove["Hamkor"]
    assert "method=cash" in remove["Hamkor"]
    # The search term survives; the stale page number does not.
    assert "q=pars" in remove["Hamkor"]
    assert "page=" not in remove["Hamkor"]


def test_tozalash_keeps_the_question_and_drops_the_narrowing(admin_client, db):
    """The search term and the davr are what is being asked; the panel is only how it
    was narrowed."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    _pay(make_contract(partner=pars, kg="9000"))

    _resp, filters = _panel(admin_client, "/supplier-payments/", partner=pars.pk,
                            method="cash", q="pars", **{"from": "2026-07-01", "to": "2026-07-31"})
    clear = filters["clear_url"]
    assert "q=pars" in clear and "from=2026-07-01" in clear and "to=2026-07-31" in clear
    assert "partner=" not in clear and "method=cash" not in clear


def test_the_panel_offers_every_filter_the_page_has(admin_client, db):
    _resp, filters = _panel(admin_client, "/supplier-payments/")
    assert [f["name"] for f in filters["fields"]] == ["partner", "method", "sort"]

    _resp, filters = _panel(admin_client, "/customer-payments/")
    assert [f["name"] for f in filters["fields"]] == ["customer", "method", "sort"]


def test_the_drawer_and_its_button_are_actually_on_the_page(admin_client, db):
    """The drawer markup and CSS sat in base.html/app.css unused — no template drew
    them. This is the test that they are wired up."""
    html = admin_client.get("/supplier-payments/").content.decode()
    assert 'id="filter-drawer"' in html
    assert "data-filter-open" in html and "data-filter-close" in html
    assert 'name="method"' in html


def test_applying_a_filter_does_not_drop_the_search_or_the_davr(admin_client, db):
    """The panel's form carries both as hidden fields; without them, choosing a usul
    would silently widen the list back out."""
    html = admin_client.get("/supplier-payments/", {
        "q": "pars", "from": "2026-07-01", "to": "2026-07-31"}).content.decode()
    drawer = html.split('id="filter-drawer"')[1]
    assert '<input type="hidden" name="q" value="pars">' in drawer
    assert '<input type="hidden" name="from" value="2026-07-01">' in drawer
    assert '<input type="hidden" name="to" value="2026-07-31">' in drawer


def test_the_search_row_carries_the_filters_the_other_way_round(admin_client, db):
    """Same rule in reverse: searching must not clear the filters that are on."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    html = admin_client.get("/supplier-payments/",
                            {"partner": pars.pk, "method": "cash"}).content.decode()
    row = html.split('class="searchbar"')[1].split("</form>")[0]
    assert f'name="partner" value="{pars.pk}"' in row
    assert 'name="method" value="cash"' in row


def test_a_page_without_filters_draws_no_panel(admin_client, db):
    """Sotuvlar has a search and a davr but no filter selects — it must not grow an
    empty Filtrlash button."""
    resp = admin_client.get("/sales/")
    body = resp.content.decode().split("</head>")[1]   # past base.html's own script
    assert "filters" not in resp.context or not resp.context["filters"]
    assert "filter-toggle" not in body
    assert 'id="filter-drawer"' not in body
