"""Link the yuklar of one birja truck.

A birja truck bigger than what its kelishuv has left takes the rest off the next
kelishuv, and a yuk belongs to one kelishuv — so that truck is several yuklar. Until
now nothing joined them but a line in the izoh; `Shipment.truck` is the link, every
part after the first pointing at the yuk the truck started on.

The trucks `manage.py birja_fifo` has already split are linked here. It gave every
part the same "Bitta mashina: …" izoh and its truck's exact `created_at` (see
`crm.birja._split_off`), so a group of birja yuklar sharing both is one truck; the
lowest id is the one it started on.
"""
import django.db.models.deletion
from django.db import migrations, models


def link_split_trucks(apps, schema_editor):
    Shipment = apps.get_model("crm", "Shipment")
    groups = {}
    for yuk in (Shipment.objects.filter(contract__partner__is_birja=True,
                                        note__contains="Bitta mashina:")
                .order_by("pk")):
        groups.setdefault(yuk.created_at, []).append(yuk)
    for parts in groups.values():
        if len(parts) > 1:
            Shipment.objects.filter(pk__in=[yuk.pk for yuk in parts[1:]]).update(
                truck_id=parts[0].pk)


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0076_drop_birja_yuklandi_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='truck',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='truck_parts', to='crm.shipment', verbose_name='Bitta mashina'),
        ),
        migrations.RunPython(link_split_trucks, migrations.RunPython.noop),
    ]
