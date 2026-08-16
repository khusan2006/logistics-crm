"""Rebuilding which lot each sotuv was costed against.

FIFO is order-dependent, so a lot assignment is not a fact — it is an ANSWER, and
the answer changes the moment an earlier sotuv is corrected: the lot empties sooner
and every sotuv behind it shifts down the chain. Nothing re-ran that split, so the
chain drifts. This module re-derives it from scratch for one marka.

Only tannarx and foyda ride on the answer. A qarz is `kg × narx` and never moves,
so a shift can be wrong without anybody being owed the wrong money — which is also
why it is safe to replay, and why it is not worth forcing when the picture is
unclear.

Two rules keep a replay from inventing history:

* A lot the operator CHOSE stays chosen. `pinned` slices are placed where they are.
* The date rule is a preference, not a wall. A truck that landed after the sotuv
  cannot really have filled it, but `arrived` is only as good as the day somebody
  typed it — several trucks share one arrival date routinely — so a sotuv that
  cannot be filled in time is filled anyway and reported as `undated`.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from crm.models import Sale, SaleLot, arrived_lots

ZERO = Decimal("0")


@dataclass
class Plan:
    """What replaying one marka would do, and what it could not do."""

    brand: str
    #: the marka's sotuvlar, oldest first — the order the replay walked them in
    sales: list = field(default_factory=list)
    #: sale pk -> [(ShipmentLine, kg)], the slices the replay wants
    placements: dict = field(default_factory=dict)
    #: sale pks whose STORED slices disagree with the replay
    diverged: list = field(default_factory=list)
    #: sale pks filled from a lot that had not landed on their date
    undated: set = field(default_factory=set)
    #: (sale, kg) for kg that do not exist on this marka at all
    short: list = field(default_factory=list)

    @property
    def is_clean(self):
        """Nothing to move and nothing missing — the stored chain already IS FIFO."""
        return not self.diverged and not self.short


def restocked_kg(sale):
    """kg this sotuv sent back to the shelf."""
    return sum((r.kg for r in sale.returns.all() if r.restock), ZERO)


def stored_slices(sale):
    return [(sl.line_id, sl.kg) for sl in sale.lots.all()]


def _take(lots, capacity, need, on_or_before=None):
    """Draw `need` kg from `lots` in arrival order, spending `capacity`. Restricted
    to lots that had landed by `on_or_before` when given. Returns (slices, short)."""
    slices = []
    for lot in lots:
        if need <= 0:
            break
        if on_or_before is not None and lot.arrived > on_or_before:
            continue
        take = min(capacity[lot.pk], need)
        if take <= 0:
            continue
        capacity[lot.pk] -= take
        slices.append((lot, take))
        need -= take
    return slices, need


def replay(brand, lots=None, sales=None):
    """Rebuild one marka's lot assignments from nothing and report what moves."""
    if lots is None:
        lots = list(arrived_lots().filter(contract_line__brand=brand)
                    .order_by("shipment__arrived", "id"))
    if sales is None:
        sales = list(Sale.objects
                     .filter(line__contract_line__brand=brand)
                     .select_related("customer",
                                     "line__shipment__contract__partner",
                                     "line__contract_line__contract")
                     .prefetch_related("returns", "lots__line")
                     .order_by("date", "id"))

    by_pk = {lot.pk: lot for lot in lots}
    capacity = {lot.pk: lot.kg for lot in lots}
    plan = Plan(brand=brand, sales=sales)

    for sale in sales:
        pinned = [sl for sl in sale.lots.all() if sl.pinned]
        if pinned:
            # The operator opened this lot because it is the one being sold. FIFO
            # does not get a vote; the kg simply come off it, even into the red —
            # refusing here would just move a deliberate choice somewhere else.
            slices = [(by_pk[sl.line_id], sl.kg) for sl in pinned
                      if sl.line_id in by_pk]
            for lot, kg in slices:
                capacity[lot.pk] -= kg
            short = sale.kg - sum((kg for _, kg in slices), ZERO)
        else:
            slices, short = _take(lots, capacity, sale.kg, on_or_before=sale.date)
            if short > 0:
                spill, short = _take(lots, capacity, short)
                if spill:
                    plan.undated.add(sale.pk)
                    slices += spill

        plan.placements[sale.pk] = slices
        if short > 0:
            plan.short.append((sale, short))
        if [(lot.pk, kg) for lot, kg in slices] != stored_slices(sale):
            plan.diverged.append(sale.pk)

        # A restocked qaytarish puts kg back on the shelf it came off. `available_kg`
        # holds one figure rather than a timeline, so giving them back here — in the
        # proportion they went out — leaves the running capacity matching it exactly.
        back = restocked_kg(sale)
        if back and sale.kg:
            for lot, kg in slices:
                capacity[lot.pk] += back * kg / sale.kg

    return plan


