import os
from decimal import Decimal

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

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


@register.filter
def usd(value):
    """Format a number as USD: $1,234.56 (2 dp, thousands separator). Blank-safe."""
    try:
        return "${:,.2f}".format(Decimal(value or 0))
    except (TypeError, ValueError, ArithmeticError):
        return "$0.00"


#: Groups a so'm figure. Written as an escape rather than typed so it is
#: visible in the source: so'm runs to nine digits, and a normal space lets a
#: table wrap mid-number, which reads as two separate figures.
NBSP = "\u00a0"


@register.filter
def som(value):
    """Format a number as so'm: 1 234 560 so'm. No decimals — tiyin have not been
    real money for a long time, and they make a nine-digit figure unreadable."""
    try:
        return "{:,.0f} so'm".format(Decimal(value or 0)).replace(",", NBSP)
    except (TypeError, ValueError, ArithmeticError):
        return "0 so'm"


@register.simple_tag(takes_context=True)
def rate(context, usd_value, som_value=None):
    """A per-kg narx in the active currency: "1.17 $/kg" or "14 040 so'm/kg".

    Separate from `money` because a rate is read differently — the dollar side keeps
    four decimals (a $/kg rounded to cents moves a 24-tonne lot by dollars) and both
    sides carry the /kg suffix that makes the figure mean anything."""
    try:
        if context.get("display_currency") != "uzs":
            return f"{_trim(usd_value)} $/kg"
        if som_value is None:
            return "—"
        return "{:,.0f} so'm/kg".format(Decimal(som_value)).replace(",", NBSP)
    except (TypeError, ValueError, ArithmeticError):
        return "—"


def _trim(value):
    """0.8140 → 0.814, 1.0000 → 1 — a narx reads as what was agreed, not padded out
    to the column's four decimals."""
    text = f"{Decimal(value or 0):.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


@register.simple_tag(takes_context=True)
def money(context, usd_value, som_value=None):
    """Render a figure in whichever currency the session is showing.

    Takes BOTH stored values rather than converting one into the other: each row was
    booked at its own kurs, so the so'm figure is the one that was actually agreed
    that day, not today's rate applied after the fact. That is also why this is a
    tag and not a filter — a filter cannot see the session.

    A missing so'm twin renders as an em dash rather than a converted guess: it means
    the row predates dual currency and genuinely has no so'm value on record."""
    if context.get("display_currency") != "uzs":
        return usd(usd_value)
    if som_value is None:
        return "—"
    return som(som_value)
