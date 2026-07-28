from decimal import Decimal

from crm.templatetags.crm_extras import NBSP, usd


def test_usd_groups_thousands_with_a_space():
    # A space, not a comma — a comma is the decimal mark here, so "$48,000"
    # would read as forty-eight dollars.
    assert usd(Decimal("48000")) == f"$48{NBSP}000"


def test_usd_drops_trailing_zeros():
    assert usd(Decimal("1")) == "$1"
    assert usd(Decimal("1200")) == f"$1{NBSP}200"
    assert usd(Decimal("0.80")) == "$0.8"


def test_usd_keeps_real_decimals():
    assert usd(Decimal("1234.56")) == f"$1{NBSP}234.56"
    assert usd(Decimal("1234.50")) == f"$1{NBSP}234.5"


def test_usd_blank_safe_none():
    assert usd(None) == "$0"
