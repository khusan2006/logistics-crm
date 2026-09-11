"""`bron_tekshir` — the report that answers "why does this bron say that many kg?".

Written for the 2026-09-11 call: 115 t of i 1561 handed to Mak Plast, 111 t
berilgan on Bronlar. Every way the two figures can part company gets a line of its
own here, so the next such call is one command rather than an afternoon in the data.
"""
from decimal import Decimal
from io import StringIO

import pytest
from conftest import make_lot
from django.core.management import call_command
from django.core.management.base import CommandError

from crm.models import Customer, Reservation, Sale, draw_down_bron


def _mijoz(name="Mak Plast"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _report(**kwargs):
    out = StringIO()
    call_command("bron_tekshir", stdout=out, **kwargs)
    return out.getvalue()


def _sale(mijoz, lot, kg, date="2026-09-08"):
    return Sale.objects.create(customer=mijoz, line=lot, kg=Decimal(kg),
                               price=Decimal("1.5"), date=date)


def test_it_refuses_when_nothing_matches(db):
    with pytest.raises(CommandError):
        _report(mijoz="yo'q bunday mijoz")


def test_it_lists_what_the_berilgan_figure_is_made_of(db):
    lot = make_lot(kg="200000", brand="i 1561", arrived="2026-09-02")
    mijoz = _mijoz()
    bron = Reservation.objects.create(customer=mijoz, brand="i 1561",
                                      kg=Decimal("115000"), price=Decimal("1.5"))
    draw_down_bron(_sale(mijoz, lot, "111000"))

    out = _report(mijoz="mak", marka="1561")
    assert f"BRON #{bron.pk}" in out
    assert "BERILGAN       111 000 kg" in out
    assert "111 000 kg" in out
    assert "berilgan figurasi hisoblangan sotuvlarga to'liq mos" in out


def test_it_names_the_sotuv_no_bron_counted(db):
    """The commonest cause: the sotuv was typed in before the bron existed, so
    `draw_down_bron` would not hand it to a promise made afterwards."""
    lot = make_lot(kg="200000", brand="i 1561", arrived="2026-09-02")
    mijoz = _mijoz()
    early = _sale(mijoz, lot, "4000")
    Reservation.objects.create(customer=mijoz, brand="i 1561",
                               kg=Decimal("115000"), price=Decimal("1.5"))
    draw_down_bron(early)                      # refused: the bron is younger

    out = _report(mijoz="mak")
    assert f"sotuv #{early.pk}" in out
    assert "sotuv brondan OLDIN kiritilgan" in out
    assert "4 000 kg sotuv bu bronga hisoblanmagan" in out


def test_it_shows_a_sotuv_capped_by_the_bron(db):
    lot = make_lot(kg="200000", brand="i 1561", arrived="2026-09-02")
    mijoz = _mijoz()
    Reservation.objects.create(customer=mijoz, brand="i 1561",
                               kg=Decimal("5000"), price=Decimal("1.5"))
    draw_down_bron(_sale(mijoz, lot, "8000"))

    out = _report(mijoz="mak")
    assert "bronda shuncha joy qolgandi" in out


def test_it_catches_the_lookalike_marka(db):
    """`i 1561` and `и 1561` read the same and are two products to the database —
    the split that left a 150 000 kg bron untouched on 2026-09-09."""
    latin = make_lot(kg="120000", brand="i 1561", arrived="2026-09-02")
    kiril = make_lot(kg="120000", brand="и 1561", arrived="2026-09-02")
    mijoz = _mijoz()
    Reservation.objects.create(customer=mijoz, brand="i 1561",
                               kg=Decimal("115000"), price=Decimal("1.5"))
    draw_down_bron(_sale(mijoz, latin, "111000"))
    _sale(mijoz, kiril, "4000")                # the same granula, the other spelling

    out = _report(mijoz="mak")
    assert "SHU MARKANING BOSHQA YOZILISHI BOR" in out
    assert "“и 1561”" in out
    assert "U+0438" in out                     # the cyrillic и, spelled out
    assert "merge_brand" in out


def test_it_reports_a_berilgan_figure_its_own_sotuvlar_cannot_explain(db):
    """The figure itself being wrong — what the old release guess used to cause."""
    lot = make_lot(kg="200000", brand="i 1561", arrived="2026-09-02")
    mijoz = _mijoz()
    bron = Reservation.objects.create(customer=mijoz, brand="i 1561",
                                      kg=Decimal("115000"), price=Decimal("1.5"))
    sale = _sale(mijoz, lot, "115000")
    draw_down_bron(sale)
    Reservation.objects.filter(pk=bron.pk).update(fulfilled_kg=Decimal("111000"))

    out = _report(mijoz="mak")
    assert "4 000 kg farq" in out
    assert "Bu raqamning o'zi noto'g'ri" in out


def test_it_reports_kg_that_came_back(db):
    from crm.models import Return

    lot = make_lot(kg="200000", brand="i 1561", arrived="2026-09-02")
    mijoz = _mijoz()
    Reservation.objects.create(customer=mijoz, brand="i 1561",
                               kg=Decimal("115000"), price=Decimal("1.5"))
    sale = _sale(mijoz, lot, "115000")
    draw_down_bron(sale)
    Return.objects.create(sale=sale, kg=Decimal("4000"), price=Decimal("1.5"),
                          date="2026-09-10")

    out = _report(mijoz="mak")
    assert "VAZVRAT" in out
    assert "bronga QAYTARILMAYDI" in out


def test_it_says_when_the_sotuv_was_bigger_than_the_bron(db):
    """115 t handed over against a 111 t promise: nothing is lost and nothing is
    wrong, but "sotildi" and "berilgan" differ by design and the report must say so
    rather than leave the operator counting."""
    lot = make_lot(kg="200000", brand="i 1561", arrived="2026-09-02")
    mijoz = _mijoz()
    Reservation.objects.create(customer=mijoz, brand="i 1561",
                               kg=Decimal("111000"), price=Decimal("1.5"))
    draw_down_bron(_sale(mijoz, lot, "115000"))

    out = _report(mijoz="mak")
    assert "BERILGAN       111 000 kg" in out
    assert "4 000 kg bronga sig'magan" in out
    assert "bron unga va'da bermagan" in out


def test_it_runs_against_a_database_without_the_draw_ledger(db, monkeypatch):
    """The database the question is being asked about has not had migration 0072 —
    the report reconstructs the split from the links and says that it did."""
    from django.db import connection

    lot = make_lot(kg="200000", brand="i 1561", arrived="2026-09-02")
    mijoz = _mijoz()
    Reservation.objects.create(customer=mijoz, brand="i 1561",
                               kg=Decimal("115000"), price=Decimal("1.5"))
    sale = _sale(mijoz, lot, "111000")
    draw_down_bron(sale)

    real = connection.introspection.table_names
    monkeypatch.setattr(connection.introspection, "table_names",
                        lambda *a, **k: [t for t in real() if t != "crm_brondraw"])

    out = _report(mijoz="mak")
    assert "BronDraw jadvali yo'q" in out
    assert f"sotuv #{sale.pk}" in out
    assert "111 000 kg hisoblangan" in out
