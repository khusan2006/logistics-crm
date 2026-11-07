"""A bron may name several interchangeable markalar, each at its own narx.

The marka and the narx move off `Reservation` onto `ReservationItem`; the kg stays on
the bron, shared by all its markalar. Every existing bron becomes a one-marka bron
carrying exactly the marka and narx it had, so nothing it reports changes."""

import django.db.models.deletion
from django.db import migrations, models


def copy_into_items(apps, schema_editor):
    Reservation = apps.get_model("crm", "Reservation")
    ReservationItem = apps.get_model("crm", "ReservationItem")
    ReservationItem.objects.bulk_create(
        ReservationItem(reservation_id=bron.pk, brand=bron.brand,
                        price=bron.price, price_uzs=bron.price_uzs)
        for bron in Reservation.objects.order_by("pk"))


def copy_back(apps, schema_editor):
    """Reversible for a one-marka bron. A bron with several markalar keeps its
    first one — the others have nowhere to go in the old shape."""
    Reservation = apps.get_model("crm", "Reservation")
    ReservationItem = apps.get_model("crm", "ReservationItem")
    for bron in Reservation.objects.all():
        item = ReservationItem.objects.filter(reservation_id=bron.pk).order_by("pk").first()
        if item is not None:
            bron.brand, bron.price, bron.price_uzs = item.brand, item.price, item.price_uzs
            bron.save(update_fields=["brand", "price", "price_uzs"])


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0079_customer_location'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReservationItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('brand', models.CharField(db_index=True, max_length=120, verbose_name='Marka')),
                ('price', models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True, verbose_name='1 kg narxi (USD)')),
                ('price_uzs', models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True, verbose_name="1 kg narxi (so'm)")),
                ('reservation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='crm.reservation', verbose_name='Bron')),
            ],
            options={
                'verbose_name': 'Bron markasi',
                'verbose_name_plural': 'Bron markalari',
                'ordering': ['pk'],
                'constraints': [models.UniqueConstraint(fields=('reservation', 'brand'), name='reservation_item_unique_brand')],
            },
        ),
        migrations.RunPython(copy_into_items, copy_back),
        migrations.RemoveField(
            model_name='reservation',
            name='brand',
        ),
        migrations.RemoveField(
            model_name='reservation',
            name='price',
        ),
        migrations.RemoveField(
            model_name='reservation',
            name='price_uzs',
        ),
    ]
