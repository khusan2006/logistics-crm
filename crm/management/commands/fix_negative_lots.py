"""Move sotuv kg off lots that went below zero — see `settle_negative_lots`.

A restocked vazvrat deleted after its kg were sold again leaves its lot below zero
and another lot of the marka holding kg nobody has. The marka adds up; the lots do
not, so those sotuvlar carry the wrong truck's tannarx and the sotuv form's Marka
list offers granula that is not on the shelf.

Only tannarx and foyda move; kg, narx and qarz stay. Reports first and changes
nothing; pass --apply to write.

    python manage.py fix_negative_lots                      # dry run
    python manage.py fix_negative_lots --apply --user Xusan
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.fifo import settle_negative_lots
from crm.models import AuditLog, Sale, arrived_lots


class _DryRun(Exception):
    pass


def _money(value):
    return f"{value:,.2f}".replace(",", " ")


class Command(BaseCommand):
    help = "Minusga tushgan lotlardagi sotuvlarni qoldig'i bor lotga ko'chirish (standart: faqat ko'rsatadi)"

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

        brands = sorted({lot.brand for lot in arrived_lots() if lot.available_kg < 0})
        if not brands:
            self.stdout.write("Minusdagi lot yo'q.")
            return
        try:
            with transaction.atomic():
                for brand in brands:
                    self.stdout.write(f"\n{brand}")
                    sales = Sale.objects.filter(line__contract_line__brand=brand)
                    before = {s.pk: (s.cost_price, s.profit) for s in sales}
                    moves = settle_negative_lots(brand)
                    for sale, src, dst, kg in moves:
                        fresh = Sale.objects.get(pk=sale.pk)
                        cost, profit = before[sale.pk]
                        line = (f"  sotuv #{sale.pk} ({sale.date}, {sale.customer.name}): "
                                f"{kg.normalize():f} kg yuk #{src.shipment_id} → "
                                f"yuk #{dst.shipment_id} · tan narx {cost:.4f} → "
                                f"{fresh.cost_price:.4f} $/kg · foyda "
                                f"{_money(profit)} → {_money(fresh.profit)} $")
                        self.stdout.write(line)
                        if apply:
                            AuditLog.record(
                                actor, AuditLog.Action.UPDATE, "Sotuv", sale.pk,
                                (f"Lot tuzatildi: {kg.normalize():f} kg yuk "
                                 f"#{src.shipment_id} → yuk #{dst.shipment_id} "
                                 f"(o'chirilgan vazvrat minusga tushirgan lot)")[:255])
                    left = [(lot.shipment_id, lot.available_kg)
                            for lot in arrived_lots().filter(contract_line__brand=brand)
                            if lot.available_kg < 0]
                    if left:
                        self.stdout.write(self.style.WARNING(f"  hali minusda: {left}"))
                if not apply:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(self.style.WARNING(
                "\nDry run — hech narsa yozilmadi. Yozish uchun: --apply --user <nom>"))
            return
        self.stdout.write(self.style.SUCCESS("\nLotlar tuzatildi."))
