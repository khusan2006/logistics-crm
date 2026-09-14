from django.db import migrations
from django.db.models import F


def convert_served_brons(apps, schema_editor):
    """Brons whose kg were edited down to exactly what was already handed over.

    The edit used to save them without touching the status, so they stayed ACTIVE
    with 0 kg left and sat on Faol as "Mol kutilmoqda". They are served, and end
    the way `draw_down_bron` ends one: CONVERTED."""
    Reservation = apps.get_model("crm", "Reservation")
    Reservation.objects.filter(status="active", fulfilled_kg__gte=F("kg")).update(
        status="converted")


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0072_contract_closed_short'),
    ]

    operations = [
        migrations.RunPython(convert_served_brons, migrations.RunPython.noop),
    ]
