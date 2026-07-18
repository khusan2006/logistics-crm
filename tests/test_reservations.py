from decimal import Decimal

from crm.models import (
    Contract, Customer, CustomerPayment, Partner, PaymentAllocation, Reservation, Sale, Shipment,
    ShipmentStatus,
)


def _customer(name="Alisher Mebel"):
    return Customer.objects.create(name=name, phone="1", address="Toshkent")


def _arrived_lot(kg="10000", brand="LLDPE", contract_price="1.00"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(
        partner=partner, brand=brand, kg=Decimal(kg), price=Decimal(contract_price),
        created="2026-07-01", deadline="2026-08-01",
    )
    return Shipment.objects.create(
        contract=contract, kg=Decimal(kg), status=ShipmentStatus.arrival(),
        sent="2026-07-05", eta="2026-07-15", arrived="2026-07-16",
        transport="01A111AA", container="MSCU-1",
    )


def _in_transit_lot(kg="5000", brand="HDPE"):
    partner = Partner.objects.create(name="Iran Co", phone="1", city="T")
    contract = Contract.objects.create(
        partner=partner, brand=brand, kg=Decimal(kg), price=Decimal("1.00"),
        created="2026-07-01", deadline="2026-08-01",
    )
    return Shipment.objects.create(
        contract=contract, kg=Decimal(kg), status=ShipmentStatus.objects.exclude(is_arrival=True).first(),
        sent="2026-07-05", eta="2026-08-01",
    )


def _reserve(admin_client, lot, customer, kg="5000", price=""):
    return admin_client.post(f"/reservations/new/?lot={lot.pk}", {
        "customer": customer.pk, "shipment": lot.pk, "kg": kg, "price": price, "note": "",
    })


class TestReservationBlocksKg:
    def test_active_reservation_reduces_available_kg(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        resp = _reserve(admin_client, lot, customer, kg="5000")
        assert resp.status_code == 302
        lot.refresh_from_db()
        assert lot.reserved_kg == Decimal("5000")
        assert lot.available_kg == Decimal("5000")

    def test_over_reserving_rejected(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        _reserve(admin_client, lot, customer, kg="7000")
        resp = _reserve(admin_client, lot, customer, kg="4000")
        assert resp.status_code == 200  # form re-rendered, invalid
        assert Reservation.objects.filter(kg=Decimal("4000")).count() == 0
        lot.refresh_from_db()
        assert lot.reserved_kg == Decimal("7000")


class TestInTransitReservation:
    def test_reservation_on_in_transit_lot_allowed(self, admin_client, db):
        lot = _in_transit_lot(kg="5000")
        customer = _customer()
        resp = _reserve(admin_client, lot, customer, kg="2000")
        assert resp.status_code == 302
        lot.refresh_from_db()
        assert lot.reserved_kg == Decimal("2000")
        assert lot.is_lot is False

    def test_over_reserving_in_transit_rejected(self, admin_client, db):
        lot = _in_transit_lot(kg="5000")
        customer = _customer()
        _reserve(admin_client, lot, customer, kg="4000")
        resp = _reserve(admin_client, lot, customer, kg="2000")
        assert resp.status_code == 200
        lot.refresh_from_db()
        assert lot.reserved_kg == Decimal("4000")

    def test_converting_in_transit_reservation_rejected(self, admin_client, db):
        lot = _in_transit_lot(kg="5000")
        customer = _customer()
        _reserve(admin_client, lot, customer, kg="2000", price="1.50")
        r = Reservation.objects.get()
        resp = admin_client.post(f"/reservations/{r.pk}/convert/", {})
        assert resp.status_code in (302, 200)
        r.refresh_from_db()
        assert r.status == "active"
        assert not Sale.objects.exists()


class TestEarmarkedPayment:
    def test_earmarked_payment_applies_first_on_convert(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        _reserve(admin_client, lot, customer, kg="5000", price="1.50")
        r = Reservation.objects.get()
        payment = CustomerPayment.objects.create(
            customer=customer, date="2026-07-17", amount=Decimal("2000.00"),
            reservation=r,
        )
        resp = admin_client.post(f"/reservations/{r.pk}/convert/", {"price": "1.50"})
        assert resp.status_code == 302
        sale = Sale.objects.get(reservation=r)
        assert sale.kg == Decimal("5000")
        assert sale.price == Decimal("1.50")
        # earmarked payment fully covers 5000 * 1.50 = 7500? No -> 2000 covers partial
        alloc = PaymentAllocation.objects.get(payment=payment, sale=sale)
        assert alloc.amount == Decimal("2000.00")


class TestConvertToSale:
    def test_convert_creates_sale_and_updates_kg(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        _reserve(admin_client, lot, customer, kg="5000", price="1.50")
        r = Reservation.objects.get()
        # A general advance (not earmarked) sitting on the customer.
        CustomerPayment.objects.create(
            customer=customer, date="2026-07-17", amount=Decimal("3000.00"),
        )
        resp = admin_client.post(f"/reservations/{r.pk}/convert/", {"price": "1.50"})
        assert resp.status_code == 302
        r.refresh_from_db()
        assert r.status == "converted"
        sale = Sale.objects.get(reservation=r)
        assert sale.kg == Decimal("5000")
        assert sale.price == Decimal("1.50")
        assert sale.cost_price == lot.landed_cost_per_kg
        assert sale.customer_id == customer.pk
        assert sale.shipment_id == lot.pk

        lot.refresh_from_db()
        assert lot.reserved_kg == Decimal("0")
        assert lot.sold_kg == Decimal("5000")
        assert lot.available_kg == Decimal("5000")  # net unchanged: 10000-5000-0

        # 5000*1.50 = 7500 total; general advance of 3000 applies (no earmark here)
        assert sale.paid == Decimal("3000.00")

    def test_convert_blocked_for_non_arrived_lot(self, admin_client, db):
        lot = _in_transit_lot(kg="5000")
        customer = _customer()
        _reserve(admin_client, lot, customer, kg="2000", price="1.00")
        r = Reservation.objects.get()
        resp = admin_client.post(f"/reservations/{r.pk}/convert/", {"price": "1.00"})
        r.refresh_from_db()
        assert r.status == "active"
        assert not Sale.objects.exists()


class TestCancel:
    def test_cancel_frees_kg(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        _reserve(admin_client, lot, customer, kg="5000")
        r = Reservation.objects.get()
        resp = admin_client.post(f"/reservations/{r.pk}/cancel/", {})
        assert resp.status_code == 302
        r.refresh_from_db()
        assert r.status == "cancelled"
        lot.refresh_from_db()
        assert lot.reserved_kg == Decimal("0")
        assert lot.available_kg == lot.kg


class TestPermissions:
    def test_translator_forbidden(self, translator_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        assert translator_client.get("/reservations/").status_code == 403
        assert translator_client.get("/reservations/new/").status_code == 403
        assert translator_client.post(f"/reservations/new/?lot={lot.pk}", {
            "customer": customer.pk, "shipment": lot.pk, "kg": "100", "price": "", "note": "",
        }).status_code == 403


class TestReservationList:
    def test_list_shows_reservation(self, admin_client, db):
        lot = _arrived_lot(kg="10000")
        customer = _customer()
        _reserve(admin_client, lot, customer, kg="5000")
        html = admin_client.get("/reservations/").content.decode()
        assert customer.name in html
