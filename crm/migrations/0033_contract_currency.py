"""A kelishuv gets its own currency, and its rows are pulled onto it.

Until now the currency lived on each product row, so "what is still owed on this
kelishuv" had no single currency to be owed in and every qarz was measured in
dollars — which a so'm kelishuv can never be settled in once the kurs moves.

The header takes the currency its rows were actually struck in (they are all one
currency in practice; a mixed one falls to whichever currency carries the most
value), and the rows are then pinned to the header so the two can never drift.
"""

from django.db import migrations, models


def adopt_line_currency(apps, schema_editor):
    Contract = apps.get_model("crm", "Contract")
    for contract in Contract.objects.prefetch_related("lines"):
        weight = {}
        for line in contract.lines.all():
            weight[line.currency] = weight.get(line.currency, 0) + (line.kg or 0) * (line.price or 0)
        if not weight:
            continue
        currency = max(weight, key=weight.get)
        if currency != contract.currency:
            contract.currency = currency
            contract.save(update_fields=["currency"])


def pin_rows_to_contract(apps, schema_editor):
    """Every priced row under a kelishuv now reads in the kelishuv's currency."""
    ContractLine = apps.get_model("crm", "ContractLine")
    ShipmentLine = apps.get_model("crm", "ShipmentLine")
    for line in ContractLine.objects.select_related("contract"):
        if line.currency != line.contract.currency:
            line.currency = line.contract.currency
            line.save(update_fields=["currency"])
    for line in ShipmentLine.objects.select_related("contract_line__contract"):
        currency = line.contract_line.contract.currency
        if line.currency != currency:
            line.currency = currency
            line.save(update_fields=["currency"])


def noop(apps, schema_editor):
    """Nothing to undo: the rows keep the currency they already had, and dropping
    the column takes the header's copy with it."""


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0032_driver_advance_flag'),
    ]

    operations = [
        migrations.AddField(
            model_name='contract',
            name='currency',
            field=models.CharField(choices=[('usd', 'Dollar'), ('uzs', "So'm")], default='usd', max_length=3, verbose_name='Valyuta'),
        ),
        migrations.RunPython(adopt_line_currency, noop),
        migrations.RunPython(pin_rows_to_contract, noop),
    ]
