"""Audit of the DISPLAY layer — crm/templatetags/crm_extras.py and crm/formatting.py.

Nothing else in the suite covers these two modules end to end: tests/test_templatetags.py
checks four `usd` cases, tests/test_formatting.py checks the happy paths of the phone and
container helpers, and tests/test_dual_currency.py asserts a handful of substrings inside
rendered pages. This file pins the exact output of every filter and tag, for every shape of
input a real page can hand them.

Two properties matter more than any single figure here:

  1. EXACTNESS. The house convention is a non-breaking space between thousands, a COMMA as
     the decimal mark (so "$1,200" would read as a dollar and change), and trailing zeros
     always trimmed. Every assertion below spells out the literal string, NBSP included,
     rather than checking a substring — a substring check passes on "$1200" too.

  2. NEVER RAISING. These run on every row of every screen. An exception in one of them is
     not a wrong figure, it is a 500 that blanks the whole page, so each one is fed a
     non-numeric string, an object that cannot become a Decimal, and the three float
     specials (NaN, +Inf, -Inf).

Run:
    cd /Users/khusan/Desktop/logistic-crm && TEST_DB_SUFFIX=_fmt \
        .venv/bin/python -m pytest tests/audit/test_formatting_display_audit.py -q -p no:randomly
"""
from decimal import Decimal

import pytest
from django import forms
from django.template import Context, Template

from crm.formatting import normalize_container, phone_country, validate_intl_phone
from crm.models import Currency, Customer, CustomerPayment, PayMethod
from crm.templatetags.crm_extras import (
    NBSP, _pair, _trim, money, money_both, money_other, rate, rate_both, rate_range_both,
    som, static_v, usd,
)

#: NBSP is the whole point of the convention, so it is written as an escape here and
#: interpolated — a literal typed into an assertion is indistinguishable from a space.
NB = " "
EM_DASH = "—"
EN_DASH = "–"


class NotANumber:
    """A value no Decimal() can be built from — a model instance, a lazy string, a dict
    that slipped into a context. Decimal() answers TypeError for these, not ArithmeticError,
    which is why the filters have to catch both."""

    def __repr__(self):
        return "<NotANumber>"


#: Everything a template can plausibly push into a money filter. Used by the
#: never-raises sweep so each new filter is exercised against the whole set.
JUNK = ["abc", "1 200", "1,200", NotANumber(), float("nan"), float("inf"), float("-inf"),
        None, "", [], {}]


# ── usd ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, "$0"),                                   # blank FK / property that returned None
    ("", "$0"),                                     # an empty form field echoed back
    (0, "$0"),
    (Decimal("0.00"), "$0"),
    (42, "$42"),                                    # int
    (3.14159, "$3.14"),                             # float, rounded to the column's 2dp
    ("1234.56", f"$1{NB}234.56"),                   # numeric string
    (Decimal("48000"), f"$48{NB}000"),
    (Decimal("999999999"), f"$999{NB}999{NB}999"),  # nine digits, the so'm-sized figure
    (Decimal("1234.50"), f"$1{NB}234.5"),           # padded zero trimmed
    (Decimal("0.80"), "$0.8"),
    (Decimal("1234.5678"), f"$1{NB}234.57"),        # more precision than the column holds
    (Decimal("0.004"), "$0"),                       # sub-cent rounds away entirely
])
def test_usd_prints_the_exact_house_format(value, expected):
    """Space-grouped thousands (NBSP, not a plain space — a plain space lets a table wrap
    a nine-digit figure into two figures), no padded decimals, blank-safe."""
    assert usd(value) == expected


def test_usd_groups_with_a_non_breaking_space_not_a_plain_one():
    """The distinction the whole convention rests on. `"$48 000"` with an ordinary space
    passes any substring test and still wraps mid-number in a narrow column."""
    out = usd(Decimal("48000"))
    assert NB in out and " " not in out
    assert out.replace(NB, " ") == "$48 000"   # what it must NOT be


