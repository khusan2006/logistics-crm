"""Seed a coherent demo dataset for previews/manual QA.

Replaces the old ad-hoc preview seed script with a real, idempotent management
command: 2 partners, 2 contracts, supplier payments, an arrived lot (with
expenses), an in-transit shipment, an overdue shipment, customers, a sale, a
customer payment (producing both a debt and an advance scenario), a
reservation, and the two demo users (admin/translator).

Safe to re-run: every row is created via get_or_create (or an explicit
existence guard) on a natural key, so running it twice never duplicates data.

Usage:
    python manage.py seed_demo
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from crm.models import (
    ContractLine,
    ShipmentLine,
    Contract,
    Customer,
    CustomerPayment,
    Partner,
    Reservation,
    Sale,
    Shipment,
    ShipmentExpense,
    ShipmentStatus,
    SupplierPayment,
    allocate_customer_payment,
)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin12345"
TRANSLATOR_USERNAME = "tarjimon"
TRANSLATOR_PASSWORD = "tarjimon12345"


class Command(BaseCommand):
    help = "Seed a coherent demo dataset (partners, contracts, lots, sales, payments, users)."

    def handle(self, *args, **options):
        with transaction.atomic():
            admin, translator = self._seed_users()
            partners = self._seed_partners()
            contracts = self._seed_contracts(partners, admin)
            self._seed_supplier_payments(contracts, admin)
            arrived_lot = self._seed_arrived_lot(contracts, admin)
            self._seed_in_transit_shipment(contracts, admin)
            self._seed_overdue_shipment(contracts, admin)
            customers = self._seed_customers()
            self._seed_sale_and_payments(arrived_lot, customers, admin)
            self._seed_reservation(contracts, customers, admin)

        self.stdout.write(self.style.SUCCESS(
            f"Demo ma'lumotlar tayyor: {Partner.objects.count()} hamkor, "
            f"{Contract.objects.count()} kelishuv, {Shipment.objects.count()} yuk, "
            f"{Customer.objects.count()} mijoz, {Sale.objects.count()} sotuv, "
            f"{Reservation.objects.count()} bron."
        ))
        self.stdout.write(
            f"Login: {ADMIN_USERNAME}/{ADMIN_PASSWORD} (admin), "
            f"{TRANSLATOR_USERNAME}/{TRANSLATOR_PASSWORD} (tarjimon)"
        )

    # -- Users -----------------------------------------------------------

    def _seed_users(self):
        admin, created = User.objects.get_or_create(
            username=ADMIN_USERNAME,
            defaults={
                "role": User.Role.ADMIN,
                "first_name": "Demo",
                "last_name": "Admin",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            admin.set_password(ADMIN_PASSWORD)
            admin.save()

        translator, created = User.objects.get_or_create(
            username=TRANSLATOR_USERNAME,
            defaults={
                "role": User.Role.TRANSLATOR,
                "first_name": "Demo",
                "last_name": "Tarjimon",
            },
        )
        if created:
            translator.set_password(TRANSLATOR_PASSWORD)
            translator.save()

        return admin, translator

    # -- Partners / contracts ---------------------------------------------

    def _seed_partners(self):
        pars, _ = Partner.objects.get_or_create(
            name="Pars Polymer", defaults={"phone": "+98 912 000 0001", "city": "Tehron"})
        jam, _ = Partner.objects.get_or_create(
            name="Jam Petrochemical", defaults={"phone": "+98 912 000 0002", "city": "Isfahon"})
        return {"pars": pars, "jam": jam}

    def _seed_contracts(self, partners, admin):
        today = timezone.localdate()
        c1, _ = Contract.objects.get_or_create(
            partner=partners["pars"], created=today - timedelta(days=60),
            defaults={
                "note": "Demo kelishuv", "created_by": admin,
            },
        )
        l1, _ = ContractLine.objects.get_or_create(
            contract=c1, brand="LLDPE 209",
            defaults={"kg": Decimal("50000"), "price": Decimal("1.05")},
        )
        c2, _ = Contract.objects.get_or_create(
            partner=partners["jam"], created=today - timedelta(days=30),
            defaults={
                "note": "Demo kelishuv", "created_by": admin,
            },
        )
        l2, _ = ContractLine.objects.get_or_create(
            contract=c2, brand="HDPE 5502",
            defaults={"kg": Decimal("30000"), "price": Decimal("1.15")},
        )
        return {"c1": c1, "c2": c2, "l1": l1, "l2": l2}

    def _seed_supplier_payments(self, contracts, admin):
        SupplierPayment.objects.get_or_create(
            contract=contracts["c1"], date=timezone.localdate() - timedelta(days=55),
            amount=Decimal("20000.00"),
            defaults={"method": "transfer", "note": "Demo boshlang'ich to'lov", "created_by": admin},
        )
        SupplierPayment.objects.get_or_create(
            contract=contracts["c2"], date=timezone.localdate() - timedelta(days=25),
            amount=Decimal("10000.00"),
            defaults={"method": "cash", "note": "Demo naqd to'lov", "created_by": admin},
        )

    # -- Shipments ---------------------------------------------------------

    def _seed_arrived_lot(self, contracts, admin):
        arrival_status = ShipmentStatus.arrival()
        today = timezone.localdate()
        lot, created = Shipment.objects.get_or_create(
            contract=contracts["c1"], container="DEMO-ARRIVED-01",
            defaults={
                "status": arrival_status,
                "sent": today - timedelta(days=40), "eta": today - timedelta(days=10),
                "arrived": today - timedelta(days=8), "transport": "TRK-001",
                "note": "Demo omborga kelgan lot", "created_by": admin,
            },
        )
        if created:
            ShipmentLine.objects.create(
                shipment=lot, contract_line=contracts["l1"], kg=Decimal("20000"))
            ShipmentExpense.objects.create(
                shipment=lot, date=today - timedelta(days=8), category="customs",
                amount=Decimal("800.00"), note="Demo bojxona xarajati", created_by=admin,
            )
            ShipmentExpense.objects.create(
                shipment=lot, date=today - timedelta(days=8), category="transport",
                amount=Decimal("400.00"), note="Demo transport xarajati", created_by=admin,
            )
        return lot

    def _seed_in_transit_shipment(self, contracts, admin):
        first_status = ShipmentStatus.objects.order_by("order", "id").first()
        today = timezone.localdate()
        transit, transit_created = Shipment.objects.get_or_create(
            contract=contracts["c2"], container="DEMO-TRANSIT-01",
            defaults={
                "status": first_status,
                "sent": today - timedelta(days=5), "eta": today + timedelta(days=7),
                "transport": "TRK-002", "note": "Demo yo'ldagi yuk", "created_by": admin,
            },
        )
        if transit_created:
            ShipmentLine.objects.create(
                shipment=transit, contract_line=contracts["l2"], kg=Decimal("8000"))

    def _seed_overdue_shipment(self, contracts, admin):
        first_status = ShipmentStatus.objects.order_by("order", "id").first()
        today = timezone.localdate()
        overdue, overdue_created = Shipment.objects.get_or_create(
            contract=contracts["c1"], container="DEMO-OVERDUE-01",
            defaults={
                "status": first_status,
                "sent": today - timedelta(days=20), "eta": today - timedelta(days=3),
                "transport": "TRK-003", "note": "Demo kechikkan yuk", "created_by": admin,
            },
        )
        if overdue_created:
            ShipmentLine.objects.create(
                shipment=overdue, contract_line=contracts["l1"], kg=Decimal("5000"))

    # -- Customers / sales / payments / reservations ------------------------

    def _seed_customers(self):
        akbar, _ = Customer.objects.get_or_create(
            name="Akbar Plastmassa MChJ", defaults={"phone": "+998 90 000 0001", "address": "Toshkent"})
        gulnora, _ = Customer.objects.get_or_create(
            name="Gulnora Savdo", defaults={"phone": "+998 90 000 0002", "address": "Samarqand"})
        return {"akbar": akbar, "gulnora": gulnora}

    def _seed_sale_and_payments(self, lot, customers, admin):
        today = timezone.localdate()
        sale, created = Sale.objects.get_or_create(
            customer=customers["akbar"], line=lot.lines.first(), date=today - timedelta(days=3),
            defaults={
                "kg": Decimal("5000"), "price": Decimal("1.35"),
                "cost_price": lot.lines.first().landed_cost_per_kg,
                "debt_deadline": today + timedelta(days=14),
                "note": "Demo sotuv", "created_by": admin,
            },
        )
        if created:
            # Partial payment: leaves a debt on this sale (qarz scenario).
            partial = CustomerPayment.objects.create(
                customer=customers["akbar"], date=today - timedelta(days=2),
                amount=Decimal("3000.00"), method="transfer",
                note="Demo qisman to'lov", created_by=admin,
            )
            allocate_customer_payment(partial)

            # Second customer pays more than they owe (no sale yet) -> advance (avans).
            CustomerPayment.objects.create(
                customer=customers["gulnora"], date=today - timedelta(days=1),
                amount=Decimal("1500.00"), method="cash",
                note="Demo avans to'lov", created_by=admin,
            )

    def _seed_reservation(self, contracts, customers, admin):
        in_transit = Shipment.objects.filter(
            contract=contracts["c2"], container="DEMO-TRANSIT-01").first()
        if in_transit is None:
            return
        Reservation.objects.get_or_create(
            customer=customers["gulnora"], line=in_transit.lines.first(),
            defaults={
                "kg": Decimal("2000"), "price": Decimal("1.40"),
                "status": Reservation.Status.ACTIVE,
                "note": "Demo bron", "created_by": admin,
            },
        )
