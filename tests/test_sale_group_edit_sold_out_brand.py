"""Editing a multi-mahsulot sotuv whose marka is now sold out.

Reported from the floor: a 15 000 kg sotuv of "2102 кампаунд" was opened for a
narx correction and the Marka box showed "2102 репак" — a different granula
altogether. Saving it would have moved the sotuv onto a marka nobody sold.

The cause is that the picker lists only markalar with kg ON THE SHELF, and this
sotuv had taken the last of its own. With no matching <option>, the browser falls
back to showing the first one in the list.
"""
from decimal import Decimal

from crm.models import Customer, Contract, ContractLine, Partner, Sale, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus


def _lot(brand, kg):
    partner = Partner.objects.create(name=f"Pars {brand}", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand,
                                kg=Decimal(kg), price=Decimal("1.00"))
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    line = ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg))
    ShipmentExpense.objects.create(shipment=shipment, amount=Decimal("100.00"),
                                   date="2026-07-16")
    return line


SOLD_OUT = "2102 кампаунд"
IN_STOCK = "2102 репак"


def _group_sale(client):
    """A two-marka sotuv that takes ALL of SOLD_OUT and part of IN_STOCK."""
    _lot(SOLD_OUT, "15000")
    _lot(IN_STOCK, "40000")
    customer = Customer.objects.create(name="Komoliddin sintafon", phone="1", address="T")
    resp = client.post("/sales/new/", {
        "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
        "date": "2026-09-06", "debt_deadline": "", "note": "",
        "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-brand": SOLD_OUT, "lines-0-kg": "15000", "lines-0-price": "1.5",
        "lines-1-brand": IN_STOCK, "lines-1-kg": "1000", "lines-1-price": "1.5",
    })
    assert resp.status_code == 302, resp.status_code
    return Sale.objects.filter(line__contract_line__brand=SOLD_OUT).first()


def test_sold_out_marka_is_still_offered_when_editing_its_sotuv(admin_client, db):
    sale = _group_sale(admin_client)
    html = admin_client.get(f"/sales/{sale.pk}/group/edit/").content.decode()

    assert f'value="{SOLD_OUT}"' in html, (
        "the sotuv's own marka is missing from the picker — the box falls back to "
        "showing whichever marka sorts first")


def test_the_sotuvs_own_marka_is_the_one_selected(admin_client, db):
    sale = _group_sale(admin_client)
    html = admin_client.get(f"/sales/{sale.pk}/group/edit/").content.decode()

    assert f'value="{SOLD_OUT}" selected' in html, (
        "the marka box does not open on the marka that was actually sold")


def test_correcting_the_narx_keeps_the_sotuv_on_its_own_marka(admin_client, db):
    """The reported edit, carried through: change 1.5 → 1.6 and save.

    This is what the display bug cost. With the sotuv's marka missing from the
    picker, the box the operator saved was showing a different granula — so a narx
    correction quietly moved 15 000 kg onto a marka nobody had sold."""
    sale = _group_sale(admin_client)
    group = Sale.objects.filter(group=sale.group)
    assert {s.line.contract_line.brand for s in group} == {SOLD_OUT, IN_STOCK}

    resp = admin_client.post(f"/sales/{sale.pk}/group/edit/", {
        "customer": sale.customer_id, "currency": "usd", "exchange_rate": "12000",
        "date": "2026-09-06", "debt_deadline": "", "note": "",
        "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "2",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-brand": SOLD_OUT, "lines-0-kg": "15000", "lines-0-price": "1.6",
        "lines-1-brand": IN_STOCK, "lines-1-kg": "1000", "lines-1-price": "1.5",
    })
    assert resp.status_code == 302, resp.status_code

    rows = Sale.objects.filter(group=sale.group)
    sold_out_kg = sum((s.kg for s in rows if s.line.contract_line.brand == SOLD_OUT),
                      Decimal("0"))
    assert sold_out_kg == Decimal("15000"), "the kg moved off the marka they were sold on"
    assert {s.price for s in rows if s.line.contract_line.brand == SOLD_OUT} == {Decimal("1.6000")}
    # and the other marka is untouched by the correction
    in_stock_kg = sum((s.kg for s in rows if s.line.contract_line.brand == IN_STOCK),
                      Decimal("0"))
    assert in_stock_kg == Decimal("1000")


def test_a_sold_out_marka_still_cannot_grow(admin_client, db):
    """Keeping the marka in the list must not turn into permission to sell more of
    it: the shelf is empty, so the only kg it may carry are the ones it already has."""
    sale = _group_sale(admin_client)
    resp = admin_client.post(f"/sales/{sale.pk}/group/edit/", {
        "customer": sale.customer_id, "currency": "usd", "exchange_rate": "12000",
        "date": "2026-09-06", "debt_deadline": "", "note": "",
        "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "2",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-brand": SOLD_OUT, "lines-0-kg": "16000", "lines-0-price": "1.5",
        "lines-1-brand": IN_STOCK, "lines-1-kg": "1000", "lines-1-price": "1.5",
    })
    assert resp.status_code == 200, "1 000 kg too many was accepted"
    assert b"oshmasligi kerak" in resp.content


def test_a_new_sotuv_is_not_offered_an_empty_marka(admin_client, db):
    """The other side of the guard: the fallback is for a row that already carries
    the marka, never for a fresh one. Nothing sold out may be picked from scratch."""
    _group_sale(admin_client)
    html = admin_client.get("/sales/new/").content.decode()

    assert f'value="{SOLD_OUT}"' not in html
    assert f'value="{IN_STOCK}"' in html
