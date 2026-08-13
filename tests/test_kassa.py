from decimal import Decimal

from crm.models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, Partner, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus, SupplierPayment,
)


def _contract(partner_name="Pars"):
    partner = Partner.objects.create(name=partner_name, phone="1", city="T")
    _contract_obj = Contract.objects.create(partner=partner, created="2026-07-01")
    _contract_obj_line = ContractLine.objects.create(
        contract=_contract_obj, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"))
    return _contract_obj


def _arrived_shipment(contract):
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal("500"))
    return _ship_obj


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def test_cash_balance_nets_in_and_out(admin_client, db):
    contract = _contract()
    shipment = _arrived_shipment(contract)
    customer = _customer()

    CustomerPayment.objects.create(
        customer=customer, date="2026-07-10", amount=Decimal("500.00"), method="cash",
    )
    SupplierPayment.objects.create(
        contract=contract, date="2026-07-11", amount=Decimal("200.00"), method="cash",
    )
    ShipmentExpense.objects.create(
        shipment=shipment, date="2026-07-12", category="transport", amount=Decimal("100.00"), method="cash",
    )

    resp = admin_client.get("/kassa/")
    assert resp.status_code == 200
    balances = resp.context["balances"]
    assert balances["cash"]["balance"] == Decimal("200.00")
    assert resp.context["net_total"] == Decimal("200.00")

    html = resp.content.decode()
    assert "200.00" in html


def test_bank_payment_shows_under_bank_and_cash_unaffected(admin_client, db):
    contract = _contract()
    customer = _customer()

    CustomerPayment.objects.create(
        customer=customer, date="2026-07-10", amount=Decimal("300.00"), method="transfer",
    )

    resp = admin_client.get("/kassa/")
    balances = resp.context["balances"]
    assert balances["transfer"]["in"] == Decimal("300.00")
    assert balances["transfer"]["balance"] == Decimal("300.00")
    assert balances["cash"]["balance"] == Decimal("0.00")


def test_date_filter_excludes_out_of_range_payment(admin_client, db):
    customer = _customer()

    CustomerPayment.objects.create(
        customer=customer, date="2026-05-01", amount=Decimal("150.00"), method="cash",
    )
    CustomerPayment.objects.create(
        customer=customer, date="2026-07-10", amount=Decimal("500.00"), method="cash",
    )

    resp = admin_client.get("/kassa/", {"from": "2026-07-01", "to": "2026-07-31"})
    balances = resp.context["balances"]
    assert balances["cash"]["in"] == Decimal("500.00")

    income_dates = [r["date"].isoformat() for r in resp.context["income_page"]]
    assert "2026-05-01" not in income_dates
    assert "2026-07-10" in income_dates


def test_translator_forbidden(translator_client, db):
    assert translator_client.get("/kassa/").status_code == 403


def test_kassa_shows_partner_payables_from_shipped_trucks(admin_client, db):
    """The Kassa surfaces what we owe hamkorlar right now: Σ per contract of
    shipped value − paid, grouped by partner."""
    from crm.models import Contract, Partner, Shipment, ShipmentStatus, SupplierPayment

    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    c = Contract.objects.create(partner=partner, created="2026-07-01")
    c_line = ContractLine.objects.create(
        contract=c, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1.00"),
        price_uzs=Decimal("12000"))
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first())
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("600"))   # owe 600
    SupplierPayment.objects.create(contract=c, date="2026-07-02",
                                   amount=Decimal("250"), amount_uzs=Decimal("3000000"),
                                   method="cash")                    # paid 250
    resp = admin_client.get("/kassa/")
    assert resp.context["payable_total"] == Decimal("350.00")
    # each partner carries (dollar debt, so'm debt) so the screen can show either
    debts = {p.name: d for p, d in resp.context["partner_debts"]}
    assert debts == {"Pars": (Decimal("350.00"), Decimal("4200000.00"))}
    assert "Hamkorlarga qarzimiz" in resp.content.decode()


