"""Browser check: a phone must not zoom the page when a box is tapped.

Safari zooms into any text field it focuses whose font is under 16px. The CRM is
opened from Telegram, and a zoomed page swallows the swipe that collapses an
in-app browser — so an operator who tapped a qidiruv box was shut inside it.

Nothing server-side can see this. The trigger is a COMPUTED font size on the
control the browser is about to focus, and whether a screen is a touch one is a
media query — both of them answers only a real browser has. So this probe asks a
touch context and a mouse one the same question and expects different answers:
16px where a tap opens a keyboard, the app's own 14px where it does not.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (Contract, ContractLine, Customer,  # noqa: E402
                        Partner, Shipment, ShipmentLine, ShipmentStatus)

from conftest import e2e_context  # noqa: E402

pw = pytest.importorskip("playwright.sync_api")

PASSWORD = "e2e-pass-123"

#: Every visible box on the form, with the figure the browser would zoom for.
FIELDS = """
  Array.from(document.querySelectorAll('input, select, textarea'))
    .filter(function (el) {
      return ['hidden', 'checkbox', 'radio', 'submit', 'button'].indexOf(el.type) === -1
        && el.offsetParent !== null;
    })
    .map(function (el) {
      return { name: el.name || el.className,
               size: parseFloat(getComputedStyle(el).fontSize) };
    })
"""


@pytest.fixture
def browser():
    with pw.sync_playwright() as b:
        chromium = b.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def world(transactional_db):
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    status = (ShipmentStatus.objects.filter(is_arrival=True).first()
              or ShipmentStatus.objects.create(name="Kelgan", order=1, is_arrival=True))
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    line = ContractLine.objects.create(contract=contract, brand="LLDPE",
                                       kg=Decimal("20000"), price=Decimal("1.00"))
    shipment = Shipment.objects.create(contract=contract, status=status,
                                       sent="2026-07-05", arrived="2026-07-16")
    ShipmentLine.objects.create(shipment=shipment, contract_line=line,
                                kg=Decimal("20000"))
    Customer.objects.create(name="Oddiy mijoz", phone="1", address="T")
    return contract


def _sale_form(browser, live_server, touch):
    """The sotuv form, opened on a phone or on a desktop.

    `has_touch` and `is_mobile` are what put the context behind
    `(hover: none) and (pointer: coarse)` — the query the fix is written against,
    and the reason it is not written against a width: a phone held sideways is
    wider than any breakpoint the stylesheet draws and zooms just the same."""
    ctx = e2e_context(
        browser,
        viewport={"width": 390, "height": 844} if touch else {"width": 1280, "height": 950},
        has_touch=touch, is_mobile=touch)
    page = ctx.new_page()
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/sales/new/")
    page.wait_for_selector("input[name$='-kg']")
    return ctx, page


def test_on_a_phone_no_box_is_under_the_size_that_zooms(browser, live_server, world):
    ctx, page = _sale_form(browser, live_server, touch=True)
    try:
        assert page.evaluate("() => matchMedia('(hover: none) and (pointer: coarse)').matches")
        fields = page.evaluate("() => " + FIELDS)
        assert fields, "the sotuv form came up with no boxes to check"
        too_small = [f for f in fields if f["size"] < 16]
        assert too_small == [], f"these would zoom the page on a tap: {too_small}"
    finally:
        ctx.close()


def test_on_a_desktop_the_boxes_keep_the_app_s_own_size(browser, live_server, world):
    """The fix is scoped to touch on purpose. 16px everywhere would loosen a form
    the operator reads a screenful of at a time on a laptop, for a zoom that
    machine never does."""
    ctx, page = _sale_form(browser, live_server, touch=False)
    try:
        sizes = {f["size"] for f in page.evaluate("() => " + FIELDS)}
        assert 14 in sizes, f"the 14px boxes are gone from the desktop form: {sizes}"
    finally:
        ctx.close()


def test_a_phone_is_not_handed_a_keyboard_it_did_not_ask_for(browser, live_server, world):
    """The other half of the same complaint. A box focused by script opens the
    keyboard over half the screen and zooms in on the way — so on touch nothing
    here takes focus, and the operator's own tap is what puts the caret in."""
    ctx, page = _sale_form(browser, live_server, touch=True)
    try:
        assert page.evaluate("() => window.opensKeyboard()")
        page.evaluate("() => { document.body.focus(); "
                      "window.focusField(document.querySelector(\"input[name$='-kg']\")); }")
        assert page.evaluate("() => document.activeElement.tagName") == "BODY"
    finally:
        ctx.close()


def test_a_desktop_still_gets_the_focus_it_always_had(browser, live_server, world):
    ctx, page = _sale_form(browser, live_server, touch=False)
    try:
        assert page.evaluate("() => !window.opensKeyboard()")
        page.evaluate("() => { document.body.focus(); "
                      "window.focusField(document.querySelector(\"input[name$='-kg']\")); }")
        assert page.evaluate("() => document.activeElement.name").endswith("-kg")
    finally:
        ctx.close()
