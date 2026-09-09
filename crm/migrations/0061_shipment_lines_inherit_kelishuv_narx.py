"""Hand every truck's tannarx back to its kelishuv.

A yuk line was always ALLOWED to carry no narx of its own — `ShipmentLine.unit_price`
falls back to the kelishuv product's price, live — but the form's JS copied the agreed
figure into the box on every create, so in practice no line ever inherited. The copy
then froze: editing the agreed narx moved the kelishuv and left every truck already
drawn from it costed at yesterday's price.

The form no longer writes the column (the box is read-only now), and this clears what
it wrote before, so existing loads inherit the same way new ones will.

Checked against production before it was written: all 56 rows carried a narx identical
to their kelishuv's, in the same currency at the same kurs — so no goods value, landed
cost, foyda or hamkor qarz moves.

Every row, deliberately, rather than only the ones that still match. A kelishuv narx
corrected between that check and this deploy is the exact case the change is for: those
trucks SHOULD follow it, and sparing them because they no longer match would leave
behind precisely the loads that prompted the complaint.

It is not reversible: the figures it removes are recoverable from the kelishuv exactly
because they were never anything else.
"""
from django.db import migrations, models


def inherit(apps, schema_editor):
    ShipmentLine = apps.get_model("crm", "ShipmentLine")
    ShipmentLine.objects.exclude(price=None, price_uzs=None).update(
        price=None, price_uzs=None)


class Migration(migrations.Migration):

    dependencies = [("crm", "0060_sale_reys")]

    operations = [
        migrations.RunPython(inherit, migrations.RunPython.noop),
        # The column's own note said "leave it blank and the kelishuv narx is used",
        # which described a choice the form no longer offers.
        migrations.AlterField(
            model_name='shipmentline',
            name='price',
            field=models.DecimalField(blank=True, decimal_places=4, help_text="Bo'sh — tannarx kelishuvdan olinadi", max_digits=14, null=True, verbose_name='1 kg narxi (USD)'),
        ),
    ]
