"""Two names for one marka — refusing the next one, and joining the ones already in.

The bug this closes, in full: on 2026-09-04 a kelishuv was entered with the marka
`и 1561` (cyrillic и) while `i 1561` (latin i) was already on the books. The screen
transliterates lotin to kiril, so BOTH draw as `и 1561` and the operator had no way
to see there were two. Every join the app makes on a marka is an exact string match,
so the halves never met: a 150 000 kg bron sat at its full kg while its own holder
bought 36 000 kg of the same granula over the counter.

Two halves to the fix. `ContractLineForm` turns any second spelling into the marka on
file — that box is the only place a marka is typed. (It used to refuse an exact
lookalike instead, and let `7000 Repak` through as a new marka; see the class below.) `merge_brand` joins the pair already entered, and
replays the sotuvlar the split hid from the bron.
"""
from decimal import Decimal
from io import StringIO

import pytest
from conftest import make_contract, make_lot
from crm.forms import ContractLineForm
from crm.yozuv import marka_kaliti, marka_nomi
from crm.models import ContractLine, Customer, Partner, Reservation, Sale
from django.core.management import CommandError, call_command


def _customer(name="Mak Plast"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _line_data(brand, **kw):
    data = {"brand": brand, "kg": "1000", "price": "1.00", "planned_trucks": ""}
    data.update(kw)
    return data


def _typed(brand):
    """What the form makes of a typed marka, or its error."""
    form = ContractLineForm(_line_data(brand))
    return form.cleaned_data["brand"] if form.is_valid() else form.errors["brand"][0]


class TestTheFormJoinsTheSpellingOnFile:
    """Reported from the floor on 2026-09-10: `7000 repak` typed on a kelishuv was
    refused because `7000 репак` was on file — while `7000 Repak` would have been
    accepted as a brand-new marka. The refusal only caught PERFECT lookalikes, so the
    split it existed to prevent was one shift key away. Every spelling that differs
    by alphabet, case or spacing now simply is the marka on file."""

    @pytest.mark.parametrize("typed", [
        "7000 repak", "7000 Repak", "7000 REPAK", "7000 РЕПАК", "7000  репак",
        " 7000 репак ", "7000 репак",
    ])
    def test_every_spelling_of_the_reported_marka_is_that_marka(self, db, typed):
        make_contract(brand="7000 репак")
        assert _typed(typed) == "7000 репак"

    def test_a_lookalike_joins_the_name_on_file_whichever_alphabet_that_is(self, db):
        """The original 2026-09-04 pair, the other way round: whatever is ON FILE is
        the name kept, even when that is the lotin one."""
        make_contract(brand="i 1561")
        assert _typed("и 1561") == "i 1561"

    def test_campaund_in_lotin_finds_kampaund(self, db):
        """`c` is outside Uzbek Latin, so `to_kiril` leaves `campaund` alone — and the
        operator's keyboard spells it exactly that way."""
        make_contract(brand="7000 кампаунд")
        assert _typed("7000 campaund") == "7000 кампаунд"
        assert _typed("7000 CAMPAUND") == "7000 кампаунд"

    def test_repac_is_not_quietly_joined_to_repak(self, db):
        """0070 kept `2102 репас` apart from `2102 репак` on purpose — whether they are
        one granula is unanswered. Matching must not answer it by accident."""
        make_contract(brand="2102 репак")
        assert _typed("2102 repac") == "2102 репас"

    def test_the_same_marka_again_is_perfectly_fine(self, db):
        """A second kelishuv for a product already bought — the normal case, and the
        one a blunt "this marka exists" check would have broken."""
        make_contract(brand="i 1561")
        assert _typed("i 1561") == "i 1561"

    def test_a_genuinely_new_marka_is_stored_in_kiril(self, db):
        make_contract(brand="7000 репак")
        assert _typed("9000  repak") == "9000 репак"

    def test_a_code_keeps_its_letters(self, db):
        """LLDPE is a code (two capitals), and so is 7000F — a digit in the token. The
        lone F would otherwise come out as Ф."""
        make_contract(brand="LLDPE")
        assert _typed("HDPE") == "HDPE"
        assert _typed("7000F") == "7000F"
        assert _typed("lldpe") == "LLDPE"

    def test_the_first_marka_of_an_empty_book_passes(self, db):
        assert _typed("i 1561") == "и 1561"

    def test_a_name_that_fits_two_split_markalar_is_not_guessed(self, db):
        """Both halves of an unmerged pair on file: joining either would be a coin
        toss, so the operator is told which two and that they want merging."""
        make_contract(brand="i 1561")
        make_contract(brand="и 1561")
        message = _typed("I 1561")
        assert "i 1561" in message and "и 1561" in message

    def test_the_exact_name_still_saves_while_a_split_pair_exists(self, db):
        """So a kg correction on a kelishuv is not blocked by somebody else's split."""
        make_contract(brand="i 1561")
        make_contract(brand="и 1561")
        assert _typed("и 1561") == "и 1561"

    def test_the_kelishuv_form_saves_onto_the_marka_on_file(self, admin_client, db):
        """End to end, the reported screen: nothing new appears in the books."""
        make_contract(brand="7000 репак")
        partner = Partner.objects.create(name="Pars", phone="1", city="T")
        resp = admin_client.post("/contracts/new/", {
            "partner": partner.pk, "currency": "usd", "created": "2026-09-09",
            "note": "", "planned_trucks": "",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-brand": "7000 Repak", "lines-0-kg": "25000", "lines-0-price": "1.2",
        })
        assert resp.status_code == 302
        assert set(ContractLine.objects.values_list("brand", flat=True)) == {"7000 репак"}

    def test_two_spellings_on_one_kelishuv_are_one_row_twice(self, admin_client, db):
        """Row identity reads the same key, or `9000 repak` and `9000 Repak` typed on
        one new kelishuv would split its qolgan kg across two counters."""
        partner = Partner.objects.create(name="Pars", phone="1", city="T")
        resp = admin_client.post("/contracts/new/", {
            "partner": partner.pk, "currency": "usd", "created": "2026-09-09",
            "note": "", "planned_trucks": "",
            "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-brand": "9000 repak", "lines-0-kg": "1000", "lines-0-price": "1.2",
            "lines-1-brand": "9000 Repak", "lines-1-kg": "1000", "lines-1-price": "1.2",
        })
        assert resp.status_code == 200
        assert not ContractLine.objects.exists()


class TestTheMarkaKey:
    def test_the_key_is_the_same_for_every_spelling(self):
        spellings = ["7000 repak", "7000 Repak", "7000 REPAK", "7000 РЕПАК", "7000  репак"]
        assert {marka_kaliti(s) for s in spellings} == {"7000 репак"}

    def test_repac_and_repak_keys_differ(self):
        assert marka_kaliti("2102 repac") != marka_kaliti("2102 repak")

    def test_the_stored_name_keeps_the_case_it_was_typed_in(self):
        assert marka_nomi("Ftor Oq") == "Фтор Оқ"


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

    def test_a_new_name_needs_saying_so(self, admin_client, db):
        """A typo in --into would otherwise rename a live marka to nothing anybody is
        looking for. `--allow-new` is how a plain rename says it means it."""
        make_contract(brand="i 1561")
        with pytest.raises(CommandError):
            call_command("merge_brand", "i 1561", into="и 1561", stdout=StringIO())
        call_command("merge_brand", "i 1561", into="и 1561", allow_new=True,
                     apply=True, stdout=StringIO())
        assert ContractLine.objects.filter(brand="и 1561").count() == 1
        assert not ContractLine.objects.filter(brand="i 1561").exists()


class TestTheKirilMigrationMap:
    """`0070_markalar_kirilcha` moves every marka to the alphabet the ombor works in.

    The map is written out by hand, and a hand-written map is where a merge nobody
    asked for gets born: two entries landing on one name join two products silently.
    """

    @property
    def _map(self):
        from importlib import import_module
        return import_module("crm.migrations.0070_markalar_kirilcha").KIRIL

    def test_every_new_name_is_actually_kiril(self):
        """A target still carrying latin letters would leave the list in two
        alphabets, which is the thing the migration exists to end."""
        latin = {kiril for kiril in self._map.values()
                 if any(ch.isascii() and ch.isalpha() for ch in kiril)}
        assert not latin, f"lotin harflar qolgan: {latin}"

    def test_no_two_markalar_land_on_the_same_name(self):
        """One target per source. `i 1561` → `и 1561` IS a merge, but it merges with a
        name already in the books rather than with another entry of this map — a
        collision here would be a typo joining two live products."""
        targets = list(self._map.values())
        assert len(targets) == len(set(targets)), "ikkita marka bitta nomga tushyapti"

    def test_repac_and_repak_stay_apart(self):
        """Whether those two are one granula is a question nobody has answered, and
        joining them is the answer that cannot be undone."""
        assert self._map["2102 repac"] != self._map["2102 repak"]

    def test_the_one_deliberate_merge_is_still_there(self):
        assert self._map["i 1561"] == "и 1561"
