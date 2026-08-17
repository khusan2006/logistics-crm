from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from conftest import make_contract, make_lot, make_shipment
from crm.models import (Contract, ContractLine, Customer, Partner, Sale, Shipment,
                        ShipmentLine, ShipmentStatus, SupplierPayment)


def test_dashboard_kpis(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    c = Contract.objects.create(partner=partner, created="2026-07-01")
    c_line = ContractLine.objects.create(
        contract=c, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1"))
    _ship_obj = Shipment.objects.create(contract=c, status=ShipmentStatus.objects.first(), eta=date.today() - timedelta(days=2))
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=c.lines.first(), kg=Decimal("400"))
    html = admin_client.get("/").content.decode()
    assert "kechikdi" in html.lower()
    assert "LLDPE" in html


def test_translator_redirected(translator_client):
    resp = translator_client.get("/")
    assert resp.status_code == 302 and resp.url == "/shipments/"


def _lot(contract, kg, sent, arrived=None, price=None):
    return make_shipment(contract=contract, kg=kg, price=price, sent=sent, arrived=arrived,
                         status=ShipmentStatus.arrival() if arrived else ShipmentStatus.objects.first())


def test_monthly_rows_count_trucks_sent_and_arrived(admin_client, db):
    """Oylik hisobot: jo'natilgan oy bo'yicha, yetib kelgan esa kelgan oy bo'yicha
    sanaladi — bitta yuk ikki xil oyga tushishi mumkin."""
    c = make_contract(kg="100000", price="1.00")
    _lot(c, "1000", sent="2026-06-20", arrived="2026-07-02")   # iyunda ketdi, iyulda keldi
    _lot(c, "2000", sent="2026-07-05", arrived="2026-07-20")
    _lot(c, "3000", sent="2026-07-28")                         # hali yo'lda

    rows = {r["month"]: r for r in admin_client.get("/").context["monthly"]}
    june, july = rows[date(2026, 6, 1)], rows[date(2026, 7, 1)]
    assert (june["sent"], june["arrived"]) == (1, 0)
    assert (july["sent"], july["arrived"]) == (2, 2)
    assert july["kg"] == Decimal("3000")                       # faqat kelganlari
    assert july["value"] == Decimal("3000.00")


def test_monthly_rows_are_newest_first_and_skip_empty_months(admin_client, db):
    c = make_contract(kg="100000", price="1.00")
    _lot(c, "1000", sent="2026-05-10", arrived="2026-05-15")
    _lot(c, "1000", sent="2026-07-10", arrived="2026-07-15")

    months = [r["month"] for r in admin_client.get("/").context["monthly"]]
    assert months == [date(2026, 7, 1), date(2026, 5, 1)]       # iyun umuman yo'q


def test_monthly_table_renders(admin_client, db):
    c = make_contract(kg="100000", price="1.00")
    _lot(c, "1000", sent="2026-07-10", arrived="2026-07-15")
    html = admin_client.get("/").content.decode()
    assert "Oylik" in html


def test_hamkor_qarzi_covers_every_kelishuv_not_just_shipped_goods(admin_client, db):
    """Dashboarddagi Hamkor qarzi butun kelishuv bo'yicha qoladigan to'lovni
    ko'rsatadi — faqat yo'lga chiqqan yuklarni emas."""
    c = make_contract(kg="1000", price="1.00")          # jami 1 000$
    make_shipment(contract=c, kg="200")                 # 200$ yuborildi

    resp = admin_client.get("/")
    assert c.debt == Decimal("200")                     # yuborilgani bo'yicha
    assert resp.context["debt_split"] == [("usd", Decimal("1000"))]   # butun kelishuv bo'yicha


def test_hamkor_qarzi_drops_as_payments_land(admin_client, db):
    c = make_contract(kg="1000", price="1.00")
    SupplierPayment.objects.create(contract=c, date="2026-07-20",
                                   amount=Decimal("300"), method="cash")
    assert admin_client.get("/").context["debt_split"] == [("usd", Decimal("700"))]


def test_kelishuvlar_chart_labels_every_marka(admin_client, db):
    """Grafik yorlig'i c.brand ni o'qirdi — u endi mahsulot qatorlarida."""
    c = make_contract(brand="2102 repak", kg="1000", price="1.00")
    ContractLine.objects.create(contract=c, brand="ftor oq", kg=Decimal("500"),
                                price=Decimal("1"))
    html = admin_client.get("/").content.decode()
    assert "2102 repak, ftor oq" in html


def test_yuk_holatlari_counts_trucks_per_hamkor(admin_client, db):
    """Har holat ostida qaysi hamkorning nechta mashinasi shu holatda ekani —
    yuklarni birma-bir sanab chiqish o'rniga."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    arya = Partner.objects.create(name="Arya", phone="2", city="S")
    loading = ShipmentStatus.objects.first()
    a = make_contract(partner=pars, kg="9000")
    b = make_contract(partner=arya, kg="9000")
    for _ in range(4):
        make_shipment(contract=a, kg="100", status=loading)
    make_shipment(contract=b, kg="100", status=loading)

    resp = admin_client.get("/")
    row = {r["status"].name: r for r in resp.context["status_rows"]}[loading.name]
    assert row["total"] == 5
    # eng ko'pi yuqorida, tenglashsa nom bo'yicha
    assert [(p["name"], p["count"]) for p in row["partners"]] == [("Pars", 4), ("Arya", 1)]
    assert "4 ta" in resp.content.decode()


def test_yuk_holatlari_opens_each_hamkor_into_markalar(admin_client, db):
    """"sobir 6 ta" o'zi kam narsa aytadi — hamkor ostida qaysi marka nechta
    mashinada ketayotgani ko'rinadi."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    loading = ShipmentStatus.objects.first()
    a = make_contract(partner=pars, brand="2102", kg="9000")
    b = make_contract(partner=pars, brand="7000", kg="9000")
    for _ in range(3):
        make_shipment(contract=a, kg="100", status=loading)
    make_shipment(contract=b, kg="100", status=loading)

    resp = admin_client.get("/")
    row = {r["status"].name: r for r in resp.context["status_rows"]}[loading.name]
    partner = row["partners"][0]
    assert partner["count"] == 4
    assert partner["brands"] == [("2102", 3), ("7000", 1)]   # ko'pi yuqorida
    assert "2102" in resp.content.decode()


def test_yuk_holatlari_counts_a_two_marka_yuk_under_both(admin_client, db):
    """Ikki marka olib ketayotgan mashina ikkalasiga ham sanaladi, shuning uchun
    marka yig'indisi hamkorning mashina sonidan katta bo'lishi mumkin."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    loading = ShipmentStatus.objects.first()
    contract = make_contract(partner=pars, brand="2102", kg="9000")
    other = ContractLine.objects.create(
        contract=contract, brand="7000", kg=Decimal("9000"), price=Decimal("1"))
    shipment = make_shipment(contract=contract, kg="100", status=loading)
    ShipmentLine.objects.create(shipment=shipment, contract_line=other, kg=Decimal("100"))

    row = admin_client.get("/").context["status_rows"][0]
    partner = row["partners"][0]
    assert partner["count"] == 1
    assert partner["brands"] == [("2102", 1), ("7000", 1)]


def test_yuk_holatlari_skips_statuses_with_no_yuk(admin_client, db):
    c = make_contract(kg="9000")
    used = ShipmentStatus.objects.first()
    make_shipment(contract=c, kg="100", status=used)
    names = [r["status"].name for r in admin_client.get("/").context["status_rows"]]
    assert names == [used.name]


def test_truck_plan_totals_per_hamkor(admin_client, db):
    """Hamkor bo'yicha jamlanadi: bir hamkorning bir necha kelishuvidagi qolgan
    mashinalar bitta qatorda."""
    pars = Partner.objects.create(name="Pars", phone="1", city="T")
    arya = Partner.objects.create(name="Arya", phone="2", city="S")
    a = make_contract(partner=pars, kg="9000")
    b = make_contract(partner=pars, kg="9000")
    c = make_contract(partner=arya, kg="9000")
    ContractLine.objects.filter(contract=a).update(planned_trucks=3)
    ContractLine.objects.filter(contract=b).update(planned_trucks=2)
    ContractLine.objects.filter(contract=c).update(planned_trucks=1)
    make_shipment(contract=a, kg="100")                 # 3 dan 1 tasi ketdi

    resp = admin_client.get("/")
    assert resp.context["truck_plan_rows"] == [("Pars", 4), ("Arya", 1)]
    assert "4 ta" in resp.content.decode()


def test_truck_plan_skips_kelishuvlar_that_are_done_or_unplanned(admin_client, db):
    done = make_contract(kg="9000")
    ContractLine.objects.filter(contract=done).update(planned_trucks=1)
    make_shipment(contract=done, kg="100")          # rejasi bajarildi
    make_contract(kg="9000")                        # rejasi yo'q

    assert admin_client.get("/").context["truck_plan_rows"] == []


def test_progress_chart_drops_yopilgan_kelishuvlar(admin_client, db):
    """Yuki ham to'lovi ham tugagan kelishuv chartdan tushadi — aks holda karta
    to'la 120 000 / 120 000 chiziqlar bilan to'lib, haqiqatan yo'ldagisi ko'rinmay
    qoladi. To'lovi qolgani esa turaveradi."""
    done = make_contract(brand="Yopilgan", kg="1000", price="1.00", planned_trucks=1)
    make_shipment(contract=done, kg="1000")
    SupplierPayment.objects.create(contract=done, amount=Decimal("1000"),
                                   date="2026-07-05")
    unpaid = make_contract(brand="To'lanmagan", kg="1000", price="1.00", planned_trucks=1)
    make_shipment(contract=unpaid, kg="1000")              # yuki tugadi, puli yo'q

    shown = admin_client.get("/").context["contracts"]
    assert [r["contract"].pk for r in shown] == [unpaid.pk]


def test_progress_chart_leads_with_the_kelishuv_owing_the_most_mashina(admin_client, db):
    behind = make_contract(brand="Ko'p qolgan", kg="9000", price="1.00", planned_trucks=5)
    make_shipment(contract=behind, kg="100")
    close = make_contract(brand="Oz qolgan", kg="9000", price="1.00", planned_trucks=2)
    make_shipment(contract=close, kg="100")
    unplanned = make_contract(brand="Rejasiz", kg="9000", price="1.00")

    shown = admin_client.get("/").context["contracts"]
    assert [r["contract"].pk for r in shown] == [behind.pk, close.pk, unplanned.pk]
    assert [r["trucks_left"] for r in shown] == [4, 1, 0]
    assert (shown[0]["sent"], shown[0]["planned"]) == (1, 5)
    assert shown[2]["planned"] is None                     # rejasiz: maxraji yo'q
    assert "1 / 5 mashina" in admin_client.get("/").content.decode()


def test_progress_chart_measures_yuk_and_tolov_apart(admin_client, db):
    """Ikki chiziq: kg yetkazilgani va shu kelishuv o'z valyutasida qancha
    to'langani. Ular birga yurmaydi — mol ketib puli kelmasligi ham mumkin."""
    contract = make_contract(kg="1000", price="1.00", planned_trucks=4)
    make_shipment(contract=contract, kg="250")
    SupplierPayment.objects.create(contract=contract, amount=Decimal("100"),
                                   date="2026-07-05")

    row = admin_client.get("/").context["contracts"][0]
    assert (row["shipped_kg"], row["kg"]) == (Decimal("250.000"), Decimal("1000.000"))
    assert row["kg_pct"] == 25
    assert (row["paid"], row["due"]) == (Decimal("100"), Decimal("1000"))
    assert row["pay_pct"] == 10


def test_a_two_marka_kelishuv_gets_a_yuk_bar_each(admin_client, db):
    """Summed into one bar the markalar hide each other: half the kg delivered
    reads as a kelishuv half done when it can be one marka finished and the other
    untouched — and it is the untouched one that still needs a truck."""
    contract = make_contract(brand="7000 campaund", kg="120000", price="1.00",
                             planned_trucks=10)
    ContractLine.objects.create(contract=contract, brand="209 campaund",
                                kg=Decimal("120000"), price=Decimal("1.00"))
    # Only the second marka has moved at all.
    make_shipment(contract=contract, kg="24000",
                  contract_line=contract.lines.get(brand="209 campaund"))

    row = admin_client.get("/").context["contracts"][0]
    assert [(ln["brand"], ln["pct"]) for ln in row["lines"]] == [
        ("7000 campaund", 0), ("209 campaund", 20)]
    # The combined figure is what would have been shown instead — and it says 10%
    # about a marka that has not started.
    assert row["kg_pct"] == 10

    # Thousands are grouped with an NBSP, so it is unpicked before comparing.
    html = admin_client.get("/").content.decode().replace("\u00a0", " ")
    assert "7000 campaund" in html and "209 campaund" in html
    assert "0 / 120 000 kg" in html and "24 000 / 120 000 kg" in html


def test_a_two_marka_kelishuv_splits_the_tolov_too(admin_client, db):
    """One gold bar across both could not say a kelishuv is square on one marka
    and untouched on the other — which is exactly what it is here."""
    contract = make_contract(brand="7000 campaund", kg="1000", price="1.00")
    second = ContractLine.objects.create(contract=contract, brand="209 campaund",
                                         kg=Decimal("1000"), price=Decimal("1.00"))
    SupplierPayment.objects.create(contract=contract, contract_line=second,
                                   amount=Decimal("1000"), date="2026-07-05")

    row = admin_client.get("/").context["contracts"][0]
    assert [(ln["brand"], ln["paid"], ln["due"], ln["pay_pct"]) for ln in row["lines"]] == [
        ("7000 campaund", Decimal("0"), Decimal("1000.00"), 0),
        ("209 campaund", Decimal("1000"), Decimal("1000.00"), 100)]
    # Combined, the same kelishuv reads half paid — true, and useless for deciding
    # which marka the hamkor is still owed for.
    assert row["pay_pct"] == 50
    assert row["unassigned_paid"] == Decimal("0")


def test_each_marka_counts_the_yuklar_its_own_money_covers(admin_client, db):
    """The gold bar carries a to'langan count now, and on a kelishuv with markalar
    it has to be the marka's own: a hamkor settled in full on one product and paid
    nothing on the other is exactly the case the kelishuv-wide figure cannot tell
    apart. Only the money that NAMED the marka counts, against that marka's share
    of each yuk."""
    contract = make_contract(brand="7000 campaund", kg="2000", price="1.00",
                             planned_trucks=3)
    second = ContractLine.objects.create(contract=contract, brand="209 campaund",
                                         kg=Decimal("2000"), price=Decimal("1.00"),
                                         planned_trucks=2)
    first = contract.lines.get(brand="7000 campaund")
    for day in (5, 6):
        make_shipment(contract=contract, kg="1000", contract_line=first,
                      sent=date(2026, 7, day))
    make_shipment(contract=contract, kg="1000", contract_line=second,
                  sent=date(2026, 7, 7))
    # Named 209 campaund, so it settles that marka's one truck and none of the
    # two sitting unpaid under 7000 campaund.
    SupplierPayment.objects.create(contract=contract, contract_line=second,
                                   amount=Decimal("1000"), date="2026-07-08")

    assert first.trucks_paid_for == (0, 2) and second.trucks_paid_for == (1, 1)
    html = admin_client.get("/").content.decode()
    # Against each marka's OWN plan, not the trucks it happens to have sent.
    assert "0 / 3 to'langan" in html and "1 / 2 to'langan" in html
    # And the kelishuv-wide note goes: it agrees with neither bar above it while
    # looking exactly like the fractions inside them.
    assert "yuk to'langan" not in html


def test_the_marka_bars_carry_their_own_mashina_count(admin_client, db):
    """The count used to sit on a heading above the pair, where it could only ever
    speak for the Yuk bar. In the bar it belongs to, each says its own thing."""
    contract = make_contract(brand="7000 campaund", kg="2000", price="1.00",
                             planned_trucks=4)
    ContractLine.objects.create(contract=contract, brand="209 campaund",
                                kg=Decimal("2000"), price=Decimal("1.00"),
                                planned_trucks=3)
    make_shipment(contract=contract, kg="1000",
                  contract_line=contract.lines.get(brand="7000 campaund"))

    html = admin_client.get("/").content.decode()
    assert "1 / 4 mashina" in html and "0 / 3 mashina" in html
    assert 'class="cprog-marka-trucks"' not in html


def test_both_bars_count_against_the_same_kelishuv_plan(admin_client, db):
    """The two bars sit one above the other, so their denominators have to be the
    same thing or the pair reads as a contradiction. Measured against the trucks
    SENT, the gold bar said "3 / 3 to'langan" under a blue one saying "3 / 5" —
    which looks like an error and is not. Both count against the PLAN."""
    contract = make_contract(kg="5000", price="1.00", planned_trucks=5)
    for day in (5, 6, 7):
        make_shipment(contract=contract, kg="1000", sent=date(2026, 7, day))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("3000"),
                                   date="2026-07-08")

    html = admin_client.get("/").content.decode()
    assert "3 / 5 mashina" in html and "3 / 5 to'langan" in html
    # The old per-load denominator, which is what made the pair look wrong.
    assert "3 / 3 to'langan" not in html