# ── som ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, "0 so'm"),
    ("", "0 so'm"),
    (0, "0 so'm"),
    (42, "42 so'm"),
    (3.14159, "3 so'm"),                                    # float, tiyin dropped
    ("1234.56", f"1{NB}235 so'm"),                          # numeric string, rounded
    (Decimal("12000000"), f"12{NB}000{NB}000 so'm"),
    (Decimal("999999999"), f"999{NB}999{NB}999 so'm"),      # nine digits
    (Decimal("12345678.99"), f"12{NB}345{NB}679 so'm"),     # more precision than shown
])
def test_som_prints_whole_som_only(value, expected):
    """Tiyin have not been real money for a long time and they make a nine-digit figure
    unreadable, so the so'm side is deliberately whole-numbered (crm_extras.py:52)."""
    assert som(value) == expected


# ── _trim, the 4-decimal narx helper ────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, "0"),
    ("", "0"),
    (0, "0"),
    (42, "42"),
    (Decimal("0.8140"), "0.814"),
    (Decimal("1.0000"), "1"),
    (Decimal("1.1700"), "1.17"),
    ("1.5", "1.5"),                       # numeric string
    (3.14159, "3.1416"),                  # float, rounded to the column's 4dp
    (Decimal("0.81405"), "0.814"),        # more precision than the column holds
    (Decimal("999999999"), "999999999"),  # nine digits — a narx helper does NOT group
    (Decimal("-0.5"), "-0.5"),
])
def test_trim_keeps_four_decimals_and_no_padding(value, expected):
    assert _trim(value) == expected


def test_trim_is_the_one_helper_that_may_raise_and_every_caller_wraps_it():
    """_trim is deliberately unguarded — but it is a private helper, not a registered
    filter, so a template can never reach it directly. Its three callers (rate,
    rate_both, rate_range_both) each wrap it. This pins that arrangement: if _trim ever
    gains a `@register.filter`, this test is the one that should be revisited."""
    from crm.templatetags import crm_extras

    with pytest.raises(Exception):
        _trim("abc")
    assert "_trim" not in crm_extras.register.filters
    assert "_trim" not in crm_extras.register.tags
    assert rate("abc") == EM_DASH          # …and the public wrapper absorbs it


# ── the never-raises sweep ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("junk", JUNK, ids=repr)
def test_no_money_filter_raises_on_junk(junk):
    """A filter that throws does not print a wrong number — it 500s the page it is on.
    Every one of these runs on every row of a list, so none may raise for any input.

    NaN and Infinity are in here for a reason: `Decimal(float("nan"))` succeeds, so they
    slip past the try/except and come out as literal text ("$NaN"). Ugly, but printed
    rather than fatal, which is the property being locked down."""
    for out in (usd(junk), som(junk),
                money(junk, junk, "uzs"), money(junk, junk, "usd"),
                money_other(junk, junk, "uzs"), money_other(junk, junk, "usd"),
                rate(junk, junk, "uzs"), rate(junk, junk, "usd"),
                money_both(junk, junk), rate_both(junk, junk),
                rate_range_both(junk, junk, junk, junk)):
        assert isinstance(out, str) and out != ""


def test_a_junk_value_is_never_dressed_up_as_a_real_figure_by_the_rate_tags():
    """The rate family answers the em dash rather than "0 $/kg" when it cannot read the
    value — a narx of zero and a narx that could not be parsed must not look alike."""
    assert rate("abc", None, "usd") == EM_DASH
    assert rate_both(NotANumber(), None) == EM_DASH
    assert rate_range_both("abc", "def") == EM_DASH


# ── money / money_other / rate: which side of the pair gets drawn ───────────────────

@pytest.mark.parametrize("currency", ["uzs", "UZS", Currency.UZS])
def test_money_draws_the_som_column_for_a_som_row(currency):
    """A row is drawn in the currency it was BOOKED in. `currency` is compared as a plain
    lowercased string (crm_extras.py:79), so the enum, "uzs" and "UZS" all mean so'm."""
    assert money(Decimal("1000"), Decimal("12000000"), currency) == f"12{NB}000{NB}000 so'm"


