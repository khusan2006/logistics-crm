"""QA audit — auth, roles, permissions, audit trail, users.

The product owner reports figures that move "by themselves". From this area that
symptom has exactly two shapes:

  * a money-mutating endpoint that a NON-ADMIN can reach, so the ledger changes
    under an admin who never touched it; and
  * an audit trail that cannot tell you who did it, or what was actually typed.

So the probes here are (1) an exhaustive sweep of every write URL in
config/urls.py against a translator client, GET and POST; (2) the money impact of
the one write a translator IS trusted with (shipment status); (3) audit-trail
integrity — actor, amount, no row on an invalid submission, append-only; and (4)
the four standing probe families — round-trip conversion, no drift on re-save,
currency stickiness, aggregate consistency — applied to this area.

TRIAGE STATE: every xfail below has been checked against the source and the build
plan and reworded to name file:line and the concrete wrong figure. Two of the
original six claims were WITHDRAWN as wrong expectations and rewritten to assert
the documented behaviour instead (translator reversing an arrival; the audit
summary printing so'm) — each carries a CLAIM WITHDRAWN comment saying what the
claim got wrong. One new defect surfaced while triaging the first of those.

Diagnosis pass only: nothing outside tests/audit/ is touched.
"""

import re
from datetime import date
from decimal import Decimal

import pytest
from django.contrib import admin as django_admin
from django.urls import get_resolver, reverse
from django.test import Client

from accounts.models import User
from crm.models import (
    AuditLog, Currency, Customer, CustomerPayment, Logist, LogistPayment, Partner,
    Reservation, Return, Sale, ShipmentExpense, ShipmentLeg, ShipmentStatus,
    SupplierPayment, fifo_lots, stock_value, transit_value,
)

from conftest import PASSWORD, make_contract, make_shipment


# ---------------------------------------------------------------------------
# world: one row of every kind, so a pk-bearing URL can actually be reached
# ---------------------------------------------------------------------------

@pytest.fixture
def world(db, admin_user):
    partner = Partner.objects.create(name="Pars", phone="+998900000000", city="Tehron")
    customer = Customer.objects.create(name="Mijoz A", phone="+998901111111")
    contract = make_contract(partner=partner, brand="LLDPE", kg="1000", price="1.00")
    supplier_payment = SupplierPayment.objects.create(
        contract=contract, amount=Decimal("500"), exchange_rate=Decimal("12000"),
        created_by=admin_user)
    shipment = make_shipment(contract=contract, kg="400", price="1.20")
    lot = shipment.lines.first()
    shipment.arrived = date(2026, 3, 1)
    shipment.status = ShipmentStatus.arrival()
    shipment.save(update_fields=["arrived", "status"])
    leg = ShipmentLeg.objects.create(shipment=shipment, from_location="Tehron",
                                     to_location="Toshkent", created_by=admin_user)
    expense = ShipmentExpense.objects.create(
        shipment=shipment, amount=Decimal("50"), exchange_rate=Decimal("12000"),
        created_by=admin_user)
    logist = Logist.objects.create(name="Logist B")
    logist_payment = LogistPayment.objects.create(
        logist=logist, amount=Decimal("100"), exchange_rate=Decimal("12000"),
        created_by=admin_user)
    sale = Sale.objects.create(
        customer=customer, line=lot, kg=Decimal("100"), price=Decimal("1.5000"),
        exchange_rate=Decimal("12000"), created_by=admin_user)
    ret = Return.objects.create(sale=sale, kg=Decimal("10"), price=sale.price,
                                exchange_rate=Decimal("12000"), created_by=admin_user)
    reservation = Reservation.objects.create(
        customer=customer, brand="LLDPE", kg=Decimal("50"), created_by=admin_user)
    customer_payment = CustomerPayment.objects.create(
        customer=customer, amount=Decimal("200"), exchange_rate=Decimal("12000"),
        created_by=admin_user)
    return {
        "partner": partner, "customer": customer, "contract": contract,
        "supplier_payment": supplier_payment, "shipment": shipment, "lot": lot,
        "leg": leg, "expense": expense, "logist": logist,
        "logist_payment": logist_payment, "sale": sale, "return": ret,
        "reservation": reservation, "customer_payment": customer_payment,
        "status": ShipmentStatus.objects.exclude(is_arrival=True).first(),
        "user": admin_user,
    }


# Every URL in config/urls.py that WRITES, with the object its pk comes from.
# Kept as a literal table (not derived from the resolver) so a route added to
# urls.py without a decorator shows up as a hole here rather than silently
# inheriting whatever the loop assumed.
ADMIN_ONLY_WRITE_ROUTES = [
    ("partner_create", None), ("partner_edit", "partner"), ("partner_delete", "partner"),
    ("customer_create", None), ("customer_edit", "customer"), ("customer_delete", "customer"),
    ("customer_quick_create", None),
    ("contract_create", None), ("contract_edit", "contract"), ("contract_delete", "contract"),
    ("supplier_payment_create", None), ("supplier_payment_edit", "supplier_payment"),
    ("supplier_payment_delete", "supplier_payment"),
    ("status_create", None), ("status_edit", "status"), ("status_delete", "status"),
    ("status_move", "status"),
    ("logist_create", None), ("logist_edit", "logist"), ("logist_delete", "logist"),
    ("logist_payment_create", None), ("logist_payment_edit", "logist_payment"),
    ("logist_payment_delete", "logist_payment"),
    ("shipment_create", None), ("shipment_edit", "shipment"), ("shipment_delete", "shipment"),
    ("expense_create", None), ("expense_edit", "expense"), ("expense_delete", "expense"),
    ("sale_create", None), ("sale_edit", "sale"), ("sale_delete", "sale"),
    ("reservation_create", None), ("reservation_edit", "reservation"),
    ("reservation_delete", "reservation"), ("reservation_cancel", "reservation"),
    ("reservation_convert", "reservation"),
    ("return_create", None), ("return_delete", "return"),
    ("customer_payment_create", None), ("customer_payment_edit", "customer_payment"),
    ("customer_payment_delete", "customer_payment"),
    ("user_create", None), ("user_edit", "user"),
]

