from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("crm", "0021_supplierpayment_commission")]

    operations = [
        migrations.AddField(
            model_name="shipment",
            name="driver_name",
            field=models.CharField(blank=True, max_length=120, verbose_name="Haydovchi"),
        ),
        migrations.AddField(
            model_name="shipment",
            name="driver_phone",
            field=models.CharField(blank=True, max_length=30,
                                   verbose_name="Haydovchi telefoni"),
        ),
    ]
