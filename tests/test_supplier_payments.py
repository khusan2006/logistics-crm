from decimal import Decimal

from conftest import make_contract, make_shipment, supplier_payment_rows
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
    resp = admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "usd", "amount": "100", "exchange_rate": "12000",
            "commission_percent": "", "method": "cash", "note": ""},
                              contract=c.pk, date="2026-07-02"))
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
    resp = admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "usd", "amount": "1101", "exchange_rate": "12000",
            "commission_percent": "", "method": "cash", "note": ""},
                              contract=c.pk, date="2026-07-02"))
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_payment_reduces_debt(admin_client, db):
    c = _contract(db)
    resp = admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "usd", "amount": "400", "exchange_rate": "12000",
            "method": "transfer", "note": ""},
                              contract=c.pk, date="2026-07-02"))
    assert resp.status_code == 302
    assert c.debt == Decimal("600.00")


def test_overpay_blocked(admin_client, db):
    c = _contract(db)
    resp = admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "usd", "amount": "1500", "exchange_rate": "12000",
            "method": "cash", "note": ""},
                              contract=c.pk, date="2026-07-02"))
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_uzs_converted_to_usd(admin_client, db):
    c = _contract(db)
    admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "uzs", "amount": "1265000", "exchange_rate": "12650",
            "method": "cash", "note": ""},
                              contract=c.pk, date="2026-07-02"))
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("100.00")
    # the typed so'm figure is kept exact, not re-derived from the rounded dollars
    assert p.amount_uzs == Decimal("1265000")
    assert p.exchange_rate == Decimal("12650")


def test_usd_entry_also_stores_a_som_value(admin_client, db):
    """The kurs is asked in both directions, so a dollar to'lov is reportable in
    so'm too — the gap that made every pre-existing dollar row unconvertible."""
    c = _contract(db)
    admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "usd", "amount": "100", "exchange_rate": "12650",
            "method": "cash", "note": ""},
                              contract=c.pk, date="2026-07-02"))
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("100.00")
    assert p.amount_uzs == Decimal("1265000")


def test_a_cross_currency_entry_without_a_kurs_is_rejected(admin_client, db):
    """Paying a dollar kelishuv in so'm: without a kurs there is no way to say how
    much of the qarz the money cleared, so the row is refused rather than guessed."""
    c = _contract(db)
    resp = admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "uzs", "amount": "1265000", "exchange_rate": "",
            "method": "cash", "note": ""},
                              contract=c.pk, date="2026-07-02"))
    assert resp.status_code == 200          # redisplayed, not saved
    assert SupplierPayment.objects.count() == 0


def test_paying_a_kelishuv_in_its_own_currency_asks_for_no_kurs(admin_client, db):
    """Same currency in and out — the summa IS what the qarz falls by. The row still
    ends up with a kurs so the kassa's other column has something to add up."""
    c = _contract(db)
    resp = admin_client.post(
        "/supplier-payments/new/",
        supplier_payment_rows({"currency": "usd", "amount": "100", "exchange_rate": "",
            "method": "cash", "note": ""},
                              contract=c.pk, date="2026-07-02"))
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
        supplier_payment_rows({"currency": "usd", "amount": "400", "exchange_rate": "12000",
                               "method": "transfer", "note": ""},
                              contract=c.pk, date="2026-07-02"),
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
    row = {"currency": "usd", "amount": amount, "exchange_rate": "12000",
           "commission_percent": "", "method": "cash", "note": ""}
    row.update(extra)
    return client.post("/supplier-payments/new/",
                       supplier_payment_rows(row, contract=contract, date="2026-07-23"))


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


# --- one to'lov, several ways it left --------------------------------------

