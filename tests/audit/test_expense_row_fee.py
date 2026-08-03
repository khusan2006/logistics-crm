"""Per-turkum perechisleniya foizi on the xarajat grid.

The grid already lets a turkum override the shared valyuta and usul, so one
submission can hold a bojxona wired by bank beside a gruzchi paid in cash. The
foiz has to follow the same rule: it is a property of the row that was actually
wired, not of the trip.

Run:
    TEST_DB_SUFFIX=_rowfee .venv/bin/python -m pytest tests/audit/test_expense_row_fee.py -q
"""
from decimal import Decimal

import pytest

from crm.forms import ExpenseGridForm
from crm.models import Contract, ContractLine, Partner, PayMethod, Shipment, ShipmentStatus

pytestmark = pytest.mark.django_db

CUSTOMS = "amount_customs"
LOADER = "amount_loader"


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("1000"), price=Decimal("1"),
                                price_uzs=Decimal("12000"))
    return Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival())


def _post(shipment, **fields):
    """The grid's payload: shared pickers plus whichever boxes are filled."""
    data = {
        "shipment": shipment.pk, "date": "2026-07-20",
        "currency": "usd", "exchange_rate": "12000",
        "method": PayMethod.CASH, "fee_percent": "0", "note": "",
    }
    data.update({k: str(v) for k, v in fields.items()})
    return data


def _rows(shipment, admin_user, **fields):
    form = ExpenseGridForm(_post(shipment, **fields))
    assert form.is_valid(), form.errors
    return {row.category: row for row in form.build(admin_user)}


# --- the point of the change ------------------------------------------------

def test_each_turkum_carries_its_own_foiz(shipment, admin_user):
    """Two boxes, both wired, different banks — each keeps its own cut."""
    rows = _rows(shipment, admin_user,
                 method=PayMethod.TRANSFER,
                 **{CUSTOMS: "1000", "fee_customs": "1.5",
                    LOADER: "200", "fee_loader": "0.4"})

    assert rows["customs"].fee_percent == Decimal("1.5")
    assert rows["loader"].fee_percent == Decimal("0.4")
    # and the money that actually leaves the till follows each row's own foiz
    assert rows["customs"].fee_amount == Decimal("15.00")     # 1000 x 1.5%
    assert rows["loader"].fee_amount == Decimal("0.80")       # 200 x 0.4%


def test_a_blank_box_falls_back_to_the_shared_foiz(shipment, admin_user):
    """Blank means "as set below", the same rule valyuta and usul follow."""
    rows = _rows(shipment, admin_user, method=PayMethod.TRANSFER, fee_percent="2",
                 **{CUSTOMS: "1000", "fee_customs": "",
                    LOADER: "500", "fee_loader": "0.5"})

    assert rows["customs"].fee_percent == Decimal("2")        # inherited
    assert rows["loader"].fee_percent == Decimal("0.5")       # its own


def test_a_typed_zero_beats_the_shared_foiz(shipment, admin_user):
    """0 is an explicit "no foiz on this row" and must not read as "unset".

    This is why row_fee tells a blank apart from a zero instead of treating both
    as falsy — the operator who types 0 is saying the bank waived it."""
    rows = _rows(shipment, admin_user, method=PayMethod.TRANSFER, fee_percent="2",
                 **{CUSTOMS: "1000", "fee_customs": "0"})

    assert rows["customs"].fee_percent == Decimal("0")
    assert rows["customs"].fee_amount == Decimal("0")


def test_a_cash_row_beside_a_wired_one_is_charged_nothing(shipment, admin_user):
    """The whole reason the foiz moved into the box.

    Shared usul is Naqd; only the bojxona is wired. The gruzchi must not be
    charged the bojxona's bank cut."""
    rows = _rows(shipment, admin_user, fee_percent="0",
                 **{CUSTOMS: "1000", "method_customs": PayMethod.TRANSFER,
                    "fee_customs": "1.5",
                    LOADER: "200"})

    assert rows["customs"].method == PayMethod.TRANSFER
    assert rows["customs"].fee_amount == Decimal("15.00")
    assert rows["loader"].method == PayMethod.CASH
    assert rows["loader"].fee_amount == Decimal("0")


def test_a_foiz_on_a_cash_row_is_ignored_not_charged(shipment, admin_user):
    """Belt and braces: even if a foiz reaches a naqd row, CashEntry.fee_amount
    drops it. The UI hides the box, but the server must not depend on that."""
    rows = _rows(shipment, admin_user,
                 **{CUSTOMS: "1000", "method_customs": PayMethod.CASH,
                    "fee_customs": "5"})

    assert rows["customs"].fee_percent == Decimal("5")
    assert rows["customs"].fee_amount == Decimal("0")
    assert rows["customs"].total_out == Decimal("1000")


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("bad", ["-1", "101"])
def test_an_out_of_range_row_foiz_is_refused(shipment, bad):
    """The same 0-100 rule FeePercentFormMixin applies to the shared field. 200%
    turns an outgoing to'lov into a double charge rather than failing loudly."""
    form = ExpenseGridForm(_post(shipment, method=PayMethod.TRANSFER,
                                 **{CUSTOMS: "1000", "fee_customs": bad}))
    assert not form.is_valid()
    assert "fee_customs" in form.errors


def test_the_grid_still_saves_through_the_real_view(shipment, admin_client):
    """End to end: the modal posts, the rows land, each with its own foiz."""
    resp = admin_client.post("/expenses/new/", _post(
        shipment, method=PayMethod.TRANSFER,
        **{CUSTOMS: "1000", "fee_customs": "1.5", LOADER: "200", "fee_loader": "0.25"}))
    assert resp.status_code in (200, 302), resp.status_code

    saved = {e.category: e for e in shipment.expenses.all()}
    assert saved["customs"].fee_percent == Decimal("1.50")
    assert saved["loader"].fee_percent == Decimal("0.25")


def test_the_modal_renders_a_foiz_box_inside_every_turkum(shipment, admin_client):
    """Seven turkumlar, seven foiz inputs — each hidden until its box is wired.

    Asked for as AJAX: form_response (crm/utils.py:11) only renders the grid
    partial for an XHR, and serves the plain full-page form otherwise — so a
    normal GET would show the fields without the .xcell markup around them."""
    page = admin_client.get(f"/expenses/new/?shipment={shipment.pk}",
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = page.content.decode()

    for category in ("customs", "declarant", "transport", "loader", "road",
                     "cert", "other"):
        assert f'name="fee_{category}"' in html, f"no foiz box for {category}"
    assert html.count("data-cell-fee") == 7
