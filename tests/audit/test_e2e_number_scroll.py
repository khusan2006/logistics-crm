"""Browser check: the mouse wheel must not turn the dial on a number input.

Every browser nudges a focused <input type=number> when the wheel rolls over it,
and app.css hides the spinners app-wide — so nothing on screen warns that the box
IS a dial. A wheel while reading down a long modal rewrote a figure that had just
been typed, silently, with the operator looking somewhere else.

There is nothing for a unit test to hold here: the bug and the fix are both a
browser default action. Playwright's wheel is a TRUSTED event, which is the only
kind that carries one, so this drives the real handler in base.html.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import Contract, ContractLine, Partner  # noqa: E402

pw = pytest.importorskip("playwright.sync_api")

PASSWORD = "e2e-pass-123"
NUMBER = "input[type=number]"
MONEY = "input[data-money]"


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def world(transactional_db):
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("96000"), price=Decimal("1.00"))
    return contract


def _open(browser, live_server):
    """The kelishuv form, which carries Nechta mashina — a plain number box."""
    ctx = browser.new_context(viewport={"width": 1100, "height": 900})
    page = ctx.new_page()
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/contracts/new/")
    page.wait_for_selector(NUMBER)
    return ctx, page


def _wheel_over(page, selector):
    box = page.locator(selector).first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 120)
    page.wait_for_timeout(120)


def test_the_wheel_leaves_a_focused_number_alone(browser, live_server, world):
    ctx, page = _open(browser, live_server)
    field = page.locator(NUMBER).first
    field.fill("24")
    field.focus()
    _wheel_over(page, NUMBER)
    assert field.input_value() == "24", "the wheel spun the dial"
    ctx.close()


def test_it_gives_up_focus_so_the_page_still_scrolls(browser, live_server, world):
    """Blur and not preventDefault: swallowing the wheel would freeze the modal
    under the pointer, which is a worse bug than the one being fixed."""
    ctx, page = _open(browser, live_server)
    field = page.locator(NUMBER).first
    field.fill("24")
    field.focus()
    _wheel_over(page, NUMBER)
    assert page.evaluate(
        "() => document.activeElement.type !== 'number'"), "kept the dial focused"
    ctx.close()


def test_an_unfocused_number_is_not_touched_either(browser, live_server, world):
    """The browser only spins a FOCUSED box, so this is the state the fix must not
    have to reach — and the value proves the wheel did nothing on its own."""
    ctx, page = _open(browser, live_server)
    field = page.locator(NUMBER).first
    field.fill("24")
    page.locator("body").click(position={"x": 5, "y": 5})
    _wheel_over(page, NUMBER)
    assert field.input_value() == "24"
    ctx.close()


def test_a_wheel_elsewhere_does_not_steal_focus(browser, live_server, world):
    """Blurring on ANY wheel would throw focus away while the operator scrolls some
    other part of the page — so the handler asks for focused AND under the pointer.
    """
    ctx, page = _open(browser, live_server)
    field = page.locator(NUMBER).first
    field.focus()
    page.mouse.move(20, 400)          # the sidebar, far from the field
    page.mouse.wheel(0, 120)
    page.wait_for_timeout(120)
    assert page.evaluate("() => document.activeElement.type === 'number'")
    ctx.close()


def test_money_boxes_were_never_dials(browser, live_server, world):
    """The grouping code retypes them as text to show "1 000 000", so the wheel has
    nothing to spin — this holds that the two mechanisms have not drifted."""
    ctx, page = _open(browser, live_server)
    money = page.locator(MONEY).first
    assert money.evaluate("el => el.type") == "text"
    money.fill("96 000")
    money.focus()
    _wheel_over(page, MONEY)
    assert money.input_value() == "96 000"
    assert page.evaluate("() => document.activeElement.hasAttribute('data-money')")
    ctx.close()
