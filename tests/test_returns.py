"""Vazvrat: goods coming back, and where the money goes.

The rule under every test here is one sentence: a vazvrat cancels open qarz first,
and only what is left over is money the mijoz had already paid and is owed back.
The three cases an operator thinks in — unpaid, part-paid, paid — are that one rule
seen at three different debts, which is why there is no branch for them in the code.
"""
from decimal import Decimal

from conftest import payment_rows, return_rows

from crm.models import (
    AuditLog, Contract, ContractLine, Currency, Customer, Partner, PaymentAllocation,
    RefundAllocation, Return, ReturnBatch, ReturnSettlement, Sale, Shipment,
    ShipmentExpense,
    ShipmentLine, ShipmentStatus, kassa_cash_by_currency, kassa_cash_by_method,
    pending_refunds,
)
from crm.templatetags.crm_extras import usd


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _pay(settlement, amount=None, date="2026-07-25", method=None):
    """POST payload for the "To'lash" modal. The summa defaults to the whole promise,
    which is what the form itself fills in — a test only spells out a part when the
    part is the point."""
    return {"amount": str(amount if amount is not None else settlement.amount_own),
            "method": method or settlement.method, "date": date}


def _lot(kg="10000", brand="LLDPE", contract_price="1.00", expense="2000.00"):
    """An arrived 10,000 kg lot @ contract price $1.00/kg + $2,000 expenses
    => landed cost = 1.00 + 2000/10000 = $1.20/kg."""
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal(contract_price))
    shipment = Shipment.objects.create(
        contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05",
        eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    shipment_line = ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal(kg))
    if expense:
        ShipmentExpense.objects.create(shipment=shipment, amount=Decimal(expense),
                                       date="2026-07-16")
    return shipment_line


def _sale(admin_client, lot, customer, kg="4000", price="1.60", date="2026-07-18",
          currency="usd", rate="12000"):
    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": kg,
        "currency": currency, "exchange_rate": rate, "price": price,
        "date": date, "debt_deadline": "", "note": "",
    })
    return Sale.objects.get(line=lot, kg=Decimal(kg), price_uzs__gte=0, date=date)


# ── qarzga berilgan tovar: pul harakat qilmaydi ────────────────────────────────

def test_unpaid_return_only_shrinks_the_debt(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    assert sale.remaining == Decimal("6400.00")

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))
    assert resp.status_code == 302

    sale.refresh_from_db()
    assert sale.net_total == Decimal("4800.00")
    assert sale.remaining == Decimal("4800.00")      # qarzi kamaydi
    customer.refresh_from_db()
    assert customer.balance == Decimal("4800.00")
    # No money moved, so nothing was settled and nobody is owed anything.
    assert not ReturnSettlement.objects.exists()
    assert not pending_refunds()


def test_full_return_of_an_unpaid_sale_clears_the_debt(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")

    admin_client.post("/returns/new/", return_rows(
        (sale, "4000"), customer=customer, date="2026-07-19"))

    sale.refresh_from_db()
    assert sale.net_total == Decimal("0.00")
    customer.refresh_from_db()
    assert customer.balance == Decimal("0.00")
    lot.refresh_from_db()
    assert lot.available_kg == Decimal("10000")      # hammasi omborga qaytdi


def test_returned_goods_go_back_to_the_lot(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    lot.refresh_from_db()
    assert lot.available_kg == Decimal("6000")

    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    lot.refresh_from_db()
    assert lot.returned_kg == Decimal("1000")
    assert lot.available_kg == Decimal("7000")


# ── to'langan tovar: pul mijozga qaytadi ───────────────────────────────────────

def _paid_sale(admin_client, lot, customer, kg="4000", price="1.60"):
    sale = _sale(admin_client, lot, customer, kg=kg, price=price)
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": str(sale.total)}, customer=customer, date="2026-07-18"))
    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")
    return sale


def test_paid_sale_return_can_stay_as_advance(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="advance"))
    assert resp.status_code == 302

    sale.refresh_from_db()
    assert sale.net_total == Decimal("4800.00")
    assert sale.remaining == Decimal("0")            # NOT negative — trimmed
    assert PaymentAllocation.objects.filter(sale=sale).aggregate(
        s=__import__("django").db.models.Sum("amount"))["s"] == Decimal("4800.00")
    customer.refresh_from_db()
    assert customer.balance == Decimal("-1600.00")   # $1,600 avans
    # Nothing left the kassa and nobody is waiting on money.
    assert not ReturnSettlement.objects.exists()


def test_advance_from_a_return_is_spendable_on_the_next_sale(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="advance"))

    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "500",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60",
        "date": "2026-07-20", "debt_deadline": "", "note": "",
    })
    new_sale = Sale.objects.get(line=lot, kg=Decimal("500"))
    new_sale.refresh_from_db()
    assert new_sale.remaining == Decimal("0")        # avansdan yopildi
    customer.refresh_from_db()
    assert customer.balance == Decimal("-800.00")


def test_paid_sale_return_can_be_handed_back_in_cash(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    cash_before = dict(kassa_cash_by_currency())[Currency.USD]

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19",
        settle="cash", method="cash"))
    assert resp.status_code == 302

    settlement = ReturnSettlement.objects.get()
    assert settlement.route == ReturnSettlement.Route.CASH
    assert settlement.amount == Decimal("1600.00")
    assert settlement.paid_date is not None          # darrov berildi
    assert not settlement.is_pending

    # The kassa is down by exactly the refund, and the mijoz holds no avans: the
    # money is in their hand, not on our books.
    assert dict(kassa_cash_by_currency())[Currency.USD] == cash_before - Decimal("1600.00")
    customer.refresh_from_db()
    assert customer.balance == Decimal("0.00")


