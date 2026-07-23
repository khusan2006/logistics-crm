from decimal import Decimal

from conftest import make_contract, make_shipment
from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus, SupplierPayment,
)


def _contract(db, ship_kg="1000"):
    """Contract with (by default) its full kg already on a truck — the payable
    to the partner accrues per shipped truck, so tests that pay need shipped value."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    c = Contract.objects.create(partner=partner, created="2026-07-01")
    c_line = ContractLine.objects.create(
        contract=c, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1.00"))
    if ship_kg:
        _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
        _ship_obj_line = ShipmentLine.objects.create(
            shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal(ship_kg))
    return c


def test_paying_before_anything_ships_is_allowed_as_avans(admin_client, db):
    """Qarz yuborilgan yuk bo'yicha o'sadi, lekin avans berish taqiqlanmaydi —
    to'lov kelishuv qiymatigacha qabul qilinadi."""
    c = _contract(db, ship_kg=None)
    assert c.debt == Decimal("0")
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "100",
        "exchange_rate": "", "commission_percent": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    assert c.paid_total == Decimal("100")


def test_debt_accrues_per_truck_at_its_own_price(admin_client, db):
    """Two trucks under one kelishuv, one at its own price: owed = Σ kg × unit
    price, not the contract total."""
    c = _contract(db, ship_kg="400")                       # 400 kg @ 1.00 (contract)
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("100"), price=Decimal("2.00"))
    assert c.shipped_value == Decimal("600.00")            # 400 + 200
    assert c.debt == Decimal("600.00")
    # The ceiling is what the kelishuv will really cost, not the 600$ shipped so
    # far and not the signed 1 000$: 600$ gone + 500 kg still due at 1.00 = 1 100$,
    # raised above the estimate by the truck that shipped at 2.00.
    assert c.payable_left == Decimal("1100.00")
    resp = admin_client.post("/supplier-payments/new/", {  # 1101 > 1100 → blocked
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "1101",
        "exchange_rate": "", "commission_percent": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_payment_reduces_debt(admin_client, db):
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "400",
        "exchange_rate": "", "method": "transfer", "note": "",
    })
    assert resp.status_code == 302
    assert c.debt == Decimal("600.00")


def test_overpay_blocked(admin_client, db):
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "1500",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_uzs_converted_to_usd(admin_client, db):
    c = _contract(db)
    admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "uzs", "amount": "1265000",
        "exchange_rate": "12650", "method": "cash", "note": "",
    })
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("100.00")
    assert p.amount_original == Decimal("1265000")
    assert p.exchange_rate == Decimal("12650")


def test_edit_excludes_own_amount_from_debt_check(admin_client, db):
    c = _contract(db)
    p = SupplierPayment.objects.create(contract=c, date="2026-07-02", amount=Decimal("1000"),
                                       amount_original=Decimal("1000"), method="cash")
    resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "900",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.amount == Decimal("900.00")