def test_kassa_kirim_chiqim_ledgers_and_cash_hero(admin_client, db):
    """Client-crm style: an all-time Kassadagi pul hero plus separate Kirim and
    Chiqim ledgers (customer payments in; hamkor payments + yuk expenses out)."""
    contract = _contract()
    shipment = _arrived_shipment(contract)
    customer = _customer()
    CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                   amount=Decimal("500.00"), method="cash")
    SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                   amount=Decimal("200.00"), method="cash")
    ShipmentExpense.objects.create(shipment=shipment, date="2026-07-12",
                                   category="transport", amount=Decimal("100.00"),
                                   method="cash")
    # the hero is all-time even when the filter excludes some rows
    resp = admin_client.get("/kassa/", {"from": "2026-07-11", "to": "2026-07-12"})
    assert resp.context["cash_total"] == Decimal("200.00")   # 500 - 200 - 100
    assert [p.amount for p in resp.context["income_page"]] == []      # 07-10 outside range
    kinds = [(r["kind"], r["amount"]) for r in resp.context["outflow_page"]]
    assert kinds == [("expense", Decimal("100.00")), ("supplier", Decimal("200.00"))]
    html = resp.content.decode()
    assert "Kirim" in html and "Chiqim" in html and "Kassada" in html


def test_kassa_income_ledger_paginates_at_20(admin_client, db):
    """Kirim ledger pages at 20; the +total still counts the whole period."""
    customer = _customer()
    for _ in range(25):
        CustomerPayment.objects.create(
            customer=customer, date="2026-07-10", amount=Decimal("10.00"), method="cash")

    resp = admin_client.get("/kassa/")
    page = resp.context["income_page"]
    assert page.paginator.per_page == 20
    assert len(page.object_list) == 20
    assert page.paginator.count == 25
    assert resp.context["net_in"] == Decimal("250.00")   # totals ignore paging

    page2 = admin_client.get("/kassa/?ipage=2").context["income_page"]
    assert page2.number == 2
    assert len(page2.object_list) == 5


def test_kassa_outflow_ledger_paginates_at_20(admin_client, db):
    """Chiqim ledger pages at 20 under its own ?opage param."""
    contract = _contract()
    for _ in range(25):
        SupplierPayment.objects.create(
            contract=contract, date="2026-07-11", amount=Decimal("5.00"), method="cash")

    resp = admin_client.get("/kassa/")
    page = resp.context["outflow_page"]
    assert page.paginator.per_page == 20
    assert len(page.object_list) == 20
    assert page.paginator.count == 25

    page2 = admin_client.get("/kassa/?opage=2").context["outflow_page"]
    assert page2.number == 2
    assert len(page2.object_list) == 5


def test_kassa_two_ledgers_page_independently(admin_client, db):
    """Paging one ledger doesn't move the other — separate ipage/opage params."""
    contract = _contract()
    customer = _customer()
    for _ in range(25):
        CustomerPayment.objects.create(
            customer=customer, date="2026-07-10", amount=Decimal("10.00"), method="cash")
        SupplierPayment.objects.create(
            contract=contract, date="2026-07-11", amount=Decimal("5.00"), method="cash")

    ctx = admin_client.get("/kassa/?ipage=2").context
    assert ctx["income_page"].number == 2
    assert ctx["outflow_page"].number == 1   # untouched

    ctx = admin_client.get("/kassa/?ipage=2&opage=2").context
    assert ctx["income_page"].number == 2
    assert ctx["outflow_page"].number == 2


class TestCashOwnership:
    """The till holds money that is not ours: a to'lov sitting on no sotuv is the
    mijoz's avans, and cancelling their order sends it back out."""

    def test_unallocated_payment_is_reported_as_held_not_earned(self, admin_client, db):
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("500.00"), method="cash")
        ctx = admin_client.get("/kassa/").context
        assert ctx["cash_total"] == Decimal("500.00")
        assert ctx["advance"] == Decimal("500.00")
        assert ctx["own_cash"] == Decimal("0.00")

    def test_money_that_paid_a_sotuv_is_ours(self, admin_client, db):
        from crm.models import Sale, allocate_customer_payment
        contract = _contract()
        shipment = _arrived_shipment(contract)
        customer = _customer()
        Sale.objects.create(customer=customer, line=shipment.lines.first(),
                            kg=Decimal("300"), price=Decimal("1.00"),
                            price_uzs=Decimal("12000"), date="2026-07-17")
        payment = CustomerPayment.objects.create(
            customer=customer, date="2026-07-18", amount=Decimal("500.00"),
            method="cash")
        # allocation is its own step — creating a to'lov does not spend it
        allocate_customer_payment(payment)
        ctx = admin_client.get("/kassa/").context
        # 300 of the 500 landed on the sotuv; 200 is still the mijoz's
        assert ctx["advance"] == Decimal("200.00")
        assert ctx["own_cash"] == ctx["cash_total"] - Decimal("200.00")

    def test_the_bank_foiz_never_becomes_an_advance(self, admin_client, db):
        """A perechisleniya foiz never reached us, so it cannot be money we hold."""
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("1000.00"), method="transfer",
                                       fee_percent=Decimal("2"))
        ctx = admin_client.get("/kassa/").context
        assert ctx["advance"] == Decimal("980.00")
        assert ctx["cash_total"] == Decimal("980.00")


