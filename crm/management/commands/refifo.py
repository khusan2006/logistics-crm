"""Re-run FIFO for one marka, optionally correcting a sotuv's kg first.

    python manage.py refifo "2102 campaund" --set 2=24000
    python manage.py refifo "2102 campaund" --set 2=24000 --apply

Dry run unless `--apply` is passed: it prints what would move and rolls back.

This is the deliberate version of the shift the sotuv form offers. That form refuses
to move sotuvlar that are not already on FIFO order, because it cannot tell a lot the
operator CHOSE from one that drifted — and overwriting a real choice is worse than
leaving a stale cost. Run from here, that judgement is the operator's: they have
looked at the marka and decided the chain is FIFO's to rebuild.

Only tannarx and foyda ride on the answer, so a shift moves no qarz. Changing a kg
with `--set` is the opposite — that is somebody's debt, and it is reported in full
before anything is written.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.fifo import replay, apply_plan
from crm.models import AuditLog, Customer, Sale, arrived_lots


class Rollback(Exception):
    """Unwinds a dry run. Never escapes `handle`."""


def _sale_totals(brand):
    rows = Sale.objects.filter(line__contract_line__brand=brand)
    return (sum((s.total for s in rows), Decimal("0")),
            sum((s.profit for s in rows), Decimal("0")))


class Command(BaseCommand):
    help = ("Bitta markaning lotlarini FIFO bo'yicha qayta hisoblaydi. "
            "--apply berilmasa faqat ko'rsatadi.")

    def add_arguments(self, parser):
        parser.add_argument("brand")
        parser.add_argument("--set", action="append", default=[], metavar="ID=KG",
                            help="Avval shu sotuvning kg sini o'zgartirish")
        parser.add_argument("--apply", action="store_true",
                            help="Haqiqatda saqlash (aks holda faqat ko'rsatiladi)")

    def handle(self, *args, **options):
        brand = options["brand"]
        if not arrived_lots().filter(contract_line__brand=brand).exists():
            raise CommandError(f"“{brand}” markasi omborda yo'q")

        edits = {}
        for pair in options["set"]:
            try:
                pk, kg = pair.split("=", 1)
                edits[int(pk)] = Decimal(kg)
            except (ValueError, ArithmeticError):
                raise CommandError(f"--set noto'g'ri: {pair} (kutilgan: ID=KG)")

        before_plan = replay(brand)
        cost0 = {s.pk: s.cost_price for s in before_plan.sales}
        rev0, profit0 = _sale_totals(brand)
        debtors = {}

        try:
            with transaction.atomic():
                for pk, kg in edits.items():
                    sale = Sale.objects.select_related("customer").get(pk=pk)
                    was = sale.kg
                    debtors.setdefault(sale.customer_id,
                                       [sale.customer, sale.customer.balance])
                    Sale.objects.filter(pk=pk).update(kg=kg)
                    Sale.objects.get(pk=pk).sync_lot()
                    AuditLog.record(
                        None, AuditLog.Action.UPDATE, "Sotuv", pk,
                        f"Sotuv tahrirlandi: {was} kg → {kg} kg · "
                        f"{sale.customer.name} (tuzatish)")
                    self.stdout.write(
                        f"  #{pk}: {was:,.0f} kg → {kg:,.0f} kg · {sale.customer.name}")

                plan = replay(brand)
                if plan.short:
                    for sale, missing in plan.short:
                        self.stdout.write(self.style.ERROR(
                            f"  ! sotuv #{sale.pk}: {missing:,.0f} kg omborda yo'q"))
                    raise CommandError("Yetishmayotgan kg bor — hech narsa saqlanmadi")

                names = {lot.pk: lot.label for lot in
                         arrived_lots().filter(contract_line__brand=brand)}
                moved_from = {s.pk: [names.get(sl.line_id, f"lot #{sl.line_id}")
                                     for sl in s.lots.all()] for s in plan.sales}
                changed = apply_plan(plan)

                self.stdout.write("")
                self.stdout.write(f"Loti o'zgargan sotuvlar: {len(changed)}")
                by_pk = {s.pk: s for s in plan.sales}
                for pk in changed:
                    sale = by_pk[pk]
                    to = " + ".join(names.get(lot.pk, f"lot #{lot.pk}")
                                    for lot, _ in plan.placements[pk])
                    frm = " + ".join(moved_from[pk])
                    self.stdout.write(f"  #{pk:<4} {sale.date:%d.%m.%y} "
                                      f"{sale.customer.name[:20]:<20} {frm} → {to}")
                    AuditLog.record(None, AuditLog.Action.UPDATE, "Sotuv", pk,
                                    f"Lot qayta hisoblandi (FIFO): {frm} → {to}"[:255])

                rev1, profit1 = _sale_totals(brand)
                self.stdout.write("")
                self.stdout.write(f"Marka tushumi : {rev0:>14,.2f} → {rev1:>14,.2f} "
                                  f"({rev1 - rev0:+,.2f})")
                self.stdout.write(f"Marka foydasi : {profit0:>14,.2f} → {profit1:>14,.2f} "
                                  f"({profit1 - profit0:+,.2f})")
                for customer, was in debtors.values():
                    now = Customer.objects.get(pk=customer.pk).balance
                    self.stdout.write(f"{customer.name} qarzi: {was:>14,.2f} → "
                                      f"{now:>14,.2f} ({now - was:+,.2f})")

                left = replay(brand)
                self.stdout.write("")
                self.stdout.write(f"Qolgan nomuvofiqlik: {len(left.diverged)}")
                if not options["apply"]:
                    raise Rollback
        except Rollback:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "SINOV — hech narsa saqlanmadi. Saqlash uchun --apply qo'shing."))
            return
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Saqlandi."))
