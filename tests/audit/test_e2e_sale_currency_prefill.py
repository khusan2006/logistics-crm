"""Browser check: the sotuv modal opens on the valyuta the mijoz last took the marka
in, and fills that valyuta's narx.

The narx was already prefilled from the last sotuv (base.html, CustomerFactsSelect);
the valyuta stayed on the form's default, so a mijoz who always buys in so'm was
offered a dollar narx until the operator switched it. Picking the mijoz and the
marka are both client-side, so only a browser can say what the form does.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (Contract, ContractLine, Customer, Partner, Sale,  # noqa: E402
                        Shipment, ShipmentLine, ShipmentStatus)

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
    line = ContractLine.objects.create(contract=contract, brand="LLDPE",
                                       kg=Decimal("20000"), price=Decimal("1.00"))
    status = (ShipmentStatus.objects.filter(is_arrival=True).first()
              or ShipmentStatus.objects.create(name="Kelgan", order=1, is_arrival=True))
    shipment = Shipment.objects.create(contract=contract, status=status,
                                       sent="2026-07-05", arrived="2026-07-16")
    lot = ShipmentLine.objects.create(shipment=shipment, contract_line=line,
                                      kg=Decimal("20000"))
    for name, currency, usd, uzs in (("Somli mijoz", "uzs", "1.5", "19800"),
                                     ("Dollarli mijoz", "usd", "1.6", "21120")):
        customer = Customer.objects.create(name=name, phone="1", address="T")
        Sale.objects.create(customer=customer, line=lot, kg=Decimal("100"),
                            price=Decimal(usd), price_uzs=Decimal(uzs),
                            currency=currency, exchange_rate=Decimal("13200"),
                            date="2026-07-18")


def _open(page, live_server):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/sales/new/")
    page.wait_for_selector("[name=customer]", state="attached")
    page.wait_for_selector(".combobox-input")
    page.wait_for_timeout(300)


def _pick(page, customer=None, brand=None):
    if customer is not None:
        box = page.locator("[name=customer]").locator(
            "xpath=ancestor::div[contains(@class,'combobox')][1]")
        box.locator(".combobox-input").click()
        box.locator(".combobox-input").fill(customer)
        box.locator(".combobox-option", has_text=customer).first.click()
    if brand is not None:
        page.select_option("[name=lines-0-brand]", value=brand)
    page.wait_for_timeout(300)


def _currency(page):
    return page.evaluate(
        "() => window.pickedControl(document.querySelector('form.stacked') || document,"
        " '[data-money-currency]').value")


def _narx(page):
    return page.input_value("[name=lines-0-price]").replace(" ", "").replace(" ", "")


def test_a_mijoz_who_buys_in_som_opens_on_som(page, live_server, world):
    _open(page, live_server)
    _pick(page, customer="Somli mijoz", brand="LLDPE")
    assert _currency(page) == "uzs"
    assert _narx(page) == "19800"


def test_a_mijoz_who_buys_in_dollars_opens_on_dollars(page, live_server, world):
    _open(page, live_server)
    _pick(page, customer="Somli mijoz", brand="LLDPE")
    _pick(page, customer="Dollarli mijoz")
    assert _currency(page) == "usd"
    assert _narx(page) == "1.6"


def test_a_valyuta_the_operator_picked_is_kept(page, live_server, world):
    """Switched back to dollars by hand, the form stays there when the mijoz
    changes — the prefill was a starting point, not a rule."""
    _open(page, live_server)
    _pick(page, customer="Somli mijoz", brand="LLDPE")
    assert _currency(page) == "uzs"
    page.select_option("[data-money-currency]", value="usd")
    page.wait_for_timeout(300)
    _pick(page, customer="Dollarli mijoz")
    _pick(page, customer="Somli mijoz")
    assert _currency(page) == "usd"
    assert _narx(page) == "1.5"
