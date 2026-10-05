"""A sotuv that spills across two brons records its kg against each of them.

24 100 kg of 2102 репак closed bron #6 with 2 700 and put 21 400 into bron #9, while
the sotuv named only #6: bron #9's berilgan kg came from nowhere anybody could see,
and deleting the sotuv gave all 24 100 back to #6.
"""
import importlib
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest

from conftest import line_data, make_bron
from django.core.management import call_command
from django.utils import timezone
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


class TestCodes:
    def test_numbered_per_mijoz_and_never_reused(self, db):
        komoliddin, other = _customer("Komoliddin sintafon"), _customer("Mak Plast")
        first = make_bron(komoliddin)
        make_bron(other)
        second = make_bron(komoliddin)
        assert (first.code, second.code) == ("komoliddin-sintafon-1", "komoliddin-sintafon-2")
        second.delete()
        assert make_bron(komoliddin).code == "komoliddin-sintafon-3"

    def test_an_edit_keeps_the_code(self, db):
        bron = make_bron(_customer("Mak Plast"))
        bron.note = "tel qildi"
        bron.save()
        bron.refresh_from_db()
        assert bron.code == "mak-plast-1"


class TestQolganTolov:
    def test_only_the_share_given_here_is_owed_here(self, admin_client, db):
        """24 100 kg at $1.50, 3 000 into the older bron and 21 100 into the newer:
        unpaid, the newer bron is owed 21 100 × 1.50."""
        _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        older, newer = _two_brons(customer)
        assert newer.unpaid_given is None
        _sell(admin_client, customer, "LLDPE", "24100", price="1.50")
        newer = Reservation.objects.get(pk=newer.pk)
        assert newer.unpaid_given == Decimal("31650.00")
        older = Reservation.objects.get(pk=older.pk)
        assert older.unpaid_given == Decimal("4500.00")


class TestASotuvDatedBeforeTheBron:
    """Komoliddin's 28-sentabr delivery at 1.42, typed in eight minutes after his
    29-sentabr bron at 1.40 was struck, went into that bron with 21 400 kg it had
    nothing to do with."""

    def _post(self, client, customer, date, kg="5000", price="1.42", **extra):
        return client.post("/sales/new/", {
            "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
            "date": date, "draw_from_bron_asked": "1", "draw_from_bron": "on",
            **extra, **line_data({"brand": "LLDPE", "kg": kg, "price": price})})

    def test_is_not_counted_into_it(self, admin_client, db):
        _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        bron = make_bron(customer, brand="LLDPE", kg="75000", price="1.40")
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        self._post(admin_client, customer, yesterday)
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0")
        assert Sale.objects.get().reservation is None

    def test_one_dated_the_same_day_is(self, admin_client, db):
        _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        bron = make_bron(customer, brand="LLDPE", kg="75000", price="1.40")
        self._post(admin_client, customer, timezone.localdate().isoformat())
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("5000.000")


class TestTheSotuvButtonOfOneBron:
    """Two brons of one marka at 1.50 and 1.60: the button on the 1.60 one fills in
    1.60, and the kg have to come off that bron, not the oldest."""

    def test_serves_that_bron(self, admin_client, db):
        _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        older = make_bron(customer, brand="LLDPE", kg="10000", price="1.50")
        newer = make_bron(customer, brand="LLDPE", kg="10000", price="1.60")
        page = admin_client.get("/reservations/").content.decode()
        assert f"bron={newer.pk}&amp;" in page
        TestASotuvDatedBeforeTheBron()._post(
            admin_client, customer, timezone.localdate().isoformat(),
            kg="10000", price="1.60", bron=str(newer.pk))
        older.refresh_from_db()
        newer.refresh_from_db()
        assert newer.fulfilled_kg == Decimal("10000.000")
        assert newer.status == Reservation.Status.CONVERTED
        assert older.fulfilled_kg == Decimal("0")

    def test_the_form_carries_it(self, admin_client, db):
        customer = _customer()
        bron = make_bron(customer, brand="LLDPE", kg="10000", price="1.60")
        page = admin_client.get(f"/sales/new/?customer={customer.pk}&bron={bron.pk}"
                                ).content.decode()
        assert f'name="bron" value="{bron.pk}"' in page


class TestRebronByDate:
    """The repair for sotuvlar already counted into a bron struck after their sana."""

    def test_moves_a_late_typed_sotuv_out_of_the_newer_bron(self, admin_user):
        lot = _arrived_lot(kg="50000", brand="LLDPE")
        customer = _customer()
        bron = make_bron(customer, brand="LLDPE", kg="75000", price="1.40")
        yesterday = timezone.localdate() - timedelta(days=1)
        sale = Sale.objects.create(customer=customer, line=lot, kg=Decimal("5000"),
                                   price=Decimal("1.42"), date=yesterday,
                                   reservation=bron)
        ReservationDraw.objects.create(reservation=bron, sale=sale, kg=Decimal("5000"))
        Reservation.objects.filter(pk=bron.pk).update(fulfilled_kg=Decimal("5000"))

        call_command("rebron_by_date", stdout=StringIO())
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("5000.000")      # dry run writes nothing

        call_command("rebron_by_date", apply=True, user=admin_user.username,
                     stdout=StringIO())
        bron.refresh_from_db()
        sale.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0.000")
        assert sale.reservation is None and sale.price == Decimal("1.4200")
