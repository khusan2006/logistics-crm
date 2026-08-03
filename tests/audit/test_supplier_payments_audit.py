"""AUDIT — Hamkor to'lovlari (supplier payments) + vositachi foizi.

Diagnosis pass, TRIAGED. Every test here either PASSES (it asserts the behaviour the
source documents as intended) or is xfail-marked with a defect this file proves.

The area's rules, from the source:
  * `convert_pair` — the side the operator TYPED is stored exact; only the other
    side is derived (crm/models.py:37).
  * `SupplierPayment` — on the way OUT the vositachi cut and the bank foiz ride ON
    TOP of the summa; the hamkor is credited the full `amount` either way
    (crm/models.py:645).
  * every percentage in this area is taken off the DOLLAR column — `commission_amount`
    (crm/models.py:682) and `CashEntry.fee_amount` (crm/models.py:130) both read
    `self.amount` — and only then rated into so'm. `amount` is the canonical column;
    the so'm twin is the derived one even on a so'm-typed row.

Triage of the previous tester's five unvalidated xfail claims:
  UPHELD    edit form of a so'm to'lov renders the dollar column (known root cause)
  UPHELD    re-saving that form divides the so'm summa by the kurs (same root cause)
  WITHDRAWN "the cut on a so'm to'lov is an exact 2% of the typed so'm" — no rule in
            this codebase produces that figure; see
            test_the_cut_is_a_percentage_of_the_dollar_column
  UPHELD    the kassa ledger row and the kassa/list totals compute the same cut two
            different ways
  WITHDRAWN "SupplierPayment.fee_amount_uzs must use the slice rule" — the slice rule
            exists for a DEDUCTED foiz; here the foiz rides on top; see
            test_the_bank_foiz_in_som_is_the_same_figure_everywhere
"""
from decimal import Decimal

import pytest

from conftest import make_contract, make_shipment
from crm.models import (
    Currency, ShipmentStatus, SupplierPayment, commission_total,
)

RATE = Decimal("12650")


# --- helpers ---------------------------------------------------------------

def _contract(kg="40000", price="1.00", shipped=True):
    """A kelishuv worth kg × price, with the whole lot already sent so there is a
    real qarz to pay against."""
    contract = make_contract(kg=kg, price=price)
    if shipped:
        make_shipment(contract=contract, kg=kg, status=ShipmentStatus.objects.first())
    return contract


def _payload(contract, **extra):
    data = {"contract": contract.pk, "date": "2026-07-20", "currency": "usd",
            "amount": "100", "exchange_rate": str(RATE), "commission_percent": "",
            "method": "cash", "fee_percent": "0", "note": ""}
    data.update({k: str(v) for k, v in extra.items()})
    return data


def _create(client, contract, **extra):
    resp = client.post("/supplier-payments/new/", _payload(contract, **extra))
    return resp


def _rendered_post(form, **changes):
    """Exactly what the browser would submit if the operator opened the tahrirlash
    modal and pressed Saqlash, having touched only the fields in `changes`."""
    data = {}
    for name in form.fields:
        value = form[name].value()
        data[name] = "" if value is None else str(value)
    data.update({k: str(v) for k, v in changes.items()})
    return data


def _money(payment):
    return (payment.currency, payment.amount, payment.amount_uzs, payment.exchange_rate)


def _ledger(client):
    """The kassa chiqim ledger, keyed by row kind (one to'lov per test)."""
    return {r["kind"]: r for r in client.get("/kassa/").context["outflow_page"]}


# --- (a) ROUND-TRIP --------------------------------------------------------

def test_a_dollar_tolov_keeps_the_typed_dollar_exact(admin_client, db):
    """USD tomonda yozilgani — aynan terilgan raqam; so'm tomoni undan hosil."""
    contract = _contract()
    assert _create(admin_client, contract, amount="1234.56").status_code == 302
    p = SupplierPayment.objects.get()
    assert p.currency == Currency.USD
    assert p.amount == Decimal("1234.56")
    assert p.amount_uzs == (Decimal("1234.56") * RATE).quantize(Decimal("0.01"))


