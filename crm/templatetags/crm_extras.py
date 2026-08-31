import os
from decimal import Decimal

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def static_v(path):
    """Static URL with a ?v=<mtime> cache-buster so browsers refetch on change."""
    url = static(path)
    abs_path = finders.find(path)
    if abs_path:
        try:
            return f"{url}?v={int(os.path.getmtime(abs_path))}"
        except OSError:
            pass
    return url


#: Groups a so'm figure. Written as an escape rather than typed so it is
#: visible in the source: so'm runs to nine digits, and a normal space lets a
#: table wrap mid-number, which reads as two separate figures.
NBSP = "\u00a0"


@register.filter
def usd(value):
    """Format a number as USD: $1 200, $1 234.56, $0.8 \u2014 space-grouped thousands
    and no trailing zeros. Blank-safe.

    Two deliberate choices, both to match how the operator reads a figure:
    thousands are grouped with a space, not a comma, because a comma is the
    DECIMAL mark here \u2014 "$1,200" reads as a dollar and change, not twelve hundred.
    And a padded .00 is dropped: the value carries decimals only if someone typed
    them, so $1 200 means exactly that and $0.8 is not pretending to be $0.80."""
    try:
        amount = Decimal(value or 0)
    except (TypeError, ValueError, ArithmeticError):
        return "$0"
    text = "{:,.2f}".format(amount)          # 2 dp is the column's precision
    if "." in text:
        text = text.rstrip("0").rstrip(".")  # 1,200.00 \u2192 1,200 \u00b7 0.80 \u2192 0.8
    return "$" + text.replace(",", NBSP)


@register.filter
def som(value):
    """Format a number as so'm: 1 234 560 so'm. No decimals — tiyin have not been
    real money for a long time, and they make a nine-digit figure unreadable."""
    try:
        return "{:,.0f} so'm".format(Decimal(value or 0)).replace(",", NBSP)
    except (TypeError, ValueError, ArithmeticError):
        return "0 so'm"


def _trim(value):
    """0.8140 → 0.814, 1.0000 → 1 — a narx reads as what was agreed, not padded out
    to the column's four decimals."""
    text = f"{Decimal(value or 0):.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _som_rate(som_value):
    return "{:,.0f} so'm/kg".format(Decimal(som_value)).replace(",", NBSP)


#: What a row's `currency` column reads as on the so'm side. Compared as a plain
#: string rather than against models.Currency so this module keeps its "formatting
#: only" dependency profile — the same reason crm.formatting imports no models.
SOM = "uzs"


def _is_som(currency):
    return str(currency or "").lower() == SOM


# ── One row's own figure ─────────────────────────────────────────────────────────
#
# A row is drawn in the currency it was BOOKED in, not in a currency the reader
# picked: a sotuv agreed in so'm reads in so'm from the sotuv list through to the
# to'lov modal, so the operator never converts in their head to check a figure they
# typed themselves. Both stored columns are still passed in — the row keeps both —
# but `currency` decides which is printed, and nothing is converted here.


@register.simple_tag
def money(usd_value, som_value=None, currency=None):
    """A row's sum in the currency that row was booked in.

    A so'm row with no so'm twin renders as an em dash rather than a converted
    guess: it predates dual currency and genuinely has no so'm value on record."""
    if not _is_som(currency):
        return usd(usd_value)
    if som_value is None:
        return "—"
    return som(som_value)


@register.simple_tag
def money_other(usd_value, som_value=None, currency=None):
    """The same sum in the currency the row was NOT booked in.

    The mirror of `money`, for the kassa's reference column: once the Kirim/Chiqim
    figure is drawn in the currency the operator typed, repeating that side beside
    it says nothing, while the twin at the row's own kurs is what a hamkor asking
    "how much is that in dollars" wants."""
    if _is_som(currency):
        return usd(usd_value)
    if som_value is None:
        return "—"
    return som(som_value)


@register.filter
def neg(value):
    """Flip the sign. A negative balance is an avans, and "avans $500" is what the
    operator reads — the minus is carried by the word, not repeated in the figure."""
    try:
        return -Decimal(value or 0)
    except (TypeError, ValueError, ArithmeticError):
        return value


@register.simple_tag
def money_in(value, currency):
    """A figure that exists in ONE currency and has no twin.

    Unlike `money`, nothing is stored on the other side to fall back to: this is a
    per-currency qarz total, and the dollar pile and the so'm pile are two different
    debts rather than two views of one. Adding them would need a kurs neither side
    agreed on, so they are printed apart."""
    return som(value) if _is_som(currency) else usd(value)


@register.simple_tag
def money_progress_in(part, total, currency):
    """"So far, of the whole" in one currency — the caption beside a progress bar.

    The so'm unit is written once, at the end: "312 500 000 / 625 000 000 so'm".
    Spelled on both sides it runs to two nine-digit figures plus the word twice,
    which squeezed the bar it captions down to a stub. The dollar sign stays on both
    — it is one character and reads as part of the figure rather than beside it."""
    if not _is_som(currency):
        return f"{usd(part)} / {usd(total)}"
    return "{:,.0f} / {}".format(Decimal(part or 0), som(total)).replace(",", NBSP)