class TestASplitPayment:
    """Half naqd, half perechisleniya: one settlement to the person making it, two
    movements of money to the kassa.

    Rows rather than a breakdown inside one row, because the two halves charge
    differently — the bank takes a foiz on the transfer and nothing on the cash, and
    a single row could not say which part it took it from."""

    def _pay(self, client, contract, *rows):
        return client.post("/supplier-payments/new/",
                           supplier_payment_rows(*rows, contract=contract.pk))

    def test_two_methods_in_one_go_become_two_rows(self, admin_client, db):
        c = _contract(db)
        resp = self._pay(admin_client, c,
                         {"amount": "300", "method": "cash"},
                         {"amount": "400", "method": "transfer"})
        assert resp.status_code == 302
        rows = SupplierPayment.objects.order_by("pk")
        assert [(r.method, r.amount) for r in rows] == [
            ("cash", Decimal("300.00")), ("transfer", Decimal("400.00"))]

    def test_the_kelishuv_falls_by_the_whole_settlement(self, admin_client, db):
        """Both halves settle the same qarz — 300 + 400 off a 1 000$ kelishuv."""
        c = _contract(db)
        self._pay(admin_client, c,
                  {"amount": "300", "method": "cash"},
                  {"amount": "400", "method": "card"})
        assert Contract.objects.get(pk=c.pk).paid_total == Decimal("700.00")

    def test_the_kelishuv_and_the_sana_are_asked_once(self, admin_client, db):
        c = _contract(db)
        self._pay(admin_client, c,
                  {"amount": "100", "method": "cash"},
                  {"amount": "200", "method": "transfer"})
        rows = SupplierPayment.objects.all()
        assert {r.contract_id for r in rows} == {c.pk}
        assert {str(r.date) for r in rows} == {"2026-07-02"}

    def test_only_the_transfer_half_pays_a_bank_foiz(self, admin_client, db):
        """The whole reason this is two rows: 2% of the perechisleniya and nothing
        off the naqd, which one shared foiz box could not express."""
        c = _contract(db)
        self._pay(admin_client, c,
                  {"amount": "300", "method": "cash", "fee_percent": "2"},
                  {"amount": "400", "method": "transfer", "fee_percent": "2"})
        naqd, bank = SupplierPayment.objects.order_by("pk")
        assert naqd.fee_amount == Decimal("0")           # naqd never pays one
        assert bank.fee_amount == Decimal("8.00")        # 2% of 400

    def test_each_half_can_leave_in_its_own_currency(self, admin_client, db):
        c = _contract(db)
        resp = self._pay(admin_client, c,
                         {"amount": "100", "currency": "usd", "method": "cash"},
                         {"amount": "1265000", "currency": "uzs",
                          "exchange_rate": "12650", "method": "transfer"})
        assert resp.status_code == 302
        usd_row, uzs_row = SupplierPayment.objects.order_by("pk")
        assert usd_row.amount == Decimal("100.00")
        assert uzs_row.amount_uzs == Decimal("1265000")   # typed side exact
        assert uzs_row.amount == Decimal("100.00")

    def test_the_kassa_shows_each_half_under_its_own_method(self, admin_client, db):
        """The point of the split, seen from the till: the safe is 300 lighter and
        the bank 400, rather than one 700 heap that is in neither."""
        c = _contract(db)
        self._pay(admin_client, c,
                  {"amount": "300", "method": "cash"},
                  {"amount": "400", "method": "transfer"})
        balances = admin_client.get("/kassa/?davr=all").context["balances"]
        assert balances["cash"]["out"] == Decimal("300.00")
        assert balances["transfer"]["out"] == Decimal("400.00")

    def test_the_ceiling_is_checked_over_the_whole_settlement(self, admin_client, db):
        """600 + 600 are each under a 1 000$ kelishuv and are 200 over it together.
        Checked per row this would go straight through."""
        c = _contract(db)
        resp = self._pay(admin_client, c,
                         {"amount": "600", "method": "cash"},
                         {"amount": "600", "method": "transfer"})
        assert resp.status_code == 200
        assert "oshib ketdi" in resp.context["lines"].non_form_errors()[0]
        assert not SupplierPayment.objects.exists()

    def test_nothing_is_written_when_one_row_is_bad(self, admin_client, db):
        """All of it or none: the rows are saved in one transaction, so a settlement
        cannot land half-written with the operator thinking it went in whole."""
        c = _contract(db)
        resp = self._pay(admin_client, c,
                         {"amount": "300", "method": "cash"},
                         {"amount": "-5", "method": "transfer"})
        assert resp.status_code == 200
        assert not SupplierPayment.objects.exists()

    def test_a_settlement_with_no_rows_at_all_is_refused(self, admin_client, db):
        c = _contract(db)
        resp = admin_client.post("/supplier-payments/new/",
                                 supplier_payment_rows(contract=c.pk))
        assert resp.status_code == 200
        assert not SupplierPayment.objects.exists()

    def test_one_row_still_saves_as_it_always_did(self, admin_client, db):
        """The ordinary case has not become a special case of the split one."""
        c = _contract(db)
        assert self._pay(admin_client, c,
                         {"amount": "250", "method": "cash"}).status_code == 302
        assert SupplierPayment.objects.get().amount == Decimal("250.00")