def test_a_som_tolov_keeps_the_typed_som_exact(admin_client, db):
    """So'mda terilgan summa hech qachon o'z konvertatsiyasidan qayta hosil
    qilinmaydi — 1 000 000 so'm 1 000 000 bo'lib qoladi."""
    contract = _contract()
    assert _create(admin_client, contract, currency="uzs",
                   amount="1000000").status_code == 302
    p = SupplierPayment.objects.get()
    assert p.currency == Currency.UZS
    assert p.amount_uzs == Decimal("1000000.00")          # typed side, untouched
    assert p.amount == Decimal("79.05")                   # 1 000 000 / 12 650
    # the give-away: re-deriving the typed side would land 17.50 so'm short
    assert (p.amount * RATE).quantize(Decimal("0.01")) == Decimal("999982.50")
    assert p.amount_uzs != (p.amount * RATE).quantize(Decimal("0.01"))


def test_a_som_tolov_keeps_its_tiyin(admin_client, db):
    """Tiyingacha terilgan so'm ham aynan saqlanadi — so'm ustuni ikki xonali."""
    contract = _contract()
    assert _create(admin_client, contract, currency="uzs",
                   amount="126505000.55").status_code == 302
    p = SupplierPayment.objects.get()
    assert p.amount_uzs == Decimal("126505000.55")
    assert p.amount == Decimal("10000.40")


def test_the_derived_dollar_side_rounds_half_up(admin_client, db):
    """6 388,25 so'm / 12 650 = 0,505 $ — yarmi yuqoriga (ROUND_HALF_UP)."""
    contract = _contract()
    assert _create(admin_client, contract, currency="uzs",
                   amount="6388.25").status_code == 302
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("0.51")
    assert p.amount_uzs == Decimal("6388.25")


def test_a_kurs_of_zero_is_refused_in_both_directions(admin_client, db):
    """Kurssiz pul qatorining ikkinchi tomoni yo'q — saqlanmaydi."""
    contract = _contract()
    for currency in ("usd", "uzs"):
        resp = _create(admin_client, contract, currency=currency,
                       amount="100", exchange_rate="0")
        assert resp.status_code == 200
    assert not SupplierPayment.objects.exists()


def test_zero_and_negative_summa_are_refused(admin_client, db):
    contract = _contract()
    for amount in ("0", "-100"):
        assert _create(admin_client, contract, amount=amount).status_code == 200
    assert not SupplierPayment.objects.exists()


# --- (c) CURRENCY STICKINESS ----------------------------------------------

def test_a_som_tolov_saves_as_som(admin_client, db):
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000")
    p = SupplierPayment.objects.get()
    assert p.currency == Currency.UZS
    assert p.is_som
    assert p.amount_uzs == Decimal("126505000.00")


def test_the_edit_form_reopens_bound_to_som(admin_client, db):
    """Valyuta tanlovi so'mda qolishi kerak — dollarga qaytib ketmasin."""
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000")
    p = SupplierPayment.objects.get()
    form = admin_client.get(f"/supplier-payments/{p.pk}/edit/").context["form"]
    assert form["currency"].value() == Currency.UZS
    assert form["exchange_rate"].value() == RATE


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_form_shows_the_som_figure_that_was_typed(admin_client, db):
    """Summa maydonida qaysi valyuta tanlangan bo'lsa, o'sha raqam turishi kerak.

    ReturnForm (crm/forms.py:917) already does exactly this for a so'm sotuv:
    `self.initial["price"] = self.sale.price_uzs`. MoneyEntryFormMixin never does
    it for an existing row, so every so'm money row edits in dollars."""
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000")
    p = SupplierPayment.objects.get()
    form = admin_client.get(f"/supplier-payments/{p.pk}/edit/").context["form"]
    assert Decimal(str(form["amount"].value())) == p.amount_uzs