@pytest.mark.parametrize("currency", ["usd", Currency.USD, None, "", "eur"])
def test_money_draws_the_dollar_column_for_everything_that_is_not_som(currency):
    """Anything that does not read as "uzs" — including a missing currency on a legacy
    row — falls to the dollar side rather than guessing."""
    assert money(Decimal("1000"), Decimal("12000000"), currency) == f"$1{NB}000"


def test_a_som_row_with_no_som_twin_shows_an_em_dash_not_a_converted_guess():
    """The rule the tag exists to enforce (crm_extras.py:97-101): such a row predates dual
    currency and genuinely has no so'm value on record. Printing the dollar column under a
    so'm label — or converting it at some rate — would invent a figure.

    Note 0 is NOT missing: a so'm row that really is worth nothing still prints "0 so'm"."""
    assert money(Decimal("1000"), None, "uzs") == EM_DASH
    assert money(None, None, "uzs") == EM_DASH
    assert money(Decimal("1000"), Decimal("0"), "uzs") == "0 so'm"
    assert rate(Decimal("1.17"), None, "uzs") == EM_DASH


def test_money_other_is_the_exact_mirror_of_money():
    """The kassa's reference column: whichever side `money` printed, this prints the other.
    Repeating the side the operator typed says nothing; the twin at the row's own kurs is
    what answers "how much is that in dollars"."""
    usd_row = (Decimal("1000"), Decimal("12000000"), "usd")
    som_row = (Decimal("1000"), Decimal("12000000"), "uzs")
    assert money(*usd_row) == f"$1{NB}000"
    assert money_other(*usd_row) == f"12{NB}000{NB}000 so'm"
    assert money(*som_row) == f"12{NB}000{NB}000 so'm"
    assert money_other(*som_row) == f"$1{NB}000"
    assert money_other(Decimal("1000"), None, "usd") == EM_DASH     # missing twin


@pytest.mark.xfail(strict=True, reason=(
    "BUG (cosmetic, low): the missing-twin em dash guards only the so'm side. "
    "crm/templatetags/crm_extras.py:112-113 — money_other() on a so'm row returns "
    "usd(usd_value), and usd(None) is '$0' (crm_extras.py:42-48), so a row whose dollar "
    "twin is missing prints a hard '$0' in the kassa reference column instead of the '—' "
    "its mirror case gets at crm_extras.py:115. Only reachable for rows built outside the "
    "forms; the USD column is non-null with a default, so no seeded row hits it today."))
def test_money_other_should_dash_a_missing_dollar_twin_too():
    assert money_other(None, Decimal("12000000"), "uzs") == EM_DASH


def test_rate_carries_the_per_kg_suffix_and_four_decimals_on_the_dollar_side():
    """A rate is read differently from a sum: the dollar side keeps four decimals (a $/kg
    rounded to cents moves a 24-tonne lot by dollars) and both sides carry /kg."""
    assert rate(Decimal("1.1700"), Decimal("14040"), "usd") == "1.17 $/kg"
    assert rate(Decimal("0.8140"), Decimal("9768"), "usd") == "0.814 $/kg"
    assert rate(Decimal("0.8140"), Decimal("9768"), "uzs") == f"9{NB}768 so'm/kg"
    assert rate(Decimal("999999999"), None, "usd") == "999999999 $/kg"
    assert rate(Decimal("14040"), Decimal("999999999"), "uzs") == \
        f"999{NB}999{NB}999 so'm/kg"
    assert rate(None) == "0 $/kg"       # blank-safe, and NOT the em dash


# ── the both-currencies totals ──────────────────────────────────────────────────────