def test_cash_refund_is_not_spendable_on_a_later_sale(admin_client, db):
    """The double-spend guard: money handed over the counter must not also settle a
    sotuv. Without `RefundAllocation` the freed to'lov would sit in the avans pool
    and the next sotuv's sweep would quietly take it."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="cash"))

    admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "500",
        "currency": "usd", "exchange_rate": "12000", "price": "1.60",
        "date": "2026-07-20", "debt_deadline": "", "note": "",
    })
    new_sale = Sale.objects.get(line=lot, kg=Decimal("500"))
    new_sale.refresh_from_db()
    # The mijoz got their $1,600 in cash, so this sotuv is a real qarz.
    assert new_sale.remaining == Decimal("800.00")
    customer.refresh_from_db()
    assert customer.balance == Decimal("800.00")


# ── kassada pul yo'q: biz qarzdor bo'lib qolamiz ───────────────────────────────

def test_refund_can_be_promised_for_a_later_day(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    cash_before = dict(kassa_cash_by_currency())[Currency.USD]

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19",
        settle="owed", due_date="2026-07-25"))
    assert resp.status_code == 302

    settlement = ReturnSettlement.objects.get()
    assert settlement.is_pending
    assert str(settlement.due_date) == "2026-07-25"
    # Promised is not paid: the till still holds the money.
    assert dict(kassa_cash_by_currency())[Currency.USD] == cash_before
    assert [s.pk for s in pending_refunds()] == [settlement.pk]
    # …but it is no longer the mijoz's avans either — they are owed cash.
    customer.refresh_from_db()
    assert customer.balance == Decimal("0.00")


def test_promised_refund_needs_a_day(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="owed"))
    assert resp.status_code == 200                   # re-rendered with the error
    assert not ReturnBatch.objects.exists()


def test_paying_a_promised_refund_moves_the_kassa(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19",
        settle="owed", due_date="2026-07-25"))
    settlement = ReturnSettlement.objects.get()
    cash_before = dict(kassa_cash_by_currency())[Currency.USD]

    resp = admin_client.post(f"/return-settlements/{settlement.pk}/pay/",
                             _pay(settlement))
    assert resp.status_code == 302

    settlement.refresh_from_db()
    assert settlement.paid_date is not None
    assert not settlement.is_pending
    assert not pending_refunds()
    assert dict(kassa_cash_by_currency())[Currency.USD] == cash_before - Decimal("1600.00")
    # Paying it changes nothing for the mijoz — they stopped holding credit the day
    # we promised it, which is the whole point of `Customer.refunded_total`.
    customer.refresh_from_db()
    assert customer.balance == Decimal("0.00")


def test_a_settled_refund_cannot_be_paid_twice(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="cash"))
    settlement = ReturnSettlement.objects.get()

    assert admin_client.post(f"/return-settlements/{settlement.pk}/pay/",
                             _pay(settlement)).status_code == 404


# ── bitta vazvratda bir nechta sotuv, ikkita valyuta ───────────────────────────

def test_one_visit_can_span_several_sales(admin_client, db):
    lot = _lot(kg="20000")
    customer = _customer()
    first = _sale(admin_client, lot, customer, kg="4000", price="1.50",
                  date="2026-07-18")
    second = _sale(admin_client, lot, customer, kg="3000", price="1.80",
                   date="2026-07-19")

    resp = admin_client.post("/returns/new/", return_rows(
        (first, "1000"), (second, "1000"), customer=customer, date="2026-07-20"))
    assert resp.status_code == 302

    batch = ReturnBatch.objects.get()
    assert batch.lines.count() == 2
    assert batch.total_kg == Decimal("2000")
    # Each line is valued at ITS OWN sotuv's narx — $1,500 + $1,800, never one
    # blended price across the marka.
    assert dict(batch.total_by_currency())[Currency.USD] == Decimal("3300.00")
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.net_total == Decimal("4500.00")     # 6000 − 1500
    assert second.net_total == Decimal("3600.00")    # 5400 − 1800


def test_two_currencies_settle_as_two_rows(admin_client, db):
    """A visit returning a dollar sotuv and a so'm sotuv hands back two figures. They
    never blend: a so'm sotuv refunded in dollars at today's kurs gives back a sum
    nobody paid."""
    lot = _lot(kg="20000")
    customer = _customer()
    dollar = _sale(admin_client, lot, customer, kg="1000", price="1.60",
                   date="2026-07-18")
    som = _sale(admin_client, lot, customer, kg="2000", price="20000",
                date="2026-07-19", currency="uzs", rate="12000")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1600"}, customer=customer, date="2026-07-18"))
    admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "uzs", "amount": "40000000", "exchange_rate": "12000"},
        customer=customer, date="2026-07-19"))

    resp = admin_client.post("/returns/new/", return_rows(
        (dollar, "500"), (som, "1000"), customer=customer, date="2026-07-20",
        settle="owed", due_date="2026-07-25"))
    assert resp.status_code == 302

    rows = {s.currency: s for s in ReturnSettlement.objects.all()}
    assert set(rows) == {Currency.USD, Currency.UZS}
    assert rows[Currency.USD].amount == Decimal("800.00")
    assert rows[Currency.UZS].amount_uzs == Decimal("20000000.00")


# ── cheklovlar ────────────────────────────────────────────────────────────────

def test_cannot_return_more_than_was_sold(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "1500"), customer=customer, date="2026-07-19"))
    assert resp.status_code == 200
    assert not ReturnBatch.objects.exists()
    assert not Return.objects.exists()


def test_cannot_return_more_than_is_left_after_an_earlier_return(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "600"), customer=customer, date="2026-07-19"))

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "500"), customer=customer, date="2026-07-20"))
    assert resp.status_code == 200
    assert ReturnBatch.objects.count() == 1


def test_a_sale_belonging_to_another_customer_is_refused(admin_client, db):
    lot = _lot(kg="20000")
    mine = _customer("Meniki")
    theirs = _customer("Boshqa")
    their_sale = _sale(admin_client, lot, theirs, kg="1000", price="1.60")

    resp = admin_client.post("/returns/new/", return_rows(
        (their_sale, "100"), customer=mine, date="2026-07-19"))
    assert resp.status_code == 200
    assert not Return.objects.exists()


def test_an_empty_vazvrat_is_refused(admin_client, db):
    lot = _lot()
    customer = _customer()
    _sale(admin_client, lot, customer, kg="1000", price="1.60")

    resp = admin_client.post("/returns/new/", return_rows(
        customer=customer, date="2026-07-19"))
    assert resp.status_code == 200
    assert not ReturnBatch.objects.exists()


# ── bekor qilish ──────────────────────────────────────────────────────────────

def test_deleting_a_vazvrat_restores_the_debt_and_the_stock(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))
    batch = ReturnBatch.objects.get()

    resp = admin_client.post(f"/returns/batch/{batch.pk}/delete/")
    assert resp.status_code == 302

    assert not ReturnBatch.objects.exists()
    assert not Return.objects.exists()
    sale.refresh_from_db()
    assert sale.net_total == Decimal("6400.00")
    lot.refresh_from_db()
    assert lot.available_kg == Decimal("6000")
    assert AuditLog.objects.filter(action=AuditLog.Action.DELETE,
                                   target_type="Vazvrat").exists()


def test_deleting_a_promised_vazvrat_gives_the_advance_back(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19",
        settle="owed", due_date="2026-07-25"))
    batch = ReturnBatch.objects.get()

    admin_client.post(f"/returns/batch/{batch.pk}/delete/")

    assert not ReturnSettlement.objects.exists()
    assert not pending_refunds()
    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")            # to'langan holatiga qaytdi
    customer.refresh_from_db()
    assert customer.balance == Decimal("0.00")


def test_a_vazvrat_whose_money_has_gone_cannot_be_deleted(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="cash"))
    batch = ReturnBatch.objects.get()

    admin_client.post(f"/returns/batch/{batch.pk}/delete/")

    assert ReturnBatch.objects.filter(pk=batch.pk).exists()


# ── ruxsat va modal ───────────────────────────────────────────────────────────

def test_only_admins_may_touch_a_vazvrat(translator_client, admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")

    assert translator_client.get("/returns/").status_code == 403
    assert translator_client.get("/returns/new/").status_code == 403
    assert translator_client.post("/returns/new/", return_rows(
        (sale, "100"), customer=customer)).status_code == 403
    assert not Return.objects.exists()


def test_the_rows_endpoint_lists_what_the_customer_is_holding(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")

    html = admin_client.get(f"/returns/rows/?customer={customer.pk}").content.decode()
    assert f"ret_{sale.pk}" in html
    assert lot.brand in html

    # Returned in full, so there is nothing left to offer.
    admin_client.post("/returns/new/", return_rows(
        (sale, "4000"), customer=customer, date="2026-07-19"))
    html = admin_client.get(f"/returns/rows/?customer={customer.pk}").content.decode()
    assert f"ret_{sale.pk}" not in html


def test_modal_get_returns_a_partial(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    resp = admin_client.get(f"/returns/new/?sale={sale.pk}",
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html
    # Opened from a sotuv, the modal already knows whose it is and shows their rows.
    assert f"ret_{sale.pk}" in html


def test_modal_post_returns_204_with_redirect(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    resp = admin_client.post(
        "/returns/new/",
        return_rows((sale, "100"), customer=customer, date="2026-07-19"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/returns/"
    assert Return.objects.filter(sale=sale).exists()


def test_modal_invalid_post_returns_422(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    resp = admin_client.post(
        "/returns/new/",
        return_rows((sale, "99999"), customer=customer, date="2026-07-19"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 422
    assert "modal-head" in resp.content.decode()


def test_a_vazvrat_is_audited(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "200"), customer=customer, date="2026-07-19", note="sifat"))

    assert AuditLog.objects.filter(action=AuditLog.Action.RETURN,
                                   target_type="Vazvrat").exists()


def test_sale_page_shows_the_customers_vazvrat_summary(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="1000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "200"), customer=customer, date="2026-07-19", note="sifat"))

    html = admin_client.get(f"/sales/{sale.pk}/").content.decode()
    assert "Qaytardi" in html
    assert "Qo&#x27;lida bor" in html or "Qo'lida bor" in html
    assert "sifat" in html


# ── uch tomonlama ajratma: qarz / avans / naqd ────────────────────────────────

def test_a_debt_only_return_reports_no_advance(admin_client, db):
    """The bug this pins: the list used to print "avansda" whenever no cash row
    existed, so a vazvrat that had simply shrunk a qarz was reported as one that had
    handed the mijoz credit."""
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")

    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    batch = ReturnBatch.objects.get()
    assert dict(batch.to_debt_by_currency()) == {Currency.USD: Decimal("1600.00")}
    assert batch.advance_by_currency() == []          # nobody was given credit
    assert batch.refund_by_currency() == []
    html = admin_client.get("/returns/").content.decode()
    assert "avansda" not in html


def test_a_paid_return_reports_an_advance_and_no_debt(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)

    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="advance"))

    batch = ReturnBatch.objects.get()
    assert batch.to_debt_by_currency() == []          # there was no qarz to close
    assert dict(batch.advance_by_currency()) == {Currency.USD: Decimal("1600.00")}


def test_a_part_paid_return_splits_between_debt_and_advance(admin_client, db):
    """The third case, which nobody has to choose: the sotuv is half settled, so the
    vazvrat lands half on the qarz and half in the mijoz's pocket."""
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")   # $6,400
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "6000"}, customer=customer, date="2026-07-18"))
    sale.refresh_from_db()
    assert sale.remaining == Decimal("400.00")

    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="advance"))

    batch = ReturnBatch.objects.get()
    assert dict(batch.to_debt_by_currency()) == {Currency.USD: Decimal("400.00")}
    assert dict(batch.advance_by_currency()) == {Currency.USD: Decimal("1200.00")}
    customer.refresh_from_db()
    assert customer.balance == Decimal("-1200.00")


