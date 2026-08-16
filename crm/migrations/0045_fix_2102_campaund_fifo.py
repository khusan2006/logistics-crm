"""Put sotuv #2 back to the 24 000 kg it was entered as, and re-run FIFO for the
marka behind it.

On 25.07 at 14:10 the sotuv was recorded as 24 000 kg — a full truck of vazifadon-1 ·
1-yuk — and every sotuv typed after it that afternoon was placed against a shelf with
that truck full. The next day at 14:19 somebody changed it to 16 950 by accident. That
freed 7 050 kg in the middle of the chain, and nothing re-ran the split: the sotuvlar
behind it stayed where they were, and three days later a sotuv dated 27.07 took the
gap that belonged to sotuvlar dated 21.07.

Restoring the kg alone makes it WORSE — 16 mismatched sotuvlar become 24 — because
the thirty-odd August sotuvlar were placed against the 16 950 world. The kg and the
replay have to land together, which is why this is one migration and not two.

The replay is written out here rather than imported from `crm.fifo`, because a data
migration has to keep doing what it did on the day it was written even after that
module has moved on.

Nothing is guessed: the sotuv is only touched when it is still in the exact state
this was written against, so the migration is a no-op on a database where it has
already run, where somebody fixed it by hand, or which never had the problem.
"""
from decimal import Decimal

from django.db import migrations

BRAND = "2102 campaund"
SALE_PK = 2
WRONG_KG = Decimal("16950")
RIGHT_KG = Decimal("24000")


def _restocked(apps, sale_ids):
    """kg each sotuv sent back to the shelf, by sale id."""
    Return = apps.get_model("crm", "Return")
    out = {}
    for kg, sale_id in Return.objects.filter(sale_id__in=sale_ids, restock=True
                                             ).values_list("kg", "sale_id"):
        out[sale_id] = out.get(sale_id, Decimal("0")) + kg
    return out


def _replay(apps, brand):
    """FIFO for one marka, rebuilt from nothing. Returns {sale id: [(lot, kg)]}.

    Oldest truck first, and a sotuv draws from what had already landed on its date —
    falling back to the whole marka when nothing had, because `arrived` is only as
    good as the day somebody typed it and several trucks share one date routinely.
    A lot the operator chose stays chosen."""
    Sale = apps.get_model("crm", "Sale")
    ShipmentLine = apps.get_model("crm", "ShipmentLine")

    lots = list(ShipmentLine.objects
                .filter(contract_line__brand=brand, shipment__arrived__isnull=False)
                .select_related("shipment")
                .order_by("shipment__arrived", "id"))
    sales = list(Sale.objects.filter(line__contract_line__brand=brand)
                 .prefetch_related("lots")
                 .order_by("date", "id"))
    back = _restocked(apps, [s.pk for s in sales])

    capacity = {lot.pk: lot.kg for lot in lots}
    by_pk = {lot.pk: lot for lot in lots}
    placements = {}
    for sale in sales:
        pinned = [sl for sl in sale.lots.all() if sl.pinned]
        if pinned:
            slices = [(by_pk[sl.line_id], sl.kg) for sl in pinned
                      if sl.line_id in by_pk]
            for lot, kg in slices:
                capacity[lot.pk] -= kg
        else:
            need, slices = sale.kg, []
            for cutoff in (sale.date, None):
                if need <= 0:
                    break
                for lot in lots:
                    if need <= 0:
                        break
                    if cutoff is not None and lot.shipment.arrived > cutoff:
                        continue
                    take = min(capacity[lot.pk], need)
                    if take <= 0:
                        continue
                    capacity[lot.pk] -= take
                    slices.append((lot, take))
                    need -= take
            if need > 0:
                raise RuntimeError(
                    f"“{brand}”: sotuv #{sale.pk} uchun {need} kg yetishmadi — "
                    f"hech narsa o'zgartirilmadi")
        placements[sale.pk] = slices

        # A restocked qaytarish goes back on the shelf it came off, in the
        # proportion it left — matching how `available_kg` counts it.
        given = back.get(sale.pk)
        if given and sale.kg:
            for lot, kg in slices:
                capacity[lot.pk] += given * kg / sale.kg
    return sales, placements