def test_money_both_stacks_the_som_twin_under_the_dollar_figure():
    """A total spans rows of both currencies so it cannot pick a side. The markup is one
    inline-block unit because these land mid-sentence as often as in a table cell.

    The apostrophe in so'm arrives HTML-escaped: _pair uses format_html, which escapes
    both halves (crm_extras.py:152)."""
    assert money_both(Decimal("1000"), Decimal("12500000")) == (
        f'<span class="money-pair">$1{NB}000'
        f'<span class="money-alt">12{NB}500{NB}000 so&#x27;m</span></span>')


@pytest.mark.parametrize("usd_value,som_value,expected", [
    (Decimal("1000"), None, f"$1{NB}000"),   # no twin → the dollar figure alone
    (None, None, "$0"),
    ("", None, "$0"),
    (0, None, "$0"),
])
def test_money_both_falls_back_to_a_bare_dollar_figure(usd_value, som_value, expected):
    """0 is a figure, None is an absence — only None collapses the pair, so a total that
    really is worth nothing still shows both halves."""
    assert money_both(usd_value, som_value) == expected
    assert money_both(0, 0) == (
        '<span class="money-pair">$0<span class="money-alt">0 so&#x27;m</span></span>')


def test_rate_both_and_rate_range_both_print_the_per_kg_spread():
    """rate_range_both is the tannarx SPREAD for a marka whose lots did not all arrive at
    the same cost. The unit goes on the upper bound only, so it is not said twice, and the
    separator is an EN dash (U+2013) — not a hyphen, not the em dash the tags use for a
    missing value."""
    assert rate_both(Decimal("1.1700"), Decimal("14040")) == (
        '<span class="money-pair">1.17 $/kg'
        f'<span class="money-alt">14{NB}040 so&#x27;m/kg</span></span>')
    assert rate_both(Decimal("1.1700"), None) == "1.17 $/kg"
    assert rate_both(None, None) == "0 $/kg"

    assert rate_range_both(Decimal("0.94"), Decimal("1.17")) == f"0.94 {EN_DASH} 1.17 $/kg"
    assert rate_range_both(Decimal("0.9400"), Decimal("1.1700"),
                           Decimal("11280"), Decimal("14040")) == (
        f'<span class="money-pair">0.94 {EN_DASH} 1.17 $/kg'
        f'<span class="money-alt">11{NB}280 {EN_DASH} 14{NB}040 so&#x27;m/kg</span></span>')
    # One bound missing collapses the whole so'm spread — half a range is not a range.
    assert rate_range_both(Decimal("0.94"), Decimal("1.17"), Decimal("11280"), None) == \
        f"0.94 {EN_DASH} 1.17 $/kg"
    assert rate_range_both(None, None) == f"0 {EN_DASH} 0 $/kg"
    assert rate_range_both(Decimal("1"), Decimal("1"), Decimal("999999999"),
                           Decimal("999999999")) == (
        f'<span class="money-pair">1 {EN_DASH} 1 $/kg<span class="money-alt">'
        f'999{NB}999{NB}999 {EN_DASH} 999{NB}999{NB}999 so&#x27;m/kg</span></span>')


def test_pair_escapes_both_halves_and_marks_the_result_safe():
    """format_html escapes what it interpolates, so a value carrying markup cannot break
    out of the span — while the span itself survives autoescaping in a template."""
    out = _pair("<b>x</b>", "a & b")
    assert out == ('<span class="money-pair">&lt;b&gt;x&lt;/b&gt;'
                   '<span class="money-alt">a &amp; b</span></span>')
    rendered = Template("{{ v }}").render(Context({"v": out}))
    assert rendered == out          # already safe — not escaped a second time


def test_the_tags_survive_a_real_template_render_with_autoescape_on():
    """Rendered rather than called: `money` and friends are simple_tags, whose output is
    conditional_escape'd by the engine. That is why so'm reads as so&#x27;m in page HTML
    while _pair's own markup comes through intact."""
    out = Template(
        "{% load crm_extras %}"
        "{% money u s c %}|{% money_other u s c %}|{% rate p pu c %}|{% money_both u s %}"
    ).render(Context({"u": Decimal("1000"), "s": Decimal("12000000"), "c": "uzs",
                      "p": Decimal("1.17"), "pu": Decimal("14040")}))
    main, other, per_kg, both = out.split("|")
    assert main == f"12{NB}000{NB}000 so&#x27;m"
    assert other == f"$1{NB}000"
    assert per_kg == f"14{NB}040 so&#x27;m/kg"
    assert both == (f'<span class="money-pair">$1{NB}000'
                    f'<span class="money-alt">12{NB}000{NB}000 so&#x27;m</span></span>')


