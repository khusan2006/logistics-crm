"""Birja yuklari and the order they go out in.

The owner's rule (2026-09-22): a birja truck comes off the OLDEST birja kelishuv
that still has the marka to send, and one kelishuv is finished before the next is
started — the order the exchange hands them over in. The operator still picks the
kelishuv on the yuk form (the owner's call: not automatic); the form opens on the
oldest one with kg left and lists the rest oldest first — see ShipmentForm.

What lives here is the one-off that puts the trucks booked before that where the
rule would have put them (`manage.py birja_fifo`). A yuk belongs to one kelishuv —
its kod, its transport rate and its to'lovlar are that kelishuv's — so a truck that
straddles two kelishuvlar under the rule becomes two yuklar, the same truck on both.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from crm.models import (
    Contract, ContractLine, Sale, SaleLot, Shipment, ShipmentLine, latest_exchange_rate,
    reconcile_supplier_allocations, sync_birja_transport,
)

ZERO = Decimal("0")

#: What a split-off yuk copies from the truck it came off — everything that
#: describes the TRUCK rather than which kelishuv it is booked against.
TRUCK_FIELDS = ("status", "sent", "eta", "arrived", "transport", "container",
                "responsible", "logist", "driver_name", "driver_phone",
                "origin", "destination", "created_by")


def kg_text(value):
    """30000.000 → "30 000", 1234.500 → "1 234.5"."""
    text = f"{Decimal(value or 0):,.3f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", " ")


def kelishuv_key(contract):
    """Oldest first: the kelishuv sanasi, then the kod for two struck the same day."""
    return (contract.created, contract.code_number, contract.pk)


def dispatch_key(shipment):
    """The order trucks went out in: the day each left, then the moment it was
    entered. A split-off yuk is given its truck's `created_at` (see `_split_off`),
    so it sorts right behind that truck rather than behind every truck entered
    after it."""
    day = shipment.sent or shipment.arrived or shipment.created_at.date()
    return (day, shipment.created_at, shipment.pk)


def _take(lines, room, kg):
    """Fill `kg` from `lines` in order, spending `room` (line pk → kg free).
    Returns ([(line, kg)], the kg that found no room)."""
    pieces = []
    for line in lines:
        if kg <= 0:
            break
        take = min(room.get(line.pk, ZERO), kg)
        if take <= 0:
            continue
        room[line.pk] -= take
        pieces.append((line, take))
        kg -= take
    return pieces, kg


@dataclass
class Part:
    """One kelishuv's share of a truck — what a single yuk carries."""

    contract: Contract
    pieces: list = field(default_factory=list)   # [(ContractLine, kg)]

    @property
    def kg(self):
        return sum((kg for _, kg in self.pieces), ZERO)


def _parts(pieces):
    """[(line, kg)] → [Part], one per kelishuv, oldest kelishuv first."""
    by_contract = {}
    for line, kg in pieces:
        by_contract.setdefault(line.contract_id, Part(line.contract)).pieces.append(
            (line, kg))
    return sorted(by_contract.values(), key=lambda p: kelishuv_key(p.contract))


def split_note(parts):
    """What every half of a split truck says, so none of them reads as a truck of
    its own."""
    return "Bitta mashina: " + " + ".join(
        f"{part.contract.code} · {kg_text(part.kg)} kg" for part in parts)


def _with_note(note, line):
    return f"{note}\n{line}" if note else line


def _write_line(shipment, contract_line, kg, position):
    """A product row on a truck, priced the way the yuk form prices one: no narx of
    its own — it follows the kelishuv, live — in the kelishuv's currency, at
    today's kurs (see ShipmentLineForm)."""
    return ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract_line, kg=kg, position=position,
        price=None, price_uzs=None, currency=contract_line.contract.currency,
        exchange_rate=latest_exchange_rate())


