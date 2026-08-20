"""QA audit — Logist (freight agent): the money we park with an outside person.

Probes the four symptoms the product owner reported, against the newest feature in
the app: round-trip fidelity of a LogistPayment typed in either currency, whether a
saved figure moves when nothing about it was edited, whether the currency the
operator picked survives a save-and-reopen, and whether the two aggregate screens
(the logist list tiles and the kassa tiles) still equal the sum of their rows once
payments MIX currencies and MIX kurs values.

Every test either passes (documenting real, intended behaviour) or is xfail with a
BUG: reason naming a defect that was re-checked against the source docstrings.
"""

from decimal import Decimal

import pytest

from conftest import make_contract, logist_payment_rows
from crm.models import (
    LEGACY_RATE, Logist, LogistPayment, Shipment, ShipmentExpense, ShipmentStatus,
    logist_positions,
)

# ── helpers ──────────────────────────────────────────────────────────────────────

PAY_DEFAULTS = {"currency": "usd", "amount": "1000", "exchange_rate": "12000",
                "method": "cash", "fee_percent": "0", "note": ""}


def _logist(name="Sardor aka"):
    return Logist.objects.create(name=name, phone="+998901112233")


def _pay_body(logist, **over):
    """The create modal's POST — the shared logist and sana, plus ONE top-up row.

    The modal takes several (part naqd, part perechisleniya); these probe how one row
    is stored, so `over` overrides that row's own fields — except `date`, which
    belongs to the settlement rather than to one way it moved."""
    date = over.pop("date", "2026-07-01")
    return logist_payment_rows({**PAY_DEFAULTS, **over}, logist=logist, date=date)


def _send(client, logist, **over):
    """Fund a logist through the REAL view. Returns the created row."""
    before = set(LogistPayment.objects.values_list("pk", flat=True))
    resp = client.post("/logist-payments/new/", _pay_body(logist, **over))
    assert resp.status_code == 302, resp.status_code
    return LogistPayment.objects.exclude(pk__in=before).get()


def _form_body(form):
    """What a browser would submit for an UNTOUCHED form: every visible field at
    the value the server rendered into it."""
    body = {}
    for bound in form:
        value = bound.value()
        if value is None:
            value = ""
        value = getattr(value, "pk", value)
        body[bound.html_name] = str(value)
    return body


def _shipment_body(contract, logist=None, advance="", line_id=None):
    line = contract.lines.first()
    body = {
        "contract": contract.pk, "status": ShipmentStatus.objects.first().pk,
        "sent": "2026-07-05", "eta": "2026-07-15", "responsible": "",
        "driver_name": "Akmal aka", "driver_phone": "", "transport": "",
        "container": "", "note": "", "driver_advance": advance,
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0" if line_id is None else "1",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "10",
        "lines-0-contract_line": line.pk, "lines-0-kg": "24000",
        "lines-0-price": "", "lines-0-currency": "usd",
        "lines-0-exchange_rate": "12000",
        "lines-0-id": "" if line_id is None else str(line_id),
    }
    if logist is not None:
        body["logist"] = logist.pk
    return body


def _dispatch(client, logist=None, advance="", contract=None):
    contract = contract or make_contract(kg="24000", price="1.00")
    resp = client.post("/shipments/new/", _shipment_body(contract, logist, advance))
    assert resp.status_code == 302, resp.status_code
    return Shipment.objects.get(), contract


def _resave(client, shipment, contract, logist=None, advance=""):
    body = _shipment_body(contract, logist, advance, line_id=shipment.lines.first().pk)
    resp = client.post(f"/shipments/{shipment.pk}/edit/", body)
    assert resp.status_code == 302, resp.status_code


# ── (a) ROUND-TRIP: the typed side is stored exact ───────────────────────────────


def test_a_dollar_top_up_stores_the_typed_dollar_exact(admin_client, db):
    logist = _logist()
    payment = _send(admin_client, logist, currency="usd", amount="1234.56",
                    exchange_rate="12345")
    assert payment.currency == "usd"
    assert payment.amount == Decimal("1234.56")            # typed, untouched
    assert payment.amount_uzs == Decimal("15240643.20")    # derived once
    assert payment.exchange_rate == Decimal("12345.00")


