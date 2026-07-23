"""Shared fixtures: an admin and a translator, plus logged-in test clients."""
from decimal import Decimal

import pytest
from django.test import Client

from accounts.models import User
from crm.models import Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus

PASSWORD = "test-pass-123"


# --- factories -------------------------------------------------------------
# A kelishuv and a yuk are both headers over product lines. Most tests only care
# about one product, so these build that shape in one call and hand back the
# piece the test actually asserts on.

def make_contract(partner=None, brand="LLDPE", kg="1000", price="1.00", **kw):
    """A kelishuv with a single product. Returns the Contract."""
    if partner is None:
        partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    fields = {"created": "2026-07-01"}
    fields.update(kw)
    contract = Contract.objects.create(partner=partner, **fields)
    ContractLine.objects.create(contract=contract, brand=brand,
                                kg=Decimal(str(kg)), price=Decimal(str(price)))
    return contract


def make_shipment(contract=None, kg="400", price=None, brand="LLDPE", status=None,
                  contract_line=None, **kw):
    """A yuk carrying one product. Returns the Shipment."""
    if contract_line is None:
        if contract is None:
            contract = make_contract(brand=brand)
        contract_line = contract.lines.first()
    shipment = Shipment.objects.create(
        contract=contract_line.contract,
        status=status or ShipmentStatus.objects.first(), **kw)
    ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract_line, kg=Decimal(str(kg)),
        price=None if price is None else Decimal(str(price)))
    return shipment


def make_lot(**kw):
    """The ombor unit: one product on one yuk. Returns the ShipmentLine."""
    return make_shipment(**kw).lines.first()


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="boss", password=PASSWORD, role=User.Role.ADMIN,
        first_name="Bosh", last_name="Admin",
    )


@pytest.fixture
def translator_user(db):
    return User.objects.create_user(
        username="tarjimon", password=PASSWORD, role=User.Role.TRANSLATOR,
        first_name="Tar", last_name="Jimon",
    )


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.fixture
def translator_client(translator_user):
    client = Client()
    client.force_login(translator_user)
    return client


def line_data(*rows, initial=0, prefix="lines"):
    """POST payload for a Mahsulotlar formset: management fields plus one dict per
    product row, e.g. line_data({"brand": "LLDPE", "kg": "100", "price": "1"})."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(rows):
        for key, value in row.items():
            data[f"{prefix}-{i}-{key}"] = "" if value is None else str(value)
    return data
