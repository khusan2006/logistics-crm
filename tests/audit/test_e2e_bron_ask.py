"""Browser check: the Brondan ushlansin box is only offered when there IS a bron.

Asked on every sotuv it is noise, and noise on a money form is how a box ends up
ticked without being read. Which mijoz and which marka are both pickers the
operator changes inside the modal, so this is entirely a client-side behaviour —
nothing server-side can see it.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (Contract, ContractLine, Customer, Reservation,  # noqa: E402
                        Partner, Shipment, ShipmentLine, ShipmentStatus)

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
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    for brand in ("LLDPE", "HDPE"):
        line = ContractLine.objects.create(contract=contract, brand=brand,
                                           kg=Decimal("20000"), price=Decimal("1.00"))
        sh = Shipment.objects.create(contract=contract, status=status,
                                     sent="2026-07-05", arrived="2026-07-16")
        ShipmentLine.objects.create(shipment=sh, contract_line=line,
                                    kg=Decimal("20000"))

    holder = Customer.objects.create(name="Bronli mijoz", phone="1", address="T")
    Customer.objects.create(name="Oddiy mijoz", phone="1", address="T")
    Reservation.objects.create(customer=holder, brand="LLDPE",
                               kg=Decimal("5000"), price=Decimal("1.50"))
    return holder


def _open(page, live_server):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/sales/new/")
    # The box starts HIDDEN — no mijoz picked yet — so wait on the form, not on it.
    page.wait_for_selector("[name=customer]")
    page.wait_for_selector("[name=draw_from_bron]", state="attached")
    page.wait_for_timeout(300)


def _asked(page):
    return page.locator("[name=draw_from_bron]").is_visible()


def _pick(page, customer=None, brand=None):
    if customer is not None:
        page.select_option("[name=customer]", label=customer)
    if brand is not None:
        page.select_option("[name=brand]", value=brand)
    page.wait_for_timeout(250)


def test_nothing_is_asked_before_a_mijoz_is_chosen(page, live_server, world):
    _open(page, live_server)
    assert not _asked(page)


def test_a_mijoz_with_no_bron_is_never_asked(page, live_server, world):
    _open(page, live_server)
    _pick(page, customer="Oddiy mijoz", brand="LLDPE")
    assert not _asked(page), "asked a mijoz who has booked nothing"


def test_the_bron_holder_is_asked_on_the_marka_they_booked(page, live_server, world):
    _open(page, live_server)
    _pick(page, customer="Bronli mijoz", brand="LLDPE")
    assert _asked(page)
    assert page.locator("[name=draw_from_bron]").is_checked()


def test_the_bron_holder_is_not_asked_on_a_marka_they_did_not_book(page, live_server, world):
    """Their LLDPE bron says nothing about HDPE."""
    _open(page, live_server)
    _pick(page, customer="Bronli mijoz", brand="HDPE")
    assert not _asked(page)


def test_changing_the_mijoz_puts_the_question_away_again(page, live_server, world):
    _open(page, live_server)
    _pick(page, customer="Bronli mijoz", brand="LLDPE")
    assert _asked(page)
    _pick(page, customer="Oddiy mijoz")
    assert not _asked(page), "the question stayed behind on a mijoz with no bron"


def test_a_hidden_box_does_not_stop_an_ordinary_sotuv(page, live_server, world):
    """The box posts nothing while hidden, which for a mijoz with no bron is the
    same as the answer being irrelevant — the sotuv must still go through."""
    from crm.models import Sale

    _open(page, live_server)
    _pick(page, customer="Oddiy mijoz", brand="LLDPE")
    page.fill("[name=kg]", "1000")
    page.fill("[name=price]", "2.00")
    # Scoped to the sotuv's own form: the sidebar's logout is a submit button too,
    # and it comes first in the DOM — a bare button[type=submit] logs you out.
    page.locator("[name=customer]").locator(
        "xpath=ancestor::form[1]").locator("button[type=submit]").click()
    page.wait_for_timeout(1200)
    assert Sale.objects.count() == 1
    assert Sale.objects.get().kg == Decimal("1000.000")