def test_switching_a_dollar_tolov_to_som_really_switches_it(admin_client, db):
    """Valyutani so'mga o'zgartirish saqlanadi — qator so'mda qoladi."""
    contract = _contract()
    _create(admin_client, contract, amount="1000")
    p = SupplierPayment.objects.get()
    resp = admin_client.post(
        f"/supplier-payments/{p.pk}/edit/",
        _payload(contract, currency="uzs", amount="12650000"))
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.currency == Currency.UZS
    assert p.amount_uzs == Decimal("12650000.00")
    assert p.amount == Decimal("1000.00")


def test_switching_a_som_tolov_back_to_dollars_really_switches_it(admin_client, db):
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="12650000")
    p = SupplierPayment.objects.get()
    resp = admin_client.post(
        f"/supplier-payments/{p.pk}/edit/",
        _payload(contract, currency="usd", amount="1000"))
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.currency == Currency.USD
    assert p.amount == Decimal("1000.00")
    assert p.amount_uzs == Decimal("12650000.00")


def test_the_valyuta_picker_reinterprets_the_number_in_the_box(admin_client, db):
    """Hujjatlashtirilgan xatti-harakat, nuqson emas: Summa qutisidagi raqam
    tanlangan valyutada o'qiladi. Aynan shuning uchun ham yuqoridagi urug'lantirish
    nuqsoni qimmatga tushadi — qutida dollar turadi, picker esa So'm deydi."""
    contract = _contract()
    _create(admin_client, contract, amount="1234.56")
    p = SupplierPayment.objects.get()
    resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/",
                             _payload(contract, currency="uzs", amount="1234.56"))
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.currency == Currency.UZS
    assert p.amount_uzs == Decimal("1234.56")     # the same digits, now so'm
    assert p.amount == Decimal("0.10")


def test_the_currency_survives_an_edit_that_only_changes_the_izoh(admin_client, db):
    """So'm qatorining valyutasi, kursi va summasi izoh o'zgarganda joyida qoladi
    (summa qayta terilgan holda — forma urug'lantirish nuqsonini chetlab)."""
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000")
    p = SupplierPayment.objects.get()
    before = _money(p)
    resp = admin_client.post(
        f"/supplier-payments/{p.pk}/edit/",
        _payload(contract, currency="uzs", amount="126505000", note="qayta tekshirildi"))
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.note == "qayta tekshirildi"
    assert _money(p) == before


# --- (b) IDEMPOTENCE / NO-DRIFT -------------------------------------------

def test_resaving_a_dollar_tolov_untouched_moves_nothing(admin_client, db):
    """Tahrirlash oynasini ochib, hech narsaga tegmay Saqlash bosish — raqamlar
    joyida qolishi kerak (ikki marta ham)."""
    contract = _contract()
    _create(admin_client, contract, amount="1234.56", commission_percent="2",
            method="transfer", fee_percent="1.5")
    p = SupplierPayment.objects.get()
    before = _money(p)
    for _ in range(2):
        form = admin_client.get(f"/supplier-payments/{p.pk}/edit/").context["form"]
        resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/", _rendered_post(form))
        assert resp.status_code == 302
        p.refresh_from_db()
    assert _money(p) == before


def test_changing_only_the_foiz_on_a_dollar_tolov_leaves_the_summa_alone(admin_client, db):
    """Faqat vositachi foizini o'zgartirish summani qimirlatmasligi kerak."""
    contract = _contract()
    _create(admin_client, contract, amount="1234.56", commission_percent="2")
    p = SupplierPayment.objects.get()
    before = _money(p)
    form = admin_client.get(f"/supplier-payments/{p.pk}/edit/").context["form"]
    resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/",
                             _rendered_post(form, commission_percent="3"))
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.commission_percent == Decimal("3.00")
    assert _money(p) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_tolov_untouched_moves_nothing(admin_client, db):
    """Bu — "raqamlar o'z-o'zidan o'zgarib ketyapti" shikoyatining o'zi."""
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000")
    p = SupplierPayment.objects.get()
    before = _money(p)
    for _ in range(2):
        form = admin_client.get(f"/supplier-payments/{p.pk}/edit/").context["form"]
        resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/", _rendered_post(form))
        assert resp.status_code == 302
        p.refresh_from_db()
    assert _money(p) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_changing_only_the_foiz_on_a_som_tolov_leaves_the_summa_alone(admin_client, db):
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000",
            commission_percent="2")
    p = SupplierPayment.objects.get()
    before = _money(p)
    form = admin_client.get(f"/supplier-payments/{p.pk}/edit/").context["form"]
    resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/",
                             _rendered_post(form, commission_percent="3"))
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.commission_percent == Decimal("3.00")
    assert _money(p) == before