def test_a_som_top_up_stores_the_typed_som_exact(admin_client, db):
    """The so'm figure is the one that was actually handed over; it must survive
    byte-for-byte, and the dollar side is the one allowed to round."""
    logist = _logist()
    payment = _send(admin_client, logist, currency="uzs", amount="12345678",
                    exchange_rate="12345")
    assert payment.currency == "uzs"
    assert payment.amount_uzs == Decimal("12345678.00")
    assert payment.amount == Decimal("1000.05")            # 12 345 678 / 12 345


def test_the_typed_som_is_never_rederived_from_its_own_dollar(admin_client, db):
    logist = _logist()
    payment = _send(admin_client, logist, currency="uzs", amount="12345678",
                    exchange_rate="12345")
    round_tripped = payment.amount * payment.exchange_rate
    assert round_tripped == Decimal("12345617.25")         # what drift would look like
    assert payment.amount_uzs != round_tripped


# ── (c) CURRENCY STICKINESS ──────────────────────────────────────────────────────


def test_the_edit_screen_reopens_on_the_currency_that_was_saved(admin_client, db):
    logist = _logist()
    payment = _send(admin_client, logist, currency="uzs", amount="12000000",
                    exchange_rate="12000")
    resp = admin_client.get(f"/logist-payments/{payment.pk}/edit/")
    assert resp.status_code == 200
    assert resp.context["form"]["currency"].value() == "uzs"
    assert 'value="uzs" selected' in resp.content.decode()


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_the_edit_screen_shows_the_figure_the_operator_actually_typed(admin_client, db):
    """MoneyEntryFormMixin stores the typed side exact but never puts it back in the
    box on edit; only ReturnForm (forms.py:913) does that by hand. Every so'm row in
    the app therefore reopens showing its dollar twin under a So'm label."""
    logist = _logist()
    payment = _send(admin_client, logist, currency="uzs", amount="12000000",
                    exchange_rate="12000")
    form = admin_client.get(f"/logist-payments/{payment.pk}/edit/").context["form"]
    assert form["amount"].value() == Decimal("12000000.00")


# Regression guard. This was an xfail documenting the so'm-edit defect; it passes
# since MoneyEntryFormMixin._seed_typed_side (crm/forms.py) opens a so'm row showing
# its so'm figure. Kept as a test so the defect cannot come back.
def test_resaving_a_som_top_up_untouched_moves_no_money(admin_client, db):
    """(b) IDEMPOTENCE, the 'values change by themselves' report. The body posted
    here is literally what the server rendered into the form."""
    logist = _logist()
    payment = _send(admin_client, logist, currency="uzs", amount="12000000",
                    exchange_rate="12000")
    form = admin_client.get(f"/logist-payments/{payment.pk}/edit/").context["form"]
    resp = admin_client.post(f"/logist-payments/{payment.pk}/edit/", _form_body(form))
    assert resp.status_code == 302
    payment.refresh_from_db()
    assert payment.amount == Decimal("1000.00")
    assert payment.amount_uzs == Decimal("12000000.00")
    assert logist.balance == Decimal("1000.00")


def test_resaving_a_dollar_top_up_untouched_moves_no_money(admin_client, db):
    """The dollar path is the one that survives, which is why the so'm one above
    went unnoticed: same form, same button, only the picker differs."""
    logist = _logist()
    payment = _send(admin_client, logist, currency="usd", amount="1234.56",
                    exchange_rate="12345")
    for _ in range(2):
        form = admin_client.get(f"/logist-payments/{payment.pk}/edit/").context["form"]
        admin_client.post(f"/logist-payments/{payment.pk}/edit/", _form_body(form))
    payment.refresh_from_db()
    assert payment.amount == Decimal("1234.56")
    assert payment.amount_uzs == Decimal("15240643.20")
    assert payment.currency == "usd"


# ── the driver advance (migration 0032) ──────────────────────────────────────────


def test_the_advance_lands_on_the_paid_side_exactly_once(admin_client, db):
    logist = _logist()
    _send(admin_client, logist, amount="10000")
    _dispatch(admin_client, logist, advance="500")
    assert ShipmentExpense.objects.filter(is_driver_advance=True).count() == 1
    logist.refresh_from_db()
    assert logist.received_total == Decimal("10000.00")
    assert logist.paid_total == Decimal("500.00")          # subtracted, not added
    assert logist.balance == Decimal("9500.00")


