from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("crm", "0022_shipment_driver")]

    operations = [
        migrations.AddField(
            model_name="shipment",
            name="responsible",
            field=models.CharField(blank=True, max_length=120,
                                   verbose_name="Mas'ul shaxs"),
        ),
    ]
