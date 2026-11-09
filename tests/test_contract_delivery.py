"""SIP Toshkent / Logist on an Eron kelishuv.

SIP Toshkent: the hamkor delivers to Toshkent, so its yuklar carry no logist, no
haydovchi avansi and no transport or yo'l xarajati. Logist: we haul it through a
logist, who must be named. What the logist paid shows per yuk and, summed, per
kelishuv on the list.
"""

from decimal import Decimal

from conftest import line_data, make_contract, make_shipment
from crm.forms import ContractForm, ExpenseGridForm, ShipmentForm
from crm.models import (Contract, Logist, Partner, ShipmentExpense, ShipmentStatus,
                        birja_partner)
from crm.views import _attach_logist_spent

SIP, LOGIST = Contract.Delivery.SIP, Contract.Delivery.LOGIST


def _logist(name="Sardor"):
    return Logist.objects.create(name=name, phone="+998901112233")


def _partner():
    return Partner.objects.create(name="Pars", phone="1", city="Tehron")


def _contract_post(partner, **over):
    body = {"partner": partner.pk, "currency": "usd", "created": "2026-07-04",
            "note": "", "delivery": "", "logist": "",
            **line_data({"brand": "LLDPE", "kg": "24000", "price": "1.05"})}
    body.update(over)
    return body


def _expense(shipment, logist=None, amount="100", currency="usd", **kw):
    amount = Decimal(amount)
    return ShipmentExpense.objects.create(
        shipment=shipment, date="2026-07-10", logist=logist, currency=currency,
        category=kw.pop("category", ShipmentExpense.Category.TRANSPORT),
        amount=amount if currency == "usd" else amount / 12000,
        amount_uzs=amount * 12000 if currency == "usd" else amount,
        exchange_rate=Decimal("12000"), **kw)


# --- the kelishuv form -------------------------------------------------------

class TestContractForm:
    def test_eron_kelishuv_must_say_how_it_is_hauled(self, admin_client, db):
        resp = admin_client.post("/contracts/new/", _contract_post(_partner()))
        assert resp.status_code != 302
        assert not Contract.objects.exists()

    def test_logist_kelishuv_must_name_its_logist(self, admin_client, db):
        resp = admin_client.post("/contracts/new/",
                                 _contract_post(_partner(), delivery=LOGIST))
        assert resp.status_code != 302
        assert "Logistni tanlang" in resp.content.decode()
        assert not Contract.objects.exists()

    def test_logist_kelishuv_saves_with_its_logist(self, admin_client, db):
        logist = _logist()
        resp = admin_client.post("/contracts/new/", _contract_post(
            _partner(), delivery=LOGIST, logist=logist.pk))
        assert resp.status_code == 302
        contract = Contract.objects.get()
        assert (contract.delivery, contract.logist_id) == (LOGIST, logist.pk)

    def test_sip_kelishuv_drops_a_logist_left_in_the_hidden_picker(self, admin_client, db):
        resp = admin_client.post("/contracts/new/", _contract_post(
            _partner(), delivery=SIP, logist=_logist().pk))
        assert resp.status_code == 302
        contract = Contract.objects.get()
        assert contract.is_sip and contract.logist_id is None

    def test_birja_form_does_not_ask(self, db):
        form = ContractForm(birja=True)
        assert "delivery" not in form.fields and "logist" not in form.fields

    def test_nothing_is_picked_in_advance(self, db):
        assert not ContractForm().initial.get("delivery")


# --- the yuk form ------------------------------------------------------------

def _yuk_post(contract, logist=None, advance=""):
    line = contract.lines.first()
    return {
        "contract": contract.pk, "status": ShipmentStatus.objects.first().pk,
        "sent": "2026-07-05", "eta": "2026-07-15", "responsible": "",
        "driver_name": "Akmal aka", "driver_phone": "", "transport": "",
        "container": "", "note": "", "driver_advance": advance,
        "logist": logist.pk if logist else "",
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "10",
        "lines-0-contract_line": line.pk, "lines-0-kg": "400",
        "lines-0-price": "", "lines-0-currency": "usd",
        "lines-0-exchange_rate": "12000", "lines-0-id": "",
    }