class TestCurrentStateTiles:
    """Hozirgi holat is a board of facts — one tile per place money or goods sits.
    Nothing is summed across them: cash plus granula plus somebody else's unpaid
    invoice is a number that describes no actual thing."""

    def _tiles(self, admin_client):
        return {t["label"]: t for t in admin_client.get("/kassa/").context["tiles"]}

    def _own(self, tile, currency=Currency.USD):
        """One currency's line off a split tile.

        A split drops the sides that net to zero, so a missing side is not missing
        data — it is the answer, and it is zero."""
        return dict(tile["split"]).get(currency, Decimal("0"))

    def test_every_tile_states_a_current_fact_and_links_somewhere(self, admin_client, db):
        contract = _contract()
        shipment = _arrived_shipment(contract)
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("500.00"), method="cash")
        SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                       amount=Decimal("200.00"), method="cash")
        tiles = self._tiles(admin_client)
        assert list(tiles) == ["Kassada", "Mijozlar qarzi", "Omborda", "Yo'lda",
                               "Hamkorlarda avansimiz", "Logistlarda", "Bojxonada",
                               "Hamkorlarga qarzimiz"]
        for tile in tiles.values():
            assert tile["note"], tile["label"]
            assert tile["url"].startswith("/"), tile["label"]
        assert self._own(tiles["Kassada"]) == resp_cash(admin_client)

    def test_no_derived_total_is_published(self, admin_client, db):
        """Sof holat is gone: it summed things that are not the same kind of thing."""
        ctx = admin_client.get("/kassa/").context
        assert "net_position" not in ctx
        assert "Sof holat" not in admin_client.get("/kassa/").content.decode()

    def test_stock_and_transit_tiles_carry_their_kg(self, admin_client, db):
        contract = _contract()
        _arrived_shipment(contract)                       # 500 kg arrived
        moving = Shipment.objects.create(
            contract=contract, status=ShipmentStatus.objects.exclude(is_arrival=True).first(),
            sent="2026-07-20", eta="2026-08-01")
        ShipmentLine.objects.create(shipment=moving, contract_line=contract.lines.first(),
                                    kg=Decimal("300"))
        tiles = self._tiles(admin_client)
        assert "500 kg" in tiles["Omborda"]["meta"]
        assert "300 kg" in tiles["Yo'lda"]["meta"]
        assert "1 ta yuk" in tiles["Yo'lda"]["meta"]

    def test_debtor_count_rides_with_the_receivable(self, admin_client, db):
        from crm.models import Sale
        contract = _contract()
        shipment = _arrived_shipment(contract)
        for name in ("Bir", "Ikki"):
            Sale.objects.create(customer=_customer(name), line=shipment.lines.first(),
                                kg=Decimal("100"), price=Decimal("1.00"),
                                price_uzs=Decimal("12000"), date="2026-07-17")
        tiles = self._tiles(admin_client)
        assert self._own(tiles["Mijozlar qarzi"]) == Decimal("200.00")
        assert tiles["Mijozlar qarzi"]["meta"] == "2 ta mijozda"

    def test_customer_advance_is_named_on_the_kassa_tile(self, admin_client, db):
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("500.00"), method="cash")
        assert "avansi" in self._tiles(admin_client)["Kassada"]["meta"]

    def test_prepaid_kelishuv_is_its_own_tile_not_a_smaller_qarz(self, admin_client, db):
        """The July bug: 5 kelishuv prepaid by $203 030.5 sat behind a $50 480
        payable because only positive debts were counted."""
        contract = _contract()              # 1000 kg @ $1, nothing shipped yet
        SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                       amount=Decimal("600.00"), method="cash")
        tiles = self._tiles(admin_client)
        assert self._own(tiles["Hamkorlarda avansimiz"]) == Decimal("600.00")
        assert tiles["Hamkorlarda avansimiz"]["meta"] == "1 ta kelishuvda"
        assert tiles["Hamkorlarga qarzimiz"]["split"] == []

    def test_both_hamkor_directions_can_be_non_zero_at_once(self, admin_client, db):
        owing = _contract("Qarzdor")
        _arrived_shipment(owing)                       # 500 kg shipped, nothing paid
        prepaid = _contract("Avansli")
        SupplierPayment.objects.create(contract=prepaid, date="2026-07-11",
                                       amount=Decimal("300.00"), method="cash")
        tiles = self._tiles(admin_client)
        assert self._own(tiles["Hamkorlarga qarzimiz"]) == Decimal("500.00")
        assert self._own(tiles["Hamkorlarda avansimiz"]) == Decimal("300.00")

    def test_the_till_counts_each_row_on_one_side_only(self, admin_client, db):
        """The bug this tile was split to kill: the pair beside it sums BOTH stored
        columns of every row, so a dollar to'lov also lands in the so'm figure as its
        converted twin. On the real database that reported 9 313 mln so'm in a till
        holding 87.8 mln. Here $1 000 arrives in dollars and 2.5 mln in so'm, and
        neither side may borrow from the other."""
        customer = _customer()
        CustomerPayment.objects.create(
            customer=customer, date="2026-07-20", amount=Decimal("1000"),
            amount_uzs=Decimal("12500000"), exchange_rate=Decimal("12500"),
            currency=Currency.USD, method="cash")
        CustomerPayment.objects.create(
            customer=customer, date="2026-07-21", amount=Decimal("200"),
            amount_uzs=Decimal("2500000"), exchange_rate=Decimal("12500"),
            currency=Currency.UZS, method="cash")

        till = self._tiles(admin_client)["Kassada"]
        assert self._own(till, Currency.USD) == Decimal("1000.00")
        assert self._own(till, Currency.UZS) == Decimal("2500000.00")
        # The dollar row's twin is 12.5 mln; if it leaked in, the so'm side would be 15.
        assert self._own(till, Currency.UZS) != Decimal("15000000.00")

    def test_a_hamkor_owed_in_both_currencies_reads_as_two_debts(self, admin_client, db):
        """Never one converted total: the two kelishuv were struck at different
        kurslar and settled in different money, so they stay two figures."""
        dollars = _contract("Dollarli")
        _arrived_shipment(dollars)                     # 500 kg @ $1, nothing paid
        sums = _contract("So'mli")
        sums.currency = Currency.UZS
        sums.exchange_rate = Decimal("12500")
        sums.save()
        line = sums.lines.first()
        line.currency, line.price_uzs = Currency.UZS, Decimal("12500")
        line.exchange_rate = Decimal("12500")
        line.save()
        _arrived_shipment(sums)                        # 500 kg @ 12 500 so'm

        owed = dict(self._tiles(admin_client)["Hamkorlarga qarzimiz"]["split"])
        assert owed[Currency.USD] == Decimal("500.00")
        assert owed[Currency.UZS] == Decimal("6250000.00")

    def test_a_cost_tile_shows_its_dollar_figure_alone(self, admin_client, db):
        """Omborda is a blended COST — a kg has ONE landed price even though the mol
        was bought in dollars and the transport paid in so'm — so it is kept in
        dollars and has no split to draw.

        It used to print the so'm twin beneath the dollar figure. Nobody ever handed
        that so'm over: it is the same money restated at a kurs no single lot agreed,
        and beside the real so'm heaps on this page it read as a second pile. The
        pair is still ON the tile (the reports need it); the page does not show it."""
        from crm.templatetags.crm_extras import som
        _arrived_shipment(_contract())
        ombor = self._tiles(admin_client)["Omborda"]
        assert ombor["split"] is None
        assert ombor["amount"] > 0
        assert ombor["amount_uzs"] > 0
        body = admin_client.get("/kassa/").content.decode()
        assert som(ombor["amount"]) not in body
        assert som(ombor["amount_uzs"]) not in body

    def test_an_empty_currency_side_is_drawn_as_a_zero(self, admin_client, db):
        """A missing currency line read as missing DATA. Both sides are drawn now, and
        a side that holds nothing says so: "0 so'm" beside a dollar figure.

        A cost tile is the exception and gets no zero: Omborda is kept in dollars and
        the mol genuinely did cost so'm — that figure is a conversion this page does
        not print, so a zero there would be a lie rather than an empty side."""
        contract = _contract()                              # a dollar kelishuv
        _arrived_shipment(contract)
        CustomerPayment.objects.create(customer=_customer(), date="2026-07-10",
                                       amount=Decimal("500.00"), method="cash")
        tiles = self._tiles(admin_client)
        # Nothing has been sent to a bojxonachi, so both sides of that one are empty.
        assert tiles["Bojxonada"]["split"] == []
        assert tiles["Bojxonada"]["split_full"] == [
            (Currency.USD, Decimal("0")), (Currency.UZS, Decimal("0"))]
        assert dict(tiles["Kassada"]["split_full"])[Currency.UZS] == Decimal("0")
        assert "split_full" not in tiles["Omborda"]
        assert "0 so&#x27;m" in admin_client.get("/kassa/").content.decode()

    def test_the_till_is_the_hero_and_the_rest_read_under_a_heading(self, admin_client, db):
        """Ten equal tiles ranked nothing — a debt that is usually zero pulled as hard
        as the money on hand. Kassada is the hero; every other tile sits in a group."""
        contract = _contract()
        _arrived_shipment(contract)
        ctx = admin_client.get("/kassa/").context
        assert ctx["hero"]["label"] == "Kassada"
        assert ctx["hero"]["group"] is None
        # A set, not a list: the groups deliberately reorder — Mol comes first on the
        # page while the flat list still leads with Mijozlar qarzi. What matters is
        # that nothing falls between the groups and nothing is drawn twice.
        grouped = [t["label"] for g in ctx["tile_groups"] for t in g["tiles"]]
        assert len(grouped) == len(set(grouped))
        assert set(grouped) == {t["label"] for t in ctx["tiles"]} - {"Kassada"}
        assert [g["title"] for g in ctx["tile_groups"]] == [
            "Mol — tannarxda", "Bizga qaytadigan pul", "Qarzlarimiz"]