# ── pul savolini faqat kerak bo'lganda so'rash ────────────────────────────────

def test_the_money_question_is_not_asked_of_a_customer_who_owes_more(admin_client, db):
    """A mijoz whose qarz would swallow anything they returned can never be owed
    money back, so the settlement question has no answer and is not on the form."""
    lot = _lot()
    customer = _customer()
    _sale(admin_client, lot, customer, kg="4000", price="1.60")   # unpaid

    resp = admin_client.get(f"/returns/new/?customer={customer.pk}")
    assert "settle" not in resp.context["form"].fields
    html = resp.content.decode()
    assert "Hozir kassadan qaytarildi" not in html
    assert "Qarzdor" in html                          # …but their state is spelled out


def test_the_money_question_is_asked_of_a_customer_who_has_paid(admin_client, db):
    lot = _lot()
    customer = _customer()
    _paid_sale(admin_client, lot, customer)

    resp = admin_client.get(f"/returns/new/?customer={customer.pk}")
    assert "settle" in resp.context["form"].fields
    html = resp.content.decode()
    assert "Hozir kassadan qaytarildi" in html
    assert "Qarzsiz" in html


def test_a_settle_choice_posted_by_a_customer_who_cannot_overpay_changes_nothing(
        admin_client, db):
    """The fields are gone from the form, so a POST that carries them anyway must not
    conjure a payout out of a vazvrat that only cancelled qarz."""
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19",
        settle="cash", method="cash"))
    assert resp.status_code == 302
    assert not ReturnSettlement.objects.exists()
    assert not pending_refunds()


