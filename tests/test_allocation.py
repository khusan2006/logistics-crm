from decimal import Decimal

from crm.models import (
    Contract, ContractLine, Customer, CustomerPayment, Partner, PaymentAllocation, Sale, Shipment, ShipmentLine, ShipmentStatus, allocate_customer_payment, apply_customer_advance,
    unspent_payment_amount,
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


def _payment(customer, amount, date="2026-07-20"):
    return CustomerPayment.objects.create(
        customer=customer, date=date, amount=Decimal(amount),
        amount_uzs=Decimal(amount) * 12000, method="cash",
    )


def test_fifo_across_two_sales(db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")  # $3,000, older
    s2 = _sale(customer, lot, "2000", "1.00", "2026-07-18")  # $2,000
    payment = _payment(customer, "4000")

    leftover = allocate_customer_payment(payment)

    assert leftover == Decimal("0")
    s1.refresh_from_db()
    s2.refresh_from_db()
    assert s1.remaining == Decimal("0")
    assert s2.remaining == Decimal("1000.00")
    customer.refresh_from_db()
    assert customer.balance == Decimal("1000.00")


def test_overpay_creates_advance(db):
    customer = _customer()
    lot = _lot()
    _sale(customer, lot, "3000", "1.00", "2026-07-17")
    _sale(customer, lot, "2000", "1.00", "2026-07-18")
    payment = _payment(customer, "6000")

    leftover = allocate_customer_payment(payment)

    assert leftover == Decimal("1000.00")
    customer.refresh_from_db()
    assert customer.balance == Decimal("-1000.00")


def test_manual_pick_specific_sale(db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    s2 = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    payment = _payment(customer, "2000")

    leftover = allocate_customer_payment(payment, picks=[(s2.pk, Decimal("2000"))])

    assert leftover == Decimal("0")
    s1.refresh_from_db()
    s2.refresh_from_db()
    assert s1.remaining == Decimal("3000.00")
    assert s2.remaining == Decimal("0")


def test_advance_auto_applies_to_new_sale(admin_client, db):
    customer = _customer()
    lot = _lot()
    # No outstanding sales yet — the whole $1,000 payment becomes an advance.
    payment = _payment(customer, "1000")
    leftover = allocate_customer_payment(payment)
    assert leftover == Decimal("1000.00")

    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "800",
        "currency": "usd", "exchange_rate": "12000", "price": "1.00", "date": "2026-07-19", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale = Sale.objects.get(line=lot)
    assert sale.total == Decimal("800.00")
    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")

    customer.refresh_from_db()
    # $1,000 advance − $800 applied to the new sale = $200 advance left (avans, negative balance).
    assert customer.balance == Decimal("-200.00")


def test_apply_customer_advance_directly_partial_cover(db):
    from django.db.models import Sum

    customer = _customer()
    lot = _lot()
    payment = _payment(customer, "1000")
    allocate_customer_payment(payment)  # whole thing becomes an advance

    sale = _sale(customer, lot, "800", "1.00", "2026-07-19")
    apply_customer_advance(sale)

    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")
    allocated = PaymentAllocation.objects.filter(payment=payment).aggregate(s=Sum("amount"))["s"]
    assert allocated == Decimal("800.00")  # $200 of the $1,000 advance remains unallocated


def test_per_sale_and_per_payment_allocation_invariants(db):
    customer = _customer()
    lot = _lot()
    s1 = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    s2 = _sale(customer, lot, "2000", "1.00", "2026-07-18")
    payment = _payment(customer, "6000")
    allocate_customer_payment(payment)

    from django.db.models import Sum
    s1_alloc = PaymentAllocation.objects.filter(sale=s1).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    s2_alloc = PaymentAllocation.objects.filter(sale=s2).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    payment_alloc = PaymentAllocation.objects.filter(payment=payment).aggregate(s=Sum("amount"))["s"] or Decimal("0")

    assert s1_alloc <= s1.net_total
    assert s2_alloc <= s2.net_total
    assert payment_alloc <= payment.amount


def test_picks_ignore_unknown_sale_id(db):
    """A bogus/stale pick id is skipped (no 500); the leftover still FIFOs onto
    the customer's real outstanding sale."""
    customer = _customer()
    lot = _lot()
    real = _sale(customer, lot, "2000", "1.00", "2026-07-17")
    payment = _payment(customer, "2000")

    # pick a non-existent sale id — must not raise
    leftover = allocate_customer_payment(payment, picks=[(999999, Decimal("2000"))])

    assert leftover == Decimal("0")
    real.refresh_from_db()
    assert real.remaining == Decimal("0")  # FIFO covered the real sale
    assert not PaymentAllocation.objects.filter(sale_id=999999).exists()


def test_advance_pays_older_sale_before_the_new_one(admin_client, db):
    """An avans that sits unspent while an OLDER sotuv owes belongs to that older
    sotuv. Creating a newer sotuv must not let the money skip the queue."""
    customer = _customer()
    lot = _lot()
    old_sale = _sale(customer, lot, "1000", "1.00", "2026-07-17")   # $1,000
    payment = _payment(customer, "1000")
    # Only part of it sits on the old sotuv, leaving $400 as an avans while that same
    # sotuv still owes $400 — the state a to'lov edit used to leave behind.
    PaymentAllocation.objects.create(payment=payment, sale=old_sale, amount=Decimal("600"))
    assert old_sale.remaining == Decimal("400.00")
    assert unspent_payment_amount(payment) == Decimal("400.00")

    resp = admin_client.post(f"/sales/new/?lot={lot.pk}", {
        "customer": customer.pk, "brand": lot.brand, "kg": "500",
        "currency": "usd", "exchange_rate": "12000", "price": "1.00",
        "date": "2026-07-25", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302

    new_sale = Sale.objects.get(date="2026-07-25")
    old_sale.refresh_from_db()
    assert old_sale.remaining == Decimal("0")          # the avans went here
    assert new_sale.remaining == Decimal("500.00")     # not onto the new sotuv


def test_payment_edit_places_other_payments_stranded_advance(admin_client, db):
    """Re-spreading the edited to'lov can leave a sotuv it used to cover short. The
    sweep pairs that qoldiq with whatever avans another to'lov is still holding."""
    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "1000", "1.00", "2026-07-17")
    first = _payment(customer, "1000", date="2026-07-18")
    allocate_customer_payment(first)                   # covers the sotuv in full
    second = _payment(customer, "400", date="2026-07-19")
    allocate_customer_payment(second)                  # nothing open -> all avans
    assert unspent_payment_amount(second) == Decimal("400.00")

    # Shrink the first to'lov: the sotuv is $400 short again.
    resp = admin_client.post(f"/customer-payments/{first.pk}/edit/", {
        "customer": customer.pk, "date": "2026-07-18", "currency": "usd",
        "amount": "600", "exchange_rate": "12000", "method": "cash",
        "fee_percent": "0", "note": "",
    })
    assert resp.status_code == 302

    sale.refresh_from_db()
    assert sale.remaining == Decimal("0")              # second to'lov's avans stepped in
    assert unspent_payment_amount(second) == Decimal("0")


def test_sale_edit_to_another_customer_drops_stale_allocations(admin_client, db):
    """Allocations are slices of the PREVIOUS mijoz's to'lovlar — they must not follow
    the sotuv to somebody else, or the money reads as paid here while still counting
    against the mijoz who handed it over."""
    payer = _customer("Payer")
    other = _customer("Boshqa mijoz")
    lot = _lot()
    sale = _sale(payer, lot, "1000", "1.00", "2026-07-17")
    payment = _payment(payer, "1000")
    allocate_customer_payment(payment)
    assert sale.remaining == Decimal("0")

    resp = admin_client.post(f"/sales/{sale.pk}/edit/", {
        "customer": other.pk, "line": lot.pk, "kg": "1000",
        "currency": "usd", "exchange_rate": "12000", "price": "1.00",
        "date": "2026-07-17", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302

    sale.refresh_from_db()
    assert sale.customer_id == other.pk
    assert sale.remaining == Decimal("1000.00")        # the new mijoz has paid nothing
    assert not PaymentAllocation.objects.filter(sale=sale).exists()
    payer.refresh_from_db()
    assert payer.balance == Decimal("-1000.00")        # money is the payer's avans again


def test_reconcile_command_moves_money_back_to_the_older_sale(db):
    """The repair pass: a slice sitting on a newer sotuv while an older one owes gets
    re-homed, and the command is a dry run unless --apply is given."""
    from django.core.management import call_command
    from io import StringIO

    customer = _customer()
    lot = _lot()
    old_sale = _sale(customer, lot, "1000", "1.00", "2026-07-17")
    new_sale = _sale(customer, lot, "500", "1.00", "2026-07-25")
    payment = _payment(customer, "400")
    # The shape prod was in: the avans landed on the NEWER sotuv.
    PaymentAllocation.objects.create(payment=payment, sale=new_sale, amount=Decimal("400"))

    out = StringIO()
    call_command("reconcile_allocations", stdout=out)
    assert f"sotuv #{new_sale.pk}" in out.getvalue()
    assert new_sale.remaining == Decimal("100.00")     # dry run changed nothing

    call_command("reconcile_allocations", "--apply", stdout=StringIO())
    old_sale.refresh_from_db()
    new_sale.refresh_from_db()
    assert old_sale.remaining == Decimal("600.00")     # $400 moved here
    assert new_sale.remaining == Decimal("500.00")
    assert unspent_payment_amount(payment) == Decimal("0")

    # Idempotent: a second pass has nothing left to say.
    out = StringIO()
    call_command("reconcile_allocations", stdout=out)
    assert "Hammasi joyida" in out.getvalue()


def test_reconcile_command_moves_one_slice_only_once(db):
    """A single slice raided by two older unpaid sotuvlar must not be planned away
    twice — the plan is capped by what the allocation actually holds."""
    from django.core.management import call_command
    from io import StringIO

    customer = _customer()
    lot = _lot()
    first = _sale(customer, lot, "300", "1.00", "2026-07-17")
    second = _sale(customer, lot, "300", "1.00", "2026-07-18")
    newest = _sale(customer, lot, "900", "1.00", "2026-07-25")
    payment = _payment(customer, "400")
    PaymentAllocation.objects.create(payment=payment, sale=newest, amount=Decimal("400"))

    call_command("reconcile_allocations", "--apply", stdout=StringIO())

    first.refresh_from_db()
    second.refresh_from_db()
    newest.refresh_from_db()
    assert first.remaining == Decimal("0")             # $300 of the slice
    assert second.remaining == Decimal("200.00")       # the other $100
    assert newest.remaining == Decimal("900.00")       # gave up all $400, no more
    assert unspent_payment_amount(payment) == Decimal("0")


def test_edit_payment_decrease_reallocates(admin_client, db):
    """Editing a payment DOWN clears the stale over-cap allocation and re-allocates
    to the smaller amount (clear-and-recompute)."""
    from django.db.models import Sum

    customer = _customer()
    lot = _lot()
    sale = _sale(customer, lot, "3000", "1.00", "2026-07-17")
    payment = _payment(customer, "3000")
    allocate_customer_payment(payment)
    assert PaymentAllocation.objects.filter(payment=payment).aggregate(s=Sum("amount"))["s"] == Decimal("3000.00")

    resp = admin_client.post(f"/customer-payments/{payment.pk}/edit/", {
        "customer": customer.pk, "date": "2026-07-20", "currency": "usd",
        "amount": "1000", "exchange_rate": "12000", "method": "cash", "note": "",
    })
    assert resp.status_code == 302

    payment.refresh_from_db()
    sale.refresh_from_db()
    alloc = PaymentAllocation.objects.filter(payment=payment).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    assert payment.amount == Decimal("1000.00")
    assert alloc == Decimal("1000.00")           # stale $3,000 allocation dropped
    assert alloc <= payment.amount               # invariant holds after decrease
    assert sale.remaining == Decimal("2000.00")