def _labels(apps):
    """lot pk → "vazifadon-3 · 2-yuk", how a yuk is named out loud.

    Spelled out from the columns: a historical model carries fields, not the `code`
    and `label` properties the live models have."""
    Shipment = apps.get_model("crm", "Shipment")
    ShipmentLine = apps.get_model("crm", "ShipmentLine")

    lots = list(ShipmentLine.objects
                .filter(contract_line__brand=BRAND, shipment__arrived__isnull=False)
                .select_related("shipment__contract"))
    position = {}
    for contract_id in {lot.shipment.contract_id for lot in lots}:
        rows = sorted(Shipment.objects.filter(contract_id=contract_id)
                      .values_list("pk", "sent"),
                      key=lambda r: (r[1] is None, r[1], r[0]))
        for index, (ship_id, _) in enumerate(rows, start=1):
            position[ship_id] = index
    out = {}
    for lot in lots:
        contract = lot.shipment.contract
        code = f"{contract.code_slug}-{contract.code_number}"
        out[lot.pk] = f"{code} · {position[lot.shipment_id]}-yuk"
    return out


def _write(apps, sales, placements):
    """Rewrite the slices, and say in the audit trail which sotuvlar moved."""
    AuditLog = apps.get_model("crm", "AuditLog")
    Sale = apps.get_model("crm", "Sale")
    SaleLot = apps.get_model("crm", "SaleLot")
    ShipmentLine = apps.get_model("crm", "ShipmentLine")

    labels = _labels(apps)
    moved = 0
    for sale in sales:
        slices = placements[sale.pk]
        was = [(sl.line_id, sl.kg) for sl in sale.lots.all()]
        if was == [(lot.pk, kg) for lot, kg in slices]:
            continue
        sale.lots.all().delete()
        SaleLot.objects.bulk_create([
            SaleLot(sale_id=sale.pk, line_id=lot.pk, kg=kg) for lot, kg in slices])
        if slices and sale.line_id != slices[0][0].pk:
            Sale.objects.filter(pk=sale.pk).update(line_id=slices[0][0].pk)
        frm = " + ".join(labels.get(lid, f"lot #{lid}") for lid, _ in was)
        to = " + ".join(labels.get(lot.pk, f"lot #{lot.pk}") for lot, _ in slices)
        AuditLog.objects.create(
            action="update", target_type="Sotuv", target_id=sale.pk,
            summary=f"Lot qayta hisoblandi (FIFO): {frm} → {to}"[:255])
        moved += 1
    return moved


def fix(apps, schema_editor):
    Sale = apps.get_model("crm", "Sale")
    AuditLog = apps.get_model("crm", "AuditLog")

    sale = (Sale.objects.filter(pk=SALE_PK, kg=WRONG_KG,
                                line__contract_line__brand=BRAND)
            .select_related("customer").first())
    if sale is None:
        # Already corrected, corrected by hand, or a database that never had it.
        return

    Sale.objects.filter(pk=sale.pk).update(kg=RIGHT_KG)
    AuditLog.objects.create(
        action="update", target_type="Sotuv", target_id=sale.pk,
        summary=f"Sotuv tahrirlandi: {WRONG_KG} kg → {RIGHT_KG} kg · "
                f"{sale.customer.name} (tuzatish)"[:255])

    sale.kg = RIGHT_KG
    sales, placements = _replay(apps, BRAND)
    _write(apps, sales, placements)


def unfix(apps, schema_editor):
    """Put the kg back and re-run FIFO again.

    Not a true undo: what the lots looked like BEFORE the fix was drift nobody ever
    recorded, so it cannot be reconstructed. This returns the marka to 16 950 kg with
    a consistent FIFO split behind it — which is a defined state, just not the exact
    muddle that was there before."""
    Sale = apps.get_model("crm", "Sale")
    sale = Sale.objects.filter(pk=SALE_PK, kg=RIGHT_KG,
                               line__contract_line__brand=BRAND).first()
    if sale is None:
        return
    Sale.objects.filter(pk=sale.pk).update(kg=WRONG_KG)
    sales, placements = _replay(apps, BRAND)
    _write(apps, sales, placements)


class Migration(migrations.Migration):

    dependencies = [("crm", "0044_backfill_sale_lots")]

    operations = [migrations.RunPython(fix, unfix)]
