"""E2E: the money widgets as the operator actually sees them, in a real browser.

These cover the half of the money logic that pytest cannot reach — the base.html
enhancers that draw the live counter-currency preview and show/hide the bank foiz.
The server-side conversion is already covered by the unit suites; what is NOT
covered anywhere is whether the screen AGREES with what the server will store,
and that gap is exactly what the operators are reporting ("I change the valyuta
but it stays on the other one").

Run:
    TEST_DB_SUFFIX=_e2e .venv/bin/python -m pytest tests/audit/test_e2e_money_ui.py -q

The suite is skipped when Playwright's chromium is not installed, so it never
breaks a plain `pytest` run on a machine that has not done `playwright install`.
"""
import os

import pytest

# Playwright's SYNC api drives the browser from a greenlet loop running in this
# very thread, and Django's ORM refuses to be called from a thread with a live
# event loop. The guard is there to stop a real async view blocking; here the
# loop belongs to the browser driver and the ORM calls are genuinely sequential,
# so it is opted out of — before django is imported.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from accounts.models import User  # noqa: E402
from crm.models import Partner, Shipment, ShipmentStatus  # noqa: E402

from conftest import e2e_context  # noqa: E402

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

PASSWORD = "e2e-pass-123"


# --- fixtures --------------------------------------------------------------
# live_server needs a real (non-atomic) database, so this suite pays for
# transactional_db. Everything it needs is built once per test.

