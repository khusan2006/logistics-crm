"""Tests for the `import_prototype` command (prototype JSON export importer).

The command wipes business data and loads an export whose records join by
numeric id: partners by `id`, contracts by `id` (brands are NOT unique, so
keying by brand would collapse contracts), payments by `contractId`, shipments
by `contractId`. Shipment expense buckets become ShipmentExpense rows.
"""
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User
from crm.models import Contract, Partner, Shipment, ShipmentExpense, SupplierPayment

# Minimal export exercising the tricky bits: partners joined by big id, a brand
# duplicated across two contracts under one partner (must key by id, not brand),
# Karta -> card, arrival status + arrived date, and per-bucket expenses.
SAMPLE = {
    "partners": [
        {"id": 111, "name": "vazifadon", "phone": "+998 99 076 71 71", "city": "tehron", "note": ""},
        {"id": 222, "name": "sobir", "phone": "+998 33 095 50 50", "city": "teh", "note": ""},
    ],
    "contracts": [
        {"id": 1, "partnerId": 111, "brand": "2102 kampaund", "created": "2026-07-01", "kg": 48000, "price": 1, "deadline": "2026-07-31"},
        {"id": 2, "partnerId": 111, "brand": "2102 kampaund", "created": "2026-07-02", "kg": 72000, "price": 1.1, "deadline": "2026-07-30"},
        {"id": 3, "partnerId": 222, "brand": "2102 repak", "created": "2026-07-02", "kg": 50000, "price": 1.455, "deadline": "2026-07-10"},
    ],
    "payments": [
        {"contractId": 1, "amount": 48000, "date": "2026-07-06", "method": "Naqd"},
        {"contractId": 3, "amount": 56000, "date": "2026-07-19", "method": "Karta"},
    ],
    "shipments": [
        {"contractId": 1, "kg": 48000, "status": "Omborga yetib keldi", "sent": "2026-07-08",
         "eta": "2026-07-17", "arrived": "2026-07-19", "transport": "224", "container": "224",
         "logist": "abbos", "transportExpense": 5600, "customsExpense": 6400,
         "handlingExpense": 130, "otherExpense": 260, "expenseNote": ""},
    ],
}


def _write(tmp_path, data):
    p = tmp_path / "proto.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_imports_sample_keyed_by_id(tmp_path, db):
    call_command("import_prototype", file=_write(tmp_path, SAMPLE), noinput=True)

    assert Partner.objects.count() == 2
    assert Contract.objects.count() == 3
    assert SupplierPayment.objects.count() == 2
    assert Shipment.objects.count() == 1

    owner = User.objects.get(username="otabek")
    assert owner.is_admin_role

    # Duplicate brand -> two distinct contracts under the same partner.
    vazifadon = Partner.objects.get(name="vazifadon")
    assert vazifadon.contracts.filter(brand="2102 kampaund").count() == 2

    # Naqd -> cash, Karta -> card.
    assert SupplierPayment.objects.filter(method="cash").count() == 1
    assert SupplierPayment.objects.filter(method="card").count() == 1


def test_shipment_expenses_and_arrival_status(tmp_path, db):
    call_command("import_prototype", file=_write(tmp_path, SAMPLE), noinput=True)

    shipment = Shipment.objects.get()
    assert shipment.status.name == "Omborga yetib keldi"
    assert shipment.status.is_arrival
    assert shipment.arrived is not None
    assert shipment.note == "Logist: abbos"

    # Four non-zero buckets -> four ShipmentExpense rows; handling folds to other.
    exps = ShipmentExpense.objects.filter(shipment=shipment)
    assert sorted(exps.values_list("category", flat=True)) == ["customs", "other", "other", "transport"]
    assert str(exps.get(category="transport").amount) == "5600.00"
    assert exps.filter(category="other", note="Yuk ortish-tushirish").count() == 1


def test_unknown_method_rolls_back(tmp_path, db):
    bad = {
        "partners": [{"id": 1, "name": "x"}],
        "contracts": [{"id": 1, "partnerId": 1, "brand": "b", "created": "2026-07-01",
                       "kg": 1, "price": 1, "deadline": "2026-07-02"}],
        "payments": [{"contractId": 1, "amount": 1, "date": "2026-07-01", "method": "Bitcoin"}],
        "shipments": [],
    }
    with pytest.raises(CommandError):
        call_command("import_prototype", file=_write(tmp_path, bad), noinput=True)

    # Atomic: the failed import left nothing behind.
    assert Partner.objects.count() == 0


def test_missing_file_errors(tmp_path, db):
    with pytest.raises(CommandError):
        call_command("import_prototype", file=str(tmp_path / "nope.json"), noinput=True)


def test_real_committed_export_loads(db):
    """The committed crm/seed_data/prototype.json loads with expected totals."""
    call_command("import_prototype", noinput=True)

    assert Partner.objects.count() == 4
    assert Contract.objects.count() == 13
    assert SupplierPayment.objects.count() == 16
    assert Shipment.objects.count() == 2
    assert ShipmentExpense.objects.count() == 8  # 4 buckets on each arrived shipment
    assert SupplierPayment.objects.filter(method="card").count() == 1
