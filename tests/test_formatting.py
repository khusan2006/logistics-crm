import pytest
from django import forms

from crm.formatting import normalize_container, phone_country, validate_intl_phone


@pytest.mark.parametrize("value", [
    "",
    "+998 90 123 45 67", "998901234567",            # UZ
    "+98 912 345 6789", "989123456789",             # IR
    "+90 532 123 45 67", "905321234567",            # TR
    "+90 216 555 44 33",                            # TR landline (Istanbul)
])
def test_validate_intl_phone_accepts(value):
    assert validate_intl_phone(value) == value


@pytest.mark.parametrize("value,country", [
    ("+998 90 123 45 67", "UZ"),
    ("+98 912 345 6789", "IR"),
    ("+90 532 123 45 67", "TR"),
    ("+1 202 555 0100", None),
])
def test_phone_country(value, country):
    """UZ, IR and TR all come to 12 digits, so the prefixes have to be told apart
    by the second digit (99 / 98 / 90) rather than by length."""
    assert phone_country(value) == country


@pytest.mark.parametrize("value", [
    "+82343905395034355", "12345", "+1 202 555 0100",
    "+90 532 123 45",      # TR too short
    "+90 532 123 45 678",  # TR too long
])
def test_validate_intl_phone_rejects(value):
    with pytest.raises(forms.ValidationError):
        validate_intl_phone(value)


@pytest.mark.parametrize("raw,expected", [
    ("msku1234567", "MSKU 123456 7"),
    ("MSKU 123456 7", "MSKU 123456 7"),
    ("MSKU1234567", "MSKU 123456 7"),
    ("  mscu-1 ", "MSCU-1"),
    ("", ""),
])
def test_normalize_container(raw, expected):
    assert normalize_container(raw) == expected