def resp_cash(admin_client):
    return admin_client.get("/kassa/").context["cash_total"]


class TestWaterfall:
    def _rows(self, ctx):
        return {b["label"]: b["amount"] for b in ctx["waterfall"]}

    def test_it_closes_on_the_cash_total(self, admin_client, db):
        contract = _contract()
        shipment = _arrived_shipment(contract)
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("500.00"), method="cash")
        SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                       amount=Decimal("200.00"), method="cash",
                                       commission_percent=Decimal("2"))
        ShipmentExpense.objects.create(shipment=shipment, date="2026-07-12",
                                       category="customs", amount=Decimal("100.00"),
                                       method="cash")
        ctx = admin_client.get("/kassa/").context
        closing = ctx["waterfall"][-1]
        assert closing["label"] == "Qoldiq"
        assert closing["running"] == ctx["cash_total"]
        assert closing["running_uzs"] == ctx["cash_total_uzs"]
        # and the steps between are the ledger, decomposed
        rows = self._rows(ctx)
        assert rows["Mijozlardan"] == Decimal("500.00")
        assert rows["Hamkorlarga"] == Decimal("-200.00")
        assert rows["Vositachi ustamasi"] == Decimal("-4.00")
        assert rows["Bojxona"] == Decimal("-100.00")

    def test_steps_sum_to_the_ledger_totals(self, admin_client, db):
        contract = _contract()
        shipment = _arrived_shipment(contract)
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("900.00"), method="cash")
        SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                       amount=Decimal("200.00"), method="transfer",
                                       fee_percent=Decimal("1"))
        ShipmentExpense.objects.create(shipment=shipment, date="2026-07-12",
                                       category="transport", amount=Decimal("50.00"),
                                       method="cash")
        ctx = admin_client.get("/kassa/").context
        steps = [b for b in ctx["waterfall"] if b["kind"] != "total"]
        assert sum(b["amount"] for b in steps if b["kind"] == "in") == ctx["net_in"]
        assert -sum(b["amount"] for b in steps if b["kind"] == "out") == ctx["net_out"]

    def test_a_customer_foiz_is_not_billed_again_as_an_outflow(self, admin_client, db):
        """net_in is already net of it — a Bank foizi step here would spend it twice."""
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("1000.00"), method="transfer",
                                       fee_percent=Decimal("2"))
        ctx = admin_client.get("/kassa/").context
        assert self._rows(ctx)["Mijozlardan"] == Decimal("980.00")
        assert "Bank foizi" not in self._rows(ctx)
        assert ctx["waterfall"][-1]["running"] == Decimal("980.00")

    def test_an_outgoing_foiz_gets_its_own_step(self, admin_client, db):
        contract = _contract()
        SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                       amount=Decimal("1000.00"), method="transfer",
                                       fee_percent=Decimal("2"))
        ctx = admin_client.get("/kassa/").context
        assert self._rows(ctx)["Bank foizi"] == Decimal("-20.00")
        assert ctx["waterfall"][-1]["running"] == Decimal("-1020.00")

    def test_opening_balance_carries_the_period_boundary(self, admin_client, db):
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-06-01",
                                       amount=Decimal("400.00"), method="cash")
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("100.00"), method="cash")
        ctx = admin_client.get("/kassa/", {"from": "2026-07-01", "to": "2026-07-31"}).context
        opening = ctx["waterfall"][0]
        assert opening["label"] == "Boshlang'ich qoldiq"
        assert opening["amount"] == Decimal("400.00")
        assert ctx["waterfall"][-1]["running"] == Decimal("500.00") == ctx["cash_total"]

    def test_unfiltered_opens_at_zero(self, admin_client, db):
        _customer()
        ctx = admin_client.get("/kassa/").context
        assert ctx["waterfall"][0]["amount"] == Decimal("0")

    def test_bars_stay_inside_the_track(self, admin_client, db):
        contract = _contract()
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("500.00"), method="cash")
        SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                       amount=Decimal("900.00"), method="cash")
        ctx = admin_client.get("/kassa/").context
        for bar in ctx["waterfall"]:
            assert bar["left"] >= -0.001, bar["label"]
            assert bar["left"] + bar["width"] <= 100.001, bar["label"]
        assert 0 <= ctx["zero_line"] <= 100

    def test_empty_period_does_not_divide_by_zero(self, admin_client, db):
        ctx = admin_client.get("/kassa/", {"from": "2030-01-01", "to": "2030-01-31"}).context
        assert ctx["waterfall"][-1]["running"] == Decimal("0")
        for bar in ctx["waterfall"]:
            assert 0 <= bar["left"] <= 100


