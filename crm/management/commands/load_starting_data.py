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
