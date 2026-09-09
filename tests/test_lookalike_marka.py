"""Two names for one marka — refusing the next one, and joining the ones already in.

The bug this closes, in full: on 2026-09-04 a kelishuv was entered with the marka
`и 1561` (cyrillic и) while `i 1561` (latin i) was already on the books. The screen
transliterates lotin to kiril, so BOTH draw as `и 1561` and the operator had no way
to see there were two. Every join the app makes on a marka is an exact string match,
so the halves never met: a 150 000 kg bron sat at its full kg while its own holder
bought 36 000 kg of the same granula over the counter.

Two halves to the fix. `ContractLineForm` refuses the second spelling — that box is
the only place a marka is typed. `merge_brand` joins the pair already entered, and
replays the sotuvlar the split hid from the bron.
"""
from decimal import Decimal
from io import StringIO

import pytest
from conftest import make_contract, make_lot
from crm.forms import ContractLineForm
from crm.models import ContractLine, Customer, Partner, Reservation, Sale
from django.core.management import CommandError, call_command


def _customer(name="Mak Plast"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _line_data(brand, **kw):
    data = {"brand": brand, "kg": "1000", "price": "1.00", "planned_trucks": ""}
    data.update(kw)
    return data


class TestTheFormRefusesTheSecondSpelling:
    def test_a_lookalike_marka_is_refused(self, db):
        make_contract(brand="i 1561")
        form = ContractLineForm(_line_data("и 1561"))
        assert not form.is_valid()
        assert "i 1561" in form.errors["brand"][0]

    def test_the_message_says_what_they_both_look_like(self, db):
        make_contract(brand="i 1561")
        form = ContractLineForm(_line_data("и 1561"))
        assert "и 1561" in form.errors["brand"][0]

    def test_the_same_marka_again_is_perfectly_fine(self, db):
        """A second kelishuv for a product already bought — the normal case, and the
        one a blunt "this marka exists" check would have broken."""
        make_contract(brand="i 1561")
        assert ContractLineForm(_line_data("i 1561")).is_valid()

    def test_a_genuinely_new_marka_passes(self, db):
        make_contract(brand="i 1561")
        assert ContractLineForm(_line_data("i 1562")).is_valid()

    def test_a_code_is_never_transliterated_so_never_collides(self, db):
        """LLDPE and HDPE keep their letters — two capitals in a row read as a code,
        which is what stops the guard firing on every marka in the ombor."""
        make_contract(brand="LLDPE")
        assert ContractLineForm(_line_data("HDPE")).is_valid()

    def test_the_first_marka_of_an_empty_book_passes(self, db):
        assert ContractLineForm(_line_data("i 1561")).is_valid()


class TestMergingThePairAlreadyEntered:
    """`merge_brand` — the repair for the two that are already in."""

    def _both_markalar(self, admin_client):
        """The prod shape: an arrived kelishuv under the latin name, an unshipped one
        under the cyrillic, and a bron booked against the wrong half."""
        lot = make_lot(kg="120000", brand="i 1561", arrived="2026-09-02")
        birja = Partner.objects.create(name="Birja", phone="1", city="T")
        kiril = make_contract(partner=birja, brand="и 1561", kg="67000")
        return lot, kiril

    def test_a_dry_run_changes_nothing(self, admin_client, db):
        self._both_markalar(admin_client)
        out = StringIO()
        call_command("merge_brand", "и 1561", into="i 1561", stdout=out)
        assert "QURUQ SINOV" in out.getvalue()
        assert ContractLine.objects.filter(brand="и 1561").exists()

    def test_apply_joins_the_two_names(self, admin_client, db):
        self._both_markalar(admin_client)
        call_command("merge_brand", "и 1561", into="i 1561", apply=True, stdout=StringIO())
        assert not ContractLine.objects.filter(brand="и 1561").exists()
        assert ContractLine.objects.filter(brand="i 1561").count() == 2

    def test_the_bron_moves_with_it_and_catches_up_on_past_sales(self, admin_client, db):
        """The heart of it: a bron booked against the retired name is renamed AND
        drawn down by the sotuvlar it should have been serving all along."""
        lot, _kiril = self._both_markalar(admin_client)
        mijoz = _customer()
        bron = Reservation.objects.create(customer=mijoz, brand="и 1561",
                                          kg=Decimal("150000"), price=Decimal("1.5"))
        for kg in ("7000", "3000"):
            Sale.objects.create(customer=mijoz, line=lot, kg=Decimal(kg),
                                price=Decimal("1.5"), date="2026-09-08")
        assert bron.remaining_kg == Decimal("150000.000")   # untouched while split

        call_command("merge_brand", "и 1561", into="i 1561", apply=True, stdout=StringIO())

        bron.refresh_from_db()
        assert bron.brand == "i 1561"
        assert bron.fulfilled_kg == Decimal("10000.000")
        assert bron.remaining_kg == Decimal("140000.000")

    def test_somebody_elses_sotuv_is_left_alone(self, admin_client, db):
        lot, _kiril = self._both_markalar(admin_client)
        holder, walk_in = _customer(), _customer("Faxriddin oka")
        bron = Reservation.objects.create(customer=holder, brand="и 1561",
                                          kg=Decimal("150000"), price=Decimal("1.5"))
        Sale.objects.create(customer=walk_in, line=lot, kg=Decimal("5000"),
                            price=Decimal("1.5"), date="2026-09-08")
        call_command("merge_brand", "и 1561", into="i 1561", apply=True, stdout=StringIO())
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0.000")

    def test_a_bron_taken_after_the_sotuv_is_not_eaten_by_it(self, admin_client, db):
        """The replay must not rewrite history backwards: a promise made today was
        not served by a sotuv from last week."""
        lot, _kiril = self._both_markalar(admin_client)
        mijoz = _customer()
        Sale.objects.create(customer=mijoz, line=lot, kg=Decimal("7000"),
                            price=Decimal("1.5"), date="2026-09-01")
        bron = Reservation.objects.create(customer=mijoz, brand="и 1561",
                                          kg=Decimal("150000"), price=Decimal("1.5"))
        call_command("merge_brand", "и 1561", into="i 1561", apply=True, stdout=StringIO())
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0.000")

    def test_it_refuses_a_marka_that_is_not_there(self, admin_client, db):
        make_contract(brand="i 1561")
        with pytest.raises(CommandError):
            call_command("merge_brand", "yo'q 999", into="i 1561", stdout=StringIO())

    def test_it_refuses_to_merge_a_name_into_itself(self, admin_client, db):
        make_contract(brand="i 1561")
        with pytest.raises(CommandError):
            call_command("merge_brand", "i 1561", into="i 1561", stdout=StringIO())