def _split_off(truck, contract, note):
    """A new yuk for the same truck, booked against `contract`."""
    yuk = Shipment(contract=contract, note=note,
                   **{name: getattr(truck, name) for name in TRUCK_FIELDS})
    yuk.save()
    # One truck, one moment: see `dispatch_key`.
    Shipment.objects.filter(pk=yuk.pk).update(created_at=truck.created_at)
    yuk.created_at = truck.created_at
    return yuk


def settle(yuklar, contracts):
    """What every change to a birja truck's kg has to be followed by: its transport
    xarajat re-derived (rate × kg — see `sync_birja_transport`), and the to'lovlar of
    every kelishuv it touched placed again (a truck changes what a marka costs)."""
    for yuk in yuklar:
        sync_birja_transport(yuk)
    seen = set()
    for contract in contracts:
        if contract.pk not in seen:
            seen.add(contract.pk)
            reconcile_supplier_allocations(contract)


# --- putting the trucks already booked where the rule says ------------------------

@dataclass
class Move:
    """One truck's kg of one marka, and the kelishuvlar they belong on."""

    shipment: Shipment
    brand: str
    kg: Decimal
    now: Contract
    target: list   # [(Contract, kg)], oldest kelishuv first

    @property
    def is_split(self):
        return len(self.target) > 1


@dataclass
class Redistribution:
    """What putting every birja truck where the rule says would change."""

    moves: list = field(default_factory=list)
    #: truck-marka pairs already on the right kelishuv
    kept: int = 0
    #: trucks left exactly where they are, and why
    pinned: list = field(default_factory=list)
    #: what stops the plan from being applied at all
    problems: list = field(default_factory=list)
    #: {kelishuv kod: (kg now, kg after)} for every kelishuv the moves touch
    totals: dict = field(default_factory=dict)
    #: sotuv slices sitting on the rows that move
    sold_slices: int = 0


def _birja_trucks():
    trucks = list(Shipment.objects.filter(contract__partner__is_birja=True)
                  .select_related("contract__partner", "status")
                  .prefetch_related("lines__contract_line__contract",
                                    "lines__sale_lots"))
    trucks.sort(key=dispatch_key)
    return trucks


def plan_redistribution():
    """Where every birja truck's kg belong under the rule — worked out from
    nothing, trucks in the order they went out, kelishuvlar oldest first — and
    which trucks are not there now.

    Two kinds of truck are left exactly where they are and only reported: one
    carrying more than one marka (its markalar could belong on different
    kelishuvlar, and splitting a mixed truck is a call for a person), and one on a
    kelishuv moved to Kam qoldiq (that kelishuv is closed as it stands). Both still
    take up their kelishuv's kg, so the trucks around them are placed correctly."""
    plan = Redistribution()
    contracts = sorted(Contract.objects.filter(partner__is_birja=True)
                       .prefetch_related("lines__shipment_lines"), key=kelishuv_key)
    trucks = _birja_trucks()

    fixed = set()
    for truck in trucks:
        brands = {ln.contract_line.brand for ln in truck.lines.all()}
        if len(brands) > 1:
            fixed.add(truck.pk)
            plan.pinned.append((truck, "bir nechta marka"))
        elif truck.contract.closed_short:
            fixed.add(truck.pk)
            plan.pinned.append((truck, "kelishuv Kam qoldiqda"))

    shipped_now = defaultdict(lambda: ZERO)
    shipped_after = defaultdict(lambda: ZERO)
    brands = sorted({ln.brand for c in contracts for ln in c.lines.all()})
    for brand in brands:
        room = {}
        for contract in contracts:
            lines = [ln for ln in contract.lines.all() if ln.brand == brand]
            if lines and not contract.closed_short:
                room[contract.pk] = sum((ln.kg for ln in lines), ZERO)
        for truck in trucks:
            if truck.pk in fixed:
                kg = sum((ln.kg for ln in truck.lines.all()
                          if ln.contract_line.brand == brand), ZERO)
                if truck.contract_id in room:
                    room[truck.contract_id] -= kg
        order = [c for c in contracts if c.pk in room]

        for truck in trucks:
            kg = sum((ln.kg for ln in truck.lines.all()
                      if ln.contract_line.brand == brand), ZERO)
            if kg <= 0:
                continue
            if truck.pk in fixed:
                shipped_now[truck.contract.code] += kg
                shipped_after[truck.contract.code] += kg
                continue
            target, need = [], kg
            for contract in order:
                if need <= 0:
                    break
                take = min(room[contract.pk], need)
                if take <= 0:
                    continue
                room[contract.pk] -= take
                need -= take
                target.append((contract, take))
            if need > 0:
                plan.problems.append(
                    f"yuk #{truck.pk}: {brand} ning {kg_text(need)} kg i hech bir "
                    f"birja kelishuviga sig'madi")
                continue
            shipped_now[truck.contract.code] += kg
            for contract, take in target:
                shipped_after[contract.code] += take
            if target == [(truck.contract, kg)]:
                plan.kept += 1
                continue
            plan.moves.append(Move(truck, brand, kg, truck.contract, target))
            plan.sold_slices += sum(len(ln.sale_lots.all()) for ln in truck.lines.all()
                                    if ln.contract_line.brand == brand)

    touched = ({m.now.code for m in plan.moves}
               | {c.code for m in plan.moves for c, _ in m.target})
    plan.totals = {code: (shipped_now[code], shipped_after[code])
                   for code in sorted(touched, key=lambda code: next(
                       kelishuv_key(c) for c in contracts if c.code == code))}
    return plan


