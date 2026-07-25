from decimal import Decimal

from crm.models import (
    AuditLog, Contract, ContractLine, Customer, Partner, Sale, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus, SupplierPayment,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="10000", brand="LLDPE", contract_price="1.00", expense="2000.00"):
    """An arrived 10,000 kg lot @ contract price $1.00/kg + $2,000 expenses
    => landed cost = 1.00 + 2000/10000 = $1.20/kg."""
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal(contract_price))
    shipment = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    shipment_line = ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg))
    if expense:
        ShipmentExpense.objects.create(shipment=shipment, amount=Decimal(expense), date="2026-07-16")
    return shipment_line


def _non_arrived_lot(kg="1000", brand="HDPE"):
    partner = Partner.objects.create(name="Basir", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal("1.00"))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.first(), sent="2026-07-05", eta="2026-08-01")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal(kg))
    return _ship_obj_line


def test_sale_snapshots_cost_and_computes_total_profit(admin_client, db):
    lot = _lot()
    assert lot.landed_cost_per_kg == Decimal("1.2000")
    customer = _customer()

    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "4000",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale = Sale.objects.get(line=lot)
    assert sale.kg == Decimal("4000")
    assert sale.cost_price == Decimal("1.2000")
    assert sale.total == Decimal("6400.00")
    assert sale.profit == Decimal("1600.00")
    assert AuditLog.objects.filter(target_type="Sotuv").exists()

    lot.refresh_from_db()
    assert lot.available_kg == Decimal("6000")

    customer.refresh_from_db()
    assert customer.balance == Decimal("6400.00")


def test_expense_added_after_sale_now_changes_profit(admin_client, db):
    """Fully-dynamic tannarx: a freight expense added AFTER the sale re-prices the
    load, so the already-recorded sale's cost and profit move with it. (This is the
    deliberate reversal of the old frozen-cost behaviour.)"""
    lot = _lot(expense="2000.00")                        # 1.00 + 2000/10000 = 1.20
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "4000",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(line=lot)
    assert sale.cost_price == Decimal("1.2000")
    assert sale.profit == Decimal("1600.00")             # (1.60 - 1.20) * 4000

    # 8 000 more freight → 10 000/10 000 = 1.00 → tannarx 2.00, and the sale follows.
    ShipmentExpense.objects.create(shipment=lot.shipment, amount=Decimal("8000.00"), date="2026-07-19")
    sale.refresh_from_db()
    assert sale.cost_price == Decimal("2.0000")
    assert sale.profit == Decimal("-1600.00")            # (1.60 - 2.00) * 4000


def test_vositachi_commission_adds_to_tannarx(db):
    """The middleman's cut on hamkor payments rides on top of tannarx: kelishuv
    commission ÷ the kelishuv's whole agreed kg, added per kg to every load."""
    lot = _lot(kg="10000", brand="LLDPE", contract_price="1.00", expense="2000.00")
    assert lot.landed_cost_per_kg == Decimal("1.2000")   # goods 1.00 + freight 0.20
    contract = lot.contract_line.contract
    SupplierPayment.objects.create(
        contract=contract, date="2026-07-12", amount=Decimal("10000"),
        commission_percent=Decimal("2"), method="cash")  # 200 cut ÷ 10 000 kg = 0.02
    assert contract.commission_per_kg == Decimal("0.0200")
    assert lot.landed_cost_per_kg == Decimal("1.2200")


def test_commission_per_kg_divides_by_whole_kelishuv_kg(db):
    """Commission spreads over the kelishuv's whole agreed kg, not one truck's."""
    partner = Partner.objects.create(name="Multi", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="A", kg=Decimal("10000"), price=Decimal("1"))
    ContractLine.objects.create(contract=contract, brand="B", kg=Decimal("20000"), price=Decimal("1"))
    SupplierPayment.objects.create(
        contract=contract, date="2026-07-10", amount=Decimal("15000"),
        commission_percent=Decimal("2"), method="cash")  # 300 cut ÷ 30 000 kg
    assert contract.commission_per_kg == Decimal("0.0100")


