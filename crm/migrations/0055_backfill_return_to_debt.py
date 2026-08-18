"""Fill in the split on vazvrat lines written before it was recorded.

`to_debt` is history — what the sotuv owed the moment the goods came back — and
history cannot be re-derived from today's qoldiq. What CAN be reproduced exactly is
the figure those rows were being reported with until now: everything that was not
handed back in cash. So that is what is written here, which leaves every existing
row saying precisely what it already said, and makes new rows able to say more.
"""
from decimal import Decimal

from django.db import migrations


def backfill(apps, schema_editor):
    ReturnBatch = apps.get_model("crm", "ReturnBatch")
    Return = apps.get_model("crm", "Return")

    # Lines that belong to no visit only ever cancelled qarz — they predate the
    # settlement rows entirely.
    for line in Return.objects.filter(batch__isnull=True):
        line.to_debt = (line.kg * line.price).quantize(Decimal("0.01"))
        line.to_debt_uzs = (line.kg * line.price_uzs).quantize(Decimal("0.01"))
        line.save(update_fields=["to_debt", "to_debt_uzs"])

    for batch in ReturnBatch.objects.prefetch_related("lines", "settlements"):
        refunds, refunds_uzs = {}, {}
        for settlement in batch.settlements.all():
            refunds[settlement.currency] = (
                refunds.get(settlement.currency, Decimal("0")) + settlement.amount)
            refunds_uzs[settlement.currency] = (
                refunds_uzs.get(settlement.currency, Decimal("0"))
                + settlement.amount_uzs)

        # Per currency, the refund comes off the lines of that currency in order,
        # oldest first — the same way any pool is spent down.
        for line in batch.lines.all():
            value = (line.kg * line.price).quantize(Decimal("0.01"))
            value_uzs = (line.kg * line.price_uzs).quantize(Decimal("0.01"))
            take = min(value, refunds.get(line.currency, Decimal("0")))
            take_uzs = min(value_uzs, refunds_uzs.get(line.currency, Decimal("0")))
            refunds[line.currency] = refunds.get(line.currency, Decimal("0")) - take
            refunds_uzs[line.currency] = (
                refunds_uzs.get(line.currency, Decimal("0")) - take_uzs)
            line.to_debt = value - take
            line.to_debt_uzs = value_uzs - take_uzs
            line.save(update_fields=["to_debt", "to_debt_uzs"])


class Migration(migrations.Migration):

    dependencies = [("crm", "0054_return_to_debt_return_to_debt_uzs")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
