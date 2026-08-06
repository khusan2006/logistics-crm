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


# ── Qaysi qarz: a mijoz who owes in both currencies at once ──────────────────
#
# Two debts, not one. Before the picker the money went oldest-first across both, so
# an operator collecting the dollar qarz could watch it settle the so'm one instead
# — and there was nowhere on the form to say what they were actually being paid for.

def _two_currency_debtor():
    """A mijoz owing $1 000 (older) and 12 000 000 so'm (newer)."""
    from crm.models import Currency

    customer = _customer()
    lot = _lot(kg="100000")
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"),
                        price=Decimal("1.00"), date="2026-07-01")
    Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"),
                        price=Decimal("1.00"), price_uzs=Decimal("12000.00"),
                        currency=Currency.UZS, exchange_rate=Decimal("12000"),
                        date="2026-07-05")
    return customer


def test_a_tolov_aimed_at_the_dollar_qarz_leaves_the_som_one_alone(admin_client, db):
    from crm.models import customer_balance_by_currency

    customer = _two_currency_debtor()
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "400"},
        customer=customer, debt_currency="usd"))
    assert resp.status_code == 302

    assert customer_balance_by_currency(customer) == [
        ("usd", Decimal("600.00")), ("uzs", Decimal("12000000.00"))]


def test_a_tolov_aimed_at_the_som_qarz_leaves_the_dollar_one_alone(admin_client, db):
    """Note the dollar sotuv is the OLDER one — under plain FIFO this money would
    have gone there, which is exactly the behaviour the picker overrides."""
    from crm.models import customer_balance_by_currency

    customer = _two_currency_debtor()
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "5000000"},
        customer=customer, debt_currency="uzs"))

    assert customer_balance_by_currency(customer) == [
        ("usd", Decimal("1000.00")), ("uzs", Decimal("7000000.00"))]


def test_money_over_the_named_qarz_stays_an_avans_instead_of_crossing(admin_client, db):
    """The point of naming a qarz: what is left over is money we are holding, not a
    reason to go and settle the debt the operator did not mention."""
    from crm.models import customer_balance_by_currency

    customer = _two_currency_debtor()
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "1500"},          # $500 more than the $ qarz
        customer=customer, debt_currency="usd"))

    assert customer_balance_by_currency(customer) == [
        ("usd", Decimal("-500.00")), ("uzs", Decimal("12000000.00"))]


def test_the_choice_survives_the_sweep_that_runs_after_every_change(admin_client, db):
    """reconcile_customer_allocations runs after every sotuv, to'lov and qaytarish.
    Without the target on the row it would re-place this money oldest-first and undo
    the operator's choice minutes later, from an unrelated screen."""
    from crm.models import customer_balance_by_currency, reconcile_customer_allocations

    customer = _two_currency_debtor()
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "5000000"},
        customer=customer, debt_currency="uzs"))
    before = customer_balance_by_currency(customer)

    reconcile_customer_allocations(customer)
    reconcile_customer_allocations(customer)
    assert customer_balance_by_currency(customer) == before


def test_paying_a_named_qarz_in_its_own_currency_needs_no_kurs(admin_client, db):
    """The figure IS the qarz — a rate would decide nothing about it."""
    payload = payment_rows({"currency": "usd", "amount": "400", "exchange_rate": ""},
                           customer=_two_currency_debtor(), debt_currency="usd")
    assert admin_client.post("/customer-payments/new/", payload).status_code == 302
    assert CustomerPayment.objects.get().exchange_rate > 0   # inherited, not typed


def test_paying_a_named_qarz_in_the_other_currency_demands_a_kurs(admin_client, db):
    """The one case where the rate decides how much of the qarz the money clears."""
    payload = payment_rows({"currency": "uzs", "amount": "5000000", "exchange_rate": ""},
                           customer=_two_currency_debtor(), debt_currency="usd")
    assert admin_client.post("/customer-payments/new/", payload).status_code == 200
    assert not CustomerPayment.objects.exists()


