"""Count every mijoz's sotuvlar into their brons again, in sana order.

Live, a bron is now recounted in sana order whenever one of its mijoz's sotuvlar is
added, corrected or deleted (`recount_brons`). The draws already in the books were
made in the order the sotuvlar were TYPED: Komoliddin's 16-oktabr sotuv, typed six
minutes before his 15-oktabr one, took the last 2 100 kg of bron-3 and left the
15th with 2 850. This pass redoes them all once, oldest sana first.

Nothing about a sotuv itself changes — narx, kg, qarz stay as they are. Only which
bron its kg count against moves, and how much of it is bronsiz.

Reports first and changes nothing; pass --apply to write.

    python manage.py recount_brons                      # dry run
    python manage.py recount_brons --apply --user Xusan
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import AuditLog, Reservation, ReservationDraw, Sale, recount_brons


class _DryRun(Exception):
    pass


def _draws():
    """{sale pk: ((bron code, kg), ...)} for every draw in the books."""
    out = {}
    for draw in (ReservationDraw.objects.select_related("reservation")
                 .order_by("sale_id", "reservation__date", "reservation_id")):
        out.setdefault(draw.sale_id, []).append((draw.reservation.code, draw.kg))
    return {pk: tuple(rows) for pk, rows in out.items()}


def _brons():
    return {r.pk: (r.code, r.fulfilled_kg, r.get_status_display())
            for r in Reservation.objects.order_by("pk")}


def _label(rows):
    return ", ".join(f"{code} {kg.normalize():f}" for code, kg in rows) or "bronsiz"


class Command(BaseCommand):
    help = "Sotuvlarni bronlarga sana tartibida qayta hisoblash (standart: faqat ko'rsatadi)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="O'zgarishlarni yozish (aks holda faqat ko'rsatiladi)")
        parser.add_argument("--user", help="AuditLog uchun foydalanuvchi nomi")

    def handle(self, *args, apply=False, user=None, **options):
        actor = None
        if apply:
            if not user:
                raise CommandError("--apply needs --user, for the AuditLog")
            actor = get_user_model().objects.filter(username=user).first()
            if actor is None:
                raise CommandError(f"No user {user!r}")

        customers = sorted(set(Reservation.objects.values_list("customer_id", flat=True)))
        try:
            with transaction.atomic():
                draws_before, brons_before = _draws(), _brons()
                for customer_id in customers:
                    recount_brons(customer_id)
                draws_after, brons_after = _draws(), _brons()
                moved = sorted(pk for pk in set(draws_before) | set(draws_after)
                               if draws_before.get(pk, ()) != draws_after.get(pk, ()))
                if not moved:
                    self.stdout.write("Hech narsa o'zgarmadi — bronlar sana tartibida.")
                    raise _DryRun
                sales = {s.pk: s for s in Sale.objects.filter(pk__in=moved)
                         .select_related("customer", "line")}
                for pk in sorted(moved, key=lambda pk: (sales[pk].customer.name,
                                                        sales[pk].date, pk)):
                    sale = sales[pk]
                    was, now = _label(draws_before.get(pk, ())), _label(draws_after.get(pk, ()))
                    self.stdout.write(
                        f"{sale.customer.name} · sotuv #{pk} ({sale.date}, "
                        f"{sale.kg.normalize():f} kg {sale.line.brand}): {was} → {now}")
                    if apply:
                        AuditLog.record(
                            actor, AuditLog.Action.UPDATE, "Sotuv", pk,
                            f"Bron sana tartibida qayta hisoblandi: sotuv #{pk} "
                            f"{sale.date} · {was} → {now}"[:255])
                self.stdout.write("")
                for pk, (code, given, status) in brons_after.items():
                    old = brons_before[pk]
                    if old != (code, given, status):
                        self.stdout.write(f"{code}: berilgan {old[1].normalize():f} → "
                                          f"{given.normalize():f}, {old[2]} → {status}")
                if not apply:
                    raise _DryRun
        except _DryRun:
            if not apply:
                self.stdout.write(self.style.WARNING(
                    "\nDry run — hech narsa yozilmadi. Yozish uchun: --apply --user <nom>"))
            return
        self.stdout.write(self.style.SUCCESS(f"\n{len(moved)} ta sotuvning broni qayta hisoblandi."))
