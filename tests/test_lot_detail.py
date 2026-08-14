"""The ombor hides what is finished; a lot's own page says where its kg went."""
from decimal import Decimal

from conftest import line_data
from crm.models import (
    Contract, ContractLine, Customer, Partner, Sale, Shipment, ShipmentLine,
    ShipmentStatus,
)


def _customer(name="Alisher"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(brand, kg, price, arrived, partner=None):
    p = Partner.objects.create(name=partner or f"Hamkor{Partner.objects.count() + 1}",
                               phone="1", city="T")
    c = Contract.objects.create(partner=p, created="2026-07-01")
    ContractLine.objects.create(contract=c, brand=brand, kg=Decimal("100000"),
                                price=Decimal("1.00"))
    ship = Shipment.objects.create(contract=c, status=ShipmentStatus.arrival(),
                                   arrived=arrived, container="MSCU-1")
    return ShipmentLine.objects.create(shipment=ship, contract_line=c.lines.first(),
                                       kg=Decimal(kg), price=Decimal(price))


def _sell(client, brand, kg, date, customer, price="3.00"):
    return client.post("/sales/new/", {
        "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
        "date": date, "debt_deadline": "", "note": "",
        **line_data({"brand": brand, "kg": kg, "price": price})})


def test_finished_lots_are_folded_away_but_still_listed(admin_client, db):
    done = _lot("LDPE", "1000", "1.00", "2026-07-10")
    open_ = _lot("LDPE", "1000", "2.00", "2026-07-15")
    customer = _customer()
    _sell(admin_client, "LDPE", "1000", "2026-07-16", customer)
    assert done.available_kg == Decimal("0")

    html = admin_client.get("/ombor/").content.decode()
    # both rows are rendered — the finished one carries the marker that hides it
    assert f'href="/ombor/lot/{done.pk}/"' in html
    assert f'href="/ombor/lot/{open_.pk}/"' in html
    assert 'class="lot-done"' in html
    assert "1 ta tugagan lotni ko'rsatish" in html
    # only the lot with kg left offers a sale
    assert f'sales/new/?lot={open_.pk}' in html
    assert f'sales/new/?lot={done.pk}' not in html


def test_all_lots_open_means_no_toggle(admin_client, db):
    _lot("HDPE", "1000", "1.00", "2026-07-10")
    html = admin_client.get("/ombor/").content.decode()
    assert "tugagan lotni ko'rsatish" not in html
    assert 'class="lot-done"' not in html


def test_lot_page_lists_every_hand_over(admin_client, db):
    lot = _lot("PP", "1000", "1.00", "2026-07-10")
    ali, vali = _customer("Ali"), _customer("Vali")
    _sell(admin_client, "PP", "400", "2026-07-16", ali, price="3.00")
    _sell(admin_client, "PP", "600", "2026-07-17", vali, price="3.00")

    html = admin_client.get(f"/ombor/lot/{lot.pk}/").content.decode()
    assert "Ali" in html and "Vali" in html
    assert "2 ta sotuv" in html and "2 ta mijoz" in html
    # the lot is named by its kelishuv and truck, not by its row id alone
    assert lot.label in html
    assert "MSCU-1" in html


def test_lot_page_shows_only_this_lots_share_of_a_split_sale(admin_client, db):
    """A sotuv reaching across two trucks must not print its whole kg — or its whole
    value — against each of them, and should say where the rest came from."""
    old = _lot("PVC", "1000", "1.00", "2026-07-10")
    new = _lot("PVC", "1000", "2.00", "2026-07-15")
    customer = _customer("Bek")
    _sell(admin_client, "PVC", "1400", "2026-07-16", customer, price="3.00")
    assert Sale.objects.count() == 2          # FIFO split it across both lots

    html = admin_client.get(f"/ombor/lot/{new.pk}/").content.decode()
    assert "400" in html                      # this lot's share, not 1 400
    assert "1 ta sotuv" in html
    # and the other truck is named so the row does not read as a short delivery
    assert old.label in html


def test_skladchi_sees_the_lot_page_without_money(skladchi_client, db):
    lot = _lot("PET", "1000", "1.00", "2026-07-10")
    resp = skladchi_client.get(f"/ombor/lot/{lot.pk}/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert lot.label in html
    assert "Tan narx" not in html


def test_translator_cannot_open_the_lot_page(translator_client, db):
    lot = _lot("ABS", "1000", "1.00", "2026-07-10")
    assert translator_client.get(f"/ombor/lot/{lot.pk}/").status_code == 403


def test_marka_title_opens_its_own_page(admin_client, db):
    """The ombor row still expands inline; the marka NAME is a link out of it."""
    _lot("LLDPE", "1000", "1.00", "2026-07-10")
    html = admin_client.get("/ombor/").content.decode()
    assert 'href="/ombor/marka/LLDPE/"' in html
    # the ▸ expander is untouched, so the quick inline look still works
    assert 'class="leg-expand"' in html


def test_marka_page_gathers_deals_lots_sales_and_brons(admin_client, db):
    old = _lot("PS", "1000", "1.00", "2026-07-10", partner="Pars")
    new = _lot("PS", "1000", "2.00", "2026-07-15", partner="Basir")
    ali, vali = _customer("Ali"), _customer("Vali")
    _sell(admin_client, "PS", "1000", "2026-07-16", ali, price="3.00")
    _sell(admin_client, "PS", "400", "2026-07-17", vali, price="3.00")

    html = admin_client.get("/ombor/marka/PS/").content.decode()
    assert "Kelishuvlar bo'yicha" in html
    # both kelishuvlar, both trucks, both mijoz, and the leftover
    assert old.contract_line.contract.code in html
    assert new.contract_line.contract.code in html
    assert "Ali" in html and "Vali" in html
    assert "2 mijoz" in html
    assert f'href="/ombor/lot/{new.pk}/"' in html
    # the drained lot is folded away but still rendered
    assert 'class="lot-done"' in html


def test_marka_page_is_404_for_an_unknown_marka(admin_client, db):
    _lot("PA", "1000", "1.00", "2026-07-10")
    assert admin_client.get("/ombor/marka/YOQ/").status_code == 404


def test_skladchi_marka_page_hides_money(skladchi_client, db):
    _lot("PC", "1000", "1.00", "2026-07-10")
    html = skladchi_client.get("/ombor/marka/PC/").content.decode()
    assert "Kelishuvlar bo'yicha" in html
    assert "Foyda" not in html and "Tushum" not in html