def test_with_no_qarz_named_a_kurs_is_still_required(admin_client, db):
    """"Avtomatik" is not the face-value case: the money may land on either
    currency's debt, so the rate still decides something."""
    payload = payment_rows({"currency": "uzs", "amount": "5000000", "exchange_rate": ""},
                           customer=_two_currency_debtor(), debt_currency="")
    assert admin_client.post("/customer-payments/new/", payload).status_code == 200
    assert not CustomerPayment.objects.exists()


def test_with_no_qarz_named_the_money_still_goes_oldest_first(admin_client, db):
    """Backwards compatible: every to'lov written before the picker existed carries
    a blank target, and has to keep behaving the way it was booked."""
    from crm.models import customer_balance_by_currency

    customer = _two_currency_debtor()
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "400"}, customer=customer, debt_currency=""))
    # the dollar sotuv is the older one, so plain FIFO lands there
    assert customer_balance_by_currency(customer) == [
        ("usd", Decimal("600.00")), ("uzs", Decimal("12000000.00"))]


def test_the_named_qarz_is_stored_on_every_row_of_the_settlement(admin_client, db):
    """One settlement, two currencies of arrival, one qarz being collected."""
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "100"},
        {"currency": "uzs", "amount": "1200000", "exchange_rate": "12000"},
        customer=_two_currency_debtor(), debt_currency="usd"))
    rows = CustomerPayment.objects.all()
    assert rows.count() == 2
    assert {r.target_currency for r in rows} == {"usd"}


# ── crossing a currency must never overdraw the to'lov ───────────────────────

def test_a_som_tolov_on_a_dollar_sotuv_never_spends_more_som_than_it_has(admin_client, db):
    """Regression: crossing a currency rounds twice — once into the sotuv's money to
    decide what to take, once back into the to'lov's to size the slice. When both
    rounds go the same way the round trip returns MORE than was there: 5 000 000 so'm
    came back as 5 000 040, the extra 40 was booked as spent, and the overdrawn
    to'lov surfaced as a 40 so'm qarz the mijoz never ran up."""
    from crm.models import (Currency, allocate_customer_payment,
                            customer_balance_by_currency, unspent_payment_amount)

    customer = _customer()
    _sale(customer, _lot(), "1000", "1.00", "2026-07-01")        # owes $1 000
    payment = CustomerPayment.objects.create(
        customer=customer, date="2026-07-20", currency=Currency.UZS,
        amount=Decimal("416.67"), amount_uzs=Decimal("5000000"),
        exchange_rate=Decimal("12000"), method="cash")
    allocate_customer_payment(payment)

    placed = sum((a.amount_uzs for a in payment.allocations.all()), Decimal("0"))
    assert placed <= payment.amount_uzs, "spent more so'm than the mijoz handed over"
    assert unspent_payment_amount(payment) >= 0, "the to'lov is overdrawn"
    # and no so'm qarz is conjured out of the rounding
    assert [amount for currency, amount in customer_balance_by_currency(customer)
            if currency == Currency.UZS and amount > 0] == []


def test_what_a_crossing_tolov_could_not_spend_stays_an_avans(admin_client, db):
    """The other side of the cap: money over the qarz is money we are HOLDING, so it
    waits as an avans rather than being forced onto something."""
    from crm.models import (Currency, allocate_customer_payment,
                            customer_balance_by_currency, unspent_payment_amount)

    customer = _customer()
    _sale(customer, _lot(), "1000", "1.00", "2026-07-01")        # owes $1 000
    payment = CustomerPayment.objects.create(
        customer=customer, date="2026-07-20", currency=Currency.UZS,
        amount=Decimal("2500"), amount_uzs=Decimal("30000000"),
        exchange_rate=Decimal("12000"), method="cash")
    allocate_customer_payment(payment)

    # $1 000 of qarz at 12 000 is 12 000 000 so'm; the rest is still the mijoz's
    assert sum((a.amount_uzs for a in payment.allocations.all()),
               Decimal("0")) == Decimal("12000000.00")
    assert unspent_payment_amount(payment) == Decimal("18000000.00")
    assert customer_balance_by_currency(customer) == [
        (Currency.UZS, Decimal("-18000000.00"))]