# ── har bir yo'l faqat o'zi so'ragan narsani ko'rsatadi ──────────────────────

def _route_tag(html, marker):
    """The opening tag `_return_rows.html` drew for one route's field or note."""
    at = html.index(marker)
    return html[html.rindex("<", 0, at):html.index(">", at) + 1]


def _route_text(html, marker):
    """What that element says, figures and all."""
    at = html.index(marker)
    return html[at:html.index("</span>", at)]


def test_the_modal_opens_folded_on_the_avans_route(admin_client, db):
    """Avans is the initial answer, and it needs neither a usul nor a day: money that
    stays in the pool leaves the till by no route and on no date. Both are SERVED
    hidden — folding them with script after paint would flash all three answers'
    fields on every open."""
    lot = _lot()
    customer = _customer()
    _paid_sale(admin_client, lot, customer)

    html = admin_client.get(f"/returns/new/?customer={customer.pk}").content.decode()
    assert "hidden" in _route_tag(html, 'data-settle-for="cash owed"')   # To'lov usuli
    assert "hidden" in _route_tag(html, 'data-settle-for="owed"')        # qaysi kuni
    assert "hidden" not in _route_tag(html, 'data-settle-for="advance"')


def test_the_promise_route_is_the_only_one_asked_for_a_day(admin_client, db):
    """A re-render after a rejected POST comes back on the route the operator chose,
    not on the initial — otherwise the day they were being asked for folds away
    under the error telling them to fill it in."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19",
        settle="owed", due_date=""))                    # a promise with no day
    html = resp.content.decode()
    assert "hidden" not in _route_tag(html, 'data-settle-for="owed"')
    assert "hidden" not in _route_tag(html, 'data-settle-for="cash owed"')
    assert "hidden" in _route_tag(html, 'data-settle-for="advance"')


def test_the_avans_route_names_the_heap_the_money_joins(admin_client, db):
    """A mijoz already holding credit is being topped up, and the note says by how
    much they are already up — the figure is on Qarzlar, which is not the screen the
    operator is on."""
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "7000.00"}, customer=customer, date="2026-07-18"))
    assert sale.total == Decimal("6400.00")             # …so $600 is left over

    html = admin_client.get(f"/returns/new/?customer={customer.pk}").content.decode()
    assert "$600" in _route_text(html, 'data-settle-for="advance"')


def test_a_customer_with_no_avans_is_told_so(admin_client, db):
    lot = _lot()
    customer = _customer()
    _paid_sale(admin_client, lot, customer)

    html = admin_client.get(f"/returns/new/?customer={customer.pk}").content.decode()
    note = _route_text(html, 'data-settle-for="advance"')
    assert "avansida pul yo'q" in note


def test_the_cash_route_names_what_is_in_the_heap_it_empties(admin_client, db):
    """Handing money back empties ONE of the three heaps, and which one is what the
    operator is choosing a line above — so that heap's qoldiq is printed beside the
    choice, and only that heap's."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "999999"), customer=customer, date="2026-07-19",   # over the ceiling
        settle="cash", method="card"))
    html = resp.content.decode()
    karta = dict(next(m for m in kassa_cash_by_method()
                      if m["code"] == "card")["split_full"])
    assert usd(karta[Currency.USD]) in _route_text(html, 'data-settle-method="card"')
    assert "hidden" not in _route_tag(html, 'data-settle-method="card"')
    assert "hidden" in _route_tag(html, 'data-settle-method="cash"')
    assert "hidden" in _route_tag(html, 'data-settle-method="transfer"')