@register.simple_tag
def rate(usd_value, som_value=None, currency=None):
    """A row's per-kg narx in the currency that row was booked in.

    Separate from `money` because a rate is read differently — the dollar side keeps
    four decimals (a $/kg rounded to cents moves a 24-tonne lot by dollars) and both
    sides carry the /kg suffix that makes the figure mean anything."""
    try:
        if not _is_som(currency):
            return f"{_trim(usd_value)} $/kg"
        if som_value is None:
            return "—"
        return _som_rate(som_value)
    except (TypeError, ValueError, ArithmeticError):
        return "—"


# ── Totals spanning both currencies ──────────────────────────────────────────────
#
# A total cannot pick a side the way a row can. A mijoz's qarz is three sotuvlar in
# so'm minus a to'lov that arrived in dollars; bucketing those by the currency each
# was typed in leaves two figures that never cancel, and the mijoz reads as still
# owing after they have settled. So both are printed in full — each row having been
# converted at ITS OWN entry-day kurs on the way in, never re-rated at today's.


def _pair(main, alt):
    """The two figures as one inline-block unit.

    Wrapped rather than emitted loose because these land mid-sentence as often as
    they land in a table cell ("· kassadan <strong>…</strong>"), and a bare block
    twin would break the line it sits in. As one unit it stacks under its own
    dollar figure and the sentence carries on beside it."""
    return format_html('<span class="money-pair">{}<span class="money-alt">{}</span></span>',
                       main, alt)


@register.simple_tag
def money_both(usd_value, som_value=None, currency=None):
    """A total in both currencies: the dollar figure with its so'm twin beneath.

    Pass `currency` when the figure DOES have a currency of its own — the goods on a
    yuk are priced in the one currency their kelishuv was struck in — and the two
    sides swap so the agreed one leads. That is the rule every other screen follows
    (`own_side`): a so'm kelishuv reads in so'm from the kelishuvlar list through to
    the to'lov, and a yuk off it leading with a dollar figure nobody agreed to made
    that one row the exception.

    Left out, the dollar leads as before. That is right for a genuinely blended
    total — a truck's xarajatlar are a so'm transport bill beside a dollar bojxona,
    so neither side is "the one that was agreed" and the app's canonical currency
    is the honest headline."""
    if som_value is None:
        return usd(usd_value)
    if _is_som(currency):
        return _pair(som(som_value), usd(usd_value))
    return _pair(usd(usd_value), som(som_value))


@register.simple_tag
def rate_both(usd_value, som_value=None):
    """The per-kg twin of `money_both` — a tannarx blended from lots of both
    currencies, so neither side can be called the one that was agreed."""
    try:
        dollars = f"{_trim(usd_value)} $/kg"
        if som_value is None:
            return dollars
        return _pair(dollars, _som_rate(som_value))
    except (TypeError, ValueError, ArithmeticError):
        return "—"


@register.simple_tag
def rate_range_both(usd_min, usd_max, som_min=None, som_max=None):
    """A tannarx SPREAD — "0.94 – 1.17 $/kg" — for a marka whose lots did not all
    arrive at the same cost, with the so'm spread beneath it.

    A range rather than four separate figures: pairing each currency's own min with
    its own max is what makes the line readable, and putting the unit on the upper
    bound only ("0.94 – 1.17 $/kg") stops it being said twice."""
    try:
        dollars = f"{_trim(usd_min)} – {_trim(usd_max)} $/kg"
        if som_min is None or som_max is None:
            return dollars
        spread = "{:,.0f} – {}".format(Decimal(som_min), _som_rate(som_max))
        return _pair(dollars, spread.replace(",", NBSP))
    except (TypeError, ValueError, ArithmeticError):
        return "—"


@register.simple_tag
def page_range(page, on_each_side=2, on_ends=1):
    """The page numbers to draw around the current one, with `None` marking a gap.

    Django's `get_elided_page_range` already does the windowing — 1 … 6 7 [8] 9 10
    … 42 — but it signals a gap with the lazily-translated `Paginator.ELLIPSIS`
    string, which a template can't compare against cheaply. Swapping it for `None`
    lets the partial branch on truthiness: a page number is never falsy."""
    paginator = page.paginator
    numbers = paginator.get_elided_page_range(
        page.number, on_each_side=on_each_side, on_ends=on_ends
    )
    return [None if n == paginator.ELLIPSIS else n for n in numbers]


@register.simple_tag(takes_context=True)
def page_url(context, number, param="page"):
    """Current query string with `param` swapped to `number`.

    `{% querystring %}` would do this, except its keys have to be literals — and
    kassa paginates two tables on one page, under `ipage` and `opage`. Taking the
    parameter name as an argument is what lets one partial serve both."""
    query = context["request"].GET.copy()
    query[param] = number
    return f"?{query.urlencode()}"
