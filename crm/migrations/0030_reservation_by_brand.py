"""A bron becomes a claim on a MARKA instead of on one lot.

The old shape pinned a reservation to a single ShipmentLine, so the mijoz waited
for one specific truck while the same granula could be sitting in the ombor from
another kelishuv. The new shape holds the marka and a FIFO queue position, and
`fulfilled_kg` lets a bron be filled in pieces as loads arrive.

`brand` is copied off the old lot before the column goes, so any existing bron
keeps pointing at the same granula rather than coming back blank.
"""

from django.db import migrations, models


def carry_brand_across(apps, schema_editor):
    """Fill the new column from each bron's old lot: line → contract_line → brand."""
    Reservation = apps.get_model("crm", "Reservation")
    for reservation in Reservation.objects.select_related(
            "line__contract_line").iterator():
        reservation.brand = reservation.line.contract_line.brand
        reservation.save(update_fields=["brand"])


def restore_lots(apps, schema_editor):
    """Reverse leg: the marka cannot say WHICH lot was reserved, so there is nothing
    faithful to put back. Refuse rather than invent a link."""
    Reservation = apps.get_model("crm", "Reservation")
    if Reservation.objects.exists():
        raise RuntimeError(
            "Bronlarni lotga qaytarib bo'lmaydi — marka qaysi lot ekanini bilmaydi")


class Migration(migrations.Migration):

    dependencies = [("crm", "0029_sale_debt_deadline_defaults_to_sale_date")]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="brand",
            field=models.CharField(db_index=True, default="", max_length=120,
                                   verbose_name="Marka"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="reservation",
            name="fulfilled_kg",
            field=models.DecimalField(
                decimal_places=3, default=0, max_digits=12,
                help_text="Sotuvga aylantirilgan qismi — qolgani navbatda turadi",
                verbose_name="Berilgan kg"),
        ),
        migrations.RunPython(carry_brand_across, restore_lots),
        migrations.RemoveField(model_name="reservation", name="line"),
    ]
