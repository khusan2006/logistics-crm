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

def make_contract(partner=None, brand="LLDPE", kg="1000", price="1.00",
                  price_uzs=None, **kw):
    """A kelishuv with a single product. Returns the Contract.

    `currency` (and any other header field) goes through **kw; the product row
    inherits it, so a so'm kelishuv is built by passing currency="uzs" here rather
    than on the row. `price_uzs` spells out the so'm side when a test cares about the
    exact figure that was typed — left out, the row's own kurs fills it in."""
    if partner is None:
        partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    fields = {"created": "2026-07-01"}
    fields.update(kw)
    contract = Contract.objects.create(partner=partner, **fields)
    line = {"kg": Decimal(str(kg)), "price": Decimal(str(price))}
    if price_uzs is not None:
        line["price_uzs"] = Decimal(str(price_uzs))
    ContractLine.objects.create(contract=contract, brand=brand, **line)
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
def skladchi_user(db):
    return User.objects.create_user(
        username="skladchi", password=PASSWORD, role=User.Role.SKLADCHI,
        first_name="Sklad", last_name="Chi",
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


@pytest.fixture
def skladchi_client(skladchi_user):
    client = Client()
    client.force_login(skladchi_user)
    return client


def payment_rows(*entries, customer, date="2026-07-20", debt_currency=""):
    """POST payload for the mijoz to'lov modal: the shared mijoz, sana and the qarz
    being collected, plus one row per way the money arrived. Rows default to a dollar
    naqd at 12,000 so a test only spells out what it is actually testing.

    `debt_currency` defaults to blank — "wherever it fits, oldest first", which is
    what every to'lov did before the picker existed."""
    data = {"customer": getattr(customer, "pk", customer), "date": date,
            "debt_currency": debt_currency,
            "form-TOTAL_FORMS": str(len(entries)), "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000"}
    defaults = {"currency": "usd", "amount": "0", "exchange_rate": "12000",
                "method": "cash", "fee_percent": "0", "note": ""}
    for i, entry in enumerate(entries):
        for key, value in {**defaults, **entry}.items():
            data[f"form-{i}-{key}"] = str(value)
    return data


def line_data(*rows, initial=0, prefix="lines"):
    """POST payload for a Mahsulotlar formset: management fields plus one dict per
    product row, e.g. line_data({"brand": "LLDPE", "kg": "100", "price": "1"}).

    No currency or kurs here: a product row is priced in the currency of the kelishuv
    it hangs off, and inherits that kelishuv's rate. A so'm narx is therefore posted
    as `currency: "uzs"` on the HEADER, next to the partner."""
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