def test_a_kelishuv_with_no_plan_counts_against_the_trucks_it_has_sent(admin_client, db):
    """A kelishuv that never set Nechta mashina has no plan to fill against, and it
    used to fall back to a bare "2 mashina" — so two rows side by side, one with a
    plan and one without, were shaped differently for a reason that is about a
    field the operator may simply not have filled in. It counts against what it HAS
    sent instead, and reads like every other row."""
    contract = make_contract(kg="3000", price="1.00")     # no planned_trucks
    for day in (5, 6):
        make_shipment(contract=contract, kg="1000", sent=date(2026, 7, day))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("1000"),
                                   date="2026-07-08")

    html = admin_client.get("/").content.decode()
    # $1 000 of the $3 000 this kelishuv will cost, over the 2 trucks it has sent.
    assert "2 / 2 mashina" in html and "0,6 / 2 to'langan" in html
    assert "/ None" not in html


def test_a_tolov_naming_no_marka_is_spread_across_the_products(admin_client, db):
    """A to'lov that names no marka is a zaklad, and a zaklad buys the kelishuv's
    products rather than sitting outside them: it splits by mashina count where
    there is a plan to split by, and otherwise runs the kelishuv in order
    (`allocate_supplier_payment`). Left out of both bars, a kelishuv six figures
    into its life read as nothing paid."""
    contract = make_contract(brand="7000 campaund", kg="1000", price="1.00")
    ContractLine.objects.create(contract=contract, brand="209 campaund",
                                kg=Decimal("1000"), price=Decimal("1.00"))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("600"),
                                   date="2026-07-05")          # names no marka

    row = admin_client.get("/").context["contracts"][0]
    # No truck plan on either product, so there is nothing to weigh by: the money
    # runs the kelishuv in its own order and stops when it is spent.
    assert [ln["paid"] for ln in row["lines"]] == [Decimal("600"), Decimal("0")]
    # Nothing is left over — every som of it found a product to buy.
    assert row["unassigned_paid"] == Decimal("0")
    # The parts and whatever no product could take add back to the kelishuv's own
    # figure, however the money was placed.
    assert sum(ln["paid"] for ln in row["lines"]) + row["unassigned_paid"] == row["paid"]


