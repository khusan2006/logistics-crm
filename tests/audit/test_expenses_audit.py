"""QA audit: Xarajat (ShipmentExpense) — the grid modal, the single-row edit form,
and the totals that read off them.

Run:
    TEST_DB_SUFFIX=_expenses .venv/bin/python -m pytest tests/audit/test_expenses_audit.py -q

Probe families, mapped to the symptoms the product owner reported:
  (a) round-trip      — the typed side must survive bit-exact at the given kurs
  (b) idempotence     — re-saving an untouched row must move no figure
  (c) stickiness      — a so'm row must come back as a so'm row, showing so'm
  (d) aggregates      — a printed total must equal the rows printed beside it

Everything marked xfail(BUG: …) is a defect I believe is genuine; the reasoning
for each is in the docstring of the test itself.
"""
from decimal import Decimal

import pytest

from crm.forms import ExpenseGridForm, ShipmentExpenseForm, ShipmentForm
from crm.models import (
    Contract, ContractLine, Logist, LogistPayment, Partner, Shipment,
    ShipmentExpense, ShipmentLine, ShipmentStatus,
)


# --- payload helpers -------------------------------------------------------

def grid(shipment, date="2026-07-10", currency="usd", method="cash",
         exchange_rate="12000", note="", fee_percent="0", **amounts):
    """POST payload for the xarajat grid modal — the same shape tests/test_expenses.py
    uses, so a failure here is not a failure of my payload builder."""
    data = {"shipment": shipment.pk, "date": date, "currency": currency,
            "method": method, "exchange_rate": exchange_rate, "note": note,
            "fee_percent": fee_percent}
    for category, value in amounts.items():
        data[f"amount_{category}"] = str(value)
    return data


def rendered_edit_payload(expense, **over):
    """Exactly what the browser would POST if the operator opened the tahrirlash
    modal and clicked Saqlash without touching anything.

    Built from the BoundField values rather than from the model, because that is
    what the <input> elements actually carry — which is the whole point of an
    idempotence probe: it must round-trip through the form the user sees."""
    form = ShipmentExpenseForm(instance=expense)
    data = {}
    for name in form.fields:
        value = form[name].value()
        data[name] = "" if value is None else str(value)
    data.update({k: ("" if v is None else str(v)) for k, v in over.items()})
    return data


def money_snapshot(expense):
    expense.refresh_from_db()
    return (expense.currency, expense.amount, expense.amount_uzs,
            expense.exchange_rate)


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("20000"), price=Decimal("1.00"),
                                price_uzs=Decimal("12000"))
    obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.first())
    ShipmentLine.objects.create(shipment=obj, contract_line=contract.lines.first(),
                                kg=Decimal("10000"))
    return obj


# --- (a) ROUND-TRIP --------------------------------------------------------

def test_a_dollar_box_stores_the_typed_dollars_bit_exact(admin_client, shipment):
    """USD typed → the USD column is the typed figure untouched, the so'm column is
    the only derived side (convert_pair's contract)."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="usd", exchange_rate="12345.67",
                           customs="3200.55"))
    e = ShipmentExpense.objects.get()
    assert e.amount == Decimal("3200.55")
    assert e.amount_uzs == Decimal("39512934.12")     # 3200.55 × 12 345.67
    assert e.currency == "usd"


def test_a_som_box_stores_the_typed_som_bit_exact(admin_client, shipment):
    """so'm typed → the so'm column is the typed figure untouched, even when the
    dollar side cannot be represented exactly."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12650",
                           customs="1234567.89"))
    e = ShipmentExpense.objects.get()
    assert e.amount_uzs == Decimal("1234567.89")      # typed side, untouched
    assert e.amount == Decimal("97.59")               # 1 234 567.89 / 12 650
    # And the derived side must NOT be re-derived from itself: 97.59 × 12 650 is
    # 1 234 513.50, which is not what the operator typed.
    assert e.amount_uzs != (e.amount * e.exchange_rate)


