"""Store every marka name in kiril, the alphabet the ombor actually works in.

The app serves lotin and transliterates to kiril in the browser, which is right for
its OWN words — they are Uzbek and the reader picks the script. A marka is not one of
those words. It is a name, typed once, and the operators read and say it in kiril
only, so the lotin spelling on file was a form nobody used.

Half-converting it was worse than not converting. `static/js/yozuv.js` refuses a word
carrying a `c` outside "ch" or a `w` — it is then not Uzbek Latin at all — so
`2102 repak` drew as `2102 репак` while `2102 campaund` stayed in lotin beside it, and
the same list read in two alphabets. Only the stored name can settle that, which is
what this does.

`i 1561` → `и 1561` is the one entry that is a MERGE rather than a rename: both
spellings were on the books, drawn identically (latin i transliterates to и), and
joined by nothing — a 150 000 kg bron stood at its full kg while its holder bought
39 000 kg of the same granula over the counter, because every join the app makes on a
marka is an exact string match. So its bronlar are replayed against the sotuvlar the
split had hidden from them. See crm/management/commands/merge_brand.py, which does the
same thing for any pair found later, and ContractLineForm.clean_brand, which is what
stops a second spelling being typed at all.

`2102 repac` becomes `2102 репас` and stays SEPARATE from `2102 репак`. Whether those
two are one product is a question about granula, not about spelling, and nobody has
answered it; keeping them apart is the answer that can still be changed by one
merge_brand run, while joining them cannot be undone.

The map is written out rather than computed, so this migration does exactly what it
says on the day it was written. A marka added afterwards is not its business — the
form now refuses a lotin name that would collide with a kiril one.

Not reversible: putting the lotin names back would re-split the pair this joined.
"""
from django.db import migrations

#: The names as they stood on 2026-09-09, and what each becomes.
KIRIL = {
    "i 1561": "и 1561",             # a MERGE — и 1561 already existed
    "209 campaund": "209 кампаунд",
    "2102 campaund": "2102 кампаунд",
    "7000 campaund": "7000 кампаунд",
    "2102 repak": "2102 репак",
    "7000 repak": "7000 репак",
    "2102 repac": "2102 репас",     # deliberately NOT joined to 2102 репак
    "ftor oq": "фтор оқ",
    "ftor sariq": "фтор сариқ",
}


def _redraw_brons(apps, brand):
    """Replay the sotuvlar of `brand` against its bronlar, oldest first.

    What `crm.models.draw_down_bron` does the moment a sotuv is saved — except these
    sotuvlar were saved while two spellings kept them and the bron apart, and a bron
    is never revisited afterwards.

    Only sales not already tied to a bron are offered, so running this twice says
    nothing twice. And only bronlar that already existed when the sotuv was entered: a
    promise made afterwards was not served by a sale that predates it.
    """
    Reservation = apps.get_model("crm", "Reservation")
    Sale = apps.get_model("crm", "Sale")
    sales = (Sale.objects
             .filter(line__contract_line__brand=brand, reservation__isnull=True)
             .order_by("created_at", "pk"))
    for sale in sales:
        remaining = sale.kg
        brons = [bron for bron in Reservation.objects
                 .filter(brand=brand, status="active", customer_id=sale.customer_id)
                 .order_by("created_at", "pk")
                 if bron.kg - bron.fulfilled_kg > 0 and bron.created_at <= sale.created_at]
        for bron in brons:
            if remaining <= 0:
                break
            take = min(bron.kg - bron.fulfilled_kg, remaining)
            bron.fulfilled_kg += take
            if bron.kg - bron.fulfilled_kg <= 0:
                bron.status = "converted"
            bron.save(update_fields=["fulfilled_kg", "status"])
            # Linked to the FIRST bron it touched, so the sotuv can say which promise
            # it went against and an edit knows where to put the kg back.
            if sale.reservation_id is None:
                sale.reservation = bron
                sale.save(update_fields=["reservation"])
            remaining -= take


def to_kiril(apps, schema_editor):
    ContractLine = apps.get_model("crm", "ContractLine")
    Reservation = apps.get_model("crm", "Reservation")

    merged = []
    for lotin, kiril in KIRIL.items():
        # A rename changes nothing about what matches what; only a name landing on one
        # that ALREADY exists joins two products, and only that needs the replay.
        joins = ContractLine.objects.filter(brand=kiril).exists()
        moved = ContractLine.objects.filter(brand=lotin).update(brand=kiril)
        moved += Reservation.objects.filter(brand=lotin).update(brand=kiril)
        if moved and joins:
            merged.append(kiril)

    for brand in merged:
        _redraw_brons(apps, brand)


class Migration(migrations.Migration):

    dependencies = [("crm", "0069_remove_customer_prefill_last_price")]

    operations = [migrations.RunPython(to_kiril, migrations.RunPython.noop)]
