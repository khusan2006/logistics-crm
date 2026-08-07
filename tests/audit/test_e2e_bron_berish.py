"""Browser check: the Berish button on Bronlar actually hands the granula over.

Regression. The button lives inside a <form data-modal>, and the document click
handler treated ANY [data-modal] ancestor as a modal opener — so it cancelled the
submit, read the form's (non-existent) href and navigated to /reservations/null.
bindForm never reached the form either, because it only ever bound the one inside
the dialog. Nothing server-side can see this: the view is correct and never gets
called.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (Contract, ContractLine, Customer, Reservation,  # noqa: E402
                        Partner, Sale, Shipment, ShipmentLine, ShipmentStatus)

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
    ctx = browser.new_context(viewport={"width": 1600, "height": 900})
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def bron(transactional_db):
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    status = (ShipmentStatus.objects.filter(is_arrival=True).first()
              or ShipmentStatus.objects.create(name="Kelgan", order=1, is_arrival=True))
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    line = ContractLine.objects.create(contract=contract, brand="LLDPE",
                                       kg=Decimal("10000"), price=Decimal("1.00"))
    sh = Shipment.objects.create(contract=contract, status=status, sent="2026-07-05",
                                 arrived="2026-07-16")
    ShipmentLine.objects.create(shipment=sh, contract_line=line, kg=Decimal("10000"))
    customer = Customer.objects.create(name="Komoliddin", phone="1", address="T")
    # A narx already agreed, so Berish needs no extra input to go through.
    return Reservation.objects.create(customer=customer, brand="LLDPE",
                                      kg=Decimal("4000"), price=Decimal("1.50"))


def _open(page, live_server):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/reservations/")
    page.wait_for_selector(".row-inline-form")
    page.wait_for_timeout(300)


def test_berish_hands_the_granula_over(page, live_server, bron):
    _open(page, live_server)
    page.click(".row-inline-form button[type=submit]")
    page.wait_for_timeout(1200)

    assert "null" not in page.url, f"navigated to {page.url}"
    sale = Sale.objects.get()
    assert sale.customer_id == bron.customer_id
    assert sale.kg == Decimal("4000.000")
    bron.refresh_from_db()
    assert bron.fulfilled_kg == Decimal("4000.000")
    assert bron.status == Reservation.Status.CONVERTED


def test_berish_hands_over_only_the_kg_that_was_typed(page, live_server, bron):
    """The box is pre-filled with the most that can be given and is editable,
    because the mijoz often takes part of what they booked."""
    _open(page, live_server)
    box = page.locator(".row-inline-form input[name=kg]")
    box.fill("1500")
    page.click(".row-inline-form button[type=submit]")
    page.wait_for_timeout(1200)

    assert Sale.objects.get().kg == Decimal("1500.000")
    bron.refresh_from_db()
    assert bron.remaining_kg == Decimal("2500.000")
    assert bron.status == Reservation.Status.ACTIVE      # still owed the rest


def test_the_row_links_still_open_their_modals(page, live_server, bron):
    """The other half of the fix: openers are matched by [data-modal][href] now, so
    the links in the same row must still work."""
    _open(page, live_server)
    page.click("a[title='Tahrirlash']")
    page.wait_for_selector("#modal[open]")
    assert page.locator("#modal").is_visible()
    assert "null" not in page.url