def test_commission_after_sale_lowers_that_sales_profit(admin_client, db):
    """Commission is retroactive: paying the hamkor after a sale lowers that sale's
    profit — money fully reflects in COGS, no timing leak."""
    lot = _lot(kg="10000", brand="HDPE", contract_price="1.00", expense="")   # tannarx 1.00
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "4000",
        "price": "1.50", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(line=lot)
    assert sale.profit == Decimal("2000.00")             # (1.50 - 1.00) * 4000
    SupplierPayment.objects.create(
        contract=lot.contract_line.contract, date="2026-07-19", amount=Decimal("10000"),
        commission_percent=Decimal("2"), method="cash")  # +0.02/kg → cost 1.02
    sale.refresh_from_db()
    assert sale.cost_price == Decimal("1.0200")
    assert sale.profit == Decimal("1920.00")             # (1.50 - 1.02) * 4000


def test_selling_more_than_available_kg_rejected(admin_client, db):
    lot = _lot(kg="1000", expense="0")
    customer = _customer()
    resp = admin_client.post("/sales/new/", {
        "customer": customer.pk, "brand": lot.brand, "kg": "1500",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 200
    assert not Sale.objects.filter(line=lot).exists()


def test_selling_from_non_arrived_shipment_rejected(admin_client, db):
    lot = _non_arrived_lot()
    customer = _customer()
    resp = admin_client.post("/sales/new/", {
        "customer": customer.pk, "brand": lot.brand, "kg": "100",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 200
    assert not Sale.objects.filter(line=lot).exists()


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/sales/").status_code == 403
    assert translator_client.get("/sales/new/").status_code == 403


def test_list_and_search(admin_client, db):
    lot = _lot(brand="LLDPE-Findme")
    customer = _customer(name="Findable Customer")
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "500",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    html = admin_client.get("/sales/?q=Findable").content.decode()
    assert "Findable Customer" in html


def test_sale_create_modal_get_returns_partial(admin_client, db):
    lot = _lot()
    resp = admin_client.get(f"/sales/new/?lot={lot.pk}", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_sale_create_modal_post_valid_returns_204_with_redirect(admin_client, db):
    lot = _lot()
    customer = _customer()
    resp = admin_client.post(
        "/sales/new/",
        {"customer": customer.pk, "brand": lot.brand, "kg": "100",
         "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/sales/"
    assert Sale.objects.filter(line=lot).exists()


def test_sale_create_modal_post_invalid_returns_422(admin_client, db):
    lot = _lot()
    customer = _customer()
    resp = admin_client.post(
        "/sales/new/",
        {"customer": customer.pk, "brand": lot.brand, "kg": "99999",
         "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": ""},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html


def test_sale_edit_re_snapshots_cost_from_current_shipment(admin_client, db):
    lot = _lot(expense="2000.00")
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "1000",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(line=lot)
    assert sale.cost_price == Decimal("1.2000")

    ShipmentExpense.objects.create(shipment=lot.shipment, amount=Decimal("8000.00"), date="2026-07-19")
    lot.refresh_from_db()
    new_landed = lot.landed_cost_per_kg
    assert new_landed != Decimal("1.2000")

    resp = admin_client.post(f"/sales/{sale.pk}/edit/", {
        "customer": customer.pk, "line": lot.pk, "kg": "1000",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale.refresh_from_db()
    assert sale.cost_price == new_landed


def test_sale_delete(admin_client, db):
    lot = _lot()
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "500",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(line=lot)
    resp = admin_client.post(f"/sales/{sale.pk}/delete/")
    assert resp.status_code == 302
    assert not Sale.objects.filter(pk=sale.pk).exists()


def test_sale_detail(admin_client, db):
    lot = _lot()
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "500",
        "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(line=lot)
    resp = admin_client.get(f"/sales/{sale.pk}/")
    assert resp.status_code == 200


def test_ombor_sotish_button_present_for_available_lot(admin_client, db):
    lot = _lot()
    html = admin_client.get("/ombor/").content.decode()
    assert f"/sales/new/?lot={lot.pk}" in html


def _second_lot(brand="LLDPE", kg="5000", price="1.50", arrived="2026-07-20"):
    """A NEWER arrived lot of the same brand from another partner."""
    partner = Partner.objects.create(name="Arya", phone="2", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-10")
    contract_line = ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal(price))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-12", arrived=arrived, container="MSCU-2")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal(kg))
    return _ship_obj_line


def test_fifo_sale_splits_across_lots_oldest_first(admin_client, db):
    """12,000 kg of one brand over two lots (10,000 old @ cost 1.20 + 5,000 new
    @ cost 1.50): an 11,000 kg sale drains the OLD lot fully, takes 1,000 from
    the new one, and each slice snapshots its own lot's landed cost."""
    old = _lot()                       # arrived 2026-07-16, landed 1.2000
    new = _second_lot()                # arrived 2026-07-20, landed 1.5000
    customer = _customer()
    resp = admin_client.post("/sales/new/", {
        "customer": customer.pk, "brand": "LLDPE", "kg": "11000",
        "price": "1.60", "date": "2026-07-21", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    s_old = Sale.objects.get(line=old)
    s_new = Sale.objects.get(line=new)
    assert s_old.kg == Decimal("10000") and s_old.cost_price == Decimal("1.2000")
    assert s_new.kg == Decimal("1000") and s_new.cost_price == Decimal("1.5000")
    assert old.available_kg == Decimal("0") and new.available_kg == Decimal("4000")
    # the customer owes the full 11,000 kg at the one sale price
    assert customer.balance == Decimal("17600.00")


def test_fifo_sale_capped_at_brand_total(admin_client, db):
    _lot(kg="1000", expense="0")
    _second_lot(kg="500")
    customer = _customer()
    resp = admin_client.post("/sales/new/", {
        "customer": customer.pk, "brand": "LLDPE", "kg": "1501",
        "price": "1.60", "date": "2026-07-21", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 200 and not Sale.objects.exists()


def test_ombor_listed_oldest_arrival_first(admin_client, db):
    old = _lot()                        # arrived 2026-07-16
    new = _second_lot()                 # arrived 2026-07-20
    html = admin_client.get("/ombor/").content.decode()
    assert html.index(f"?lot={old.pk}") < html.index(f"?lot={new.pk}")


def _lot_at(brand, kg, price, arrived):
    """An arrived lot of `brand` carrying its own USD/kg."""
    p = Partner.objects.create(name=f"P-{price}", phone="1", city="T")
    c = Contract.objects.create(partner=p, created="2026-07-01")
    c_line = ContractLine.objects.create(
        contract=c, brand=brand, kg=Decimal("100000"), price=Decimal("1.00"))
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.arrival(), arrived=arrived)
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal(kg), price=Decimal(price))
    return _ship_obj_line


def test_sale_from_a_chosen_lot_ignores_fifo(admin_client, db):
    """Selling from inside a marka takes THAT lot, even when an older/cheaper lot
    of the same marka would normally be consumed first — that is the whole point
    of opening the item up."""
    cheap = _lot_at("2102 kampaund", "1000", "1.20", "2026-07-19")
    dear = _lot_at("2102 kampaund", "1000", "1.30", "2026-07-23")
    customer = Customer.objects.create(name="Ali")

    resp = admin_client.post(f"/sales/new/?lot={dear.pk}", {
        "lot": dear.pk, "customer": customer.pk, "kg": "300", "price": "2.00",
        "date": "2026-07-24", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sales = list(Sale.objects.all())
    assert len(sales) == 1
    assert sales[0].line_id == dear.pk                 # not the older cheap lot
    assert sales[0].cost_price == dear.landed_cost_per_kg
    assert cheap.available_kg == Decimal("1000")


def test_sale_from_a_lot_cannot_exceed_that_lot(admin_client, db):
    """The cap is the chosen lot's own qoldiq, not the marka's total stock."""
    _lot_at("2102 kampaund", "1000", "1.20", "2026-07-19")
    small = _lot_at("2102 kampaund", "100", "1.30", "2026-07-23")
    customer = Customer.objects.create(name="Ali")

    resp = admin_client.post(f"/sales/new/?lot={small.pk}", {
        "lot": small.pk, "customer": customer.pk, "kg": "300", "price": "2.00",
        "date": "2026-07-24", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 200 and not Sale.objects.exists()


def test_sale_without_a_lot_still_runs_fifo_by_brand(admin_client, db):
    """The plain Yangi sotuv path is unchanged: oldest lot first, split as needed."""
    old = _lot_at("2102 kampaund", "200", "1.20", "2026-07-19")
    new = _lot_at("2102 kampaund", "200", "1.30", "2026-07-23")
    customer = Customer.objects.create(name="Ali")

    resp = admin_client.post("/sales/new/", {
        "brand": "2102 kampaund", "customer": customer.pk, "kg": "300", "price": "2.00",
        "date": "2026-07-24", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    assert [(s.line_id, s.kg) for s in Sale.objects.order_by("id")] == [
        (old.pk, Decimal("200.000")), (new.pk, Decimal("100.000"))]


def _brand_label(brand):
    """The <option> label the sale-by-brand dropdown shows for a marka."""
    from crm.forms import SaleCreateForm
    choices = dict(SaleCreateForm().fields["brand"].choices)
    return choices[brand]


def test_sale_brand_option_is_readable_not_scientific(db):
    """24 000 kg must read as '24000', never Decimal.normalize()'s '2.4E+4'.
    The label carries no filler words — no 'mavjud', no 'tannarx'."""
    _lot(kg="24000", brand="LLDPE", contract_price="1.00", expense="")
    label = _brand_label("LLDPE")
    assert "E+" not in label and "e+" not in label.lower()
    assert "24000 kg" in label
    assert "mavjud" not in label and "tannarx" not in label


def test_sale_brand_option_shows_kelishuv_code(db):
    """marka · kelishuv kod · remaining · tannarx — the kod ties the stock to the
    kelishuv it came from, like the yuk/kelishuv dropdowns."""
    lot = _lot(kg="10000", brand="HDPE", contract_price="1.00", expense="2000.00")
    kod = lot.contract_line.contract.code
    label = _brand_label("HDPE")
    assert kod in label
    assert "10000 kg" in label
    assert "1.2 $/kg" in label
    # order is marka, then kod, then kg, then price
    assert label.index("HDPE") < label.index(kod) < label.index("10000 kg") < label.index("1.2 $/kg")


def test_sale_brand_option_tannarx_is_weighted_across_lots(db):
    """Two lots of one marka at different landed costs → blended cost.
    10 000 kg @ $1.00 (no expense) + 10 000 kg @ $1.20 => (1.00+1.20)/2 = $1.10."""
    _lot(kg="10000", brand="PP", contract_price="1.00", expense="")
    _lot(kg="10000", brand="PP", contract_price="1.00", expense="2000.00")
    label = _brand_label("PP")
    assert "20000 kg" in label
    assert "1.1 $/kg" in label


def test_reconcile_reports_old_vs_new_profit(db):
    """The reconciliation command compares each sale's frozen snapshot profit to the
    new live profit, per month, and totals the delta — read-only."""
    from crm.management.commands.reconcile_cost import monthly_reconciliation, old_profit
    lot = _lot(kg="10000", brand="LLDPE", contract_price="1.00", expense="")  # live cost 1.00
    customer = _customer()
    sale = Sale.objects.create(customer=customer, line=lot, kg=Decimal("4000"),
                               price=Decimal("1.50"), date="2026-07-10")
    # Pretend this sale was booked earlier at a frozen cost of 0.90/kg.
    Sale.objects.filter(pk=sale.pk).update(cost_price_snapshot=Decimal("0.9000"))
    sale.refresh_from_db()

    assert old_profit(sale) == Decimal("2400.00")   # (1.50 - 0.90) * 4000, as recorded
    assert sale.profit == Decimal("2000.00")        # (1.50 - 1.00) * 4000, live now

    rows, without_snapshot = monthly_reconciliation()
    assert without_snapshot == 0
    assert len(rows) == 1
    assert rows[0]["old"] == Decimal("2400.00")
    assert rows[0]["new"] == Decimal("2000.00")
    assert rows[0]["delta"] == Decimal("-400.00")


def test_reconcile_ignores_sales_without_a_snapshot(db):
    """Sales created after the switch carry no snapshot and are excluded."""
    from crm.management.commands.reconcile_cost import monthly_reconciliation
    lot = _lot(kg="10000", brand="LLDPE", contract_price="1.00", expense="")
    Sale.objects.create(customer=_customer(), line=lot, kg=Decimal("1000"),
                        price=Decimal("1.50"), date="2026-07-10")  # no snapshot
    rows, without_snapshot = monthly_reconciliation()
    assert rows == []
    assert without_snapshot == 1