# Admin-only screens that only READ, but read money. A translator seeing the
# kassa is a leak, not a miscalculation — swept anyway, same decorator.
ADMIN_ONLY_READ_ROUTES = [
    ("audit_list", None), ("kassa", None), ("reports", None), ("ombor", None),
    ("debt_list", None), ("debt_customer", "customer"), ("partner_list", None),
    ("customer_list", None), ("contract_list", None), ("supplier_payment_list", None),
    ("customer_payment_list", None), ("sale_list", None), ("sale_detail", "sale"),
    ("reservation_list", None), ("status_list", None), ("logist_list", None),
    ("logist_detail", "logist"), ("user_list", None),
    ("export_contracts", None), ("export_supplier_payments", None),
    ("export_shipments", None), ("export_sales", None), ("export_debts", None),
]


def _url(route, key, world):
    return reverse(route, args=[world[key].pk] if key else [])


def _money_snapshot():
    """Every stored money figure in the database, keyed by row. Any difference
    between two snapshots is a figure that moved."""
    snap = {}
    for model, fields in (
            (SupplierPayment, ("amount", "amount_uzs")),
            (CustomerPayment, ("amount", "amount_uzs")),
            (LogistPayment, ("amount", "amount_uzs")),
            (ShipmentExpense, ("amount", "amount_uzs")),
            (Sale, ("price", "price_uzs")),
            (Return, ("price", "price_uzs")),
            (Reservation, ("price", "price_uzs")),
    ):
        for row in model.objects.all():
            snap[(model.__name__, row.pk)] = tuple(
                [getattr(row, f) for f in fields] + [row.currency, row.exchange_rate])
    snap["stock_value"] = stock_value()
    snap["transit_value"] = transit_value()
    return snap


# ---------------------------------------------------------------------------
# (1) role enforcement sweep
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route,key", ADMIN_ONLY_WRITE_ROUTES,
                         ids=[r for r, _ in ADMIN_ONLY_WRITE_ROUTES])
def test_translator_post_refused_on_every_admin_write_url(translator_client, world, route, key):
    """A single unguarded POST is how a figure moves under an admin who never
    touched it. Every admin-only write must 403 a translator."""
    resp = translator_client.post(_url(route, key, world), {})
    assert resp.status_code == 403, f"{route} accepted a translator POST ({resp.status_code})"


@pytest.mark.parametrize("route,key", ADMIN_ONLY_WRITE_ROUTES,
                         ids=[r for r, _ in ADMIN_ONLY_WRITE_ROUTES])
def test_translator_get_refused_on_every_admin_write_url(translator_client, world, route, key):
    """The GET side renders the edit form / confirm modal, which leaks the money it
    is about. 405 is also a refusal — the POST-only routes are decorated
    @require_POST OUTSIDE @role_required, so a GET never reaches the role check."""
    resp = translator_client.get(_url(route, key, world))
    assert resp.status_code in (403, 405), f"{route} served a translator GET ({resp.status_code})"


@pytest.mark.parametrize("route,key", ADMIN_ONLY_READ_ROUTES,
                         ids=[r for r, _ in ADMIN_ONLY_READ_ROUTES])
def test_translator_refused_on_every_admin_money_screen(translator_client, world, route, key):
    resp = translator_client.get(_url(route, key, world))
    assert resp.status_code == 403, f"{route} served a translator ({resp.status_code})"


@pytest.mark.parametrize("route,key", ADMIN_ONLY_WRITE_ROUTES,
                         ids=[r for r, _ in ADMIN_ONLY_WRITE_ROUTES])
def test_anonymous_write_is_bounced_to_login_and_writes_nothing(client, world, route, key):
    before = _money_snapshot()
    audit_before = AuditLog.objects.count()
    resp = client.post(_url(route, key, world), {})
    assert resp.status_code == 302 and "/login/" in resp["Location"]
    assert _money_snapshot() == before
    assert AuditLog.objects.count() == audit_before


def _declared_roles(view):
    """The roles a view's @role_required was given, or None if it has none.
    role_required closes over `roles`; @wraps leaves a __wrapped__ chain to walk
    through any outer decorator (@require_POST)."""
    seen, fn = set(), view
    while fn is not None and id(fn) not in seen:
        seen.add(id(fn))
        for cell in (fn.__closure__ or ()):
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if (isinstance(value, tuple) and value
                    and all(isinstance(v, str) and v in User.Role.values for v in value)):
                return set(value)
        fn = getattr(fn, "__wrapped__", None)
    return None


# login/logout are @login_not_required by design; the dashboard is deliberately
# open to both roles and redirects a translator to the loads list itself.
UNDECORATED_BY_DESIGN = {"login", "logout", "dashboard"}


def test_every_routed_view_declares_its_roles():
    """A dropped @role_required is invisible until somebody exercises the route.
    Assert it structurally instead, over whatever urls.py currently holds."""
    missing = []
    for pattern in get_resolver().url_patterns:
        name = getattr(pattern, "name", None)
        if name is None or name in UNDECORATED_BY_DESIGN:
            continue
        callback = getattr(pattern, "callback", None)
        if callback is None or getattr(callback, "admin_site", None) is not None:
            continue
        if _declared_roles(callback) is None:
            missing.append(name)
    assert not missing, f"routes with no @role_required: {missing}"


def test_role_required_refuses_a_user_whose_role_is_not_a_known_role(client, admin_user):
    """role_required is a membership test, so anything unrecognised must fail
    closed — a blank role from a bad import must not read as admin."""
    admin_user.role = ""
    admin_user.save(update_fields=["role"])
    client.force_login(admin_user)
    assert client.get(reverse("kassa")).status_code == 403