def test_every_box_in_the_grid_converts_at_the_one_shared_kurs(admin_client, shipment):
    """Seven boxes, one Valyuta and one kurs above them — each box must use THAT
    kurs, not a per-box default."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12500",
                           customs="1250000", declarant="250000",
                           transport="62500", loader="12500"))
    rows = {e.category: e for e in ShipmentExpense.objects.all()}
    assert {e.exchange_rate for e in rows.values()} == {Decimal("12500.00")}
    assert rows["customs"].amount == Decimal("100.00")
    assert rows["declarant"].amount == Decimal("20.00")
    assert rows["transport"].amount == Decimal("5.00")
    assert rows["loader"].amount == Decimal("1.00")
    assert sum(e.amount_uzs for e in rows.values()) == Decimal("1575000.00")


def test_a_per_turkum_currency_override_still_uses_the_shared_kurs(admin_client, shipment):
    """The override picks the currency only; sana and kurs describe the trip."""
    admin_client.post("/expenses/new/", dict(
        grid(shipment, currency="uzs", exchange_rate="12800",
             customs="1280000", loader="640000"),
        currency_customs="usd"))
    rows = {e.category: e for e in ShipmentExpense.objects.all()}
    boj = rows["customs"]
    assert boj.currency == "usd"
    assert boj.amount == Decimal("1280000.00")               # typed as dollars
    assert boj.amount_uzs == Decimal("16384000000.00")       # × 12 800
    gruz = rows["loader"]
    assert gruz.currency == "uzs" and gruz.amount == Decimal("50.00")


def test_rounding_lands_on_the_cent_quantum_half_up(admin_client, shipment):
    """Lump sums are 2dp USD (MoneyEntryFormMixin.usd_places). 5 so'm at 1 000 is
    exactly half a tiyin of a dollar and must go up, not to even."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="1000", other="5"))
    e = ShipmentExpense.objects.get()
    assert e.amount == Decimal("0.01")
    assert e.amount_uzs == Decimal("5.00")


# --- (b) IDEMPOTENCE / NO DRIFT -------------------------------------------

def test_resaving_a_dollar_expense_unchanged_moves_nothing(admin_client, shipment):
    """Open the tahrirlash modal, click Saqlash, twice. Nothing may move."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="usd", exchange_rate="12650",
                           customs="3200.55"))
    e = ShipmentExpense.objects.get()
    before = money_snapshot(e)
    for _ in range(2):
        resp = admin_client.post(f"/expenses/{e.pk}/edit/", rendered_edit_payload(e))
        assert resp.status_code in (200, 302), resp.status_code
        assert money_snapshot(e) == before


def test_resaving_a_dollar_expense_with_one_unrelated_field_changed_moves_no_money(
        admin_client, shipment):
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="usd", exchange_rate="12650",
                           customs="3200.55"))
    e = ShipmentExpense.objects.get()
    before = money_snapshot(e)
    admin_client.post(f"/expenses/{e.pk}/edit/",
                      rendered_edit_payload(e, note="bojxona kvitansiyasi"))
    assert money_snapshot(e) == before
    admin_client.post(f"/expenses/{e.pk}/edit/",
                      rendered_edit_payload(e, note="ikkinchi izoh"))
    assert money_snapshot(e) == before


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_expense_unchanged_moves_nothing(admin_client, shipment):
    """The "values change by themselves" symptom, reproduced end to end.

    crm/forms.py:97 MoneyEntryFormMixin says the operator types `amount` IN
    `currency` — it even strips "(USD)" off the label for that reason — but
    ShipmentExpenseForm never swaps the initial to `amount_uzs` for a so'm row the
    way ReturnForm (crm/forms.py:916) does. So the box shows the derived USD side
    while the Valyuta picker says So'm, and clean() converts that dollar figure as
    if it were so'm."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12650",
                           customs="1265000"))
    e = ShipmentExpense.objects.get()
    before = money_snapshot(e)
    assert before == ("uzs", Decimal("100.00"), Decimal("1265000.00"),
                      Decimal("12650.00"))
    for _ in range(2):
        admin_client.post(f"/expenses/{e.pk}/edit/", rendered_edit_payload(e))
        assert money_snapshot(e) == before


