"""Browser check: grab-and-drag on a table that is wider than its box.

The whole feature is a browser behaviour — a cursor, a scroll position and a
suppressed click — so there is nothing for a unit test to hold. These drive the
real enhancer in base.html.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (Contract, ContractLine, Customer, Sale,  # noqa: E402
                        Shipment, ShipmentLine, ShipmentStatus, Partner)

pw = pytest.importorskip("playwright.sync_api")

PASSWORD = "e2e-pass-123"


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
    status = ShipmentStatus.objects.first() or ShipmentStatus.objects.create(
        name="Yo'lda", order=1)
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    line = ContractLine.objects.create(contract=contract, brand="LLDPE",
                                       kg=Decimal("96000"), price=Decimal("1.00"))
    shipment = Shipment.objects.create(contract=contract, status=status,
                                       sent="2026-07-05", arrived="2026-07-16")
    lot = ShipmentLine.objects.create(shipment=shipment, contract_line=line,
                                      kg=Decimal("96000"))
    for i in range(6):
        customer = Customer.objects.create(name=f"Mijoz {i}", phone="1", address="T")
        Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"),
                            price=Decimal("1.00"), date="2026-07-20")
    return lot


def _open(browser, live_server, width=560):
    """A narrow window, so the sotuvlar table is genuinely wider than its box."""
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/sales/")
    page.wait_for_selector(".table-wrap")
    page.wait_for_timeout(300)
    return ctx, page


def _drag(page, dx):
    box = page.locator(".table-wrap").first.bounding_box()
    y = box["y"] + 12                      # the header row, never a link
    page.mouse.move(box["x"] + box["width"] - 30, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 30 + dx, y, steps=8)
    page.mouse.up()
    page.wait_for_timeout(150)


def test_a_wide_table_offers_the_grab_cursor(browser, live_server, world):
    ctx, page = _open(browser, live_server)
    wrap = page.locator(".table-wrap").first
    assert wrap.evaluate("el => el.classList.contains('is-draggable')")
    assert wrap.evaluate("el => getComputedStyle(el).cursor") == "grab"
    ctx.close()


def test_a_table_that_fits_offers_nothing(browser, live_server, world):
    """A grab cursor on a table with nowhere to go is an offer the page cannot
    keep, so the class is only added when it can actually scroll."""
    ctx, page = _open(browser, live_server, width=1800)
    fits = page.evaluate("""() => {
      const w = document.querySelector('.table-wrap');
      return { scrollable: w.scrollWidth > w.clientWidth + 1,
               draggable: w.classList.contains('is-draggable') };
    }""")
    assert fits["scrollable"] is False
    assert fits["draggable"] is False
    ctx.close()


def test_dragging_scrolls_the_table_sideways(browser, live_server, world):
    ctx, page = _open(browser, live_server)
    before = page.locator(".table-wrap").first.evaluate("el => el.scrollLeft")
    _drag(page, -160)
    after = page.locator(".table-wrap").first.evaluate("el => el.scrollLeft")
    assert after > before, "dragging left should scroll the table right"
    ctx.close()


def test_the_cursor_closes_while_dragging_and_opens_again_after(browser, live_server, world):
    ctx, page = _open(browser, live_server)
    wrap = page.locator(".table-wrap").first
    box = wrap.bounding_box()
    y = box["y"] + 12
    page.mouse.move(box["x"] + box["width"] - 30, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 130, y, steps=6)
    page.wait_for_timeout(80)
    assert wrap.evaluate("el => el.classList.contains('is-dragging')")
    assert wrap.evaluate("el => getComputedStyle(el).cursor") == "grabbing"
    page.mouse.up()
    page.wait_for_timeout(120)
    assert not wrap.evaluate("el => el.classList.contains('is-dragging')")
    assert wrap.evaluate("el => getComputedStyle(el).cursor") == "grab"
    ctx.close()


def test_a_drag_that_ends_on_a_link_does_not_open_it(browser, live_server, world):
    """The reason the click has to be suppressed: every row in these tables is a
    link or opens a modal, so a drag across one would fire it on release."""
    ctx, page = _open(browser, live_server)
    link = page.locator(".table-wrap a[href]").first
    box = link.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 - 120,
                    box["y"] + box["height"] / 2, steps=8)
    page.mouse.up()
    page.wait_for_timeout(400)
    assert page.url.endswith("/sales/"), f"a drag navigated away to {page.url}"
    ctx.close()


def test_a_plain_click_on_a_link_still_works(browser, live_server, world):
    """The threshold earns its keep here: pressing and releasing without moving is
    still a click, however wide the table is."""
    ctx, page = _open(browser, live_server)
    page.locator(".table-wrap a[href]").first.click()
    page.wait_for_timeout(500)
    assert not page.url.endswith("/sales/"), "a plain click was swallowed"
    ctx.close()