def test_money_writes_require_a_csrf_token(world, admin_user):
    """CSRF is the other way a figure moves without the operator: an admin's live
    session posting a form somebody else built."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user)
    resp = client.post(reverse("supplier_payment_create"), {
        "contract": world["contract"].pk, "date": "2026-07-20", "currency": "usd",
        "amount": "999", "exchange_rate": "12000", "method": "cash",
    })
    assert resp.status_code == 403
    assert not SupplierPayment.objects.filter(amount=Decimal("999")).exists()


# ---------------------------------------------------------------------------
# (2) the write a translator IS trusted with — shipment status
# ---------------------------------------------------------------------------

def test_translator_may_move_a_non_arrival_status_and_no_money_moves(
        translator_client, world):
    """Status/ETA/legs are the translator's job and carry no money (see the
    ShipmentLeg docstring). Confirm the trusted writes really are inert."""
    shipment = make_shipment(kg="200", price="1.00")
    before = _money_snapshot()
    target = ShipmentStatus.objects.exclude(is_arrival=True).exclude(
        pk=shipment.status_id).first()
    resp = translator_client.post(reverse("shipment_set_status", args=[shipment.pk]),
                                  {"status": target.pk})
    assert resp.status_code == 302
    translator_client.post(reverse("leg_create"), {
        "shipment": shipment.pk, "from_location": "A", "to_location": "B"})
    assert _money_snapshot() == before


def test_translator_cannot_flip_a_load_to_arrived(translator_client, world):
    """Arrival is the moment goods become sellable stock, so it is admin-only."""
    shipment = make_shipment(kg="200", price="1.00")
    resp = translator_client.post(reverse("shipment_set_status", args=[shipment.pk]),
                                  {"status": ShipmentStatus.arrival().pk})
    assert resp.status_code == 403


# CLAIM WITHDRAWN. The original tester asserted 403 and called the asymmetric guard
# in shipment_set_status a bug. It is the documented rule, not an oversight:
# docs/superpowers/plans/2026-07-18-granulalog-phase-1.md:1176 spells the endpoint out
# as "translator may set any non-arrival status; only admin sets the arrival status;
# entering arrival stamps `arrived`, leaving clears it", and the ShipmentLeg docstring
# (crm/models.py:1836) repeats that status is the translator's job. A move BACK IS a
# move to a non-arrival status, so the translator is inside their remit and the lot
# leaving the ombor is the documented consequence of `leaving clears it`.
# What IS a hole is narrower and is tested on its own below: the move-back is allowed
# even when the lot already has sotuvlar booked against it.
def test_translator_may_move_an_arrived_load_back_to_a_non_arrival_status(
        translator_client, world):
    """Documented rule: any non-arrival status is the translator's to set, including
    on a load that has already arrived. Leaving arrival clears `arrived`, so the lot
    leaves the ombor and its goods go back to being in transit."""
    shipment = world["shipment"]
    back = ShipmentStatus.objects.exclude(is_arrival=True).first()
    resp = translator_client.post(
        reverse("shipment_set_status", args=[shipment.pk]), {"status": back.pk})
    assert resp.status_code == 302
    shipment.refresh_from_db()
    assert shipment.status == back and shipment.arrived is None
    # the documented ledger consequence: off the shelf, back on the road
    assert stock_value()[2] == Decimal("0")
    assert transit_value()[2] == shipment.kg


@pytest.mark.xfail(reason="CLAIM UPHELD — crm/views.py:1208. "
                          "`shipment.arrived = (shipment.arrived or localdate()) if "
                          "status.is_arrival else None` writes None on the way OUT of "
                          "arrival, so the `or` that was put there precisely to avoid "
                          "restamping has nothing left to preserve: a status move-back "
                          "and forward turns a load that arrived 2026-03-01 into one "
                          "that arrived today. The real-world date is destroyed, not "
                          "merely hidden, and nothing in the app can recover it.",
                   strict=False)
def test_status_round_trip_keeps_the_original_arrival_date(admin_client, world):
    shipment = world["shipment"]
    original = shipment.arrived
    assert original == date(2026, 3, 1)
    back = ShipmentStatus.objects.exclude(is_arrival=True).first()
    admin_client.post(reverse("shipment_set_status", args=[shipment.pk]), {"status": back.pk})
    admin_client.post(reverse("shipment_set_status", args=[shipment.pk]),
                      {"status": ShipmentStatus.arrival().pk})
    shipment.refresh_from_db()
    assert shipment.arrived == original, (
        f"yetib kelgan sana {original} -> {shipment.arrived}")


@pytest.mark.xfail(reason="CLAIM UPHELD — the money consequence of crm/views.py:1208. "
                          "fifo_lots() (crm/models.py:1275) orders by "
                          "shipment__arrived, so restamping `arrived` to today moves "
                          "the round-tripped lot to the BACK of the queue. Measured on "
                          "this fixture: the head of the LLDPE FIFO queue goes from the "
                          "1.3250$/kg lot to the 2.0000$/kg one, so the very next sotuv "
                          "books a tannarx 51% too high and its foyda is wrong by that "
                          "much — with nothing on any screen saying why.",
                   strict=False)
def test_status_round_trip_does_not_reorder_fifo(admin_client, world):
    older = world["shipment"]                       # arrived 2026-03-01
    newer = make_shipment(kg="300", price="2.00", brand="LLDPE")
    newer.arrived = date(2026, 4, 1)
    newer.status = ShipmentStatus.arrival()
    newer.save(update_fields=["arrived", "status"])
    order_before = [lot.pk for lot in fifo_lots("LLDPE")]
    cost_before = fifo_lots("LLDPE")[0].landed_cost_per_kg
    assert cost_before == Decimal("1.3250")         # 1.20 narx + 50$/400 kg xarajat

    back = ShipmentStatus.objects.exclude(is_arrival=True).first()
    admin_client.post(reverse("shipment_set_status", args=[older.pk]), {"status": back.pk})
    admin_client.post(reverse("shipment_set_status", args=[older.pk]),
                      {"status": ShipmentStatus.arrival().pk})

    after = fifo_lots("LLDPE")
    assert [lot.pk for lot in after] == order_before, (
        f"FIFO head tannarx {cost_before}$/kg -> {after[0].landed_cost_per_kg}$/kg")


@pytest.mark.xfail(reason="CLAIM NEW (found while triaging the withdrawn "
                          "translator-reverses-arrival claim) — crm/views.py:1199-1209. "
                          "shipment_set_status will move a load OUT of arrival even "
                          "when sotuvlar are already booked against its lots, and a "
                          "translator is allowed to do it. Sale.line is on_delete=PROTECT "
                          "(crm/models.py:1340) exactly so sold goods cannot be removed "
                          "from the ledger, but de-arrival removes them from the ombor "
                          "anyway: the sold kg keep their Sotuv row AND come back into "
                          "transit_value, so the same 90 kg is counted twice. On this "
                          "fixture ombor 410.75$/310 kg -> 0, yo'lda 0 -> 480.00$/400 kg.",
                   strict=False)
def test_de_arriving_a_lot_that_has_sales_does_not_double_count_the_sold_kg(
        translator_client, world):
    """Aggregate invariant, stated so it holds whichever way the hole is closed
    (refuse the move-back, or exclude sold kg from transit): every kg on the truck is
    either still on the road, or on the shelf, or already owned by a mijoz — never two
    of those at once."""
    shipment, sale, ret = world["shipment"], world["sale"], world["return"]
    sold_net = sale.kg - ret.kg                     # 90 kg the mijoz owns
    total_kg = shipment.kg                          # 400 kg on the truck
    assert transit_value()[2] + stock_value()[2] + sold_net == total_kg

    back = ShipmentStatus.objects.exclude(is_arrival=True).first()
    translator_client.post(reverse("shipment_set_status", args=[shipment.pk]),
                           {"status": back.pk})

    assert Sale.objects.filter(pk=sale.pk).exists(), "the sotuv survives the move-back"
    assert transit_value()[2] + stock_value()[2] + sold_net == total_kg, (
        f"yo'lda {transit_value()[2]} kg + ombor {stock_value()[2]} kg + sotilgan "
        f"{sold_net} kg != yukdagi {total_kg} kg")


# ---------------------------------------------------------------------------
# (3) audit trail — actor, amount, no row on failure, append-only
# ---------------------------------------------------------------------------

def test_audit_records_the_actor_and_the_amount_of_a_dollar_payment(admin_client, world, admin_user):
    admin_client.post(reverse("supplier_payment_create"), {
        "contract": world["contract"].pk, "date": "2026-07-20", "currency": "usd",
        "amount": "234.56", "exchange_rate": "12000", "method": "cash",
        "commission_percent": "0", "fee_percent": "0", "note": "",
    })
    payment = SupplierPayment.objects.get(amount=Decimal("234.56"))
    row = AuditLog.objects.filter(target_type="Hamkor to'lovi", target_id=payment.pk).get()
    assert row.user == admin_user
    assert row.action == AuditLog.Action.PAYMENT
    assert "234.56" in row.summary


def test_audit_records_the_translator_who_moved_a_status(translator_client, translator_user):
    shipment = make_shipment(kg="200", price="1.00")
    target = ShipmentStatus.objects.exclude(is_arrival=True).exclude(
        pk=shipment.status_id).first()
    translator_client.post(reverse("shipment_set_status", args=[shipment.pk]),
                           {"status": target.pk})
    row = AuditLog.objects.filter(target_type="Yuk", target_id=shipment.pk).latest("created_at")
    assert row.user == translator_user and row.action == AuditLog.Action.STATUS


# CLAIM WITHDRAWN. The original tester demanded the typed so'm figure in the audit
# summary and called its absence a bug. The journal is USD-normalised by design \u2014
# docs/superpowers/plans/2026-07-18-granulalog-phase-1.md:7 "All money is canonical
# USD; so'm entries are converted at entry with a stored rate" \u2014 and all 49
# AuditLog.record call sites in crm/views.py follow that one convention. Crucially the
# printed figure is not a third derivation that could drift: it is `payment.amount`,
# the exact stored USD column, and the typed so'm side is one lookup away through the
# row's own target_type/target_id. Nothing moves and nothing is lost, so this is a
# reporting wish, not a defect. Asserted below as the convention it is, with the
# no-drift property that actually matters made explicit.
def test_audit_of_a_som_payment_journals_the_stored_usd_side_exactly(admin_client, world):
    admin_client.post(reverse("supplier_payment_create"), {
        "contract": world["contract"].pk, "date": "2026-07-20", "currency": "uzs",
        "amount": "2400000", "exchange_rate": "12000", "method": "cash",
        "commission_percent": "0", "fee_percent": "0", "note": "",
    })
    payment = SupplierPayment.objects.get(amount_uzs=Decimal("2400000.00"))
    # the typed side is kept exact, the dollar side derived once \u2014 convert_pair's rule
    assert payment.currency == Currency.UZS and payment.amount == Decimal("200.00")

    row = AuditLog.objects.filter(target_type="Hamkor to'lovi", target_id=payment.pk).get()
    flat = re.sub(r"[\s\u00a0]", "", row.summary)
    # the journalled dollar figure IS the stored column, not a re-conversion of it
    assert f"{payment.amount}$" in flat, row.summary
    # and the typed so'm figure stays recoverable from the row the entry points at
    assert row.target_id == payment.pk
    assert SupplierPayment.objects.get(pk=row.target_id).amount_uzs == Decimal("2400000.00")


def test_invalid_payment_submission_writes_no_audit_row_and_no_money(admin_client, world):
    before_money, before_audit = _money_snapshot(), AuditLog.objects.count()
    resp = admin_client.post(reverse("supplier_payment_create"), {
        "contract": world["contract"].pk, "date": "2026-07-20", "currency": "usd",
        "amount": "-5", "exchange_rate": "12000", "method": "cash",
        "commission_percent": "0", "fee_percent": "0", "note": "",
    })
    assert resp.status_code == 200          # form re-rendered with errors
    assert AuditLog.objects.count() == before_audit
    assert _money_snapshot() == before_money


def test_invalid_user_create_writes_no_audit_row_and_no_user(admin_client):
    before_audit, before_users = AuditLog.objects.count(), User.objects.count()
    resp = admin_client.post(reverse("user_create"), {
        "username": "yomon", "first_name": "A", "last_name": "B",
        "phone": "12345",                    # not an intl number -> invalid
        "role": User.Role.TRANSLATOR, "password": "s3cret-pass-99",
    })
    assert resp.status_code == 200
    assert User.objects.count() == before_users
    assert AuditLog.objects.count() == before_audit


def test_empty_payment_grid_writes_no_audit_row(admin_client, world):
    """customer_payment_create calls AuditLog.record unconditionally after the
    atomic block, so an empty grid would journal a "0 ta · 0$" no-op — except
    BaseCustomerPaymentFormSet.clean refuses a settlement with no rows first.
    Asserted here because that guard is the only thing keeping the trail clean."""
    before_audit = AuditLog.objects.count()
    resp = admin_client.post(reverse("customer_payment_create"), {
        "customer": world["customer"].pk, "date": "2026-07-20",
        "form-TOTAL_FORMS": "0", "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
    })
    assert resp.status_code == 200          # re-rendered instead of saved
    lines = resp.context["lines"]
    assert not lines.is_valid()
    assert any("Kamida bitta to'lov" in e for e in lines.non_form_errors())
    assert not CustomerPayment.objects.filter(date=date(2026, 7, 20)).exists()
    assert AuditLog.objects.count() == before_audit


def test_translator_cannot_reach_the_django_admin(translator_client):
    """The CRM role gate is the app's; /admin/ has its own (is_staff). UserForm ties
    the two together, so a translator must land on the admin login, not the site."""
    resp = translator_client.get("/admin/")
    assert resp.status_code == 302 and "/admin/login/" in resp["Location"]


def test_audit_log_is_not_writable_through_any_route(admin_client, world, admin_user):
    """Append-only means: nothing in the app can edit or remove a row. There is no
    audit_edit/audit_delete url, AuditLog is not registered in django admin, and the
    only view over it renders a read-only table."""
    assert AuditLog not in django_admin.site._registry
    names = {p.name for p in get_resolver().url_patterns if getattr(p, "name", None)}
    assert not {n for n in names if n.startswith("audit_") and n != "audit_list"}
    AuditLog.record(admin_user, AuditLog.Action.CREATE, "Hamkor", 1, "seed")
    resp = admin_client.get(reverse("audit_list"))
    assert resp.status_code == 200
    assert "<form" not in resp.content.decode().split("<main")[-1].lower()


@pytest.mark.xfail(reason="CLAIM UPHELD — crm/models.py:161-164. AuditLog.user is "
                          "on_delete=SET_NULL and the row carries no denormalised "
                          "username, while accounts/forms.py:48-50 makes every "
                          "admin-role user is_superuser=True, so /admin/ really is one "
                          "click away for them. Deleting a colleague rewrites every "
                          "audit row that colleague ever wrote: user becomes NULL and "
                          "templates/crm/audit_list.html:8 renders `|default:\"Tizim\"`, "
                          "so a status change made by a person is re-labelled as one "
                          "made by the system. That is a misattribution, not a gap, and "
                          "it contradicts the class's own docstring (\"Append-only "
                          "trail\"). Only reachable for a user with no PROTECT-ed money "
                          "rows — i.e. exactly a translator, whose rows are the status "
                          "moves this audit exists to trace.",
                   strict=False)
def test_deleting_a_user_does_not_de_attribute_their_audit_rows(admin_user, translator_user):
    # A real CRM admin is created through UserForm, which stamps is_staff and
    # is_superuser from the role — so /admin/ genuinely is one click away for them.
    admin_user.is_staff = admin_user.is_superuser = True
    admin_user.save(update_fields=["is_staff", "is_superuser"])
    client = Client()
    client.force_login(admin_user)

    AuditLog.record(translator_user, AuditLog.Action.STATUS, "Yuk", 1, "Yo'lda → Yetib keldi")
    resp = client.post(f"/admin/accounts/user/{translator_user.pk}/delete/", {"post": "yes"})
    assert resp.status_code == 302, resp.status_code
    assert not User.objects.filter(pk=translator_user.pk).exists()
    row = AuditLog.objects.get(target_type="Yuk", target_id=1)
    assert row.user is not None, "the trail forgot who did it"


def test_money_rows_pin_their_author_so_the_trail_cannot_be_orphaned(db, translator_user, world):
    """The counterweight to the above: created_by is PROTECT on the money models, so
    a user who ever entered money genuinely cannot be deleted. Documented intent —
    asserted so a later on_delete change is caught."""
    from django.db.models import ProtectedError
    SupplierPayment.objects.create(
        contract=world["contract"], amount=Decimal("10"),
        exchange_rate=Decimal("12000"), created_by=translator_user)
    with pytest.raises(ProtectedError):
        translator_user.delete()


# ---------------------------------------------------------------------------
# (4) users: idempotence, stickiness of role, credential handling
# ---------------------------------------------------------------------------

def test_user_edit_resubmitted_twice_changes_nothing(admin_client, translator_user):
    """The 'values change by themselves' probe applied to accounts: post the same
    edit through the real view twice and assert the row is byte-identical."""
    url = reverse("user_edit", args=[translator_user.pk])
    payload = {"username": translator_user.username, "first_name": "Tar",
               "last_name": "Jimon", "phone": "+998 90 123 45 67",
               "role": User.Role.TRANSLATOR, "password": ""}
    admin_client.post(url, payload)
    translator_user.refresh_from_db()
    first = (translator_user.username, translator_user.phone, translator_user.role,
             translator_user.password, translator_user.is_staff, translator_user.is_superuser)
    admin_client.post(url, payload)
    admin_client.post(url, payload)
    translator_user.refresh_from_db()
    assert (translator_user.username, translator_user.phone, translator_user.role,
            translator_user.password, translator_user.is_staff,
            translator_user.is_superuser) == first
    assert translator_user.check_password(PASSWORD)


def test_role_sticks_to_what_was_submitted_and_binds_back_on_reopen(admin_client, translator_user):
    """The 'I change it but it stays on the other value' probe: submit admin,
    assert the saved row is admin AND the re-opened edit form is bound to admin."""
    url = reverse("user_edit", args=[translator_user.pk])
    admin_client.post(url, {"username": translator_user.username, "first_name": "Tar",
                            "last_name": "Jimon", "phone": "", "role": User.Role.ADMIN,
                            "password": ""})
    translator_user.refresh_from_db()
    assert translator_user.role == User.Role.ADMIN
    assert translator_user.is_staff and translator_user.is_superuser
    form = admin_client.get(url).context["form"]
    assert form.initial["role"] == User.Role.ADMIN


def test_demotion_takes_effect_on_the_demoted_users_live_session(admin_client, admin_user):
    """A revoked role must bite the session already open, not the next login —
    otherwise the demoted operator keeps writing to the ledger."""
    victim = User.objects.create_user(username="ikkinchi", password=PASSWORD,
                                      role=User.Role.ADMIN, first_name="I", last_name="K")
    victim.is_staff = victim.is_superuser = True
    victim.save()
    victim_client = Client()
    victim_client.force_login(victim)
    assert victim_client.get(reverse("kassa")).status_code == 200

    admin_client.post(reverse("user_edit", args=[victim.pk]), {
        "username": "ikkinchi", "first_name": "I", "last_name": "K", "phone": "",
        "role": User.Role.TRANSLATOR, "password": ""})
    assert victim_client.get(reverse("kassa")).status_code == 403


def test_deactivated_user_cannot_log_in(client, admin_user):
    admin_user.is_active = False
    admin_user.save(update_fields=["is_active"])
    resp = client.post(reverse("login"), {"username": "boss", "password": PASSWORD})
    assert resp.status_code == 200          # re-rendered form, not a redirect
    assert not resp.wsgi_request.user.is_authenticated


def test_deactivating_a_user_kills_their_live_session(admin_user):
    """Deactivation is the emergency brake. If the open tab keeps working, an
    ex-operator can still move money after being switched off."""
    victim_client = Client()
    victim_client.force_login(admin_user)
    assert victim_client.get(reverse("kassa")).status_code == 200
    admin_user.is_active = False
    admin_user.save(update_fields=["is_active"])
    assert victim_client.get(reverse("kassa")).status_code == 302


@pytest.mark.xfail(reason="CLAIM UPHELD — accounts/forms.py:43-53. UserForm declares "
                          "`password` as a plain CharField and save() calls "
                          "set_password() on it directly; there is no clean_password "
                          "and no call to django.contrib.auth.password_validation."
                          "validate_password, so the four validators configured at "
                          "config/settings.py:116-121 (MinimumLength, CommonPassword, "
                          "NumericPassword, UserAttributeSimilarity) never run on the "
                          "only screen that creates users. Verified: posting "
                          "password='1' with role=admin returns 302 and creates an "
                          "is_superuser=True account whose check_password('1') is True "
                          "— an account that can reach the kassa and /admin/ both.",
                   strict=False)
def test_user_create_enforces_the_configured_password_validators(admin_client):
    resp = admin_client.post(reverse("user_create"), {
        "username": "zaif", "first_name": "Z", "last_name": "A",
        "phone": "+998 90 000 00 00", "role": User.Role.ADMIN, "password": "1",
    })
    assert resp.status_code == 200
    assert not User.objects.filter(username="zaif").exists()


def test_the_weak_password_that_slips_through_really_does_open_the_ledger(admin_client):
    """Companion to the xfail above: pins what the missing validation actually buys an
    attacker, so the fix is not mistaken for cosmetics. Passes today by construction —
    it asserts the hole, not the wish — and must be deleted with the xfail."""
    admin_client.post(reverse("user_create"), {
        "username": "zaif", "first_name": "Z", "last_name": "A",
        "phone": "+998 90 000 00 00", "role": User.Role.ADMIN, "password": "1",
    })
    weak = User.objects.get(username="zaif")
    assert weak.check_password("1") and weak.is_superuser
    guessed = Client()
    assert guessed.login(username="zaif", password="1")
    assert guessed.get(reverse("kassa")).status_code == 200


def test_created_user_password_is_hashed_and_role_is_what_was_posted(admin_client):
    admin_client.post(reverse("user_create"), {
        "username": "yangi", "first_name": "Y", "last_name": "N",
        "phone": "+998 90 000 00 00", "role": User.Role.TRANSLATOR,
        "password": "s3cret-pass-99",
    })
    user = User.objects.get(username="yangi")
    assert user.role == User.Role.TRANSLATOR
    assert not user.is_staff and not user.is_superuser
    assert user.password != "s3cret-pass-99" and user.check_password("s3cret-pass-99")
    row = AuditLog.objects.filter(target_type="Foydalanuvchi", target_id=user.pk).get()
    assert row.action == AuditLog.Action.CREATE


# ---------------------------------------------------------------------------
# (5) the four standing probe families, applied to this area
#
# Added during triage: the original sweep covered who may reach what, but not what
# happens to the money and the trail when an ALLOWED actor repeats an allowed action.
# ---------------------------------------------------------------------------

def _payment_payload(payment, **overrides):
    """The edit form's own values, re-posted verbatim — the shape an operator who
    opens the modal and presses Saqlash without touching anything produces."""
    data = {
        "contract": payment.contract_id, "date": payment.date.isoformat(),
        "currency": payment.currency, "amount": str(payment.amount),
        "exchange_rate": str(payment.exchange_rate), "method": payment.method,
        "commission_percent": "0", "fee_percent": "0", "note": payment.note,
    }
    data.update(overrides)
    return data


# --- round-trip conversion -------------------------------------------------

def test_a_dollar_write_by_an_admin_round_trips_through_its_own_kurs(admin_client, world):
    """convert_pair keeps the typed side exact and derives the other once. Asserted
    from the audit side: the figure the trail journals must be the same figure the
    row stores, and converting it back at the row's own kurs must land on the stored
    so'm column — no third derivation anywhere in the write path."""
    admin_client.post(reverse("supplier_payment_create"), {
        "contract": world["contract"].pk, "date": "2026-07-20", "currency": "usd",
        "amount": "123.45", "exchange_rate": "12345.67", "method": "cash",
        "commission_percent": "0", "fee_percent": "0", "note": "",
    })
    payment = SupplierPayment.objects.get(amount=Decimal("123.45"))
    assert payment.amount_uzs == (Decimal("123.45") * Decimal("12345.67")).quantize(
        Decimal("0.01"))
    assert payment.in_som(payment.amount) == payment.amount_uzs

    row = AuditLog.objects.filter(target_type="Hamkor to'lovi", target_id=payment.pk).get()
    assert f"{payment.amount}$" in row.summary


