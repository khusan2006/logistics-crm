"""Nechta mashina moves from the kelishuv onto each of its mahsulotlar, and a
hamkor to'lovi gains the product it was paid against.

The operation order matters: both new columns are added, the existing kelishuv
plans are carried onto the rows, and only then is the old column dropped.
"""

import django.db.models.deletion
from django.db import migrations, models


def spread_onto_lines(apps, schema_editor):
    """Carry each kelishuv's truck plan down onto its products.

    A one-product kelishuv is exact — the number was always about that product.
    A multi-product one has to be split, and it is split by KG, because kg is
    what fills a truck: a 120 000 kg product and a 600 kg one on a 5-truck
    kelishuv were never 2.5 trucks each.

    Largest remainder, so the shares still add to the number that was typed. A
    share that rounds down to nothing is left NULL rather than stored as 0 — the
    600 kg tail of a kelishuv is a rounding error on a truck, not a plan for no
    trucks, and blank is what "no target" already means everywhere else.
    """
    Contract = apps.get_model("crm", "Contract")
    ContractLine = apps.get_model("crm", "ContractLine")

    for contract in Contract.objects.exclude(planned_trucks=None).prefetch_related("lines"):
        planned = contract.planned_trucks
        lines = list(contract.lines.all())
        if not lines or not planned:
            continue
        if len(lines) == 1:
            ContractLine.objects.filter(pk=lines[0].pk).update(planned_trucks=planned)
            continue

        total_kg = sum(line.kg for line in lines)
        if not total_kg:
            continue
        exact = [(line, planned * line.kg / total_kg) for line in lines]
        shares = {line.pk: int(value) for line, value in exact}
        # Hand the trucks lost to rounding to the biggest remainders first.
        left = planned - sum(shares.values())
        for line, value in sorted(exact, key=lambda pair: pair[1] - int(pair[1]),
                                  reverse=True)[:left]:
            shares[line.pk] += 1
        for pk, count in shares.items():
            ContractLine.objects.filter(pk=pk).update(planned_trucks=count or None)


def gather_back_onto_contracts(apps, schema_editor):
    """Reverse: the kelishuv's plan becomes the sum of its products' again."""
    Contract = apps.get_model("crm", "Contract")
    for contract in Contract.objects.prefetch_related("lines"):
        counts = [line.planned_trucks for line in contract.lines.all()
                  if line.planned_trucks]
        Contract.objects.filter(pk=contract.pk).update(
            planned_trucks=sum(counts) if counts else None)


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0045_fix_2102_campaund_fifo'),
    ]

    operations = [
        migrations.AddField(
            model_name='contractline',
            name='planned_trucks',
            field=models.PositiveIntegerField(blank=True, help_text='Shu mahsulot uchun rejalashtirilgan mashinalar soni', null=True, verbose_name='Nechta mashina'),
        ),
        migrations.AddField(
            model_name='supplierpayment',
            name='contract_line',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='supplier_payments', to='crm.contractline', verbose_name='Mahsulot'),
        ),
        migrations.RunPython(spread_onto_lines, gather_back_onto_contracts),
        migrations.RemoveField(
            model_name='contract',
            name='planned_trucks',
        ),
    ]
