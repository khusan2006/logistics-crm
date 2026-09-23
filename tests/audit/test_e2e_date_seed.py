"""Browser check: an empty sana box opens on this month and year.

A date input holds a whole date or nothing, so today's date is written in the moment
the box is clicked and the operator types the day over it. What makes that safe is
the other half: a seed nobody typed on is taken back out again — on blur and on
submit — because kutilgan sana, to'lov muddati and the kod's planned day all MEAN
something by being empty. Only a browser can say whether either half runs.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (Contract, ContractLine, Partner,  # noqa: E402
                        Shipment, ShipmentStatus)

from conftest import e2e_context  # noqa: E402

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
    ctx = e2e_context(browser, viewport={"width": 1280, "height": 950})
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def world(transactional_db):
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("20000"), price=Decimal("1.00"))
    if not ShipmentStatus.objects.exists():
        ShipmentStatus.objects.create(name="Yo'lda", order=1)
    return contract


def _open(page, live_server, path):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}{path}")
    page.wait_for_selector("input[type=date]")


def _month(value):
    return value[:7]


def test_clicking_an_empty_sana_box_opens_on_this_month(page, live_server, world):
    _open(page, live_server, "/shipments/new/")
    box = page.locator("[name=eta]")
    assert box.input_value() == ""
    box.click()
    assert _month(box.input_value()) == date.today().strftime("%Y-%m")


def test_a_day_typed_over_the_seed_is_kept(page, live_server, world):
    _open(page, live_server, "/shipments/new/")
    box = page.locator("[name=eta]")
    box.click()
    box.fill("2026-11-05")
    page.locator("[name=transport]").click()
    assert box.input_value() == "2026-11-05"


def test_a_seed_nobody_typed_on_goes_away_again(page, live_server, world):
    """Kutilgan sana means something by being empty — a stray click must not fill
    it in."""
    _open(page, live_server, "/shipments/new/")
    box = page.locator("[name=eta]")
    box.click()
    assert box.input_value() != ""
    page.locator("[name=transport]").click()
    assert box.input_value() == ""


def test_a_seed_never_travels_with_the_form(page, live_server, world):
    """Clicked, then saved straight away without the box being left — the yuk must
    land with no kutilgan sana on it."""
    _open(page, live_server, "/shipments/new/")
    page.select_option("[name=contract]", value=str(world.pk))
    page.select_option("[name=status]", index=1)
    page.select_option("[name=lines-0-contract_line]",
                       value=str(world.lines.get().pk))
    page.fill("[name=lines-0-kg]", "1000")
    page.locator("[name=sent]").click()
    page.locator("[name=sent]").fill("2026-09-20")
    page.locator("[name=eta]").click()              # seeded, nothing typed
    page.click("form.stacked button[type=submit]")
    page.wait_for_load_state("networkidle")
    shipment = Shipment.objects.latest("pk")
    assert shipment.eta is None
    assert str(shipment.sent) == "2026-09-20"


def test_a_box_that_already_has_a_date_is_left_alone(page, live_server, world):
    """The sotuv form's sana is filled by the server with today; clicking it must
    not rewrite it, and leaving it must not empty it."""
    _open(page, live_server, "/sales/new/")
    box = page.locator("[name=date]")
    was = box.input_value()
    assert was
    box.click()
    page.locator("[name=lines-0-kg]").click()
    assert box.input_value() == was
