"""Band the sotuvlar that were already entered together before `group` existed.

The field only starts stamping rows created after it, so every sotuv already in the
database reads on Sotuvlar as a lone row — including the ones that were one trip to
the counter split FIFO across lots, which is most of what is in there.

Nothing recorded the submission, but the write does: the rows of one sotuv are
created inside a single transaction, milliseconds apart, carrying the same mijoz,
sana, valyuta, kurs and operator. Two sotuvlar typed one after the other cannot be
that close — somebody has to reopen the modal and fill it in. In the data this was
written against the widest gap inside a batch was 23 ms and the tightest gap between
two separate sotuvlar was 25.7 s, so the two-second window below sits three orders of
magnitude clear of both edges.

Rows are walked in write order and a run ends the moment any of those fields differ,
so the failure mode is a batch left ungrouped — the same flat rows as today — rather
than two unrelated sotuvlar merged into one.

The reverse is a no-op: which groups came from here is exactly what is not recorded,
and clearing every group on the way back would also strip the ones the sale form has
stamped since.
"""
from uuid import uuid4

from django.db import migrations

# How far apart two rows may be written and still be the same submission.
WINDOW_SECONDS = 2


def _same_submission(previous, sale):
    return (previous.customer_id == sale.customer_id
            and previous.date == sale.date
            and previous.currency == sale.currency
            and previous.exchange_rate == sale.exchange_rate
            and previous.created_by_id == sale.created_by_id
            and (sale.created_at - previous.created_at).total_seconds() <= WINDOW_SECONDS)


def backfill(apps, schema_editor):
    Sale = apps.get_model("crm", "Sale")
    run = []

    def close(run):
        if len(run) > 1:
            Sale.objects.filter(pk__in=[s.pk for s in run]).update(group=uuid4())

    for sale in Sale.objects.filter(group__isnull=True).order_by("created_at", "pk"):
        if run and _same_submission(run[-1], sale):
            run.append(sale)
            continue
        close(run)
        run = [sale]
    close(run)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0041_sale_group"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