def test_editing_an_expense_never_makes_a_second_row(admin_client, shipment):
    admin_client.post("/expenses/new/", grid(shipment, customs="100", loader="50"))
    assert ShipmentExpense.objects.count() == 2
    e = ShipmentExpense.objects.get(category="customs")
    admin_client.post(f"/expenses/{e.pk}/edit/", rendered_edit_payload(e, note="x"))
    admin_client.post(f"/expenses/{e.pk}/edit/", rendered_edit_payload(e, note="y"))
    assert ShipmentExpense.objects.count() == 2


@pytest.mark.xfail(reason="BUG: ShipmentForm.sync_driver_advance re-derives the "
                          "advance's so'm value from logist.latest_rate on EVERY "
                          "yuk save, so a later top-up at a new kurs silently "
                          "re-rates an advance that was already handed over",
                   strict=False)
def test_resaving_a_yuk_does_not_re_rate_an_already_recorded_driver_advance(
        admin_client, db):
    """crm/forms.py:493 states the rule outright — "re-rating it at today's kurs
    would give it a so'm value that money never had". The implementation reads
    `logist.latest_rate` (crm/models.py:266, the MOST RECENT top-up) unconditionally
    on every save, so editing an unrelated field on the yuk moves the advance's
    so'm figure and with it the yuk's tannarx in so'm."""
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand="LLDPE",
                                kg=Decimal("24000"), price=Decimal("1.00"),
                                price_uzs=Decimal("12000"))
    logist = Logist.objects.create(name="Sardor aka")
    LogistPayment.objects.create(logist=logist, date="2026-07-01",
                                 amount=Decimal("10000"),
                                 amount_uzs=Decimal("120000000"),
                                 exchange_rate=Decimal("12000"), method="cash")

    def body(**over):
        line = contract.lines.first()
        data = {"contract": contract.pk, "status": ShipmentStatus.objects.first().pk,
                "sent": "2026-07-05", "eta": "2026-07-15", "logist": logist.pk,
                "responsible": "", "driver_name": "Akmal aka", "driver_phone": "",
                "transport": "", "container": "", "note": "",
                "driver_advance": "500",
                "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "10",
                "lines-0-contract_line": line.pk, "lines-0-kg": "24000",
                "lines-0-price": "", "lines-0-currency": "usd",
                "lines-0-exchange_rate": "12000", "lines-0-id": ""}
        data.update(over)
        return data

    admin_client.post("/shipments/new/", body())
    advance = ShipmentExpense.objects.get(is_driver_advance=True)
    assert advance.amount_uzs == Decimal("6000000.00")      # 500 × 12 000

    # The logist is topped up again, later, at a different kurs. Nothing about the
    # advance already handed to the driver has changed.
    LogistPayment.objects.create(logist=logist, date="2026-07-20",
                                 amount=Decimal("5000"),
                                 amount_uzs=Decimal("64000000"),
                                 exchange_rate=Decimal("12800"), method="cash")

    ship = Shipment.objects.get()
    line = ship.lines.first()
    admin_client.post(f"/shipments/{ship.pk}/edit/",
                      body(transport="01 A 123 BB", **{"lines-INITIAL_FORMS": "1",
                                                       "lines-0-id": line.pk}))
    advance.refresh_from_db()
    assert advance.exchange_rate == Decimal("12000.00")
    assert advance.amount_uzs == Decimal("6000000.00")


# --- (c) CURRENCY STICKINESS ----------------------------------------------

def test_a_som_grid_submission_saves_som_rows_holding_the_typed_figure(
        admin_client, shipment):
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12650",
                           customs="1265000", loader="126500"))
    rows = {e.category: e for e in ShipmentExpense.objects.all()}
    assert {e.currency for e in rows.values()} == {"uzs"}
    assert rows["customs"].amount_uzs == Decimal("1265000.00")
    assert rows["loader"].amount_uzs == Decimal("126500.00")
    # ...and NOT a USD-interpreted figure sitting in the dollar column
    assert rows["customs"].amount == Decimal("100.00")