def test_a_som_write_by_an_admin_round_trips_through_its_own_kurs(admin_client, world):
    admin_client.post(reverse("supplier_payment_create"), {
        "contract": world["contract"].pk, "date": "2026-07-20", "currency": "uzs",
        "amount": "2400000", "exchange_rate": "12000", "method": "cash",
        "commission_percent": "0", "fee_percent": "0", "note": "",
    })
    payment = SupplierPayment.objects.get(currency=Currency.UZS)
    # the TYPED side survives untouched; the dollar side is the derived one
    assert payment.amount_uzs == Decimal("2400000.00")
    assert payment.amount == Decimal("200.00")
    assert payment.amount * payment.exchange_rate == payment.amount_uzs


# --- no drift on re-save ---------------------------------------------------

def test_resaving_a_dollar_payment_through_the_admin_screen_three_times_drifts_nothing(
        admin_client, world):
    """The "figures move by themselves" probe on an admin-only money screen: open the
    edit modal, press Saqlash with the values it handed back, three times over."""
    payment = world["supplier_payment"]
    url = reverse("supplier_payment_edit", args=[payment.pk])
    before = (payment.amount, payment.amount_uzs, payment.currency, payment.exchange_rate)
    for i in range(3):
        # a real operator changes SOMETHING or Django would skip the write entirely
        resp = admin_client.post(url, _payment_payload(payment, note=f"izoh {i}"))
        assert resp.status_code in (200, 302)
        payment.refresh_from_db()
        assert (payment.amount, payment.amount_uzs, payment.currency,
                payment.exchange_rate) == before, f"drifted on pass {i + 1}"