def test_reposting_the_same_som_figure_twice_is_stable(admin_client, db):
    """Konvertatsiyaning o'zi barqaror: bir xil so'm raqamini qayta yuborsak,
    hech narsa siljimaydi. Demak yuqoridagi nuqson formaning ko'rsatishida."""
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000")
    p = SupplierPayment.objects.get()
    before = _money(p)
    for _ in range(2):
        resp = admin_client.post(
            f"/supplier-payments/{p.pk}/edit/",
            _payload(contract, currency="uzs", amount="126505000"))
        assert resp.status_code == 302
        p.refresh_from_db()
    assert _money(p) == before


# --- foiz rides ON TOP -----------------------------------------------------

def test_the_cut_and_the_bank_foiz_ride_on_top_of_the_summa(admin_client, db):
    """Hamkor to'liq summani oladi; vositachi va bank ulushi ustiga qo'shiladi."""
    contract = _contract()
    _create(admin_client, contract, amount="10000", commission_percent="2",
            method="transfer", fee_percent="1")
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("10000.00")
    assert p.commission_amount == Decimal("200.00")
    assert p.fee_amount == Decimal("100.00")
    assert p.total_out == Decimal("10300.00")
    # the hamkor's qarz falls by what they RECEIVE, never by the charges on top
    assert contract.paid_total == Decimal("10000.00")
    assert contract.debt == Decimal("30000.00")


def test_no_side_of_the_total_is_converted_twice(admin_client, db):
    """Dollarda kiritilgan qatorda so'm jami — dollar jamining aynan kursdagi
    aksi; hech bir bo'lak ikki marta konvert qilinmaydi."""
    contract = _contract()
    _create(admin_client, contract, amount="10000", commission_percent="2",
            method="transfer", fee_percent="1")
    p = SupplierPayment.objects.get()
    assert p.total_out_uzs == p.amount_uzs + p.commission_amount_uzs + p.fee_amount_uzs
    assert p.total_out_uzs == (p.total_out * RATE).quantize(Decimal("0.01"))


def test_the_bank_foiz_is_only_charged_on_a_perechisleniya(admin_client, db):
    contract = _contract()
    _create(admin_client, contract, amount="10000", method="cash", fee_percent="5")
    p = SupplierPayment.objects.get()
    assert p.fee_percent == Decimal("5.00")       # stored, as typed
    assert p.fee_amount == Decimal("0")           # but never charged on naqd
    assert p.total_out == p.amount


