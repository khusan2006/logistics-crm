"""Give every existing sotuv the slice it has always implicitly had: all of its kg
against the one lot in `Sale.line`.

`pinned` is left False across the board, including for sotuvlar entered through the
lot picker. Those choices are real and a replay must not walk over them, but the two
kinds are indistinguishable in the old rows — a legacy sotuv and a hand-picked one
both just point at a lot. Marking everything pinned would freeze the whole history
and no correction could ever shift anything; marking nothing pinned leans on the
edit flow, which shows what will move and waits for a yes before it moves it.
New sotuvlar from the lot picker set the flag themselves.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Sale = apps.get_model("crm", "Sale")
    SaleLot = apps.get_model("crm", "SaleLot")
    SaleLot.objects.bulk_create([
        SaleLot(sale_id=pk, line_id=line_id, kg=kg, pinned=False)
        for pk, line_id, kg in Sale.objects.values_list("pk", "line_id", "kg")
    ])


def drop(apps, schema_editor):
    apps.get_model("crm", "SaleLot").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [("crm", "0043_salelot")]

    operations = [migrations.RunPython(backfill, drop)]