def test_the_audit_row_never_disagrees_with_the_figure_that_was_stored(admin_client, world):
    """Fix-agnostic drift detector. Whatever a write ends up storing — right or wrong —
    the trail must journal THAT figure, so a reconciliation against the journal can
    still find a bad row. Stated as an invariant so it keeps holding after the so'm
    edit defect (tests/audit/test_som_edit_dataloss.py) is fixed."""
    payment = world["supplier_payment"]
    admin_client.post(reverse("supplier_payment_edit", args=[payment.pk]),
                      _payment_payload(payment, note="tekshiruv"))
    payment.refresh_from_db()
    row = AuditLog.objects.filter(target_type="Hamkor to'lovi",
                                  target_id=payment.pk).latest("created_at")
    assert row.action == AuditLog.Action.UPDATE
    assert f"{payment.amount}$" in row.summary, (row.summary, payment.amount)


def test_user_edit_does_not_rehash_the_password_when_the_box_is_left_blank(
        admin_client, translator_user):
    """The accounts twin of no-drift: a blank parol means keep, so the stored hash must
    be byte-identical afterwards — a re-hash of the same password would still verify
    and would hide a form that silently rewrites credentials on every edit."""
    hash_before = translator_user.password
    admin_client.post(reverse("user_edit", args=[translator_user.pk]), {
        "username": translator_user.username, "first_name": "Boshqa",
        "last_name": "Jimon", "phone": "", "role": User.Role.TRANSLATOR, "password": ""})
    translator_user.refresh_from_db()
    assert translator_user.first_name == "Boshqa"      # the edit really landed
    assert translator_user.password == hash_before