# CLAIM WITHDRAWN. The original test here demanded commission_amount_uzs ==
# 2 530 100,00, i.e. an exact 2% of the TYPED so'm. No rule in this codebase produces
# that figure: every percentage in the area is taken off the DOLLAR column
# (commission_amount, crm/models.py:682, and CashEntry.fee_amount, crm/models.py:130,
# both read `self.amount`), and the dollar column of a so'm row is itself a rounded
# derivation (126 505 000 / 12 650 = 10 000,3952 -> 10 000,40). That cent quantum is
# worth ~60 so'm, which is the whole of the ~26 so'm gap. The claimed figure matches
# neither the code (2 530 126,50) nor the documented uzs_slice rule (2 530 125,30),
# so it was evidence of nothing. The real defect in this neighbourhood is the
# two-formulas-for-one-figure split, kept as an xfail below.
def test_the_cut_is_a_percentage_of_the_dollar_column(admin_client, db):
    """Vositachi ulushi — hamkor oladigan summaning (dollar ustuni) foizi, keyin
    shu qatorning o'z kursida so'mga o'giriladi."""
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000",
            commission_percent="2")
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("10000.40")
    assert p.commission_amount == Decimal("200.01")        # 2% of the dollar column
    # Rated at the row's own kurs: 2 530 126,50 as the code stands, 2 530 125,30 if
    # the uzs_slice rule of the xfail below is applied instead — NOT the 2 530 100
    # the original claim demanded.
    assert Decimal("2530125") < p.commission_amount_uzs < Decimal("2530127")
    assert p.total_out == Decimal("10200.41")


@pytest.mark.xfail(reason="BUG: one figure, two formulas, both printed on /kassa/. "
                          "The chiqim ledger row uses uzs_slice(p, cut) "
                          "(crm/views.py:2096) while SupplierPayment.commission_amount_uzs "
                          "(crm/models.py:694) uses in_som(cut) — and that is what the "
                          "to'lovlar list, the kassa waterfall step (crm/views.py:2192) "
                          "and total_out_uzs all feed on. On a 126 505 000 so'm to'lov "
                          "at 2% the ledger row shows 2 530 125,30 and the waterfall "
                          "2 530 126,50, so the visible chiqim rows sum to "
                          "130 300 125,30 against a Chiqim total of 130 300 126,50",
                   strict=False)
def test_the_kassa_row_and_the_list_agree_on_the_cut(admin_client, db):
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000",
            commission_percent="2")
    p = SupplierPayment.objects.get()
    ctx = admin_client.get("/kassa/").context
    ledger = {r["kind"]: r for r in ctx["outflow_page"]}
    assert ledger["commission"]["amount_uzs"] == p.commission_amount_uzs
    # and therefore the rows on screen add up to the total printed above them
    assert (sum((r["amount_uzs"] for r in ctx["outflow_page"]), Decimal("0"))
            == ctx["net_out_uzs"])


# CLAIM WITHDRAWN. The original test here demanded fee_amount_uzs == uzs_slice(p, fee),
# calling the SupplierPayment override of CashEntry.fee_amount_uzs a defect. The
# base-class slice rule protects an identity that does not exist here: where the foiz
# is DEDUCTED (CustomerPayment.net_amount_uzs, crm/models.py:1552) net + foiz must add
# back up to the stored so'm value, and only the slice guarantees that. On a hamkor
# to'lov the foiz rides ON TOP (crm/models.py:687), so there is nothing to reconcile —
# and both formulas rate at THIS row's kurs, which is the stated purpose of the rule.
# The two differ by 0,60 so'm on 126 505 000, and the override is what keeps the
# ledger row, the kassa foiz subtotal and total_out_uzs on a single figure.
def test_the_bank_foiz_in_som_is_the_same_figure_everywhere(admin_client, db):
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000",
            method="transfer", fee_percent="1")
    p = SupplierPayment.objects.get()
    assert p.fee_amount == Decimal("100.00")               # 1% of the dollar column
    # 1 265 000,00 as the code stands; 1 264 999,40 under the slice rule. Either way
    # the ledger row, the model property and the total have to be ONE number.
    assert Decimal("1264999") < p.fee_amount_uzs < Decimal("1265001")
    assert _ledger(admin_client)["fee_supplier"]["amount_uzs"] == p.fee_amount_uzs
    assert p.total_out_uzs == p.amount_uzs + p.commission_amount_uzs + p.fee_amount_uzs


# --- (d) AGGREGATE CONSISTENCY --------------------------------------------