def test_resaving_the_yuk_neither_duplicates_nor_double_counts_the_advance(
        admin_client, db):
    logist = _logist()
    _send(admin_client, logist, amount="10000")
    shipment, contract = _dispatch(admin_client, logist, advance="500")
    for _ in range(2):
        _resave(admin_client, shipment, contract, logist, advance="500")
    assert ShipmentExpense.objects.filter(is_driver_advance=True).count() == 1
    logist.refresh_from_db()
    assert logist.paid_total == Decimal("500.00")
    assert logist.balance == Decimal("9500.00")


# Regression guard. This was an xfail documenting the defect that sync_driver_advance
# re-derived an advance's so'm value from logist.latest_rate on EVERY yuk save, so a
# later top-up at a new kurs silently re-priced cash handed over months earlier. An
# advance booked keeps its own kurs now; only a move to another logist re-rates it.
def test_a_later_top_up_does_not_re_price_an_advance_already_handed_over(
        admin_client, db):
    """forms.py:470 states the rule it then breaks on edit: "re-rating it at today's
    kurs would give it a so'm value that money never had". Creating stores the kurs;
    re-saving the yuk (to fix a plate number, say) overwrites it."""
    logist = _logist()
    _send(admin_client, logist, amount="10000", exchange_rate="12000")
    shipment, contract = _dispatch(admin_client, logist, advance="500")
    advance = ShipmentExpense.objects.get(is_driver_advance=True)
    assert advance.exchange_rate == Decimal("12000.00")
    assert advance.amount_uzs == Decimal("6000000.00")

    _send(admin_client, logist, amount="5000", exchange_rate="13000",
          date="2026-07-20")
    _resave(admin_client, shipment, contract, logist, advance="500")

    advance.refresh_from_db()
    assert advance.exchange_rate == Decimal("12000.00")
    assert advance.amount_uzs == Decimal("6000000.00")


def test_correcting_the_advance_figure_keeps_the_kurs_it_was_handed_over_at(
        admin_client, db):
    """Restating how much the driver got is a correction to the SAME handover, so it
    is re-figured at that handover's kurs — not at whatever the logist has since been
    topped up at."""
    logist = _logist()
    _send(admin_client, logist, amount="10000", exchange_rate="12000")
    shipment, contract = _dispatch(admin_client, logist, advance="500")
    _send(admin_client, logist, amount="5000", exchange_rate="13000",
          date="2026-07-20")

    _resave(admin_client, shipment, contract, logist, advance="600")

    advance = ShipmentExpense.objects.get(is_driver_advance=True)
    assert advance.amount == Decimal("600.00")
    assert advance.exchange_rate == Decimal("12000.00")
    assert advance.amount_uzs == Decimal("7200000.00")     # 600 × 12 000, not 13 000


def test_moving_the_advance_to_another_logist_takes_that_logists_kurs(
        admin_client, db):
    """The one case that DOES re-rate: the money now comes out of somebody else's
    float, so it is worth what their funding was converted at."""
    first, second = _logist(), _logist("Bekzod aka")
    _send(admin_client, first, amount="10000", exchange_rate="12000")
    _send(admin_client, second, amount="10000", exchange_rate="13000")
    shipment, contract = _dispatch(admin_client, first, advance="500")

    _resave(admin_client, shipment, contract, second, advance="500")

    advance = ShipmentExpense.objects.get(is_driver_advance=True)
    assert advance.logist_id == second.pk
    assert advance.exchange_rate == Decimal("13000.00")
    assert advance.amount_uzs == Decimal("6500000.00")
    assert first.balance == Decimal("10000.00")            # handed back
    assert second.balance == Decimal("9500.00")


def test_an_advance_for_an_unfunded_logist_uses_the_legacy_rate(admin_client, db):
    """latest_rate's documented fallback — an advance must still be recordable for
    somebody we have never sent money to."""
    logist = _logist()
    assert logist.latest_rate == LEGACY_RATE
    _dispatch(admin_client, logist, advance="500")
    advance = ShipmentExpense.objects.get(is_driver_advance=True)
    assert advance.exchange_rate == Decimal("12000.00")
    assert advance.amount_uzs == Decimal("6000000.00")
    assert logist.balance == Decimal("-500.00")            # we owe them