# --- currency stickiness ---------------------------------------------------

def test_the_currency_picker_sticks_to_som_when_a_som_payment_is_reopened(
        admin_client, world, admin_user):
    """Only the picker is asserted here. The AMOUNT box does not stick — it is seeded
    from the dollar column while wearing the so'm label — but that is the already
    established MoneyEntryFormMixin defect proven in tests/audit/test_som_edit_dataloss.py
    and is not re-reported from this area."""
    payment = SupplierPayment.objects.create(
        contract=world["contract"], amount=Decimal("200"), amount_uzs=Decimal("2400000"),
        currency=Currency.UZS, exchange_rate=Decimal("12000"), created_by=admin_user)
    form = admin_client.get(reverse("supplier_payment_edit", args=[payment.pk])).context["form"]
    assert form["currency"].value() == Currency.UZS
    assert form["exchange_rate"].value() == Decimal("12000")


def test_the_role_picker_sticks_to_translator_when_a_translator_is_reopened(
        admin_client, translator_user):
    form = admin_client.get(reverse("user_edit", args=[translator_user.pk])).context["form"]
    assert form["role"].value() == User.Role.TRANSLATOR
    assert form["password"].value() in (None, "")      # never echoed back


# --- aggregate consistency -------------------------------------------------

def test_one_audit_row_per_successful_money_write_and_none_per_refused_one(
        admin_client, world):
    """The trail is only an aggregate if the count matches the writes. Walk a whole
    lifecycle — create, refused create, edit, delete — and assert the journal grew by
    exactly the number of writes that actually landed."""
    before = AuditLog.objects.filter(target_type="Hamkor to'lovi").count()
    payload = {"contract": world["contract"].pk, "date": "2026-07-20", "currency": "usd",
               "amount": "10", "exchange_rate": "12000", "method": "cash",
               "commission_percent": "0", "fee_percent": "0", "note": ""}

    admin_client.post(reverse("supplier_payment_create"), payload)               # +1
    payment = SupplierPayment.objects.get(amount=Decimal("10"))
    admin_client.post(reverse("supplier_payment_create"), {**payload, "amount": "-1"})
    admin_client.post(reverse("supplier_payment_create"), {**payload, "amount": "999999"})
    admin_client.post(reverse("supplier_payment_edit", args=[payment.pk]),
                      _payment_payload(payment, note="tuzatildi"))               # +1
    admin_client.post(reverse("supplier_payment_delete", args=[payment.pk]))     # +1

    assert AuditLog.objects.filter(target_type="Hamkor to'lovi").count() == before + 3
    assert not SupplierPayment.objects.filter(pk=payment.pk).exists()


