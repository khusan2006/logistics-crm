"""Browser check: SIP Toshkent / Logist show and hide the right boxes.

The server refuses a logist on a SIP yuk and a Logist kelishuv with no logist
(tests/test_contract_delivery.py). What only a browser can say is whether the
boxes that cannot apply are actually off the screen, and whether picking a Logist
kelishuv on a new yuk fills the logist for the operator.
"""
import os
from decimal import Decimal

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import Contract, ContractLine, Logist, Partner  # noqa: E402

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
    ctx = e2e_context(browser)
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def world(transactional_db):
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    logist = Logist.objects.create(name="Sardor", phone="+998901112233")
    hauled = Contract.objects.create(partner=partner, created="2026-07-01",
                                     delivery="logist", logist=logist)
    sip = Contract.objects.create(partner=partner, created="2026-07-02", delivery="sip")
    for contract in (hauled, sip):
        ContractLine.objects.create(contract=contract, brand="LLDPE",
                                    kg=Decimal("24000"), price=Decimal("1.00"))
    return {"logist": logist, "hauled": hauled, "sip": sip}


def _login(page, live_server, path):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}{path}")
    page.wait_for_timeout(250)


def _visible(page, selector):
    return page.eval_on_selector(selector, "el => !!el.offsetParent")


def test_kelishuv_form_shows_logist_only_for_logist(page, live_server, world):
    _login(page, live_server, "/contracts/new/")
    assert not _visible(page, "[name=logist]")
    page.select_option("[name=delivery]", value="logist")
    page.wait_for_timeout(100)
    assert _visible(page, "[name=logist]")
    page.select_option("[name=delivery]", value="sip")
    page.wait_for_timeout(100)
    assert not _visible(page, "[name=logist]")


def test_yuk_form_hides_logist_box_on_sip_and_fills_it_on_logist(page, live_server, world):
    _login(page, live_server, "/shipments/new/")
    page.select_option("[name=contract]", value=str(world["sip"].pk))
    page.wait_for_timeout(150)
    assert not _visible(page, "[name=logist]")
    assert not _visible(page, "[name=driver_advance]")

    page.select_option("[name=contract]", value=str(world["hauled"].pk))
    page.wait_for_timeout(150)
    assert _visible(page, "[name=logist]")
    assert page.input_value("[name=logist]") == str(world["logist"].pk)
