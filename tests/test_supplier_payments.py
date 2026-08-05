from decimal import Decimal

from conftest import make_contract, make_shipment
from crm.templatetags.crm_extras import NBSP
from crm.models import (
    Contract, ContractLine, Currency, Partner, Shipment, ShipmentLine, ShipmentStatus,
    SupplierPayment,
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
        "exchange_rate": "12000", "commission_percent": "", "method": "cash", "note": "",
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
        "exchange_rate": "12000", "commission_percent": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_payment_reduces_debt(admin_client, db):
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "400",
        "exchange_rate": "12000", "method": "transfer", "note": "",
    })
    assert resp.status_code == 302
    assert c.debt == Decimal("600.00")


def test_overpay_blocked(admin_client, db):
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "1500",
        "exchange_rate": "12000", "method": "cash", "note": "",
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
    # the typed so'm figure is kept exact, not re-derived from the rounded dollars
    assert p.amount_uzs == Decimal("1265000")
    assert p.exchange_rate == Decimal("12650")


def test_usd_entry_also_stores_a_som_value(admin_client, db):
    """The kurs is asked in both directions, so a dollar to'lov is reportable in
    so'm too — the gap that made every pre-existing dollar row unconvertible."""
    c = _contract(db)
    admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "100",
        "exchange_rate": "12650", "method": "cash", "note": "",
    })
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("100.00")
    assert p.amount_uzs == Decimal("1265000")


def test_a_cross_currency_entry_without_a_kurs_is_rejected(admin_client, db):
    """Paying a dollar kelishuv in so'm: without a kurs there is no way to say how
    much of the qarz the money cleared, so the row is refused rather than guessed."""
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "uzs", "amount": "1265000",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 200          # redisplayed, not saved
    assert SupplierPayment.objects.count() == 0


def test_paying_a_kelishuv_in_its_own_currency_asks_for_no_kurs(admin_client, db):
    """Same currency in and out — the summa IS what the qarz falls by. The row still
    ends up with a kurs so the kassa's other column has something to add up."""
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "100",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    payment = SupplierPayment.objects.get()
    assert payment.amount == Decimal("100.00")
    assert payment.exchange_rate > 0
    assert Contract.objects.get(pk=c.pk).paid_total_own == Decimal("100.00")


def test_edit_excludes_own_amount_from_debt_check(admin_client, db):
    c = _contract(db)
    p = SupplierPayment.objects.create(contract=c, date="2026-07-02", amount=Decimal("1000"),
                                       amount_uzs=Decimal("12000000"), method="cash")
    resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "900",
        "exchange_rate": "12000", "method": "cash", "note": "",
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
            "exchange_rate": "12000", "method": "transfer", "note": "",
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
            "exchange_rate": "12000", "method": "cash", "note": "",
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
            "amount": amount, "exchange_rate": "12000", "commission_percent": "",
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


# --- a kelishuv is owed, capped and closed in the currency it was struck in ---

def _som_contract(kg="1000", price_uzs="12650", rate="12650"):
    """A kelishuv agreed in so'm: 1 000 kg at 12 650 so'm/kg = 12 650 000 so'm."""
    price = (Decimal(price_uzs) / Decimal(rate)).quantize(Decimal("0.0001"))
    contract = make_contract(kg=kg, price=price, price_uzs=price_uzs,
                             currency=Currency.UZS)
    contract.lines.update(exchange_rate=Decimal(rate))
    return Contract.objects.get(pk=contract.pk)


def test_a_som_kelishuv_is_owed_in_som(db):
    contract = _som_contract()
    assert contract.currency == Currency.UZS
    assert contract.lines.get().currency == Currency.UZS
    assert contract.total_value_own == Decimal("12650000.00")
    assert contract.payable_left_own == Decimal("12650000.00")


def test_a_som_kelishuv_paid_off_in_som_is_settled_whatever_the_kurs_did(admin_client, db):
    """Paid to the tiyin, in the currency it was agreed in, a week later at another
    kurs. The dollar twin of the two figures cannot line up — they were derived at
    rates a week apart — and that is exactly why it is not what settles anything."""
    contract = _som_contract()
    make_shipment(contract=contract, kg="1000")
    resp = _post_payment(admin_client, contract, "12650000",
                         currency="uzs", exchange_rate="12800")
    assert resp.status_code == 302

    contract = Contract.objects.get(pk=contract.pk)
    assert contract.payable_left_own == Decimal("0.00")
    assert contract.payable_left != Decimal("0.00")     # the derived side, unused
    assert contract.is_settled


def test_a_settled_som_kelishuv_takes_no_further_payment(admin_client, db):
    """The cap is the so'm figure too. Measured on the dollar side it would still
    read a remainder and go on asking for money that is not owed."""
    contract = _som_contract()
    make_shipment(contract=contract, kg="1000")
    _post_payment(admin_client, contract, "12650000", currency="uzs",
                  exchange_rate="12800")

    resp = _post_payment(admin_client, contract, "1000", currency="uzs",
                         exchange_rate="12800")
    assert resp.status_code == 200
    assert Contract.objects.get(pk=contract.pk).paid_total_uzs == Decimal("12650000.00")


def test_a_som_kelishuv_drops_off_the_working_list_once_settled(admin_client, db):
    contract = _som_contract()
    make_shipment(contract=contract, kg="1000")
    _post_payment(admin_client, contract, "12650000", currency="uzs",
                  exchange_rate="12800")

    listed = admin_client.get("/contracts/", {"state": "open"}).context["page"]
    assert [c.pk for c in listed.object_list] == []


