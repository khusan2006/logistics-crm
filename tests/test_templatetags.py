from decimal import Decimal

from crm.templatetags.crm_extras import usd


def test_usd_formats_thousands_and_two_decimals():
    assert usd(Decimal("48000")) == "$48,000.00"


def test_usd_formats_small_value():
    assert usd(Decimal("1")) == "$1.00"


def test_usd_blank_safe_none():
    assert usd(None) == "$0.00"
