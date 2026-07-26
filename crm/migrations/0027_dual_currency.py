"""Dual currency, sitewide.

Every money row gains the pair (dollar value, so'm value) plus the kurs that links
them, and the cash-movement rows gain a perechisleniya foiz. `amount_original` is
retired: with both values stored and `currency` recording which one was typed, the
old "whatever the operator entered" column carried strictly less information.

Ordering matters here. The auto-generated migration dropped `amount_original`
FIRST, which would have thrown away the exact so'm figures on existing to'lovlar
before anything could copy them into `amount_uzs`. The three RemoveField operations
therefore run last, after the back-fill below.

Rows entered before this migration have no so'm side at all — dollar entries stored
`exchange_rate = 0`. Those are back-filled at LEGACY_RATE (12,000 so'm/$, the figure
the operator supplied). Their so'm values are an assumption, not history: they say
what the money was worth at 12,000, not what the kurs was on the day.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations, models

LEGACY_RATE = Decimal("12000")
CENTS = Decimal("0.01")

#: (model, dollar field, so'm field) — the rows whose value is a lump sum
CASH_MODELS = [
    ("SupplierPayment", "amount", "amount_uzs"),
    ("CustomerPayment", "amount", "amount_uzs"),
    ("ShipmentExpense", "amount", "amount_uzs"),
]

#: the rows whose value is a per-kg price
PRICE_MODELS = [
    ("ContractLine", "price", "price_uzs"),
    ("ShipmentLine", "price", "price_uzs"),
    ("Sale", "price", "price_uzs"),
    ("Return", "price", "price_uzs"),
    ("Reservation", "price", "price_uzs"),
]


def backfill(apps, schema_editor):
    for name, usd_field, uzs_field in CASH_MODELS:
        model = apps.get_model("crm", name)
        rows = []
        for row in model.objects.all():
            rate = row.exchange_rate or Decimal("0")
            if rate <= 0:
                rate = LEGACY_RATE
                row.exchange_rate = rate
            if row.currency == "uzs":
                # The typed so'm figure survived in amount_original — keep it exact
                # rather than recomputing it from the rounded dollar value.
                row.amount_uzs = (row.amount_original or Decimal("0")).quantize(CENTS)
            else:
                row.amount_uzs = (row.amount * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
            rows.append(row)
        if rows:
            model.objects.bulk_update(rows, ["exchange_rate", uzs_field])

    # Every pre-existing price was a dollar price; there was nowhere to record
    # anything else. exchange_rate arrives already defaulted to LEGACY_RATE.
    for name, usd_field, uzs_field in PRICE_MODELS:
        model = apps.get_model("crm", name)
        rows = []
        for row in model.objects.all():
            price = getattr(row, usd_field)
            if price is None:  # nullable on ShipmentLine / Reservation
                setattr(row, uzs_field, None)
            else:
                setattr(row, uzs_field,
                        (price * LEGACY_RATE).quantize(CENTS, rounding=ROUND_HALF_UP))
            rows.append(row)
        if rows:
            model.objects.bulk_update(rows, [uzs_field])


def unbackfill(apps, schema_editor):
    """Rebuild amount_original so the reverse migration is lossless: it held the
    typed figure, which is the so'm value on a so'm row and the dollar value
    otherwise."""
    for name, usd_field, uzs_field in CASH_MODELS:
        model = apps.get_model("crm", name)
        rows = []
        for row in model.objects.all():
            row.amount_original = (row.amount_uzs if row.currency == "uzs" else row.amount)
            rows.append(row)
        if rows:
            model.objects.bulk_update(rows, ["amount_original"])


def currency_field():
    return models.CharField("Valyuta", max_length=3, default="usd",
                            choices=[("usd", "Dollar"), ("uzs", "So'm")])


def rate_field():
    return models.DecimalField("Dollar kursi (1$ = so'm)", max_digits=12,
                               decimal_places=2, default=LEGACY_RATE)


def fee_field():
    return models.DecimalField(
        "Perechisleniya foizi (%)", max_digits=5, decimal_places=2, default=0, blank=True,
        help_text="Faqat perechisleniya uchun; naqd va kartada e'tiborga olinmaydi")


def uzs_amount_field(label):
    return models.DecimalField(label, max_digits=18, decimal_places=2, default=0)


def uzs_price_field(label, nullable=False):
    if nullable:
        return models.DecimalField(label, max_digits=18, decimal_places=2,
                                   null=True, blank=True)
    return models.DecimalField(label, max_digits=18, decimal_places=2, default=0)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0026_remove_sale_cost_price"),
    ]

    operations = [
        # 1. the price models, which had no currency concept at all
        migrations.AddField("contractline", "currency", currency_field()),
        migrations.AddField("contractline", "exchange_rate", rate_field()),
        migrations.AddField("contractline", "price_uzs",
                            uzs_price_field("1 kg narxi (so'm)")),

        migrations.AddField("shipmentline", "currency", currency_field()),
        migrations.AddField("shipmentline", "exchange_rate", rate_field()),
        migrations.AddField("shipmentline", "price_uzs",
                            uzs_price_field("1 kg narxi (so'm)", nullable=True)),

        migrations.AddField("sale", "currency", currency_field()),
        migrations.AddField("sale", "exchange_rate", rate_field()),
        migrations.AddField("sale", "price_uzs",
                            uzs_price_field("1 kg sotuv narxi (so'm)")),

        migrations.AddField("return", "currency", currency_field()),
        migrations.AddField("return", "exchange_rate", rate_field()),
        migrations.AddField("return", "price_uzs",
                            uzs_price_field("1 kg narxi (so'm)")),

        migrations.AddField("reservation", "currency", currency_field()),
        migrations.AddField("reservation", "exchange_rate", rate_field()),
        migrations.AddField("reservation", "price_uzs",
                            uzs_price_field("1 kg narxi (so'm)", nullable=True)),

        # 2. the cash models: the so'm twin and the bank foiz
        migrations.AddField("supplierpayment", "amount_uzs",
                            uzs_amount_field("Summa (so'm)")),
        migrations.AddField("supplierpayment", "fee_percent", fee_field()),
        migrations.AlterField("supplierpayment", "exchange_rate", rate_field()),

        migrations.AddField("customerpayment", "amount_uzs",
                            uzs_amount_field("Summa (so'm)")),
        migrations.AddField("customerpayment", "fee_percent", fee_field()),
        migrations.AlterField("customerpayment", "exchange_rate", rate_field()),

        migrations.AddField("shipmentexpense", "amount_uzs",
                            uzs_amount_field("Summa (so'm)")),
        migrations.AddField("shipmentexpense", "fee_percent", fee_field()),
        migrations.AlterField("shipmentexpense", "exchange_rate", rate_field()),

        # 3. fill the new columns while amount_original is still readable
        migrations.RunPython(backfill, unbackfill),

        # 4. only now is the old column safe to drop
        migrations.RemoveField("customerpayment", "amount_original"),
        migrations.RemoveField("shipmentexpense", "amount_original"),
        migrations.RemoveField("supplierpayment", "amount_original"),
    ]
