from decimal import Decimal

from conftest import line_data
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
        "customer": customer.pk, "kg": "4000",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
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
        "customer": customer.pk, "kg": "4000",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
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
        "customer": customer.pk, "kg": "4000",
        "currency": "usd", "exchange_rate": "12000", "price": "1.50",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
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
    resp = admin_client.post("/sales/new/", {"customer": customer.pk,
         "currency": "usd", "exchange_rate": "12000", "date": "2026-07-18",
         "debt_deadline": "", "note": "",
         **line_data({"brand": lot.brand, "kg": "1500", "price": "1.60"})})
    assert resp.status_code == 200
    assert not Sale.objects.filter(line=lot).exists()


def test_selling_from_non_arrived_shipment_rejected(admin_client, db):
    lot = _non_arrived_lot()
    customer = _customer()
    resp = admin_client.post("/sales/new/", {"customer": customer.pk,
         "currency": "usd", "exchange_rate": "12000", "date": "2026-07-18",
         "debt_deadline": "", "note": "",
         **line_data({"brand": lot.brand, "kg": "100", "price": "1.60"})})
    assert resp.status_code == 200
    assert not Sale.objects.filter(line=lot).exists()


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/sales/").status_code == 403
    assert translator_client.get("/sales/new/").status_code == 403


def test_list_and_search(admin_client, db):
    lot = _lot(brand="LLDPE-Findme")
    customer = _customer(name="Findable Customer")
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "kg": "500",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
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
        {"customer": customer.pk,
         "currency": "usd", "exchange_rate": "12000", "date": "2026-07-18",
         "debt_deadline": "", "note": "",
         **line_data({"brand": lot.brand, "kg": "100", "price": "1.60"})},
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
        {"customer": customer.pk,
         "currency": "usd", "exchange_rate": "12000", "date": "2026-07-18",
         "debt_deadline": "", "note": "",
         **line_data({"brand": lot.brand, "kg": "99999", "price": "1.60"})},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html


def test_sale_edit_re_snapshots_cost_from_current_shipment(admin_client, db):
    lot = _lot(expense="2000.00")
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "kg": "1000",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(line=lot)
    assert sale.cost_price == Decimal("1.2000")

    ShipmentExpense.objects.create(shipment=lot.shipment, amount=Decimal("8000.00"), date="2026-07-19")
    lot.refresh_from_db()
    new_landed = lot.landed_cost_per_kg
    assert new_landed != Decimal("1.2000")

    resp = admin_client.post(f"/sales/{sale.pk}/edit/", {
        "customer": customer.pk, "line": lot.pk, "kg": "1000",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60", "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale.refresh_from_db()
    assert sale.cost_price == new_landed


def test_sale_delete(admin_client, db):
    lot = _lot()
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "kg": "500",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
    })
    sale = Sale.objects.get(line=lot)
    resp = admin_client.post(f"/sales/{sale.pk}/delete/")
    assert resp.status_code == 302
    assert not Sale.objects.filter(pk=sale.pk).exists()


def test_sale_detail(admin_client, db):
    lot = _lot()
    customer = _customer()
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "kg": "500",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
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
    resp = admin_client.post("/sales/new/", {"customer": customer.pk,
         "currency": "usd", "exchange_rate": "12000", "date": "2026-07-21",
         "debt_deadline": "", "note": "",
         **line_data({"brand": "LLDPE", "kg": "11000", "price": "1.60"})})
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
    resp = admin_client.post("/sales/new/", {"customer": customer.pk,
         "currency": "usd", "exchange_rate": "12000", "date": "2026-07-21",
         "debt_deadline": "", "note": "",
         **line_data({"brand": "LLDPE", "kg": "1501", "price": "1.60"})})
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
        "lot": dear.pk, "customer": customer.pk, "kg": "300", "currency": "usd", "exchange_rate": "12000", "price": "2.00",
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
        "lot": small.pk, "customer": customer.pk, "kg": "300", "currency": "usd", "exchange_rate": "12000", "price": "2.00",
        "date": "2026-07-24", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 200 and not Sale.objects.exists()


def test_sale_without_a_lot_still_runs_fifo_by_brand(admin_client, db):
    """The plain Yangi sotuv path is unchanged: oldest lot first, split as needed."""
    old = _lot_at("2102 kampaund", "200", "1.20", "2026-07-19")
    new = _lot_at("2102 kampaund", "200", "1.30", "2026-07-23")
    customer = Customer.objects.create(name="Ali")

    resp = admin_client.post("/sales/new/", {
        "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
        "date": "2026-07-24", "debt_deadline": "", "note": "",
        **line_data({"brand": "2102 kampaund", "kg": "300", "price": "2.00"}),
    })
    assert resp.status_code == 302
    assert [(s.line_id, s.kg) for s in Sale.objects.order_by("id")] == [
        (old.pk, Decimal("200.000")), (new.pk, Decimal("100.000"))]


