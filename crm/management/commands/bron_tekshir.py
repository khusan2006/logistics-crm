"""Why does this bron say that many kg berilgan?

    python manage.py bron_tekshir --mijoz "mak plast" --marka "i 1561" \
        --settings=config.settings_prodcopy

Reads only — nothing is written, so it is safe against the live database.

"Berilgan" on Bronlar is `Reservation.fulfilled_kg`, and it is a STORED figure: it
moves when a sotuv is saved, edited or deleted, not when the page is opened. When it
disagrees with what the operator remembers handing over, the difference is always one
of a short list, and this prints every item on that list side by side:

  · sotuvlar counted into the bron (the `BronDraw` rows that add up to berilgan),
  · sotuvlar of that marka to that mijoz that NO bron counted — typed in before the
    bron existed, or entered with "Brondan ushlansin" unticked. These are what the
    Bronlar row's "Oldingi sotuvni bronga hisoblash" button is for,
  · sotuvlar under a LOOKALIKE spelling of the marka — `i 1561` beside `и 1561` is
    two products to the database and one product to the reader (see merge_brand),
  · a sotuv capped by the bron: it took the room that was left, and its other kg
    are an ordinary sale that was never promised,
  · vazvrat — kg that came back after being given. A qaytarish does not put kg
    back on the bron, so berilgan counts goods the mijoz no longer holds,
  · and finally berilgan against the kg its own draws account for. A gap there is
    the accounting itself being wrong rather than a sotuv being missed.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from crm.models import Reservation, Return, Sale
from crm.yozuv import marka_kaliti


def _kg(value):
    return f"{value:,.3f}".replace(",", " ").rstrip("0").rstrip(".")


def _codepoints(text):
    """The marka spelled out, so the report can SHOW a difference the screen hides."""
    return " ".join(f"U+{ord(ch):04X}" for ch in text if not ch.isspace())


class Command(BaseCommand):
    help = ("Explain a bron's 'berilgan' figure: what was counted into it, what was "
            "not, and why. Reads only.")

    def add_arguments(self, parser):
        parser.add_argument("--mijoz", default="",
                            help="mijoz nomi (qismi ham bo'ladi)")
        parser.add_argument("--marka", default="",
                            help="marka nomi (qismi ham bo'ladi)")
        parser.add_argument("--bron", type=int, default=None,
                            help="aniq bron raqami — boshqa filtrlar o'rniga")
        parser.add_argument("--hammasi", action="store_true",
                            help="yopilgan va bekor qilingan bronlarni ham ko'rsatish")

    def handle(self, *args, **options):
        brons = Reservation.objects.select_related("customer").order_by("created_at",
                                                                        "pk")
        if options["bron"]:
            brons = brons.filter(pk=options["bron"])
        else:
            if options["mijoz"]:
                brons = brons.filter(customer__name__icontains=options["mijoz"])
            if options["marka"]:
                brons = brons.filter(brand__icontains=options["marka"])
            if not options["hammasi"]:
                brons = brons.filter(status__in=[Reservation.Status.ACTIVE,
                                                 Reservation.Status.CONVERTED])
        brons = list(brons)
        if not brons:
            raise CommandError("Bunday bron topilmadi — filtrlarni tekshiring")
        for bron in brons:
            self._report(bron)

    # ── one bron ────────────────────────────────────────────────────────────────

    def _report(self, bron):
        write = self.stdout.write
        write(self.style.MIGRATE_HEADING(
            f"\nBRON #{bron.pk} · {bron.customer.name} · {bron.brand} · "
            f"{bron.get_status_display()}"))
        write(f"  marka kodlari  {_codepoints(bron.brand)}")
        write(f"  ochilgan       {bron.created_at:%Y-%m-%d %H:%M}")
        write(f"  bron qilingan  {_kg(bron.kg)} kg")
        write(f"  BERILGAN       {_kg(bron.fulfilled_kg)} kg")
        write(f"  qolgan         {_kg(bron.remaining_kg)} kg")

        drawn = self._draws(bron)
        counted = self._uncounted(bron)
        self._lookalikes(bron)
        self._returns(bron)
        self._verdict(bron, drawn, counted)

    def _draws(self, bron):
        """The sotuvlar that make up berilgan, and how much of each one it took."""
        draws = list(bron.draws.select_related("sale__line__contract_line")
                     .order_by("sale__date", "sale__pk"))
        self.stdout.write("\n  BRONGA HISOBLANGAN SOTUVLAR")
        if not draws:
            self.stdout.write("    (yo'q)")
            return Decimal("0")
        total = Decimal("0")
        for draw in draws:
            sale = draw.sale
            capped = "" if draw.kg == sale.kg else (
                f"   ← sotuv {_kg(sale.kg)} kg edi, bronda shuncha joy qolgandi")
            self.stdout.write(
                f"    sotuv #{sale.pk:<6} {sale.date:%Y-%m-%d}  "
                f"{_kg(draw.kg)} kg{capped}")
            total += draw.kg
        self.stdout.write(f"    jami {_kg(total)} kg")
        return total

    def _uncounted(self, bron):
        """This mijoz's sotuvlar of this marka that no bron ever counted, with the
        reason each one was passed over. These are the rows the Bronlar button
        'Oldingi sotuvni bronga hisoblash' offers."""
        rows = (Sale.objects
                .filter(customer_id=bron.customer_id, reservation__isnull=True,
                        line__contract_line__brand=bron.brand)
                .select_related("line__contract_line").order_by("date", "pk"))
        self.stdout.write("\n  HISOBLANMAGAN SOTUVLAR (shu marka, shu mijoz)")
        rows = list(rows)
        if not rows:
            self.stdout.write("    (yo'q)")
            return Decimal("0")
        total = Decimal("0")
        for sale in rows:
            # `draw_down_bron` refuses a bron younger than the sotuv: a promise made
            # afterwards is not one that sotuv can have served. The other way in is
            # the operator unticking "Brondan ushlansin".
            why = ("sotuv brondan OLDIN kiritilgan"
                   if sale.created_at and sale.created_at < bron.created_at
                   else "“Brondan ushlansin” belgilanmagan bo'lishi mumkin")
            self.stdout.write(
                f"    sotuv #{sale.pk:<6} {sale.date:%Y-%m-%d}  "
                f"{_kg(sale.kg)} kg   ← {why}")
            total += sale.kg
        self.stdout.write(
            f"    jami {_kg(total)} kg — Bronlar sahifasidagi “Oldingi sotuvni "
            f"bronga hisoblash” tugmasi shularni taklif qiladi")
        return total

    def _lookalikes(self, bron):
        """Other spellings of the same marka, and what this mijoz bought under them.

        Every join the app makes on a marka is an exact string match, so `i 1561` and
        `и 1561` are two products. The halves of one product then never meet and the
        bron sits at its full kg while its own holder buys the granula."""
        key = marka_kaliti(bron.brand)
        names = {name for name in Sale.objects
                 .filter(customer_id=bron.customer_id)
                 .values_list("line__contract_line__brand", flat=True).distinct()
                 if name != bron.brand and marka_kaliti(name) == key}
        if not names:
            return
        self.stdout.write(self.style.WARNING(
            "\n  DIQQAT — SHU MARKANING BOSHQA YOZILISHI BOR"))
        for name in sorted(names):
            sales = Sale.objects.filter(customer_id=bron.customer_id,
                                        line__contract_line__brand=name)
            kg = sum((s.kg for s in sales), Decimal("0"))
            self.stdout.write(
                f"    “{name}”  {_codepoints(name)}  ·  {sales.count()} ta sotuv, "
                f"{_kg(kg)} kg")
        self.stdout.write(
            "    Ekranda bir xil ko'rinadi, bazada boshqa mahsulot. Agar bitta "
            "granula bo'lsa: python manage.py merge_brand ... --into ...")

    def _returns(self, bron):
        """Kg that came BACK after being given. A vazvrat does not put kg back on the
        bron — berilgan therefore counts granula the mijoz no longer holds."""
        rows = list(Return.objects
                    .filter(sale__reservation_id=bron.pk)
                    .select_related("sale").order_by("date", "pk"))
        if not rows:
            return
        total = sum((r.kg for r in rows), Decimal("0"))
        self.stdout.write("\n  VAZVRAT (bronga hisoblangan sotuvlardan qaytgan)")
        for row in rows:
            self.stdout.write(f"    sotuv #{row.sale_id:<6} {row.date:%Y-%m-%d}  "
                              f"{_kg(row.kg)} kg qaytgan")
        self.stdout.write(
            f"    jami {_kg(total)} kg — bu kg bronga QAYTARILMAYDI, berilgan "
            f"figurasi ichida turaveradi")

    def _verdict(self, bron, drawn, uncounted):
        write = self.stdout.write
        write("\n  XULOSA")
        if drawn != bron.fulfilled_kg:
            write(self.style.ERROR(
                f"    berilgan {_kg(bron.fulfilled_kg)} kg, hisoblangan sotuvlar "
                f"esa {_kg(drawn)} kg — {_kg(abs(bron.fulfilled_kg - drawn))} kg "
                f"farq. Bu raqamning o'zi noto'g'ri."))
        if uncounted:
            write(self.style.WARNING(
                f"    {_kg(uncounted)} kg sotuv bu bronga hisoblanmagan — mijoz "
                f"olgan, bron ko'rmagan. Eng ehtimolli sabab shu."))
        if drawn == bron.fulfilled_kg and not uncounted:
            write("    berilgan figurasi hisoblangan sotuvlarga to'liq mos.")
