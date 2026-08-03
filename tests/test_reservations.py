from decimal import Decimal

from crm.models import (
    Contract, ContractLine, Customer, CustomerPayment, Partner, PaymentAllocation, Reservation, Sale, Shipment, ShipmentLine, ShipmentStatus,
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


def _convert(admin_client, reservation, price=None):
    body = {"price": price} if price else {}
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


class TestFifoQueue:
    def test_first_bron_is_served_first(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        first = _customer("Birinchi")
        second = _customer("Ikkinchi")
        _reserve(admin_client, "LLDPE", first, kg="4000", price="2.00")
        _reserve(admin_client, "LLDPE", second, kg="4000", price="2.00")
        a, b = Reservation.objects.order_by("created_at", "pk")

        # the second cannot jump the queue
        _convert(admin_client, b)
        b.refresh_from_db()
        assert b.fulfilled_kg == Decimal("0")
        assert not Sale.objects.exists()

        _convert(admin_client, a)
        a.refresh_from_db()
        assert a.status == "converted"
        assert Sale.objects.get().customer == first

        # now the second's turn
        _convert(admin_client, b)
        b.refresh_from_db()
        assert b.status == "converted"

    def test_queue_is_per_marka(self, admin_client, db):
        """An older bron for a DIFFERENT marka blocks nothing."""
        _arrived_lot(kg="10000", brand="LLDPE")
        _arrived_lot_for("Ikkinchi", kg="10000", brand="HDPE")
        _reserve(admin_client, "LLDPE", _customer("Bir"), kg="1000", price="2.00")
        _reserve(admin_client, "HDPE", _customer("Ikki"), kg="1000", price="2.00")
        hdpe = Reservation.objects.get(brand="HDPE")
        _convert(admin_client, hdpe)
        hdpe.refresh_from_db()
        assert hdpe.status == "converted"

    def test_cancelling_the_head_lets_the_next_through(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bir"), kg="1000", price="2.00")
        _reserve(admin_client, "LLDPE", _customer("Ikki"), kg="1000", price="2.00")
        a, b = Reservation.objects.order_by("created_at", "pk")
        admin_client.post(f"/reservations/{a.pk}/cancel/", {})
        _convert(admin_client, b)
        b.refresh_from_db()
        assert b.status == "converted"

    def test_list_reports_position_and_who_is_ahead(self, admin_client, db):
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Birinchi"), kg="1000")
        _reserve(admin_client, "LLDPE", _customer("Ikkinchi"), kg="1000")
        rows = {r.customer.name: r for r in admin_client.get("/reservations/").context["page"]}
        assert rows["Birinchi"].queue_pos == 1
        assert rows["Ikkinchi"].queue_pos == 2
        assert rows["Ikkinchi"].blocked_by.customer.name == "Birinchi"
        assert rows["Ikkinchi"].servable_kg == Decimal("0")


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

    def test_a_part_filled_bron_only_blocks_what_is_still_owed(self, admin_client, db):
        _arrived_lot(kg="12000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000", price="2.00")
        _convert(admin_client, Reservation.objects.get())
        _arrived_lot_for("Keyingi", kg="30000", brand="LLDPE")
        from crm.models import brand_free_kg, brand_reserved_kg
        assert brand_reserved_kg("LLDPE") == Decimal("8000.000")
        assert brand_free_kg("LLDPE") == Decimal("22000.000")   # 30000 − 8000

    def test_nothing_on_the_shelf_is_refused_not_half_done(self, admin_client, db):
        _in_transit_lot(kg="5000", brand="HDPE")
        _reserve(admin_client, "HDPE", _customer(), kg="2000", price="2.00")
        bron = Reservation.objects.get()
        _convert(admin_client, bron)
        bron.refresh_from_db()
        assert bron.fulfilled_kg == Decimal("0")
        assert not Sale.objects.exists()


class TestBronsBlockOrdinarySales:
    def test_bronned_kg_cannot_be_sold_over_the_counter(self, admin_client, db):
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": _customer("Kelgan mijoz").pk, "brand": "LLDPE", "kg": "5000",
            "currency": "usd", "price": "2.00", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
        })
        assert resp.status_code == 200          # re-rendered, invalid
        assert not Sale.objects.exists()
        assert "bronlangan" in resp.content.decode()

    def test_free_kg_still_sells(self, admin_client, db):
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        resp = admin_client.post("/sales/new/", {
            "customer": _customer("Kelgan mijoz").pk, "brand": "LLDPE", "kg": "4000",
            "currency": "usd", "price": "2.00", "exchange_rate": "12000",
            "date": "2026-07-20", "debt_deadline": "", "note": "",
        })
        assert resp.status_code == 302
        assert Sale.objects.get().kg == Decimal("4000.000")

    def test_ombor_says_where_the_kg_went(self, admin_client, db):
        """The user's ask: nobody should have to work out why Sotish mumkin is
        smaller than Kirim."""
        _arrived_lot(kg="24000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer("Bron egasi"), kg="20000")
        ctx = admin_client.get("/ombor/").context
        g = next(x for x in ctx["page"] if x["brand"] == "LLDPE")
        assert g["on_hand"] == Decimal("24000.000")
        assert g["reserved"] == Decimal("20000.000")
        assert g["available"] == Decimal("4000.000")
        html = admin_client.get("/ombor/").content.decode()
        assert "Bronlangan" in html and "Sotish mumkin" in html
        assert "Bron egasi" in html

    def test_ombor_flags_a_shortfall(self, admin_client, db):
        """Bronned more than has landed — legitimate, and worth saying out loud."""
        _arrived_lot(kg="5000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="20000")
        g = next(x for x in admin_client.get("/ombor/").context["page"]
                 if x["brand"] == "LLDPE")
        assert g["available"] == Decimal("0")
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
    def test_cancel_frees_the_kg_for_ordinary_sales(self, admin_client, db):
        from crm.models import brand_free_kg
        _arrived_lot(kg="10000", brand="LLDPE")
        _reserve(admin_client, "LLDPE", _customer(), kg="5000")
        assert brand_free_kg("LLDPE") == Decimal("5000.000")
        admin_client.post(f"/reservations/{Reservation.objects.get().pk}/cancel/", {})
        assert brand_free_kg("LLDPE") == Decimal("10000.000")


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