def test_the_heaps_are_not_shown_where_no_money_is_leaving(admin_client, db):
    """The till figure belongs to the cash route alone. A promise moves nothing today
    and an avans moves nothing at all, so quoting the till under either would be
    answering a question neither asks."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)

    resp = admin_client.post("/returns/new/", return_rows(
        (sale, "999999"), customer=customer, date="2026-07-19",
        settle="owed", due_date="2026-07-25"))
    html = resp.content.decode()
    for code in ("cash", "card", "transfer"):
        assert "hidden" in _route_tag(html, f'data-settle-method="{code}"')


def test_a_customer_who_cannot_overpay_is_asked_none_of_it(admin_client, db):
    """The whole block goes with the question: no routes, no heaps, no till figures
    on a screen where no money can move."""
    lot = _lot()
    customer = _customer()
    _sale(admin_client, lot, customer, kg="4000", price="1.60")      # unpaid

    html = admin_client.get(f"/returns/new/?customer={customer.pk}").content.decode()
    assert 'data-settle-for="advance"' not in html
    assert 'data-settle-method="cash"' not in html


# ── sotuvlar ro'yxati qaytgan kg ni ko'rsatadi ────────────────────────────────

def test_the_sales_list_shows_what_came_back(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    sale.refresh_from_db()
    assert sale.net_kg == Decimal("3000.000")
    html = admin_client.get("/sales/").content.decode().replace(" ", " ")
    assert "3 000" in html                            # kept, printed as the kg
    assert "qaytdi" in html                           # and said to have come back
    # Jami follows the same netting, so it cannot disagree with Qoldiq beside it.
    assert "4 800" in html


# ── sotuvning o'z tarixi ──────────────────────────────────────────────────────

def test_the_sale_history_records_the_return(admin_client, db):
    """A sotuv's Tarix reads the trail of its OWN pk, so a vazvrat logged only
    against the visit left the one page telling that sotuv's story silent about the
    goods coming back."""
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    assert AuditLog.objects.filter(action=AuditLog.Action.RETURN,
                                   target_type="Sotuv", target_id=sale.pk).exists()
    html = admin_client.get(f"/sales/{sale.pk}/").content.decode()
    assert "Vazvrat: 1 000 kg qaytdi" in html.replace(" ", " ")
    assert "−1 000" in html.replace(" ", " ")     # came back, so it reads minus


def test_the_return_line_does_not_disturb_the_kg_trail(admin_client, db):
    """The Kg column walks a running value, and a vazvrat states a DIFFERENT
    quantity — how much came back. Letting it set the running value would make the
    next edit's "before" the returned figure."""
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    from crm.views import sale_history
    sale.refresh_from_db()
    trail = sale_history(sale)
    ret = next(h for h in trail if h["returned"])
    assert ret["returned"] == Decimal("1000")
    assert ret["before"] is None and ret["after"] is None   # not a state, a movement


def test_undoing_a_return_is_written_on_the_sale_too(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))
    batch = ReturnBatch.objects.get()

    admin_client.post(f"/returns/batch/{batch.pk}/delete/")

    assert AuditLog.objects.filter(action=AuditLog.Action.DELETE,
                                   target_type="Sotuv", target_id=sale.pk).exists()
    html = admin_client.get(f"/sales/{sale.pk}/").content.decode()
    assert "Vazvrat bekor qilindi" in html


