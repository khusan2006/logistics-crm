"""A hamkor to'lov's slices are an answer to a question that keeps moving.

`allocate_supplier_payment` caps every slice at what the marka COSTS
(`expected_value`), and that figure moves whenever a truck is sent, re-priced or
deleted, or the kelishuv's own kg/narx is edited. The placement is only right for as
long as something re-runs it, so these are about the TRIGGERS rather than the
arithmetic — tests/test_supplier_allocation.py covers the split itself.

Plus the zaklad, which the model has always been able to place and the form refused
to let through.
"""
from decimal import Decimal

from conftest import make_contract, supplier_payment_rows
from crm.models import (ContractLine, SupplierPayment, own_side,
                        unspent_supplier_payment_pair)


def _placed(contract):
    """What each marka is holding, in the kelishuv's own currency."""
    contract.refresh_from_db()
    return {ln.brand: own_side(contract, ln.paid_total, ln.paid_total_uzs)
            for ln in contract.lines.all()}


def _two_marka(**kw):
    contract = make_contract(brand="209", kg="1000", price="1.00", **kw)
    ContractLine.objects.create(contract=contract, brand="7000",
                                kg=Decimal("1000"), price=Decimal("1.00"))
    return contract


# ── the zaklad: no marka named ────────────────────────────────────────────────

def test_a_tolov_naming_no_marka_is_accepted_and_split(admin_client, db):
    """The whole point of the zaklad branch. The form used to demand a marka on any
    kelishuv with more than one, so `allocate_supplier_payment`'s split-by-mashina
    path could never be reached from a screen."""
    contract = _two_marka(planned_trucks=5)
    contract.lines.filter(brand="7000").update(planned_trucks=5)

    # contract_line left out entirely — the helper omits it unless a test names one,
    # which is exactly the POST the modal sends with Mahsulot blank.
    resp = admin_client.post("/supplier-payments/new/", supplier_payment_rows(
        {"amount": "1600"}, contract=contract.pk, date="2026-07-10"))
    assert resp.status_code == 302, resp.status_code
    assert SupplierPayment.objects.count() == 1, "the zaklad was refused"
    # Five trucks each, so an even split — "5 ga 5 bo'lsa teng".
    assert _placed(contract) == {"209": Decimal("800.00"), "7000": Decimal("800.00")}


def test_a_zaklad_bigger_than_the_kelishuv_leaves_the_rest_as_avans(admin_client, db):
    """The split is capped by what each marka costs, and money no product can take
    is the hamkor's — it is not forced onto a marka to make the sum come out."""
    contract = _two_marka(planned_trucks=5)          # 1 000 + 1 000 to be had
    contract.lines.filter(brand="7000").update(planned_trucks=5)
    payment = SupplierPayment.objects.create(contract=contract, contract_line=None,
                                             amount=Decimal("2500"), date="2026-07-10")

    assert _placed(contract) == {"209": Decimal("1000.00"), "7000": Decimal("1000.00")}
    assert own_side(contract, *unspent_supplier_payment_pair(payment)) == Decimal("500.00")


def test_an_uneven_truck_plan_splits_in_that_proportion(admin_client, db):
    contract = _two_marka(planned_trucks=3)
    contract.lines.filter(brand="7000").update(planned_trucks=1)
    SupplierPayment.objects.create(contract=contract, contract_line=None,
                                   amount=Decimal("400"), date="2026-07-10")

    assert _placed(contract) == {"209": Decimal("300.00"), "7000": Decimal("100.00")}


def test_a_single_marka_kelishuv_still_fills_the_marka_in(admin_client, db):
    """One product IS the kelishuv — the operator is not asked, and the to'lov still
    records which marka it bought."""
    contract = make_contract(brand="209", kg="1000", price="1.00")
    admin_client.post("/supplier-payments/new/", supplier_payment_rows(
        {"amount": "500"}, contract=contract.pk, date="2026-07-10"))
    payment = SupplierPayment.objects.get()
    assert payment.contract_line_id == contract.lines.get().pk


# ── the slices follow the trucks ──────────────────────────────────────────────