@pytest.fixture
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # chromium not installed on this machine
            pytest.skip(f"playwright chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = e2e_context(browser)
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def boss(transactional_db):
    return User.objects.create_user(username="e2eboss", password=PASSWORD,
                                    role=User.Role.ADMIN, first_name="E", last_name="Two")


@pytest.fixture
def shipment(boss):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    status = ShipmentStatus.objects.first() or ShipmentStatus.objects.create(
        name="Yo'lda", order=1)
    from conftest import make_contract
    contract = make_contract(partner=partner)
    ship = Shipment.objects.create(contract=contract, status=status)
    return ship


def login(page, live_server):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")


def open_expense_modal(page, live_server, shipment):
    """The xarajat grid — the one modal whose Valyuta and Usul are RADIO groups."""
    page.goto(f"{live_server.url}/shipments/{shipment.pk}/")
    page.wait_for_load_state("networkidle")
    page.click("a[href*='expenses/new']")
    page.wait_for_selector(".xgrid-form", timeout=5000)
    page.wait_for_timeout(150)  # let the modal:loaded enhancers settle


def pick_seg(page, name, value):
    """Choose an option in a segmented control the way an operator does.

    The real <input type=radio> is visually replaced by the .seg-body span next to
    it, so Playwright cannot click the input directly — the span is over the top of
    it. Clicking the wrapping <label> is both what actually happens on screen and
    what fires the change event the enhancers listen for.
    """
    page.locator(f".seg input[name={name}][value={value}]").locator(
        "xpath=ancestor::label[1]").click()
    page.wait_for_timeout(200)


def fee_input(page):
    """The foiz field itself.

    Deliberately NOT its label: the enhancer hides
    `closest('.lineset-field') || closest('p') || the input`, and this modal wraps
    the field in a bare <label> that is neither — so the input is what moves.
    """
    return page.locator("[name=fee_percent]")


# --- the counter-currency preview -----------------------------------------

def test_preview_follows_the_currency_the_operator_picked(page, live_server, shipment):
    """Pick So'm, type 600 000 at 12 000 → the hint must read about $50.

    This is the operator's "I changed the valyuta but it stayed on the other one"
    report, and it is the preview that is lying: the enhancer resolves the currency
    with querySelector('[data-money-currency]'), which on a radio group returns the
    FIRST radio rather than the CHECKED one.
    """
    login(page, live_server)
    open_expense_modal(page, live_server, shipment)

    pick_seg(page, "currency", "uzs")
    page.fill("[name=exchange_rate]", "12000")
    first_amount = page.locator(".xgrid input[data-money-amount]").first
    first_amount.fill("600000")
    page.wait_for_timeout(200)

    preview = page.locator(".xgrid [data-money-preview]").first
    text = preview.inner_text()
    assert "so'm" not in text, (
        f"So'm is selected, so the preview must show the DOLLAR counter-value; got {text!r}. "
        "The enhancer read the first radio (usd) instead of the checked one."
    )
    assert "50" in text, f"600 000 so'm at 12 000 is ~$50; preview said {text!r}"


def test_preview_updates_when_the_currency_is_switched_back(page, live_server, shipment):
    """Dollar → the same box must flip to a so'm counter-value."""
    login(page, live_server)
    open_expense_modal(page, live_server, shipment)

    page.fill("[name=exchange_rate]", "12000")
    first_amount = page.locator(".xgrid input[data-money-amount]").first
    first_amount.fill("50")
    pick_seg(page, "currency", "usd")

    text = page.locator(".xgrid [data-money-preview]").first.inner_text()
    assert "so'm" in text, f"Dollar is selected, so the preview must be in so'm; got {text!r}"
    assert "600" in text.replace(" ", ""), f"$50 at 12 000 is 600 000 so'm; preview said {text!r}"


def test_switching_currency_repreviews_every_box_in_the_grid(page, live_server, shipment):
    """The grid shares one Valyuta across seven boxes — switching it must move all
    the previews, not just the one that was last touched."""
    login(page, live_server)
    open_expense_modal(page, live_server, shipment)

    page.fill("[name=exchange_rate]", "12000")
    boxes = page.locator(".xgrid input[data-money-amount]")
    boxes.nth(0).fill("120000")
    boxes.nth(1).fill("240000")
    pick_seg(page, "currency", "uzs")

    previews = page.locator(".xgrid [data-money-preview]")
    shown = [previews.nth(i).inner_text() for i in range(2)]
    for text in shown:
        assert "so'm" not in text, (
            f"every box must preview in dollars once So'm is picked; got {shown!r}")


def test_the_defect_is_order_dependent_which_is_why_it_looks_random(page, live_server,
                                                                   shipment):
    """Same two actions, both orders. Only one of them previews correctly.

    Picking the valyuta fires `change`, and that handler is passed the radio that
    was clicked — so it reads the right one. Typing an amount fires `input`, and
    THAT handler re-finds the currency with querySelector, which returns the first
    radio in the group no matter which is checked.

    So: pick-then-type (the natural order) is wrong, type-then-pick is right. That
    is why the report is "SOMETIMES the valyuta doesn't take" rather than "always".
    """
    login(page, live_server)

    # type first, then pick — the handler that runs last is the good one
    open_expense_modal(page, live_server, shipment)
    page.fill("[name=exchange_rate]", "12000")
    page.locator(".xgrid input[data-money-amount]").first.fill("600000")
    pick_seg(page, "currency", "uzs")
    type_then_pick = page.locator(".xgrid [data-money-preview]").first.inner_text()

    # pick first, then type — the handler that runs last is the broken one
    open_expense_modal(page, live_server, shipment)
    pick_seg(page, "currency", "uzs")
    page.fill("[name=exchange_rate]", "12000")
    page.locator(".xgrid input[data-money-amount]").first.fill("600000")
    page.wait_for_timeout(200)
    pick_then_type = page.locator(".xgrid [data-money-preview]").first.inner_text()

    assert type_then_pick == pick_then_type, (
        "the same So'm figure previews two different ways depending on the order the "
        f"operator worked in: type-then-pick gave {type_then_pick!r}, "
        f"pick-then-type gave {pick_then_type!r}")


# --- the bank foiz field ---------------------------------------------------

def test_fee_percent_is_hidden_until_the_method_is_a_transfer(page, live_server, shipment):
    """Naqd is the default, and CashEntry.fee_amount ignores a foiz on naqd — so
    the field must not be sitting there inviting one.

    The enhancer calls update() once per matching element; on a radio group that is
    once per radio, and the LAST radio (transfer) decides the final visibility.
    """
    login(page, live_server)
    open_expense_modal(page, live_server, shipment)

    assert page.is_checked(".seg input[name=method][value=cash]"), "naqd is the default"
    assert fee_input(page).is_hidden(), (
        "Naqd is selected, so the Perechisleniya foizi field must be hidden; it is visible. "
        "The enhancer ran update() for every radio and the last one (transfer) won.")


def test_choosing_transfer_reveals_the_fee_and_naqd_hides_it_again(page, live_server, shipment):
    login(page, live_server)
    open_expense_modal(page, live_server, shipment)

    pick_seg(page, "method", "transfer")
    assert fee_input(page).is_visible(), "transfer must reveal the foiz field"

    pick_seg(page, "method", "cash")
    assert fee_input(page).is_hidden(), "switching back to naqd must hide the foiz field again"


def test_fee_percent_value_survives_opening_the_modal(page, live_server, shipment):
    """update() writes fee_percent = '0' on every non-transfer method. Run once per
    radio at load, that clears whatever the field was initialised with — a value the
    operator never touched changing on its own, which is the reported symptom."""
    login(page, live_server)
    open_expense_modal(page, live_server, shipment)

    pick_seg(page, "method", "transfer")
    page.fill("[name=fee_percent]", "1.5")
    # Re-fire the enhancer the way a re-render does (an invalid submit reloads the
    # modal body and dispatches modal:loaded again).
    page.evaluate("document.dispatchEvent(new CustomEvent('modal:loaded', {detail: document}))")
    page.wait_for_timeout(150)

    assert page.input_value("[name=fee_percent]") == "1.5", (
        "the foiz the operator typed was reset by the enhancer")


# --- the control group: a SELECT-based money form --------------------------
# If these pass while the radio ones fail, the defect is specifically the
# radio-group lookup and not the enhancer's arithmetic.

def test_select_based_form_previews_the_right_way_round(page, live_server, shipment):
    """Mijoz to'lovi uses a <select> for Valyuta, so querySelector finds the one
    element that carries the value. Same enhancer, correct answer — which is what
    pins the defect on the radio lookup."""
    from crm.models import Customer
    Customer.objects.create(name="Ali", phone="2")
    login(page, live_server)
    page.goto(f"{live_server.url}/customer-payments/new/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(150)

    currency = page.locator("select[data-money-currency]").first
    if currency.count() == 0:
        pytest.skip("mijoz to'lov form is not a plain select on this build")
    currency.select_option("uzs")
    page.locator("[data-money-rate]").first.fill("12000")
    page.locator("[data-money-amount]").first.fill("600000")
    page.wait_for_timeout(200)

    text = page.locator("[data-money-preview]").first.inner_text()
    assert "so'm" not in text and "50" in text, (
        f"a select-based Valyuta must preview the dollar side; got {text!r}")
