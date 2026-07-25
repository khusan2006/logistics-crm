"""What currency the screens are drawn in.

The choice is per-session, not per-user-record: it is a way of looking at the same
figures, not a property of the person, and an operator flips it several times a day
while reading one report to a hamkor and the next to a mijoz.
"""
from .models import Currency

SESSION_KEY = "display_currency"


def display_currency(request):
    """`display_currency` in every template — "usd" (the default) or "uzs".

    Nothing is converted here. Each row already stores both values at its own
    entry-time kurs, so switching currency changes which stored column is read,
    never what a past total is worth."""
    chosen = request.session.get(SESSION_KEY) if hasattr(request, "session") else None
    if chosen not in Currency.values:
        chosen = Currency.USD
    return {"display_currency": chosen, "showing_som": chosen == Currency.UZS}
