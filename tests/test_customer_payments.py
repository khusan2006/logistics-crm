from decimal import Decimal

from conftest import payment_rows
from crm.templatetags.crm_extras import NBSP

from crm.models import (
    Contract, ContractLine, Customer, CustomerPayment, Partner, PaymentAllocation, Sale, Shipment, ShipmentLine, ShipmentStatus,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="10000", brand="LLDPE", contract_price="1.00"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal(contract_price))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal(kg))
    return _ship_obj_line


def _sale(customer, lot, kg, price, date):
    return Sale.objects.create(
        customer=customer, line=lot, kg=Decimal(kg), price=Decimal(price),
        date=date,
    )


def test_uzs_converted_to_usd(admin_client, db):
    customer = _customer()
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "1265000", "exchange_rate": "12650"},
        customer=customer))
    p = CustomerPayment.objects.get()
    assert p.amount == Decimal("100.00")
    assert p.amount_uzs == Decimal("1265000")
    assert p.exchange_rate == Decimal("12650")


def test_usd_payment_also_records_a_som_value(admin_client, db):
    """A dollar to'lov carries its so'm twin. This used to pass with a blank kurs,
    which is precisely why old dollar rows could never appear in a so'm total."""
    customer = _customer()
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "400", "method": "transfer"}, customer=customer))
    assert resp.status_code == 302
    p = CustomerPayment.objects.get(customer=customer)
    assert p.amount == Decimal("400.00")
    assert p.amount_uzs == Decimal("4800000")


def test_payment_fifo_allocates_across_customer_sales(admin_client, db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    s2 = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "4000"}, customer=customer))
    assert resp.status_code == 302
    s1.refresh_from_db()
    s2.refresh_from_db()
    assert s1.remaining == Decimal("0")
    assert s2.remaining == Decimal("1000.00")


