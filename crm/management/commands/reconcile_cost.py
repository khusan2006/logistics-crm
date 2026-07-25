"""Read-only reconciliation: compare each sale's originally-recorded profit (the
frozen `cost_price_snapshot`) against the new live profit, grouped by month, so you
can see exactly how much history moves before/after switching to fully-live tannarx.

    python manage.py reconcile_cost --settings=config.settings_dev

Changes nothing — it only reads.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from crm.models import Sale


def old_profit(sale):
    """Profit as it was originally recorded, from the frozen snapshot — mirroring
    the pre-live-tannarx formula. None when the sale has no snapshot (created after
    the switch, so old and new are the same by definition)."""
    snap = sale.cost_price_snapshot
    if snap is None:
        return None
    base = ((sale.price - snap) * sale.kg).quantize(Decimal("0.01"))
    returned = sum(((r.price - snap) * r.kg for r in sale.returns.all() if r.restock),
                   Decimal("0"))
    return base - returned


def monthly_reconciliation(sales=None):
    """(rows, sales_without_snapshot). Each row: month, old, new, delta, count."""
    qs = Sale.objects.all() if sales is None else sales
    qs = qs.select_related("line__shipment", "line__contract_line__contract")
    buckets = defaultdict(lambda: {"old": Decimal("0"), "new": Decimal("0"), "count": 0})
    without_snapshot = 0
    for sale in qs:
        old = old_profit(sale)
        if old is None:
            without_snapshot += 1
            continue
        b = buckets[sale.date.replace(day=1)]
        b["old"] += old
        b["new"] += sale.profit
        b["count"] += 1
    rows = [{"month": m, "old": b["old"], "new": b["new"],
             "delta": b["new"] - b["old"], "count": b["count"]}
            for m, b in sorted(buckets.items())]
    return rows, without_snapshot


class Command(BaseCommand):
    help = ("Read-only: per-month old (frozen) vs new (live) sale profit and the "
            "difference. Run before migrating production to preview the impact.")

    def handle(self, *args, **options):
        rows, without_snapshot = monthly_reconciliation()
        if not rows:
            self.stdout.write("Solishtirishga tarixiy snapshotli sotuv yo'q.")
            if without_snapshot:
                self.stdout.write(f"({without_snapshot} ta sotuvda tarixiy tannarx yo'q.)")
            return

        w = 14
        self.stdout.write(f"{'Oy':<10}{'Eski foyda':>{w}}{'Yangi foyda':>{w}}"
                          f"{'Farq':>{w}}{'Sotuv':>8}")
        self.stdout.write("-" * (10 + w * 3 + 8))
        tot_old = tot_new = Decimal("0")
        tot_n = 0
        for r in rows:
            month = f"{r['month']:%Y-%m}"
            self.stdout.write(
                f"{month:<10}{r['old']:>{w},.2f}{r['new']:>{w},.2f}"
                f"{r['delta']:>{w},.2f}{r['count']:>8}")
            tot_old += r["old"]
            tot_new += r["new"]
            tot_n += r["count"]
        self.stdout.write("-" * (10 + w * 3 + 8))
        self.stdout.write(
            f"{'JAMI':<10}{tot_old:>{w},.2f}{tot_new:>{w},.2f}"
            f"{tot_new - tot_old:>{w},.2f}{tot_n:>8}")
        self.stdout.write("")
        self.stdout.write(f"Umumiy foyda o'zgarishi: {tot_new - tot_old:,.2f} $")
        if without_snapshot:
            self.stdout.write(
                f"({without_snapshot} ta sotuv tarixiy snapshotsiz — solishtirishga kirmadi.)")
