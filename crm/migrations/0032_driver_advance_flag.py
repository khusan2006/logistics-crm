"""Mark the one expense row the yuk form owns: the driver's advance.

A logist can pay a load's bojxona as well as its driver, so `logist` being set is
not enough to find the advance again when the yuk is edited. This flag is what the
form rewrites, so editing a yuk can never overwrite an unrelated expense.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("crm", "0031_logist")]

    operations = [
        migrations.AddField(
            model_name="shipmentexpense",
            name="is_driver_advance",
            field=models.BooleanField(default=False, editable=False,
                                      verbose_name="Haydovchi avansi"),
        ),
    ]
