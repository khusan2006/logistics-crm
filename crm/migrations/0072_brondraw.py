"""Write down how many kg of each sotuv a bron actually took.

`Sale.reservation` names the promise a sotuv went against; it never said how much
of the sotuv the promise took, and the two differ whenever a bron had less room
left than the sotuv had kg. Giving the kg back therefore guessed, and the guess was
the sotuv's whole kg — so an edit or a delete could take kg off a bron that another
sotuv had put there, and the bron read as less served than the mijoz was given.

The backfill reconstructs the draws for the rows already on the books: each bron's
linked sotuvlar, oldest first, each taking what was left of that bron's CURRENT
`fulfilled_kg`. No `fulfilled_kg` is touched — a figure a sotuv spilled onto a
second bron is not something the link can prove, so the numbers stay exactly as the
operator sees them today and only the accounting under them is filled in. Where
they disagree with the sotuvlar, `bron_tekshir` reports it for a person to decide.
"""

from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


def backfill_draws(apps, schema_editor):
    Reservation = apps.get_model("crm", "Reservation")
    Sale = apps.get_model("crm", "Sale")
    BronDraw = apps.get_model("crm", "BronDraw")

    draws = []
    for bron in Reservation.objects.filter(fulfilled_kg__gt=0).iterator():
        budget = bron.fulfilled_kg
        sales = (Sale.objects.filter(reservation_id=bron.pk)
                 .order_by("created_at", "pk"))
        for sale in sales:
            if budget <= 0:
                break
            take = min(sale.kg, budget)
            draws.append(BronDraw(sale_id=sale.pk, reservation_id=bron.pk, kg=take))
            budget -= take
    BronDraw.objects.bulk_create(draws, batch_size=500)


def drop_draws(apps, schema_editor):
    apps.get_model("crm", "BronDraw").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0071_sale_position"),
    ]

    operations = [
        migrations.CreateModel(
            name="BronDraw",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("kg", models.DecimalField(decimal_places=3, max_digits=12,
                                           verbose_name="Hisoblangan kg")),
                ("reservation", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="draws", to="crm.reservation", verbose_name="Bron")),
                ("sale", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="bron_draws", to="crm.sale", verbose_name="Sotuv")),
            ],
            options={
                "verbose_name": "Bronga hisoblangan kg",
                "verbose_name_plural": "Bronga hisoblangan kg",
            },
        ),
        migrations.AddConstraint(
            model_name="brondraw",
            constraint=models.UniqueConstraint(fields=("sale", "reservation"),
                                               name="uniq_bron_draw_per_sale"),
        ),
        migrations.RunPython(backfill_draws, drop_draws),
    ]
