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


def test_if_empty_seeds_when_database_is_empty(tmp_path, db):
    """First deploy: nothing loaded yet, so --if-empty imports normally."""
    call_command("import_prototype", file=_write(tmp_path, SAMPLE), noinput=True, if_empty=True)

    assert Partner.objects.count() == 2
    assert Contract.objects.count() == 3


def test_if_empty_is_a_noop_when_data_exists(tmp_path, db):
    """Redeploy: existing data must survive — no wipe, no re-import."""
    Partner.objects.create(name="Real prod hamkor", phone="+998 90 000 0000")

    call_command("import_prototype", file=_write(tmp_path, SAMPLE), noinput=True, if_empty=True)

    # Untouched: the pre-existing partner is still the only one, nothing imported.
    assert Partner.objects.count() == 1
    assert Partner.objects.get().name == "Real prod hamkor"
    assert Contract.objects.count() == 0
    assert SupplierPayment.objects.count() == 0


def test_if_empty_noop_survives_missing_file(tmp_path, db):
    """An already-seeded redeploy must not fail even if the export file is gone."""
    Partner.objects.create(name="Real prod hamkor")

    call_command("import_prototype", file=str(tmp_path / "gone.json"), noinput=True, if_empty=True)

    assert Partner.objects.count() == 1


def test_curly_apostrophes_still_match(tmp_path, db):
    """The export writes 'Yo‘lda' / 'Bank o‘tkazmasi' with U+2018; the DB uses a
    straight quote. Lookups must normalise or the import dies on real exports."""
    curly = {
        "partners": [{"id": 1, "name": "Pars Polymer Co."}],
        "contracts": [{"id": 101, "partnerId": 1, "brand": "LLDPE 209AA",
                       "kg": 50000, "price": 0.96, "created": "2026-07-28",
                       "deadline": "2026-07-28"}],
        "payments": [{"contractId": 101, "amount": 18000, "date": "2026-07-02",
                      "method": "Bank o‘tkazmasi"}],
        "shipments": [{"contractId": 101, "kg": 20000, "status": "Yo‘lda",
                       "sent": "2026-07-06", "eta": "2026-07-19", "arrived": "",
                       "transport": "01 777 AAA", "container": "MSCU-442109",
                       "logist": "Akmal"}],
    }
    call_command("import_prototype", file=_write(tmp_path, curly), noinput=True)

    assert SupplierPayment.objects.get().method == "transfer"
    assert Shipment.objects.get().status.name == "Yo'lda"


def test_warns_about_sections_it_cannot_import(tmp_path, capsys, db):
    """Non-empty sections with no importer must be reported, never dropped quietly."""
    with_extras = dict(SAMPLE)
    with_extras["sales"] = [{"id": 1}, {"id": 2}]
    with_extras["cashEntries"] = [{"id": 1}]
    with_extras["settings"] = {"usdRate": 12649}
    with_extras["mystery"] = [{"id": 9}]

    call_command("import_prototype", file=_write(tmp_path, with_extras), noinput=True)

    out = capsys.readouterr().out
    assert "import QILINMADI" in out
    assert "sales (2)" in out
    assert "cashEntries (1)" in out
    assert "mystery (1)" in out and "NOMA'LUM" in out


def test_no_warning_when_nothing_is_skipped(tmp_path, capsys, db):
    call_command("import_prototype", file=_write(tmp_path, SAMPLE), noinput=True)
    assert "import QILINMADI" not in capsys.readouterr().out


SCHEMA_B = {  # "agreements" generation: grade / type / sentDate / numeric costs
    "partners": [{"id": "P-100001", "name": "Pars Polymer Co.", "phone": "+98 912 345 67 89",
                  "city": "Tehron", "note": "HDPE va LLDPE"}],
    "agreements": [{"id": "K-200001", "partnerId": "P-100001", "grade": "HDPE 7000F",
                    "kg": 50000, "price": 0.91, "total": 45500, "date": "2026-07-01",
                    "deadline": "2026-07-28", "note": "2 ta mashinada"}],
    "payments": [{"id": "T-300001", "agreementId": "K-200001", "amount": 15000,
                  "date": "2026-07-02", "type": "Bank o‘tkazmasi", "note": "1-to‘lov"}],
    "shipments": [{"id": "Y-400002", "agreementId": "K-200001", "kg": 18000,
                   "status": "Bojxonada", "sentDate": "2026-07-07", "eta": "2026-07-15",
                   "arrival": "", "transport": 2100, "customs": 850, "other": 140,
                   "note": "2-mashina"}],
}

