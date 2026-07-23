from django.db import migrations, models

from crm.models import partner_code_slug


def backfill_codes(apps, schema_editor):
    """Number the kelishuvlar that predate codes: oldest first inside each slug, so
    the sequence matches the order they were actually agreed. Each hamkor's counter
    then starts where their existing codes leave off."""
    Partner = apps.get_model("crm", "Partner")
    Contract = apps.get_model("crm", "Contract")

    for partner in Partner.objects.all():
        partner.code_slug = partner_code_slug(partner.name)
        partner.save(update_fields=["code_slug"])

    counters = {}
    for contract in Contract.objects.select_related("partner").order_by("created", "id"):
        slug = contract.partner.code_slug
        counters[slug] = counters.get(slug, 0) + 1
        contract.code_slug, contract.code_number = slug, counters[slug]
        contract.save(update_fields=["code_slug", "code_number"])

    for partner in Partner.objects.all():
        partner.code_counter = counters.get(partner.code_slug, 0)
        partner.save(update_fields=["code_counter"])


class Migration(migrations.Migration):

    dependencies = [("crm", "0018_alter_shipment_destination_alter_shipment_origin")]

    operations = [
        migrations.AddField(
            model_name="partner",
            name="code_slug",
            field=models.CharField(db_index=True, default="", editable=False, max_length=120),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="partner",
            name="code_counter",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="contract",
            name="code_slug",
            field=models.CharField(db_index=True, default="", editable=False, max_length=120),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="contract",
            name="code_number",
            field=models.PositiveIntegerField(default=0, editable=False),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_codes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="contract",
            constraint=models.UniqueConstraint(
                fields=("code_slug", "code_number"), name="unique_contract_code"),
        ),
    ]
