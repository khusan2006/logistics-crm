"""Bron sanasi — the day the mijoz actually asked, editable on the form.

Existing brons get the day they were typed in, read in Tashkent time, which is what
the screens showed for them until now."""

import django.utils.timezone
from django.db import migrations, models
from django.utils import timezone


def date_from_entry(apps, schema_editor):
    Reservation = apps.get_model("crm", "Reservation")
    for bron in Reservation.objects.all():
        bron.date = timezone.localdate(bron.created_at)
        bron.save(update_fields=["date"])


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0084_scale_require_id'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='reservation',
            options={'ordering': ['-date', '-created_at'], 'verbose_name': 'Bron', 'verbose_name_plural': 'Bronlar'},
        ),
        migrations.AddField(
            model_name='reservation',
            name='date',
            field=models.DateField(default=django.utils.timezone.localdate, verbose_name='Bron sanasi'),
        ),
        migrations.RunPython(date_from_entry, migrations.RunPython.noop),
    ]
