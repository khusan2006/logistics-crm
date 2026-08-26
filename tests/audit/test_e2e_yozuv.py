"""Browser check: the page really does arrive in kiril, and stays in it.

tests/test_yozuv.py pins the alphabet and the wiring; neither can say whether the
thing works in a browser. Three questions only a real one can answer:

  * does the first paint show kiril, or does the operator watch it change alphabet
  * does a modal fetched after load get converted too — that is the MutationObserver,
    and it is the whole reason this is client-side rather than a response filter
  * does what the operator TYPED come back untouched, since that is the string on
    its way to the database

Run:
    TEST_DB_SUFFIX=_e2e .venv/bin/python -m pytest tests/audit/test_e2e_yozuv.py -q

Skipped where Playwright's chromium is not installed, like the other e2e probes.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (Contract, ContractLine, Partner,  # noqa: E402
                        Shipment, ShipmentLine, ShipmentStatus)

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
    # Deliberately NOT conftest.e2e_context: every other probe pins itself to lotin
    # so it can read the page it is testing. This one is about the default, so it
    # takes the app exactly as a new operator gets it.
    ctx = browser.new_context(viewport={"width": 1280, "height": 950})
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def world(transactional_db):
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    status = (ShipmentStatus.objects.filter(is_arrival=True).first()
              or ShipmentStatus.objects.create(name="Kelgan", order=1, is_arrival=True))
    partner = Partner.objects.create(name="Vazifadon", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    line = ContractLine.objects.create(contract=contract, brand="ftor oq",
                                       kg=Decimal("20000"), price=Decimal("1.00"))
    yuk = Shipment.objects.create(contract=contract, status=status,
                                  sent="2026-07-05", arrived="2026-07-16")
    ShipmentLine.objects.create(shipment=yuk, contract_line=line, kg=Decimal("20000"))
    return contract


def _login(page, live_server):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")


def test_the_page_arrives_in_kiril_with_nothing_left_hidden(page, live_server, world):
    """Kiril is the default and no switch has been touched. The body must be showing:
    it is hidden between parse and conversion, and a `finally` that failed to fire
    would leave the operator a blank screen rather than the wrong alphabet."""
    _login(page, live_server)
    page.goto(f"{live_server.url}/shipments/")
    page.wait_for_load_state("networkidle")

    assert page.locator("html").get_attribute("data-yozuv") == "kiril"
    assert page.locator("html").get_attribute("data-yozuv-wait") is None
    assert page.evaluate("getComputedStyle(document.body).visibility") == "visible"

    body = page.inner_text("body")
    assert "Юклар" in body and "Yuklar" not in body
    assert "Келишув" in body
    # ...and the app's own name is a name, in either script
    assert "GranulaLog" in body


def test_a_modal_fetched_after_load_is_converted_too(page, live_server, world):
    """The MutationObserver, and the reason none of this is a response filter: the
    yuk form is fetched long after the page it opens over was converted."""
    _login(page, live_server)
    page.goto(f"{live_server.url}/shipments/")
    page.wait_for_load_state("networkidle")
    page.click('a[href="/shipments/new/"]')
    page.wait_for_selector("select[data-line-source]")
    page.wait_for_timeout(300)

    modal = page.inner_text(".modal, dialog")
    assert "Маҳсулот" in modal and "Mahsulot" not in modal
    assert "Сақлаш" in modal
    # the narx box the kelishuv fills, labelled in kiril and still read-only
    assert "1 кг нархи" in modal
    assert page.locator('input[name$="-price"]').first.is_disabled()


def test_what_the_operator_types_is_left_in_lotin(page, live_server, world):
    """The rule with money behind it. An input's value is a string on its way to the
    database — transliterated on the way past, it would be stored in kiril and stop
    matching every row already filed under its lotin spelling."""
    _login(page, live_server)
    page.goto(f"{live_server.url}/shipments/")
    page.wait_for_load_state("networkidle")
    page.click('a[href="/shipments/new/"]')
    page.wait_for_selector("[name=driver_name]")

    page.fill("[name=driver_name]", "Qo'chqor aka")
    page.fill("[name=note]", "Yo'lda to'xtadi")
    page.wait_for_timeout(300)

    assert page.input_value("[name=driver_name]") == "Qo'chqor aka"
    assert page.input_value("[name=note]") == "Yo'lda to'xtadi"


def test_the_switcher_puts_the_page_back_into_lotin(page, live_server, world):
    _login(page, live_server)
    page.goto(f"{live_server.url}/shipments/")
    page.wait_for_load_state("networkidle")

    page.click("#yozuv-toggle")
    page.wait_for_load_state("networkidle")

    assert page.locator("html").get_attribute("data-yozuv") == "lotin"
    body = page.inner_text("body")
    assert "Yuklar" in body and "Юклар" not in body
    # and it holds across a navigation, not just the reload it did itself
    page.goto(f"{live_server.url}/ombor/")
    page.wait_for_load_state("networkidle")
    assert "Ombor" in page.inner_text("body")
