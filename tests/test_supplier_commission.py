"""Vositachi foizi: hamkor pulni to'g'ridan-to'g'ri olmaydi — oradagi odam yetkazib
beradi va foiz ushlab qoladi. Kiritilgan summa — hamkor OLADIGAN summa; foiz uning
ustiga qo'shiladi va kassadan chiqadi."""
from decimal import Decimal

from conftest import make_contract, make_shipment
from crm.models import Contract, ShipmentStatus, SupplierPayment, commission_total


def _contract(kg="1000", price="1.00"):
    """A kelishuv with one truck sent, so there is a qarz to pay against."""
    contract = make_contract(kg=kg, price=price)
    make_shipment(contract=contract, kg=kg, status=ShipmentStatus.objects.first())
    return contract


def _pay(contract, amount="10000", percent="2"):
    return SupplierPayment.objects.create(
        contract=contract, date="2026-07-20", amount=Decimal(amount),
        commission_percent=Decimal(percent), method="cash")


def test_cut_rides_on_top_of_what_the_hamkor_receives(db):
    p = _pay(_contract(kg="20000", price="1.00"))
    assert p.amount == Decimal("10000")          # hamkor qo'liga tegadi
    assert p.commission_amount == Decimal("200.00")
    assert p.total_out == Decimal("10200.00")    # kassadan chiqadi


def test_no_percent_means_no_cut(db):
    p = _pay(_contract(kg="20000"), percent="0")
    assert p.commission_amount == Decimal("0.00")
    assert p.total_out == p.amount


def test_the_cut_does_not_settle_the_hamkors_qarz(db):
    """Qarz hamkor olgan summaga qarab kamayadi — vositachining ulushi emas."""
    contract = _contract(kg="20000", price="1.00")   # 20 000$ qarz
    _pay(contract, amount="10000", percent="2")
    assert contract.paid_total == Decimal("10000")
    assert contract.debt == Decimal("10000")         # 10 200 emas


def test_percent_is_per_payment(db):
    contract = _contract(kg="20000")
    a = _pay(contract, amount="1000", percent="1.5")
    b = _pay(contract, amount="1000", percent="3")
    assert a.commission_amount == Decimal("15.00")
    assert b.commission_amount == Decimal("30.00")
    assert commission_total([a, b]) == Decimal("45.00")


def test_rounding_lands_on_cents(db):
    p = _pay(_contract(kg="20000"), amount="333.33", percent="2.5")
    assert p.commission_amount == Decimal("8.33")     # 8.33325 → 8.33


# --- form ------------------------------------------------------------------

def _post(client, contract, **extra):
    data = {"contract": contract.pk, "date": "2026-07-20", "currency": "usd",
            "amount": "500", "exchange_rate": "", "commission_percent": "2",
            "method": "cash", "note": ""}
    data.update(extra)
    return client.post("/supplier-payments/new/", data)


def test_form_saves_the_percent(admin_client, db):
    contract = _contract(kg="20000")
    assert _post(admin_client, contract).status_code == 302
    p = SupplierPayment.objects.get()
    assert p.commission_percent == Decimal("2.00")
    assert p.total_out == Decimal("510.00")


def test_form_defaults_to_no_cut(admin_client, db):
    contract = _contract(kg="20000")
    _post(admin_client, contract, commission_percent="")
    assert SupplierPayment.objects.get().commission_amount == Decimal("0.00")


def test_form_rejects_a_percent_over_100(admin_client, db):
    contract = _contract(kg="20000")
    assert _post(admin_client, contract, commission_percent="140").status_code == 200
    assert not SupplierPayment.objects.exists()


def test_the_debt_cap_ignores_the_cut(admin_client, db):
    """Qarz 500$ bo'lsa, 500$ to'lash mumkin — vositachining 10$ i qarzga kirmaydi."""
    contract = _contract(kg="500", price="1.00")   # 500$ qarz
    assert _post(admin_client, contract, amount="500").status_code == 302
    assert SupplierPayment.objects.get().total_out == Decimal("510.00")


# --- kassa -----------------------------------------------------------------

def test_kassa_takes_the_cut_out_of_the_till(admin_client, db):
    contract = _contract(kg="20000")
    _pay(contract, amount="1000", percent="2")
    resp = admin_client.get("/kassa/")
    assert resp.context["cash_total"] == Decimal("-1020.00")
    assert resp.context["balances"]["cash"]["out"] == Decimal("1020.00")


def test_kassa_lists_the_cut_as_its_own_chiqim_row(admin_client, db):
    contract = _contract(kg="20000")
    _pay(contract, amount="1000", percent="2")
    rows = admin_client.get("/kassa/").context["outflow_rows"]
    kinds = {r["kind"]: r["amount"] for r in rows}
    assert kinds["supplier"] == Decimal("1000")
    assert kinds["commission"] == Decimal("20.00")


def test_a_zero_percent_payment_adds_no_chiqim_row(admin_client, db):
    contract = _contract(kg="20000")
    _pay(contract, amount="1000", percent="0")
    rows = admin_client.get("/kassa/").context["outflow_rows"]
    assert [r["kind"] for r in rows] == ["supplier"]