def test_what_no_product_can_take_is_still_shown_as_unassigned(admin_client, db):
    """The hamkor's avans: money past what the whole kelishuv costs belongs to the
    hamkor rather than to any marka, and the card says so instead of quietly
    crediting the last product with it."""
    contract = make_contract(brand="7000 campaund", kg="1000", price="1.00")
    ContractLine.objects.create(contract=contract, brand="209 campaund",
                                kg=Decimal("1000"), price=Decimal("1.00"))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("2500"),
                                   date="2026-07-05")          # 500 past the lot

    row = admin_client.get("/").context["contracts"][0]
    assert [ln["paid"] for ln in row["lines"]] == [Decimal("1000"), Decimal("1000")]
    assert row["unassigned_paid"] == Decimal("500")
    assert "taqsimlanmagan" in admin_client.get("/").content.decode().lower()


def test_the_same_money_reads_the_same_whether_a_truck_has_left_or_not(admin_client, db):
    """Two markalar, same cost, same money on each — one with a truck out and one
    without. Their gold bars are drawn to the same width because the money is the
    same, so the labels have to say the same thing.

    They did not. The count measured against the trucks already SENT, so the marka
    with nothing on the road could not be credited with a truck at any amount of
    money and read "0" beside a bar that was plainly not empty."""
    contract = make_contract(brand="7000", kg="1000", price="1.00", planned_trucks=5)
    second = ContractLine.objects.create(contract=contract, brand="209",
                                         kg=Decimal("1000"), price=Decimal("1.00"),
                                         planned_trucks=5)
    # Only 209 has a truck on the road; both markalar are paid exactly the same.
    make_shipment(contract=contract, kg="200", contract_line=second,
                  sent=date(2026, 7, 5))
    for line in (contract.lines.get(brand="7000"), second):
        SupplierPayment.objects.create(contract=contract, contract_line=line,
                                       amount=Decimal("100"), date="2026-07-06")

    row = admin_client.get("/").context["contracts"][0]
    labels = {ln["brand"]: ln["paid_count"] for ln in row["lines"]}
    assert labels["7000"] == labels["209"], labels
    # And the label IS the bar's own fill: $100 of $1 000 is a tenth of 5 trucks.
    assert labels["7000"] == "0,5 / 5"
    assert {ln["brand"]: ln["pay_pct"] for ln in row["lines"]} == {"7000": 10, "209": 10}