class TestShipmentForm:
    def test_sip_yuk_refuses_a_logist(self, admin_client, db):
        contract = make_contract(kg="9000", delivery=SIP)
        resp = admin_client.post("/shipments/new/", _yuk_post(contract, _logist(), "500"))
        assert resp.status_code != 302
        assert "SIP Toshkent kelishuvida logist" in resp.content.decode()
        assert not ShipmentExpense.objects.exists()

    def test_sip_yuk_saves_without_one(self, admin_client, db):
        contract = make_contract(kg="9000", delivery=SIP)
        resp = admin_client.post("/shipments/new/", _yuk_post(contract))
        assert resp.status_code == 302

    def test_an_old_yuk_keeps_its_logist_after_the_switch(self, db):
        """A yuk loaded before its kelishuv became SIP must stay saveable as is."""
        logist = _logist()
        contract = make_contract(kg="9000", delivery=LOGIST, logist=logist)
        shipment = make_shipment(contract, logist=logist, sent="2026-07-05",
                                 eta="2026-07-15")
        contract.delivery, contract.logist = SIP, None
        contract.save()
        data = _yuk_post(contract, logist)
        form = ShipmentForm(data, instance=shipment)
        assert "logist" not in form.errors

    def test_contract_options_carry_delivery_and_logist(self, db):
        logist = _logist()
        hauled = make_contract(kg="9000", delivery=LOGIST, logist=logist)
        make_contract(partner=hauled.partner, kg="9000", delivery=SIP)
        html = str(ShipmentForm()["contract"])
        assert f'data-logist="{logist.pk}"' in html
        assert 'data-delivery="sip"' in html


# --- the xarajat grid ----------------------------------------------------------

class TestExpenseGrid:
    def _boxes(self, shipment):
        return {value for value, _ in ExpenseGridForm(shipment=shipment).categories}

    def test_sip_yuk_has_no_transport_or_road_box(self, db):
        shipment = make_shipment(make_contract(kg="9000", delivery=SIP))
        boxes = self._boxes(shipment)
        assert "transport" not in boxes and "road" not in boxes
        assert {"customs", "declarant", "loader", "cert", "other"} <= boxes

    def test_logist_yuk_has_every_box(self, db):
        shipment = make_shipment(make_contract(kg="9000", delivery=LOGIST,
                                               logist=_logist()))
        assert self._boxes(shipment) == {v for v, _ in ShipmentExpense.Category.choices}

    def test_a_row_entered_before_the_switch_keeps_its_box(self, db):
        shipment = make_shipment(make_contract(kg="9000", delivery=SIP))
        _expense(shipment, category=ShipmentExpense.Category.ROAD)
        assert "road" in self._boxes(shipment)


# --- what the logist paid ------------------------------------------------------

class TestLogistSpent:
    def test_per_yuk_counts_every_row_a_logist_paid(self, db):
        logist = _logist()
        shipment = make_shipment(make_contract(kg="9000", delivery=LOGIST, logist=logist))
        _expense(shipment, logist, "500", is_driver_advance=True)
        _expense(shipment, logist, "300", category=ShipmentExpense.Category.CUSTOMS)
        _expense(shipment, logist, "2400000", currency="uzs")
        _expense(shipment, None, "999")  # the kassa paid this one
        assert shipment.logist_spent_by_currency() == [
            ("usd", Decimal("800")), ("uzs", Decimal("2400000"))]

    def test_per_kelishuv_sums_its_yuklar_and_the_list_agrees(self, db):
        logist = _logist()
        contract = make_contract(kg="9000", delivery=LOGIST, logist=logist)
        first, second = make_shipment(contract), make_shipment(contract)
        _expense(first, logist, "500")
        _expense(second, logist, "600")
        other = make_shipment(make_contract(kg="9000", delivery=LOGIST, logist=logist))
        _expense(other, logist, "9999")
        assert contract.logist_spent_by_currency() == [("usd", Decimal("1100"))]
        _attach_logist_spent([contract])
        assert contract.logist_spent == contract.logist_spent_by_currency()

    def test_the_list_shows_it(self, admin_client, db):
        logist = _logist("Sardor aka")
        contract = make_contract(kg="9000", delivery=LOGIST, logist=logist)
        _expense(make_shipment(contract), logist, "1100")
        make_contract(partner=contract.partner, kg="9000", delivery=SIP)
        html = admin_client.get("/contracts/?state=").content.decode()
        assert "Sardor aka" in html and "SIP Toshkent" in html
        assert "1 100" in html or "1 100" in html

    def test_the_yuk_page_shows_it(self, admin_client, db):
        logist = _logist()
        shipment = make_shipment(make_contract(kg="9000", delivery=LOGIST, logist=logist))
        _expense(shipment, logist, "700")
        html = admin_client.get(f"/shipments/{shipment.pk}/").content.decode()
        assert "Logist xarajati" in html


