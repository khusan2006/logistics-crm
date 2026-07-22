"""Tests for the `load_starting_data` baseline seed command.

The command wipes existing business data and loads the fixed prototype dataset
(3 partners, 3 contracts, 3 supplier payments, 4 shipments) owned by the created
'Otabek Yo'ldoshev' user. ShipmentStatus reference rows and other users are
preserved. Re-running resets to the same baseline (wipe-then-load).
"""
from decimal import Decimal

from django.core.management import call_command

from accounts.models import User
from crm.models import Contract, Partner, Shipment, SupplierPayment


def test_creates_exact_dataset(db):
    call_command("load_starting_data", noinput=True)

    assert Partner.objects.count() == 3
    assert Contract.objects.count() == 3
    assert SupplierPayment.objects.count() == 3
    assert Shipment.objects.count() == 4

    owner = User.objects.get(username="otabek")
    assert owner.is_admin_role
    assert owner.is_superuser
    assert owner.get_full_name() == "Otabek Yo'ldoshev"
    assert all(c.created_by_id == owner.id for c in Contract.objects.all())


def test_method_and_status_mapping(db):
    call_command("load_starting_data", noinput=True)

    assert SupplierPayment.objects.filter(method="transfer").count() == 2
    assert SupplierPayment.objects.filter(method="cash").count() == 1

    statuses = set(Shipment.objects.values_list("status__name", flat=True))
    assert statuses == {"Yo'lda", "Chegarada", "Bojxona", "Tayyorlanmoqda"}

    yolda = Shipment.objects.get(container="MSCU-442109")
    assert yolda.note == "Logist: Akmal"
    assert yolda.kg == Decimal("20000.000")
    assert yolda.status.name == "Yo'lda"

    # Empty source dates become NULL; empty transport/container stay blank.
    prep = Shipment.objects.get(status__name="Tayyorlanmoqda")
    assert prep.sent is None
    assert prep.transport == ""
    assert prep.container == ""


def test_wipe_replaces_existing_data(db):
    call_command("seed_demo")
    assert Partner.objects.count() >= 2

    call_command("load_starting_data", noinput=True)

    assert Partner.objects.count() == 3
    assert set(Partner.objects.values_list("name", flat=True)) == {
        "Pars Polymer Co.", "Arya Petrochem", "Toshkent Polimer Savdo",
    }


def test_rerun_is_idempotent(db):
    call_command("load_starting_data", noinput=True)
    call_command("load_starting_data", noinput=True)

    assert Partner.objects.count() == 3
    assert Contract.objects.count() == 3
    assert SupplierPayment.objects.count() == 3
    assert Shipment.objects.count() == 4
    assert User.objects.filter(username="otabek").count() == 1