def test_a_refused_translator_write_leaves_the_ledger_and_the_trail_untouched(
        translator_client, world):
    """403 has to mean nothing happened, not "nothing much". Sweeps the whole
    admin-only write table in one pass against both snapshots."""
    money_before, audit_before = _money_snapshot(), AuditLog.objects.count()
    for route, key in ADMIN_ONLY_WRITE_ROUTES:
        translator_client.post(_url(route, key, world), {})
    assert _money_snapshot() == money_before
    assert AuditLog.objects.count() == audit_before


def test_stock_and_transit_never_count_the_same_kg_twice_on_an_allowed_status_move(
        translator_client, world):
    """The translator's own write, on a load with no sotuvlar: whatever the status
    does to the two ombor aggregates, their kg must still add up to the truck."""
    shipment = make_shipment(kg="200", price="1.00", brand="HDPE")
    total = transit_value()[2] + stock_value()[2]
    target = ShipmentStatus.objects.exclude(is_arrival=True).exclude(
        pk=shipment.status_id).first()
    translator_client.post(reverse("shipment_set_status", args=[shipment.pk]),
                           {"status": target.pk})
    assert transit_value()[2] + stock_value()[2] == total


# --- role gate: the remaining holes in the original sweep -------------------

def test_role_required_fails_closed_on_an_unrecognised_role_string(client, admin_user):
    """Sibling of the blank-role case: a role that is not blank but not a known value
    either (a bad import, a renamed choice) must not read as admin."""
    admin_user.role = "administrator"
    admin_user.save(update_fields=["role"])
    client.force_login(admin_user)
    assert client.get(reverse("kassa")).status_code == 403
    assert client.post(reverse("partner_create"), {"name": "X"}).status_code == 403
    assert not Partner.objects.filter(name="X").exists()


