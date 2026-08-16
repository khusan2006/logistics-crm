"""Place every to'lov that was entered before a to'lov could be placed.

Until now a hamkor to'lov pointed at one product and stopped there, so a kelishuv
covering two markalar showed its whole paid figure against one of them — or, where
the to'lov named nothing at all, against neither. This runs the same rule the app
now applies as money arrives (`allocate_supplier_payment`): a named marka first and
the rest forward through the kelishuv, a zaklad split by mashina count, and whatever
no product can take left as the hamkor's avans.

Deliberately imports the live engine rather than reimplementing it against the
historical models. A backfill that spreads money by its own private copy of the rule
is a second answer to the same question, and the day the two disagree the ledger has
no way to say which one it is holding. The engine reads only fields this migration
already depends on.

Reverse simply drops the slices: they are derived, so nothing is lost that the
forward run cannot work out again.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    from crm.models import Contract, reconcile_supplier_allocations

    for contract in Contract.objects.all():
        reconcile_supplier_allocations(contract)


def drop(apps, schema_editor):
    apps.get_model("crm", "SupplierPaymentAllocation").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [("crm", "0049_supplierpaymentallocation")]

    operations = [migrations.RunPython(backfill, drop)]
