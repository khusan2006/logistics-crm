"""Model-free formatting + validation helpers for the contact-style inputs
(phone, container). Importable by any app without pulling in crm.models."""
import re

from django import forms

# The countries the business actually calls. Each pattern is the full international
# digit string with no punctuation, so "+998 90 123 45 67" and "998901234567" are
# the same number here.
#
# The three prefixes cannot be confused with one another even though UZ, IR and TR
# all come to 12 digits: they differ by the second digit (99 / 98 / 90), and
# fullmatch anchors both ends.
_PHONE_PATTERNS = (
    ("UZ", re.compile(r"998\d{9}")),    # +998 + 9 national digits
    ("IR", re.compile(r"98\d{10}")),    # +98  + 10
    ("TR", re.compile(r"90\d{10}")),    # +90  + 10
)

# ISO 6346: 4 owner/category letters + 6 serial digits + 1 check digit.
_CONTAINER_ISO = re.compile(r"^([A-Z]{4})(\d{6})(\d)$")


def phone_country(value):
    """"UZ" / "IR" / "TR" for a recognised number, else None. Punctuation is
    ignored, so it reads the same value the operator sees."""
    digits = re.sub(r"\D", "", value or "")
    for code, pattern in _PHONE_PATTERNS:
        if pattern.fullmatch(digits):
            return code
    return None


def validate_intl_phone(value):
    """Blank, or an Uzbek / Iranian / Turkish number. Formatting (spaces, +, -) is
    ignored — only the digits are checked."""
    v = (value or "").strip()
    if not v:
        return v
    if phone_country(v):
        return v
    raise forms.ValidationError(
        "Telefon O'zbekiston (+998 XX XXX XX XX), Eron (+98 XXX XXX XXXX) yoki "
        "Turkiya (+90 XXX XXX XX XX) formatida bo'lishi kerak")


def phone_intl_widget():
    """A fresh phone TextInput (so forms don't share a mutable attrs dict). The
    base.html data-phone-intl enhancer turns this into an inline country picker."""
    return forms.TextInput(attrs={
        "data-phone-intl": "", "inputmode": "tel", "autocomplete": "tel",
        "placeholder": "+998 90 123 45 67  ·  +98 912 345 6789  ·  +90 532 123 45 67",
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
