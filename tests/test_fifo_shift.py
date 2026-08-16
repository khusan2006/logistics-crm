"""Correcting an old sotuv after the load has been handed out.

The lot a sotuv is billed to is FIFO's answer, not a fact the operator typed, so
correcting an early sotuv changes the answer for every sotuv behind it. These pin
the three things that can happen: the chain moves, the chain cannot move and is
shown instead, or the kg are simply not there.
"""
from decimal import Decimal

from conftest import line_data
from crm.models import (
    Contract, ContractLine, Customer, Partner, Sale, Shipment, ShipmentLine,
    ShipmentStatus,
)


def _customer(name="Alisher"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(brand, kg, price, arrived):
    """An arrived lot of `brand` at its own USD/kg. No xarajat, so the landed cost
    is exactly `price` and a shifted slice's tannarx is readable at a glance."""
    partner = Partner.objects.create(name=f"P-{brand}-{arrived}", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand,
                                kg=Decimal("100000"), price=Decimal("1.00"))
    shipment = Shipment.objects.create(contract=contract,
                                       status=ShipmentStatus.arrival(), arrived=arrived)
    return ShipmentLine.objects.create(shipment=shipment,
                                       contract_line=contract.lines.first(),
                                       kg=Decimal(kg), price=Decimal(price))


def _sell(client, brand, kg, date, customer, price="3.00"):
    """A sotuv entered by marka — the FIFO path, which picks the lots itself."""
    return client.post("/sales/new/", {
        "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
        "date": date, "debt_deadline": "", "note": "",
        **line_data({"brand": brand, "kg": kg, "price": price})})


def _edit(client, sale, kg):
    return client.post(f"/sales/{sale.pk}/edit/", {
        "customer": sale.customer_id, "line": sale.line_id, "kg": kg,
        "currency": "usd", "exchange_rate": "12000", "price": str(sale.price),
        "date": str(sale.date), "debt_deadline": "", "note": ""})


def _slices(sale):
    sale.refresh_from_db()
    return [(sl.line_id, sl.kg) for sl in sale.lots.all()]


def test_edit_up_spills_into_another_lot_of_the_same_marka(admin_client, db):
    """The block this whole thing exists to remove: the sotuv's own lot is empty,
    but the marka is sitting in the ombor on a second truck."""
    old = _lot("LDPE", "1000", "1.00", "2026-07-10")
    new = _lot("LDPE", "1000", "2.00", "2026-07-15")
    customer = _customer()
    _sell(admin_client, "LDPE", "1000", "2026-07-16", customer)
    sale = Sale.objects.get()
    assert old.available_kg == Decimal("0")          # its lot is drained

    resp = _edit(admin_client, sale, "1500")
    assert resp.status_code == 302

    assert _slices(sale) == [(old.pk, Decimal("1000")), (new.pk, Decimal("500"))]
    # 1000 kg at 1.00 + 500 kg at 2.00, weighted over 1500
    assert sale.cost_price == Decimal("1.3333")
    assert new.available_kg == Decimal("500")


def test_edit_up_shifts_the_sotuvlar_behind_it(admin_client, db):
    """A clean FIFO chain re-runs: the corrected sotuv takes more of the cheap lot,
    so the sotuv behind it is pushed onto the dearer one."""
    cheap = _lot("HDPE", "1000", "1.00", "2026-07-10")
    dear = _lot("HDPE", "1000", "2.00", "2026-07-15")
    customer = _customer()
    _sell(admin_client, "HDPE", "500", "2026-07-16", customer)
    _sell(admin_client, "HDPE", "1000", "2026-07-17", customer)
    first = Sale.objects.order_by("date", "id").first()
    behind = Sale.objects.order_by("date", "id").exclude(pk=first.pk)

    assert first.cost_price == Decimal("1.0000")
    # 500 kg off the cheap lot + 500 off the dear one
    assert sum(s.kg for s in behind) == Decimal("1000")

    resp = _edit(admin_client, first, "800")
    assert resp.status_code == 302

    first.refresh_from_db()
    assert _slices(first) == [(cheap.pk, Decimal("800"))]
    # only 200 cheap kg are left for the sotuv behind it; the rest comes off the dear lot
    moved = [sl for s in behind for sl in s.lots.all()]
    assert sum(sl.kg for sl in moved if sl.line_id == cheap.pk) == Decimal("200")
    assert sum(sl.kg for sl in moved if sl.line_id == dear.pk) == Decimal("800")


def test_hand_picked_lot_behind_it_stops_the_shift(admin_client, db):
    """A lot the operator CHOSE is not FIFO's to move, so the chain is left alone
    and only the edited sotuv is re-placed."""
    cheap = _lot("PP", "1000", "1.00", "2026-07-10")
    dear = _lot("PP", "1000", "2.00", "2026-07-15")
    customer = _customer()
    _sell(admin_client, "PP", "300", "2026-07-16", customer)
    first = Sale.objects.get()
    # Sotish from inside the dearer lot — deliberately not what FIFO would pick
    admin_client.post(f"/sales/new/?lot={dear.pk}", {
        "customer": customer.pk, "kg": "400", "currency": "usd",
        "exchange_rate": "12000", "price": "3.00", "date": "2026-07-17",
        "debt_deadline": "", "note": ""})
    chosen = Sale.objects.exclude(pk=first.pk).get()
    assert _slices(chosen) == [(dear.pk, Decimal("400"))]

    resp = _edit(admin_client, first, "600")
    assert resp.status_code == 302

    first.refresh_from_db()
    assert _slices(first) == [(cheap.pk, Decimal("600"))]
    # untouched — it is still on the lot it was pointed at
    assert _slices(chosen) == [(dear.pk, Decimal("400"))]


def test_edit_beyond_the_marka_is_refused(admin_client, db):
    """The one ceiling left is a physical one: kg that never arrived."""
    _lot("PVC", "1000", "1.00", "2026-07-10")
    customer = _customer()
    _sell(admin_client, "PVC", "1000", "2026-07-16", customer)
    sale = Sale.objects.get()

    resp = _edit(admin_client, sale, "1200")
    assert resp.status_code == 200          # re-rendered with an error, not saved
    sale.refresh_from_db()
    assert sale.kg == Decimal("1000")


def test_preview_shows_what_would_shift(admin_client, db):
    cheap = _lot("PET", "1000", "1.00", "2026-07-10")
    _lot("PET", "1000", "2.00", "2026-07-15")
    customer = _customer()
    _sell(admin_client, "PET", "500", "2026-07-16", customer)
    _sell(admin_client, "PET", "1000", "2026-07-17", customer)
    first = Sale.objects.order_by("date", "id").first()
    before = [_slices(s) for s in Sale.objects.order_by("pk")]

    html = admin_client.get(
        f"/sales/{first.pk}/shift-preview/?kg=800").content.decode()
    assert "tannarxi o'zgaradi" in html
    assert "Jami foyda kamayadi" in html or "Jami foyda ortadi" in html

    # unchanged kg has nothing to say
    same = admin_client.get(
        f"/sales/{first.pk}/shift-preview/?kg=500").content.decode()
    assert "tannarxi o'zgaradi" not in same

    # The preview runs the real replay and throws it away — nothing may survive it.
    assert [_slices(s) for s in Sale.objects.order_by("pk")] == before
    assert first.kg == Decimal("500")
    assert cheap.available_kg == Decimal("0")


def test_preview_names_the_sotuvlar_that_block_a_shift(admin_client, db):
    _lot("ABS", "1000", "1.00", "2026-07-10")
    dear = _lot("ABS", "1000", "2.00", "2026-07-15")
    customer = _customer("Bekzod")
    _sell(admin_client, "ABS", "300", "2026-07-16", customer)
    first = Sale.objects.get()
    admin_client.post(f"/sales/new/?lot={dear.pk}", {
        "customer": customer.pk, "kg": "400", "currency": "usd",
        "exchange_rate": "12000", "price": "3.00", "date": "2026-07-17",
        "debt_deadline": "", "note": ""})
    chosen = Sale.objects.exclude(pk=first.pk).get()

    html = admin_client.get(
        f"/sales/{first.pk}/shift-preview/?kg=600").content.decode()
    assert "Avtomatik siljitib bo'lmaydi" in html
    assert f"#{chosen.pk}" in html
