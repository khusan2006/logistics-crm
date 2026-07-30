"""Repair pass for to'lov taqsimoti left mis-ordered by the old per-row allocation.

Two things are fixed, per mijoz:

1. **Money on a newer sotuv while an older one owes.** `apply_customer_advance` used
   to fill the sotuv being created straight from the avans, so a to'lov's leftover
   could skip an older unpaid sotuv and land on the newest one. Those slices are
   moved back to the oldest sotuv that still has a qoldiq.
2. **To'lov money left unlinked while a sotuv reads as short.** Editing a to'lov only
   ever re-spread that one to'lov, so another to'lov's remainder could stay unlinked
   right next to the sotuv it had paid. The mijoz is not owed anything and does not
   owe anything — the same sum just sits on both sides of the books, so the sotuv
   shows a qoldiq the mijoz has in fact already settled.
   `reconcile_customer_allocations` writes the link.

Reports first and changes nothing; pass --apply to write.

    python manage.py reconcile_allocations                # dry run
    python manage.py reconcile_allocations --apply
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from crm.models import (Customer, PaymentAllocation,
                        reconcile_customer_allocations)

ZERO = Decimal("0")


def _sales(customer):
    """This mijoz's sotuvlar oldest-first with allocations and qaytarishlar already
    loaded. Prefetched deliberately: the pass walks every row of every mijoz, and one
    query per sotuv against a remote database turns a one-off into minutes."""
    return list(customer.sales.prefetch_related("returns", "allocations")
                .order_by("date", "id"))


def _payments(customer):
    return list(customer.customer_payments.prefetch_related("allocations")
                .order_by("date", "id"))


def _allocated(row):
    """Σ of the prefetched allocations — no query, unlike `Sale.paid`."""
    return sum((a.amount for a in row.allocations.all()), ZERO)


def planned_moves(sales):
    """[(allocation, from_sale, to_sale, amount)] — slices sitting on a newer sotuv
    while an older one of the same mijoz still has a qoldiq, newest sotuv raided
    first. Read-only: amounts are tracked in local ledgers, so several moves can be
    planned in a row (and printed) without the rows having been touched."""
    owed = {s.pk: s.net_total - _allocated(s) for s in sales}
    # How much of each allocation is still available to move. Without this a $400
    # slice raided by two different targets would be planned away twice.
    movable = {a.pk: a.amount for s in sales for a in s.allocations.all()}
    moves = []
    for index, target in enumerate(sales):
        if owed[target.pk] <= 0:
            continue
        # Raid the newest sotuvlar first: money that landed furthest out of order is
        # the least likely to be a deliberate pick.
        for source in reversed(sales[index + 1:]):
            if owed[target.pk] <= 0:
                break
            for alloc in sorted(source.allocations.all(), key=lambda a: -a.pk):
                if owed[target.pk] <= 0:
                    break
                take = min(movable[alloc.pk], owed[target.pk])
                if take <= 0:
                    continue
                moves.append((alloc, source, target, take))
                movable[alloc.pk] -= take
                owed[target.pk] -= take
                owed[source.pk] += take
    return moves


def unlinked_payment_money(customer, sales, payments):
    """[(payment, amount)] — to'lov money not written against any sotuv while one of
    this mijoz's sotuvlar still reads as short. Money left over with nothing open to
    pay is a real avans, correctly unlinked, so it is not reported."""
    if not any(s.net_total - _allocated(s) > 0 for s in sales):
        return []
    return [(p, p.net_amount - _allocated(p))
            for p in payments if p.net_amount - _allocated(p) > 0]


def apply_moves(moves):
    """Re-home each slice: the to'lov keeps its money, the sotuv it sits on changes."""
    with transaction.atomic():
        for alloc, _source, target, amount in moves:
            alloc.refresh_from_db()
            if alloc.amount > amount:
                alloc.amount -= amount
                alloc.save(update_fields=["amount"])
            else:
                alloc.delete()
            PaymentAllocation.objects.create(
                payment_id=alloc.payment_id, sale=target, amount=amount)


class Command(BaseCommand):
    help = ("Move to'lov taqsimoti back onto the oldest unpaid sotuv and place any "
            "stranded avans. Dry run unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes (default is a dry run)")

    def handle(self, *args, **options):
        write = options["apply"]
        touched = 0
        for customer in Customer.objects.order_by("name"):
            sales = _sales(customer)
            if not sales:
                continue
            payments = _payments(customer)
            moves = planned_moves(sales)
            unlinked = unlinked_payment_money(customer, sales, payments)
            if not moves and not unlinked:
                continue

            touched += 1
            self.stdout.write(self.style.MIGRATE_HEADING(customer.name))
            for _alloc, source, target, amount in moves:
                self.stdout.write(
                    f"  ko'chiriladi ${amount} : sotuv #{source.pk} ({source.date}) "
                    f"-> sotuv #{target.pk} ({target.date})")
            for payment, amount in unlinked:
                self.stdout.write(
                    f"  bog'lanadi ${amount} : to'lov #{payment.pk} ({payment.date}) "
                    f"ning taqsimlanmagan qismi")
            if write:
                apply_moves(moves)
                reconcile_customer_allocations(customer)

        if not touched:
            self.stdout.write("Hammasi joyida — tuzatish kerak emas.")
            return
        self.stdout.write("")
        if write:
            self.stdout.write(self.style.SUCCESS(f"{touched} mijoz tuzatildi."))
        else:
            self.stdout.write(f"{touched} mijozda tuzatish kerak. Yozish uchun: --apply")