def test_a_one_marka_kelishuv_keeps_the_single_yuk_bar(admin_client, db):
    """Splitting a kelishuv with nothing to split would just draw the same bar
    twice, under a heading that already names the marka."""
    contract = make_contract(brand="2102 repak", kg="1000", price="1.00")
    make_shipment(contract=contract, kg="250")

    row = admin_client.get("/").context["contracts"][0]
    assert row["lines"] == [] and row["kg_pct"] == 25
    assert "2102 repak" in admin_client.get("/").content.decode()


def test_the_tolov_bar_says_how_many_yuklar_the_money_covers(admin_client, db):
    """"$96 400 of $288 000" is a share of a figure nobody thinks in. The question
    asked of a hamkor is which TRUCKS are settled, so the bar says that too —
    oldest truck first, the way the debt is actually worked off."""
    contract = make_contract(kg="4000", price="1.00", planned_trucks=4)
    for day in (5, 6, 7):
        make_shipment(contract=contract, kg="1000", sent=date(2026, 7, day))
    # Two trucks' worth and a half: $2 000 covers the first two outright, and the
    # $500 sitting in the third is half of that truck — counted as the half it is.
    SupplierPayment.objects.create(contract=contract, amount=Decimal("2500"),
                                   date="2026-07-08")

    html = admin_client.get("/").content.decode()
    # Twice inside the gold bar — the label is written a second time so it can
    # turn white where the fill reaches it — and once more on the note below,
    # which a kelishuv with a single mahsulot keeps.
    assert html.count("2,5 / 4 to'langan") == 2
    assert "2,5 / 4 yuk to'langan" in html


