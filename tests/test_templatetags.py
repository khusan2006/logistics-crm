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


def test_no_multiline_django_comments_in_templates():
    """`{# … #}` is single-line ONLY. Spanning it across lines is not a comment —
    Django renders it as literal text, and inside a <table> the browser hoists that
    text out to the top of the page. It throws no error and fails no assertion that
    only checks figures, so it ships looking fine and reads broken on screen."""
    import glob
    import re

    offenders = []
    for path in sorted(glob.glob("templates/**/*.html", recursive=True)):
        text = open(path).read()
        for match in re.finditer(r"\{#", text):
            rest = text[match.start():]
            end = rest.find("#}")
            if end == -1 or "\n" in rest[:end]:
                offenders.append(f"{path}:{text[:match.start()].count(chr(10)) + 1}")
    assert not offenders, (
        "multi-line or unclosed {# #} — use {% comment %}{% endcomment %}: "
        + ", ".join(offenders))
