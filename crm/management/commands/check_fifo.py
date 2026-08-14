"""Read-only FIFO consistency check: replay every marka's sales from scratch and
report the ones sitting on a different lot than FIFO would put them on today.

    python manage.py check_fifo --settings=config.settings_dev
    python manage.py check_fifo --brand "2102 campaund" --settings=config.settings_dev

Why the answer can differ from what is stored: FIFO is order-dependent. A sale
entered — or later corrected — out of order shifts every sale after it down the
chain, and nothing re-runs the split. The lot only drives `cost_price`, so a stale
chain never moves a qarz; it misstates tannarx and foyda per sotuv.

Two deliberate simplifications, both worth knowing before reading the numbers:

* A restocked qaytarish is netted off the sale that drew the kg, matching
  `ShipmentLine.available_kg` — which holds one capacity figure rather than a
  timeline, so a return gives its kg back to the lot at no particular moment.
* There is no pinned flag yet, so a sale entered through the lot picker (which
  bypasses FIFO on purpose — see `sale_create_lot`) is indistinguishable from a
  legacy one. Sales carrying a `group` were definitely split by FIFO, so a
  disagreement there is real drift; the rest are reported separately because the
  operator may have chosen that lot on purpose.

Changes nothing — it only reads.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from crm.fifo import replay, weighted_cost
from crm.models import Sale, arrived_lots


def check(brand=None):
    """[(marka, moved, undated, short, sales_count)] for every marka that has sotuv,
    where `moved` holds one dict per sale the replay disagrees with."""
    lots_by_brand = defaultdict(list)
    for lot in arrived_lots().order_by("shipment__arrived", "id"):
        lots_by_brand[lot.brand].append(lot)

    sales_by_brand = defaultdict(list)
    sales = (Sale.objects
             .select_related("customer", "line__shipment",
                             "line__contract_line__contract")
             .prefetch_related("returns", "lots__line")
             .order_by("date", "id"))
    for sale in sales:
        sales_by_brand[sale.line.contract_line.brand].append(sale)

    report = []
    for marka in sorted(sales_by_brand):
        if brand and marka != brand:
            continue
        rows = sales_by_brand[marka]
        plan = replay(marka, lots=lots_by_brand.get(marka, []), sales=rows)
        diverged = set(plan.diverged)
        moved = []
        for sale in rows:
            if sale.pk not in diverged:
                continue
            slices = plan.placements[sale.pk]
            now = sale.cost_price
            new = weighted_cost(slices)
            moved.append({
                "sale": sale,
                "kg": sale.kg,
                "slices": slices,
                "cost_now": now,
                "cost_new": new,
                # Foyda is (narx − tannarx) × kg, so a cheaper lot lifts it.
                "delta": ((now - new) * sale.kg).quantize(Decimal("0.01"))
                         if new is not None else None,
                # A sotuv split by FIFO cannot have had its lot chosen by hand, so a
                # disagreement here is drift rather than a deliberate override.
                "fifo": sale.group is not None,
                "undated": sale.pk in plan.undated,
            })
        report.append((marka, moved, plan.undated, plan.short, len(rows)))
    return report


class Command(BaseCommand):
    help = ("Read-only: replay FIFO per marka and list the sotuvlar whose lot — and "
            "therefore tannarx and foyda — disagree with it.")

    def add_arguments(self, parser):
        parser.add_argument("--brand", help="Faqat shu markani tekshirish")

    def handle(self, *args, **options):
        report = check(options.get("brand"))
        if not report:
            self.stdout.write("Tekshiriladigan sotuv yo'q.")
            return

        total_sales = total_moved = total_fifo = total_undated = 0
        total_delta = Decimal("0")
        problem_brands = []

        for marka, moved, undated, short, count in report:
            total_sales += count
            total_moved += len(moved)
            total_undated += len(undated)
            if not moved and not short:
                continue
            problem_brands.append(marka)
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"{marka} · {count} sotuv · {len(moved)} tasi boshqa lotga tushadi"))

            for row in moved:
                sale = row["sale"]
                target = "+".join(f"#{lot.pk}" for lot, _ in row["slices"]) or "—"
                mark = "FIFO" if row["fifo"] else "qo'lda?"
                total_fifo += 1 if row["fifo"] else 0
                if row["delta"] is not None:
                    total_delta += row["delta"]
                    cost = (f"{row['cost_now']:.4f} → {row['cost_new']:.4f}"
                            f"  foyda {row['delta']:+,.2f}")
                else:
                    cost = "joylashtirib bo'lmadi"
                if row["undated"]:
                    mark += ", sana"
                self.stdout.write(
                    f"  #{sale.pk:<4} {sale.date:%d.%m.%y}  "
                    f"{sale.customer.name[:18]:<18} {row['kg']:>9,.0f} kg  "
                    f"lot #{sale.line_id} → lot {target:<10} {cost}   [{mark}]")

            for sale, missing in short:
                self.stdout.write(self.style.ERROR(
                    f"  ! sotuv #{sale.pk}: {missing:,.0f} kg omborda umuman yo'q "
                    f"(kelgan miqdor yoki boshqa sotuv noto'g'ri)"))

        self.stdout.write("")
        self.stdout.write("-" * 72)
        self.stdout.write(f"Tekshirildi: {total_sales} sotuv, {len(report)} marka")
        if not problem_brands:
            self.stdout.write(self.style.SUCCESS(
                "Hammasi FIFO tartibida — hech narsa siljimaydi."))
            return
        self.stdout.write(
            f"Mos kelmadi:  {total_moved} sotuv ({total_fifo} tasi FIFO bilan "
            f"kiritilgan — aniq siljish)")
        self.stdout.write(f"Foyda farqi:  {total_delta:+,.2f} $")
        self.stdout.write(
            "Qarzga ta'sir qilmaydi — faqat tannarx va foyda o'zgaradi.")
        if total_undated:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"{total_undated} sotuv o'z sanasida hali kelmagan lotdan olindi. "
                "Yuklarning “yetib kelgan sana”si tekshirilsin — bir kunda "
                "to'planib qo'yilgan bo'lsa, FIFO tartibi ham shundan buziladi."))