def test_a_part_paid_truck_is_never_rounded_up_into_a_whole_one(admin_client, db):
    """The partial is the point, but it must not overshoot into a load the hamkor
    is still owed for. $1 990 of two $1 000 trucks is 1,9 — never 2, which would
    say the second one is settled when $10 of it is not."""
    contract = make_contract(kg="3000", price="1.00", planned_trucks=3)
    for day in (5, 6):
        make_shipment(contract=contract, kg="1000", sent=date(2026, 7, day))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("1990"),
                                   date="2026-07-08")

    assert contract.trucks_paid_for == (Decimal("1.9"), 2)
    assert "1,9 / 3 to'langan" in admin_client.get("/").content.decode()


def test_a_trailing_zero_is_never_printed_on_a_whole_count(admin_client, db):
    """Every truck paid for reads "2 / 2", not "2,0 / 2" — the same rule the kg
    and money figures on this card follow."""
    # A third truck's worth of kg still to send, so the kelishuv is not settled
    # and stays on the card — the chart only carries business still in flight.
    contract = make_contract(kg="3000", price="1.00", planned_trucks=3)
    for day in (5, 6):
        make_shipment(contract=contract, kg="1000", sent=date(2026, 7, day))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("2000"),
                                   date="2026-07-08")

    html = admin_client.get("/").content.decode()
    assert "2 / 3 to'langan" in html and "2,0 / 3" not in html


def test_an_avans_counts_even_before_the_load_it_bought_leaves(admin_client, db):
    """Money paid ahead of the goods is progress, and the bar always drew it as
    such — it was the LABEL that refused to, because it counted against the trucks
    already sent. One truck out of three and $2 500 of the $3 000 this kelishuv
    will cost reads 2,5 of 3, not the 1 the old count capped it at: the hamkor is
    holding the money whether or not they have loaded the lorry yet."""
    contract = make_contract(kg="3000", price="1.00", planned_trucks=3)
    make_shipment(contract=contract, kg="1000", sent=date(2026, 7, 5))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("2500"),
                                   date="2026-07-08")

    assert "2,5 / 3 to'langan" in admin_client.get("/").content.decode()


def test_the_oldest_truck_is_the_one_paid_off_first(admin_client, db):
    """Entry order is not delivery order — a truck entered later can have left
    earlier, and it is the one settled first."""
    contract = make_contract(kg="2000", price="1.00")
    make_shipment(contract=contract, kg="1000", sent=date(2026, 7, 20))
    make_shipment(contract=contract, kg="1000", sent=date(2026, 7, 5))
    SupplierPayment.objects.create(contract=contract, amount=Decimal("1000"),
                                   date="2026-07-21")

    assert contract.trucks_paid_for == (1, 2)


