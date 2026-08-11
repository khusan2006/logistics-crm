from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from crm.models import (
    Contract, ContractLine, Partner, Shipment, ShipmentExpense, ShipmentLeg,
    ShipmentLine, ShipmentStatus, SupplierPayment,
)

# A tarjimon has exactly two screens — Kelishuvlar to read, Yuklar to read and to
# keep the haydovchi and konteyner current — and this module is where that sentence
# is enforced rather than merely described.
ADMIN_ONLY_URLS = [
    "/partners/", "/partners/new/", "/contracts/new/",
    "/supplier-payments/", "/supplier-payments/new/", "/statuses/",
    "/expenses/new/", "/audit/", "/kassa/",
    # Reachable from the sidebar until the Logistlar link was moved inside the admin
    # guard, so the link showed and then 403'd.
    "/logists/", "/customs/", "/customers/", "/sales/", "/ombor/", "/reports/",
]


@pytest.mark.parametrize("url", ADMIN_ONLY_URLS)
def test_translator_gets_403(translator_client, url):
    assert translator_client.get(url).status_code == 403


@pytest.mark.parametrize("url", ["/shipments/", "/contracts/"])
def test_translator_allowed(translator_client, url):
    assert translator_client.get(url).status_code == 200


def test_translator_reaches_the_finished_loads(translator_client, db):
    """`/shipments/done/` is a redirect into the same list with ?all=1, so "allowed"
    here means it is not a 403 — a tarjimon follows it and lands on the loads."""
    resp = translator_client.get("/shipments/done/")
    assert resp.status_code == 302 and resp.url == "/shipments/?all=1"
    assert translator_client.get(resp.url).status_code == 200


def test_a_tarjimon_is_offered_no_create_button(translator_client, db):
    """Both create FABs were guarded by an `{% if %}` wrapped AROUND their
    `{% block fab %}`, which does nothing — a block is resolved by inheritance before
    any condition surrounding it runs. So the buttons rendered for a tarjimon and
    403'd on click. Neither create view was ever reachable; this pins the screen
    saying so."""
    for url, label in (("/shipments/", "Yuk qo&#x27;shish"),
                       ("/contracts/", "Yangi kelishuv")):
        html = translator_client.get(url).content.decode()
        assert label not in html, url
        assert 'class="fab"' not in html, url


def test_no_template_guards_a_block_from_outside_it(db):
    """The same mistake anywhere else would be just as silent. An `{% if %}` on the
    line before a `{% block %}` never gates it — the guard has to go inside."""
    import glob
    import re

    offenders = []
    for path in sorted(glob.glob("templates/**/*.html", recursive=True)):
        lines = open(path).read().splitlines()
        for i, line in enumerate(lines[1:], start=1):
            if not re.search(r"{%\s*block\s", line):
                continue
            # An `{% if %}` that is opened AND closed on the previous line encloses
            # nothing below it — which is exactly the correct fixed form, where the
            # guard lives inside a one-line block. Only a still-open one is the bug.
            prev = lines[i - 1]
            hanging = (len(re.findall(r"{%\s*if\s", prev))
                       - len(re.findall(r"{%\s*endif\s*%}", prev)))
            if hanging > 0:
                offenders.append(f"{path}:{i + 1}")
    assert not offenders, (
        "{% if %} wrapped around {% block %} does not gate it — move the condition "
        "inside the block: " + ", ".join(offenders))