def test_a_new_truck_pulls_back_money_that_spilled_to_the_next_marka(admin_client, db):
    """Pay 1 500 naming a marka that costs 1 000 and 500 spills to the next one.
    Send that marka a truck at a higher narx and it now costs 5 000 — the 500 was
    always its own money and has to come back, or the neighbour reads as paid for
    goods nobody bought from them."""
    contract = _two_marka()
    named = contract.lines.get(brand="209")
    SupplierPayment.objects.create(contract=contract, contract_line=named,
                                   amount=Decimal("1500"), date="2026-07-06")
    assert _placed(contract) == {"209": Decimal("1000.00"), "7000": Decimal("500.00")}

    admin_client.post(f"/shipments/{_send(admin_client, contract, named)}/", {})
    assert _placed(contract) == {"209": Decimal("1500.00"), "7000": Decimal("0")}


def _send(admin_client, contract, line):
    """A truck for one marka at 5.00/kg, through the dispatch screen — the trigger
    is on the VIEW, so creating the Shipment directly would not exercise it."""
    from crm.models import Shipment, ShipmentStatus
    resp = admin_client.post("/shipments/new/", {
        "contract": contract.pk, "status": ShipmentStatus.objects.first().pk,
        "sent": "2026-07-07", "eta": "", "arrived": "", "qr_date": "",
        "logist": "", "responsible": "", "driver_name": "", "driver_phone": "",
        "transport": "", "container": "", "note": "",
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-contract_line": line.pk, "lines-0-kg": "1000", "lines-0-price": "5.00",
    })
    assert resp.status_code in (200, 302), resp.status_code
    return Shipment.objects.latest("pk").pk


def test_a_deleted_truck_hands_back_what_the_marka_can_no_longer_cost(admin_client, db):
    """The mirror. A marka holding 5 000 because a truck made it cost that much
    cannot still hold it once the truck is gone — the excess is the hamkor's avans,
    not a settled marka."""
    contract = _two_marka()
    named = contract.lines.get(brand="209")
    pk = _send(admin_client, contract, named)
    payment = SupplierPayment.objects.create(contract=contract, contract_line=named,
                                             amount=Decimal("5000"), date="2026-07-08")
    assert _placed(contract)["209"] == Decimal("5000.00")

    admin_client.post(f"/shipments/{pk}/delete/", {})

    contract.refresh_from_db()
    named.refresh_from_db()
    held = _placed(contract)["209"]
    ceiling = own_side(contract, named.expected_value, named.expected_value_uzs)
    assert held <= ceiling, f"{held} sitting on a marka that costs {ceiling}"
    # What it can no longer hold went to the other marka and then to the avans —
    # nothing is lost either way.
    payment.refresh_from_db()
    avans = own_side(contract, *unspent_supplier_payment_pair(payment))
    assert sum(_placed(contract).values()) + avans == Decimal("5000.00")


def test_editing_the_kelishuv_replaces_the_money(admin_client, db):
    """kg and narx move what a marka costs just as a truck does."""
    contract = _two_marka()
    named = contract.lines.get(brand="209")
    other = contract.lines.get(brand="7000")
    SupplierPayment.objects.create(contract=contract, contract_line=named,
                                   amount=Decimal("1500"), date="2026-07-06")
    assert _placed(contract)["7000"] == Decimal("500.00")

    resp = admin_client.post(f"/contracts/{contract.pk}/edit/", {
        "partner": contract.partner_id, "created": "2026-07-01",
        "currency": contract.currency, "exchange_rate": "12000", "note": "",
        "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "2",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-id": named.pk, "lines-0-brand": "209",
        "lines-0-kg": "2000", "lines-0-price": "1.00", "lines-0-planned_trucks": "",
        "lines-1-id": other.pk, "lines-1-brand": "7000",
        "lines-1-kg": "1000", "lines-1-price": "1.00", "lines-1-planned_trucks": "",
    })
    assert resp.status_code in (200, 302), resp.status_code
    # 209 costs 2 000 now, so all 1 500 is its own.
    assert _placed(contract) == {"209": Decimal("1500.00"), "7000": Decimal("0")}
