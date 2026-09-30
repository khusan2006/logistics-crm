"""A sotuv that spills across two brons records its kg against each of them.

24 100 kg of 2102 репак closed bron #6 with 2 700 and put 21 400 into bron #9, while
the sotuv named only #6: bron #9's berilgan kg came from nowhere anybody could see,
and deleting the sotuv gave all 24 100 back to #6.
"""
import importlib
from decimal import Decimal

import pytest

from conftest import make_bron
from crm.models import Reservation, ReservationDraw, ReservationItem, Sale
from tests.test_bron_markalar import _sell
from tests.test_reservations import _arrived_lot, _customer

backfill_draws = importlib.import_module(
    "crm.migrations.0081_reservation_draws").backfill_draws


def _two_brons(customer):
    older = make_bron(customer, brand="LLDPE", kg="3000")
    newer = make_bron(customer, brand=["HDPE", "LLDPE"], kg="75000")
    return older, newer


class TestSpill:
    def test_a_sotuv_records_what_it_gave_each_bron(self, admin_client, db):
        _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        older, newer = _two_brons(customer)
        _sell(admin_client, customer, "LLDPE", "24100")
        sale = Sale.objects.get()
        assert sale.reservation == older
        assert [(d.reservation_id, d.kg) for d in sale.bron_draws.all()] == [
            (older.pk, Decimal("3000.000")), (newer.pk, Decimal("21100.000"))]
        newer.refresh_from_db()
        assert newer.fulfilled_kg == Decimal("21100.000")

    def test_deleting_it_gives_each_bron_back_its_own_share(self, admin_client, db):
        _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        older, newer = _two_brons(customer)
        _sell(admin_client, customer, "LLDPE", "24100")
        admin_client.post(f"/sales/{Sale.objects.get().pk}/delete/", {})
        older.refresh_from_db()
        newer.refresh_from_db()
        assert older.fulfilled_kg == Decimal("0") and newer.fulfilled_kg == Decimal("0")
        assert older.status == Reservation.Status.ACTIVE
        assert not ReservationDraw.objects.exists()


class TestDetailPage:
    def test_lists_only_the_sotuvlar_counted_into_this_bron(self, admin_client, db):
        _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        older, newer = _two_brons(customer)
        _sell(admin_client, customer, "LLDPE", "2000")   # all of it into the older
        _sell(admin_client, customer, "LLDPE", "5000")   # 1 000 older, 4 000 newer
        first, second = Sale.objects.order_by("pk")
        page = admin_client.get(f"/reservations/{newer.pk}/").content.decode()
        assert f"/sales/{second.pk}/" in page
        assert f"/sales/{first.pk}/" not in page

    def test_translator_forbidden(self, translator_client, db):
        bron = make_bron(_customer())
        assert translator_client.get(f"/reservations/{bron.pk}/").status_code == 403


class TestBackfill:
    def test_rebuilds_a_spill_from_fulfilled_kg(self, db):
        """The prod shape: the sotuv links the older bron only, and the newer bron's
        berilgan kg are its spill."""
        lot = _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        older, newer = _two_brons(customer)
        sale = Sale.objects.create(customer=customer, line=lot, kg=Decimal("24100"),
                                   price=Decimal("1.4"), date="2026-07-20",
                                   reservation=older)
        Reservation.objects.filter(pk=older.pk).update(fulfilled_kg=Decimal("3000"))
        Reservation.objects.filter(pk=newer.pk).update(fulfilled_kg=Decimal("21100"))
        backfill_draws(Reservation, ReservationItem, ReservationDraw, Sale)
        assert [(d.reservation_id, d.kg) for d in sale.bron_draws.all()] == [
            (older.pk, Decimal("3000.000")), (newer.pk, Decimal("21100.000"))]

    def test_stops_when_a_bron_cannot_be_explained(self, db):
        bron = make_bron(_customer(), kg="5000")
        Reservation.objects.filter(pk=bron.pk).update(fulfilled_kg=Decimal("100"))
        with pytest.raises(RuntimeError, match=f"{bron.pk}"):
            backfill_draws(Reservation, ReservationItem, ReservationDraw, Sale)