def test_an_avans_with_no_truck_yet_covers_nothing(admin_client, db):
    """Money can run ahead of the goods, and it has still bought no yuk. The bar
    says so rather than going blank: "0", not an empty track the operator has to
    guess at. $900 against a kelishuv with nothing sent buys nothing."""
    contract = make_contract(kg="1000", price="1.00")
    SupplierPayment.objects.create(contract=contract, amount=Decimal("900"),
                                   date="2026-07-08")

    assert contract.trucks_paid_for == (0, 0)
    html = admin_client.get("/").content.decode()
    assert "0 yuk to'langan" in html and "0 to'langan" in html
    # No plan on this kelishuv, so there is no denominator to invent one from.
    assert "0 / 0 to'langan" not in html


def test_progress_chart_bar_never_runs_past_its_track(admin_client, db):
    """Avans mol ketishidan oldin to'lansa foiz 100 dan oshib ketardi — chiziq
    kartadan chiqib ketmasin."""
    contract = make_contract(kg="1000", price="1.00", planned_trucks=2)
    SupplierPayment.objects.create(contract=contract, amount=Decimal("1500"),
                                   date="2026-07-05")

    row = admin_client.get("/").context["contracts"][0]
    assert row["pay_pct"] == 100


def test_progress_chart_says_when_it_is_showing_a_subset(admin_client, db):
    for i in range(10):
        make_contract(brand=f"K{i}", kg="1000", price="1.00")
    resp = admin_client.get("/")
    assert resp.context["contracts_shown"] == 8
    assert resp.context["contracts_total"] == 10
    assert "10 tadan 8 tasi" in resp.content.decode()


def test_the_card_is_read_in_the_order_it_was_dragged_into(admin_client, db):
    """Trucks-still-to-send is a default, not a verdict: the kelishuv being
    watched is dragged to the top and stays there."""
    behind = make_contract(brand="Ko'p qolgan", kg="9000", price="1.00", planned_trucks=5)
    make_shipment(contract=behind, kg="100")
    watched = make_contract(brand="Kuzatilayotgan", kg="9000", price="1.00", planned_trucks=2)
    make_shipment(contract=watched, kg="100")

    assert [r["contract"].pk for r in admin_client.get("/").context["contracts"]] \
        == [behind.pk, watched.pk]

    resp = admin_client.post("/dashboard/contract-order/",
                             {"order": f"{watched.pk},{behind.pk}"})
    assert resp.status_code == 200
    assert [r["contract"].pk for r in admin_client.get("/").context["contracts"]] \
        == [watched.pk, behind.pk]


def test_a_kelishuv_nobody_dragged_keeps_its_automatic_rank(admin_client, db):
    """Dragging one row must not scramble the rest. An undragged kelishuv holds
    the ranking it had and falls in behind the ones that were placed by hand —
    which is what lets a NEW kelishuv appear without the order being redone."""
    first = make_contract(brand="Bir", kg="9000", price="1.00", planned_trucks=5)
    make_shipment(contract=first, kg="100")
    second = make_contract(brand="Ikki", kg="9000", price="1.00", planned_trucks=3)
    make_shipment(contract=second, kg="100")

    admin_client.post("/dashboard/contract-order/", {"order": str(second.pk)})

    fresh = make_contract(brand="Yangi", kg="9000", price="1.00", planned_trucks=9)
    make_shipment(contract=fresh, kg="100")
    shown = [r["contract"].pk for r in admin_client.get("/").context["contracts"]]
    # The dragged one leads; the other two follow in trucks-left order.
    assert shown == [second.pk, fresh.pk, first.pk]


def test_dragging_does_not_demote_the_kelishuvlar_below_the_fold(admin_client, admin_user, db):
    """The card shows eight, so a drop posts eight pk's — the ranking of the ones
    the user never saw has to survive it."""
    made = [make_contract(brand=f"K{i}", kg="1000", price="1.00") for i in range(10)]
    admin_client.post("/dashboard/contract-order/",
                      {"order": ",".join(str(c.pk) for c in reversed(made))})

    top_two = made[-1].pk, made[-2].pk
    admin_client.post("/dashboard/contract-order/", {"order": f"{top_two[1]},{top_two[0]}"})

    admin_user.refresh_from_db()
    saved = admin_user.dashboard_contract_order
    assert saved[:2] == [top_two[1], top_two[0]]
    assert sorted(saved) == sorted(c.pk for c in made)      # nobody dropped off


def test_a_junk_order_is_refused(admin_client, db):
    make_contract(kg="1000", price="1.00")
    assert admin_client.post("/dashboard/contract-order/",
                             {"order": "3,not-a-pk"}).status_code == 400


def test_only_an_admin_can_reorder_the_card(translator_client, db):
    assert translator_client.post("/dashboard/contract-order/",
                                  {"order": "1"}).status_code in (302, 403)


def test_monthly_sent_counts_every_load_with_a_date_in_that_month(admin_client, db):
    """To'qqizta yuk iyulda jo'natilsa, iyul qatorida 9 turishi kerak."""
    c = make_contract(kg="90000")
    for day in range(1, 10):
        make_shipment(contract=c, kg="100", sent=date(2026, 7, day))

    rows = {r["month"]: r for r in admin_client.get("/").context["monthly"]}
    assert rows[date(2026, 7, 1)]["sent"] == 9


