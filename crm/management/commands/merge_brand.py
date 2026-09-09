"""Join two names for one marka — the pair that reads the same on screen but is two
different products to the database.

    python manage.py merge_brand "и 1561" --into "i 1561" --settings=config.settings_dev
    python manage.py merge_brand "и 1561" --into "i 1561" --apply --settings=config.settings_dev

Reports and writes nothing by default; `--apply` does the work in one transaction.

Why it is needed: a marka is a free-typed string, held on `ContractLine.brand` and
`Reservation.brand`, and every join the app makes on it — a bron to a sotuv, a truck
to an ombor shelf, a queue position — is an exact string match. A latin `i` and a
cyrillic `и` are one keystroke apart and the screen transliterates lotin to kiril, so
the two spellings are indistinguishable to the reader and unequal to Postgres. The
halves of one product then never meet: on 2026-09-09 a 150 000 kg bron stood at its
full kg while its own holder bought 36 000 kg of the same granula over the counter,
because the bron was booked against `и 1561` and every sotuv came off `i 1561`.

Renaming alone does not finish it. A bron is drawn down at the moment a sotuv is
SAVED (`draw_down_bron`), so sales already in the books left it alone and will never
revisit it. So the command replays them, oldest first, which is what would have
happened had the two names been one from the start. `draw_down_bron` refuses a bron
younger than the sotuv, so a promise made after a sale is never eaten by it.

What it does NOT do: decide which spelling is right. That is the operator's call —
name the one to retire and the one to keep, and check the report before `--apply`.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import (
    AuditLog,
    ContractLine,
    Reservation,
    Sale,
    brand_on_hand_kg,
    brand_reserved_kg,
    draw_down_bron,
)


def _codepoints(text):
    """The name spelled out, so a report can SHOW the difference the screen hides."""
    return " ".join(f"U+{ord(ch):04X}" for ch in text if not ch.isspace())


def _shape(brand):
    """What one marka currently amounts to — the figures worth reading twice before
    two of them are joined."""
    lines = ContractLine.objects.filter(brand=brand).select_related("contract")
    sales = Sale.objects.filter(line__contract_line__brand=brand)
    return {
        "lines": list(lines),
        "sold_kg": sum((s.kg for s in sales), Decimal("0")),
        "sales": sales.count(),
        "on_hand": brand_on_hand_kg(brand),
        "reserved": brand_reserved_kg(brand),
        "brons": list(Reservation.objects.filter(brand=brand).select_related("customer")),
    }


class Command(BaseCommand):
    help = "Rename one marka into another and replay the bronlar the split hid."

    def add_arguments(self, parser):
        parser.add_argument("source", help="the marka name to retire")
        parser.add_argument("--into", required=True, help="the marka name to keep")
        parser.add_argument(
            "--apply", action="store_true",
            help="write the change; without it nothing is saved")

    def handle(self, *args, **options):
        source, into, apply_it = options["source"], options["into"], options["apply"]
        if source == into:
            raise CommandError("source va --into bir xil — birlashtiradigan narsa yo'q")

        line_count = ContractLine.objects.filter(brand=source).count()
        bron_count = Reservation.objects.filter(brand=source).count()
        if not line_count and not bron_count:
            raise CommandError(f"{source!r} nomli marka topilmadi")
        if not ContractLine.objects.filter(brand=into).exists():
            raise CommandError(f"{into!r} nomli marka topilmadi — --into ni tekshiring")

        self.stdout.write("BIRLASHTIRISH")
        for label, name in (("ketadi ", source), ("qoladi ", into)):
            self.stdout.write(f"  {label} {name!r}   {_codepoints(name)}")
        self.stdout.write("")
        for label, name in (("OLDIN — " + source, source), ("OLDIN — " + into, into)):
            self._report(label, _shape(name))

        if not apply_it:
            self.stdout.write(self.style.WARNING(
                f"\nQURUQ SINOV — hech narsa yozilmadi. {line_count} ta kelishuv satri "
                f"va {bron_count} ta bron ko'chirilardi.\n"
                "Rostdan bajarish uchun --apply qo'shing."))
            return

        with transaction.atomic():
            ContractLine.objects.filter(brand=source).update(brand=into)
            Reservation.objects.filter(brand=source).update(brand=into)
            AuditLog.record(
                None, AuditLog.Action.UPDATE, "Marka", None,
                f"Marka birlashtirildi: {source} → {into} · "
                f"{line_count} ta kelishuv satri, {bron_count} ta bron")
            moved = self._redraw_brons(into)

        self.stdout.write("")
        self._report("KEYIN — " + into, _shape(into))
        self.stdout.write("")
        if moved:
            self.stdout.write("BRONLAR QAYTA HISOBLANDI")
            for bron, before, after in moved:
                self.stdout.write(
                    f"  {bron.customer.name}: qolgan {before:,.0f} → {after:,.0f} kg"
                    .replace(",", " "))
        else:
            self.stdout.write("Qayta hisoblanadigan bron topilmadi.")
        self.stdout.write(self.style.SUCCESS("\nBajarildi."))

    def _redraw_brons(self, brand):
        """Replay the sotuvlar of `brand` against the bronlar, oldest first.

        Only sales not already tied to a bron are offered — one that came from a bron
        has been booked once and must not be counted twice. Returns the bronlar that
        actually moved, with their remaining kg before and after."""
        brons = {b.pk: b.remaining_kg
                 for b in Reservation.objects.filter(
                     brand=brand, status=Reservation.Status.ACTIVE)}
        sales = (Sale.objects
                 .filter(line__contract_line__brand=brand, reservation__isnull=True)
                 .select_related("line__contract_line", "customer")
                 .order_by("created_at", "pk"))
        for sale in sales:
            drawn = draw_down_bron(sale)
            if drawn:
                self.stdout.write(
                    f"  · sotuv #{sale.pk} ({sale.customer.name}, {sale.kg:,.0f} kg) "
                    f"→ bronga hisoblandi".replace(",", " "))
        moved = []
        for bron in Reservation.objects.filter(pk__in=brons).select_related("customer"):
            before = brons[bron.pk]
            if bron.remaining_kg != before:
                moved.append((bron, before, bron.remaining_kg))
        return moved

    def _report(self, title, shape):
        self.stdout.write(self.style.MIGRATE_HEADING(title))
        for line in shape["lines"]:
            self.stdout.write(
                f"    kelishuv {line.contract.code:12} kelishilgan {line.kg:,.0f} kg · "
                f"yuborilgan {line.shipped_kg:,.0f} kg".replace(",", " "))
        self.stdout.write(
            f"    omborda {shape['on_hand']:,.0f} kg · bronlangan {shape['reserved']:,.0f} kg · "
            f"sotilgan {shape['sold_kg']:,.0f} kg ({shape['sales']} ta sotuv)"
            .replace(",", " "))
        for bron in shape["brons"]:
            self.stdout.write(
                f"    BRON {bron.customer.name}: {bron.kg:,.0f} kg, berilgan "
                f"{bron.fulfilled_kg:,.0f}, qolgan {bron.remaining_kg:,.0f} · "
                f"{bron.get_status_display()}".replace(",", " "))
