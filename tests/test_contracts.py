from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from conftest import line_data, make_contract, make_shipment
from crm.templatetags.crm_extras import NBSP
from crm.models import (
    Contract, ContractLine, Currency, Partner, Shipment, ShipmentLine, ShipmentStatus,
    SupplierPayment,
)


def _contract(**kw):
    partner = kw.pop("partner", None) or Partner.objects.create(name="Pars", phone="1", city="Tehron")
    defaults = dict(brand="LLDPE 209AA", kg="50000", price="0.96",
                    created="2026-07-01")
    defaults.update(kw)
    return make_contract(partner=partner, **defaults)


def _ship(contract, kg="100", price="1.00"):
    """One truck under the kelishuv, priced so its goods_value is easy to read."""
    return make_shipment(contract=contract, kg=kg, price=price)


def _pay(contract, amount):
    return SupplierPayment.objects.create(contract=contract, amount=Decimal(amount))


def _listed(client, **params):
    resp = client.get("/contracts/", params)
    assert resp.status_code == 200
    # `page` pages HAMKOR groups now; `rows` is this page's kelishuvlar, flattened.
    return resp, [c.pk for c in resp.context["rows"]]


def test_total_value(db):
    c = _contract()
    assert c.total_value == Decimal("48000.00")
    # nothing shipped yet → nothing owed (debt accrues per shipped truck)
    assert c.shipped_value == Decimal("0")
    assert c.debt == Decimal("0")
    assert c.remaining_kg == Decimal("50000")


def test_create_via_view(admin_client, admin_user):
    p = Partner.objects.create(name="Arya", phone="1", city="Shiroz")
    resp = admin_client.post("/contracts/new/", {
        "partner": p.pk, "currency": "usd", "created": "2026-07-04", "note": "",
        **line_data({"brand": "HDPE 7000F", "kg": "30000", "price": "1.05"}),
    })
    assert resp.status_code == 302
    c = Contract.objects.get(lines__brand="HDPE 7000F")
    assert c.created_by == admin_user