def _reslice(slices, lines):
    """Lay the sotuv slices that sat on a truck's old rows over its new ones —
    oldest sotuv first, rows filled in the order FIFO reads lots (arrival, then
    id), so a later `crm.fifo.replay` finds nothing to move. What the rows cannot
    hold (a lot sold past its kg on restocked returns) stays on the last one, as it
    stood before."""
    if not slices:
        return
    lines = sorted(lines, key=lambda ln: (ln.shipment.arrived or date.max, ln.pk))
    room = {ln.pk: ln.kg for ln in lines}
    fresh, sales = [], {}
    for old in slices:
        need = old.kg
        for line in lines:
            if need <= 0:
                break
            take = min(room[line.pk], need)
            if take <= 0:
                continue
            room[line.pk] -= take
            need -= take
            fresh.append(SaleLot(sale_id=old.sale_id, line=line, kg=take,
                                 pinned=old.pinned))
        if need > 0:
            fresh.append(SaleLot(sale_id=old.sale_id, line=lines[-1], kg=need,
                                 pinned=old.pinned))
        sales[old.sale_id] = old.sale
    SaleLot.objects.filter(pk__in=[old.pk for old in slices]).delete()
    SaleLot.objects.bulk_create(fresh)
    # `Sale.line` follows the sotuv's first slice — see crm.fifo.apply_plan.
    for sale in sales.values():
        first = min(SaleLot.objects.filter(sale=sale).select_related("line__shipment"),
                    key=lambda sl: (sl.line.shipment.arrived or date.max, sl.line_id))
        if sale.line_id != first.line_id:
            Sale.objects.filter(pk=sale.pk).update(line_id=first.line_id)


