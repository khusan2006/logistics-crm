"""Drop the "Yuklandi" birja holat.

It was one of the three placeholders 0063 seeded for a chain nobody had named yet.
The birja pipeline does not use it: a birja yuk is entered once it is bought, or
straight onto "Omborga yetib keldi".

`Shipment.status` is PROTECT, so a yuk still sitting on the row stops the
migration with its numbers rather than being moved to a holat nobody chose.
"""
from django.db import migrations

NAME, SCOPE, ORDER = "Yuklandi", "birja", 20


def drop(apps, schema_editor):
    ShipmentStatus = apps.get_model("crm", "ShipmentStatus")
    Shipment = apps.get_model("crm", "Shipment")
    row = ShipmentStatus.objects.filter(name=NAME, scope=SCOPE).first()
    if row is None:
        return
    in_use = list(Shipment.objects.filter(status=row).values_list("pk", flat=True))
    if in_use:
        raise RuntimeError(
            f"«{NAME}» birja holatida yuklar bor ({in_use}). Ularni boshqa holatga "
            f"o'tkazing, keyin migratsiyani qayta ishga tushiring.")
    row.delete()


def restore(apps, schema_editor):
    ShipmentStatus = apps.get_model("crm", "ShipmentStatus")
    ShipmentStatus.objects.get_or_create(
        name=NAME, scope=SCOPE, defaults={"order": ORDER, "is_arrival": False})


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0075_logist_commission"),
    ]

    operations = [
        migrations.RunPython(drop, restore),
    ]
