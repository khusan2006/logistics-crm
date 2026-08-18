"""Write the vazvrat line that a sotuv's own Tarix reads.

A sotuv's Tarix is the audit trail of its OWN pk. Vazvratlar entered before that was
recorded were logged against the visit alone, so the goods came back and the one page
that tells the story of that sotuv stayed silent about it.

This adds the missing line — nothing is invented: the kg, the money and the day all
come off the vazvrat row itself, and a line is only written where none exists, so the
migration can be re-run and says nothing twice. It is not reversible, because
removing an audit entry is not a thing an audit trail does.
"""
from decimal import Decimal

from django.db import migrations


def _kg(value):
    text = f"{Decimal(value or 0):,.3f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", " ")


def _money(amount, currency):
    text = f"{Decimal(amount or 0):,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    return f"{text} so'm" if currency == "uzs" else f"${text}"


def backfill(apps, schema_editor):
    AuditLog = apps.get_model("crm", "AuditLog")
    Return = apps.get_model("crm", "Return")

    known = set(AuditLog.objects
                .filter(action="return", target_type="Sotuv")
                .values_list("target_id", flat=True))

    for line in (Return.objects.select_related("sale__line__contract_line",
                                               "batch__created_by")
                 .order_by("pk")):
        if line.sale_id in known:
            continue
        is_som = line.currency == "uzs"
        to_debt = line.to_debt_uzs if is_som else line.to_debt
        value = (line.kg * (line.price_uzs if is_som else line.price))
        to_customer = value - to_debt
        note = (f"Vazvrat: {_kg(line.kg)} kg qaytdi "
                f"({line.sale.line.contract_line.brand})")
        if to_debt:
            note += f" — {_money(to_debt, line.currency)} qarzdan ayirildi"
        if to_customer > 0:
            note += f" — {_money(to_customer, line.currency)} mijozga qaytdi"
        row = AuditLog.objects.create(
            user=line.batch.created_by if line.batch_id else line.created_by,
            action="return", target_type="Sotuv", target_id=line.sale_id,
            summary=note[:255])
        # `created_at` is auto_now_add, so the real day is written back over it —
        # a trail that dates every old vazvrat to the day of a deploy is worse than
        # no trail at all.
        AuditLog.objects.filter(pk=row.pk).update(created_at=line.created_at)
        known.add(line.sale_id)


class Migration(migrations.Migration):

    dependencies = [("crm", "0056_alter_return_to_debt_alter_return_to_debt_uzs")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
