"""Bank foizini kim ko'taradi — the choice, on both directions of the money.

The bank takes its cut either way; that is not a decision anybody makes. What IS a
decision is whose side of the ledger absorbs it, and until now each kind of row had
that answer hard-coded: money coming in was always the sender's loss, money going out
always rode on top of ours. Both are still the default, so nothing already booked
moves — a blank `fee_bearer` reads as "the way this kind of row has always behaved".

The pair to hold in mind is that the CASH never changes with the choice, only who is
credited: a 2% cut on 1 000 means 980 crosses the wire whoever pays for it.
"""
from decimal import Decimal

import pytest

from conftest import make_contract, make_shipment
from crm.models import (
    Contract, Customer, CustomerPayment, FeeBearer, Logist, LogistPayment,
    ShipmentExpense, SupplierPayment,
)

pytestmark = pytest.mark.django_db


def _customer_payment(bearer="", **kw):
    customer = kw.pop("customer", None) or Customer.objects.create(name="Alisher")
    return CustomerPayment.objects.create(
        customer=customer, date="2026-08-05", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), exchange_rate=Decimal("12000"),
        method="transfer", fee_percent=Decimal("2"), fee_bearer=bearer, **kw)


# --- money coming IN --------------------------------------------------------

def test_a_mijoz_carrying_the_cut_is_credited_only_what_arrived():
    """The default, and how every to'lov booked before the question existed reads."""
    payment = _customer_payment()
    assert payment.fee_amount == Decimal("20.00")
    assert payment.net_amount == Decimal("980.00")       # what the kassa gained
    assert payment.settled_amount == Decimal("980.00")   # what their qarz falls by
    assert not payment.fee_on_company


def test_the_company_carrying_the_cut_credits_the_mijoz_in_full():
    payment = _customer_payment(bearer=FeeBearer.COMPANY)
    assert payment.net_amount == Decimal("980.00")       # the cash is unchanged
    assert payment.settled_amount == Decimal("1000.00")  # but they owe 20 less
    assert payment.settled_amount_uzs == Decimal("12000000.00")


def test_the_mijozs_qarz_follows_who_carried_the_cut(admin_client):
    """End to end: the same 1 000 clears 980 of a sotuv or all 1 000 of it."""
    customer = Customer.objects.create(name="Alisher")
    theirs = _customer_payment(customer=customer)
    assert customer.paid_total == Decimal("980.00")

    theirs.fee_bearer = FeeBearer.COMPANY
    theirs.save(update_fields=["fee_bearer"])
    assert Customer.objects.get(pk=customer.pk).paid_total == Decimal("1000.00")


# --- money going OUT --------------------------------------------------------

def _supplier_payment(bearer=""):
    contract = make_contract(kg="1000", price="2.00")
    return SupplierPayment.objects.create(
        contract=contract, date="2026-08-05", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), exchange_rate=Decimal("12000"),
        method="transfer", fee_percent=Decimal("2"), fee_bearer=bearer)


def test_a_cut_we_carry_rides_on_top_and_the_hamkor_is_paid_in_full():
    payment = _supplier_payment()
    assert payment.fee_on_company                        # the outgoing default
    assert payment.credited_amount == Decimal("1000.00")
    assert payment.total_out == Decimal("1020.00")


def test_a_cut_the_hamkor_carries_comes_out_of_what_we_sent():
    payment = _supplier_payment(bearer=FeeBearer.COUNTERPARTY)
    assert payment.credited_amount == Decimal("980.00")  # what reached them
    assert payment.total_out == Decimal("1000.00")       # the kassa is out no more


def test_the_kelishuvs_qarz_follows_who_carried_the_cut():
    payment = _supplier_payment()
    contract = Contract.objects.get(pk=payment.contract_id)
    assert contract.paid_total == Decimal("1000.00")

    payment.fee_bearer = FeeBearer.COUNTERPARTY
    payment.save(update_fields=["fee_bearer"])
    contract = Contract.objects.get(pk=payment.contract_id)
    assert contract.paid_total == Decimal("980.00")
    assert contract.payable_left == Decimal("1020.00")   # 2 000 agreed, 980 paid


# --- the other two places money moves ---------------------------------------

def test_a_logist_is_funded_by_whichever_side_did_not_carry_the_cut():
    """This one used to charge the cut twice: the logist was funded 980 while the
    kassa was billed 1 020, so 20 vanished into neither side's ledger."""
    logist = Logist.objects.create(name="Sardor", phone="1")
    payment = LogistPayment.objects.create(
        logist=logist, date="2026-08-05", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), exchange_rate=Decimal("12000"),
        method="transfer", fee_percent=Decimal("2"))
    assert payment.net_amount == Decimal("1000.00")
    assert payment.total_out == Decimal("1020.00")

    payment.fee_bearer = FeeBearer.COUNTERPARTY
    payment.save(update_fields=["fee_bearer"])
    assert payment.net_amount == Decimal("980.00")
    assert payment.total_out == Decimal("1000.00")


def test_an_expense_leaves_the_kassa_by_whichever_side_carried_the_cut():
    shipment = make_shipment(kg="1000")
    expense = ShipmentExpense.objects.create(
        shipment=shipment, date="2026-08-05", amount=Decimal("1000"),
        amount_uzs=Decimal("12000000"), exchange_rate=Decimal("12000"),
        method="transfer", fee_percent=Decimal("2"))
    assert expense.total_out == Decimal("1020.00")

    expense.fee_bearer = FeeBearer.COUNTERPARTY
    expense.save(update_fields=["fee_bearer"])
    assert expense.total_out == Decimal("1000.00")


# --- the question is only asked when there is a cut -------------------------

def test_a_naqd_row_has_no_cut_for_anybody_to_carry():
    """The foiz is ignored on naqd and karta, so the bearer changes nothing."""
    payment = _customer_payment(bearer=FeeBearer.COMPANY)
    payment.method = "cash"
    assert payment.fee_amount == Decimal("0")
    assert payment.net_amount == payment.settled_amount == Decimal("1000.00")


def test_the_form_offers_the_choice_named_after_the_other_side(admin_client):
    """Each screen names the counterparty it is actually facing."""
    from crm.forms import (CustomerPaymentForm, LogistPaymentForm,
                           SupplierPaymentForm)

    labels = {form: dict(form().fields["fee_bearer"].choices)[FeeBearer.COUNTERPARTY]
              for form in (SupplierPaymentForm, CustomerPaymentForm, LogistPaymentForm)}
    assert labels[SupplierPaymentForm] == "Hamkordan ushlansin"
    assert labels[CustomerPaymentForm] == "Mijozdan ushlansin"
    assert labels[LogistPaymentForm] == "Logistdan ushlansin"
    # and each opens on the way its own kind of row has always behaved
    assert SupplierPaymentForm().initial["fee_bearer"] == FeeBearer.COMPANY
    assert CustomerPaymentForm().initial["fee_bearer"] == FeeBearer.COUNTERPARTY