class TestLedgerColumns:
    """Kirim and Chiqim answer, per row, what happened to the money on the way
    through: what was taken off it, what it settled, and what is still ours to
    hand back."""

    def _sale(self, customer, lot, kg="100", price="1.00", **kwargs):
        from crm.models import Sale
        return Sale.objects.create(customer=customer, line=lot, kg=Decimal(kg),
                                   price=Decimal(price), date="2026-07-17", **kwargs)

    def test_the_ledger_headline_is_one_heap_per_currency(self, admin_client, db):
        """The Kirim/Chiqim totals used to be a dollar figure with a so'm twin, and the
        dollar figure counted the so'm rows too — restated at whatever kurs each one
        carried. A so'm to'lov is not dollars that arrived, so each currency is summed
        in itself and printed on its own line."""
        customer = _customer()
        CustomerPayment.objects.create(customer=customer, date="2026-07-10",
                                       amount=Decimal("100.00"),
                                       amount_uzs=Decimal("1200000"),
                                       exchange_rate=Decimal("12000"),
                                       currency=Currency.USD, method="cash")
        CustomerPayment.objects.create(customer=customer, date="2026-07-11",
                                       amount=Decimal("200.00"),
                                       amount_uzs=Decimal("2400000"),
                                       exchange_rate=Decimal("12000"),
                                       currency=Currency.UZS, method="cash")
        ctx = admin_client.get("/kassa/").context
        assert ctx["income_split"] == [(Currency.USD, Decimal("100.00")),
                                       (Currency.UZS, Decimal("2400000.00"))]
        # The converted pair still exists for the Oqim waterfall — it just is not what
        # the headline says any more.
        assert ctx["net_in"] == Decimal("300.00")

    def test_kurs_is_shown_only_where_it_was_chosen(self, admin_client, db):
        """Rule 4 on screen. A to'lov in the sotuv's own currency needs no kurs —
        the one stored on it was inherited from whatever anybody typed last, so
        printing it would dress an irrelevant number up as a fact about this row."""
        from crm.models import allocate_customer_payment
        contract = _contract()
        lot = _arrived_shipment(contract).lines.first()
        customer = _customer()
        self._sale(customer, lot)                       # a $100 sotuv
        same = CustomerPayment.objects.create(
            customer=customer, date="2026-07-20", amount=Decimal("100"),
            amount_uzs=Decimal("1200000"), exchange_rate=Decimal("12000"),
            currency=Currency.USD, method="cash")
        allocate_customer_payment(same)
        assert same.crosses_currency is False

        crossing = CustomerPayment.objects.create(
            customer=customer, date="2026-07-21", amount=Decimal("100"),
            amount_uzs=Decimal("1250000"), exchange_rate=Decimal("12500"),
            currency=Currency.UZS, method="cash")
        self._sale(customer, lot, price="2.00")         # another dollar sotuv
        allocate_customer_payment(crossing)
        assert crossing.crosses_currency is True

        html = admin_client.get("/kassa/").content.decode()
        assert "Foiz / kurs" in html
        assert "12 500" in html.replace(" ", " ")   # the crossing row's kurs

    def test_what_settled_and_what_stayed_avans_are_named_apart(self, admin_client, db):
        """A to'lov bigger than the qarz clears what there is; the rest is money we
        are HOLDING. One figure cannot say both, so both are named — on the to'lov's
        own detail card, since the Kirim ledger no longer carries the column."""
        from crm.models import allocate_customer_payment
        contract = _contract()
        lot = _arrived_shipment(contract).lines.first()
        customer = _customer()
        self._sale(customer, lot, kg="100", price="1.00")        # owes $100
        payment = CustomerPayment.objects.create(
            customer=customer, date="2026-07-20", amount=Decimal("250"),
            amount_uzs=Decimal("3000000"), exchange_rate=Decimal("12000"),
            currency=Currency.USD, method="cash")
        allocate_customer_payment(payment)

        assert payment.allocated_own == Decimal("100.00")
        assert payment.unspent_own == Decimal("150.00")
        assert payment.net_amount_own == Decimal("250.00")
        html = admin_client.get(f"/customer-payments/{payment.pk}/").content.decode()
        assert "avans" in html

    def test_a_foiz_is_taken_off_before_anything_is_settled(self, admin_client, db):
        """The bank's cut never reached us, so it cannot pay down a qarz either."""
        from crm.models import allocate_customer_payment
        contract = _contract()
        lot = _arrived_shipment(contract).lines.first()
        customer = _customer()
        self._sale(customer, lot, kg="1000", price="1.00")       # owes $1 000
        payment = CustomerPayment.objects.create(
            customer=customer, date="2026-07-20", amount=Decimal("500"),
            amount_uzs=Decimal("6000000"), exchange_rate=Decimal("12000"),
            currency=Currency.USD, method="transfer", fee_percent=Decimal("2"))
        allocate_customer_payment(payment)

        assert payment.net_amount == Decimal("490.00")           # 500 − 2%
        assert payment.allocated_own == Decimal("490.00")
        assert payment.unspent_own == Decimal("0")