# ── static_v ────────────────────────────────────────────────────────────────────────

def test_static_v_busts_the_cache_only_for_files_that_exist():
    """?v=<mtime> so a browser refetches app.css after a deploy. A path the finders cannot
    resolve degrades to the plain URL rather than raising — a missing asset must not take
    the page down with it."""
    real = static_v("css/app.css")
    assert real.startswith("/static/css/app.css?v=")
    assert real.split("?v=")[1].isdigit()
    assert static_v("does/not/exist.css") == "/static/does/not/exist.css"
    assert static_v("css/app.css") == real          # stable between calls


# ── the sign on a negative figure ───────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason=(
    "BUG (cosmetic, low): the minus lands INSIDE the currency symbol. "
    "crm/templatetags/crm_extras.py:45-48 formats the signed Decimal first and then "
    "prefixes '$', so a negative comes out '$-1 200' instead of '-$1 200'; som() has the "
    "same shape at crm_extras.py:56 and prints '-0 so'm' for any value that rounds to a "
    "negative zero. Reachable wherever a balance can go the other way — "
    "templates/crm/customer_list.html:29 (avans), templates/crm/logist_list.html:73, "
    "templates/crm/dashboard.html:78 (Foyda)."))
def test_a_negative_sum_keeps_the_sign_outside_the_currency_symbol():
    assert usd(Decimal("-1200")) == f"-$1{NB}200"
    assert usd(Decimal("-0.001")) == "$0"
    assert som(Decimal("-0.5")) == "0 so'm"


def test_what_a_negative_sum_actually_prints_today():
    """The measured half of the claim above, kept passing so the regression is visible as
    a diff rather than as an xfail flipping colour."""
    assert usd(Decimal("-1200")) == f"$-1{NB}200"
    assert usd(Decimal("-0.001")) == "$-0"
    assert som(Decimal("-1234.5")) == f"-1{NB}234 so'm"
    assert som(Decimal("-0.5")) == "-0 so'm"
    assert rate(Decimal("-1.5"), None, "usd") == "-1.5 $/kg"


def test_the_mijozlar_list_prints_an_avans_without_a_minus(admin_client, db):
    """A mijoz who paid before buying is holding an avans, and the cell says so in
    words. The figure beside it is the size of that avans, so it carries no sign —
    "avans $-500" made the reader work out that two negatives were one fact."""
    customer = Customer.objects.create(name="Dilnoza", phone="998901234567")
    CustomerPayment.objects.create(
        customer=customer, date="2026-07-20", amount=Decimal("500"),
        amount_uzs=Decimal("6000000"), exchange_rate=Decimal("12000"),
        currency=Currency.USD, method=PayMethod.CASH)

    html = admin_client.get("/customers/").content.decode()
    assert "avans" in html
    assert "$500" in html
    assert "$-500" not in html and "-$500" not in html


