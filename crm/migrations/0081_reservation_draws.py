"""Record how many kg of each sotuv went into each bron.

Until now a sotuv named only the first bron it drew from, so a sotuv that spilled
into a second bron left that bron's kg unexplained. The rows are rebuilt from what
the books already say, in two passes:

1. Each bron's `fulfilled_kg` is shared out over the sotuvlar linked to it, oldest
   first, none taking more than it weighs.
2. What a bron still cannot explain came from a spill: it is taken from the same
   mijoz's sotuvlar of one of its markalar that were drawn from ANOTHER bron and
   have kg left over, entered after this bron existed — the only sotuvlar
   `draw_down_bron` could have spilled into it.

Every bron must come out explained to the gram, or the migration stops."""

from collections import defaultdict
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


def backfill_draws(Reservation, ReservationItem, ReservationDraw, Sale):
    """Build the draw rows from `fulfilled_kg` and `Sale.reservation`. Takes the
    model classes so the migration hands in historical ones and a test real ones."""
    brands = defaultdict(set)
    for bron_id, brand in ReservationItem.objects.values_list("reservation_id", "brand"):
        brands[bron_id].add(brand)
    sales = {s["pk"]: s for s in Sale.objects.filter(customer__reservations__isnull=False)
             .distinct().values("pk", "kg", "customer_id", "reservation_id", "created_at",
                                "line__contract_line__brand")}
    taken = defaultdict(Decimal)           # sale pk → kg already placed
    rows = []
    brons = list(Reservation.objects.order_by("pk"))
    short = {}

    def place(bron, sale, left):
        take = min(sale["kg"] - taken[sale["pk"]], left)
        if take > 0:
            taken[sale["pk"]] += take
            rows.append(ReservationDraw(reservation_id=bron.pk, sale_id=sale["pk"], kg=take))
        return left - max(take, Decimal("0"))

    order = lambda s: (s["created_at"], s["pk"])  # noqa: E731
    for bron in brons:
        left = bron.fulfilled_kg
        for sale in sorted((s for s in sales.values() if s["reservation_id"] == bron.pk),
                           key=order):
            if left <= 0:
                break
            left = place(bron, sale, left)
        short[bron.pk] = left

    for bron in brons:
        left = short[bron.pk]
        if left <= 0:
            continue
        spilled = sorted(
            (s for s in sales.values()
             if s["customer_id"] == bron.customer_id
             and s["reservation_id"] not in (None, bron.pk)
             and s["line__contract_line__brand"] in brands[bron.pk]
             and s["created_at"] >= bron.created_at
             and s["kg"] > taken[s["pk"]]),
            key=order)
        for sale in spilled:
            if left <= 0:
                break
            left = place(bron, sale, left)
        short[bron.pk] = left

    unexplained = {pk: left for pk, left in short.items() if left != 0}
    if unexplained:
        raise RuntimeError(
            "Bron fulfilled_kg could not be matched to sotuvlar "
            f"(bron pk → kg left unexplained): {unexplained}")
    ReservationDraw.objects.bulk_create(rows)


def forwards(apps, schema_editor):
    backfill_draws(apps.get_model("crm", "Reservation"),
                   apps.get_model("crm", "ReservationItem"),
                   apps.get_model("crm", "ReservationDraw"),
                   apps.get_model("crm", "Sale"))


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0080_reservation_items'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReservationDraw',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kg', models.DecimalField(decimal_places=3, max_digits=12, verbose_name='Bronga hisoblangan kg')),
                ('reservation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='draws', to='crm.reservation', verbose_name='Bron')),
                ('sale', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bron_draws', to='crm.sale', verbose_name='Sotuv')),
            ],
            options={
                'verbose_name': 'Brondan berilgan',
                'verbose_name_plural': 'Brondan berilganlar',
                'ordering': ['pk'],
                'constraints': [models.UniqueConstraint(fields=('reservation', 'sale'), name='reservation_draw_unique_sale')],
            },
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