def test_latest_rate_follows_the_newest_top_up_by_date_not_by_entry_order(
        admin_client, db):
    """A back-dated top-up entered afterwards must not become "the latest"."""
    logist = _logist()
    _send(admin_client, logist, amount="1000", exchange_rate="13000",
          date="2026-07-20")
    _send(admin_client, logist, amount="1000", exchange_rate="11000",
          date="2026-06-01")                               # entered late, dated early
    assert logist.latest_rate == Decimal("13000.00")


# ── (d) AGGREGATE CONSISTENCY across mixed currencies and mixed rates ────────────


def test_the_balance_adds_up_over_top_ups_in_both_currencies_at_two_rates(
        admin_client, db):
    logist = _logist()
    _send(admin_client, logist, currency="usd", amount="500", exchange_rate="12000")
    _send(admin_client, logist, currency="uzs", amount="6500000",
          exchange_rate="13000", date="2026-07-10")
    assert logist.received_total == Decimal("1000.00")     # 500 + 6 500 000/13 000
    assert logist.received_total_uzs == Decimal("12500000.00")
    _dispatch(admin_client, logist, advance="400")
    logist.refresh_from_db()
    assert logist.paid_total == Decimal("400.00")
    assert logist.paid_total_uzs == Decimal("5200000.00")  # at the newest kurs, 13 000
    assert logist.balance == Decimal("600.00")
    assert logist.balance_uzs == Decimal("7300000.00")


def test_the_list_tiles_equal_the_sum_of_the_rows_on_the_list(admin_client, db):
    """Per currency on both sides now: the tiles are the heaps the rows below hold,
    and a dollar heap is never folded into a so'm one to make a single figure."""
    from crm.models import Currency
    holder, ower = _logist("Ushlab turgan"), _logist("Qarzdor")
    _send(admin_client, holder, amount="5000", exchange_rate="12500")
    _dispatch(admin_client, ower, advance="800")
    ctx = admin_client.get("/logists/").context
    rows = list(ctx["page"])
    assert dict(ctx["held"]) == {Currency.USD: sum(
        (r.balance for r in rows if r.balance > 0), Decimal("0"))}
    assert dict(ctx["owed"]) == {Currency.USD: -sum(
        (r.balance for r in rows if r.balance < 0), Decimal("0"))}


def test_a_dollar_only_logist_publishes_no_som_gap_at_all(admin_client, db):
    """Two dollar top-ups at different kurs values and one dollar advance that exactly
    cancels them. Not one so'm was ever handed to this logist.

    The old blended hisob reported a 500 000 so'm hole here — 12 500 000 in against
    13 000 000 out, purely because the advance was restated at the newer kurs. It was
    filed as a bug that the figure vanished from the tiles when the dollar side hit
    zero; the real problem was the figure existing. Per-currency, every row is a dollar
    row, the dollar side is square, and the board says nothing about so'm.

    `balance_uzs` still carries the restatement for the detail page's running column —
    it is simply not a position any more."""
    from crm.models import Currency
    logist = _logist()
    _send(admin_client, logist, amount="500", exchange_rate="12000")
    _send(admin_client, logist, amount="500", exchange_rate="13000",
          date="2026-07-10")
    _dispatch(admin_client, logist, advance="1000")
    logist.refresh_from_db()
    assert logist.balance == Decimal("0.00")
    assert logist.balance_uzs == Decimal("-500000.00")

    assert logist.balance_by_currency() == []
    held, owed = logist_positions()
    assert (held, owed) == ([], [])
    assert "500&nbsp;000 so&#x27;m" not in admin_client.get("/kassa/?davr=all").content.decode()


# ── the bank foiz on a top-up ────────────────────────────────────────────────────


def test_a_bank_foiz_is_charged_to_exactly_one_side(admin_client, db):
    """The cut comes off ONE side, not both ends of it.

    It is ours: the logist is funded the whole 1 000 and the kassa is out 1 020. The
    figure used to be taken off both at once — funded 980 while costing 1 020 —
    which billed the same 20 twice."""
    logist = _logist()
    ours = _send(admin_client, logist, amount="1000", method="transfer",
                 fee_percent="2", exchange_rate="12000")
    assert ours.fee_amount == Decimal("20.00")
    assert ours.net_amount == Decimal("1000.00")           # funded in full
    assert ours.total_out == Decimal("1020.00")            # what the kassa lost
    assert logist.received_total == Decimal("1000.00")