def test_create_contract_modal_get_returns_partial(admin_client):
    resp = admin_client.get("/contracts/new/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "modal-head" in html
    assert "<html" not in html


def test_create_contract_modal_post_valid_returns_204_with_redirect(admin_client):
    p = Partner.objects.create(name="Zamin", phone="1", city="Buxoro")
    resp = admin_client.post(
        "/contracts/new/",
        {
            "partner": p.pk, "currency": "usd", "created": "2026-07-05", "note": "",
            **line_data({"brand": "LDPE 2100TN00", "kg": "20000", "price": "1.10"}),
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 204
    assert resp["X-Redirect"] == "/contracts/"
    assert Contract.objects.filter(lines__brand="LDPE 2100TN00").exists()


def test_create_contract_modal_post_invalid_returns_422(admin_client):
    p = Partner.objects.create(name="Zamin", phone="1", city="Buxoro")
    resp = admin_client.post(
        "/contracts/new/",
        {
            "partner": p.pk, "created": "2026-07-10", "note": "",
            **line_data({"brand": "B", "kg": "0", "price": "1"}),   # kg musbat emas
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    html = resp.content.decode()
    assert resp.status_code == 422
    assert "modal-head" in html


def _mixed_book(partner):
    """One kelishuv per to'lov holati. Holat follows the kelishuv's own value, so a
    yuk is not needed for one to count as fully paid — avans is normal."""
    paid = _contract(partner=partner, kg="100", price="1.00")     # jami 100$
    _pay(paid, "100")
    partial = _contract(partner=partner, kg="100", price="1.00")
    _pay(partial, "40")
    unpaid = _contract(partner=partner, kg="100", price="1.00")
    return paid, partial, unpaid


def test_filter_by_payment_status(admin_client, db):
    """To'lov holati kelishuv qiymatiga qarab: to'liq to'langan / qisman / hech
    to'lanmagan. Ilgari yuborilgan yukka bog'liq edi, shuning uchun avans
    berilgan kelishuv hech qaysi chipga tushmasdi."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    paid, partial, unpaid = _mixed_book(partner)

    assert _listed(admin_client, pay="paid")[1] == [paid.pk]
    assert _listed(admin_client, pay="partial")[1] == [partial.pk]
    assert _listed(admin_client, pay="unpaid")[1] == [unpaid.pk]
    assert set(_listed(admin_client)[1]) == {paid.pk, partial.pk, unpaid.pk}


def test_payment_chips_carry_counts(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    _mixed_book(partner)
    resp, _ = _listed(admin_client)
    counts = {t["key"]: t["count"] for t in resp.context["pay_tabs"]}
    assert counts == {"": 3, "paid": 1, "partial": 1, "unpaid": 1}


def test_chip_counts_reflect_the_other_filters(admin_client, db):
    """Counts are faceted: they narrow with partner/holat/search, but the
    payment filter itself never shrinks its own chips."""
    a = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    b = Partner.objects.create(name="Arya", phone="2", city="Shiroz")
    _mixed_book(a)
    other = _contract(partner=b)
    _ship(other)                               # b: bitta to'lanmagan kelishuv

    resp, _ = _listed(admin_client, partner=b.pk, pay="unpaid")
    counts = {t["key"]: t["count"] for t in resp.context["pay_tabs"]}
    assert counts == {"": 1, "paid": 0, "partial": 0, "unpaid": 1}


def test_filter_by_partner(admin_client, db):
    a = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    b = Partner.objects.create(name="Arya", phone="2", city="Shiroz")
    mine = _contract(partner=a)
    _contract(partner=b)
    assert _listed(admin_client, partner=a.pk)[1] == [mine.pk]


def test_filter_by_completion_state(admin_client, db):
    """Yakunlangan = hamma kg yuborilgan VA hamkorga qarz qolmagan. To'liq
    yuborilgan, lekin to'lanmagan kelishuv hali ham qolganlar orasida."""
    done = _contract(kg=Decimal("100"))
    _ship(done, kg="100"), _pay(done, "100")
    owed = _contract(kg=Decimal("100"))
    _ship(owed, kg="100")                      # yuborilgan, lekin qarz bor
    part = _contract(kg=Decimal("100"))
    _ship(part, kg="40")

    assert _listed(admin_client, state="done")[1] == [done.pk]
    assert set(_listed(admin_client, state="open")[1]) == {owed.pk, part.pk}


def test_finished_kelishuvlar_are_hidden_by_default(admin_client, db):
    """Filtrsiz kirilganda faqat qolganlar ko'rinadi — `Hammasi` ataylab tanlanadi."""
    done = _contract(kg=Decimal("100"))
    _ship(done, kg="100"), _pay(done, "100")
    open_one = _contract(kg=Decimal("100"))

    assert _listed(admin_client)[1] == [open_one.pk]
    assert set(_listed(admin_client, state="")[1]) == {done.pk, open_one.pk}


def test_filters_combine_with_search(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    hit = _contract(partner=partner, brand="LLDPE 209AA")
    _ship(hit)
    _pay(_contract(partner=partner, brand="LLDPE 100AA"), "0.01")   # boshqa holat
    other = _contract(partner=partner, brand="HDPE 7000F")
    _ship(other)

    assert _listed(admin_client, q="LLDPE", pay="unpaid")[1] == [hit.pk]


def test_filtered_list_does_not_query_per_contract(admin_client, db,
                                                   django_assert_max_num_queries):
    """The pay/holat filters read shipments + payments off the prefetch, so the
    page cost stays flat instead of growing two queries per kelishuv."""
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    for _ in range(6):
        c = _contract(partner=partner)
        _ship(c), _pay(c, "10")
    with django_assert_max_num_queries(12):
        admin_client.get("/contracts/", {"pay": "partial"})


def test_list_shows_every_marka_with_its_kg_and_narx(admin_client, db):
    """A kelishuv covering several products must show all of them — the earlier
    single-brand columns rendered blank once brand/kg/price moved onto the lines."""
    c = _contract(brand="2102 repak", kg="1000", price="1.25")
    ContractLine.objects.create(contract=c, brand="ftor oq", kg=Decimal("500"),
                                price=Decimal("0.80"))

    html = admin_client.get("/contracts/").content.decode()
    assert "2102 repak" in html and "ftor oq" in html
    assert "1.25" in html and "0.8" in html           # each product's own narx (no trailing zeros)
    assert f"$1{NBSP}650" in html                      # 1000×1.25 + 500×0.80


def test_dropdowns_name_every_marka(db):
    """The kelishuv <option> abbreviated to "2102 +1", which hid the very thing
    the operator is choosing between."""
    c = _contract(brand="2102 repak")
    ContractLine.objects.create(contract=c, brand="ftor oq", kg=Decimal("500"),
                                price=Decimal("0.80"))
    assert c.brand_summary == "2102 repak, ftor oq"
    assert str(c) == f"{c.code} · 2102 repak, ftor oq"



def test_kelishuv_option_shows_the_price(db):
    """Yuk ochayotganda narx ham ko'rinsin — bitta mahsulot bo'lsa o'z narxi,
    bir nechta bo'lsa oralig'i."""
    from crm.forms import contract_option_label

    one = _contract(kg="1000", price="1.25")
    assert "1.25 $/kg" in contract_option_label(one)

    many = _contract(kg="1000", price="1.00")
    ContractLine.objects.create(contract=many, brand="ftor oq", kg=Decimal("500"),
                                price=Decimal("2.50"))
    assert "1 $/kg – 2.5 $/kg" in contract_option_label(many)


def test_kelishuv_option_shows_a_som_narx_in_som(db):
    """A kelishuv struck in so'm is offered in so'm — including both ends of a price
    range, since a shared trailing "$/kg" would misprice every row under it."""
    from crm.forms import contract_option_label

    som_only = _contract(kg="1000", price="1.00", currency="uzs",
                         price_uzs=Decimal("12500"))
    som_only.lines.update(exchange_rate=Decimal("12500"))
    assert f"12{NBSP}500 so'm/kg" in contract_option_label(som_only)
    assert "$/kg" not in contract_option_label(som_only)

    ContractLine.objects.create(contract=som_only, brand="ftor oq", kg=Decimal("500"),
                                price=Decimal("2.50"), price_uzs=Decimal("31250"),
                                exchange_rate=Decimal("12500"))
    label = contract_option_label(som_only)
    assert f"12{NBSP}500 so'm/kg" in label
    assert f"31{NBSP}250 so'm/kg" in label
    assert "$/kg" not in label


def test_kelishuv_has_no_deadline(db):
    """Yetkazish muddati olib tashlandi."""
    from crm.forms import ContractForm

    assert not hasattr(_contract(), "deadline")
    assert "deadline" not in ContractForm().fields


def test_planned_trucks_is_optional_and_saved(admin_client, db):
    """Kelishuvga nechta mashina biriktirilishi — ixtiyoriy."""
    p = Partner.objects.create(name="Zamin", phone="1", city="Buxoro")
    payload = {"partner": p.pk, "currency": "usd", "created": "2026-07-05", "note": "",
               **line_data({"brand": "2102", "kg": "20000", "price": "1.10"})}
    assert admin_client.post("/contracts/new/", payload).status_code == 302
    assert Contract.objects.get().planned_trucks is None

    Contract.objects.all().delete()
    admin_client.post("/contracts/new/", {**payload, "planned_trucks": "2"})
    assert Contract.objects.get().planned_trucks == 2


def test_truck_progress_counts_sent_against_planned(db):
    """Yuklar sahifasidagi progress mashina soni bo'yicha: 1/2."""
    c = _contract(kg="1000", planned_trucks=2)
    assert c.truck_progress == (0, 2)
    _ship(c, kg="400")
    assert c.truck_progress == (1, 2)


def test_truck_progress_without_a_plan_has_no_denominator(db):
    c = _contract(kg="1000")
    _ship(c, kg="400")
    assert c.truck_progress == (1, None)


def test_kelishuv_option_ends_with_the_whole_agreement(db):
    """Variantda qolgan kg dan tashqari kelishuvning jami kg si ham ko'rinadi."""
    from crm.forms import contract_option_label

    c = _contract(kg="1000", price="1.25")
    _ship(c, kg="400")
    assert contract_option_label(c) == f"{c.code} · LLDPE 209AA · 600 kg qolgan · 1.25 $/kg · jami 1000 kg"


def test_the_holat_select_is_renamed(admin_client, db):
    """Yetkazish emas, Holat — va variantlar Tugallanmagan / Tugallangan."""
    _contract()
    html = admin_client.get("/contracts/").content.decode()
    assert "Yetkazish" not in html and "Qolgan kelishuvlar" not in html
    assert "Tugallanmagan" in html and "Tugallangan" in html


def test_tolov_is_a_select_with_counts(admin_client, db):
    paid = _contract(kg="100", price="1.00")
    _pay(paid, "100")
    _contract(kg="100", price="1.00")                    # to'lanmagan

    html = admin_client.get("/contracts/").content.decode()
    assert 'name="pay"' in html
    # Django escapes the apostrophe, so match on the tail of each label
    assert "langan (1)" in html and "lanmagan (1)" in html
    assert "status-tab" not in html                       # chiplar yo'q


def test_tolov_filter_hidden_and_ignored_for_tugallangan(admin_client, db):
    """Tugallangan kelishuv ta'rifi bo'yicha to'liq to'langan — bu yerda to'lov
    filtri ma'nosiz, shuning uchun ko'rsatilmaydi va e'tiborga olinmaydi."""
    done = _contract(kg="100", price="1.00")
    _ship(done, kg="100"), _pay(done, "100")

    resp = admin_client.get("/contracts/", {"state": "done"})
    assert 'name="pay"' not in resp.content.decode()
    # hatto so'ralganda ham qatorni yo'qotmaydi
    assert _listed(admin_client, state="done", pay="unpaid")[1] == [done.pk]


def _sorted_codes(client, sort):
    """The kelishuv kodlar in the order the page draws them.

    Read off `rows` rather than the paginator: the page holds hamkor groups now, and
    the sort still has to come out of the flattened list — a group takes the position
    its first kelishuv was sorted into, so sorting by narx or sana still orders the
    screen even though the rows are grouped."""
    resp = client.get("/contracts/", {"state": "", "sort": sort})
    return [c.code for c in resp.context["rows"]]


def test_sorting_options(admin_client, db):
    """Saralash: sana, kod, hamkor, jami — har biri o'z tartibida."""
    zeta = Partner.objects.create(name="Zeta", phone="1", city="T")
    alfa = Partner.objects.create(name="Alfa", phone="1", city="T")
    old = _contract(partner=zeta, created="2026-01-05", kg="100", price="1.00")   # jami 100
    new = _contract(partner=alfa, created="2026-09-09", kg="100", price="9.00")   # jami 900

    assert _sorted_codes(admin_client, "-created") == [new.code, old.code]
    assert _sorted_codes(admin_client, "created") == [old.code, new.code]
    assert _sorted_codes(admin_client, "partner") == [new.code, old.code]   # Alfa < Zeta
    assert _sorted_codes(admin_client, "code") == [new.code, old.code]      # alfa-1 < zeta-1
    assert _sorted_codes(admin_client, "-total") == [new.code, old.code]    # 900 > 100
    assert _sorted_codes(admin_client, "total") == [old.code, new.code]


def test_sort_defaults_to_newest_and_is_offered_in_the_toolbar(admin_client, db):
    old = _contract(created="2026-01-05")
    new = _contract(created="2026-09-09")
    resp = admin_client.get("/contracts/", {"state": ""})
    assert [c.code for c in resp.context["rows"]] == [new.code, old.code]

    html = resp.content.decode()
    assert 'name="sort"' in html and "Saralash" in html


def test_an_unknown_sort_falls_back_to_the_default(admin_client, db):
    old = _contract(created="2026-01-05")
    new = _contract(created="2026-09-09")
    assert _sorted_codes(admin_client, "junk") == [new.code, old.code]


# --- one kelishuv, one currency -------------------------------------------

def test_a_kelishuv_is_struck_in_one_currency_and_its_rows_follow(admin_client, db):
    """The valyuta is the agreement's, not the product row's — that is what lets
    "qolgan to'lov" be a single figure in a single currency."""
    p = Partner.objects.create(name="Sobir", phone="1", city="Tehron")
    resp = admin_client.post("/contracts/new/", {
        "partner": p.pk, "currency": "uzs", "created": "2026-07-04", "note": "",
        **line_data({"brand": "2102 repak", "kg": "1000", "price": "12650"},
                    {"brand": "ftor oq", "kg": "500", "price": "13000"}),
    })
    assert resp.status_code == 302
    c = Contract.objects.get()
    assert c.currency == Currency.UZS
    assert {ln.currency for ln in c.lines.all()} == {Currency.UZS}
    assert c.is_som and c.total_value_own == c.total_value_uzs


def test_a_product_row_asks_for_neither_a_valyuta_nor_a_kurs(admin_client, db):
    """Both are the kelishuv's. The kurs is still stored — a landed cost has to mix
    currencies — it is just inherited instead of typed."""
    c = _contract(currency=Currency.UZS)
    resp = admin_client.get(f"/contracts/{c.pk}/edit/")
    row = resp.context["lines"].forms[0]
    assert "currency" not in row.fields
    assert "exchange_rate" not in row.fields
    assert "currency" in resp.context["form"].fields
    assert c.lines.get().exchange_rate > 0


def test_the_currency_is_frozen_once_money_or_goods_are_attached(admin_client, db):
    """Re-striking a live kelishuv in the other currency would re-read every figure
    already booked against it — a 10 000$ to'lov becoming 10 000 so'm."""
    c = _contract()
    assert not admin_client.get(
        f"/contracts/{c.pk}/edit/").context["form"].fields["currency"].disabled

    SupplierPayment.objects.create(contract=c, date="2026-07-23",
                                   amount=Decimal("100"), method="cash")
    form = admin_client.get(f"/contracts/{c.pk}/edit/").context["form"]
    assert form.fields["currency"].disabled
    # and a posted currency is ignored rather than half-applied
    admin_client.post(f"/contracts/{c.pk}/edit/", {
        "partner": c.partner_id, "currency": "uzs", "created": "2026-07-01",
        "note": "", "planned_trucks": "",
        **line_data({"id": c.lines.get().pk, "brand": "LLDPE 209AA",
                     "kg": "50000", "price": "0.96"}, initial=1)})
    c.refresh_from_db()
    assert c.currency == Currency.USD


class TestNarxBoxNamesItsCurrency:
    """A so'm kelishuv's narx box used to keep calling itself USD, so the operator
    typed 12 500 into a field labelled dollars. The label is served correct and
    base.html retitles it live off data-currency-label while the picker moves."""

    def _price_field(self, currency):
        from crm.forms import ContractLineForm
        return ContractLineForm(currency=currency).fields["price"]

    def test_the_label_follows_the_kelishuv_currency(self, db):
        assert self._price_field(Currency.USD).label == "1 kg narxi (USD)"
        assert self._price_field(Currency.UZS).label == "1 kg narxi (so'm)"

    def test_a_som_narx_drops_the_dollar_decimals(self, db):
        """0.0000 in a so'm box reads as a dollar box. Four decimals are what a
        $/kg needs — a cent per kg is dollars on a 24-tonne lot — and what a
        so'm/kg never does."""
        dollars = self._price_field(Currency.USD).widget.attrs
        sums = self._price_field(Currency.UZS).widget.attrs
        assert (dollars["step"], dollars["placeholder"]) == ("0.0001", "0.0000")
        assert (sums["step"], sums["placeholder"]) == ("1", "0")

    def test_the_box_is_marked_for_the_live_retitle(self, db):
        """Without the attribute the JS has nothing to find, and the label goes
        stale again the moment Valyuta is switched without a reload."""
        for currency in (Currency.USD, Currency.UZS):
            attrs = self._price_field(currency).widget.attrs
            assert attrs["data-currency-label"] == "1 kg narxi"

    def test_a_som_kelishuv_form_renders_the_som_label(self, admin_client, db):
        c = _contract()
        c.currency = Currency.UZS
        c.save()
        html = admin_client.get(f"/contracts/{c.pk}/edit/").content.decode()
        # The label span itself, not just the text — base.html carries both spellings
        # in a comment explaining the bug this pins.
        assert "<span>1 kg narxi (so&#x27;m)</span>" in html
        assert "<span>1 kg narxi (USD)</span>" not in html


class TestHamkorGrouping:
    """One hamkor is one block, however many kelishuv they hold. Repeating their
    name down a column made a hamkor with three kelishuv read as three hamkor, and
    hid the fact that one of them was owed in a different currency."""

    def _groups(self, client, **params):
        resp = client.get("/contracts/", {"state": "", **params})
        assert resp.status_code == 200
        return resp, resp.context["groups"]

    def test_a_hamkor_appears_once_however_many_kelishuv_they_hold(self, admin_client, db):
        partner = Partner.objects.create(name="Majid Mehdi", phone="1", city="T")
        _contract(partner=partner, kg="200", price="1.00")
        _contract(partner=partner, kg="100", price="1.20")
        _contract(partner=Partner.objects.create(name="abulqosim", phone="1", city="T"))

        _, groups = self._groups(admin_client)
        # Which hamkor lands first is the sort's business (pinned separately); what
        # this pins is that each of them appears exactly once, with all their rows.
        held = {g["partner"].name: len(g["contracts"]) for g in groups}
        assert held == {"Majid Mehdi": 2, "abulqosim": 1}
        assert len(groups) == 2

    def test_two_currencies_are_two_figures_on_the_hamkor_head(self, admin_client, db):
        """The whole point of the grouping. Majid Mehdi is owed dollars on one
        kelishuv and so'm on another; the head says both and adds neither."""
        partner = Partner.objects.create(name="Majid Mehdi", phone="1", city="T")
        _contract(partner=partner, kg="100", price="1.20")           # $120
        som = _contract(partner=partner, kg="200", price="1.00")
        som.currency = Currency.UZS
        som.exchange_rate = Decimal("12000")
        som.save()
        line = som.lines.first()
        line.currency, line.price_uzs = Currency.UZS, Decimal("12000")
        line.exchange_rate = Decimal("12000")
        line.save()

        _, groups = self._groups(admin_client)
        payable = dict(groups[0]["payable"])
        assert payable[Currency.USD] == Decimal("120.00")
        assert payable[Currency.UZS] == Decimal("2400000.00")

    def test_a_settled_hamkor_head_says_qarzsiz_rather_than_zero(self, admin_client, db):
        partner = Partner.objects.create(name="sobir", phone="1", city="T")
        c = _contract(partner=partner, kg="100", price="1.00")
        SupplierPayment.objects.create(contract=c, amount=Decimal("100"))

        _, groups = self._groups(admin_client)
        assert groups[0]["payable"] == []
        assert "Qarzsiz" in _.content.decode()

    def test_paging_never_splits_a_hamkor_across_two_pages(self, admin_client, db):
        """Paging the flat list cut a hamkor wherever their 20th kelishuv fell, so
        the rest turned up on page two under a second copy of their name."""
        partner = Partner.objects.create(name="vazifadon", phone="1", city="T")
        for _i in range(25):
            _contract(partner=partner)
        resp, groups = self._groups(admin_client)
        assert len(groups) == 1
        assert len(groups[0]["contracts"]) == 25
        assert resp.context["page"].paginator.count == 1     # one hamkor, one page

    def test_every_kelishuv_is_drawn_under_its_hamkor(self, admin_client, db):
        """No opener, no folding: the head summarises the block and the kelishuvlar
        are simply listed beneath it."""
        partner = Partner.objects.create(name="Majid Mehdi", phone="1", city="T")
        a = _contract(partner=partner, brand="7000 campaund")
        b = _contract(partner=partner, brand="2102")

        html = self._groups(admin_client)[0].content.decode()
        assert "hamkor-toggle" not in html and "Ko'rish" not in html
        for c in (a, b):
            assert c.code in html

    def test_the_head_totals_the_block_in_both_currencies(self, admin_client, db):
        """Jami answers "how much business is this", Qolgan to'lov "how much is
        still owed" — and neither adds a dollar figure to a so'm one."""
        partner = Partner.objects.create(name="Majid Mehdi", phone="1", city="T")
        _contract(partner=partner, kg="100", price="1.20")            # jami $120
        som = _contract(partner=partner, kg="200", price="1.00")
        som.currency, som.exchange_rate = Currency.UZS, Decimal("12000")
        som.save()
        line = som.lines.first()
        line.currency, line.price_uzs = Currency.UZS, Decimal("12000")
        line.exchange_rate = Decimal("12000")
        line.save()

        _, groups = self._groups(admin_client)
        total = dict(groups[0]["total"])
        assert total[Currency.USD] == Decimal("120.00")
        assert total[Currency.UZS] == Decimal("2400000.00")