def test_the_list_totals_equal_the_rows_across_mixed_currencies_and_kurslar(admin_client, db):
    """Uch xil valyuta/kursdagi qatorlar — sarlavhadagi jami aynan qatorlar
    yig'indisi bo'lishi kerak."""
    contract = _contract(kg="80000")
    _create(admin_client, contract, amount="10000", exchange_rate="12000",
            commission_percent="2", method="transfer", fee_percent="1")
    _create(admin_client, contract, currency="uzs", amount="126505000",
            exchange_rate="12650", commission_percent="1.5")
    _create(admin_client, contract, currency="uzs", amount="13500000",
            exchange_rate="13500", method="transfer", fee_percent="0.5")
    rows = list(SupplierPayment.objects.all())
    assert len(rows) == 3

    ctx = admin_client.get("/supplier-payments/").context
    assert ctx["total_paid"] == sum((r.amount for r in rows), Decimal("0"))
    assert ctx["total_paid_uzs"] == sum((r.amount_uzs for r in rows), Decimal("0"))
    assert ctx["total_out"] == sum((r.total_out for r in rows), Decimal("0"))
    assert ctx["total_out_uzs"] == sum((r.total_out_uzs for r in rows), Decimal("0"))
    # and the kelishuv's own view of what it has paid is the same set of rows
    assert contract.paid_total == ctx["total_paid"]
    assert contract.paid_total_uzs == ctx["total_paid_uzs"]


def test_commission_total_matches_the_rows_it_summed(admin_client, db):
    contract = _contract(kg="80000")
    _create(admin_client, contract, amount="333.33", commission_percent="2.5")
    _create(admin_client, contract, currency="uzs", amount="126505000",
            commission_percent="1.5")
    rows = list(SupplierPayment.objects.all())
    assert commission_total(rows) == sum((r.commission_amount for r in rows), Decimal("0"))
    assert commission_total(rows) == contract.commission_accrued


def test_the_kassa_chiqim_rows_add_up_to_the_chiqim_total(admin_client, db):
    """Dollarda kiritilgan qatorda kassa chiqim jadvalining uchta qatori — summa,
    vositachi, bank foizi — aynan Chiqim jamini beradi."""
    contract = _contract()
    _create(admin_client, contract, amount="10000", commission_percent="2",
            method="transfer", fee_percent="1")
    ctx = admin_client.get("/kassa/").context
    rows = list(ctx["outflow_page"])
    assert {r["kind"] for r in rows} == {"supplier", "commission", "fee_supplier"}
    assert sum((r["amount"] for r in rows), Decimal("0")) == ctx["net_out"]
    assert sum((r["amount_uzs"] for r in rows), Decimal("0")) == ctx["net_out_uzs"]
    assert ctx["net_out"] == Decimal("10300.00")


def test_the_hamkor_qarzi_in_som_falls_by_the_typed_som(admin_client, db):
    """Hamkorning so'mdagi qarzi to'lovning so'm ustuni bo'yicha kamayadi —
    dollardan qayta hisoblangan raqam bo'yicha emas."""
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000")
    assert contract.paid_total_uzs == Decimal("126505000.00")
    assert contract.paid_total == Decimal("10000.40")
    assert contract.debt_uzs == contract.shipped_value_uzs - Decimal("126505000.00")


def test_deleting_the_only_tolov_empties_every_total(admin_client, db):
    contract = _contract()
    _create(admin_client, contract, currency="uzs", amount="126505000",
            commission_percent="2", method="transfer", fee_percent="1")
    p = SupplierPayment.objects.get()
    assert admin_client.post(f"/supplier-payments/{p.pk}/delete/").status_code == 302
    assert not SupplierPayment.objects.exists()
    assert contract.paid_total == Decimal("0")
    assert contract.paid_total_uzs == Decimal("0")
    assert contract.commission_accrued == Decimal("0")
    ctx = admin_client.get("/supplier-payments/").context
    assert ctx["total_paid"] == ctx["total_paid_uzs"] == Decimal("0")
    assert ctx["total_out"] == ctx["total_out_uzs"] == Decimal("0")
    assert admin_client.get("/kassa/").context["net_out_uzs"] == Decimal("0")


