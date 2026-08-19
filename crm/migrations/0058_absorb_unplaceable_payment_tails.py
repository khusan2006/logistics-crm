"""Clear the to'lov tails that no sweep could ever place.

Spending a so'm to'lov against a dollar qarz rounds the trip twice, and where it came
back SHORT the difference stayed behind: 39.20 so'm on one mijoz's card, printed as an
avans beside the $3 088.99 they really owed. It is worth a third of a cent, so
converted into the sotuv's money it is 0.00 and every later sweep passes over it.

`_absorb_tail` stops new ones being made. This puts the existing ones where they were
always heading — onto the last slice of their own to'lov, in that to'lov's own money
alone, so the sotuv's side is untouched and nobody is credited a tiyin they were not
owed. Only tails worth nothing in the sotuv's currency are moved; a remainder with any
value left in it is a real avans and is left exactly where it sits.

Not reversible: the tail carries no record of having been separate, and putting it
back would only restore a figure that was wrong.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations

CENT = Decimal("0.01")


def _settled_pair(payment):
    """(usd, so'm) the mijoz was credited — the pool a slice is drawn from.

    Mirrors `CustomerPayment.settled_amount`/`settled_amount_uzs` rather than calling
    them, because a migration reads the historical model, which carries the columns
    but none of the properties."""
    fee = Decimal("0")
    if payment.method == "transfer" and payment.fee_percent:
        fee = (payment.amount * payment.fee_percent / 100).quantize(
            CENT, rounding=ROUND_HALF_UP)
    # CustomerPayment.default_fee_bearer is COUNTERPARTY: money coming in has always
    # been the sender's loss unless the row says otherwise.
    if (payment.fee_bearer or "counterparty") == "company":
        return payment.amount, payment.amount_uzs
    net = payment.amount - fee
    if not payment.amount:
        return net, Decimal("0")
    return net, (payment.amount_uzs * net / payment.amount).quantize(
        CENT, rounding=ROUND_HALF_UP)


def absorb(apps, schema_editor):
    CustomerPayment = apps.get_model("crm", "CustomerPayment")
    rows = CustomerPayment.objects.prefetch_related(
        "allocations__sale", "refund_allocations")

    for payment in rows:
        last = payment.allocations.order_by("pk").last()
        if last is None or not payment.exchange_rate:
            continue
        # A slice landing in the money it arrived in never crosses a kurs, so a
        # remainder there is a real avans rather than the wreckage of a conversion.
        if last.sale.currency == payment.currency:
            continue

        settled, settled_uzs = _settled_pair(payment)
        spent = spent_uzs = Decimal("0")
        for slice_ in list(payment.allocations.all()) + list(payment.refund_allocations.all()):
            spent += slice_.amount
            spent_uzs += slice_.amount_uzs

        som = payment.currency == "uzs"
        tail = (settled_uzs - spent_uzs) if som else (settled - spent)
        if tail <= 0:
            continue

        worth = (tail / payment.exchange_rate) if som else (tail * payment.exchange_rate)
        if worth.quantize(CENT, rounding=ROUND_HALF_UP) > 0:
            continue    # still buys something on the sotuv's side: a real avans

        if som:
            last.amount_uzs += tail
            last.save(update_fields=["amount_uzs"])
        else:
            last.amount += tail
            last.save(update_fields=["amount"])


class Migration(migrations.Migration):

    dependencies = [("crm", "0057_backfill_return_audit_on_sales")]

    operations = [migrations.RunPython(absorb, migrations.RunPython.noop)]
