"""Tests for the `seed_demo` management command and the dashboard's sell-side KPIs.

`seed_demo` replaces the ad-hoc preview seed script: it builds a coherent demo
dataset (partners, contracts, an arrived lot with expenses, an in-transit and an
overdue shipment, customers, a sale, a customer payment, a reservation, and the
two demo users) and is idempotent — re-running it must not duplicate rows.
"""
from django.core.management import call_command

from accounts.models import User
from crm.models import Contract, Partner, Reservation, Sale


def test_seed_demo_creates_dataset(db):
    call_command("seed_demo")

    assert Partner.objects.count() >= 2
    assert Contract.objects.count() >= 2
    assert Sale.objects.count() >= 1
    assert Reservation.objects.count() >= 1

    admin = User.objects.get(username="admin")
    assert admin.is_admin_role
    translator = User.objects.get(username="tarjimon")
    assert translator.role == User.Role.TRANSLATOR


def test_seed_demo_is_idempotent(db):
    call_command("seed_demo")
    counts_1 = {
        "partners": Partner.objects.count(),
        "contracts": Contract.objects.count(),
        "sales": Sale.objects.count(),
        "reservations": Reservation.objects.count(),
        "users": User.objects.count(),
    }

    call_command("seed_demo")
    counts_2 = {
        "partners": Partner.objects.count(),
        "contracts": Contract.objects.count(),
        "sales": Sale.objects.count(),
        "reservations": Reservation.objects.count(),
        "users": User.objects.count(),
    }

    assert counts_1 == counts_2


def test_dashboard_shows_sell_side_kpis(admin_client, db):
    call_command("seed_demo")
    html = admin_client.get("/").content.decode()
    assert "Omborda qoldiq" in html
    assert "Mijoz qarzi" in html
    assert "Sotuvdan foyda" in html
