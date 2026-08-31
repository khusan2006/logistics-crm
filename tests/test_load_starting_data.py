"""Tests for the `load_starting_data` baseline seed command.

The command wipes existing business data and loads the fixed prototype dataset
(3 partners, 3 contracts, 3 supplier payments, 4 shipments) owned by the created
'Otabek Yo'ldoshev' user. ShipmentStatus reference rows and other users are
preserved. Re-running resets to the same baseline (wipe-then-load).
"""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command

from accounts.models import User
from crm.models import Contract, Partner, Shipment, SupplierPayment
from crm.seeding import ensure_owner


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


# ── The owner's password ──────────────────────────────────────────────────────
#
# A password in the repository is a password every past employee still knows, and
# this account is a superuser. So: SEED_OWNER_PASSWORD when set, otherwise random.

def test_the_owner_password_comes_from_the_environment(db, monkeypatch):
    monkeypatch.setenv("SEED_OWNER_PASSWORD", "sirli-parol-123")
    owner, shown_once = ensure_owner()

    assert owner.check_password("sirli-parol-123")
    # Nothing to echo: the operator already knows a password they configured.
    assert shown_once is None


def test_without_the_env_var_the_owner_gets_a_random_password(db, monkeypatch):
    monkeypatch.delenv("SEED_OWNER_PASSWORD", raising=False)
    owner, shown_once = ensure_owner()

    assert shown_once, "birinchi kirish uchun parol qaytarilishi kerak"
    assert owner.check_password(shown_once)
    assert len(shown_once) >= 16
    # The password that used to sit in git must not be what anyone gets.
    assert not owner.check_password("otabek12345")


def test_the_command_prints_the_password_on_creation_and_never_again(db, monkeypatch):
    """A redeploy re-runs this command. It must not echo credentials every time —
    the first run is the one that has something the operator does not already know."""
    monkeypatch.delenv("SEED_OWNER_PASSWORD", raising=False)

    first = StringIO()
    call_command("load_starting_data", noinput=True, stdout=first)
    assert "Egasi yaratildi" in first.getvalue()

    second = StringIO()
    call_command("load_starting_data", noinput=True, stdout=second)
    assert "Egasi yaratildi" not in second.getvalue()


def test_a_configured_password_is_never_echoed(db, monkeypatch):
    monkeypatch.setenv("SEED_OWNER_PASSWORD", "sirli-parol-123")

    out = StringIO()
    call_command("load_starting_data", noinput=True, stdout=out)

    assert "sirli-parol-123" not in out.getvalue()


def test_a_rerun_never_resets_a_password_the_operator_changed(db, monkeypatch):
    monkeypatch.delenv("SEED_OWNER_PASSWORD", raising=False)
    owner, _first = ensure_owner()
    owner.set_password("operator-uni-ozgartirdi")
    owner.save()

    again, shown_once = ensure_owner()

    assert shown_once is None
    assert again.check_password("operator-uni-ozgartirdi")
