"""A to'lov taqsimoti carries its so'm figure instead of slicing it off the parent.

The so'm value was a property: the same fraction of the to'lov's so'm side that the
dollar column is of its dollar side. That was right while every qarz was measured in
dollars. It is not enough now that a sotuv is settled in the currency it was agreed
in — a so'm sotuv paid off by a so'm to'lov has to land on exactly zero, and a figure
re-derived through the dollar column lands a tiyin off it.

The back-fill stores precisely what the property returned, so no balance moves.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations, models


def store_the_slice(apps, schema_editor):
    """Freeze each allocation's so'm value at what it has been reading all along."""
    PaymentAllocation = apps.get_model("crm", "PaymentAllocation")
    rows = []
    for alloc in PaymentAllocation.objects.select_related("payment"):
        payment = alloc.payment
        if payment.amount:
            alloc.amount_uzs = (payment.amount_uzs * alloc.amount / payment.amount
                                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            alloc.amount_uzs = Decimal("0")
        rows.append(alloc)
    PaymentAllocation.objects.bulk_update(rows, ["amount_uzs"], batch_size=500)


def noop(apps, schema_editor):
    """Nothing to undo: dropping the column takes the stored figure with it, and the
    property it replaced computed the same number."""


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0033_contract_currency'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentallocation',
            name='amount_uzs',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=18, verbose_name="Summa (so'm)"),
        ),
        migrations.RunPython(store_the_slice, noop),
    ]
