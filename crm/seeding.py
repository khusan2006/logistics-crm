"""Shared helpers for the seed/import management commands.

Both `load_starting_data` (fixed baseline) and `import_prototype` (arbitrary
prototype JSON export) wipe the same business tables and attribute their rows to
the same owner user, so that logic lives here rather than being duplicated.
"""
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
OWNER_PASSWORD = "otabek12345"

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

    Idempotent: the password is only set on first creation. Returns the user.
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
    if created:
        owner.set_password(OWNER_PASSWORD)
        owner.save()
    return owner