def test_role_required_is_a_membership_test_so_a_two_role_route_admits_both(
        admin_client, translator_client, world):
    """The other direction: the shared routes must not have been narrowed to admin by
    a stray edit, or the translator silently loses the job they are here to do."""
    for c in (admin_client, translator_client):
        assert c.get(reverse("shipment_list")).status_code == 200


@pytest.mark.parametrize("route,key", ADMIN_ONLY_READ_ROUTES,
                         ids=[r for r, _ in ADMIN_ONLY_READ_ROUTES])
def test_anonymous_is_bounced_to_login_on_every_admin_money_screen(client, world, route, key):
    """The write side of this sweep was already covered; the read side was not, and a
    money screen served to a logged-out browser is the same leak."""
    resp = client.get(_url(route, key, world))
    assert resp.status_code == 302 and "/login/" in resp["Location"]


def test_a_translator_cannot_promote_themselves(translator_client, translator_user):
    """The escalation that would make every other check pointless."""
    resp = translator_client.post(reverse("user_edit", args=[translator_user.pk]), {
        "username": translator_user.username, "first_name": "Tar", "last_name": "Jimon",
        "phone": "", "role": User.Role.ADMIN, "password": ""})
    assert resp.status_code == 403
    translator_user.refresh_from_db()
    assert translator_user.role == User.Role.TRANSLATOR
    assert not translator_user.is_staff and not translator_user.is_superuser


def test_a_bogus_status_pk_from_a_translator_is_a_404_not_a_500(translator_client, world):
    """The one field a translator controls on the one write they are trusted with."""
    resp = translator_client.post(
        reverse("shipment_set_status", args=[world["shipment"].pk]), {"status": "999999"})
    assert resp.status_code == 404
    world["shipment"].refresh_from_db()
    assert world["shipment"].status.is_arrival        # unchanged


def test_the_status_endpoint_redirects_to_the_page_it_was_posted_from(
        translator_client, world):
    """`next` comes from `{{ request.get_full_path }}` in shipment_list.html, i.e.
    always a path on this site. Pinned so the field cannot quietly start carrying a
    full URL, which `redirect()` would follow off-site without checking."""
    shipment = make_shipment(kg="200", price="1.00")
    target = ShipmentStatus.objects.exclude(is_arrival=True).exclude(
        pk=shipment.status_id).first()
    resp = translator_client.post(reverse("shipment_set_status", args=[shipment.pk]),
                                  {"status": target.pk, "next": "/shipments/?status=2"})
    assert resp.status_code == 302
    assert resp["Location"].startswith("/") and "//" not in resp["Location"]
