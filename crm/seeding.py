"""Shared helpers for the seed/import management commands.

Both `load_starting_data` (fixed baseline) and `import_prototype` (arbitrary
prototype JSON export) wipe the same business tables and attribute their rows to
the same owner user, so that logic lives here rather than being duplicated.
"""
import os
import secrets

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
    SupplierPayment,
)

OWNER_USERNAME = "otabek"

# The owner's password is never written into the repository: a password in git is
# a password every past employee still knows. It comes from SEED_OWNER_PASSWORD,
# and when that is unset a fresh random one is generated so a first deploy is
# still usable — `ensure_owner` hands it back for the caller to show once.
OWNER_PASSWORD_ENV = "SEED_OWNER_PASSWORD"

# Children before parents — deleting in this order never trips a PROTECT FK.
# Reference data (ShipmentStatus) and auth users are deliberately NOT here.
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


def wipe_business_data():
    """Delete all CRM business rows, keeping ShipmentStatus and users intact."""
    for model in WIPE_MODELS:
        model.objects.all().delete()


def ensure_owner():
    """Get-or-create the 'Otabek Yo'ldoshev' owner (admin, staff+superuser).

    Idempotent: the password is only set on first creation, so a redeploy never
    resets a password the operator has since changed.

    Returns (owner, shown_once), where `shown_once` is the generated password the
    caller must display exactly once — or None when the account already existed or
    the password came from SEED_OWNER_PASSWORD (which must never be echoed).
    """
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
    if not created:
        return owner, None

    configured = os.environ.get(OWNER_PASSWORD_ENV, "").strip()
    shown_once = None if configured else secrets.token_urlsafe(15)
    owner.set_password(configured or shown_once)
    owner.save()
    return owner, shown_once