def _brand_label(brand):
    """The <option> label the sale-by-brand dropdown shows for a marka. On the row
    form now: a sotuv carries a Mahsulot row per marka."""
    from crm.forms import SaleLineForm
    choices = dict(SaleLineForm().fields["brand"].choices)
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


class TestMultipleProductsInOneSotuv:
    """One trip to the counter is one sotuv, however many markalar the mijoz took.
    Each Mahsulot row still runs FIFO on its own marka, so a row becomes as many Sale
    rows as it takes lots to fill it."""

    def _two_markas(self):
        _lot_at("LLDPE", "500", "1.00", "2026-07-10")
        _lot_at("HDPE", "400", "2.00", "2026-07-11")
        return Customer.objects.create(name="Ikki marka")

    def _post(self, client, customer, *rows, **extra):
        data = {"customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
                "date": "2026-07-24", "debt_deadline": "", "note": "",
                **line_data(*rows)}
        data.update(extra)
        return client.post("/sales/new/", data)

    def test_two_markas_become_two_sales_in_one_go(self, admin_client, db):
        customer = self._two_markas()
        resp = self._post(admin_client, customer,
                          {"brand": "LLDPE", "kg": "100", "price": "1.50"},
                          {"brand": "HDPE", "kg": "50", "price": "3.00"})
        assert resp.status_code == 302
        sold = {(s.line.brand, s.kg, s.price) for s in Sale.objects.all()}
        assert sold == {("LLDPE", Decimal("100.000"), Decimal("1.5000")),
                        ("HDPE", Decimal("50.000"), Decimal("3.0000"))}

    def test_each_marka_keeps_its_own_narx(self, admin_client, db):
        """The whole point of a narx per row — one price for the lot would quietly
        sell the dearer granula at the cheaper one's rate."""
        customer = self._two_markas()
        self._post(admin_client, customer,
                   {"brand": "LLDPE", "kg": "100", "price": "1.50"},
                   {"brand": "HDPE", "kg": "50", "price": "3.00"})
        by_brand = {s.line.brand: s for s in Sale.objects.all()}
        assert by_brand["LLDPE"].total == Decimal("150.00")
        assert by_brand["HDPE"].total == Decimal("150.00")

    def test_the_mijoz_owes_the_whole_basket(self, admin_client, db):
        customer = self._two_markas()
        self._post(admin_client, customer,
                   {"brand": "LLDPE", "kg": "100", "price": "1.50"},
                   {"brand": "HDPE", "kg": "50", "price": "3.00"})
        customer.refresh_from_db()
        assert customer.balance == Decimal("300.00")

    def test_a_row_still_splits_across_lots_fifo(self, admin_client, db):
        """Multi-product did not replace FIFO — it runs per row."""
        old = _lot_at("2102 kampaund", "200", "1.20", "2026-07-19")
        new = _lot_at("2102 kampaund", "200", "1.30", "2026-07-23")
        _lot_at("HDPE", "100", "2.00", "2026-07-11")
        customer = Customer.objects.create(name="Ali")
        resp = self._post(admin_client, customer,
                          {"brand": "2102 kampaund", "kg": "300", "price": "2.00"},
                          {"brand": "HDPE", "kg": "40", "price": "4.00"})
        assert resp.status_code == 302
        assert [(s.line_id, s.kg) for s in Sale.objects.filter(
            line__contract_line__brand="2102 kampaund").order_by("id")] == [
            (old.pk, Decimal("200.000")), (new.pk, Decimal("100.000"))]

    def test_one_row_over_stock_rejects_the_whole_sotuv(self, admin_client, db):
        """All or nothing: half a basket saved is a sotuv the operator did not make,
        and they would have no way to see which half went in."""
        customer = self._two_markas()
        resp = self._post(admin_client, customer,
                          {"brand": "LLDPE", "kg": "100", "price": "1.50"},
                          {"brand": "HDPE", "kg": "9999", "price": "3.00"})
        assert resp.status_code == 200
        assert not Sale.objects.exists()

    def test_the_same_marka_twice_is_refused(self, admin_client, db):
        """Two rows of one granula would each be checked against the whole shelf and
        pass, then take twice what is there."""
        customer = self._two_markas()
        resp = self._post(admin_client, customer,
                          {"brand": "LLDPE", "kg": "300", "price": "1.50"},
                          {"brand": "LLDPE", "kg": "300", "price": "1.50"})
        assert resp.status_code == 200
        assert not Sale.objects.exists()

    def test_a_sotuv_with_no_rows_is_refused(self, admin_client, db):
        customer = self._two_markas()
        resp = self._post(admin_client, customer)
        assert resp.status_code == 200
        assert not Sale.objects.exists()

    def test_every_row_shares_the_one_currency_and_kurs(self, admin_client, db):
        """One deal, one valyuta — a sotuv must never end up half in dollars."""
        customer = self._two_markas()
        self._post(admin_client, customer,
                   {"brand": "LLDPE", "kg": "100", "price": "12000"},
                   {"brand": "HDPE", "kg": "50", "price": "24000"},
                   currency="uzs")
        rows = list(Sale.objects.all())
        assert {s.currency for s in rows} == {"uzs"}
        assert {s.exchange_rate for s in rows} == {Decimal("12000")}
        # The so'm side is what was typed; the dollar twin is derived at that kurs.
        by_brand = {s.line.brand: s for s in rows}
        assert by_brand["LLDPE"].price_uzs == Decimal("12000.00")
        assert by_brand["LLDPE"].price == Decimal("1.0000")

    def test_the_blank_extra_row_is_not_a_product(self, admin_client, db):
        """The formset always renders one empty row for the next product; submitting
        it untouched must not fail the sotuv."""
        customer = self._two_markas()
        resp = self._post(admin_client, customer,
                          {"brand": "LLDPE", "kg": "100", "price": "1.50"},
                          {"brand": "", "kg": "", "price": ""})
        assert resp.status_code == 302
        assert Sale.objects.count() == 1


