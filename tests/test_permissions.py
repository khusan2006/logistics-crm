from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentExpense, ShipmentLine, ShipmentStatus, SupplierPayment,
)

ADMIN_ONLY_URLS = [
    "/partners/", "/partners/new/", "/contracts/", "/contracts/new/",
    "/supplier-payments/", "/supplier-payments/new/", "/statuses/",
    "/expenses/new/", "/audit/", "/kassa/",
]


@pytest.mark.parametrize("url", ADMIN_ONLY_URLS)
def test_translator_gets_403(translator_client, url):
    assert translator_client.get(url).status_code == 403


@pytest.mark.parametrize("url", ["/shipments/"])
def test_translator_allowed(translator_client, url):
    assert translator_client.get(url).status_code == 200


def test_anonymous_redirected(client, db):
    assert client.get("/shipments/").status_code == 302


# --- Expanded sweep: admin-only MUTATION endpoints -------------------------
# The GET-only sweep above only caught a dropped decorator on list/create pages.
# This fixture builds real objects so we can hit edit/delete/move URLs with
# real PKs and confirm a translator is 403'd on every admin-only mutation route,
# not just list/create GETs.

@pytest.fixture
def crm_objects(db):
    partner = Partner.objects.create(name="Pars Polymer", phone="+998900000000", city="Tehran")
    contract = Contract.objects.create(partner=partner, created=date(2026, 1, 1), deadline=date(2026, 8, 1))
    contract_line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1.5"))
    payment = SupplierPayment.objects.create(contract=contract, amount=Decimal("500"))
    status = ShipmentStatus.objects.first()
    shipment = Shipment.objects.create(contract=contract, status=status)
    shipment_line = ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal("500"))
    expense = ShipmentExpense.objects.create(shipment=shipment, amount=Decimal("50"))
    return {
        "partner": partner, "contract": contract, "payment": payment,
        "status": status, "shipment": shipment, "expense": expense,
    }


MUTATION_ROUTES = [
    ("contract_edit", "contract"),
    ("contract_delete", "contract"),
    ("partner_edit", "partner"),
    ("partner_delete", "partner"),
    ("supplier_payment_edit", "payment"),
    ("supplier_payment_delete", "payment"),
    ("status_edit", "status"),
    ("status_delete", "status"),
    ("status_move", "status"),
    ("expense_edit", "expense"),
    ("expense_delete", "expense"),
    ("shipment_edit", "shipment"),
    ("shipment_delete", "shipment"),
]


@pytest.mark.parametrize("route_name,obj_key", MUTATION_ROUTES)
def test_translator_post_gets_403_on_admin_mutation(translator_client, crm_objects, route_name, obj_key):
    obj = crm_objects[obj_key]
    url = reverse(route_name, args=[obj.pk])
    resp = translator_client.post(url)
    assert resp.status_code == 403


@pytest.mark.parametrize("route_name,obj_key", MUTATION_ROUTES)
def test_translator_get_gets_403_on_admin_mutation(translator_client, crm_objects, route_name, obj_key):
    # GET renders either the edit form or the confirm modal — both must 403 too.
    obj = crm_objects[obj_key]
    url = reverse(route_name, args=[obj.pk])
    resp = translator_client.get(url)
    assert resp.status_code == 403


def test_translator_can_extend_shipment(translator_client, crm_objects):
    # Translators ARE allowed to extend a shipment's ETA.
    shipment = crm_objects["shipment"]
    resp = translator_client.get(reverse("shipment_extend", args=[shipment.pk]))
    assert resp.status_code == 200


def test_translator_set_status_non_arrival_allowed(translator_client, crm_objects):
    shipment = crm_objects["shipment"]
    non_arrival = ShipmentStatus.objects.exclude(is_arrival=True).exclude(pk=shipment.status_id).first()
    resp = translator_client.post(
        reverse("shipment_set_status", args=[shipment.pk]), {"status": non_arrival.pk}
    )
    assert resp.status_code == 302


def test_translator_set_status_arrival_forbidden(translator_client, crm_objects):
    # Belt-and-suspenders: also covered in test_status_flow.py; kept here so the
    # mutation-endpoint sweep is self-contained.
    shipment = crm_objects["shipment"]
    arrival = ShipmentStatus.arrival()
    resp = translator_client.post(
        reverse("shipment_set_status", args=[shipment.pk]), {"status": arrival.pk}
    )
    assert resp.status_code == 403