def test_reopening_the_edit_form_comes_back_bound_to_som(admin_client, shipment):
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12650",
                           customs="1265000"))
    e = ShipmentExpense.objects.get()
    html = admin_client.get(f"/expenses/{e.pk}/edit/",
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    assert ShipmentExpenseForm(instance=e)["currency"].value() == "uzs"
    assert '<option value="uzs" selected>' in html


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_form_shows_the_som_figure_for_a_som_row(admin_client, shipment):
    """The same defect as the re-save drift, isolated to what is rendered.

    ReturnForm does this correctly (`self.initial["price"] = self.sale.price_uzs`
    when the sale is in so'm); ShipmentExpenseForm has no equivalent."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12650",
                           customs="1265000"))
    e = ShipmentExpense.objects.get()
    assert ShipmentExpenseForm(instance=e)["amount"].value() == Decimal("1265000.00")


def test_switching_a_dollar_row_to_som_on_edit_sticks(admin_client, shipment):
    """Typing a so'm figure into a row that was in dollars must land as a so'm row
    holding that exact so'm figure."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="usd", exchange_rate="12650",
                           customs="100"))
    e = ShipmentExpense.objects.get()
    admin_client.post(f"/expenses/{e.pk}/edit/",
                      rendered_edit_payload(e, currency="uzs", amount="1265000"))
    e.refresh_from_db()
    assert e.currency == "uzs"
    assert e.amount_uzs == Decimal("1265000.00")
    assert e.amount == Decimal("100.00")


# --- (d) AGGREGATE CONSISTENCY --------------------------------------------

def test_the_yuk_total_equals_the_sum_of_rows_across_mixed_currencies_and_kurs(
        admin_client, shipment):
    """Two submissions at different kursi, one in so'm and one in dollars — the
    figure printed under the table must be the plain sum of both columns."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12500",
                           customs="1250000"))
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="usd", exchange_rate="13000",
                           date="2026-07-12", transport="200"))
    shipment.refresh_from_db()
    rows = list(shipment.expenses.all())
    assert shipment.expenses_total == sum(e.amount for e in rows) == Decimal("300.00")
    assert (shipment.expenses_total_uzs == sum(e.amount_uzs for e in rows)
            == Decimal("3850000.00"))
    # 300 $ at "a" kurs would be 3 750 000 or 3 900 000 — neither is the truth.
    assert shipment.expenses_total_uzs != shipment.expenses_total * Decimal("12500")
    assert shipment.expenses_total_uzs != shipment.expenses_total * Decimal("13000")


def test_landed_cost_is_the_narx_plus_the_expense_share_per_kg(admin_client, shipment):
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12500",
                           customs="1250000", transport="1250000"))
    lot = shipment.lines.first()
    assert shipment.expenses_total == Decimal("200.00")
    assert shipment.expense_per_kg == Decimal("0.02")        # 200 / 10 000 kg
    assert lot.landed_cost_per_kg == Decimal("1.0200")


def test_deleting_one_expense_reprices_the_lot_by_exactly_that_row(
        admin_client, shipment):
    admin_client.post("/expenses/new/", grid(shipment, customs="300", transport="500"))
    lot = shipment.lines.first()
    assert lot.landed_cost_per_kg == Decimal("1.0800")
    boj = ShipmentExpense.objects.get(category="customs")
    admin_client.post(f"/expenses/{boj.pk}/delete/")
    shipment.refresh_from_db()
    assert not ShipmentExpense.objects.filter(pk=boj.pk).exists()
    assert shipment.lines.first().landed_cost_per_kg == Decimal("1.0500")


@pytest.mark.xfail(reason="BUG: ShipmentExpense.total_out_uzs reconverts the bank "
                          "foiz with in_som() while the kassa ledger prints "
                          "fee_amount_uzs, which SLICES the stored so'm value — "
                          "the two disagree, so the so'm chiqim total is not the "
                          "sum of the so'm rows printed beside it",
                   strict=False)
def test_the_som_chiqim_total_equals_the_som_rows_it_is_made_of(admin_client, shipment):
    """crm/models.py:139 states the rule for the so'm side of a foiz: "taken as a
    slice of the row's stored so'm value rather than reconverted". total_out_uzs
    (crm/models.py:1802) breaks it, and the kassa docstring promises "every total
    equal to the figures printed beside them"."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12650",
                           method="transfer", fee_percent="2", customs="1000000"))
    e = ShipmentExpense.objects.get()
    assert e.amount == Decimal("79.05")               # 1 000 000 / 12 650
    assert e.amount_uzs == Decimal("1000000.00")
    ledger_som = e.amount_uzs + e.fee_amount_uzs      # the two rows the kassa prints
    assert e.total_out_uzs == ledger_som
    # ...and the same gap reaches the page: the so'm chiqim total is short of the
    # so'm rows listed under it.
    ctx = admin_client.get("/kassa/").context
    rows = [r for r in ctx["outflow_page"].object_list if r["kind"].endswith("expense")]
    assert sum(r["amount_uzs"] for r in rows) == ctx["net_out_uzs"]