def test_the_foiz_cannot_be_pushed_onto_the_logist_through_the_post(admin_client, db):
    """The form stopped asking whose the cut is (see `LogistPaymentForm`), so a
    hand-made POST still carrying the old answer changes nothing. Otherwise the rule
    would hold only as far as the browser, and the figure that gets saved is the one
    place it matters."""
    logist = _logist()
    row = _send(admin_client, logist, amount="1000", method="transfer",
                fee_percent="2", exchange_rate="12000", fee_bearer="counterparty")
    assert row.fee_bearer == ""
    assert row.fee_on_company
    assert row.net_amount == Decimal("1000.00")            # funded in full anyway
    assert row.total_out == Decimal("1020.00")


def test_the_kassa_loses_the_top_up_and_the_foiz_and_nothing_else(admin_client, db):
    """One payment must not be spent twice: the advance prices the yuk but must not
    reappear as cash leaving the till."""
    logist = _logist()
    _send(admin_client, logist, amount="10000", method="transfer", fee_percent="2")
    _dispatch(admin_client, logist, advance="500")
    ctx = admin_client.get("/kassa/?davr=all").context
    assert ctx["net_out"] == Decimal("10200.00")
    rows = ctx["outflow_page"].paginator.object_list
    assert sum(r["amount"] for r in rows) == Decimal("10200.00")
    assert ctx["waterfall"][-1]["running"] == ctx["cash_total"] == Decimal("-10200.00")


@pytest.mark.xfail(reason="BUG: the kassa waterfall's Bank foizi step sums the foiz "
                          "on EVERY expense including logist-funded ones, whose "
                          "cash never left the till — the waterfall stops landing "
                          "on the Kassada figure",
                   strict=False)
def test_the_waterfall_still_closes_when_a_logist_paid_a_yuk_bojxona_by_transfer(
        admin_client, db):
    """views.py:2196 builds `fees` from all `expenses`, but `cash_total` scores a
    logist-funded expense at zero (ShipmentExpense.total_out). A logist paying a
    load's bojxona is explicitly supported — models.py:1770 says so."""
    logist = _logist()
    _send(admin_client, logist, amount="10000")
    shipment, _ = _dispatch(admin_client, logist, advance="500")
    ShipmentExpense.objects.create(
        shipment=shipment, date="2026-07-06", category="customs",
        amount=Decimal("3200"), amount_uzs=Decimal("38400000"),
        exchange_rate=Decimal("12000"), method="transfer",
        fee_percent=Decimal("2"), logist=logist)
    ctx = admin_client.get("/kassa/?davr=all").context
    assert ctx["cash_total"] == Decimal("-10000.00")
    assert ctx["waterfall"][-1]["running"] == ctx["cash_total"]


def test_a_logist_paid_bojxona_comes_off_their_balance_once(admin_client, db):
    """Documented intent (models.py:1764): anything a logist funded is drawn from
    the float we sent them, driver advance or not."""
    logist = _logist()
    _send(admin_client, logist, amount="10000")
    shipment, _ = _dispatch(admin_client, logist, advance="500")
    ShipmentExpense.objects.create(
        shipment=shipment, date="2026-07-06", category="customs",
        amount=Decimal("3200"), amount_uzs=Decimal("38400000"),
        exchange_rate=Decimal("12000"), method="cash", logist=logist)
    logist.refresh_from_db()
    assert logist.paid_total == Decimal("3700.00")
    assert logist.balance == Decimal("6300.00")


# ── deleting a row other rows lean on ────────────────────────────────────────────


def test_deleting_a_top_up_leaves_the_balance_and_the_kassa_consistent(
        admin_client, db):
    logist = _logist()
    first = _send(admin_client, logist, amount="10000", exchange_rate="12000")
    _send(admin_client, logist, amount="5000", exchange_rate="13000",
          date="2026-07-10")
    _dispatch(admin_client, logist, advance="500")
    resp = admin_client.post(f"/logist-payments/{first.pk}/delete/", {})
    assert resp.status_code == 302
    logist.refresh_from_db()
    assert logist.received_total == Decimal("5000.00")
    assert logist.received_total_uzs == Decimal("65000000.00")
    assert logist.balance == Decimal("4500.00")
    from crm.models import Currency
    held, owed = logist_positions()
    assert (held, owed) == ([(Currency.USD, Decimal("4500.00"))], [])
    assert admin_client.get("/kassa/?davr=all").context["net_out"] == Decimal("5000.00")