def test_the_customer_history_shows_the_return(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    html = admin_client.get(f"/debts/{customer.pk}/").content.decode()
    assert "Qaytarish" in html
    assert lot.brand in html


# ── sotuvlar ro'yxatidagi ochiladigan qator ───────────────────────────────────

def test_a_returned_sale_opens_into_its_vazvratlar(admin_client, db):
    """The same control Yuklar carries: a caret on the row, and the vazvratlar in a
    panel underneath. Only on a sotuv that HAS them — an expander that opens an empty
    panel teaches the operator to stop pressing it."""
    lot = _lot(kg="20000")
    customer = _customer()
    returned = _sale(admin_client, lot, customer, kg="4000", price="1.60",
                     date="2026-07-18")
    untouched = _sale(admin_client, lot, customer, kg="1000", price="1.60",
                      date="2026-07-19")
    admin_client.post("/returns/new/", return_rows(
        (returned, "1000"), customer=customer, date="2026-07-20", note="sifat"))

    html = admin_client.get("/sales/").content.decode()
    assert f'data-sale="{returned.pk}"' in html
    assert f'data-sale="{untouched.pk}"' not in html   # nothing to open
    # The panel is there, closed, and names what came back off which sotuv.
    assert 'class="sale-detail"' in html
    assert "Qarzdan ayirildi" in html
    assert "sifat" in html


def test_the_sales_row_says_what_came_off_the_debt(admin_client, db):
    """Jami is net of the vazvrat; the line under it is the half an operator is asked
    about — how much of that came off the QARZ rather than back to the mijoz."""
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    html = admin_client.get("/sales/").content.decode().replace(" ", " ")
    assert "qarzdan" in html
    assert "−$1 600" in html                            # the qarz fell by the full value


def test_a_paid_sale_shows_no_debt_subtraction(admin_client, db):
    """The mirror: nothing came off the qarz because there was none, so the row does
    not claim a subtraction that never happened."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="advance"))

    batch = ReturnBatch.objects.get()
    assert batch.to_debt_by_currency() == []
    html = admin_client.get("/sales/").content.decode()
    # No blue figure anywhere: nothing was taken off a qarz that did not exist.
    assert "badge-todebt" not in html
    # …but the panel still opens, and says the money went to the mijoz instead.
    assert f'data-sale="{sale.pk}"' in html
    assert "mijoz avansiga o&#x27;tdi" in html


# ── "yangilandi" chipi va ranglar ─────────────────────────────────────────────

def test_a_sale_changed_today_is_flagged(admin_client, db):
    """A vazvrat changes a sotuv from OUTSIDE it, so the row lands here smaller with
    nothing saying why. The chip marks the change on the day it is made."""
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    html = admin_client.get("/sales/").content.decode().replace(" ", " ")
    assert "yangilandi" in html
    # The chip says WHAT moved rather than only that something did.
    assert "Vazvrat: 1 000 kg qaytdi" in html
    assert "qarzdan $1 600 ayirildi" in html


def test_the_flag_is_gone_the_next_day(admin_client, db):
    """A marker that never goes away marks nothing."""
    from datetime import timedelta
    from django.utils import timezone as tz
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    # `created_at` is auto_now_add, so it is pushed back rather than set at creation.
    Return.objects.update(created_at=tz.now() - timedelta(days=1))

    html = admin_client.get("/sales/").content.decode()
    assert "yangilandi" not in html
    # …the figures themselves stay, of course.
    assert "qaytdi" in html


def test_an_advance_return_is_flagged_too(admin_client, db):
    """The chip is about the sotuv having moved, not about which way the money went —
    a vazvrat that lands in the mijoz's avans changes the row just as much."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="advance"))

    html = admin_client.get("/sales/").content.decode().replace(" ", " ")
    assert "yangilandi" in html
    assert "1 600" in html and "mijoz avansiga o&#x27;tdi" in html


def test_the_three_destinations_are_colour_coded(admin_client, db):
    """Blue came off the qarz and moved nothing, violet sits as avans, red left the
    kassa. The classes are asserted because the colour IS the information here."""
    lot = _lot(kg="20000")
    customer = _customer()
    unpaid = _sale(admin_client, lot, customer, kg="4000", price="1.60",
                   date="2026-07-18")
    admin_client.post("/returns/new/", return_rows(
        (unpaid, "1000"), customer=customer, date="2026-07-19"))
    html = admin_client.get("/returns/").content.decode()
    assert "badge-todebt" in html
    assert "badge-toadvance" not in html and "badge-torefund" not in html

    # A mijoz of their own: on a shared one the to'lov is swept onto the OLDEST
    # outstanding sotuv, so the sotuv under test would never be paid at all.
    other = _customer("To'lagan mijoz")
    paid = _sale(admin_client, lot, other, kg="1000", price="1.60", date="2026-07-20")
    admin_client.post("/customer-payments/new/", payment_rows(
        {"amount": "1600"}, customer=other, date="2026-07-20"))
    admin_client.post("/returns/new/", return_rows(
        (paid, "500"), customer=other, date="2026-07-21", settle="advance"))
    assert "badge-toadvance" in admin_client.get("/returns/").content.decode()

    admin_client.post("/returns/new/", return_rows(
        (paid, "500"), customer=other, date="2026-07-22", settle="cash"))
    assert "badge-torefund" in admin_client.get("/returns/").content.decode()


def test_the_debt_column_says_ayirildi(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    for url in ("/returns/", f"/sales/{sale.pk}/"):
        html = admin_client.get(url).content.decode()
        assert "ayirildi" in html, url
        assert "yopildi" not in html, url


# ── ombor qaytgan kg ni ko'rsatadi ────────────────────────────────────────────

def test_the_warehouse_says_what_came_back(admin_client, db):
    """The kg were already back on the shelf — `available_kg` has always counted
    them. What was missing is the shelf SAYING so: Kirim − Sotilgan does not equal
    Sotish mumkin on a marka that has had a vazvrat, and nothing explained the gap."""
    lot = _lot(kg="10000")
    customer = _customer()
    sale = _sale(admin_client, lot, customer, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19"))

    groups = admin_client.get("/ombor/").context["page"].object_list
    row = next(g for g in groups if g["brand"] == lot.brand)
    assert row["kirim"] == Decimal("10000")
    assert row["sold"] == Decimal("4000")             # every kg that ever left
    assert row["returned"] == Decimal("1000")         # …of which this came back
    assert row["on_hand"] == Decimal("7000")          # 10000 − 4000 + 1000

    html = admin_client.get("/ombor/").content.decode().replace(" ", " ")
    assert "+1 000 qaytdi" in html


def test_a_marka_with_no_returns_says_nothing(admin_client, db):
    lot = _lot(kg="10000")
    customer = _customer()
    _sale(admin_client, lot, customer, kg="4000", price="1.60")

    groups = admin_client.get("/ombor/").context["page"].object_list
    row = next(g for g in groups if g["brand"] == lot.brand)
    assert row["returned"] == Decimal("0")
    assert "qaytdi" not in admin_client.get("/ombor/").content.decode()


# ── kassa daftarida qatori bo'lsin ────────────────────────────────────────────

def test_a_cash_refund_appears_in_the_kassa_ledger(admin_client, db):
    """The balance always fell — what was missing is the ROW. A till whose figure
    drops with nothing in the chiqim daftar to explain it reads as money gone
    missing, which is exactly how it was reported."""
    lot = _lot()
    customer = _customer("Doran pochcha")
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19", settle="cash"))

    html = admin_client.get("/kassa/?davr=all").content.decode().replace(" ", " ")
    assert "Doran pochcha" in html
    # Badged for what it was, in the blue every vazvrat figure wears — the title
    # beside it is free to say only WHOSE money went back.
    assert '<span class="badge badge-info">Vazvrat</span>' in html


def test_a_promised_refund_stays_out_of_the_ledger(admin_client, db):
    """Promised is not paid: it has not moved the till, so a chiqim row would spend
    it once now and again on the day it is handed over."""
    lot = _lot()
    customer = _customer("Kutayotgan mijoz")
    sale = _paid_sale(admin_client, lot, customer)
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=customer, date="2026-07-19",
        settle="owed", due_date="2026-07-25"))

    html = admin_client.get("/kassa/?davr=all").content.decode()
    assert "Vazvrat ·" not in html
    # It is on the page as money that must go out — just not as money that has.
    assert "Vazvrat bo'yicha qaytariladi" in html


# ── bugun o'zgarganini topish ─────────────────────────────────────────────────

def test_changed_today_finds_an_old_sale(admin_client, db):
    """A vazvrat entered today changes a sotuv from weeks ago, and this list is
    ordered by the SOTUV's date — so the row it moved is pages from where the
    operator is standing. The toggle is the way back to it."""
    lot = _lot(kg="20000")
    customer = _customer()
    old = _sale(admin_client, lot, customer, kg="4000", price="1.60",
                date="2026-06-01")
    _sale(admin_client, lot, customer, kg="1000", price="1.60", date="2026-07-30")
    admin_client.post("/returns/new/", return_rows(
        (old, "1000"), customer=customer, date="2026-07-19"))

    resp = admin_client.get("/sales/?changed=today")
    shown = {s.pk for g in resp.context["groups"] for s in g["sales"]}
    assert shown == {old.pk}                          # only what moved today
    assert resp.context["changed_count"] == 1
    assert "Yangilanganlar" in resp.content.decode()

    # Unfiltered, both are there — the toggle narrows, it does not reorder.
    everything = {s.pk for g in admin_client.get("/sales/").context["groups"]
                  for s in g["sales"]}
    assert old.pk in everything and len(everything) == 2


def test_the_chip_names_where_the_money_went(admin_client, db):
    """Three routes, three sentences. "mijozga qaytdi" for all of them read as cash
    handed over on a vazvrat where nothing had moved at all."""
    lot = _lot(kg="20000")

    unpaid = _customer("Qarzdor")
    sale = _sale(admin_client, lot, unpaid, kg="4000", price="1.60")
    admin_client.post("/returns/new/", return_rows(
        (sale, "1000"), customer=unpaid, date="2026-07-19"))

    to_advance = _customer("Avansga")
    paid = _paid_sale(admin_client, lot, to_advance, kg="3000")
    admin_client.post("/returns/new/", return_rows(
        (paid, "1000"), customer=to_advance, date="2026-07-19", settle="advance"))

    in_cash = _customer("Naqd")
    cash_sale = _paid_sale(admin_client, lot, in_cash, kg="2000")
    admin_client.post("/returns/new/", return_rows(
        (cash_sale, "1000"), customer=in_cash, date="2026-07-19", settle="cash"))

    owed = _customer("Kutmoqda")
    owed_sale = _paid_sale(admin_client, lot, owed, kg="5000")
    admin_client.post("/returns/new/", return_rows(
        (owed_sale, "1000"), customer=owed, date="2026-07-19",
        settle="owed", due_date="2026-07-25"))

    html = admin_client.get("/sales/?changed=today").content.decode()
    assert "qarzdan" in html and "ayirildi" in html
    assert "mijoz avansiga o&#x27;tdi" in html
    assert "kassadan qaytarildi" in html              # already handed over
    assert "kassadan qaytariladi" in html             # promised for a later day


# ── biz qarzdormiz: qismli to'lov, tahrir, avansga o'tkazish ──────────────────
#
# A promise is not always kept in one go, on the day we said, in the form we said.
# Until these three existed the only way to correct any of that was to undo the whole
# vazvrat and enter it again — which rewrites history to move a date.

def _promised(admin_client, customer, sale, kg="1000", due="2026-07-25"):
    admin_client.post("/returns/new/", return_rows(
        (sale, kg), customer=customer, date="2026-07-19",
        settle="owed", due_date=due))
    return ReturnSettlement.objects.get()


def test_a_promise_can_be_paid_in_part(admin_client, db):
    """The kassa does not always hold the whole promise on the day it falls due.
    Paying some of it leaves the rest standing as the promise it already was."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)      # $1 600
    cash_before = dict(kassa_cash_by_currency())[Currency.USD]

    resp = admin_client.post(f"/return-settlements/{settlement.pk}/pay/",
                             _pay(settlement, amount="600"))
    assert resp.status_code == 302

    settlement.refresh_from_db()
    assert settlement.amount == Decimal("1000.00")            # …the remainder
    assert settlement.is_pending
    assert settlement.due_date.isoformat() == "2026-07-25"    # the day did not move
    paid = ReturnSettlement.objects.exclude(pk=settlement.pk).get()
    assert paid.amount == Decimal("600.00") and paid.paid_date is not None
    # Only the part that actually went out left the kassa.
    assert dict(kassa_cash_by_currency())[Currency.USD] == cash_before - Decimal("600.00")
    assert [s.amount for s in pending_refunds()] == [Decimal("1000.00")]


def test_the_two_halves_of_a_split_promise_add_back_up(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)
    whole, whole_uzs = settlement.amount, settlement.amount_uzs

    admin_client.post(f"/return-settlements/{settlement.pk}/pay/",
                      _pay(settlement, amount="333.33"))

    settlement.refresh_from_db()
    paid = ReturnSettlement.objects.exclude(pk=settlement.pk).get()
    assert paid.amount + settlement.amount == whole
    assert paid.amount_uzs + settlement.amount_uzs == whole_uzs


def test_paying_the_whole_promise_writes_no_second_row(admin_client, db):
    """The row WAS the promise; paying it in full is a date on that row, not a new
    one beside it — two rows would count the same money twice."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)

    admin_client.post(f"/return-settlements/{settlement.pk}/pay/", _pay(settlement))

    assert ReturnSettlement.objects.count() == 1
    settlement.refresh_from_db()
    assert settlement.paid_date.isoformat() == "2026-07-25"


def test_a_promise_cannot_be_overpaid(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)

    resp = admin_client.post(f"/return-settlements/{settlement.pk}/pay/",
                             _pay(settlement, amount="99999"))
    assert resp.status_code == 200                            # re-rendered, not saved
    settlement.refresh_from_db()
    assert settlement.is_pending
    assert ReturnSettlement.objects.count() == 1


def test_the_day_of_a_promise_can_be_moved(admin_client, db):
    """We said Friday, the money is not there, the mijoz agrees to Monday. Moving the
    day is not the same as forgetting it."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)

    resp = admin_client.post(f"/return-settlements/{settlement.pk}/edit/",
                             {"method": "card", "due_date": "2026-08-30"})
    assert resp.status_code == 302

    settlement.refresh_from_db()
    assert settlement.due_date.isoformat() == "2026-08-30"
    assert settlement.method == "card"
    assert settlement.amount == Decimal("1600.00")            # the summa is not typed
    assert settlement.is_pending                              # still owed


def test_a_promise_can_become_the_customers_advance(admin_client, db):
    """The mijoz agrees to leave it against their next sotuv. Nothing leaves the
    kassa and the promise stops existing — which is the same state the avans route
    writes, because leaving the money in the pool IS that route."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)
    cash_before = dict(kassa_cash_by_currency())[Currency.USD]

    resp = admin_client.post(f"/return-settlements/{settlement.pk}/to-advance/")
    assert resp.status_code == 302

    assert not ReturnSettlement.objects.exists()
    assert not RefundAllocation.objects.exists()              # back in the pool
    assert not pending_refunds()
    assert dict(kassa_cash_by_currency())[Currency.USD] == cash_before
    # …and the money is theirs to spend again: a mijoz holding credit reads as a
    # negative balance, the same way the avans route leaves them.
    customer.refresh_from_db()
    assert customer.balance == Decimal("-1600.00")


def test_an_advance_from_a_moved_promise_pays_the_next_sale(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)
    admin_client.post(f"/return-settlements/{settlement.pk}/to-advance/")

    later = _sale(admin_client, lot, customer, kg="1000", price="1.60",
                  date="2026-07-30")
    later.refresh_from_db()
    assert later.remaining == Decimal("0.00")                 # paid out of the avans


def test_a_settled_promise_offers_none_of_the_three(admin_client, db):
    """Money that has gone is not a promise any more: it cannot be paid again, moved
    to another day, or turned into credit."""
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)
    admin_client.post(f"/return-settlements/{settlement.pk}/pay/", _pay(settlement))

    for action in ("pay", "edit", "to-advance"):
        assert admin_client.get(
            f"/return-settlements/{settlement.pk}/{action}/").status_code == 404


def test_the_owed_table_offers_all_three(admin_client, db):
    lot = _lot()
    customer = _customer()
    sale = _paid_sale(admin_client, lot, customer)
    settlement = _promised(admin_client, customer, sale)

    html = admin_client.get("/returns/").content.decode()
    assert f"/return-settlements/{settlement.pk}/pay/" in html
    assert f"/return-settlements/{settlement.pk}/edit/" in html
    assert f"/return-settlements/{settlement.pk}/to-advance/" in html
    assert "To'lashimiz kerak bo'lgan sana" in html
