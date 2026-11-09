"""Every Eron kelishuv struck before the SIP Toshkent / Logist choice existed was
hauled through a logist, so that is what they all become. Its logist is filled in
only where the answer is not a guess: every yuk under it that names a logist names
the same one. The rest stay blank, and the form asks for one the next time the
kelishuv is edited.

Birja and mahalliy kelishuvlar are left blank — neither is hauled from Eron."""

from django.db import migrations


def backfill(apps, schema_editor):
    Contract = apps.get_model("crm", "Contract")
    Shipment = apps.get_model("crm", "Shipment")
    eron = Contract.objects.filter(partner__is_birja=False, partner__is_local=False)
    for contract in eron.iterator():
        logists = set(Shipment.objects.filter(contract=contract, logist__isnull=False)
                      .values_list("logist_id", flat=True))
        contract.delivery = "logist"
        contract.logist_id = logists.pop() if len(logists) == 1 else None
        contract.save(update_fields=["delivery", "logist"])


def clear(apps, schema_editor):
    Contract = apps.get_model("crm", "Contract")
    Contract.objects.update(delivery="", logist=None)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0087_contract_delivery_logist"),
    ]

    operations = [
        migrations.RunPython(backfill, clear),
    ]