class TestTheKompaniyaCarriesTheBankFoiz:
    """The perechisleniya foizi on a hamkor to'lov is always ours to carry.

    The hamkor is owed a figure and has to RECEIVE that figure, so the bank's cut
    rides on top of it rather than out of it: $1 000 owed is $1 000 sent, $1 000
    credited, and $1 020 out of the kassa. That is the bank's own rule, not a policy
    we chose — asked per to'lov, the other answer credited the hamkor less than we
    sent and left every kelishuv paid by perechisleniya short by exactly the foiz.

    So the form no longer asks (see `SupplierPaymentForm`), and the ceiling is the
    summa typed: what leaves is what lands."""

    def _pay(self, client, contract, amount, date="2026-07-02", **extra):
        return client.post("/supplier-payments/new/", supplier_payment_rows(
            {"currency": "usd", "amount": amount, "exchange_rate": "12000",
             "method": "transfer", "fee_percent": "2", **extra},
            contract=contract.pk, date=date))

    def test_the_qarz_falls_by_the_whole_summa_sent(self, admin_client, db):
        c = _contract(db)                                    # $1 000, all shipped
        assert self._pay(admin_client, c, "1000").status_code == 302
        payment = SupplierPayment.objects.get()
        assert payment.amount == Decimal("1000.00")          # what the hamkor gets
        assert payment.credited_amount == Decimal("1000.00")  # and what they are credited
        assert payment.total_out == Decimal("1020.00")       # the foiz is ours on top
        assert c.payable_left_own == Decimal("0.00")
        assert c.is_settled

    def test_a_posted_fee_bearer_cannot_push_the_cut_onto_the_hamkor(self, admin_client, db):
        """The radios are gone from the form, so a hand-made POST still carrying the
        old value changes nothing — otherwise the rule would hold only as far as the
        browser, and the one place it matters is the figure that gets saved."""
        c = _contract(db)
        assert self._pay(admin_client, c, "1000",
                         fee_bearer="counterparty").status_code == 302
        payment = SupplierPayment.objects.get()
        assert payment.fee_bearer == ""
        assert payment.fee_on_company
        assert payment.credited_amount == Decimal("1000.00")

    def test_paying_past_the_kelishuv_is_still_refused(self, admin_client, db):
        """The ceiling is the summa itself now: $1 001 credits $1 001, a dollar more
        than the hamkor is owed."""
        c = _contract(db)
        self._pay(admin_client, c, "1000")
        resp = self._pay(admin_client, c, "1", date="2026-07-03")
        assert resp.status_code == 200
        assert SupplierPayment.objects.count() == 1

    def test_editing_a_to_lov_weighs_it_by_what_it_credited(self, admin_client, db):
        """The row being edited comes off the kelishuv before the new figure is
        checked, so raising it to the full $1 000 settles the kelishuv rather than
        reading as $1 900 against a $1 000 debt."""
        c = _contract(db)
        self._pay(admin_client, c, "900")
        payment = SupplierPayment.objects.get()
        resp = admin_client.post(f"/supplier-payments/{payment.pk}/edit/", {
            "contract": c.pk, "date": "2026-07-02", "currency": "usd",
            "amount": "1000", "exchange_rate": "12000", "commission_percent": "",
            "method": "transfer", "fee_percent": "2", "note": ""})
        assert resp.status_code == 302
        payment.refresh_from_db()
        assert payment.credited_amount == Decimal("1000.00")
        assert c.payable_left_own == Decimal("0.00")

    def test_an_edit_cannot_take_the_kelishuv_past_its_value(self, admin_client, db):
        c = _contract(db)
        self._pay(admin_client, c, "1000")
        payment = SupplierPayment.objects.get()
        resp = admin_client.post(f"/supplier-payments/{payment.pk}/edit/", {
            "contract": c.pk, "date": "2026-07-02", "currency": "usd",
            "amount": "1100", "exchange_rate": "12000", "commission_percent": "",
            "method": "transfer", "fee_percent": "2", "note": ""})
        assert resp.status_code == 200                         # 1100 > 1000
        payment.refresh_from_db()
        assert payment.amount == Decimal("1000.00")            # unchanged

    def test_a_split_to_lov_credits_both_halves_in_full(self, admin_client, db):
        """$500 naqd and $500 by perechisleniya settle a $1 000 kelishuv. The bank
        charges its foiz on the transfer half only, and that comes out of the kassa
        on top — neither half is credited any lighter for it."""
        c = _contract(db)
        resp = admin_client.post("/supplier-payments/new/", supplier_payment_rows(
            {"currency": "usd", "amount": "500", "exchange_rate": "12000",
             "method": "cash", "fee_percent": "2"},
            {"currency": "usd", "amount": "500", "exchange_rate": "12000",
             "method": "transfer", "fee_percent": "2"},
            contract=c.pk, date="2026-07-02"))
        assert resp.status_code == 302
        assert c.payable_left_own == Decimal("0.00")
        assert c.is_settled
        naqd, bank = SupplierPayment.objects.order_by("pk")
        assert naqd.total_out == Decimal("500.00")             # naqd pays no foiz
        assert bank.total_out == Decimal("510.00")             # 500 + 2%


