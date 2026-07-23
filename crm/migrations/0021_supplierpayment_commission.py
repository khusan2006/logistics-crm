from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("crm", "0020_contract_and_shipment_lines")]

    operations = [
        migrations.AddField(
            model_name="supplierpayment",
            name="commission_percent",
            field=models.DecimalField(
                blank=True, decimal_places=2, default=0,
                help_text="Vositachisiz to'lov uchun bo'sh qoldiring",
                max_digits=5, verbose_name="Vositachi foizi (%)"),
        ),
    ]