def test_the_sidebar_offers_a_tarjimon_only_their_two_screens(translator_client, db):
    """A link that 403s is worse than no link: it reads as a broken app rather than
    as a boundary. Logistlar sat outside the admin guard and did exactly that."""
    html = translator_client.get("/shipments/").content.decode()
    assert 'href="/shipments/"' in html and 'href="/contracts/"' in html
    for gone in ("/logists/", "/customs/", "/kassa/", "/sales/", "/ombor/",
                 "/partners/", "/supplier-payments/", "/customers/", "/reports/"):
        assert f'href="{gone}"' not in html, gone


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
    contract = Contract.objects.create(partner=partner, created=date(2026, 1, 1))
    contract_line = ContractLine.objects.create(
        contract=contract, brand="LLDPE", kg=Decimal("1000"), price=Decimal("1.5"))
    payment = SupplierPayment.objects.create(contract=contract, amount=Decimal("500"))
    status = ShipmentStatus.objects.first()
    shipment = Shipment.objects.create(contract=contract, status=status)
    shipment_line = ShipmentLine.objects.create(
        shipment=shipment, contract_line=contract.lines.first(), kg=Decimal("500"))
    expense = ShipmentExpense.objects.create(shipment=shipment, amount=Decimal("50"))
    leg = ShipmentLeg.objects.create(
        shipment=shipment, order=1, from_location="Tehron", to_location="Chegara")
    return {
        "partner": partner, "contract": contract, "payment": payment,
        "status": status, "shipment": shipment, "expense": expense, "leg": leg,
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
    # Everything about a yuk EXCEPT the haydovchi and konteyner.
    ("shipment_extend", "shipment"),
    ("leg_edit", "leg"),
    ("leg_delete", "leg"),
]

#: Routes that take POST only. `require_POST` sits outside `role_required`, so a GET
#: is 405'd before the role is ever looked at — correct (a GET cannot mutate), but it
#: means they belong in the POST sweep and not the GET one.
POST_ONLY_ROUTES = [
    ("shipment_set_status", "shipment"),
    ("leg_move", "leg"),
]


@pytest.mark.parametrize("route_name,obj_key", MUTATION_ROUTES + POST_ONLY_ROUTES)
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


# --- The one write a tarjimon has ------------------------------------------------

class TestTarjimonDriverEdit:
    """Haydovchi va konteyner is the whole of a tarjimon's write access. What makes
    that true is ShipmentDriverForm's field list, not the template: a ModelForm binds
    only the fields it declares, so anything else in the request body is ignored."""

    def _url(self, shipment):
        return reverse("shipment_driver_edit", args=[shipment.pk])

    def test_a_tarjimon_may_open_and_save_it(self, translator_client, crm_objects):
        shipment = crm_objects["shipment"]
        assert translator_client.get(self._url(shipment)).status_code == 200

        resp = translator_client.post(self._url(shipment), {
            "driver_name": "Akmal aka", "driver_phone": "+998901112233",
            "transport": "01 777 AAA", "container": "MSKU1234567"})
        assert resp.status_code == 302
        shipment.refresh_from_db()
        assert shipment.driver_name == "Akmal aka"
        assert shipment.driver_phone == "+998901112233"
        assert shipment.transport == "01 777 AAA"
        assert shipment.container == "MSKU 123456 7"      # normalised on the way in

    def test_posting_other_fields_changes_nothing(self, translator_client, crm_objects):
        """The lock. A tarjimon who hand-crafts a request cannot reach the kelishuv,
        the holat, the sana or the logist through this endpoint, because the form
        never binds them."""
        shipment = crm_objects["shipment"]
        other_status = ShipmentStatus.objects.exclude(pk=shipment.status_id).first()
        before = (shipment.contract_id, shipment.status_id, shipment.eta,
                  shipment.sent, shipment.logist_id, shipment.responsible)

        resp = translator_client.post(self._url(shipment), {
            "driver_name": "Akmal aka", "driver_phone": "", "transport": "", "container": "",
            # none of these are on the form
            "status": other_status.pk, "contract": shipment.contract_id,
            "eta": "2030-01-01", "sent": "2030-01-01", "responsible": "Men",
        })
        assert resp.status_code == 302
        shipment.refresh_from_db()
        assert shipment.driver_name == "Akmal aka"        # the allowed field landed
        assert (shipment.contract_id, shipment.status_id, shipment.eta,
                shipment.sent, shipment.logist_id, shipment.responsible) == before

    def test_it_is_written_to_the_audit_log(self, translator_client, crm_objects):
        from crm.models import AuditLog
        shipment = crm_objects["shipment"]
        translator_client.post(self._url(shipment), {
            "driver_name": "Akmal aka", "driver_phone": "", "transport": "01 777 AAA",
            "container": ""})
        entry = AuditLog.objects.filter(target_id=shipment.pk).latest("id")
        assert "Akmal aka" in entry.summary and "01 777 AAA" in entry.summary
