import django.db.models.deletion
from django.db import migrations, models


def split_into_lines(apps, schema_editor):
    """Every existing kelishuv and yuk carried exactly one product, so each becomes
    a single line holding the brand/kg/price it already had. Sotuvlar and bronlar
    then move from the truck to that truck's only lot — an unambiguous mapping."""
    Contract = apps.get_model("crm", "Contract")
    ContractLine = apps.get_model("crm", "ContractLine")
    Shipment = apps.get_model("crm", "Shipment")
    ShipmentLine = apps.get_model("crm", "ShipmentLine")
    Sale = apps.get_model("crm", "Sale")
    Reservation = apps.get_model("crm", "Reservation")

    line_of_contract = {}
    for contract in Contract.objects.all():
        line = ContractLine.objects.create(
            contract=contract, brand=contract.brand, kg=contract.kg,
            price=contract.price, position=0)
        line_of_contract[contract.pk] = line

    line_of_shipment = {}
    for shipment in Shipment.objects.all():
        line = ShipmentLine.objects.create(
            shipment=shipment, contract_line=line_of_contract[shipment.contract_id],
            kg=shipment.kg, price=shipment.price, position=0)
        line_of_shipment[shipment.pk] = line

    for model in (Sale, Reservation):
        for row in model.objects.all():
            model.objects.filter(pk=row.pk).update(
                line=line_of_shipment[row.shipment_id])


def merge_lines_back(apps, schema_editor):
    """Reverse: only single-product rows can go back into the old shape."""
    Contract = apps.get_model("crm", "Contract")
    Shipment = apps.get_model("crm", "Shipment")
    Sale = apps.get_model("crm", "Sale")
    Reservation = apps.get_model("crm", "Reservation")

    for contract in Contract.objects.prefetch_related("lines"):
        line = contract.lines.first()
        if line is None:
            continue
        Contract.objects.filter(pk=contract.pk).update(
            brand=line.brand, kg=line.kg, price=line.price)

    for shipment in Shipment.objects.prefetch_related("lines"):
        line = shipment.lines.first()
        if line is None:
            continue
        Shipment.objects.filter(pk=shipment.pk).update(kg=line.kg, price=line.price)

    for model in (Sale, Reservation):
        for row in model.objects.select_related("line"):
            model.objects.filter(pk=row.pk).update(shipment_id=row.line.shipment_id)


class Migration(migrations.Migration):

    dependencies = [("crm", "0019_contract_code")]

    operations = [
        migrations.CreateModel(
            name="ContractLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("brand", models.CharField(max_length=100, verbose_name="Granula markasi")),
                ("kg", models.DecimalField(decimal_places=3, max_digits=12,
                                           verbose_name="Kelishilgan kg")),
                ("price", models.DecimalField(decimal_places=4, max_digits=14,
                                              verbose_name="1 kg narxi (USD)")),
                ("position", models.PositiveIntegerField(default=0, editable=False)),
                ("contract", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name="lines", to="crm.contract",
                                               verbose_name="Kelishuv")),
            ],
            options={"verbose_name": "Kelishuv mahsuloti",
                     "verbose_name_plural": "Kelishuv mahsulotlari",
                     "ordering": ["position", "id"]},
        ),
        migrations.CreateModel(
            name="ShipmentLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("kg", models.DecimalField(decimal_places=3, max_digits=12,
                                           verbose_name="Yuborilgan kg")),
                ("price", models.DecimalField(
                    blank=True, decimal_places=4,
                    help_text="Bo'sh qoldirilsa kelishuv narxi olinadi", max_digits=14,
                    null=True, verbose_name="1 kg narxi (USD)")),
                ("position", models.PositiveIntegerField(default=0, editable=False)),
                ("contract_line", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="shipment_lines", to="crm.contractline",
                    verbose_name="Mahsulot")),
                ("shipment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name="lines", to="crm.shipment",
                                               verbose_name="Yuk")),
            ],
            options={"verbose_name": "Yuk mahsuloti",
                     "verbose_name_plural": "Yuk mahsulotlari",
                     "ordering": ["position", "id"]},
        ),
        # Nullable first so the rows can be filled in, then locked down below.
        migrations.AddField(
            model_name="sale",
            name="line",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="sales", to="crm.shipmentline",
                                    verbose_name="Lot (mahsulot)"),
        ),
        migrations.AddField(
            model_name="reservation",
            name="line",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="reservations", to="crm.shipmentline",
                                    verbose_name="Lot (mahsulot)"),
        ),
        migrations.RunPython(split_into_lines, merge_lines_back),
        migrations.AlterField(
            model_name="sale",
            name="line",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    related_name="sales", to="crm.shipmentline",
                                    verbose_name="Lot (mahsulot)"),
        ),
        migrations.AlterField(
            model_name="reservation",
            name="line",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    related_name="reservations", to="crm.shipmentline",
                                    verbose_name="Lot (mahsulot)"),
        ),
        migrations.RemoveField(model_name="sale", name="shipment"),
        migrations.RemoveField(model_name="reservation", name="shipment"),
        migrations.RemoveField(model_name="contract", name="brand"),
        migrations.RemoveField(model_name="contract", name="kg"),
        migrations.RemoveField(model_name="contract", name="price"),
        migrations.RemoveField(model_name="shipment", name="kg"),
        migrations.RemoveField(model_name="shipment", name="price"),
    ]
