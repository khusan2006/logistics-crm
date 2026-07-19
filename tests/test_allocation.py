from decimal import Decimal

from crm.models import (
    Contract, Customer, CustomerPayment, Partner, PaymentAllocation, Sale, Shipment, ShipmentStatus,
    allocate_customer_payment, apply_customer_advance,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _lot(kg="10000", brand="LLDPE", contract_price="1.00"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(
        partner=partner, brand=brand, kg=Decimal(kg), price=Decimal(contract_price),
        created="2026-07-01", deadline="2026-08-01",
    )
    return Shipment.objects.create(
        contract=contract, kg=Decimal(kg), status=ShipmentStatus.arrival(),
        sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16",
        transport="01A111AA", container="MSCU-1",
    )


def _sale(customer, lot, kg, price, date):
    return Sale.objects.create(
        customer=customer, shipment=lot, kg=Decimal(kg), price=Decimal(price),
        cost_price=lot.landed_cost_per_kg, date=date,
    )


def _payment(customer, amount, date="2026-07-20"):
    return CustomerPayment.objects.create(
        customer=customer, date=date, amount=Decimal(amount), amount_original=Decimal(amount),
        method="cash",
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
        "customer": customer.pk, "brand": lot.contract.brand, "kg": "800",
        "price": "1.00", "date": "2026-07-19", "debt_deadline": "", "note": "",
    })
    assert resp.status_code == 302
    sale = Sale.objects.get(shipment=lot)
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
        "amount": "1000", "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302

    payment.refresh_from_db()
    sale.refresh_from_db()
    alloc = PaymentAllocation.objects.filter(payment=payment).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    assert payment.amount == Decimal("1000.00")
    assert alloc == Decimal("1000.00")           # stale $3,000 allocation dropped
    assert alloc <= payment.amount               # invariant holds after decrease
    assert sale.remaining == Decimal("2000.00")