def test_manual_pick_via_view(admin_client, db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    s2 = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    resp = admin_client.post("/customer-payments/new/", {
        **payment_rows({"amount": "2000"}, customer=customer),
        f"alloc_{s2.pk}": "2000",
    })
    assert resp.status_code == 302
    s1.refresh_from_db()
    s2.refresh_from_db()
    assert s1.remaining == Decimal("3000.00")
    assert s2.remaining == Decimal("0")


def _som_sale(customer, lot, kg, price_uzs, rate, date):
    """A sotuv agreed in so'm — the mijoz and the operator only ever spoke in so'm,
    and the dollar column is the derived side."""
    return Sale.objects.create(
        customer=customer, line=lot, kg=Decimal(kg),
        price=Decimal(price_uzs) / Decimal(rate), price_uzs=Decimal(price_uzs),
        currency="uzs", exchange_rate=Decimal(rate), date=date)


def test_a_som_sotuv_is_settled_by_typing_som(admin_client, db):
    """The Taqsimlash box takes the currency the sotuv was agreed in. 1000 kg at
    12 000 so'm/kg is 12 000 000 so'm; typing that clears the sotuv outright."""
    customer = _customer()
    lot = _lot()
    sale = _som_sale(customer, lot, "1000", "12000", "12000", "2026-07-17")
    assert sale.remaining == Decimal("1000.00")          # $1,000 on the stored side

    resp = admin_client.post("/customer-payments/new/", {
        **payment_rows({"amount": "1000"}, customer=customer),
        f"alloc_{sale.pk}": "12000000",
    })
    assert resp.status_code == 302
    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")


def test_a_som_figure_is_not_taken_for_dollars(admin_client, db):
    """The guard this replaces: 12 000 000 read as dollars would allocate the whole
    to'lov to one sotuv and starve the rest. Here it settles exactly one."""
    customer = _customer()
    lot = _lot()
    som_sale = _som_sale(customer, lot, "500", "12000", "12000", "2026-07-17")
    usd_sale = _sale(customer, lot, "2000", "1.00", "2026-07-18")

    admin_client.post("/customer-payments/new/", {
        **payment_rows({"amount": "2000"}, customer=customer),
        f"alloc_{som_sale.pk}": "6000000",               # = $500 at this sotuv's kurs
    })
    som_sale.refresh_from_db()
    usd_sale.refresh_from_db()
    assert som_sale.remaining == Decimal("0")
    assert usd_sale.remaining == Decimal("500.00")       # the $1,500 left over, FIFO'd


def test_a_dollar_sotuv_still_takes_dollars(admin_client, db):
    """The sotuv's own currency decides — a dollar sotuv is unaffected by the
    so'm one sitting next to it in the same table."""
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    admin_client.post("/customer-payments/new/", {
        **payment_rows({"amount": "2000"}, customer=customer),
        f"alloc_{sale.pk}": "2000",
    })
    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")


def test_the_alloc_box_is_labelled_in_the_sotuv_currency(admin_client, db):
    customer = _customer()
    lot = _lot()
    _som_sale(customer, lot, "1000", "12000", "12000", "2026-07-17")
    html = admin_client.get(f"/customer-payments/new/?customer={customer.pk}").content.decode()
    assert 'placeholder="so\'m"' in html
    assert f"12{NBSP}000{NBSP}000 so&#x27;m" in html      # the qoldiq beside it


def test_create_preselects_customer_and_shows_alloc_table(admin_client, db):
    customer = _customer()
    lot = _lot()
    _sale(customer, lot, "500", "1.00", "2026-07-17")
    resp = admin_client.get(f"/customer-payments/new/?customer={customer.pk}")
    assert resp.status_code == 200
    assert resp.context["form"].initial.get("customer") == str(customer.pk)
    html = resp.content.decode()
    assert "alloc_" in html


def test_create_without_customer_has_no_alloc_table(admin_client, db):
    resp = admin_client.get("/customer-payments/new/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "alloc_" not in html


def test_create_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/customer-payments/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_modal_post_valid_returns_204_with_redirect(admin_client, db):
    customer = _customer()
    resp = admin_client.post(
        "/customer-payments/new/",
        payment_rows({"amount": "400", "method": "transfer"}, customer=customer),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/customer-payments/"
    assert CustomerPayment.objects.filter(customer=customer).exists()


def test_create_modal_post_invalid_returns_422(admin_client, db):
    resp = admin_client.post(
        "/customer-payments/new/",
        payment_rows({"amount": "400", "method": "transfer"}, customer=""),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html
    assert not CustomerPayment.objects.exists()


def test_edit_reallocates_after_amount_change(admin_client, db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1000"}, customer=customer))
    payment = CustomerPayment.objects.get()
    s1.refresh_from_db()
    assert s1.remaining == Decimal("2000.00")

    resp = admin_client.post(f"/customer-payments/{payment.pk}/edit/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd", "amount": "3000",
        "exchange_rate": "12000", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    s1.refresh_from_db()
    assert s1.remaining == Decimal("0")
    payment.refresh_from_db()
    assert payment.amount == Decimal("3000.00")


def test_delete_removes_allocations(admin_client, db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1000"}, customer=customer))
    payment = CustomerPayment.objects.get()
    resp = admin_client.post(f"/customer-payments/{payment.pk}/delete/")
    assert resp.status_code == 302
    assert not CustomerPayment.objects.filter(pk=payment.pk).exists()
    assert not PaymentAllocation.objects.filter(sale=s1).exists()
    s1.refresh_from_db()
    assert s1.remaining == Decimal("3000.00")


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/customer-payments/").status_code == 403
    assert translator_client.get("/customer-payments/new/").status_code == 403


def test_list_shows_payment(admin_client, db):
    customer = _customer()
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "400", "method": "transfer"}, customer=customer))
    html = admin_client.get("/customer-payments/").content.decode()
    assert customer.name in html


def test_customer_list_has_payment_action(admin_client, db):
    customer = _customer()
    html = admin_client.get("/customers/").content.decode()
    assert f"/customer-payments/new/?customer={customer.pk}" in html


def test_customer_options_show_remaining_debt(admin_client, db):
    """The to'lov modal names each mijoz's ostatka, so the operator does not have to
    read it off the Qarzlar screen first."""
    customer = _customer()
    lot = _lot()
    _sale(customer, lot, "3000", "1.00", "2026-07-17")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1000"}, customer=customer))
    html = admin_client.get("/customer-payments/new/").content.decode()
    assert f"{customer.name} · qarz $2{NBSP}000" in html


def test_customer_options_show_advance_and_settled(admin_client, db):
    paid_up = _customer("Qarzsiz mijoz")
    overpaid = _customer("Avansli mijoz")
    lot = _lot()
    _sale(overpaid, lot, "1000", "1.00", "2026-07-17")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1500"}, customer=overpaid))
    html = admin_client.get("/customer-payments/new/").content.decode()
    assert f"{overpaid.name} · avans $500" in html
    assert f"{paid_up.name} · qarzsiz" in html


def test_one_settlement_in_two_currencies(admin_client, db):
    """The case the modal exists for: a 10 000$ qarz cleared with 5 000$ naqd and the
    rest handed over in so'm. Two rows, one settlement — the qarz closes."""
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "10000", "1.00", "2026-07-17")
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "5000", "exchange_rate": "12000"},
        {"currency": "uzs", "amount": "60000000", "exchange_rate": "12000"},
        customer=customer))
    assert resp.status_code == 302
    usd_row, uzs_row = CustomerPayment.objects.order_by("id")
    assert usd_row.amount == Decimal("5000.00")
    assert uzs_row.amount == Decimal("5000.00")
    assert uzs_row.amount_uzs == Decimal("60000000")
    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")
    assert customer.balance == Decimal("0")


