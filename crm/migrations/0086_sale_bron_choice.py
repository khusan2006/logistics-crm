"""Keep the bron answers a sotuv was entered with, so `recount_brons` can redo them.

`draws_bron` — the Brondan ushlansin answer. It was never stored, so it is read
back from the draws: an entry with kg in any bron was ticked, and the whole entry
(one header, one answer) gets True. An entry with no draws gets False — it may have
been ticked and found the bron full, but it may as well have been unticked, and
pulling an unticked sotuv into a bron is the worse mistake.

`bron_pick` — a bron named for the sotuv. Only the hand-counted ones can be read
back: a draw into a bron struck after the sotuv (`bron_postdates`) was put there
by hand on Bronlar, or kept there through an edit, and must stay there.

Nothing is recounted here; the date-order pass is `manage.py recount_brons`."""

import django.db.models.deletion
from django.db import migrations, models


def backfill(apps, schema_editor):
    Sale = apps.get_model("crm", "Sale")
    ReservationDraw = apps.get_model("crm", "ReservationDraw")
    drawn = set(ReservationDraw.objects.values_list("sale_id", flat=True))
    drawn_groups = set(Sale.objects.filter(pk__in=drawn, group__isnull=False)
                       .values_list("group", flat=True))
    Sale.objects.exclude(pk__in=drawn).exclude(group__in=drawn_groups).update(
        draws_bron=False)
    for draw in ReservationDraw.objects.select_related("reservation", "sale"):
        bron, sale = draw.reservation, draw.sale
        # `bron_postdates`, inlined: a migration cannot call the live model's helper.
        postdates = ((sale.created_at and bron.created_at > sale.created_at)
                     or bron.date > sale.date)
        if postdates and sale.bron_pick_id is None:
            Sale.objects.filter(pk=sale.pk).update(bron_pick=bron)


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0085_reservation_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='sale',
            name='bron_pick',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='crm.reservation', verbose_name='Tanlangan bron'),
        ),
        migrations.AddField(
            model_name='sale',
            name='draws_bron',
            field=models.BooleanField(default=True, verbose_name='Brondan ushlansin'),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