class TestSotuvlarShowsWhatWasSoldTogether:
    """The rows of one submission carry one `group`, and Sotuvlar bands them under a
    header. Without it a mijoz who took three markalar at the counter reads on the
    page as three unrelated sotuvlar made to happen to share a day."""

    def _post(self, client, customer, *rows, **extra):
        data = {"customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
                "date": "2026-07-24", "debt_deadline": "", "note": "",
                **line_data(*rows)}
        data.update(extra)
        return client.post("/sales/new/", data)

    def test_two_markas_in_one_modal_share_one_group(self, admin_client, db):
        _lot_at("LLDPE", "500", "1.00", "2026-07-10")
        _lot_at("HDPE", "400", "2.00", "2026-07-11")
        customer = Customer.objects.create(name="Ikki marka")
        self._post(admin_client, customer,
                   {"brand": "LLDPE", "kg": "100", "price": "1.50"},
                   {"brand": "HDPE", "kg": "50", "price": "3.00"})
        groups = {s.group for s in Sale.objects.all()}
        assert len(groups) == 1 and None not in groups

    def test_fifo_slices_of_one_marka_share_it_too(self, admin_client, db):
        """One marka off two lots is still ONE product the mijoz took."""
        _lot_at("LLDPE", "200", "1.20", "2026-07-19")
        _lot_at("LLDPE", "200", "1.30", "2026-07-23")
        customer = Customer.objects.create(name="Ali")
        self._post(admin_client, customer, {"brand": "LLDPE", "kg": "300", "price": "2.00"})
        rows = list(Sale.objects.all())
        assert len(rows) == 2
        assert rows[0].group is not None and rows[0].group == rows[1].group

    def test_two_separate_sotuvlar_are_not_one_group(self, admin_client, db):
        _lot_at("LLDPE", "500", "1.00", "2026-07-10")
        customer = Customer.objects.create(name="Ali")
        self._post(admin_client, customer, {"brand": "LLDPE", "kg": "100", "price": "1.50"})
        self._post(admin_client, customer, {"brand": "LLDPE", "kg": "100", "price": "1.50"})
        assert len({s.group for s in Sale.objects.all()}) == 2

    def test_a_sotuv_from_one_chosen_lot_has_no_group(self, admin_client, db):
        """Nothing to band: the ombor's Sotish sells one lot, one row."""
        lot = _lot_at("LLDPE", "500", "1.00", "2026-07-10")
        customer = Customer.objects.create(name="Ali")
        admin_client.post(f"/sales/new/?lot={lot.pk}", {
            "lot": lot.pk, "customer": customer.pk, "kg": "100", "currency": "usd",
            "exchange_rate": "12000", "price": "2.00",
            "date": "2026-07-24", "debt_deadline": "", "note": "",
        })
        assert Sale.objects.get().group is None

    def test_a_marka_split_across_lots_is_still_one_row(self, admin_client, db):
        """The case that made the page unreadable before `group`: one sotuv, two
        rows, nothing saying they were the same 300 kg."""
        _lot_at("LLDPE", "200", "1.20", "2026-07-19")
        _lot_at("LLDPE", "200", "1.30", "2026-07-23")
        customer = Customer.objects.create(name="Ali")
        self._post(admin_client, customer, {"brand": "LLDPE", "kg": "300", "price": "2.00"})
        assert Sale.objects.count() == 2
        html = admin_client.get("/sales/").content.decode()
        assert html.count("<tr") == 2                 # the header row and the sotuv
        assert "1.2 $/kg" in html and "1.3 $/kg" in html   # both lots' tannarx

    def test_the_detail_page_names_the_other_rows(self, admin_client, db):
        _lot_at("LLDPE", "500", "1.00", "2026-07-10")
        _lot_at("HDPE", "400", "2.00", "2026-07-11")
        customer = Customer.objects.create(name="Ikki marka")
        self._post(admin_client, customer,
                   {"brand": "LLDPE", "kg": "100", "price": "1.50"},
                   {"brand": "HDPE", "kg": "50", "price": "3.00"})
        one, other = Sale.objects.order_by("pk")
        html = admin_client.get(f"/sales/{one.pk}/").content.decode()
        assert "Birgalikda sotildi" in html
        assert f"/sales/{other.pk}/" in html

    def test_a_lone_sotuv_detail_has_no_such_card(self, admin_client, db):
        lot = _lot_at("LLDPE", "500", "1.00", "2026-07-10")
        customer = Customer.objects.create(name="Ali")
        admin_client.post(f"/sales/new/?lot={lot.pk}", {
            "lot": lot.pk, "customer": customer.pk, "kg": "100", "currency": "usd",
            "exchange_rate": "12000", "price": "2.00",
            "date": "2026-07-24", "debt_deadline": "", "note": "",
        })
        sale = Sale.objects.get()
        html = admin_client.get(f"/sales/{sale.pk}/").content.decode()
        assert "Birgalikda sotildi" not in html