def blockers(plan, sale):
    """The sotuvlar that stop this edit from shifting automatically.

    Only what comes AFTER matters: correcting a sotuv empties its lot sooner and
    pushes the ones behind it along, so those are the rows a shift would rewrite.
    If they are all on FIFO order the shift is just re-running a rule they already
    follow. If any of them are not, the shift would overwrite an assignment that
    came from somewhere else — a hand-picked lot, a sotuv typed in days late — and
    the operator has to see it rather than have it quietly replaced."""
    after = {s.pk for s in plan.sales
             if (s.date, s.pk) >= (sale.date, sale.pk) and s.pk != sale.pk}
    return [s for s in plan.sales
            if s.pk in after and s.pk in set(plan.diverged)]


def apply_plan(plan):
    """Write the plan's slices. Returns the sale pks whose lots actually changed.

    Rewrites SaleLot and nothing else: to'lovlar and qaytarishlar hang off the Sale,
    which is never touched, so a re-costing cannot disturb money that was already
    counted. `Sale.line` follows the first slice so the lists and forms that read it
    keep agreeing with the slices."""
    changed = []
    for sale in plan.sales:
        slices = plan.placements.get(sale.pk)
        if slices is None or [(lot.pk, kg) for lot, kg in slices] == stored_slices(sale):
            continue
        sale.lots.all().delete()
        SaleLot.objects.bulk_create([
            SaleLot(sale=sale, line=lot, kg=kg) for lot, kg in slices])
        if slices and sale.line_id != slices[0][0].pk:
            sale.line_id = slices[0][0].pk
            # Straight to the column: Sale.save() would call sync_lot and undo the
            # multi-lot split we just wrote.
            Sale.objects.filter(pk=sale.pk).update(line_id=sale.line_id)
        changed.append(sale.pk)
    return changed


def brand_available_kg(brand, excluding=None):
    """kg of this marka on the shelf across every lot, ignoring one sotuv's own
    slices — what an edit to that sotuv is allowed to grow into.

    The ceiling a sotuv is measured against is the MARKA's stock, not the one lot it
    happens to sit on. The same granula routinely sits in several lots, and refusing
    to grow a sotuv because ITS lot is empty while the marka has 40 000 kg next door
    is the block that made correcting an old sotuv impossible."""
    total = ZERO
    for lot in arrived_lots().filter(contract_line__brand=brand):
        total += lot.available_kg
    if excluding is not None and excluding.pk:
        total += sum((sl.kg for sl in excluding.lots.all()), ZERO)
    return total


def place_one(sale):
    """Re-slice ONE sotuv against what is on the shelf now, leaving every other
    sotuv exactly where it is.

    This is the narrow move for when the chain cannot be replayed: the edit still
    has to land somewhere real, and a sotuv grown past its own lot would otherwise
    push that lot below zero. It fills from the sotuv's current lot first — staying
    put is the smallest change — then spills into the oldest lot with room."""
    brand = sale.line.contract_line.brand
    lots = list(arrived_lots().filter(contract_line__brand=brand)
                .order_by("shipment__arrived", "id"))
    pinned = [sl for sl in sale.lots.all() if sl.pinned]
    if pinned:
        return False

    # This sotuv's own kg are not an obstacle to itself.
    own = {sl.line_id: sl.kg for sl in sale.lots.all()}
    capacity = {lot.pk: lot.available_kg + own.get(lot.pk, ZERO) for lot in lots}
    order = ([lot for lot in lots if lot.pk == sale.line_id]
             + [lot for lot in lots if lot.pk != sale.line_id])

    slices, short = _take(order, capacity, sale.kg)
    if short > 0 or not slices:
        return False
    if [(lot.pk, kg) for lot, kg in slices] == stored_slices(sale):
        return False

    sale.lots.all().delete()
    SaleLot.objects.bulk_create([
        SaleLot(sale=sale, line=lot, kg=kg) for lot, kg in slices])
    if sale.line_id != slices[0][0].pk:
        sale.line_id = slices[0][0].pk
        Sale.objects.filter(pk=sale.pk).update(line_id=sale.line_id)
    return True


def weighted_cost(slices):
    """kg-weighted tannarx across a set of (lot, kg) slices."""
    kg = sum((k for _, k in slices), ZERO)
    if kg <= 0:
        return None
    total = sum((lot.landed_cost_per_kg * k for lot, k in slices), ZERO)
    return (total / kg).quantize(Decimal("0.0001"))
