"""Backfill the muddat on sotuvlar that never got one.

`Sale.save` now treats a missing To'lov muddati as "due the day it was sold", but
the rows already in the database kept their NULL — and a NULL muddat is invisible
to every Qarzlar signal, so an unpaid sotuv nobody put a date on never showed up as
due. This applies the same rule backwards.

Not reversible in any meaningful sense: which rows were NULL before is exactly the
information being thrown away, so the reverse is a no-op rather than a guess that
would blank out muddatlar the operator actually typed.
"""
from django.db import migrations, models


def backfill(apps, schema_editor):
    Sale = apps.get_model("crm", "Sale")
    Sale.objects.filter(debt_deadline__isnull=True).update(debt_deadline=models.F("date"))


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0028_alter_shipmentexpense_category"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
