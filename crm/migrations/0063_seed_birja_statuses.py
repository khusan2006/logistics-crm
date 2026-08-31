"""Split the holat chain in two.

Everything already in the table came from the Eron road, so it stays `hamkor` —
except the arrival row, which becomes `umumiy`: reaching it is what turns a yuk
into an ombor loti, and both pipelines have to end there or a birja purchase could
never land on the shelf.

The arrival row is also pushed to the end of the number line. Ordering is global
(`Meta.ordering = ["order", "id"]`), so a birja holat numbered after the Eron chain
would otherwise sort BELOW "Omborga yetib keldi" and the birja pipeline would read
as though loads arrive before they are dispatched.

The three birja holatlar are placeholders. The operator has not settled what the
real chain looks like yet, so these are named for the steps a birja purchase
obviously takes and are meant to be renamed on the Holatlar page.
"""
from django.db import migrations

# Well clear of anything `status_create` will ever hand out on its own, so the
# arrival row stays last however many holatlar are added to either chain.
ARRIVAL_ORDER = 900

BIRJA_STATUSES = [("Sotib olindi", 10), ("Yuklandi", 20), ("Yetkazilmoqda", 30)]


def seed(apps, schema_editor):
    ShipmentStatus = apps.get_model("crm", "ShipmentStatus")
    # Everything existing is an Eron holat; the field default already says so, and
    # this is only here so a re-run after a partial deploy cannot leave a gap.
    ShipmentStatus.objects.filter(is_arrival=False).update(scope="hamkor")
    ShipmentStatus.objects.filter(is_arrival=True).update(scope="umumiy",
                                                          order=ARRIVAL_ORDER)
    for name, order in BIRJA_STATUSES:
        ShipmentStatus.objects.get_or_create(
            name=name, scope="birja", defaults={"order": order, "is_arrival": False})


def unseed(apps, schema_editor):
    """Drop only the seeded birja rows, and only while nothing is using them —
    a yuk on one is a PROTECT that must surface rather than be worked around."""
    ShipmentStatus = apps.get_model("crm", "ShipmentStatus")
    ShipmentStatus.objects.filter(scope="birja",
                                  name__in=[n for n, _ in BIRJA_STATUSES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0062_birja"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
