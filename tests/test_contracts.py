from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from conftest import line_data, make_contract, make_shipment
from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentLine, ShipmentStatus, SupplierPayment,
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
    return resp, [c.pk for c in resp.context["page"].object_list]


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
        "partner": p.pk, "created": "2026-07-04", "note": "",
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
            "partner": p.pk, "created": "2026-07-05", "note": "",
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
    assert "1.25" in html and "0.80" in html          # each product's own narx
    assert "$1,650.00" in html                        # 1000×1.25 + 500×0.80


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
    assert "1–2.5 $/kg" in contract_option_label(many)


def test_kelishuv_has_no_deadline(db):
    """Yetkazish muddati olib tashlandi."""
    from crm.forms import ContractForm

    assert not hasattr(_contract(), "deadline")
    assert "deadline" not in ContractForm().fields


def test_planned_trucks_is_optional_and_saved(admin_client, db):
    """Kelishuvga nechta mashina biriktirilishi — ixtiyoriy."""
    p = Partner.objects.create(name="Zamin", phone="1", city="Buxoro")
    payload = {"partner": p.pk, "created": "2026-07-05", "note": "",
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
