import html
import re
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import openpyxl
from conftest import line_data
from django.utils import timezone
from crm.models import (
    Contract, ContractLine, Customer, CustomerPayment, Partner, PaymentAllocation, Reservation, Sale, Shipment, ShipmentLine, ShipmentStatus,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _plain(page_html):
    """The rendered page as a reader sees it: the NBSP thousands separators read
    as ordinary spaces and the entities unescaped, so an assertion can look for
    "1 000" and "so'm" rather than for \\xa0 and `so&#x27;m`."""
    return html.unescape(page_html).replace("\u00a0", " ")


def _arrived_lot(kg="10000", brand="LLDPE", contract_price="1.00"):
    return _arrived_lot_for("Pars", kg=kg, brand=brand, contract_price=contract_price)


def _arrived_lot_for(partner_name, kg="10000", brand="LLDPE", contract_price="1.00"):
    partner = Partner.objects.create(name=partner_name, phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal(contract_price))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(), sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16", transport="01A111AA", container="MSCU-1")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal(kg))
    return _ship_obj_line


def _in_transit_lot(kg="5000", brand="HDPE"):
    partner = Partner.objects.create(name="Iran Co", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    contract_line = ContractLine.objects.create(
        contract=contract, brand=brand, kg=Decimal(kg), price=Decimal("1.00"))
    _ship_obj = Shipment.objects.create(contract=contract, status=ShipmentStatus.objects.exclude(is_arrival=True).first(), sent="2026-07-05", eta="2026-08-01")
    _ship_obj_line = ShipmentLine.objects.create(
        shipment=_ship_obj, contract_line=contract.lines.first(), kg=Decimal(kg))
    return _ship_obj_line


def _reserve(admin_client, brand, customer, kg="5000", price="", currency="usd",
             exchange_rate="12000"):
    """A bron is taken against a MARKA — no lot, because whichever kelishuv's truck
    lands first with that granula fills it."""
    return admin_client.post("/reservations/new/", {
        "customer": customer.pk, "brand": brand, "kg": kg, "currency": currency,
        "price": price, "exchange_rate": exchange_rate, "note": "",
    })


def _convert(admin_client, reservation, price=None, kg=None):
    """Hand kg over from a bron.

    Through the ordinary sotuv form with Brondan ushlansin ticked, which is what the
    Bronlar row links to now that the one-click Berish is gone. Without `kg`, the
    most the bron can still take off the shelf — the ceiling the old button used.

    The bron's money shape is posted with it, the way the button used to carry it
    over: its currency, its kurs and its agreed narx, so a so'm bron does not become
    a dollar sotuv re-rated at today's kurs.
    """
    from crm.models import brand_on_hand_kg

    reservation.refresh_from_db()
    give = kg if kg is not None else min(reservation.remaining_kg,
                                         brand_on_hand_kg(reservation.brand))
    if price is not None:
        narx = price
    elif reservation.is_som:
        narx = reservation.price_uzs      # typed in the row's own currency
    else:
        narx = reservation.price
    return admin_client.post("/sales/new/", {
        "customer": reservation.customer_id,
        "currency": reservation.currency,
        "exchange_rate": str(reservation.exchange_rate),
        "date": "2026-07-20", "debt_deadline": "", "note": "",
        "draw_from_bron_asked": "1", "draw_from_bron": "on",
        **line_data({"brand": reservation.brand, "kg": str(give),
                     "price": "" if narx is None else str(narx)}),
    })


class TestBronIsAgainstAMarka:
    def test_bron_needs_no_lot_and_no_stock(self, admin_client, db):
        """Booking granula that has not been sent yet is the point of the screen."""
        lot = _in_transit_lot(kg="5000", brand="HDPE")
        resp = _reserve(admin_client, "HDPE", _customer(), kg="40000")
        assert resp.status_code == 302
        bron = Reservation.objects.get()
        assert bron.brand == "HDPE"
        assert bron.kg == Decimal("40000.000")
        assert bron.remaining_kg == Decimal("40000.000")
        assert not hasattr(bron, "line")

    def test_any_kelishuv_can_fill_it(self, admin_client, db):
        """The bron names LLDPE; a truck from a different hamkor brings LLDPE and
        that is what fills it."""
        _arrived_lot(kg="6000", brand="LLDPE")             # Pars
        other = _arrived_lot_for("Boshqa hamkor", kg="4000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="9000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron)
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("9000.000")
        # spread across both kelishuvlar's lots, oldest first
        assert sorted(s.kg for s in Sale.objects.all()) == [Decimal("3000.000"),
                                                            Decimal("6000.000")]
        assert {s.line.contract_line.contract.partner.name for s in Sale.objects.all()} \
            == {"Pars", "Boshqa hamkor"}

    def test_brand_choices_include_markalar_with_no_stock(self, admin_client, db):
        _in_transit_lot(kg="5000", brand="HDPE")
        html = admin_client.get("/reservations/new/").content.decode()
        assert "HDPE" in html
        # Django escapes the apostrophe in "yo'q", so match the part without one
        assert "hozircha omborda" in html


class TestBookingOrderIsOnlyVisual:
    """Who bronned first is shown, never enforced — the hand-over is agreed off the
    screen, so the screen must not veto it."""

    def test_a_later_bron_can_be_served_first(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        first = _customer("Birinchi")
        second = _customer("Ikkinchi")
        _reserve(admin_client, "LLDPE", first, kg="4000", price="2.00")
        _reserve(admin_client, "LLDPE", second, kg="4000", price="2.00")
        a, b = Reservation.objects.order_by("created_at", "pk")

        _convert(admin_client, b)
        b.refresh_from_db()
        assert b.status == "converted"
        assert Sale.objects.get().customer == second

        # and the earlier one is untouched, still owed its full kg
        a.refresh_from_db()
        assert a.fulfilled_kg == Decimal("0")
        _convert(admin_client, a)
        a.refresh_from_db()
        assert a.status == "converted"

    def test_queue_positions_are_per_marka(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _arrived_lot_for("Ikkinchi", kg="10000", brand="HDPE")
        _reserve(admin_client, "LLDPE", _customer("Bir"), kg="1000", price="2.00")
        _reserve(admin_client, "HDPE", _customer("Ikki"), kg="1000", price="2.00")
        rows = {r.customer.name: r for r in admin_client.get("/reservations/").context["rows"]}
        assert rows["Bir"].queue_pos == 1
        assert rows["Ikki"].queue_pos == 1       # first of its own marka

    def test_list_reports_position_and_who_asked_first(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Birinchi"), kg="1000")
        _reserve(admin_client, "LLDPE", _customer("Ikkinchi"), kg="1000")
        rows = {r.customer.name: r for r in admin_client.get("/reservations/").context["rows"]}
        assert rows["Birinchi"].queue_pos == 1
        assert rows["Ikkinchi"].queue_pos == 2
        assert rows["Ikkinchi"].ahead_of.customer.name == "Birinchi"
        # being second says nothing about whether it can be handed over
        assert rows["Ikkinchi"].servable_kg == Decimal("1000.000")

    def test_cancelling_one_renumbers_the_rest(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bir"), kg="1000", price="2.00")
        _reserve(admin_client, "LLDPE", _customer("Ikki"), kg="1000", price="2.00")
        a, _b = Reservation.objects.order_by("created_at", "pk")
        admin_client.post(f"/reservations/{a.pk}/cancel/", {})
        rows = {r.customer.name: r for r in admin_client.get("/reservations/").context["rows"]}
        assert rows["Ikki"].queue_pos == 1
        assert rows["Ikki"].ahead_of is None


class TestPartialFulfilment:
    def test_short_arrival_fills_what_it_can_and_stays_open(self, admin_client, db):
        _arrived_lot(kg="12000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron)
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("12000.000")
        assert bron.remaining_kg == Decimal("8000.000")
        assert bron.status == "active"          # still queued for the rest
        assert Sale.objects.get().kg == Decimal("12000.000")

    def test_next_arrival_closes_it(self, admin_client, db):
        _arrived_lot(kg="12000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron)
        _arrived_lot_for("Keyingi", kg="30000", brand="LLDPE")
        _convert(admin_client, bron)
        bron.refresh_from_db()
        assert bron.remaining_kg == Decimal("0.000")
        assert bron.status == "converted"
        assert sum(s.kg for s in Sale.objects.all()) == Decimal("20000.000")

    def test_a_part_filled_bron_only_reports_what_is_still_owed(self, admin_client, db):
        _arrived_lot(kg="12000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000", price="2.00")
        _convert(admin_client, Reservation.objects.get())
        _arrived_lot_for("Keyingi", kg="30000", brand="LLDPE")
        from crm.models import brand_on_hand_kg, brand_reserved_kg
        assert brand_reserved_kg("LLDPE") == Decimal("8000.000")
        # promised, not held: the whole new truck is still sellable
        assert brand_on_hand_kg("LLDPE") == Decimal("30000.000")

    def test_nothing_on_the_shelf_is_refused_not_half_done(self, admin_client, db):
        _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "HDPE", _customer(), kg="2000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron)
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0")
        assert not Sale.objects.exists()


class TestPartOfABronCanBeHandedOver:
    """The mijoz booked 20 000 and came for 5 000 today. That is the ordinary case,
    not an exception — so the kg handed over is the operator's to type."""

    def test_giving_less_than_is_available_leaves_the_rest_open(self, admin_client, db):
        _arrived_lot(kg="20000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron, kg="5000")
        bron.refresh_from_db()
        assert Sale.objects.get().kg == Decimal("5000.000")
        assert bron.fulfilled_kg == Decimal("5000.000")
        assert bron.remaining_kg == Decimal("15000.000")
        assert bron.status == "active"

    def test_the_rest_can_be_collected_later(self, admin_client, db):
        _arrived_lot(kg="20000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron, kg="5000")
        _convert(admin_client, bron, kg="15000")
        bron.refresh_from_db()
        assert bron.status == "converted"
        assert sum(s.kg for s in Sale.objects.all()) == Decimal("20000.000")

    def test_more_than_is_owed_draws_the_bron_dry_and_sells_the_rest(self, admin_client, db):
        """The old one-click Berish refused this outright; the sotuv form does not,
        and should not. The mijoz booked 5 000, wants 6 000, and there are 20 000 on
        the shelf — the bron is served in full and the extra 1 000 is an ordinary
        sotuv. Only the shelf can say no."""
        _arrived_lot(kg="20000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="2.00")
        bron = Reservation.objects.get()
        assert _convert(admin_client, bron, kg="6000").status_code == 302

        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("5000.000")   # drawn dry, never past it
        assert bron.remaining_kg == Decimal("0.000")
        assert bron.status == "converted"
        assert sum(s.kg for s in Sale.objects.all()) == Decimal("6000.000")

    def test_more_than_has_landed_is_refused(self, admin_client, db):
        _arrived_lot(kg="3000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron, kg="5000")
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0")
        assert not Sale.objects.exists()

    def test_a_zero_or_negative_kg_is_refused(self, admin_client, db):
        _arrived_lot(kg="20000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="2.00")
        bron = Reservation.objects.get()
        for kg in ["0", "-100", "salom"]:
            _convert(admin_client, bron, kg=kg)
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0")
        assert not Sale.objects.exists()


class TestBronsDoNotBlockOrdinarySales:
    """A bron names who is waiting; it does not hold the granula. A mijoz turns up
    with cash for kg promised to somebody who is not collecting, and that sotuv has
    to go through — the operator settles the promise, the screen does not."""

    def test_bronned_kg_can_still_be_sold_to_somebody_else(self, admin_client, db):
        """The whole shelf, every kg of it promised to another mijoz."""
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": _customer("Kelgan mijoz").pk,
            "currency": "usd", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
            **line_data({"brand": "LLDPE", "kg": "24000", "price": "2.00"}),
        })
        assert resp.status_code == 302
        assert Sale.objects.get().kg == Decimal("24000.000")

    def test_the_bron_it_walked_over_stays_open(self, admin_client, db):
        """Selling to somebody else does not settle the promise — it is still owed,
        and now the ombor says the marka is short."""
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        admin_client.post("/sales/new/", {
            "customer": _customer("Kelgan mijoz").pk,
            "currency": "usd", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
            **line_data({"brand": "LLDPE", "kg": "24000", "price": "2.00"}),
        })
        bron = Reservation.objects.get()
        assert bron.remaining_kg == Decimal("20000.000")
        assert bron.status == Reservation.Status.ACTIVE
        g = next(x for x in admin_client.get("/ombor/").context["page"]
                 if x["brand"] == "LLDPE")
        assert g["short"] == Decimal("20000.000")

    def test_a_bronned_marka_can_be_sold_from_one_lot(self, admin_client, db):
        """The per-lot sotuv had the same bron ceiling on top of the lot's own kg."""
        lot = _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="10000")
        resp = admin_client.post("/sales/new/", {
            "lot": lot.pk, "customer": _customer("Kelgan mijoz").pk, "kg": "10000",
            "currency": "usd", "price": "2.00", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
        })
        assert resp.status_code == 302
        assert Sale.objects.get().kg == Decimal("10000.000")

    def test_the_bron_holder_may_buy_their_own_bronned_granula(self, admin_client, db):
        """Their promise does not stand between them and what it promises them."""
        _arrived_lot(kg="24000", brand="LLDPE")
        holder = _customer("Bron egasi")
        _reserve(admin_client, "LLDPE", holder, kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": holder.pk,
            "currency": "usd", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
            **line_data({"brand": "LLDPE", "kg": "24000", "price": "2.00"}),
        })
        assert resp.status_code == 302
        assert Sale.objects.get().kg == Decimal("24000.000")

    def test_the_shelf_is_still_the_ceiling(self, admin_client, db):
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": _customer("Kelgan mijoz").pk,
            "currency": "usd", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
            **line_data({"brand": "LLDPE", "kg": "24001", "price": "2.00"}),
        })
        assert resp.status_code == 200          # re-rendered, invalid
        assert not Sale.objects.exists()

    def test_ombor_shows_the_promise_beside_the_shelf(self, admin_client, db):
        """Bronlangan says who asked; it does not come out of Sotish mumkin."""
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        ctx = admin_client.get("/ombor/").context
        g = next(x for x in ctx["page"] if x["brand"] == "LLDPE")
        assert g["on_hand"] == Decimal("24000.000")
        assert g["reserved"] == Decimal("20000.000")
        html = admin_client.get("/ombor/").content.decode()
        assert "Bronlangan" in html and "Sotish mumkin" in html
        assert "Bron egasi" in html

    def test_ombor_flags_a_shortfall(self, admin_client, db):
        """Bronned more than has landed — legitimate, and worth saying out loud."""
        _arrived_lot(kg="5000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000")
        g = next(x for x in admin_client.get("/ombor/").context["page"]
                 if x["brand"] == "LLDPE")
        assert g["on_hand"] == Decimal("5000.000")
        assert g["short"] == Decimal("15000.000")


class TestArrivalSurfacesBrons:
    def test_marking_arrived_counts_brons_for_that_marka(self, admin_client, db):
        lot = _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "HDPE", _customer(), kg="2000", price="1.50")
        resp = admin_client.post(
            f"/shipments/{lot.shipment_id}/status/",
            {"status": ShipmentStatus.arrival().pk},
            headers={"X-Requested-With": "XMLHttpRequest"})
        data = resp.json()
        assert data["arrived"] is True and data["bron_count"] == 1

    def test_a_bron_for_another_marka_does_not_count(self, admin_client, db):
        lot = _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="2000")
        resp = admin_client.post(
            f"/shipments/{lot.shipment_id}/status/",
            {"status": ShipmentStatus.arrival().pk},
            headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.json()["bron_count"] == 0

    def test_cancelled_brons_do_not_count(self, admin_client, db):
        lot = _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "HDPE", _customer(), kg="2000")
        admin_client.post(f"/reservations/{Reservation.objects.get().pk}/cancel/", {})
        resp = admin_client.post(
            f"/shipments/{lot.shipment_id}/status/",
            {"status": ShipmentStatus.arrival().pk},
            headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.json()["bron_count"] == 0


class TestEditAndDelete:
    def test_edit_changes_kg(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        customer = _customer()
        _reserve(admin_client, "LLDPE", customer, kg="5000")
        r = Reservation.objects.get()
        resp = admin_client.post(f"/reservations/{r.pk}/edit/", {
            "customer": customer.pk, "brand": "LLDPE", "kg": "3000",
            "currency": "usd", "price": "1.25", "exchange_rate": "12000", "note": "",
        })
        assert resp.status_code == 302
        r.refresh_from_db()
        assert r.kg == Decimal("3000.000")

    def test_cannot_shrink_below_what_is_already_given(self, admin_client, db):
        _arrived_lot(kg="12000", brand="LLDPE")
        customer = _customer()
        _reserve(admin_client, "LLDPE", customer, kg="20000", price="2.00")
        r = Reservation.objects.get()
        _convert(admin_client, r)                       # 12 000 kg handed over
        resp = admin_client.post(f"/reservations/{r.pk}/edit/", {
            "customer": customer.pk, "brand": "LLDPE", "kg": "5000",
            "currency": "usd", "price": "2.00", "exchange_rate": "12000", "note": "",
        })
        assert resp.status_code == 200                  # invalid, re-rendered
        r.refresh_from_db()
        assert r.kg == Decimal("20000.000")

    def test_delete_removes_it(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000")
        r = Reservation.objects.get()
        assert admin_client.post(f"/reservations/{r.pk}/delete/", {}).status_code == 302
        assert not Reservation.objects.exists()

    def test_converted_bron_is_not_deletable(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="2.00")
        r = Reservation.objects.get()
        _convert(admin_client, r)
        admin_client.post(f"/reservations/{r.pk}/delete/", {})
        assert Reservation.objects.filter(pk=r.pk).exists()


class TestCancel:
    def test_cancel_drops_the_promise_and_leaves_the_shelf_alone(self, admin_client, db):
        from crm.models import brand_on_hand_kg, brand_reserved_kg
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000")
        assert brand_reserved_kg("LLDPE") == Decimal("5000.000")
        admin_client.post(f"/reservations/{Reservation.objects.get().pk}/cancel/", {})
        assert brand_reserved_kg("LLDPE") == Decimal("0")
        assert brand_on_hand_kg("LLDPE") == Decimal("10000.000")   # never moved


class TestEarmarkedPayment:
    def test_earmarked_payment_applies_first_on_convert(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        customer = _customer()
        _reserve(admin_client, "LLDPE", customer, kg="5000", price="1.50")
        r = Reservation.objects.get()
        payment = CustomerPayment.objects.create(
            customer=customer, date="2026-07-17", amount=Decimal("2000.00"),
            reservation=r)
        _convert(admin_client, r)
        sale = Sale.objects.get(reservation=r)
        assert PaymentAllocation.objects.get(payment=payment, sale=sale).amount \
            == Decimal("2000.00")


class TestPermissions:
    def test_translator_forbidden(self, translator_client, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        customer = _customer()
        assert translator_client.get("/reservations/").status_code == 403
        assert translator_client.get("/reservations/new/").status_code == 403
        assert translator_client.post("/reservations/new/", {
            "customer": customer.pk, "brand": "LLDPE", "kg": "100", "note": "",
        }).status_code == 403

    def test_translator_cannot_edit_or_delete(self, translator_client, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000")
        r = Reservation.objects.get()
        assert translator_client.get(f"/reservations/{r.pk}/edit/").status_code == 403
        assert translator_client.post(f"/reservations/{r.pk}/delete/", {}).status_code == 403


class TestReservationList:
    def test_create_button_on_the_page(self, admin_client, db):
        html = admin_client.get("/reservations/").content.decode()
        assert "/reservations/new/" in html and "Yangi bron" in html

    def test_ready_and_waiting_filters(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "LLDPE", _customer("Tayyor"), kg="1000")
        _reserve(admin_client, "HDPE", _customer("Kutmoqda"), kg="1000")
        ready = admin_client.get("/reservations/?lot=ready").context["rows"]
        waiting = admin_client.get("/reservations/?lot=waiting").context["rows"]
        assert [r.customer.name for r in ready] == ["Tayyor"]
        assert [r.customer.name for r in waiting] == ["Kutmoqda"]

    def test_search_by_customer_and_marka(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "LLDPE", _customer("Alisher Mebel"), kg="2000")
        _reserve(admin_client, "HDPE", _customer("Bobur Plast"), kg="1000")
        for query, expected in [("Alisher", 1), ("HDPE", 1), ("zzz", 0)]:
            ctx = admin_client.get("/reservations/", {"q": query}).context
            assert len(ctx["rows"]) == expected, query

    def test_status_counts_are_faceted(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("A"), kg="2000")
        _reserve(admin_client, "LLDPE", _customer("B"), kg="3000")
        admin_client.post(
            f"/reservations/{Reservation.objects.order_by('pk').first().pk}/cancel/", {})
        tabs = {t["key"]: t["count"]
                for t in admin_client.get("/reservations/").context["status_tabs"]}
        assert tabs == {"active": 1, "converted": 0, "closed": 0,
                        "cancelled": 1, "": 2}


class TestBronlarReadsLikeKelishuvlar:
    """The list wears the Kelishuvlar shape: one row per booking with its markalar
    stacked inside it, Qolgan as a badge that says whether anything is left, a Jami
    column, and the toolbar every other ro'yxat has — davr, Filtrlash, Excel."""

    def test_one_mijoz_on_one_kun_is_one_row(self, admin_client, db):
        """Two markalar booked together are one booking, not two table rows."""
        _arrived_lot(kg="10000", brand="LLDPE")
        _in_transit_lot(kg="5000", brand="HDPE")
        mijoz = _customer("Mak Plast")
        _reserve(admin_client, "LLDPE", mijoz, kg="1000", price="2.00")
        _reserve(admin_client, "HDPE", mijoz, kg="2000", price="2.00")
        groups = admin_client.get("/reservations/").context["groups"]
        assert len(groups) == 1
        assert groups[0]["customer"] == mijoz
        assert sorted(r.brand for r in groups[0]["items"]) == ["HDPE", "LLDPE"]

    def test_another_mijoz_is_another_row(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bir"), kg="1000", price="2.00")
        _reserve(admin_client, "LLDPE", _customer("Ikki"), kg="1000", price="2.00")
        groups = admin_client.get("/reservations/").context["groups"]
        assert [g["customer"].name for g in groups] == ["Bir", "Ikki"]
        assert [len(g["items"]) for g in groups] == [1, 1]

    def test_a_different_kun_is_a_different_row(self, admin_client, db):
        """The group is the BOOKING — what was put down on one day — so the same
        mijoz coming back next week starts a row of their own."""
        _arrived_lot(kg="10000", brand="LLDPE")
        _in_transit_lot(kg="5000", brand="HDPE")
        mijoz = _customer("Mak Plast")
        _reserve(admin_client, "LLDPE", mijoz, kg="1000", price="2.00")
        _reserve(admin_client, "HDPE", mijoz, kg="1000", price="2.00")
        old = Reservation.objects.order_by("pk").first()
        Reservation.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=7))
        groups = admin_client.get("/reservations/").context["groups"]
        assert [len(g["items"]) for g in groups] == [1, 1]
        assert len({g["date"] for g in groups}) == 2

    def test_the_page_never_splits_a_booking_across_two_pages(self, admin_client, db):
        """Paginated by GROUP: 20 bookings to a page, not 20 bronlar — half a mijoz's
        day at the foot of one page and half at the head of the next is the thing
        grouping exists to prevent."""
        for n in range(21):
            mijoz = _customer(f"Mijoz {n:02d}")
            for brand in ("LLDPE", "HDPE"):
                Reservation.objects.create(customer=mijoz, brand=brand,
                                           kg=Decimal("10"), price=Decimal("2"))
        page = admin_client.get("/reservations/?sort=customer").context["page"]
        assert page.paginator.num_pages == 2
        assert len(page.object_list) == 20
        assert all(len(g["items"]) == 2 for g in page.object_list)

    def test_the_columns_stand_in_kelishuvlar_order(self, admin_client, db):
        """Marka, then what was agreed, at what narx, for how much, and what of it is
        still outstanding — the order Kelishuvlar reads in, so the two lists do not ask
        the reader to change gear between them. Qolgan kg sits AFTER Jami, not between
        Kg and Narx.

        The avans pair comes after Qolgan kg rather than between it and Jami: it is the
        money side of what is still outstanding (read off the kg still owed), so it
        continues the sentence instead of interrupting it."""
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="1000", price="2.00")
        page = _plain(admin_client.get("/reservations/").content.decode())
        header = page[page.index("<table"):page.index("</tr>")]
        assert re.findall(r">([^<>]+)</th>", header) == [
            "Mijoz", "Sana", "Marka", "Navbat", "Bron qilingan kg", "Narx", "Jami",
            "Qolgan kg", "Avansdan band", "Yetishmayapti", "Holat"]

    def test_qolgan_is_a_badge_that_says_whether_anything_is_left(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="1000", price="2.00")
        html = _plain(admin_client.get("/reservations/").content.decode())
        assert 'class="badge badge-warning">1 000</span>' in html

    def test_a_served_out_bron_goes_green(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="1000", price="2.00")
        _convert(admin_client, Reservation.objects.get())
        html = _plain(admin_client.get("/reservations/?status=converted").content.decode())
        assert 'class="badge badge-ok">0</span>' in html

    def test_jami_is_kg_times_narx_in_the_bron_s_own_currency(self, admin_client, db):
        """A so'm bron reads in so'm. The dollar twin is stored, but printing it here
        would be a conversion nobody agreed to — the rule the rest of the app follows."""
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="17000",
                 currency="uzs")
        assert Reservation.objects.get().total_uzs == Decimal("85000000.00")
        html = _plain(admin_client.get("/reservations/").content.decode())
        assert "85 000 000 so'm" in html
        assert "$7 083" not in html

    def test_jami_is_kelishilmagan_until_a_narx_is_agreed(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000")
        assert Reservation.objects.get().total is None
        assert "kelishilmagan" in admin_client.get("/reservations/").content.decode()

    def test_the_four_selects_live_in_the_filtrlar_panel(self, admin_client, db):
        filters = admin_client.get("/reservations/").context["filters"]
        assert [f["name"] for f in filters["fields"]] == [
            "customer", "status", "lot", "sort"]
        # Faol and Navbat are where the page opens, so standing on them is not a
        # filter and draws no chip.
        assert filters["count"] == 0

    def test_a_narrowed_list_says_so_as_a_chip(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Mak Plast"), kg="1000")
        filters = admin_client.get("/reservations/?lot=ready").context["filters"]
        assert filters["count"] == 1
        assert filters["chips"][0]["label"] == "Berish"
        assert filters["chips"][0]["value"] == "Berish mumkin"

    def test_the_davr_narrows_the_list(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Eski"), kg="1000")
        _reserve(admin_client, "LLDPE", _customer("Yangi"), kg="1000")
        old = Reservation.objects.get(customer__name="Eski")
        Reservation.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=40))
        bugun = timezone.localdate().isoformat()
        rows = admin_client.get("/reservations/", {"from": bugun}).context["rows"]
        assert [r.customer.name for r in rows] == ["Yangi"]
        # Hammasi puts the older bron back.
        assert len(admin_client.get("/reservations/").context["rows"]) == 2

    def test_the_excel_button_downloads_what_the_screen_shows(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "LLDPE", _customer("Tayyor"), kg="1000", price="2.00")
        _reserve(admin_client, "HDPE", _customer("Kutmoqda"), kg="1000", price="2.00")
        assert "/reservations/export.xlsx" in admin_client.get(
            "/reservations/").content.decode()

        resp = admin_client.get("/reservations/export.xlsx", {"lot": "ready"})
        ws = openpyxl.load_workbook(BytesIO(resp.content)).worksheets[0]
        headers = [c.value for c in ws[1]]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        assert len(rows) == 1
        assert rows[0][headers.index("Mijoz")] == "Tayyor"
        assert rows[0][headers.index("Qolgan kg")] == 1000


class TestReservationTotal:
    def test_total_is_none_until_a_narx_is_agreed(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000")
        assert Reservation.objects.get().total is None
        assert "kelishilmagan" in admin_client.get("/reservations/").content.decode()

    def test_total_is_kg_times_narx_in_both_currencies(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="1.50")
        r = Reservation.objects.get()
        assert r.total == Decimal("7500.00")
        assert r.total_uzs == Decimal("90000000.00")   # 5000 × 1.50 × 12000


class TestAnOrdinarySotuvDrawsTheBronDown:
    """The bug: fulfilled_kg was only ever written by the Brondan sotuv button, so
    an ordinary sotuv to the bron's OWN holder left the promise at its full kg —
    bron #1 in the real book (74 400 kg of 2102 campaund) still read as fully owed
    while its holder bought 24 000 kg of it over the counter."""

    def _bron(self, customer, brand="LLDPE", kg="6000"):
        return Reservation.objects.create(
            customer=customer, brand=brand, kg=Decimal(kg), price=Decimal("1.50"))

    def _sell(self, client, customer, brand, kg):
        return client.post("/sales/new/", {
            "customer": customer.pk,
            "currency": "usd", "exchange_rate": "12000", "date": "2026-07-20",
            **line_data({"brand": brand, "kg": kg, "price": "1.50"})})

    def test_selling_to_the_holder_shrinks_their_bron(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        bron = self._bron(customer)                       # 6 000 kg promised

        assert self._sell(admin_client, customer, lot.brand, "2000").status_code in (204, 302)
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("2000.000")
        assert bron.remaining_kg == Decimal("4000.000")
        assert bron.status == Reservation.Status.ACTIVE

    def test_the_sotuv_says_which_bron_it_went_against(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        bron = self._bron(customer)
        self._sell(admin_client, customer, lot.brand, "2000")
        assert Sale.objects.get().reservation_id == bron.pk

    def test_a_bron_served_in_full_closes(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        bron = self._bron(customer, kg="2000")
        self._sell(admin_client, customer, lot.brand, "2000")
        bron.refresh_from_db()
        assert bron.remaining_kg == Decimal("0.000")
        assert bron.status == Reservation.Status.CONVERTED

    def test_somebody_elses_bron_is_not_touched(self, admin_client, db):
        """A bron is a promise to ONE mijoz. Serving a different one does not
        settle it, and must not quietly hand their granula away."""
        lot = _arrived_lot(kg="10000")
        holder, walk_in = _customer("Bron egasi"), _customer("Boshqa mijoz")
        bron = self._bron(holder, kg="6000")

        self._sell(admin_client, walk_in, lot.brand, "1000")
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0.000")

    def test_the_holders_own_bron_does_not_block_them(self, admin_client, db):
        """The freeze. On-hand 4 000, bronned 6 000 for this mijoz — the marka used
        to be unsellable even to the person it was being held for."""
        lot = _arrived_lot(kg="4000")
        customer = _customer()
        self._bron(customer, kg="6000")

        self._sell(admin_client, customer, lot.brand, "4000")
        assert Sale.objects.count() == 1

    def test_a_walk_in_is_not_blocked_by_that_bron_either(self, admin_client, db):
        """Nor is anybody else. The bron stays open and unsettled — it was promised
        to the holder, and selling elsewhere does not pretend otherwise."""
        lot = _arrived_lot(kg="4000")
        holder, walk_in = _customer("Bron egasi"), _customer("Boshqa mijoz")
        bron = self._bron(holder, kg="6000")

        self._sell(admin_client, walk_in, lot.brand, "4000")
        assert Sale.objects.count() == 1
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0.000")

    def test_deleting_the_sotuv_gives_the_kg_back(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        bron = self._bron(customer, kg="2000")
        self._sell(admin_client, customer, lot.brand, "2000")
        bron.refresh_from_db()
        assert bron.status == Reservation.Status.CONVERTED

        admin_client.post(f"/sales/{Sale.objects.get().pk}/delete/")
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0.000")
        assert bron.status == Reservation.Status.ACTIVE   # the promise is unkept again


# ── Brondan ushlansinmi ──────────────────────────────────────────────────────

def _sell(admin_client, brand, customer, kg="2000", price="1.50", **extra):
    """The Yangi sotuv form as the browser posts it — the box ticked unless a test
    says otherwise, because that is how it renders."""
    data = {"customer": customer.pk, "currency": "usd",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
            "draw_from_bron_asked": "1", "draw_from_bron": "on",
            **line_data({"brand": brand, "kg": kg, "price": price})}
    data.update(extra)
    return admin_client.post("/sales/new/", data)


class TestBronDrawIsAsked:
    """Serving a bron holder normally makes their promise smaller — else the bron
    goes on blocking the shelf for granula they already took. But a sotuv to that
    same mijoz may be something extra they bought, with the booking still standing.
    Only the operator knows which, so the form asks."""

    def test_ticked_draws_the_sotuv_out_of_the_bron(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        customer = _customer()
        _reserve(admin_client, "LLDPE", customer, kg="6000")
        assert _sell(admin_client, "LLDPE", customer, kg="2000").status_code == 302

        bron = Reservation.objects.get()
        assert bron.fulfilled_kg == Decimal("2000.000")
        assert bron.remaining_kg == Decimal("4000.000")
        assert Sale.objects.get().reservation_id == bron.pk

    def test_unticked_leaves_the_bron_whole(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        customer = _customer()
        _reserve(admin_client, "LLDPE", customer, kg="6000")
        resp = _sell(admin_client, "LLDPE", customer, kg="2000", draw_from_bron="")
        assert resp.status_code == 302

        bron = Reservation.objects.get()
        assert bron.fulfilled_kg == Decimal("0.000")
        assert bron.remaining_kg == Decimal("6000.000")
        assert Sale.objects.get().reservation_id is None

    def test_a_post_that_never_saw_the_box_still_draws(self, admin_client, db):
        """An unticked checkbox posts nothing, which is byte-for-byte a POST that
        never carried the field. Those must not mean the same thing: a caller that
        predates the box keeps the behaviour it always had."""
        _arrived_lot(kg="10000", brand="LLDPE")
        customer = _customer()
        _reserve(admin_client, "LLDPE", customer, kg="6000")
        resp = admin_client.post("/sales/new/", {
            "customer": customer.pk, "currency": "usd", "date": "2026-07-20",
            "debt_deadline": "", "note": "",          # no box, no twin
            **line_data({"brand": "LLDPE", "kg": "2000", "price": "1.50"})})
        assert resp.status_code == 302
        assert Reservation.objects.get().fulfilled_kg == Decimal("2000.000")

    def test_an_edit_does_not_turn_a_plain_sotuv_into_a_bron_one(self, admin_client, db):
        """Whether a sotuv came out of a bron is decided when it is made. Re-drawing
        on every edit would flip that the first time anybody fixed a kg."""
        lot = _arrived_lot(kg="10000", brand="LLDPE")
        customer = _customer()
        _reserve(admin_client, "LLDPE", customer, kg="6000")
        _sell(admin_client, "LLDPE", customer, kg="2000", draw_from_bron="")
        sale = Sale.objects.get()

        admin_client.post(f"/sales/{sale.pk}/edit/", {
            "customer": customer.pk, "line": lot.pk, "kg": "2500",
            "currency": "usd", "price": "1.50", "date": "2026-07-20",
            "debt_deadline": "", "note": ""})
        sale.refresh_from_db()
        assert sale.kg == Decimal("2500.000")
        assert sale.reservation_id is None
        assert Reservation.objects.get().fulfilled_kg == Decimal("0.000")


# ── Bronni tugatish ──────────────────────────────────────────────────────────

class TestClosingABron:
    """The mijoz took what they took and wants no more. Not the same act as
    cancelling, which is a bron that never happened."""

    def test_closing_frees_the_rest_of_the_shelf(self, admin_client, db):
        from crm.models import brand_on_hand_kg, brand_reserved_kg

        _arrived_lot(kg="10000", brand="LLDPE")
        customer = _customer()
        _reserve(admin_client, "LLDPE", customer, kg="6000")
        _sell(admin_client, "LLDPE", customer, kg="2000")     # takes 2 000 of it
        assert brand_reserved_kg("LLDPE") == Decimal("4000.000")

        resp = admin_client.post(
            f"/reservations/{Reservation.objects.get().pk}/close/", {})
        assert resp.status_code == 302

        bron = Reservation.objects.get()
        assert bron.status == Reservation.Status.CLOSED
        assert bron.fulfilled_kg == Decimal("2000.000")   # what was served stands
        assert bron.is_open is False
        # and the 4 000 it was holding is on the shelf for anybody
        assert brand_reserved_kg("LLDPE") == Decimal("0")
        # Asked of what is physically there, because that IS the shelf now. The
        # figure this line used to read — brand_free_kg, on-hand minus bronned —
        # was retired along with the rule behind it: a bron no longer holds kg back
        # from another mijoz (see brand_on_hand_kg), so "free" and "on hand" became
        # the same number and only one of them was worth keeping.
        assert brand_on_hand_kg("LLDPE") == Decimal("8000.000")

    def test_closed_is_its_own_status_not_cancelled(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="6000")
        admin_client.post(f"/reservations/{Reservation.objects.get().pk}/close/", {})
        assert Reservation.objects.get().get_status_display() == "Tugatildi"

    def test_a_closed_bron_cannot_be_closed_again(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="6000")
        pk = Reservation.objects.get().pk
        admin_client.post(f"/reservations/{pk}/close/", {})
        admin_client.post(f"/reservations/{pk}/close/", {})
        assert Reservation.objects.get().status == Reservation.Status.CLOSED

    def test_the_list_can_be_filtered_to_closed_brons(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("A"), kg="2000")
        _reserve(admin_client, "LLDPE", _customer("B"), kg="3000")
        admin_client.post(
            f"/reservations/{Reservation.objects.order_by('pk').first().pk}/close/", {})
        rows = admin_client.get("/reservations/?status=closed").context["rows"]
        assert [r.customer.name for r in rows] == ["A"]


def test_the_bron_row_offers_a_full_sotuv_prefilled(admin_client, db):
    """Berish is the quick hand-over; this is for the sotuv that needs a sana or a
    to'lov muddati, without retyping who and what."""
    _arrived_lot(kg="10000", brand="LLDPE")
    customer = _customer()
    _reserve(admin_client, "LLDPE", customer, kg="6000")
    html = admin_client.get("/reservations/").content.decode()
    assert f"/sales/new/?customer={customer.pk}&amp;brand=LLDPE" in html


def test_the_bron_row_links_the_name_to_that_mijoz_page(admin_client, db):
    _arrived_lot(kg="10000", brand="LLDPE")
    customer = _customer()
    _reserve(admin_client, "LLDPE", customer, kg="6000")
    html = admin_client.get("/reservations/").content.decode()
    # The ism is in its own `data-lotin` span — the operator's text, which the
    # transliterator leaves alone (see test_yozuv) — so the link wraps the span.
    assert (f'href="/debts/{customer.pk}/"><span data-lotin>{customer.name}</span></a>'
            in html)