def test_rows_keep_their_own_method_and_kurs(admin_client, db):
    """Each row is its own arrival: naqd against perechisleniya, at its own kurs. The
    foiz is charged on the transfer alone — that is why they are separate rows."""
    customer = _customer()
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1000", "method": "cash", "exchange_rate": "12000"},
        {"amount": "1000", "method": "transfer", "fee_percent": "2", "exchange_rate": "12650"},
        customer=customer))
    cash, transfer = CustomerPayment.objects.order_by("id")
    assert cash.net_amount == Decimal("1000.00")
    assert cash.exchange_rate == Decimal("12000")
    assert transfer.net_amount == Decimal("980.00")
    assert transfer.exchange_rate == Decimal("12650")
    assert customer.balance == Decimal("-1980.00")   # avans, net of the bank's cut


def test_manual_pick_is_filled_across_rows(admin_client, db):
    """One pick list for the whole settlement: the first row fills the chosen sotuv
    as far as it reaches, the second carries on from there."""
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    s2 = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    resp = admin_client.post("/customer-payments/new/", {
        **payment_rows({"amount": "1200"}, {"amount": "800"}, customer=customer),
        f"alloc_{s2.pk}": "2000",
    })
    assert resp.status_code == 302
    s1.refresh_from_db()
    s2.refresh_from_db()
    assert s2.remaining == Decimal("0")              # the picked sotuv, paid by both rows
    assert s1.remaining == Decimal("3000.00")        # never touched


def test_at_least_one_row_is_required(admin_client, db):
    customer = _customer()
    resp = admin_client.post(
        "/customer-payments/new/", payment_rows(customer=customer),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 422
    assert "Kamida bitta to&#x27;lov kiritilishi kerak" in resp.content.decode()
    assert not CustomerPayment.objects.exists()


def test_modal_offers_an_add_row_button(admin_client, db):
    html = admin_client.get(
        "/customer-payments/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    assert "data-line-add" in html
    assert "+ To&#x27;lov qo&#x27;shish" in html