def test_a_load_sent_in_another_month_lands_in_that_month(admin_client, db):
    """Iyunda jo'natilib iyulda kelgan yuk iyul 'jo'natilgan' iga kirmaydi —
    hisobot kamaygandek ko'rinishining eng ehtimolli sababi shu."""
    c = make_contract(kg="90000")
    for day in range(1, 9):
        make_shipment(contract=c, kg="100", sent=date(2026, 7, day))
    make_shipment(contract=c, kg="100", sent=date(2026, 6, 28), arrived=date(2026, 7, 3),
                  status=ShipmentStatus.arrival())

    rows = {r["month"]: r for r in admin_client.get("/").context["monthly"]}
    assert rows[date(2026, 7, 1)]["sent"] == 8      # iyulda jo'natilganlar
    assert rows[date(2026, 6, 1)]["sent"] == 1      # to'qqizinchisi iyunda
    assert rows[date(2026, 7, 1)]["arrived"] == 1   # lekin iyulda yetib kelgan


class TestKechikkanYuklarNamesTheTruck:
    """Chasing a late load needs the numbers it answers to: the transport raqami on
    the phone, the konteyner at the border. Both, on both boards that list them."""

    def _late(self, **kw):
        contract = make_contract(kg="100000", price="1.00")
        return make_shipment(contract=contract, kg="400",
                             eta=date.today() - timedelta(days=3), **kw)

    def test_dashboard_shows_transport_and_container(self, admin_client, db):
        self._late(transport="01A111AA", container="MSCU-778")
        html = admin_client.get("/").content.decode()
        assert "01A111AA" in html and "MSCU-778" in html

    def test_reports_shows_transport_and_container(self, admin_client, db):
        self._late(transport="01B222BB", container="TCLU-991")
        html = admin_client.get("/reports/").content.decode()
        assert "01B222BB" in html and "TCLU-991" in html

    def test_the_transport_is_the_one_carrying_it_now(self, admin_client, db):
        """A load that changed vehicle mid-route names the leg's truck, not the
        one it left on — the old plate would send somebody after the wrong driver."""
        from crm.models import ShipmentLeg
        shipment = self._late(transport="01A111AA", container="MSCU-778")
        ShipmentLeg.objects.create(shipment=shipment, order=1, transport="90Z999ZZ",
                                   from_location="Xorgos", to_location="Toshkent",
                                   departed=date.today() - timedelta(days=5))
        html = admin_client.get("/").content.decode()
        assert "90Z999ZZ" in html and "MSCU-778" in html

    def test_a_load_with_neither_number_still_renders(self, admin_client, db):
        self._late(transport="", container="")
        assert admin_client.get("/").status_code == 200
        assert admin_client.get("/reports/").status_code == 200


