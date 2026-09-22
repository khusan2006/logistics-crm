"""Browser check: on a new birja yuk the operator types the marka and the truck's kg,
and the Mahsulotlar rows fill themselves — oldest kelishuv first, each lot taking
what it has left — with a line naming each kelishuv's share.

The filling is base.html's `fillFromTotal`, so only a browser can say it runs; the
server side of the same truck is pinned in tests/test_birja_truck.py.
"""
import os
from decimal import Decimal

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402

from accounts.models import User  # noqa: E402
from crm.models import (  # noqa: E402
    Contract, ContractLine, Shipment, ShipmentStatus, birja_partner,
)

from conftest import e2e_context, make_shipment  # noqa: E402

pw = pytest.importorskip("playwright.sync_api")

# The holatlar are seeded by a migration; a transactional test flushes them after
# the first run unless they are serialized back in.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

PASSWORD = "e2e-pass-123"
BRAND = "и 1561"


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


def _kelishuv(created, *lots):
    contract = Contract.objects.create(partner=birja_partner(), created=created)
    for position, kg in enumerate(lots):
        ContractLine.objects.create(contract=contract, brand=BRAND, kg=Decimal(kg),
                                    price=Decimal("1.00"), position=position)
    return contract


@pytest.fixture
def world(db):
    """birja-1 has 15 000 kg left, birja-2 all 30 000."""
    User.objects.create_user(username="e2eboss", password=PASSWORD,
                             role=User.Role.ADMIN, first_name="E", last_name="T")
    older = _kelishuv("2026-09-07", "30000")
    make_shipment(contract_line=older.lines.get(), kg="15000",
                  status=ShipmentStatus.for_kind(birja=True).first())
    newer = _kelishuv("2026-09-10", "30000")
    return {"older": older, "newer": newer}


def _open(page, live_server):
    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/birja/yuklar/new/")
    page.wait_for_selector("[data-fill-kg]")


def _rows(page):
    """[(lot pk, kg)] of the visible Mahsulotlar rows."""
    return page.eval_on_selector_all(
        "[data-line-rows] [data-line-row]:not([hidden])",
        """rows => rows.map(r => [r.querySelector('select').value,
                                   r.querySelector('input[name$="-kg"]').value
                                    .replace(/[\\s\\u00a0]/g, '')])""")


def test_typing_the_kg_fills_the_rows_oldest_kelishuv_first(page, live_server, world):
    _open(page, live_server)
    page.fill("[data-fill-kg]", "20000")
    page.wait_for_timeout(700)
    older_lot = world["older"].lines.get().pk
    newer_lot = world["newer"].lines.get().pk
    assert _rows(page) == [[str(older_lot), "15000"], [str(newer_lot), "5000"]]
    summary = page.inner_text("[data-fill-summary]")
    assert world["older"].code in summary and world["newer"].code in summary
    assert "bitta mashina" in summary
    if os.environ.get("E2E_SHOT"):              # a picture of the filled modal
        page.screenshot(path=os.environ["E2E_SHOT"], full_page=True)

    page.fill("[name=sent]", "2026-09-20")
    page.fill("[name=eta]", "2026-09-25")
    page.click("form.stacked button[type=submit]")
    page.wait_for_load_state("networkidle")
    truck = Shipment.objects.filter(truck__isnull=True).latest("pk")
    assert truck.contract == world["older"]
    assert truck.truck_kg == Decimal("20000.000")


def test_more_than_the_birja_has_says_so(page, live_server, world):
    _open(page, live_server)
    page.fill("[data-fill-kg]", "50000")
    page.wait_for_timeout(700)
    summary = page.inner_text("[data-fill-summary]")
    assert "5 000 kg yetmaydi" in summary or "5 000 kg yetmaydi" in summary


def test_the_edit_modal_opens_on_the_truck_and_refills_it(page, live_server, world):
    """A truck of 15 000 (birja-1) + 5 000 (birja-2): the box opens on 20 000 and
    the split, and 26 000 fills the rest of birja-2 without a third yuk."""
    older, newer = world["older"], world["newer"]
    status = ShipmentStatus.for_kind(birja=True).first()
    older.lines.get().shipment_lines.all().delete()     # birja-1 back to 30 000
    head = make_shipment(contract_line=older.lines.get(), kg="15000", status=status,
                         sent="2026-09-20")
    part = make_shipment(contract_line=newer.lines.get(), kg="5000", status=status,
                         sent="2026-09-20")
    Shipment.objects.filter(pk=part.pk).update(truck=head)
    Shipment.objects.exclude(pk__in=[head.pk, part.pk]).delete()

    page.goto(f"{live_server.url}/login/")
    page.fill("[name=username]", "e2eboss")
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}/shipments/{part.pk}/edit/")
    page.wait_for_selector("[data-fill-kg]")
    assert page.input_value("[data-fill-kg]").replace(" ", "").replace(" ", "") == "20000"
    assert newer.code in page.inner_text("[data-fill-summary]")

    page.fill("[data-fill-kg]", "36000")
    page.wait_for_timeout(700)
    rows = _rows(page)
    assert rows == [[str(older.lines.get().pk), "30000"], [str(newer.lines.get().pk), "6000"]]
    if os.environ.get("E2E_SHOT_EDIT"):
        page.screenshot(path=os.environ["E2E_SHOT_EDIT"], full_page=True)
    page.click("form.stacked button[type=submit]")
    page.wait_for_load_state("networkidle")
    head.refresh_from_db()
    assert {y.contract.code: y.kg for y in head.truck_yuklar} == {
        older.code: Decimal("30000.000"), newer.code: Decimal("6000.000")}
    assert Shipment.objects.count() == 2
