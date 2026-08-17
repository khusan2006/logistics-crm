"""Browser check: the Mahsulot picker on a hamkor to'lov shows only the chosen
kelishuv's products.

The <select> is rendered holding EVERY selectable kelishuv's products at once and
the page narrows it — no dependent AJAX. So the server can be perfectly right about
the pairing (tests/test_supplier_payments.py covers that `clean` refuses a product
from another kelishuv) and the operator can still be offered a marka that is not on
the kelishuv they picked. Only a browser can say whether the narrowing runs.
"""
import os
from decimal import Decimal

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import Contract, ContractLine, Partner  # noqa: E402

pw = pytest.importorskip("playwright.sync_api")

PASSWORD = "e2e-pass-123"


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def world(transactional_db):
    """Two kelishuvlar for one hamkor. The first carries two markalar so there is a
    real choice to narrow to; the second carries one, which is the case the page is
    supposed to answer for the operator."""
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    two = Contract.objects.create(partner=partner, created="2026-07-01")
    for brand in ("2102 campaund", "7000 campaund"):
        ContractLine.objects.create(contract=two, brand=brand,
                                    kg=Decimal("1000"), price=Decimal("1.00"))
    one = Contract.objects.create(partner=partner, created="2026-07-02")
    ContractLine.objects.create(contract=one, brand="ftor oq",
                                kg=Decimal("1000"), price=Decimal("1.00"))
    return {"two": two, "one": one}


def _open(page, live_server):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/supplier-payments/new/")
    page.wait_for_selector("[name=contract_line]")
    page.wait_for_timeout(250)


def _offered(page):
    """The markalar the operator can actually pick, placeholder excluded."""
    return page.eval_on_selector(
        "[name=contract_line]",
        "s => [...s.options].filter(o => o.value).map(o => o.textContent.trim())")


def _pick_contract(page, contract):
    page.select_option("[name=contract]", value=str(contract.pk))
    page.wait_for_timeout(250)


def test_only_the_chosen_kelishuvs_markalar_are_offered(page, live_server, world):
    _open(page, live_server)
    _pick_contract(page, world["two"])

    offered = _offered(page)
    assert any("2102 campaund" in o for o in offered)
    assert any("7000 campaund" in o for o in offered)
    # The other kelishuv's marka is rendered into the same <select> and must not
    # survive the narrowing — offered here, it is a to'lov the server will refuse.
    assert not any("ftor oq" in o for o in offered), offered


def test_switching_kelishuv_swaps_the_markalar(page, live_server, world):
    """The list is rebuilt from the full set each time, not whittled down — picking
    the second kelishuv after the first must put its marka back."""
    _open(page, live_server)
    _pick_contract(page, world["two"])
    _pick_contract(page, world["one"])

    offered = _offered(page)
    assert [o for o in offered if "ftor oq" in o], offered
    assert not any("campaund" in o for o in offered), offered


def test_a_one_marka_kelishuv_is_not_asked_the_question(page, live_server, world):
    """One option is not a choice. The server fills it in on save either way, so
    this only spares the click — but leaving it blank asks the operator to answer
    something with a single possible answer."""
    _open(page, live_server)
    _pick_contract(page, world["one"])

    assert page.input_value("[name=contract_line]") == str(
        world["one"].lines.first().pk)


def test_a_marka_chosen_before_the_kelishuv_changed_does_not_survive(page, live_server, world):
    """Picking a marka and THEN switching kelishuv must not leave the old marka
    selected — the pairing would be one the server refuses, discovered on save."""
    _open(page, live_server)
    _pick_contract(page, world["two"])
    page.select_option("[name=contract_line]",
                       value=str(world["two"].lines.first().pk))
    _pick_contract(page, world["one"])

    chosen = page.input_value("[name=contract_line]")
    assert chosen not in {str(ln.pk) for ln in world["two"].lines.all()}
