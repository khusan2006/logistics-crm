from decimal import Decimal

from crm.models import (
    Contract, ContractLine, Customer, CustomerPayment, Partner, PaymentAllocation, Reservation, Sale, Shipment, ShipmentLine, ShipmentStatus,
    brand_free_kg,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


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
    """Hand kg over from a bron. Without `kg` the view gives everything it can —
    the whole remainder, or whatever of it has landed."""
    body = {}
    if price:
        body["price"] = price
    if kg is not None:
        body["kg"] = kg
    return admin_client.post(f"/reservations/{reservation.pk}/convert/", body)


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
        rows = {r.customer.name: r for r in admin_client.get("/reservations/").context["page"]}
        assert rows["Bir"].queue_pos == 1
        assert rows["Ikki"].queue_pos == 1       # first of its own marka

    def test_list_reports_position_and_who_asked_first(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Birinchi"), kg="1000")
        _reserve(admin_client, "LLDPE", _customer("Ikkinchi"), kg="1000")
        rows = {r.customer.name: r for r in admin_client.get("/reservations/").context["page"]}
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
        rows = {r.customer.name: r for r in admin_client.get("/reservations/").context["page"]}
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

    def test_more_than_is_owed_is_refused(self, admin_client, db):
        _arrived_lot(kg="20000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron, kg="6000")
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0")
        assert not Sale.objects.exists()

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


class TestBronsBlockOrdinarySales:
    """A bron holds the granula for the mijoz who booked it. Somebody else walking
    in cannot be sold out from under them — the shelf is not the ceiling, the FREE
    part of it is."""

    def test_bronned_kg_cannot_be_sold_to_somebody_else(self, admin_client, db):
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": _customer("Kelgan mijoz").pk, "brand": "LLDPE", "kg": "24000",
            "currency": "usd", "price": "2.00", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
        })
        assert resp.status_code == 200          # re-rendered, invalid
        assert not Sale.objects.exists()

    def test_the_free_part_of_the_shelf_still_sells(self, admin_client, db):
        """4 000 of the 24 000 is unpromised, and that much goes through."""
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": _customer("Kelgan mijoz").pk, "brand": "LLDPE", "kg": "4000",
            "currency": "usd", "price": "2.00", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
        })
        assert resp.status_code == 302
        assert Sale.objects.get().kg == Decimal("4000.000")

    def test_the_bron_holder_may_buy_their_own_bronned_granula(self, admin_client, db):
        """Their promise does not stand between them and what it promises them."""
        _arrived_lot(kg="24000", brand="LLDPE")
        holder = _customer("Bron egasi")
        _reserve(admin_client, "LLDPE", holder, kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": holder.pk, "brand": "LLDPE", "kg": "24000",
            "currency": "usd", "price": "2.00", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
        })
        assert resp.status_code == 302
        assert Sale.objects.get().kg == Decimal("24000.000")

    def test_the_shelf_is_still_the_ceiling(self, admin_client, db):
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": _customer("Kelgan mijoz").pk, "brand": "LLDPE", "kg": "24001",
            "currency": "usd", "price": "2.00", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
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
        ready = admin_client.get("/reservations/?lot=ready").context["page"]
        waiting = admin_client.get("/reservations/?lot=waiting").context["page"]
        assert [r.customer.name for r in ready] == ["Tayyor"]
        assert [r.customer.name for r in waiting] == ["Kutmoqda"]

    def test_search_by_customer_and_marka(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "LLDPE", _customer("Alisher Mebel"), kg="2000")
        _reserve(admin_client, "HDPE", _customer("Bobur Plast"), kg="1000")
        for query, expected in [("Alisher", 1), ("HDPE", 1), ("zzz", 0)]:
            ctx = admin_client.get("/reservations/", {"q": query}).context
            assert len(ctx["page"].object_list) == expected, query

    def test_status_counts_are_faceted(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("A"), kg="2000")
        _reserve(admin_client, "LLDPE", _customer("B"), kg="3000")
        admin_client.post(
            f"/reservations/{Reservation.objects.order_by('pk').first().pk}/cancel/", {})
        tabs = {t["key"]: t["count"]
                for t in admin_client.get("/reservations/").context["status_tabs"]}
        assert tabs == {"active": 1, "converted": 0, "cancelled": 1, "": 2}


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
    an ordinary sotuv to the bron's OWN holder left the promise at its full kg. And
    since a bron blocks the shelf, bron #1 in the real book (74 400 kg of 2102
    campaund against 41 640 kg on hand) froze the marka for everybody while its
    holder bought 24 000 kg of it over the counter."""

    def _bron(self, customer, brand="LLDPE", kg="6000"):
        return Reservation.objects.create(
            customer=customer, brand=brand, kg=Decimal(kg), price=Decimal("1.50"))

    def _sell(self, client, customer, brand, kg):
        return client.post("/sales/new/", {
            "customer": customer.pk, "brand": brand, "kg": kg,
            "currency": "usd", "price": "1.50", "exchange_rate": "12000",
            "date": "2026-07-20"})

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
        """The freeze. On-hand 4 000, bronned 6 000 for this mijoz — shelf-wide free
        is 0, so the marka used to be unsellable even to the person it was being
        held for."""
        lot = _arrived_lot(kg="4000")
        customer = _customer()
        self._bron(customer, kg="6000")

        assert brand_free_kg(lot.brand) == Decimal("0")
        assert brand_free_kg(lot.brand, customer) == Decimal("4000.000")
        self._sell(admin_client, customer, lot.brand, "4000")
        assert Sale.objects.count() == 1

    def test_a_walk_in_is_still_blocked_by_that_bron(self, admin_client, db):
        lot = _arrived_lot(kg="4000")
        holder, walk_in = _customer("Bron egasi"), _customer("Boshqa mijoz")
        self._bron(holder, kg="6000")

        self._sell(admin_client, walk_in, lot.brand, "4000")
        assert Sale.objects.count() == 0                  # rejected, all of it promised

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
