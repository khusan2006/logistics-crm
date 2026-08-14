"""A sotuv's kg read as a path, not as a single current figure.

"16 950 kg" says nothing about the 24 000 it was entered as, and a correction made
months ago is only understandable in order. These pin that the trail reads end to
end, including the parts written against a sibling row.
"""
from decimal import Decimal

from conftest import line_data
from crm.models import (
    AuditLog, Contract, ContractLine, Customer, Partner, Sale, Shipment,
    ShipmentLine, ShipmentStatus,
)


def _customer(name="Alisher"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(brand, kg, price, arrived):
    p = Partner.objects.create(name=f"Hamkor{Partner.objects.count() + 1}",
                               phone="1", city="T")
    c = Contract.objects.create(partner=p, created="2026-07-01")
    ContractLine.objects.create(contract=c, brand=brand, kg=Decimal("100000"),
                                price=Decimal("1.00"))
    ship = Shipment.objects.create(contract=c, status=ShipmentStatus.arrival(),
                                   arrived=arrived)
    return ShipmentLine.objects.create(shipment=ship, contract_line=c.lines.first(),
                                       kg=Decimal(kg), price=Decimal(price))


def _sell(client, brand, kg, date, customer, price="3.00"):
    return client.post("/sales/new/", {
        "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
        "date": date, "debt_deadline": "", "note": "",
        **line_data({"brand": brand, "kg": kg, "price": price})})


def _edit(client, sale, kg):
    return client.post(f"/sales/{sale.pk}/edit/", {
        "customer": sale.customer_id, "line": sale.line_id, "kg": kg,
        "currency": "usd", "exchange_rate": "12000", "price": str(sale.price),
        "date": str(sale.date), "debt_deadline": "", "note": ""})


def test_an_edit_records_both_sides(admin_client, db):
    """Without the before, a correction months later is unreadable — which is how a
    24 000 kg sotuv became 16 950 with nothing on the record to say so."""
    _lot("LDPE", "5000", "1.00", "2026-07-10")
    customer = _customer()
    _sell(admin_client, "LDPE", "2000", "2026-07-16", customer)
    sale = Sale.objects.get()

    _edit(admin_client, sale, "1500")
    entry = AuditLog.objects.filter(target_type="Sotuv", action="update").first()
    assert "2000" in entry.summary and "1500" in entry.summary
    assert "→" in entry.summary


def test_history_reads_as_a_path_oldest_first(admin_client, db):
    _lot("HDPE", "5000", "1.00", "2026-07-10")
    customer = _customer()
    _sell(admin_client, "HDPE", "2000", "2026-07-16", customer)
    sale = Sale.objects.get()
    _edit(admin_client, sale, "1500")
    _edit(admin_client, sale, "1800")

    from crm.views import sale_history
    trail = sale_history(sale)
    assert [h["action"] for h in trail] == ["Qo'shildi", "O'zgartirildi", "O'zgartirildi"]
    assert trail[0]["after"] == Decimal("2000")
    assert (trail[1]["before"], trail[1]["after"]) == (Decimal("2000"), Decimal("1500"))
    assert (trail[2]["before"], trail[2]["after"]) == (Decimal("1500"), Decimal("1800"))

    html = admin_client.get(f"/sales/{sale.pk}/").content.decode()
    assert "Tarix" in html


def test_a_split_sotuv_still_shows_the_trip_it_came_from(admin_client, db):
    """FIFO writes ONE audit line per hand-over, against whichever row was created
    first. The other rows must not read as having appeared from nowhere."""
    _lot("PP", "1000", "1.00", "2026-07-10")
    _lot("PP", "1000", "2.00", "2026-07-15")
    customer = _customer()
    _sell(admin_client, "PP", "1400", "2026-07-16", customer)
    first, second = Sale.objects.order_by("pk")

    from crm.views import sale_history
    for sale in (first, second):
        trail = sale_history(sale)
        assert trail, f"#{sale.pk} tarixsiz qoldi"
        assert trail[0]["after"] == Decimal("1400")     # the whole trip, not the slice
        assert trail[0]["whole_trip"] is True


def test_history_survives_a_sotuv_with_no_group(admin_client, db):
    """A lot-picked sotuv is one row by nature and carries no group — its own trail
    is still the trail."""
    lot = _lot("PVC", "1000", "1.00", "2026-07-10")
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "kg": "400", "currency": "usd",
        "exchange_rate": "12000", "price": "3.00", "date": "2026-07-16",
        "debt_deadline": "", "note": ""})
    sale = Sale.objects.get()
    assert sale.group is None

    from crm.views import sale_history
    trail = sale_history(sale)
    assert len(trail) == 1 and trail[0]["after"] == Decimal("400")
    # one row, one lot — nothing about it is "the whole trip"
    assert trail[0]["whole_trip"] is False


def test_a_multi_marka_trip_reports_only_this_markas_kg(admin_client, db):
    """One trip can carry three products. Reading the first and last kg off that line
    turns a single purchase into a change from one marka's kg to another's."""
    _lot("PA6", "5000", "1.00", "2026-07-10")
    _lot("PA66", "5000", "1.00", "2026-07-10")
    customer = _customer()
    admin_client.post("/sales/new/", {
        "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
        "date": "2026-07-16", "debt_deadline": "", "note": "",
        **line_data({"brand": "PA6", "kg": "990", "price": "3.00"},
                    {"brand": "PA66", "kg": "4000", "price": "3.00"})})

    from crm.views import sale_history
    small = Sale.objects.get(line__contract_line__brand="PA6")
    trail = sale_history(small)
    assert trail[0]["after"] == Decimal("990")     # not 4 000, and not a change
    assert trail[0]["before"] is None


def test_a_deletion_states_what_went_not_a_movement(admin_client, db):
    """A delete says what was removed. An arrow there invents a change."""
    _lot("POM", "5000", "1.00", "2026-07-10")
    customer = _customer()
    _sell(admin_client, "POM", "2000", "2026-07-16", customer)
    sale = Sale.objects.get()
    admin_client.post(f"/sales/{sale.pk}/delete/")

    from crm.views import brand_activity
    events = brand_activity("POM", [], [])
    gone = [e for e in events if e["is_delete"]]
    assert gone, "o'chirish yozuvi topilmadi"
    assert gone[0]["before"] is None
    assert gone[0]["after"] == Decimal("2000")
