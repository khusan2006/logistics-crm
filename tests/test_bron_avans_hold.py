"""An avans with an open bron against it is not spare money.

Reported from the floor: Komoliddin sintafon had $40 866.81 sitting as avans and a
46 180 kg bron of 2102 репак at $1.42 — $65 575.60 of granula already promised to
him — and nothing on any screen connected the two. The avans read as free money.

What a bron does here is SPEAK FOR the avans, not spend it: nothing has been handed
over, so nothing is owed and the mijoz's balans does not move. The kg become a qarz
when they go out, which is the sotuv path and is pinned at the bottom of this file.
"""
from decimal import Decimal

from crm.models import (
    Currency, Customer, CustomerPayment, Reservation, Sale,
    apply_customer_advance, bron_advance_holds, customer_balance_by_currency,
)
from tests.test_reservations import _arrived_lot, _customer


def _payment(customer, amount, currency=Currency.USD, date="2026-07-20"):
    usd = Decimal(amount) if currency == Currency.USD else Decimal(amount) / 12000
    uzs = Decimal(amount) * 12000 if currency == Currency.USD else Decimal(amount)
    return CustomerPayment.objects.create(
        customer=customer, date=date, amount=usd.quantize(Decimal("0.01")),
        amount_uzs=uzs.quantize(Decimal("0.01")), currency=currency, method="cash")


def _bron(customer, brand="LLDPE", kg="10000", price="1.42",
          currency=Currency.USD, fulfilled="0"):
    usd = Decimal(price) if currency == Currency.USD else Decimal(price) / 12000
    uzs = Decimal(price) * 12000 if currency == Currency.USD else Decimal(price)
    return Reservation.objects.create(
        customer=customer, brand=brand, kg=Decimal(kg),
        fulfilled_kg=Decimal(fulfilled), price=usd, price_uzs=uzs,
        currency=currency, status=Reservation.Status.ACTIVE)


def _hold(bron):
    return bron_advance_holds([bron])[bron.pk]


def test_an_open_bron_speaks_for_the_avans(db):
    """The reported case, to the tiyin: avans under the bron's value, so the whole
    avans is held and the rest is what the mijoz would still owe."""
    customer = _customer("Komoliddin sintafon")
    _payment(customer, "40866.81")
    bron = _bron(customer, "2102 repak", kg="46180", price="1.42")

    hold = _hold(bron)
    assert hold["held"] == Decimal("40866.81")
    assert hold["short"] == Decimal("24708.79")     # 46 180 × 1.42 − 40 866.81


def test_the_bron_does_not_make_the_mijoz_a_qarzdor(db):
    """The whole point of holding rather than spending. Nothing has gone out, so the
    balans must still read as an avans of the full amount — not a qarz of the
    shortfall, and not a smaller avans."""
    customer = _customer()
    _payment(customer, "40866.81")
    _bron(customer, kg="46180", price="1.42")

    assert customer_balance_by_currency(customer) == [(Currency.USD, Decimal("-40866.81"))]


def test_an_avans_bigger_than_the_bron_is_only_partly_held(db):
    customer = _customer()
    _payment(customer, "10000")
    bron = _bron(customer, kg="1000", price="1.50")     # $1 500

    hold = _hold(bron)
    assert hold["held"] == Decimal("1500.00")
    assert hold["short"] == Decimal("0.00")


def test_a_som_bron_is_not_covered_by_a_dollar_avans(db):
    """Never across currencies. A dollar avans against a so'm bron would need a kurs
    nobody agreed, and the money went in as dollars."""
    customer = _customer()
    _payment(customer, "50000", currency=Currency.USD)
    bron = _bron(customer, kg="1000", price="16900", currency=Currency.UZS)

    hold = _hold(bron)
    assert hold["currency"] == Currency.UZS
    assert hold["held"] == Decimal("0")
    assert hold["short"] == Decimal("16900000.00")


def test_the_oldest_bron_is_covered_first(db):
    """Queue order, the same one Bronlar shows: whoever asked first is the one their
    own avans reaches."""
    customer = _customer()
    _payment(customer, "1500")
    first = _bron(customer, "marka-a", kg="1000", price="1.00")     # $1 000
    second = _bron(customer, "marka-b", kg="1000", price="1.00")    # $1 000

    holds = bron_advance_holds([first, second])
    assert holds[first.pk]["held"] == Decimal("1000.00")
    assert holds[first.pk]["short"] == Decimal("0.00")
    assert holds[second.pk]["held"] == Decimal("500.00")
    assert holds[second.pk]["short"] == Decimal("500.00")


def test_a_bron_with_no_agreed_narx_holds_nothing(db):
    """A real state — a bron may be struck before the price is. What it is worth is
    not known, and a figure invented here would read as an agreed one."""
    customer = _customer()
    _payment(customer, "10000")
    bron = Reservation.objects.create(
        customer=customer, brand="LLDPE", kg=Decimal("1000"),
        status=Reservation.Status.ACTIVE)

    hold = _hold(bron)
    assert hold["held"] is None and hold["short"] is None


def test_only_the_kg_still_owed_are_held_against(db):
    """A partly served bron holds against what is LEFT: the kg already handed over
    became a sotuv, and their share of the avans went with it."""
    customer = _customer()
    _payment(customer, "10000")
    bron = _bron(customer, kg="10000", price="1.00", fulfilled="6000")

    hold = _hold(bron)
    assert hold["held"] == Decimal("4000.00")      # 4 000 kg still owed × $1.00
    assert hold["short"] == Decimal("0.00")


def test_the_hold_does_not_depend_on_which_rows_were_asked_about(db):
    """Computed over the mijoz's whole open queue. Filtering Bronlar down to the
    second row must not make it look like the first one's money is free."""
    customer = _customer()
    _payment(customer, "1500")
    first = _bron(customer, "marka-a", kg="1000", price="1.00")
    second = _bron(customer, "marka-b", kg="1000", price="1.00")

    assert bron_advance_holds([second])[second.pk] == \
           bron_advance_holds([first, second])[second.pk]


def test_handing_the_granula_over_is_what_creates_the_qarz(db):
    """The other half, which already worked and is pinned here so the two are read
    together: on the sotuv the avans is really spent and the shortfall is really a
    qarz. Until then the bron above changes neither figure."""
    customer = _customer()
    _payment(customer, "1000")
    lot = _arrived_lot(kg="10000")
    sale = Sale.objects.create(customer=customer, line=lot, kg=Decimal("1000"),
                               price=Decimal("1.50"), date="2026-07-25")   # $1 500
    apply_customer_advance(sale)

    assert customer_balance_by_currency(customer) == [(Currency.USD, Decimal("500.00"))]


def test_the_hold_does_not_query_per_tolov(db, django_assert_max_num_queries):
    """A regular mijoz has dozens of to'lovlar — Komoliddin has 28. Each one's
    unspent part reads its sotuv slices AND its cash refunds, and leaving either
    unprefetched put a query per to'lov on every page that shows a bron."""
    customer = _customer()
    for i in range(30):
        _payment(customer, "100", date=f"2026-07-{(i % 28) + 1:02d}")
    bron = _bron(customer, kg="1000", price="1.00")

    with django_assert_max_num_queries(5):
        bron_advance_holds([bron])
