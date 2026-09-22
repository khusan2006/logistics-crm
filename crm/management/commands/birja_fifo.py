"""Put every birja yuk on the kelishuv the rule says it belongs on.

    python manage.py birja_fifo                    # what would move — nothing written
    python manage.py birja_fifo --apply --user X   # move it, logged under X

The rule (crm.birja): a truck's kg come off the oldest birja kelishuv that still
has the marka to send. Trucks booked before the rule existed were booked by hand,
so they are not all where it would put them; this is the one-off that fixes them.
Safe to run again — a second run finds nothing to move.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from crm.birja import apply_redistribution, kg_text, plan_redistribution
from crm.models import AuditLog


class Command(BaseCommand):
    help = ("Birja yuklarini eng eski kelishuvdan boshlab qayta taqsimlaydi. Sukut "
            "bo'yicha faqat rejani ko'rsatadi; --apply bilan yozadi.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Rejani bazaga yozish")
        parser.add_argument("--user", help="AuditLog'ga yoziladigan foydalanuvchi")

    def handle(self, *args, apply=False, user=None, **options):
        actor = None
        if user:
            actor = User.objects.filter(username=user).first()
            if actor is None:
                raise CommandError(f"Foydalanuvchi topilmadi: {user}")

        plan = plan_redistribution()
        self._report(plan)
        if plan.problems:
            raise CommandError("Reja bajarilmaydi: " + "; ".join(plan.problems))
        if not plan.moves:
            self.stdout.write("Hamma birja yuklari o'z joyida — ko'chiriladigan yuk yo'q.")
            return
        if not apply:
            self.stdout.write("Faqat reja, hech narsa yozilmadi. Yozish uchun: --apply")
            return

        with transaction.atomic():
            done = apply_redistribution(plan)
            for move, yuklar in done:
                target = " + ".join(f"{yuk.contract.code} · {kg_text(yuk.kg)} kg"
                                    for yuk in yuklar)
                AuditLog.record(
                    actor, AuditLog.Action.UPDATE, "Yuk", move.shipment.pk,
                    f"Birja FIFO: {move.brand} {move.now.code} → {target}"[:255])
        self.stdout.write(self.style.SUCCESS(f"{len(done)} ta yuk ko'chirildi."))

    def _report(self, plan):
        write = self.stdout.write
        write(f"Joyida: {plan.kept} ta yuk · ko'chadi: {len(plan.moves)} ta")
        for truck, why in plan.pinned:
            write(f"  tegilmaydi: yuk #{truck.pk} ({truck.contract.code}) — {why}")
        for move in plan.moves:
            day = move.shipment.sent or move.shipment.arrived
            day = day.strftime("%d.%m") if day else "sanasiz"
            target = " + ".join(f"{contract.code} ({kg_text(kg)} kg)"
                                for contract, kg in move.target)
            split = " — ikki yukka bo'linadi" if move.is_split else ""
            write(f"  yuk #{move.shipment.pk} · {day} · {move.brand} · "
                  f"{kg_text(move.kg)} kg: {move.now.code} → {target}{split}")
        if plan.totals:
            write("Kelishuvlar bo'yicha yuborilgan kg (hozir → keyin):")
            for code, (now, after) in plan.totals.items():
                write(f"  {code}: {kg_text(now)} → {kg_text(after)}")
        write(f"Ko'chadigan yuklardagi sotuv bo'laklari: {plan.sold_slices}"
              + (" — sotuvlarning kg'i o'zgarmaydi, faqat qaysi lotdan ekani"
                 if plan.sold_slices else ""))
        for problem in plan.problems:
            write(self.style.ERROR(f"  muammo: {problem}"))
