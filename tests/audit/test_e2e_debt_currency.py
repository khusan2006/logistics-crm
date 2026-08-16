"""Browser check: the kurs box on a mijoz to'lov.

The server already refuses a crossing row that carries no rate — that is covered in
tests/test_customer_payments.py. What no unit test can see is whether the SCREEN
agrees with it: a box that stays visible on a face-value row asks the operator for a
figure that decides nothing, and one that hides on a crossing row hides the only
figure that does. This drives the real enhancer in base.html to check both.
"""
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import Customer  # noqa: E402

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
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def world(transactional_db):
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    return Customer.objects.create(name="Ikki Valyuta Qarzdor", phone="1", address="T")


def _open(page, live_server, world):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/customer-payments/new/")
    page.wait_for_selector("[name=debt_currency]")
    page.wait_for_timeout(250)


def _rate(page):
    return page.locator("[name='form-0-exchange_rate']")


def _pick(page, debt=None, arrived=None):
    if debt is not None:
        page.select_option("[name=debt_currency]", debt)
    if arrived is not None:
        page.select_option("[name='form-0-currency']", arrived)
    page.wait_for_timeout(250)


def test_no_qarz_named_still_asks_for_a_kurs(page, live_server, world):
    """"Avtomatik" is a decision, not a gap: the money may land on either currency's
    debt, so the rate still decides how much of one it clears."""
    _open(page, live_server, world)
    assert _rate(page).is_visible(), "Avtomatik rejimda kurs so'ralishi kerak"


def test_paying_a_dollar_qarz_in_dollars_hides_the_kurs(page, live_server, world):
    _open(page, live_server, world)
    _pick(page, debt="usd", arrived="usd")
    assert _rate(page).is_hidden(), "o'z valyutasida to'lovda kurs so'ralmasligi kerak"


def test_paying_a_dollar_qarz_in_som_shows_the_kurs(page, live_server, world):
    _open(page, live_server, world)
    _pick(page, debt="usd", arrived="uzs")
    assert _rate(page).is_visible(), "valyuta chegarasini kesib o'tganda kurs kerak"


def test_paying_a_som_qarz_in_som_hides_the_kurs(page, live_server, world):
    _open(page, live_server, world)
    _pick(page, debt="uzs", arrived="uzs")
    assert _rate(page).is_hidden()


def test_paying_a_som_qarz_in_dollars_shows_the_kurs(page, live_server, world):
    _open(page, live_server, world)
    _pick(page, debt="uzs", arrived="usd")
    assert _rate(page).is_visible()


def test_changing_the_qarz_alone_flips_the_box(page, live_server, world):
    """The picker is the thing that moved, not the row — the box has to follow it."""
    _open(page, live_server, world)
    _pick(page, debt="usd", arrived="usd")
    assert _rate(page).is_hidden()
    _pick(page, debt="uzs")
    assert _rate(page).is_visible(), "qarz valyutasi o'zgarganda quti qayta chiqishi kerak"


def test_a_typed_kurs_is_cleared_when_the_box_goes_away(page, live_server, world):
    """An invisible rate would still drive the "≈ …" preview beside the summa,
    quoting a so'm figure off a number the operator can no longer see."""
    _open(page, live_server, world)
    _pick(page, debt="usd", arrived="uzs")
    _rate(page).fill("13500")
    _pick(page, arrived="usd")
    assert _rate(page).is_hidden()
    assert _rate(page).input_value() == "", "yashirilgan kurs yuborilmasligi kerak"


def test_the_fee_bearer_radios_sit_beside_their_own_words(page, live_server, world):
    """Regression: `.lineset-field input { width: 100% }` is right for every text box
    in a money row and catastrophic for a radio — the dot stretched to the full width
    of its column and shoved its own label off the end, so the pair read as four
    stray items rather than two choices. A radio is ~13px wide, never 100.

    Measured with the usul on perechisleniya, because that is the only time the
    question exists: a naqd row pays no bank foiz, so there is no cut for anybody to
    carry and the whole field — caption and radios together — is away."""
    _open(page, live_server, world)
    field = page.locator(".lineset-field--fee_bearer")
    assert field.is_hidden(), "naqd to'lovda komissiya savoli chiqmasligi kerak"
    page.select_option("[name='form-0-method']", "transfer")
    page.wait_for_timeout(250)
    assert field.is_visible(), "perechisleniyada komissiya savoli chiqishi kerak"
    radios = page.locator(".lineset-field--fee_bearer input[type=radio]")
    assert radios.count() == 2
    for i in range(2):
        box = radios.nth(i).bounding_box()
        assert box["width"] < 30, f"radio {i} stretched to {box['width']}px"
    # and each one shares a line with the text it belongs to
    first, second = (radios.nth(i).bounding_box() for i in range(2))
    assert abs(first["y"] - second["y"]) < 2, "the two options must sit on one line"


def test_the_method_select_is_not_clipped(page, live_server, world):
    """"Bank o'tkazmasi" is the longest option and the one that matters most —
    forced into an equal third of the row it rendered as "Bank o'ti"."""
    _open(page, live_server, world)
    width = page.locator("[name='form-0-method']").bounding_box()["width"]
    assert width > 150, f"To'lov usuli only {width}px — long options will clip"


def test_a_second_row_keeps_its_own_answer(page, live_server, world):
    """One settlement, two ways the money arrived. A dollar row needing no kurs has
    to sit beside a so'm row that does — the enhancer scopes per row, not per form."""
    _open(page, live_server, world)
    _pick(page, debt="usd", arrived="usd")
    page.click("[data-line-add]")
    page.wait_for_selector("[name='form-1-currency']")
    page.select_option("[name='form-1-currency']", "uzs")
    page.wait_for_timeout(250)
    assert _rate(page).is_hidden(), "dollar qatorida kurs so'ralmasligi kerak"
    assert page.locator("[name='form-1-exchange_rate']").is_visible(), \
        "so'm qatorida kurs so'ralishi kerak"
