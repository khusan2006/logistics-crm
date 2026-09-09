"""Browser check: the per-turkum foiz appears only on the box that is wired."""
import os
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")
import pytest
from accounts.models import User
from crm.models import Partner, Shipment, ShipmentStatus
from conftest import e2e_context  # noqa: E402

pw = pytest.importorskip("playwright.sync_api")

PASSWORD = "e2e-pass-123"

@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(); yield b; b.close()

@pytest.fixture
def page(browser):
    ctx = e2e_context(browser); pg = ctx.new_page(); yield pg; ctx.close()

@pytest.fixture
def ship(transactional_db):
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    from conftest import make_contract
    p = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    # Created rather than looked up: transactional_db flushes the tables between
    # tests, which takes the migration-seeded statuses with it.
    status = ShipmentStatus.objects.first() or ShipmentStatus.objects.create(
        name="Yo'lda", order=1)
    return Shipment.objects.create(contract=make_contract(partner=p), status=status)

def _open(page, live_server, ship):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss"); page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]"); page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/shipments/{ship.pk}/"); page.wait_for_load_state("networkidle")
    page.click("a[href*='expenses/new']"); page.wait_for_selector(".xgrid-form"); page.wait_for_timeout(250)

def _fee(page, cat):
    return page.locator(f".xcell[data-cat={cat}] input[name=fee_{cat}]")

def test_all_fee_boxes_hidden_on_a_cash_grid(page, live_server, ship):
    _open(page, live_server, ship)
    for cat in ("customs", "loader", "transport"):
        assert _fee(page, cat).is_hidden(), f"{cat} foiz box shows on a Naqd grid"

def test_only_the_wired_box_reveals_its_foiz(page, live_server, ship):
    _open(page, live_server, ship)
    # override just the bojxona to Bank
    page.select_option(".xcell[data-cat=customs] select[name=method_customs]", "transfer")
    page.wait_for_timeout(250)
    assert _fee(page, "customs").is_visible(), "the wired box must offer a foiz"
    assert _fee(page, "loader").is_hidden(), "a cash box beside it must not"

def test_switching_the_shared_usul_to_bank_reveals_every_box(page, live_server, ship):
    _open(page, live_server, ship)
    page.locator(".seg input[name=method][value=transfer]").locator("xpath=ancestor::label[1]").click()
    page.wait_for_timeout(250)
    for cat in ("customs", "loader", "transport"):
        assert _fee(page, cat).is_visible(), f"{cat} should inherit the shared Bank usul"

def test_a_typed_foiz_is_cleared_when_the_box_goes_back_to_cash(page, live_server, ship):
    _open(page, live_server, ship)
    sel = ".xcell[data-cat=customs] select[name=method_customs]"
    page.select_option(sel, "transfer"); page.wait_for_timeout(200)
    _fee(page, "customs").fill("1.5")
    page.select_option(sel, "cash"); page.wait_for_timeout(250)
    assert _fee(page, "customs").is_hidden()
    assert _fee(page, "customs").input_value() == "", "a hidden foiz must not be submitted"