def test_the_kassa_som_outflow_equals_the_som_rows_of_its_own_ledger(
        admin_client, shipment):
    """The same consistency check through the real page, with a clean kurs so the
    slice-vs-reconvert difference cannot hide behind rounding."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="12500",
                           method="transfer", fee_percent="2", customs="1250000"))
    ctx = admin_client.get("/kassa/").context
    rows = [r for r in ctx["outflow_page"].object_list if r["kind"].endswith("expense")]
    assert len(rows) == 2                              # the xarajat and its foiz
    assert sum(r["amount"] for r in rows) == ctx["net_out"]
    assert sum(r["amount_uzs"] for r in rows) == ctx["net_out_uzs"]


def test_a_logist_funded_expense_costs_the_kassa_nothing(admin_client, shipment):
    """The cash left when we topped the logist up; charging it again would bill us
    twice for one payment (ShipmentExpense.total_out)."""
    logist = Logist.objects.create(name="Sardor aka")
    e = ShipmentExpense.objects.create(
        shipment=shipment, date="2026-07-10", category="customs",
        amount=Decimal("100"), amount_uzs=Decimal("1265000"),
        exchange_rate=Decimal("12650"), currency="uzs", method="cash", logist=logist)
    assert e.total_out == Decimal("0")
    assert e.total_out_uzs == Decimal("0")
    # ...but it still prices the goods
    assert shipment.expenses_total == Decimal("100.00")
    assert shipment.lines.first().landed_cost_per_kg == Decimal("1.0100")


# --- boundaries ------------------------------------------------------------

@pytest.mark.xfail(reason="BUG: MoneyEntryFormMixin.__init__ flips the grid's kurs "
                          "field to required=False and ExpenseGridForm.clean never "
                          "re-checks it, so a blank or zero kurs validates and "
                          "build() raises ValueError out of the view — a 500, not "
                          "a form error",
                   strict=False)
def test_a_missing_or_zero_kurs_is_a_form_error_not_a_crash(admin_client, shipment):
    """The mechanism: ExpenseGridForm declares a required kurs, but the money mixin
    un-requires it (its own clean() is a no-op here — the grid has no field named
    `amount` for it to look at), and ExpenseGridForm.clean never re-checks. So the
    form validates and convert_pair raises out of build()."""
    assert ExpenseGridForm().fields["exchange_rate"].required is False  # ← the mixin
    admin_client.raise_request_exception = False
    for rate in ("", "0"):
        resp = admin_client.post("/expenses/new/",
                                 grid(shipment, exchange_rate=rate, customs="100"),
                                 HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        assert resp.status_code == 422, rate
        assert not ShipmentExpense.objects.exists()


def test_a_zero_box_is_rejected_rather_than_saved_as_a_zero_row(admin_client, shipment):
    resp = admin_client.post("/expenses/new/", grid(shipment, customs="0", loader="50"),
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 422
    assert not ShipmentExpense.objects.exists()


def test_a_huge_and_a_tiny_kurs_both_survive_the_2dp_quantum(admin_client, shipment):
    """A boundary worth knowing about: at an absurd kurs a real so'm cost lands as
    $0.00 and stops pricing the goods, while still showing a so'm figure. Not a bug
    on its own (2dp is the documented USD quantum) — recorded so the behaviour is
    not mistaken for a conversion fault later. A tiny kurs is the mirror image and
    must not blow up either."""
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="uzs", exchange_rate="1000000",
                           customs="1000"))
    e = ShipmentExpense.objects.get()
    assert e.amount == Decimal("0.00")
    assert e.amount_uzs == Decimal("1000.00")
    assert e.fee_amount_uzs == Decimal("0")            # guarded, no ZeroDivision
    assert shipment.expense_per_kg == Decimal("0")

    ShipmentExpense.objects.all().delete()
    admin_client.post("/expenses/new/",
                      grid(shipment, currency="usd", exchange_rate="0.01",
                           customs="100"))
    tiny = ShipmentExpense.objects.get()
    assert tiny.amount == Decimal("100.00")            # typed side, exact
    assert tiny.amount_uzs == Decimal("1.00")


def test_a_fee_over_a_hundred_percent_is_refused(admin_client, shipment):
    resp = admin_client.post("/expenses/new/",
                             grid(shipment, method="transfer", fee_percent="150",
                                  customs="100"),
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert resp.status_code == 422
    assert not ShipmentExpense.objects.exists()


def test_a_fee_on_a_cash_row_is_ignored_not_charged(admin_client, shipment):
    """The method may be overridden per box; a foiz must follow the row's OWN
    method, not the shared one."""
    admin_client.post("/expenses/new/", dict(
        grid(shipment, method="transfer", fee_percent="2",
             customs="1000", loader="500"),
        method_loader="cash"))
    rows = {e.category: e for e in ShipmentExpense.objects.all()}
    assert rows["customs"].fee_amount == Decimal("20.00")
    assert rows["loader"].fee_amount == Decimal("0")
    assert rows["loader"].total_out == Decimal("500.00")


def test_deleting_the_yuk_takes_its_expenses_with_it(admin_client, shipment):
    """CASCADE, so nothing is left pointing at a load that no longer exists."""
    admin_client.post("/expenses/new/", grid(shipment, customs="100", loader="50"))
    assert ShipmentExpense.objects.count() == 2
    shipment.delete()
    assert ShipmentExpense.objects.count() == 0


def test_the_grid_never_saves_a_logist_funded_row(admin_client, shipment):
    """Pinning the current behaviour, because it is a live miscalculation risk.

    The grid — the only create path in the UI — has no Logist picker at all, so
    every xarajat entered the normal way is charged to the kassa. The single-row
    form, on the same yuk, defaults the picker to the yuk's own logist. Two entry
    points, two different answers to "did this money leave the kassa?"."""
    logist = Logist.objects.create(name="Sardor aka")
    shipment.logist = logist
    shipment.save()
    LogistPayment.objects.create(logist=logist, date="2026-07-01",
                                 amount=Decimal("1000"), amount_uzs=Decimal("12000000"),
                                 exchange_rate=Decimal("12000"), method="cash")
    assert "logist" not in ExpenseGridForm().fields
    admin_client.post("/expenses/new/", grid(shipment, customs="300"))
    grid_row = ShipmentExpense.objects.get()
    assert grid_row.logist_id is None and grid_row.from_kassa is True
    # the single-row form, on the same yuk, pre-selects the logist instead
    assert ShipmentExpenseForm(initial={"shipment": shipment})["logist"].value() \
        == logist.pk
    # Consequence when the logist really did pay it: 1 300 shown as gone from the
    # kassa though only the 1 000 top-up left, and the logist still reads as
    # holding the full 1 000 they have already spent 300 of.
    ctx = admin_client.get("/kassa/").context
    assert ctx["net_out"] == Decimal("1300.00")
    logist.refresh_from_db()
    assert logist.balance == Decimal("1000.00")


def test_the_shared_sana_and_kurs_are_not_overridable_per_box(admin_client, shipment):
    form = ExpenseGridForm()
    for category, _ in ShipmentExpense.Category.choices:
        assert f"date_{category}" not in form.fields
        assert f"exchange_rate_{category}" not in form.fields
        assert f"currency_{category}" in form.fields
        assert f"method_{category}" in form.fields


def test_the_grid_modal_renders_currency_and_method_as_radio_groups(
        admin_client, shipment):
    """The new modal draws both as segmented radio controls (crm/_seg_field.html).
    A <select> here would mean the whole choice is hidden behind a click."""
    html = admin_client.get("/expenses/new/?shipment=%d" % shipment.pk,
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
    for name, values in (("currency", ("usd", "uzs")),
                         ("method", ("cash", "card", "transfer"))):
        for value in values:
            assert f'name="{name}" value="{value}"' in html, (name, value)
    assert html.count('type="radio"') == 5