def test_deleting_the_only_top_up_does_not_re_price_the_advance_it_paid_for(
        admin_client, db):
    """The advance's so'm value is stored, not looked up — it must not fall back to
    the legacy rate just because the funding row is gone."""
    logist = _logist()
    payment = _send(admin_client, logist, amount="10000", exchange_rate="13000")
    _dispatch(admin_client, logist, advance="500")
    advance = ShipmentExpense.objects.get(is_driver_advance=True)
    assert advance.amount_uzs == Decimal("6500000.00")
    admin_client.post(f"/logist-payments/{payment.pk}/delete/", {})
    advance.refresh_from_db()
    assert advance.exchange_rate == Decimal("13000.00")
    assert advance.amount_uzs == Decimal("6500000.00")
    logist.refresh_from_db()
    assert logist.balance == Decimal("-500.00")
    assert logist.balance_uzs == Decimal("-6500000.00")


# ── boundaries ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("amount", ["0", "-250"])
def test_a_top_up_of_nothing_or_less_is_refused(admin_client, db, amount):
    logist = _logist()
    resp = admin_client.post("/logist-payments/new/", _pay_body(logist, amount=amount))
    assert resp.status_code == 200                         # re-rendered, invalid
    assert not LogistPayment.objects.exists()


@pytest.mark.parametrize("rate", ["", "0"])
def test_a_som_top_up_with_no_usable_kurs_is_refused(admin_client, db, rate):
    """A logist's float is kept in dollars, so so'm sent to them converts into it —
    and without a rate there is no saying how much they ended up holding."""
    logist = _logist()
    resp = admin_client.post("/logist-payments/new/",
                             _pay_body(logist, currency="uzs", amount="12000000",
                                       exchange_rate=rate))
    assert resp.status_code == 200
    assert not LogistPayment.objects.exists()


@pytest.mark.parametrize("rate", ["", "0"])
def test_a_dollar_top_up_asks_for_no_kurs_at_all(admin_client, db, rate):
    """Dollars into a dollar float cross nothing: the summa IS what the logist ends
    up holding. The row still takes a rate for the kassa's so'm column."""
    logist = _logist()
    resp = admin_client.post("/logist-payments/new/",
                             _pay_body(logist, exchange_rate=rate))
    assert resp.status_code == 302
    payment = LogistPayment.objects.get()
    assert payment.amount == Decimal("1000.00")
    assert payment.exchange_rate > 0
    assert logist.balance == Decimal("1000.00")


def test_a_cleared_foiz_box_does_not_break_the_save(admin_client, db):
    """The foiz field is optional on the model, so an operator clearing it must not
    take the whole to'lov down with it."""
    logist = _logist()
    resp = admin_client.post("/logist-payments/new/",
                             _pay_body(logist, method="transfer", fee_percent=""))
    assert resp.status_code == 302
    payment = LogistPayment.objects.get()
    assert payment.fee_percent == Decimal("0.00")
    assert payment.fee_amount == Decimal("0")
    assert logist.received_total == Decimal("1000.00")


def test_a_som_top_up_below_a_cent_keeps_the_som_figure_handed_over(admin_client, db):
    """Boundary: under half a cent the dollar side has nowhere to go, but the so'm
    side is the figure that was actually handed over and must survive exactly."""
    logist = _logist()
    payment = _send(admin_client, logist, currency="uzs", amount="50",
                    exchange_rate="12000")
    assert payment.amount_uzs == Decimal("50.00")
    assert payment.amount == Decimal("0.00")


@pytest.mark.xfail(reason="BUG (low): uzs_slice() short-circuits to 0 whenever the "
                          "row's dollar column is zero, so a so'm row whose dollar "
                          "twin rounded to 0.00 is dropped from received_total_uzs "
                          "entirely — its so'm value is discarded, not rounded",
                   strict=False)
def test_a_som_row_whose_dollar_twin_rounds_to_zero_still_counts_in_som(
        admin_client, db):
    """models.py:1616 guards on `row.amount` to avoid a divide-by-zero, but the
    fallback throws the row's whole so'm value away instead of passing it through.
    Bounded by half a cent per row, so it is a correctness smell rather than a
    money leak — but the aggregate genuinely stops equalling the sum of its rows."""
    logist = _logist()
    _send(admin_client, logist, currency="uzs", amount="50", exchange_rate="12000")
    assert logist.received_total_uzs == Decimal("50.00")
