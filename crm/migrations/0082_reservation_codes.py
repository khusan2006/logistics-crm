"""Bron codes — komoliddin-sintafon-3 — numbered per mijoz, as kelishuv codes are
per hamkor.

Existing brons are numbered in the order they were struck, per mijoz slug, and each
mijoz's counter is left at the highest number issued so the next bron carries on."""

from collections import defaultdict

from django.db import migrations, models
from django.utils.text import slugify


def _slug(name):
    # Frozen copy of `customer_code_slug`: a migration must not follow later edits.
    return slugify(name, allow_unicode=True) or "mijoz"


def number_brons(apps, schema_editor):
    Customer = apps.get_model("crm", "Customer")
    Reservation = apps.get_model("crm", "Reservation")
    slugs = {}
    for customer in Customer.objects.all():
        customer.code_slug = slugs[customer.pk] = _slug(customer.name)
        customer.save(update_fields=["code_slug"])
    issued = defaultdict(int)          # slug → last number
    top = defaultdict(int)             # customer pk → their highest number
    for bron in Reservation.objects.order_by("created_at", "pk"):
        slug = slugs[bron.customer_id]
        issued[slug] += 1
        bron.code_slug, bron.code_number = slug, issued[slug]
        bron.save(update_fields=["code_slug", "code_number"])
        top[bron.customer_id] = issued[slug]
    for customer_id, number in top.items():
        Customer.objects.filter(pk=customer_id).update(code_counter=number)


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0081_reservation_draws'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='code_slug',
            field=models.CharField(db_index=True, default='', editable=False, max_length=200),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='customer',
            name='code_counter',
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name='reservation',
            name='code_slug',
            field=models.CharField(db_index=True, default='', editable=False, max_length=200),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='reservation',
            name='code_number',
            field=models.PositiveIntegerField(default=0, editable=False),
            preserve_default=False,
        ),
        migrations.RunPython(number_brons, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='reservation',
            constraint=models.UniqueConstraint(fields=('code_slug', 'code_number'),
                                               name='unique_reservation_code'),
        ),
    ]