SCHEMA_A = {  # gl_* generation: separate keys, no expenses at all
    "gl_partners": [{"id": "P178", "name": "SARDOR", "phone": "558882552255",
                     "city": "SAMARQAND", "note": ""}],
    "gl_agreements": [{"id": "A490", "partnerId": "P178", "grade": "2102",
                       "date": "2026-07-01", "kg": 100000, "price": 1,
                       "deadline": "2026-07-30"}],
    "gl_payments": [{"id": 1784, "agreementId": "A490", "amount": 25000,
                     "date": "2026-07-30", "type": "Naqd", "note": ""}],
    "gl_shipments": [{"id": 1784, "agreementId": "A490", "kg": 25000,
                      "date": "2026-07-02", "status": "Yo‘lda", "note": ""}],
}


def test_schema_b_agreements_generation(tmp_path, db):
    """`agreements`/`grade`/`type`/`sentDate` must import like the newer shape."""
    call_command("import_prototype", file=_write(tmp_path, SCHEMA_B), noinput=True)

    contract = Contract.objects.get()
    assert contract.brand == "HDPE 7000F"          # grade -> brand
    assert str(contract.created) == "2026-07-01"   # date -> created
    assert SupplierPayment.objects.get().method == "transfer"  # type -> method

    shipment = Shipment.objects.get()
    assert shipment.status.name == "Bojxona"       # "Bojxonada" alias
    assert str(shipment.sent) == "2026-07-07"      # sentDate -> sent
    assert shipment.arrived is None                # empty arrival -> NULL
    assert shipment.note == "2-mashina"


def test_schema_b_numeric_transport_is_money_not_a_plate(tmp_path, db):
    """`transport: 2100` is a COST here, not a vehicle plate — must not be stored
    in the plate field, and must become a transport expense."""
    call_command("import_prototype", file=_write(tmp_path, SCHEMA_B), noinput=True)

    shipment = Shipment.objects.get()
    assert shipment.transport == ""  # numeric transport is never a plate

    exps = {e.category: e.amount for e in shipment.expenses.all()}
    assert str(exps["transport"]) == "2100.00"
    assert str(exps["customs"]) == "850.00"
    assert str(exps["other"]) == "140.00"


def test_schema_a_gl_keys_generation(tmp_path, db):
    call_command("import_prototype", file=_write(tmp_path, SCHEMA_A), noinput=True)

    assert Partner.objects.get().name == "SARDOR"
    assert Contract.objects.get().brand == "2102"
    assert SupplierPayment.objects.get().method == "cash"

    shipment = Shipment.objects.get()
    assert shipment.status.name == "Yo'lda"   # curly apostrophe + gl_ key
    assert str(shipment.sent) == "2026-07-02"  # `date` -> sent
    assert shipment.expenses.count() == 0      # this generation has no costs


def test_alias_sections_are_not_reported_as_skipped(tmp_path, capsys, db):
    call_command("import_prototype", file=_write(tmp_path, SCHEMA_B), noinput=True)
    assert "import QILINMADI" not in capsys.readouterr().out


def test_real_committed_export_loads(db):
    """The committed crm/seed_data/prototype.json loads with expected totals."""
    call_command("import_prototype", noinput=True)

    assert Partner.objects.count() == 4
    assert Contract.objects.count() == 13
    assert SupplierPayment.objects.count() == 16
    assert Shipment.objects.count() == 2
    assert ShipmentExpense.objects.count() == 8  # 4 buckets on each arrived shipment
    assert SupplierPayment.objects.filter(method="card").count() == 1
