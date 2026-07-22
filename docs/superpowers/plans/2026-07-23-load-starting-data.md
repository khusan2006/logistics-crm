# Load Starting Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `load_starting_data` management command that wipes existing CRM business data and loads the fixed prototype dataset (3 partners, 3 contracts, 3 supplier payments, 4 shipments) owned by a new "Otabek Yo'ldoshev" user, then run it against the dev DB.

**Architecture:** A single Django management command in `crm/management/commands/`. It runs one destructive `transaction.atomic()` block: wipe business tables in FK-safe (children-first) order, create the owner user, load the dataset from in-module constants. Reference data (`ShipmentStatus`) and existing auth users are preserved. Confirmation is required unless `--noinput` is passed.

**Tech Stack:** Django management command, pytest / pytest-django (`db` fixture, `call_command`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-23-load-starting-data-design.md`.
- Load the dataset **faithfully as-is** — do not "correct" the source's odd dates (contract `created` == `deadline`; some payments predate their contract).
- Method mapping: "Bank o'tkazmasi" → `transfer`, "Naqd" → `cash` (verified `PayMethod` values: `cash`, `card`, `transfer`).
- Statuses resolved from existing `ShipmentStatus` rows **by name**: `Yo'lda`, `Chegarada`, `Bojxona`, `Tayyorlanmoqda` (seeded by migration `0006`). Never create/wipe `ShipmentStatus`.
- `logist` → shipment `note` as `"Logist: <name>"`. Skip `audit`, `settings.usdRate`, and the empty `sales`/`cashEntries`/`debtPayments`.
- Owner: username `otabek`, `role=admin`, staff+superuser, temp password `otabek12345`, created via `get_or_create` (idempotent). All loaded records set `created_by=owner`.
- Empty date strings (`""`) → `NULL`.
- Do **not** wipe auth users; do **not** touch the existing `seed_demo` command.
- Follow the existing command style in `crm/management/commands/seed_demo.py` and the test style in `tests/test_seed_and_dashboard.py`.

---

## File Structure

- **Create** `crm/management/commands/load_starting_data.py` — the command (wipe + owner + load), dataset constants, confirmation guard.
- **Create** `tests/test_load_starting_data.py` — command tests (counts, mapping, wipe, idempotency).
- **Run** the command against `dev.sqlite3` (Task 2) — no file change, but the deliverable is the data actually landing in the dev DB.

---

### Task 1: `load_starting_data` command + tests

**Files:**
- Create: `crm/management/commands/load_starting_data.py`
- Test: `tests/test_load_starting_data.py`

**Interfaces:**
- Consumes: `crm.models` (`Partner`, `Contract`, `SupplierPayment`, `Shipment`, `ShipmentStatus`, and the wipe-list models), `accounts.models.User`.
- Produces: management command `load_starting_data` with a `--noinput`/`--no-input` flag (dest `noinput`). After a run: exactly 3 `Partner`, 3 `Contract`, 3 `SupplierPayment`, 4 `Shipment`, and a `User(username="otabek")`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_load_starting_data.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_load_starting_data.py -v`
Expected: FAIL — `CommandError: Unknown command: 'load_starting_data'` (command not created yet).

- [ ] **Step 3: Write the command**

Create `crm/management/commands/load_starting_data.py`:

```python
"""Wipe CRM business data and load the canonical starting dataset (baseline).

Loads the fixed prototype dataset — 3 partners, 3 contracts, 3 supplier
payments, 4 shipments — owned by the created 'Otabek Yo'ldoshev' user.
Reference data (ShipmentStatus) and existing auth users are preserved.

DESTRUCTIVE. Prompts for confirmation unless --noinput is given. Everything
runs in one atomic transaction, so a failure leaves the DB untouched. Because
it wipes-then-loads, re-running just resets the DB to this exact baseline.

Usage:
    python manage.py load_starting_data
    python manage.py load_starting_data --noinput
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from crm.models import (
    AuditLog,
    Contract,
    Customer,
    CustomerPayment,
    Partner,
    PaymentAllocation,
    Reservation,
    Return,
    Sale,
    Shipment,
    ShipmentDelay,
    ShipmentExpense,
    ShipmentLeg,
    ShipmentStatus,
    SupplierPayment,
)

OWNER_USERNAME = "otabek"
OWNER_PASSWORD = "otabek12345"

# Children before parents — deleting in this order never trips a PROTECT FK.
WIPE_MODELS = [
    PaymentAllocation,
    Return,
    CustomerPayment,
    Sale,
    Reservation,
    ShipmentExpense,
    ShipmentDelay,
    ShipmentLeg,
    Shipment,
    SupplierPayment,
    Contract,
    Customer,
    Partner,
    AuditLog,
]

PARTNERS = [
    {"name": "Pars Polymer Co.", "phone": "+98 912 440 1122", "city": "Tehron",
     "note": "Asosiy yetkazib beruvchi"},
    {"name": "Arya Petrochem", "phone": "+98 917 201 8877", "city": "Shiroz",
     "note": "HDPE va LDPE"},
    {"name": "Toshkent Polimer Savdo", "phone": "+998 90 555 44 33", "city": "Toshkent",
     "note": "Mahalliy hamkor"},
]

# Contracts/payments/shipments reference their contract by `brand` (unique here).
CONTRACTS = [
    {"partner": "Pars Polymer Co.", "brand": "LLDPE 209AA", "kg": "50000",
     "price": "0.96", "created": "2026-07-28", "deadline": "2026-07-28"},
    {"partner": "Arya Petrochem", "brand": "HDPE 7000F", "kg": "30000",
     "price": "1.05", "created": "2026-08-05", "deadline": "2026-08-05"},
    {"partner": "Pars Polymer Co.", "brand": "LDPE 2420H", "kg": "20000",
     "price": "1.12", "created": "2026-08-12", "deadline": "2026-08-12"},
]

PAYMENTS = [
    {"brand": "LLDPE 209AA", "amount": "18000", "date": "2026-07-02", "method": "transfer"},
    {"brand": "LLDPE 209AA", "amount": "12000", "date": "2026-07-09", "method": "transfer"},
    {"brand": "HDPE 7000F", "amount": "10000", "date": "2026-07-11", "method": "cash"},
]

SHIPMENTS = [
    {"brand": "LLDPE 209AA", "kg": "20000", "status": "Yo'lda", "sent": "2026-07-06",
     "eta": "2026-07-19", "arrived": "", "transport": "01 777 AAA",
     "container": "MSCU-442109", "logist": "Akmal"},
    {"brand": "LLDPE 209AA", "kg": "15000", "status": "Chegarada", "sent": "2026-07-08",
     "eta": "2026-07-17", "arrived": "", "transport": "10 888 BBB",
     "container": "TGHU-771200", "logist": "Javlon"},
    {"brand": "HDPE 7000F", "kg": "12000", "status": "Bojxona", "sent": "2026-07-03",
     "eta": "2026-07-14", "arrived": "", "transport": "01 909 CCC",
     "container": "CAIU-902811", "logist": "Akmal"},
    {"brand": "LDPE 2420H", "kg": "8000", "status": "Tayyorlanmoqda", "sent": "",
     "eta": "2026-07-29", "arrived": "", "transport": "", "container": "",
     "logist": "Javlon"},
]


def _d(value):
    """ISO date string -> date, or None for the source's empty strings."""
    return date.fromisoformat(value) if value else None


class Command(BaseCommand):
    help = "Wipe business data and load the canonical starting dataset."

    def add_arguments(self, parser):
        parser.add_argument(
            "--noinput", "--no-input", action="store_true", dest="noinput",
            help="Skip the destructive-action confirmation prompt.",
        )

    def handle(self, *args, **options):
        if not options["noinput"]:
            self.stdout.write(self.style.WARNING(
                "DIQQAT: bu buyruq BARCHA biznes ma'lumotlarini o'chiradi va "
                "boshlang'ich ma'lumotlar bilan almashtiradi."
            ))
            if input("Davom etish uchun 'yes' deb yozing: ").strip().lower() != "yes":
                raise CommandError("Bekor qilindi.")

        with transaction.atomic():
            self._wipe()
            owner = self._owner()
            self._load(owner)

        self.stdout.write(self.style.SUCCESS(
            f"Boshlang'ich ma'lumotlar yuklandi: {Partner.objects.count()} hamkor, "
            f"{Contract.objects.count()} kelishuv, {SupplierPayment.objects.count()} to'lov, "
            f"{Shipment.objects.count()} yuk."
        ))
        self.stdout.write(self.style.WARNING(
            f"Egasi: {OWNER_USERNAME} / {OWNER_PASSWORD} — prodda parolni o'zgartiring."
        ))

    def _wipe(self):
        for model in WIPE_MODELS:
            model.objects.all().delete()

    def _owner(self):
        owner, created = User.objects.get_or_create(
            username=OWNER_USERNAME,
            defaults={
                "role": User.Role.ADMIN,
                "first_name": "Otabek",
                "last_name": "Yo'ldoshev",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            owner.set_password(OWNER_PASSWORD)
            owner.save()
        return owner

    def _load(self, owner):
        partners = {
            row["name"]: Partner.objects.create(
                name=row["name"], phone=row["phone"], city=row["city"], note=row["note"],
            )
            for row in PARTNERS
        }

        contracts = {}  # keyed by brand
        for row in CONTRACTS:
            contracts[row["brand"]] = Contract.objects.create(
                partner=partners[row["partner"]], brand=row["brand"],
                kg=Decimal(row["kg"]), price=Decimal(row["price"]),
                created=_d(row["created"]), deadline=_d(row["deadline"]),
                created_by=owner,
            )

        for row in PAYMENTS:
            SupplierPayment.objects.create(
                contract=contracts[row["brand"]], amount=Decimal(row["amount"]),
                date=_d(row["date"]), method=row["method"], created_by=owner,
            )

        status_by_name = {s.name: s for s in ShipmentStatus.objects.all()}
        for row in SHIPMENTS:
            status = status_by_name.get(row["status"])
            if status is None:
                raise CommandError(
                    f"ShipmentStatus '{row['status']}' topilmadi — migratsiyalar qo'llanganmi?"
                )
            Shipment.objects.create(
                contract=contracts[row["brand"]], kg=Decimal(row["kg"]), status=status,
                sent=_d(row["sent"]), eta=_d(row["eta"]), arrived=_d(row["arrived"]),
                transport=row["transport"], container=row["container"],
                note=f"Logist: {row['logist']}", created_by=owner,
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_load_starting_data.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass (the new command touches no shared state outside its own transaction).

- [ ] **Step 6: Commit**

```bash
git add crm/management/commands/load_starting_data.py tests/test_load_starting_data.py
git commit -m "feat: load_starting_data command — wipe + load baseline dataset"
```

---

### Task 2: Load the dataset into the dev DB

**Files:** none changed — the deliverable is the data present in `dev.sqlite3`.

**Interfaces:**
- Consumes: the `load_starting_data` command from Task 1.

- [ ] **Step 1: Run the command against the dev DB**

Run: `.venv/bin/python manage.py load_starting_data --noinput`
Expected: success line — `... 3 hamkor, 3 kelishuv, 3 to'lov, 4 yuk.` and the owner credentials line.

- [ ] **Step 2: Verify the data landed**

Run:
```bash
.venv/bin/python manage.py shell -c "from crm.models import Partner, Contract, SupplierPayment, Shipment; from accounts.models import User; print(Partner.objects.count(), Contract.objects.count(), SupplierPayment.objects.count(), Shipment.objects.count(), User.objects.filter(username='otabek').exists())"
```
Expected: `3 3 3 4 True`

- [ ] **Step 3 (optional visual check): start the dev server and open the dashboard**

Use the preview tooling (dev server), log in as `otabek / otabek12345`, and confirm partners/contracts/shipments render. Screenshot for the user.

---

## Self-Review

**Spec coverage:**
- Management command runnable on prod → Task 1 (command), `--noinput` for non-interactive/prod use. ✅
- Wipe business data, FK-safe, keep ShipmentStatus + users → `WIPE_MODELS` order + `_wipe`, statuses/users untouched, `test_wipe_replaces_existing_data`. ✅
- Owner user Otabek, temp password, idempotent → `_owner` via `get_or_create`, `test_rerun_is_idempotent`. ✅
- Dataset (3/3/3/4), method + status mapping, logist→note, empty dates→NULL, faithful dates → constants + `_load`, `test_creates_exact_dataset` / `test_method_and_status_mapping`. ✅
- Skip audit/usdRate/empty collections → not referenced. ✅
- Atomic + confirmation guard → `transaction.atomic()` + prompt. ✅
- Actually load into the DB → Task 2. ✅

**Placeholder scan:** none — all steps carry full code/commands.

**Type consistency:** `noinput` dest matches `call_command(..., noinput=True)`; contracts keyed by `brand` consistently across CONTRACTS/PAYMENTS/SHIPMENTS; `_d`/`OWNER_*`/`WIPE_MODELS` names consistent.