class TestBackfillingTheSotuvlarThatCameBefore:
    """Migration 0042: the rows written before `group` existed get banded from the
    only trace the submission left — one transaction, milliseconds apart, same
    mijoz/sana/valyuta/kurs/operator."""

    def _rows(self, customer, lot, stamps):
        """Sotuvlar written at `stamps` seconds past a fixed moment. created_at is
        auto_now_add, so the clock is set afterwards the way real history has it."""
        from datetime import timedelta
        from django.utils import timezone
        base = timezone.now() - timedelta(days=1)
        made = []
        for offset in stamps:
            sale = Sale.objects.create(
                customer=customer, line=lot, kg=Decimal("10"), price=Decimal("2.0000"),
                price_uzs=Decimal("25000"), currency="usd",
                exchange_rate=Decimal("12500"), date="2026-07-24")
            Sale.objects.filter(pk=sale.pk).update(created_at=base + timedelta(seconds=offset))
            made.append(sale.pk)
        return made

    def _run(self):
        # A module name starting with a digit is not importable by name.
        from importlib import import_module
        from django.apps import apps
        import_module("crm.migrations.0042_backfill_sale_group").backfill(apps, None)

    def test_one_transaction_becomes_one_group(self, admin_client, db):
        lot = _lot_at("LLDPE", "5000", "1.00", "2026-07-10")
        customer = Customer.objects.create(name="Ali")
        a, b = self._rows(customer, lot, [0, 0.02])
        self._run()
        rows = {s.pk: s.group for s in Sale.objects.all()}
        assert rows[a] is not None and rows[a] == rows[b]

    def test_two_sotuvlar_typed_apart_stay_apart(self, admin_client, db):
        """25 seconds is somebody reopening the modal, not one submission."""
        lot = _lot_at("LLDPE", "5000", "1.00", "2026-07-10")
        customer = Customer.objects.create(name="Ali")
        a, b = self._rows(customer, lot, [0, 25])
        self._run()
        rows = {s.pk: s.group for s in Sale.objects.all()}
        assert rows[a] is None and rows[b] is None

    def test_a_lone_sotuv_is_left_alone(self, admin_client, db):
        lot = _lot_at("LLDPE", "5000", "1.00", "2026-07-10")
        customer = Customer.objects.create(name="Ali")
        (a,) = self._rows(customer, lot, [0])
        self._run()
        assert Sale.objects.get(pk=a).group is None

    def test_two_mijoz_in_the_same_instant_are_not_one_sotuv(self, admin_client, db):
        """Same millisecond, different mijoz — a batch import, or two operators."""
        lot = _lot_at("LLDPE", "5000", "1.00", "2026-07-10")
        one = Customer.objects.create(name="Ali")
        two = Customer.objects.create(name="Vali")
        a = self._rows(one, lot, [0])[0]
        b = self._rows(two, lot, [0.01])[0]
        self._run()
        rows = {s.pk: s.group for s in Sale.objects.all()}
        assert rows[a] is None and rows[b] is None

    def test_a_group_already_stamped_is_not_touched(self, admin_client, db):
        """The sale form stamps its own rows; the backfill only fills the blanks."""
        lot = _lot_at("LLDPE", "5000", "1.00", "2026-07-10")
        customer = Customer.objects.create(name="Ali")
        a, b = self._rows(customer, lot, [0, 0.02])
        from uuid import uuid4
        mine = uuid4()
        Sale.objects.filter(pk__in=[a, b]).update(group=mine)
        self._run()
        assert {s.group for s in Sale.objects.all()} == {mine}


