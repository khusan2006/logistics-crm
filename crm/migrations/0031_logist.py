"""Logist: the outside party who arranges transport and pays the drivers.

We send them money in a lump (LogistPayment — a real kassa outflow) and they hand
each driver an advance when a yuk goes out. That advance is a ShipmentExpense with
`logist` set: it still prices the granula, but it does NOT leave the kassa a second
time, because the cash already left when we topped the logist up.

Both foreign keys are nullable — loads have been moving without a logist since the
system started, and expenses have been paid straight out of the till.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("crm", "0030_reservation_by_brand"),
    ]

    operations = [
        migrations.CreateModel(
            name="Logist",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200, verbose_name="Ismi")),
                ("phone", models.CharField(blank=True, max_length=30, verbose_name="Telefon")),
                ("note", models.TextField(blank=True, verbose_name="Izoh")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name": "Logist", "verbose_name_plural": "Logistlar",
                     "ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="shipment",
            name="logist",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="shipments", to="crm.logist", verbose_name="Logist"),
        ),
        migrations.AddField(
            model_name="shipmentexpense",
            name="logist",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="driver_advances", to="crm.logist",
                verbose_name="Logist to'ladi"),
        ),
        migrations.CreateModel(
            name="LogistPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("currency", models.CharField(
                    choices=[("usd", "Dollar"), ("uzs", "So'm")], default="usd",
                    max_length=3, verbose_name="Valyuta")),
                ("exchange_rate", models.DecimalField(
                    decimal_places=2, default=12000, max_digits=12,
                    verbose_name="Dollar kursi (1$ = so'm)")),
                ("fee_percent", models.DecimalField(
                    blank=True, decimal_places=2, default=0, max_digits=5,
                    help_text="Faqat perechisleniya uchun; naqd va kartada e'tiborga olinmaydi",
                    verbose_name="Perechisleniya foizi (%)")),
                ("date", models.DateField(default=timezone.localdate, verbose_name="Sana")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14,
                                               verbose_name="Summa (USD)")),
                ("amount_uzs", models.DecimalField(decimal_places=2, default=0,
                                                   max_digits=18,
                                                   verbose_name="Summa (so'm)")),
                ("method", models.CharField(
                    choices=[("cash", "Naqd"), ("card", "Karta"),
                             ("transfer", "Bank o'tkazmasi")],
                    default="cash", max_length=8, verbose_name="To'lov usuli")),
                ("note", models.CharField(blank=True, max_length=255, verbose_name="Izoh")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="logist_payments", to=settings.AUTH_USER_MODEL,
                    verbose_name="Kim kiritdi")),
                ("logist", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name="payments",
                    to="crm.logist", verbose_name="Logist")),
            ],
            options={"verbose_name": "Logistga to'lov",
                     "verbose_name_plural": "Logistga to'lovlar",
                     "ordering": ["-date", "-created_at"]},
        ),
    ]