class TestTheDoskaOpensOnThisMonth:
    """The five flow KPIs are a slice of a davr; the four standing ones are not.

    "1 091 950 kg yuborilgan" since the book began is a figure nobody is measured
    against, so the board opens on the month the business is actually being run in and
    the ‹ davr › bar steps it. What it must NOT do is narrow the standing figures with
    it: a "hamkor qarzi in Iyul" is not a sum anybody is owed, and a qoldiq is what is
    on the floor now or it is nothing. See `_dashboard_window`.
    """

    #: Today, and a day that is certainly in the month before it — the window is real
    #: wall-clock time here (nothing in this suite freezes the clock), so a date typed
    #: as "2026-07-05" would drift out of the month it was written for.
    def _dates(self):
        today = timezone.localdate()
        return today, today.replace(day=1) - timedelta(days=1)

    def _world(self):
        """One truck's worth of every flow, this month and last, so each card can be
        read against a figure that is unambiguously one month's."""
        today, last_month = self._dates()
        customer = Customer.objects.create(name="Alisher", phone="1")
        for when, kg in ((today, "400"), (last_month, "700")):
            contract = make_contract(kg="100000", price="1.00", created=when)
            lot = make_lot(contract=contract, kg=kg, price="1.00", sent=when,
                           arrived=when, status=ShipmentStatus.arrival())
            SupplierPayment.objects.create(contract=contract, date=when,
                                           amount=Decimal("100"))
            Sale.objects.create(customer=customer, line=lot, kg=Decimal("10"),
                                price=Decimal("3"), date=when)
        return today, last_month

    def test_it_opens_on_this_month_rather_than_on_everything(self, admin_client, db):
        self._world()
        ctx = admin_client.get("/").context
        # This month's truck only — last month's 700 kg is a different month's work.
        assert ctx["shipped_kg"] == Decimal("400.000")
        assert ctx["arrived_kg"] == Decimal("400.000")
        assert ctx["total_kg"] == Decimal("100000.000")
        assert dict(ctx["paid_split"])["usd"] == Decimal("100")

    def test_hammasi_is_the_way_back_out_to_all_time(self, admin_client, db):
        """An empty querystring is the month it opened in, so "everything" has to be
        written down — the kassa says it the same way."""
        self._world()
        ctx = admin_client.get("/?davr=all").context
        assert ctx["shipped_kg"] == Decimal("1100.000")        # 400 + 700
        assert ctx["arrived_kg"] == Decimal("1100.000")
        assert ctx["total_kg"] == Decimal("200000.000")
        assert dict(ctx["paid_split"])["usd"] == Decimal("200")

    def test_a_named_month_wins_over_the_default(self, admin_client, db):
        _today, last_month = self._world()
        first = last_month.replace(day=1)
        ctx = admin_client.get("/", {"from": first.isoformat(),
                                     "to": last_month.isoformat()}).context
        assert ctx["shipped_kg"] == Decimal("700.000")
        assert ctx["arrived_kg"] == Decimal("700.000")
        assert dict(ctx["paid_split"])["usd"] == Decimal("100")

    def test_the_standing_figures_ignore_the_window(self, admin_client, db):
        """Qarz, qoldiq and kechikkan yuklar are the state of things TODAY. Narrowed to
        a month they would answer a question nobody asked — and the qoldiq of a month
        that is over is not a number the ombor holds."""
        self._world()
        first = timezone.localdate().replace(day=1) - timedelta(days=1)
        month = admin_client.get("/", {"from": first.replace(day=1).isoformat(),
                                       "to": first.isoformat()}).context
        everything = admin_client.get("/?davr=all").context
        for key in ("stock_kg", "debt_split", "customer_debt_split"):
            assert list(month[key]) == list(everything[key]) if key.endswith("_split") \
                else month[key] == everything[key]
        assert len(month["overdue"]) == len(everything["overdue"])

    def test_each_card_reads_the_date_that_makes_it_that_month(self, admin_client, db):
        """One truck that LEFT last month and LANDED this one belongs to both cards —
        Yuborilgan to the month it went, Omborga kelgan to the month it arrived. The
        two figures are meant to disagree; that is why each card names its own sana."""
        today, last_month = self._dates()
        contract = make_contract(kg="100000", price="1.00", created=last_month)
        make_lot(contract=contract, kg="500", price="1.00", sent=last_month,
                 arrived=today, status=ShipmentStatus.arrival())

        this = admin_client.get("/").context
        assert this["shipped_kg"] == 0 and this["arrived_kg"] == Decimal("500.000")
        # …and the kelishuv itself was struck last month, so it counts there.
        assert this["total_kg"] == 0

        prev = admin_client.get("/", {"from": last_month.replace(day=1).isoformat(),
                                      "to": last_month.isoformat()}).context
        assert prev["shipped_kg"] == Decimal("500.000") and prev["arrived_kg"] == 0
        assert prev["total_kg"] == Decimal("100000.000")

    def test_foyda_is_this_month_s_sotuvlar(self, admin_client, db):
        today, last_month = self._dates()
        customer = Customer.objects.create(name="Alisher", phone="1")
        lot = make_lot(contract=make_contract(kg="100000", price="1.00"), kg="1000",
                       price="1.00", sent=last_month, arrived=last_month,
                       status=ShipmentStatus.arrival())
        Sale.objects.create(customer=customer, line=lot, kg=Decimal("100"),
                            price=Decimal("3"), date=today)
        Sale.objects.create(customer=customer, line=lot, kg=Decimal("100"),
                            price=Decimal("3"), date=last_month)

        # (3.00 - 1.00) * 100 — one sotuv's worth, not both.
        assert admin_client.get("/").context["sales_profit_total"] == Decimal("200.00")
        assert admin_client.get("/?davr=all").context["sales_profit_total"] \
            == Decimal("400.00")

    def test_the_oylik_hisobot_below_still_covers_every_month(self, admin_client, db):
        """The table is the year read a row at a time — narrowing it to the one month
        the cards above are showing would leave it saying what they already said."""
        self._world()
        months = {row["month"] for row in admin_client.get("/").context["monthly"]}
        today, last_month = self._dates()
        assert {today.replace(day=1), last_month.replace(day=1)} <= months

    def test_the_bar_is_drawn_with_its_arrows_and_a_spelled_out_hammasi(self, admin_client, db):
        html = admin_client.get("/").content.decode()
        assert "daterange-bar" in html
        # It landed on a period, so it has arrows rather than reading "Hammasi"…
        assert "daterange--bare" not in html
        # …and Hammasi has to be written down, since no period is where it started.
        assert "davr=all" in html and "data-opens-period" in html

    def test_the_bar_sits_on_the_group_it_narrows(self, admin_client, db):
        """Caption then bar, one .listbar row, and the standing group's own caption
        after both KPI grids' worth of cards — the layout is what says which figures
        answer to the davr."""
        html = admin_client.get("/").content.decode()
        assert html.index('class="listbar"') < html.index('class="daterange-bar"')
        assert (html.index("Davr natijalari")
                < html.index("Sotuvdan foyda")
                < html.index("Hozirgi holat")
                < html.index("Hamkor qarzi"))

    def test_a_mistyped_period_falls_back_to_this_month_instead_of_500ing(self, admin_client, db):
        """A querystring is typed by hand and lives in bookmarks, so a stale or
        mistyped one must not take the board down.

        It lands on this month rather than on Hammasi: neither end parsed, which is
        the same as not having said a period at all, and on this screen that is the
        month it opens in. Falling through to all-time would answer a garbled
        question with the widest possible figure."""
        today = timezone.localdate()
        resp = admin_client.get("/", {"from": "kecha", "to": "2026-13-45"})
        assert resp.status_code == 200
        assert resp.context["date_from"] == today.replace(day=1).isoformat()
        assert resp.context["date_to"] == today.isoformat()
