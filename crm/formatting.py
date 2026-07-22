"""Model-free formatting + validation helpers for the contact-style inputs
(phone, container). Importable by any app without pulling in crm.models."""
import re

from django import forms

# Uzbek (+998 + 9 national digits) or Iranian (+98 + 10 national digits).
_PHONE_UZ = re.compile(r"998\d{9}")
_PHONE_IR = re.compile(r"98\d{10}")

# ISO 6346: 4 owner/category letters + 6 serial digits + 1 check digit.
_CONTAINER_ISO = re.compile(r"^([A-Z]{4})(\d{6})(\d)$")


def validate_intl_phone(value):
    """Blank, or a valid Uzbek/Iranian number. Formatting (spaces, +, -) is ignored."""
    v = (value or "").strip()
    if not v:
        return v
    digits = re.sub(r"\D", "", v)
    if _PHONE_UZ.fullmatch(digits) or _PHONE_IR.fullmatch(digits):
        return v
    raise forms.ValidationError(
        "Telefon O'zbekiston (+998 XX XXX XX XX) yoki Eron (+98 XXX XXX XXXX) "
        "formatida bo'lishi kerak")


def phone_intl_widget():
    """A fresh phone TextInput (so forms don't share a mutable attrs dict). The
    base.html data-phone-intl enhancer turns this into an inline country picker."""
    return forms.TextInput(attrs={
        "data-phone-intl": "", "inputmode": "tel", "autocomplete": "tel",
        "placeholder": "+998 90 123 45 67  yoki  +98 912 345 6789",
    })


def normalize_container(value):
    """Uppercase + strip; when the compacted value is ISO 6346 (4 letters + 7
    digits) render it grouped as 'ABCD 123456 7'. Otherwise return the uppercased,
    space-collapsed string unchanged. Lets 'msku1234567' and 'MSKU 123456 7'
    compare and store identically."""
    v = (value or "").strip().upper()
    if not v:
        return v
    compact = re.sub(r"\s+", "", v)
    m = _CONTAINER_ISO.match(compact)
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)}"
    return re.sub(r"\s+", " ", v)