class TestTheOneRowView:
    """A sotuv is ONE row, its mahsulotlar stacked inside the columns they belong to,
    the way Kelishuvlar already lists a kelishuv's markalar."""

    def _sotuv(self, client):
        _lot_at("LLDPE", "500", "1.00", "2026-07-10")
        _lot_at("HDPE", "400", "2.00", "2026-07-11")
        customer = Customer.objects.create(name="Ikki marka")
        client.post("/sales/new/", {
            "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
            "date": "2026-07-24", "debt_deadline": "", "note": "",
            **line_data({"brand": "LLDPE", "kg": "100", "price": "1.50"},
                        {"brand": "HDPE", "kg": "50", "price": "3.00"})})
        return customer

    def test_two_markas_land_on_one_row(self, admin_client, db):
        self._sotuv(admin_client)
        assert Sale.objects.count() == 2
        html = admin_client.get("/sales/").content.decode()
        assert html.count("<tr") == 2                 # the header row and the sotuv
        assert "LLDPE" in html and "HDPE" in html

    def test_jami_is_the_whole_sotuv(self, admin_client, db):
        """$150 + $150 printed once as $300, not twice as its halves."""
        self._sotuv(admin_client)
        html = admin_client.get("/sales/").content.decode()
        assert "$300" in html and "$150" not in html

    def test_a_merged_sotuv_offers_its_page_not_a_guessed_row(self, admin_client, db):
        """Tahrirlash acts on one lot's row, so a sotuv of several links to its page
        instead of silently picking the first of them."""
        self._sotuv(admin_client)
        first = Sale.objects.order_by("pk").first()
        html = admin_client.get("/sales/").content.decode()
        assert f"/sales/{first.pk}/" in html
        assert f"/sales/{first.pk}/edit/" not in html

    def test_a_lone_sotuv_keeps_its_own_actions(self, admin_client, db):
        _lot_at("LLDPE", "500", "1.00", "2026-07-10")
        customer = Customer.objects.create(name="Ali")
        admin_client.post("/sales/new/", {
            "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
            "date": "2026-07-24", "debt_deadline": "", "note": "",
            **line_data({"brand": "LLDPE", "kg": "100", "price": "1.50"})})
        sale = Sale.objects.get()
        html = admin_client.get("/sales/").content.decode()
        assert f"/sales/{sale.pk}/edit/" in html and f"/sales/{sale.pk}/delete/" in html

    def test_the_search_still_narrows_the_list(self, admin_client, db):
        """Rows are folded after the filter, so a search that matches one marka of a
        sotuv shows that sotuv — not every sotuv of that mijoz."""
        self._sotuv(admin_client)
        _lot_at("PVC", "500", "1.00", "2026-07-12")
        other = Customer.objects.create(name="Boshqa mijoz")
        admin_client.post("/sales/new/", {
            "customer": other.pk, "currency": "usd", "exchange_rate": "12000",
            "date": "2026-07-24", "debt_deadline": "", "note": "",
            **line_data({"brand": "PVC", "kg": "100", "price": "1.50"})})
        html = admin_client.get("/sales/?q=PVC").content.decode()
        assert "Boshqa mijoz" in html and "Ikki marka" not in html
