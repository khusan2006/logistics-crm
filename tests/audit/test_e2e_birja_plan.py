"""Browser check: the birja kelishuv form draws what saving it would move.

A kelishuv struck earlier but entered later takes its place in the order, and the
trucks booked after it belong on it — which the form says while the sana is being
typed. The server side is a dry run (tests/test_birja_truck.py); what only a browser
can answer is whether the box refreshes as the form is filled in.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (Contract, ContractLine, Shipment,  # noqa: E402
                        ShipmentLine, ShipmentStatus, birja_partner)

from conftest import e2e_context  # noqa: E402

pw = pytest.importorskip("playwright.sync_api")

PASSWORD = "e2e-pass-123"
BRAND = "и 1561"


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = e2e_context(browser, viewport={"width": 1280, "height": 950})
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def world(transactional_db):
    """One birja kelishuv of 10.09 with a 20 t truck already booked on it."""
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    later = Contract.objects.create(partner=birja_partner(), created="2026-09-10")
    line = ContractLine.objects.create(contract=later, brand=BRAND,
                                       kg=Decimal("30000"), price=Decimal("1.10"))
    arrival = (ShipmentStatus.objects.filter(is_arrival=True).first()
               or ShipmentStatus.objects.create(name="Kelgan", order=9, is_arrival=True))
    truck = Shipment.objects.create(contract=later, status=arrival,
                                    sent="2026-09-12", arrived="2026-09-12")
    ShipmentLine.objects.create(shipment=truck, contract_line=line,
                                kg=Decimal("20000"))
    return {"later": later, "truck": truck}


def _open(page, live_server):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/birja/kelishuvlar/new/")
    # Attached, not visible: the box starts empty, and an empty div has no size.
    page.wait_for_selector("[data-plan-preview]", state="attached")


def _fill(page, created, kg="30000"):
    page.fill("[name=created]", created)
    page.fill("[name=lines-0-brand]", BRAND)
    page.fill("[name=lines-0-kg]", kg)
    page.fill("[name=lines-0-price]", "1.00")
    page.wait_for_timeout(1200)


def test_the_form_says_which_yuk_would_move(page, live_server, world):
    _open(page, live_server)
    _fill(page, "2026-09-07")
    box = page.locator("[data-plan-preview]")
    assert "qayta taqsimlanadi" in box.inner_text()
    assert f"#{world['truck'].pk}" in box.inner_text()
    if os.environ.get("E2E_SHOT_PLAN"):
        page.screenshot(path=os.environ["E2E_SHOT_PLAN"], full_page=True)
    # Still a dry run: nothing has been written while the operator looks at it.
    world["truck"].refresh_from_db()
    assert world["truck"].contract == world["later"]
    assert Contract.objects.count() == 1


def test_the_box_follows_the_sana_being_changed(page, live_server, world):
    """Dated after the truck went out it moves nothing, and the box empties."""
    _open(page, live_server)
    _fill(page, "2026-09-07")
    assert "qayta taqsimlanadi" in page.locator("[data-plan-preview]").inner_text()
    page.fill("[name=created]", "2026-09-20")
    page.wait_for_timeout(1200)
    assert page.locator("[data-plan-preview]").inner_text().strip() == ""


def test_saving_moves_the_yuk_the_box_named(page, live_server, world):
    _open(page, live_server)
    _fill(page, "2026-09-07")
    page.click("form.stacked button[type=submit]")
    page.wait_for_load_state("networkidle")
    world["truck"].refresh_from_db()
    assert world["truck"].contract == Contract.objects.exclude(
        pk=world["later"].pk).get()