def _rehome(move, parts):
    """Put one truck's kg of one marka where the plan says, keeping every sotuv
    slice drawn off them. The truck carries the oldest part; each further part is
    split off into a yuk of its own. Returns every yuk written."""
    truck = move.shipment
    old = sorted((ln for ln in truck.lines.all()
                  if ln.contract_line.brand == move.brand),
                 key=lambda ln: (ln.position, ln.pk))
    slices = sorted(SaleLot.objects.filter(line__in=old).select_related("sale"),
                    key=lambda sl: (sl.sale.date, sl.sale_id, sl.pk))

    original_note = truck.note
    note = split_note(parts) if len(parts) > 1 else None
    truck.contract = parts[0].contract
    if note:
        truck.note = _with_note(original_note, note)
    truck.save(update_fields=["contract", "note"])
    yuklar = [truck] + [_split_off(truck, part.contract, _with_note(original_note, note))
                        for part in parts[1:]]

    rows, spare = [], iter(old)
    for yuk, part in zip(yuklar, parts):
        for position, (contract_line, kg) in enumerate(part.pieces):
            row = next(spare, None)
            if row is None:
                row = _write_line(yuk, contract_line, kg, position)
            else:
                row.shipment, row.contract_line, row.kg = yuk, contract_line, kg
                row.position, row.currency = position, contract_line.contract.currency
                row.save(update_fields=["shipment", "contract_line", "kg", "position",
                                        "currency"])
            rows.append(row)
    leftover = list(spare)
    _reslice(slices, rows)
    for row in leftover:
        row.delete()
    return yuklar


def apply_redistribution(plan):
    """Carry the plan out. Runs inside the caller's transaction and checks what
    must not move — every lot within its kg, every marka's shipped kg and every
    sotuv's kg exactly as they were — raising if anything did, so the transaction
    rolls back rather than leaving half a move. Returns [(Move, yuklar written)]."""
    if plan.problems:
        raise ValueError("; ".join(plan.problems))
    before = _invariants()

    moving = {ln.pk for m in plan.moves for ln in m.shipment.lines.all()
              if ln.contract_line.brand == m.brand}
    birja_lines = list(ContractLine.objects.filter(contract__partner__is_birja=True)
                       .select_related("contract")
                       .prefetch_related("shipment_lines")
                       .order_by("position", "id"))
    room = {cl.pk: cl.kg - sum((sl.kg for sl in cl.shipment_lines.all()
                                if sl.pk not in moving), ZERO)
            for cl in birja_lines}
    by_contract = defaultdict(list)
    for cl in birja_lines:
        by_contract[(cl.contract_id, cl.brand)].append(cl)

    done = []
    for move in sorted(plan.moves, key=lambda m: dispatch_key(m.shipment)):
        pieces = []
        for contract, kg in move.target:
            taken, short = _take(by_contract[(contract.pk, move.brand)], room, kg)
            if short > 0:
                raise ValueError(f"{contract.code}: {move.brand} uchun {short} kg "
                                 f"joy topilmadi")
            pieces += taken
        written = _rehome(move, _parts(pieces))
        # Read back fresh: the truck came in with its OLD rows prefetched, and
        # `Shipment.kg` would go on reading that list — pricing its transport off kg
        # it no longer carries.
        fresh = {yuk.pk: yuk for yuk in Shipment.objects.filter(
            pk__in=[yuk.pk for yuk in written]).select_related("contract__partner")}
        yuklar = [fresh[yuk.pk] for yuk in written]
        settle(yuklar, [move.now] + [contract for contract, _ in move.target])
        done.append((move, yuklar))

    after = _invariants()
    for key in ("sold", "shipped"):
        if before[key] != after[key]:
            raise ValueError(f"{key} o'zgarib qoldi — hech narsa yozilmadi")
    over = [cl for cl in ContractLine.objects.filter(contract__partner__is_birja=True)
            .prefetch_related("shipment_lines") if cl.remaining_kg < 0]
    if over:
        raise ValueError("kelishuvdan ortiq yuborilgan lot: "
                         + ", ".join(str(cl.pk) for cl in over))
    return done


def _invariants():
    """What a redistribution may never change: how much of each marka the birja
    trucks carry, and how many kg each sotuv drew."""
    shipped = defaultdict(lambda: ZERO)
    for line in ShipmentLine.objects.filter(
            shipment__contract__partner__is_birja=True).select_related("contract_line"):
        shipped[line.contract_line.brand] += line.kg
    sold = defaultdict(lambda: ZERO)
    for sl in SaleLot.objects.all():
        sold[sl.sale_id] += sl.kg
    return {"shipped": dict(shipped), "sold": dict(sold)}