# --- crossing a currency: the kurs is asked for, then frozen ----------------

def test_a_som_kelishuv_paid_in_dollars_falls_by_the_converted_figure(admin_client, db):
    """12 650 000 so'm owed, 500$ handed over at 13 000 → 6 500 000 so'm of it is
    cleared. What settles the qarz is the money AS CONVERTED, not the dollars."""
    contract = _som_contract()
    make_shipment(contract=contract, kg="1000")
    resp = _post_payment(admin_client, contract, "500", currency="usd",
                         exchange_rate="13000")
    assert resp.status_code == 302

    contract = Contract.objects.get(pk=contract.pk)
    assert contract.paid_total_own == Decimal("6500000.00")
    assert contract.payable_left_own == Decimal("6150000.00")


def test_a_dollar_kelishuv_paid_in_som_falls_by_the_converted_figure(admin_client, db):
    """The mirror case: 2 000$ owed, 6 500 000 so'm handed over at 13 000 clears
    500$ of it."""
    contract = _fresh_contract()                      # jami 2,000$
    make_shipment(contract=contract, kg="1000")
    resp = _post_payment(admin_client, contract, "6500000", currency="uzs",
                         exchange_rate="13000")
    assert resp.status_code == 302

    contract = Contract.objects.get(pk=contract.pk)
    assert contract.paid_total_own == Decimal("500.00")
    assert contract.payable_left_own == Decimal("1500.00")


def test_a_booked_tolov_does_not_move_when_the_kurs_does(admin_client, db):
    """The kurs a to'lov was made at is part of the to'lov. A later one at another
    rate settles its own money and leaves the earlier figure exactly where it was —
    otherwise a kelishuv squared last week re-opens itself this week."""
    contract = _som_contract()
    make_shipment(contract=contract, kg="1000")
    _post_payment(admin_client, contract, "500", currency="usd", exchange_rate="13000")
    first = SupplierPayment.objects.get()

    _post_payment(admin_client, contract, "500", currency="usd", exchange_rate="12000")
    first.refresh_from_db()
    assert first.amount_uzs == Decimal("6500000.00")   # untouched by the new rate
    assert Contract.objects.get(pk=contract.pk).paid_total_own == Decimal("12500000.00")


def test_correcting_a_tolovs_kurs_by_hand_re_rates_that_tolov(admin_client, db):
    """Frozen is not immutable: the operator can reopen a to'lov and put it on
    another kurs, and the qarz follows. It just never happens on its own."""
    contract = _som_contract()
    make_shipment(contract=contract, kg="1000")
    _post_payment(admin_client, contract, "500", currency="usd", exchange_rate="13000")
    payment = SupplierPayment.objects.get()

    resp = admin_client.post(f"/supplier-payments/{payment.pk}/edit/", {
        "contract": contract.pk, "date": "2026-07-23", "currency": "usd",
        "amount": "500", "exchange_rate": "12000", "commission_percent": "",
        "method": "cash", "fee_percent": "0", "note": ""})
    assert resp.status_code == 302

    payment.refresh_from_db()
    assert payment.amount_uzs == Decimal("6000000.00")
    assert Contract.objects.get(pk=contract.pk).payable_left_own == Decimal("6650000.00")


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
    assert f"$1{NBSP}200" in html and "-800" not in html


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


class TestKelishuvOptionShowsWhatIsLeftToPay:
    """The to'lov form's kelishuv picker carries the figure the form is about to
    spend down — and the very ceiling it will check the entry against. Without it
    the operator left the modal to look it up on Kelishuvlar and came back."""

    def _label(self, contract):
        from crm.forms import SupplierPaymentForm
        labels = [str(label) for _value, label in
                  list(SupplierPaymentForm().fields["contract"].choices)[1:]]
        return next(l for l in labels if l.startswith(contract.code))

    def test_a_dollar_kelishuv_offers_its_qarz_in_dollars(self, db):
        c = make_contract(kg="100", price="1.20")        # jami $120
        SupplierPayment.objects.create(contract=c, amount=Decimal("20"))
        assert f"to'lash: $100" in self._label(c)

    def test_a_som_kelishuv_offers_its_qarz_in_som(self, db):
        """Never the dollar column: it drifts with the kurs and would offer to
        collect money that is not owed."""
        c = make_contract(kg="200", price="1.00")
        c.currency, c.exchange_rate = Currency.UZS, Decimal("12000")
        c.save()
        line = c.lines.first()
        line.currency, line.price_uzs = Currency.UZS, Decimal("12000")
        line.exchange_rate = Decimal("12000")
        line.save()

        label = self._label(c)
        assert "so'm" in label.split("to'lash:")[1]
        assert "$" not in label.split("to'lash:")[1]

    def test_the_yuk_form_still_counts_in_kg(self, db):
        """Two forms, two questions: a yuk spends kg down, a to'lov spends money."""
        from crm.forms import ShipmentForm
        c = make_contract(kg="100", price="1.20")
        labels = [str(label) for _v, label in
                  list(ShipmentForm().fields["contract"].choices)[1:]]
        label = next(l for l in labels if l.startswith(c.code))
        assert "jami 100 kg" in label
        assert "to'lash" not in label
