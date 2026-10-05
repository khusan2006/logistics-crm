"""Repair pass for sotuvlar counted into a bron struck AFTER their sana.

`draw_down_bron` used to ask only whether a bron existed when the sotuv was
ENTERED. A delivery typed in late is entered after every bron on the board, so it
went into the newest one: Komoliddin's 28-sentabr sotuv at 1.42, typed in eight
minutes after his 29-sentabr bron at 1.40 was struck, put 21 400 kg into it.

Every sotuv with such a draw is released from all its brons and drawn again under
today's rule (`bron_postdates`), oldest entry first — so each bron ends as it would
have if the rule had always been there. Nothing about the sotuv itself changes: its
narx, kg and qarz stay as they are. Only which bron it counts against moves.

Reports first and changes nothing; pass --apply to write.

    python manage.py rebron_by_date                      # dry run
    python manage.py rebron_by_date --apply --user Xusan
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import (AuditLog, Reservation, ReservationDraw, Sale, bron_postdates,
                        draw_down_bron, release_bron)


class _DryRun(Exception):
    pass


def _misdrawn_sales():
    """Sotuvlar with at least one draw into a bron struck after their sana, in the
    order they were entered — the order `draw_down_bron` saw them in live."""
    ids = {draw.sale_id for draw in ReservationDraw.objects.select_related(
        "reservation", "sale") if bron_postdates(draw.reservation, draw.sale)
        and draw.reservation.created_at <= draw.sale.created_at}
    return list(Sale.objects.filter(pk__in=ids).order_by("created_at", "pk"))


def _draws(sale):
    return [(d.reservation.code, d.kg) for d in sale.bron_draws.select_related("reservation")]


def _bron_state(pks):
    return {r.pk: (r.code, r.fulfilled_kg, r.remaining_kg, r.get_status_display())
            for r in Reservation.objects.filter(pk__in=pks).order_by("pk")}


class Command(BaseCommand):
    help = "Sotuvlarni bronlardan sana bo'yicha qayta hisoblash (standart: faqat ko'rsatadi)"

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

        sales = _misdrawn_sales()
        if not sales:
            self.stdout.write("Hech narsa topilmadi — barcha bronlar sana bo'yicha to'g'ri.")
            return
        brons = {d.reservation_id for s in sales for d in s.bron_draws.all()}
        try:
            with transaction.atomic():
                before = _bron_state(brons)
                for sale in sales:
                    was = _draws(sale)
                    release_bron(sale)
                    sale.reservation = None
                    sale.save(update_fields=["reservation"])
                    draw_down_bron(sale)
                    now = _draws(sale)
                    brons |= {d.reservation_id for d in sale.bron_draws.all()}
                    self.stdout.write(
                        f"sotuv #{sale.pk} ({sale.date}, {sale.kg} kg, {sale.price_own}): "
                        f"{was} → {now or 'bronsiz'}")
                    if apply:
                        AuditLog.record(
                            actor, AuditLog.Action.UPDATE, "Sotuv", sale.pk,
                            f"Bron tuzatildi (sana bo'yicha): sotuv #{sale.pk} "
                            f"{sale.date} · {was} → {now or 'bronsiz'}")
                after = _bron_state(brons)
                self.stdout.write("")
                for pk in sorted(after):
                    code, given, left, status = after[pk]
                    old = before.get(pk)
                    self.stdout.write(
                        f"{code}: berilgan {old[1] if old else '-'} → {given}, "
                        f"qolgan {old[2] if old else '-'} → {left}, {status}")
                if not apply:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(self.style.WARNING("\nDry run — hech narsa yozilmadi. "
                                                 "Yozish uchun: --apply --user <nom>"))
            return
        self.stdout.write(self.style.SUCCESS(f"\n{len(sales)} ta sotuv qayta hisoblandi."))