class TestPaymentDetail:
    def test_it_lists_the_sotuvlar_the_tolov_paid_down(self, admin_client, db):
        from crm.models import Sale, allocate_customer_payment
        contract = _contract()
        lot = _arrived_shipment(contract).lines.first()
        customer = _customer()
        Sale.objects.create(customer=customer, line=lot, kg=Decimal("100"),
                            price=Decimal("1.00"), date="2026-07-17")
        payment = CustomerPayment.objects.create(
            customer=customer, date="2026-07-20", amount=Decimal("250"),
            amount_uzs=Decimal("3000000"), exchange_rate=Decimal("12000"),
            currency=Currency.USD, method="cash")
        allocate_customer_payment(payment)

        resp = admin_client.get(f"/customer-payments/{payment.pk}/",
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "Qaysi sotuvlarga tushdi" in html
        assert "LLDPE" in html                       # the marka it was sold as
        assert "Qarzga ta&#x27;sir" in html or "Qarzga ta'sir" in html

    def test_it_still_renders_as_a_full_page_without_ajax(self, admin_client, db):
        customer = _customer()
        payment = CustomerPayment.objects.create(
            customer=customer, date="2026-07-20", amount=Decimal("100"),
            amount_uzs=Decimal("1200000"), method="cash")
        resp = admin_client.get(f"/customer-payments/{payment.pk}/")
        assert resp.status_code == 200
        html = resp.content.decode()
        # Nothing on it yet, so the table says so rather than showing an empty grid.
        assert "hammasi avans" in html