# ── crm/formatting.py: phones ───────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    # UZ: +998 + 9 national digits, in the punctuation variants an operator really types
    ("+998 90 123 45 67", "UZ"),
    ("998901234567", "UZ"),
    ("998-90-123-45-67", "UZ"),
    ("  +998 (90) 123-45-67  ", "UZ"),
    # IR: +98 + 10
    ("+98 912 345 6789", "IR"),
    ("+98 812 345 6789", "IR"),      # national part starting 8 — still not confusable with UZ
    ("989123456789", "IR"),
    # TR: +90 + 10
    ("+90 532 123 45 67", "TR"),
    ("+90.532.123.45.67", "TR"),
    ("+90 (216) 555 44 33", "TR"),   # Istanbul landline
    # nearly-but-not-quite for each: one digit short, one digit long
    ("+998 90 123 45 6", None),
    ("+998 90 123 45 678", None),
    ("+98 912 345 678", None),
    ("+98 912 345 67890", None),
    ("+90 532 123 456", None),
    ("+90 532 123 45 678", None),
    # a country we do not call, and the 00-prefixed international form
    ("+7 912 345 67 89", None),
    ("00998901234567", None),
    # blank shapes
    ("", None),
    (None, None),
])
def test_phone_country_tells_uz_ir_and_tr_apart(value, expected):
    """All three come to 12 digits, so length cannot separate them — only the second digit
    (99 / 98 / 90) with fullmatch anchoring both ends (crm/formatting.py:14-18).

    Punctuation is stripped before matching, which is deliberate ("it reads the same value
    the operator sees") and means grouping is irrelevant: the 00-prefixed form is rejected
    on digit COUNT, not on its punctuation."""
    assert phone_country(value) == expected


def test_validate_intl_phone_accepts_blank_and_returns_the_stripped_value():
    """Blank is allowed — phone is optional on every model that uses this. A value that is
    only whitespace comes back as "", not as the spaces, so a blank-looking field is
    genuinely stored blank."""
    assert validate_intl_phone(None) == ""
    assert validate_intl_phone("") == ""
    assert validate_intl_phone("   ") == ""
    assert validate_intl_phone("  +998 90 123 45 67  ") == "+998 90 123 45 67"


@pytest.mark.parametrize("value", [
    "+998 90 123 45 6",     # UZ, one short
    "+98 912 345 678",      # IR, one short
    "+90 532 123 456",      # TR, one short
    "00998901234567",       # the 00 international prefix
    "+7 912 345 67 89",     # a country not on the list
    "salom",                # not a number at all
])
def test_validate_intl_phone_rejects_a_near_miss_with_the_format_hint(value):
    """The error names all three accepted formats, so an operator who typed 00998… is told
    what to type instead rather than just being refused."""
    with pytest.raises(forms.ValidationError) as caught:
        validate_intl_phone(value)
    message = " ".join(caught.value.messages)
    assert "+998" in message and "+98" in message and "+90" in message


# ── crm/formatting.py: containers ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("MSKU1234567", "MSKU 123456 7"),       # valid ISO 6346, compact
    ("msku1234567", "MSKU 123456 7"),       # lowercase
    ("  msku   123456   7  ", "MSKU 123456 7"),      # odd spacing, inside and out
    ("MSKU\t123456\n7", "MSKU 123456 7"),            # tab / newline from a paste
    ("MSKU 123456 8", "MSKU 123456 8"),     # wrong check digit — shape is what is checked
    ("ABC1234567", "ABC1234567"),           # non-conforming: 3 letters, left alone
    ("abcd 12345 6", "ABCD 12345 6"),       # non-conforming: 6 digits, spaces collapsed
    ("  mscu-1 ", "MSCU-1"),
    ("", ""),
    (None, ""),
])
def test_normalize_container_groups_only_a_conforming_value(raw, expected):
    """The point is that "msku1234567" and "MSKU 123456 7" store identically so they
    compare. A value that is not 4 letters + 7 digits is uppercased and space-collapsed but
    NOT reshaped — the operator may be recording something that is not a container number,
    and mangling it would lose what they typed.

    The check digit is not verified: the docstring commits to "4 letters + 7 digits", so
    'MSKU 123456 8' formatting cleanly is the documented behaviour, not a defect."""
    assert normalize_container(raw) == expected


def test_normalize_container_round_trips_its_own_output():
    """Idempotence is what makes it safe to run on every save: re-normalising a stored
    value must not walk it into a different string."""
    once = normalize_container("msku1234567")
    assert normalize_container(once) == once
    assert normalize_container(normalize_container("  mscu-1 ")) == "MSCU-1"