def _two_marka_contract():
    """A kelishuv covering two markalar, both fully shipped so either can be paid."""
    contract = make_contract(brand="7000 campaund", kg="1000", price="1.00")
    second = ContractLine.objects.create(contract=contract, brand="209 campaund",
                                         kg=Decimal("1000"), price=Decimal("1.00"))
    make_shipment(contract=contract, kg="1000")
    make_shipment(contract=contract, kg="1000", contract_line=second)
    return contract, contract.lines.get(brand="7000 campaund"), second


def _pay_marka(client, contract, amount="100", line=None, **row):
    """Post a one-row hamkor to'lov naming (or not naming) the marka it paid for.

    The marka rides on the HEADER beside the kelishuv and the sana, not on the rows:
    a to'lov split between naqd and perechisleniya is one delivery settled two ways,
    so it is asked once (see `SupplierPaymentTargetForm`)."""
    return client.post("/supplier-payments/new/", supplier_payment_rows(
        {"currency": "usd", "amount": amount, "exchange_rate": "12000",
         "commission_percent": "", "method": "cash", "note": "", **row},
        contract=contract.pk, date="2026-07-02", contract_line=line))


def test_a_multi_marka_tolov_must_name_the_product(admin_client, db):
    """"Paid $96 400 of $288 000" said nothing about WHICH marka the money went
    to, and a kelishuv covering two of them is two deliveries sharing a piece of
    paper."""
    contract, first, _second = _two_marka_contract()

    resp = _pay_marka(admin_client, contract)
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()
    assert "contract_line" in resp.context["form"].errors

    resp = _pay_marka(admin_client, contract, line=first)
    assert resp.status_code == 302
    assert SupplierPayment.objects.get().contract_line_id == first.pk


def test_a_one_marka_kelishuv_fills_the_product_in_by_itself(admin_client, db):
    """Nothing to choose: one product IS the kelishuv, so the operator is not asked
    and the to'lov still records which marka it bought."""
    contract = _contract(db)
    resp = _pay_marka(admin_client, contract)
    assert resp.status_code == 302
    assert SupplierPayment.objects.get().contract_line_id == contract.lines.get().pk


def test_every_row_of_a_split_tolov_carries_the_same_marka(admin_client, db):
    """Asked once, stamped on both halves — otherwise the naqd half would say which
    delivery it paid for and the bank half would say nothing."""
    contract, first, _second = _two_marka_contract()
    resp = admin_client.post("/supplier-payments/new/", supplier_payment_rows(
        {"currency": "usd", "amount": "300", "exchange_rate": "12000",
         "method": "cash", "note": ""},
        {"currency": "usd", "amount": "400", "exchange_rate": "12000",
         "method": "transfer", "note": ""},
        contract=contract.pk, date="2026-07-02", contract_line=first.pk))
    assert resp.status_code == 302
    rows = SupplierPayment.objects.order_by("pk")
    assert [r.contract_line_id for r in rows] == [first.pk, first.pk]


def test_a_product_from_another_kelishuv_is_refused(admin_client, db):
    """The client-side filter is a convenience, not the authority — a stale page
    can still post a product that belongs to somebody else's kelishuv."""
    contract, _first, _second = _two_marka_contract()
    stranger = make_contract(brand="Boshqa", kg="1000", price="1.00")

    resp = _pay_marka(admin_client, contract, line=stranger.lines.get())
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()
    assert "contract_line" in resp.context["form"].errors


def test_the_qarz_is_still_settled_per_kelishuv(admin_client, db):
    """Naming the product records where the money went; it does not split what is
    owed. The ceiling this form checks is still the whole kelishuv's."""
    contract, first, _second = _two_marka_contract()
    # 1 500 is more than the 1 000 this marka is worth, and inside the 2 000 the
    # kelishuv is worth — so it is accepted, as it was before the field existed.
    resp = _pay_marka(admin_client, contract, amount="1500", line=first)
    assert resp.status_code == 302
    assert contract.paid_total == Decimal("1500")


def test_the_product_box_offers_no_whole_kelishuv_escape(db):
    """It read "Butun kelishuv" at first — a choice `clean` then refused, so the
    box named an option the form would not accept. On a kelishuv with two markalar
    the money went to one of them, and a to'lov nobody attributes on the day is one
    nobody can attribute later either."""
    from crm.forms import SupplierPaymentForm
    make_contract(kg="1000", price="1.00")
    field = SupplierPaymentForm().fields["contract_line"]
    assert field.empty_label == "Mahsulotni tanlang"
    assert "Butun kelishuv" not in [str(label) for _value, label in field.choices]
