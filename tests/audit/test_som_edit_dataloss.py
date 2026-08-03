"""The headline defect, verified independently of the testers that first found it.

Five separate auditors converged on the same root cause, so this file re-proves it
from scratch with the plainest possible evidence: take a so'm row, open its edit
screen, press Save on an otherwise untouched form, and look at the money.

ROOT CAUSE
    MoneyEntryFormMixin (crm/forms.py:97) puts the operator's figure in
    `amount`/`price` — the USD column — and the derived so'm figure in
    `amount_uzs`/`price_uzs`. The so'm column is NOT a form field (see the
    _post_clean docstring at crm/forms.py:167), so when a ModelForm binds an
    existing row it renders the USD column into the one visible money box.

    For a row typed in so'm that box is labelled so'm and re-read as so'm on
    submit. The figure the operator sees is the DOLLAR value wearing a so'm label,
    and saving it writes that dollar number back as the so'm amount.

    crm/forms.py:912-917 (ReturnForm) is the only place in the file that seeds the
    so'm side — and only for a NEW return, never for an edit.

Run:
    TEST_DB_SUFFIX=_dl .venv/bin/python -m pytest tests/audit/test_som_edit_dataloss.py -q
"""
from decimal import Decimal

import pytest

from crm.models import Currency, Customer, CustomerPayment, PayMethod


@pytest.fixture
def som_payment(db):
    """A mijoz to'lov the operator typed as 12 000 000 so'm at a kurs of 12 000.

    Stored as the pair the app keeps for every money row: 1 000 in the USD column,
    12 000 000 in the so'm column, currency='uzs'.
    """
    customer = Customer.objects.create(name="Dilnoza", phone="998901234567")
    return CustomerPayment.objects.create(
        customer=customer, amount=Decimal("1000"), amount_uzs=Decimal("12000000"),
        currency=Currency.UZS, exchange_rate=Decimal("12000"),
        method=PayMethod.CASH, date="2026-07-20")


def test_the_edit_screen_shows_the_dollar_figure_in_the_som_box(som_payment):
    """Before anything is saved: what does the operator actually see?

    The row is 12 000 000 so'm. The box they are looking at says 1000.
    """
    from crm.forms import CustomerPaymentForm

    form = CustomerPaymentForm(instance=som_payment)
    shown = form.initial.get("amount")

    assert str(shown) == "12000000", (
        f"the Summa box on a so'm to'lov shows {shown}, but the mijoz paid "
        f"12 000 000 so'm. The operator is being shown the dollar column under a "
        f"so'm label — and it is that figure that gets submitted back.")


def test_pressing_save_on_an_untouched_som_payment_destroys_the_amount(som_payment,
                                                                      admin_client):
    """The whole bug in one action: open, save, nothing typed — money gone.

    12 000 000 so'm becomes 1 000 so'm. The mijoz's qarz jumps by 11 999 000 so'm
    and nobody touched a figure.
    """
    from crm.forms import CustomerPaymentForm

    before_usd, before_uzs = som_payment.amount, som_payment.amount_uzs

    # Exactly what the browser posts when the operator opens the modal and clicks
    # Saqlash: every rendered field at its rendered value, nothing edited.
    form = CustomerPaymentForm(instance=som_payment)
    posted = {name: ("" if value is None else str(value))
              for name, value in form.initial.items()}
    posted.setdefault("customer", som_payment.customer_id)
    posted.setdefault("date", "2026-07-20")
    posted.setdefault("method", PayMethod.CASH)
    posted.setdefault("currency", Currency.UZS)
    posted.setdefault("exchange_rate", "12000")
    posted.setdefault("fee_percent", "0")

    resp = admin_client.post(f"/customer-payments/{som_payment.pk}/edit/", posted)
    assert resp.status_code in (200, 302), resp.status_code

    som_payment.refresh_from_db()
    assert (som_payment.amount, som_payment.amount_uzs) == (before_usd, before_uzs), (
        f"a no-op Save moved the money: {before_uzs} so'm (${before_usd}) became "
        f"{som_payment.amount_uzs} so'm (${som_payment.amount})")


def test_a_dollar_payment_survives_the_same_no_op_save(db, admin_client):
    """The control. Same action on a DOLLAR row does no harm.

    This is what pins the defect on the so'm path rather than on editing in general
    — and it is why the operators only see it sometimes.
    """
    from crm.forms import CustomerPaymentForm

    customer = Customer.objects.create(name="Bobur", phone="998901234568")
    payment = CustomerPayment.objects.create(
        customer=customer, amount=Decimal("1000"), amount_uzs=Decimal("12000000"),
        currency=Currency.USD, exchange_rate=Decimal("12000"),
        method=PayMethod.CASH, date="2026-07-20")
    before = (payment.amount, payment.amount_uzs)

    form = CustomerPaymentForm(instance=payment)
    posted = {name: ("" if value is None else str(value))
              for name, value in form.initial.items()}
    posted.setdefault("customer", customer.pk)
    posted.setdefault("date", "2026-07-20")
    posted.setdefault("method", PayMethod.CASH)
    posted.setdefault("currency", Currency.USD)
    posted.setdefault("exchange_rate", "12000")
    posted.setdefault("fee_percent", "0")

    admin_client.post(f"/customer-payments/{payment.pk}/edit/", posted)
    payment.refresh_from_db()

    assert (payment.amount, payment.amount_uzs) == before, (
        "a dollar row must survive a no-op save; if this fails the defect is wider "
        "than the so'm path")
