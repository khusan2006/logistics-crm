import pytest
from django import forms

from crm.formatting import normalize_container, validate_intl_phone


@pytest.mark.parametrize("value", [
    "", "+998 90 123 45 67", "+98 912 345 6789", "998901234567", "989123456789",
])
def test_validate_intl_phone_accepts(value):
    assert validate_intl_phone(value) == value


@pytest.mark.parametrize("value", ["+82343905395034355", "12345", "+1 202 555 0100"])
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
