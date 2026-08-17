"""A split to'lov's shared answers stay shared, however it is edited afterwards.

Half naqd and half perechisleniya is ONE settlement written as several rows. The
header asks the settlement's own questions once — whose money, what it was for, when
— and stamps every row with the answer. Nothing enforced that afterwards: the edit
form reopens a single row and shows those same boxes, so one row could be moved to
another marka, another mijoz or another day while its twin stayed put. The result is
a payment that claims to have bought two different things, in a shape the entry form
refuses outright, and screens that read a to'lov per product then split one delivery
across two bars with nothing saying why.

Every model that is entered as a split names those fields in `settlement_fields`, and
`_sync_settlement` carries a correction back across the rest of the group.
"""
from decimal import Decimal

import pytest

from conftest import make_contract, make_shipment, payment_rows, supplier_payment_rows
from crm.models import (
    ContractLine, Customer, CustomerPayment, CustomsPayment, Kapital, Logist,
    LogistPayment, SupplierPayment,
)

pytestmark = pytest.mark.django_db


def _two_marka_contract():
    contract = make_contract(brand="7000 campaund", kg="1000", price="1.00")
    second = ContractLine.objects.create(contract=contract, brand="209 campaund",
                                         kg=Decimal("1000"), price=Decimal("1.00"))
    make_shipment(contract=contract, kg="1000")
    make_shipment(contract=contract, kg="1000", contract_line=second)
    return contract, contract.lines.get(brand="7000 campaund"), second


def _split_supplier_payment(client, contract, line):
    resp = client.post("/supplier-payments/new/", supplier_payment_rows(
        {"currency": "usd", "amount": "300", "exchange_rate": "12000", "method": "cash"},
        {"currency": "usd", "amount": "400", "exchange_rate": "12000", "method": "transfer"},
        contract=contract.pk, date="2026-07-02", contract_line=line.pk))
    assert resp.status_code == 302
    return list(SupplierPayment.objects.order_by("pk"))


def test_the_rows_of_one_settlement_start_out_agreeing(admin_client, db):
    contract, first, _second = _two_marka_contract()
    rows = _split_supplier_payment(admin_client, contract, first)
    assert len(rows) == 2
    assert {r.contract_line_id for r in rows} == {first.pk}
    assert rows[0].group is not None and rows[0].group == rows[1].group


def test_editing_one_row_moves_the_whole_settlement_to_the_new_marka(admin_client, db):
    """The regression this file exists for: the marka is a fact about the delivery,
    so correcting it on one row corrects the payment rather than splitting it."""
    contract, first, second = _two_marka_contract()
    naqd, bank = _split_supplier_payment(admin_client, contract, first)

    resp = admin_client.post(f"/supplier-payments/{naqd.pk}/edit/", {
        "contract": contract.pk, "contract_line": second.pk, "date": "2026-07-02",
        "currency": "usd", "amount": "300", "exchange_rate": "12000",
        "commission_percent": "", "method": "cash", "fee_percent": "0", "note": ""})
    assert resp.status_code == 302

    naqd.refresh_from_db(), bank.refresh_from_db()
    assert naqd.contract_line_id == second.pk
    assert bank.contract_line_id == second.pk, "juft qator eski markada qolib ketdi"


def test_the_sana_travels_with_it(admin_client, db):
    """Same rule, different column: one settlement happened on one day."""
    contract, first, _second = _two_marka_contract()
    naqd, bank = _split_supplier_payment(admin_client, contract, first)

    resp = admin_client.post(f"/supplier-payments/{naqd.pk}/edit/", {
        "contract": contract.pk, "contract_line": first.pk, "date": "2026-07-05",
        "currency": "usd", "amount": "300", "exchange_rate": "12000",
        "commission_percent": "", "method": "cash", "fee_percent": "0", "note": ""})
    assert resp.status_code == 302
    bank.refresh_from_db()
    assert str(bank.date) == "2026-07-05"


def test_a_lone_to_lov_is_not_a_group_and_touches_nothing_else(admin_client, db):
    """Two unrelated one-row to'lovlar: editing one must not reach the other. They
    carry no group id at all, which is what keeps them apart."""
    contract, first, second = _two_marka_contract()
    for amount in ("100", "200"):
        admin_client.post("/supplier-payments/new/", supplier_payment_rows(
            {"currency": "usd", "amount": amount, "exchange_rate": "12000",
             "method": "cash"},
            contract=contract.pk, date="2026-07-02", contract_line=first.pk))
    one, two = SupplierPayment.objects.order_by("pk")
    assert one.group is None and two.group is None

    admin_client.post(f"/supplier-payments/{one.pk}/edit/", {
        "contract": contract.pk, "contract_line": second.pk, "date": "2026-07-02",
        "currency": "usd", "amount": "100", "exchange_rate": "12000",
        "commission_percent": "", "method": "cash", "fee_percent": "0", "note": ""})
    two.refresh_from_db()
    assert two.contract_line_id == first.pk, "begona to'lov ham o'zgarib ketdi"


def test_a_mijoz_settlement_moves_together_and_its_money_follows(admin_client, db):
    """The incoming side has a consequence the others do not: a to'lov's allocations
    hang off its mijoz, so a row that moves has to be re-spread or its money stays
    sitting on the old mijoz's sotuvlar."""
    alisher = Customer.objects.create(name="Alisher")
    bobur = Customer.objects.create(name="Bobur")
    resp = admin_client.post("/customer-payments/new/", payment_rows(
        {"currency": "usd", "amount": "300", "exchange_rate": "12000", "method": "cash"},
        {"currency": "usd", "amount": "400", "exchange_rate": "12000", "method": "transfer"},
        customer=alisher.pk, date="2026-07-02"))
    assert resp.status_code == 302
    naqd, bank = CustomerPayment.objects.order_by("pk")
    assert naqd.group == bank.group

    resp = admin_client.post(f"/customer-payments/{naqd.pk}/edit/", {
        "customer": bobur.pk, "date": "2026-07-02", "target_currency": "",
        "currency": "usd", "amount": "300", "exchange_rate": "12000",
        "method": "cash", "fee_percent": "0", "note": ""})
    assert resp.status_code == 302

    naqd.refresh_from_db(), bank.refresh_from_db()
    assert naqd.customer_id == bobur.pk
    assert bank.customer_id == bobur.pk, "juft qator eski mijozda qolib ketdi"
    # and nothing of theirs is still pointed at Alisher's sotuvlar
    assert not bank.allocations.exclude(sale__customer=bobur).exists()


@pytest.mark.parametrize("model", [SupplierPayment, CustomerPayment, LogistPayment,
                                   CustomsPayment, Kapital])
def test_every_split_model_names_the_answers_its_header_asks(model):
    """A model entered as a split must say WHICH of its columns the header owns —
    left empty, `_sync_settlement` would quietly do nothing for it."""
    assert model.settlement_fields, model.__name__
    names = {f.name for f in model._meta.get_fields()}
    for field in model.settlement_fields:
        assert field in names, f"{model.__name__}.{field}"