# --- boundaries ------------------------------------------------------------

def test_a_som_tolov_smaller_than_one_cent_credits_the_hamkor_nothing(admin_client, db):
    """Chegara: 3 so'mlik to'lov dollar ustunida 0 bo'lib qoladi, shuning uchun
    hamkorning qarzi umuman kamaymaydi — so'm jamida esa ko'rinadi."""
    contract = _contract()
    assert _create(admin_client, contract, currency="uzs", amount="3").status_code == 302
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("0.00")
    assert p.amount_uzs == Decimal("3.00")
    assert contract.paid_total == Decimal("0.00")        # the cent quantum swallows it
    assert contract.paid_total_uzs == Decimal("3.00")
    assert p.total_out == Decimal("0.00")


def test_a_tiny_kurs_still_round_trips(admin_client, db):
    """Kichkina kurs (1 $ = 1 so'm) — konvertatsiya sinmaydi."""
    contract = _contract()
    assert _create(admin_client, contract, currency="uzs", amount="500",
                   exchange_rate="1").status_code == 302
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("500.00") and p.amount_uzs == Decimal("500.00")


def test_the_cap_is_on_what_the_hamkor_receives_not_on_what_leaves_the_kassa(
        admin_client, db):
    """Vositachi ulushi shiftga kirmaydi: 40 000$ lik kelishuvga 40 000$ to'lash
    mumkin, kassadan 40 800$ chiqsa ham."""
    contract = _contract(kg="40000", price="1.00")
    assert _create(admin_client, contract, amount="40000",
                   commission_percent="2").status_code == 302
    p = SupplierPayment.objects.get()
    assert p.total_out == Decimal("40800.00")
    assert contract.payable_left == Decimal("0.00")


def test_the_cap_is_measured_on_the_dollar_column(admin_client, db):
    """Shift dollar ustunida o'lchanadi: 1 000 $ lik kelishuvga 12 650 006 so'm
    o'tadi (dollarda 1 000,00), 12 660 000 so'm esa rad etiladi."""
    contract = _contract(kg="1000", price="1.00")       # 1 000 $
    assert _create(admin_client, contract, currency="uzs",
                   amount="12650006").status_code == 302
    SupplierPayment.objects.all().delete()
    resp = _create(admin_client, contract, currency="uzs", amount="12660000")
    assert resp.status_code == 200
    assert "oshib ketdi" in resp.context["form"].errors["amount"][0]
    assert not SupplierPayment.objects.exists()


def test_editing_a_tolov_counts_only_the_other_rows_against_the_cap(admin_client, db):
    """Tahrirlashda o'z summasi shiftdan chegirilmasligi kerak — aks holda
    o'zgartirmasdan saqlash ham "oshib ketdi" deb rad etiladi."""
    contract = _contract(kg="1000", price="1.00")       # 1 000 $
    _create(admin_client, contract, amount="1000")
    p = SupplierPayment.objects.get()
    resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/",
                             _payload(contract, amount="1000"))
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.amount == Decimal("1000.00")


def test_deleting_a_tolov_takes_its_cut_back_out_of_the_tannarx(admin_client, db):
    """Hujjatlashtirilgan xatti-harakat: vositachi ulushi JONLI — to'lovni
    o'chirsak, har bir yukning tannarxi darhol qaytadi (models.py:493)."""
    contract = _contract(kg="20000", price="1.00")
    lot = contract.shipments.first().lines.first()
    base = lot.landed_cost_per_kg
    _create(admin_client, contract, amount="10000", commission_percent="2")
    p = SupplierPayment.objects.get()
    lot.refresh_from_db()
    assert lot.landed_cost_per_kg == base + Decimal("0.0100")   # 200$ / 20 000 kg
    admin_client.post(f"/supplier-payments/{p.pk}/delete/")
    assert not SupplierPayment.objects.exists()
    lot.refresh_from_db()
    assert lot.landed_cost_per_kg == base