def test_birja_kelishuv_list_has_no_logist_column(admin_client, db):
    make_contract(partner=birja_partner(), kg="9000")
    html = admin_client.get("/birja/kelishuvlar/?state=").content.decode()
    assert ">Logist</th>" not in html


def test_backfill_marks_old_eron_kelishuvlar_logist(db):
    """Old Eron kelishuvlar become Logist; the logist is filled only when every yuk
    that names one names the same one. Birja stays blank."""
    import importlib

    from django.apps import apps
    backfill = importlib.import_module(
        "crm.migrations.0088_contract_delivery_backfill").backfill
    sardor, ali = _logist("Sardor"), _logist("Ali")
    one = make_contract(kg="9000")
    make_shipment(one, logist=sardor)
    make_shipment(one)
    two = make_contract(partner=one.partner, kg="9000")
    make_shipment(two, logist=sardor)
    make_shipment(two, logist=ali)
    birja = make_contract(partner=birja_partner(), kg="9000")
    backfill(apps, None)
    for c in (one, two, birja):
        c.refresh_from_db()
    assert (one.delivery, one.logist_id) == (LOGIST, sardor.pk)
    assert (two.delivery, two.logist_id) == (LOGIST, None)
    assert (birja.delivery, birja.logist_id) == ("", None)


# --- who paid, on a Logist kelishuv's yuk ---------------------------------------

def _grid(shipment, **over):
    body = {"shipment": shipment.pk, "date": "2026-07-08", "currency": "usd",
            "method": "cash", "exchange_rate": "12000", "fee_percent": "0",
            "note": ""}
    for category, _label in ShipmentExpense.Category.choices:
        for prefix in ("amount", "currency", "method", "fee", "row", "payer"):
            body[f"{prefix}_{category}"] = ""
    body.update(over)
    return body


class TestWhoPaidMustBeSaid:
    """Whether the logist or the kassa paid the driver differs load to load, so
    on a Logist kelishuv's yuk the Transport and Yo'l xarajati boxes have no
    default and must be answered."""

    def _hauled(self):
        logist = _logist()
        contract = make_contract(kg="9000", delivery=LOGIST, logist=logist)
        return make_shipment(contract), logist

    def test_both_boxes_open_blank_with_kassa_and_logist_offered(self, db):
        shipment, logist = self._hauled()
        form = ExpenseGridForm(shipment=shipment)
        for category in ("transport", "road"):
            field = form.fields[f"payer_{category}"]
            values = [value for value, _ in field.choices]
            assert values[:2] == ["", "kassa"]
            assert f"logist:{logist.pk}" in values
            assert not form.initial.get(f"payer_{category}")

    def test_a_figure_with_nobody_named_is_refused(self, admin_client, db):
        shipment, _logist_ = self._hauled()
        resp = admin_client.post("/expenses/new/", _grid(
            shipment, amount_transport="1100", amount_road="50"))
        assert resp.status_code != 302
        assert "laganini tanlang — kassa yoki logist" in resp.content.decode()
        assert not ShipmentExpense.objects.exists()

    def test_the_logist_pick_counts_and_the_kassa_pick_does_not(self, admin_client, db):
        shipment, logist = self._hauled()
        resp = admin_client.post("/expenses/new/", _grid(
            shipment, amount_transport="1100", payer_transport=f"logist:{logist.pk}",
            amount_road="50", payer_road="kassa"))
        assert resp.status_code == 302
        transport = ShipmentExpense.objects.get(category="transport")
        road = ShipmentExpense.objects.get(category="road")
        assert transport.logist_id == logist.pk
        assert road.logist_id is None and road.from_kassa
        assert shipment.logist_spent_by_currency() == [("usd", Decimal("1100"))]

    def test_a_kassa_row_reopens_showing_the_kassa(self, db):
        shipment, _logist_ = self._hauled()
        _expense(shipment, None, "200")
        form = ExpenseGridForm(shipment=shipment)
        assert form.initial["payer_transport"] == "kassa"

    def test_a_birja_yuk_is_unchanged(self, db):
        shipment = make_shipment(make_contract(partner=birja_partner(), kg="9000"))
        form = ExpenseGridForm(shipment=shipment)
        assert "payer_road" not in form.fields
        assert form.fields["payer_transport"].choices[0] == ("", "Kassadan to'landi")