def test_create_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/supplier-payments/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_modal_post_valid_returns_204_with_redirect(admin_client, db):
    c = _contract(db)
    resp = admin_client.post(
        "/supplier-payments/new/",
        {
            "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "400",
            "exchange_rate": "", "method": "transfer", "note": "",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/supplier-payments/"
    assert SupplierPayment.objects.filter(contract=c).exists()


def test_create_modal_post_invalid_returns_422(admin_client, db):
    c = _contract(db)
    resp = admin_client.post(
        "/supplier-payments/new/",
        {
            "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "1500",
            "exchange_rate": "", "method": "cash", "note": "",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html
    assert not SupplierPayment.objects.exists()


def test_create_preselects_contract_from_query_param(admin_client, db):
    c = _contract(db)
    resp = admin_client.get(f"/supplier-payments/new/?contract={c.pk}")
    assert resp.status_code == 200
    assert resp.context["form"].initial.get("contract") == c.pk


# --- avans: paying before anything ships -----------------------------------

def _fresh_contract(kg="1000", price="2.00"):
    """A kelishuv with nothing shipped — jami 2,000$, qarz 0$."""
    return make_contract(kg=kg, price=price)


def _post_payment(client, contract, amount, **extra):
    data = {"contract": contract.pk, "date": "2026-07-23", "currency": "usd",
            "amount": amount, "exchange_rate": "", "commission_percent": "",
            "method": "cash", "note": ""}
    data.update(extra)
    return client.post("/supplier-payments/new/", data)


def test_can_pay_before_any_yuk_is_sent(admin_client, db):
    """Hamkorga avans berish mumkin — qarz hali paydo bo'lmagan bo'lsa ham."""
    contract = _fresh_contract()
    assert _post_payment(admin_client, contract, "500").status_code == 302
    assert contract.paid_total == Decimal("500")


def test_avans_can_run_up_to_the_whole_kelishuv(admin_client, db):
    contract = _fresh_contract()                      # jami 2,000$
    assert _post_payment(admin_client, contract, "2000").status_code == 302


def test_paying_more_than_the_kelishuv_is_worth_is_blocked(admin_client, db):
    contract = _fresh_contract()                      # jami 2,000$
    assert _post_payment(admin_client, contract, "2001").status_code == 200
    assert contract.paid_total == Decimal("0")


def test_the_cap_counts_what_was_already_paid(admin_client, db):
    contract = _fresh_contract()                      # jami 2,000$
    _post_payment(admin_client, contract, "1500")
    assert _post_payment(admin_client, contract, "600").status_code == 200
    assert _post_payment(admin_client, contract, "500").status_code == 302


def test_paying_ahead_leaves_less_to_pay(db):
    contract = _fresh_contract()                      # jami 2,000$
    SupplierPayment.objects.create(contract=contract, date="2026-07-23",
                                   amount=Decimal("800"), method="cash")
    assert contract.payable_left == Decimal("1200")
    assert contract.debt == Decimal("-800")           # xom hisob: yuk hali yo'q


def test_shipping_turns_the_avans_into_a_real_qarz(db):
    contract = _fresh_contract()
    make_shipment(contract=contract, kg="1000")       # 2,000$ yuborildi
    SupplierPayment.objects.create(contract=contract, date="2026-07-23",
                                   amount=Decimal("800"), method="cash")
    assert contract.debt == Decimal("1200")
    assert contract.payable_left == Decimal("1200")


def test_the_list_shows_what_is_left_to_pay(admin_client, db):
    """Ustun endi "yana qancha to'lash kerak" ni ko'rsatadi, manfiy qarzni emas."""
    contract = _fresh_contract()                      # jami 2,000$
    SupplierPayment.objects.create(contract=contract, date="2026-07-23",
                                   amount=Decimal("800"), method="cash")
    html = admin_client.get("/contracts/", {"state": ""}).content.decode()
    assert "$1,200.00" in html and "-800" not in html


# --- qolgan to'lov follows the real cost, not the signed estimate -----------

def _one_truck(contract, kg, price=None):
    return make_shipment(contract=contract, kg=kg, price=price,
                         status=ShipmentStatus.arrival(), arrived="2026-07-10")


def test_a_cheaper_truck_lowers_what_is_left_to_pay(db):
    """Yuk kelishilganidan arzonroq kelsa, qolgan to'lov ham kamayadi — kelishuv
    qiymati faqat reja edi."""
    c = make_contract(kg="1000", price="1.00")          # reja: 1 000$
    _one_truck(c, "1000", price="0.50")                 # haqiqatda: 500$
    SupplierPayment.objects.create(contract=c, date="2026-07-11",
                                   amount=Decimal("500"), method="cash")
    assert c.payable_left == Decimal("0.00")
    assert c.is_settled                                  # yopilgan


def test_a_dearer_truck_raises_what_is_left_to_pay(db):
    c = make_contract(kg="1000", price="1.00")
    _one_truck(c, "1000", price="2.00")                 # haqiqatda: 2 000$
    SupplierPayment.objects.create(contract=c, date="2026-07-11",
                                   amount=Decimal("1000"), method="cash")
    assert c.payable_left == Decimal("1000.00")
    assert not c.is_settled


def test_goods_still_to_come_are_counted_at_the_agreed_narx(db):
    c = make_contract(kg="1000", price="1.00")
    _one_truck(c, "400")                                 # 400$ ketdi
    SupplierPayment.objects.create(contract=c, date="2026-07-11",
                                   amount=Decimal("400"), method="cash")
    # 600 kg hali kelishilgan narxda kutilmoqda
    assert c.payable_left == Decimal("600.00")


def test_the_column_and_the_filter_never_disagree(db):
    """Ustunda "to'lash kerak" turib, qator Yakunlangan ga tushmasligi kerak."""
    for price, paid in [("0.50", "500"), ("2.00", "1000"), (None, "1000")]:
        c = make_contract(kg="1000", price="1.00")
        _one_truck(c, "1000", price=price)
        SupplierPayment.objects.create(contract=c, date="2026-07-11",
                                       amount=Decimal(paid), method="cash")
        assert c.is_settled == (c.payable_left <= 0)


def test_the_payment_cap_follows_the_real_cost(admin_client, db):
    c = make_contract(kg="1000", price="1.00")
    _one_truck(c, "1000", price="2.00")                 # haqiqatda 2 000$ turadi
    assert _post_payment(admin_client, c, "2000").status_code == 302
