"""Skladchi: the person on the shelf.

Two pages, read-only, and no narx on either. What they answer for is the granula —
how much came in, how much went out, and to whom — so anything that would let them
change it, or tell them what it is worth, must be out of reach rather than merely
out of sight: hidden buttons still have URLs.
"""
from decimal import Decimal

from conftest import line_data
from crm.models import (
    Contract, ContractLine, Customer, Partner, Sale, Shipment, ShipmentLine,
    ShipmentStatus,
)


def _stock(brand="LLDPE", kg="1000", price="1.23", arrived="2026-07-10"):
    """An arrived lot whose landed cost is a figure no other number on the page
    could be mistaken for — so "the tannarx is not shown" can be asserted exactly."""
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, created="2026-07-01")
    ContractLine.objects.create(contract=contract, brand=brand,
                                kg=Decimal(kg), price=Decimal(price))
    shipment = Shipment.objects.create(contract=contract, status=ShipmentStatus.arrival(),
                                       sent="2026-07-05", arrived=arrived)
    return ShipmentLine.objects.create(shipment=shipment,
                                       contract_line=contract.lines.first(), kg=Decimal(kg))


def _table(html):
    """Just the rows. base.html carries a money-formatting JS helper whose comment
    holds a "$", so a page-wide search for money finds the app's own source."""
    return html.split("<table", 1)[-1].split("</table>")[0]


def _sale(admin_client, lot, customer_name="Alisher Mebel", kg="120", price="7.77"):
    customer = Customer.objects.create(name=customer_name, phone="1", address="T")
    admin_client.post("/sales/new/", {
        "customer": customer.pk, "currency": "usd", "exchange_rate": "12000",
        "date": "2026-07-18", "debt_deadline": "", "note": "",
        **line_data({"brand": lot.brand, "kg": kg, "price": price})})
    return Sale.objects.get()


class TestWhatASkladchiCanOpen:

    def test_ombor_and_sotuvlar_are_theirs(self, skladchi_client, db):
        assert skladchi_client.get("/ombor/").status_code == 200
        assert skladchi_client.get("/sales/").status_code == 200

    def test_login_lands_on_ombor(self, skladchi_client, db):
        """The dashboard sends every non-admin somewhere; Yuklar is not theirs, so
        sending them there would 403 them at the door."""
        resp = skladchi_client.get("/")
        assert resp.status_code == 302 and resp.url == "/ombor/"

    def test_every_other_page_is_refused(self, skladchi_client, db):
        for url in ["/contracts/", "/shipments/", "/customers/", "/partners/",
                    "/debts/", "/kassa/", "/reports/", "/audit/", "/users/",
                    "/reservations/", "/customer-payments/", "/supplier-payments/",
                    "/logists/", "/customs/", "/statuses/"]:
            assert skladchi_client.get(url).status_code == 403, url

    def test_a_sotuv_page_is_refused(self, skladchi_client, admin_client, db):
        """The sotuv's own page carries the narx, so the list does not link to it
        and the URL does not answer either."""
        sale = _sale(admin_client, _stock())
        assert skladchi_client.get(f"/sales/{sale.pk}/").status_code == 403


class TestASkladchiChangesNothing:

    def test_the_sotuv_forms_are_refused(self, skladchi_client, admin_client, db):
        sale = _sale(admin_client, _stock())
        for url in ["/sales/new/", f"/sales/{sale.pk}/edit/", f"/sales/{sale.pk}/delete/"]:
            assert skladchi_client.get(url).status_code == 403, url
            assert skladchi_client.post(url, {}).status_code == 403, url

    def test_selling_from_the_ombor_is_refused(self, skladchi_client, db):
        """The Sotish button is not drawn for them; the POST behind it is refused
        too, which is the half that matters."""
        lot = _stock()
        resp = skladchi_client.post("/sales/new/", {
            "customer": 1, "currency": "usd", "exchange_rate": "12000",
            "date": "2026-07-18", "debt_deadline": "", "note": "",
            **line_data({"brand": lot.brand, "kg": "10", "price": "2"})})
        assert resp.status_code == 403
        assert not Sale.objects.exists()

    def test_bron_is_refused(self, skladchi_client, db):
        _stock()
        assert skladchi_client.post("/reservations/new/", {}).status_code == 403


class TestNoNarxAnywhere:

    def test_ombor_shows_the_kg_but_not_the_tannarx(self, skladchi_client, db):
        _stock(brand="LLDPE", kg="1000", price="1.23")
        html = skladchi_client.get("/ombor/").content.decode()
        assert "LLDPE" in html and "1 000" in html      # marka and its kg
        assert "Tan narx" not in html
        assert "1.23" not in html
        assert "$" not in _table(html)

    def test_ombor_offers_no_way_to_sell(self, skladchi_client, db):
        _stock()
        html = skladchi_client.get("/ombor/").content.decode()
        # By URL, not by word: the kg columns are called "Sotish mumkin" and
        # "Bronlangan", and those stay — they are counts of granula, not offers.
        assert "/sales/new/" not in html
        assert "/reservations/new/" not in html

    def test_ombor_links_nowhere_a_skladchi_cannot_go(self, skladchi_client, db):
        """A lot number opens its yuk for an admin. For them it is plain text: a
        link that 403s reads as a broken page rather than as a limit."""
        lot = _stock()
        html = skladchi_client.get("/ombor/").content.decode()
        assert f"#{lot.pk}" in html
        assert f"/shipments/{lot.shipment_id}/" not in html

    def test_sotuvlar_shows_who_and_how_much_only(self, skladchi_client, admin_client, db):
        _sale(admin_client, _stock(), customer_name="Alisher Mebel", kg="120", price="7.77")
        html = skladchi_client.get("/sales/").content.decode()
        assert "Alisher Mebel" in html and "120" in html
        # In the TABLE, for the reason `_table` exists: base.html carries the app's
        # own money JS, and a page-wide search for a column name finds its source.
        for column in ["Tan narx", "Sotuv narxi", "Jami", "Foyda", "Qoldiq"]:
            assert column not in _table(html), column
        assert "7.77" not in html and "$" not in _table(html)

    def test_sotuvlar_offers_no_actions(self, skladchi_client, admin_client, db):
        sale = _sale(admin_client, _stock())
        html = skladchi_client.get("/sales/").content.decode()
        assert f"/sales/{sale.pk}/edit/" not in html
        assert f"/sales/{sale.pk}/delete/" not in html
        # The create button, by its URL rather than its label: "Yangi sotuvlar" is
        # also the name of a FILTER on this page, and a skladchi may read that one —
        # narrowing a list they are allowed to see is not an action on it.
        assert "/sales/new/" not in html

    def test_an_admin_still_sees_all_of_it(self, admin_client, db):
        """The gates are on the role, not on the page — the admin's view is the one
        that was there before."""
        _sale(admin_client, _stock(), price="7.77")
        html = admin_client.get("/sales/").content.decode()
        assert "Sotuv narxi" in html and "7.77" in html
        assert "Tan narx" in admin_client.get("/ombor/").content.decode()


class TestTheNav:

    def test_only_the_two_pages_are_linked(self, skladchi_client, db):
        html = skladchi_client.get("/ombor/").content.decode()
        assert 'href="/ombor/"' in html and 'href="/sales/"' in html
        for url in ['href="/"', 'href="/shipments/"', 'href="/contracts/"',
                    'href="/kassa/"', 'href="/debts/"', 'href="/users/"',
                    'href="/reports/"']:
            assert url not in html, url

    def test_the_role_is_named_on_screen(self, skladchi_client, db):
        assert "Skladchi" in skladchi_client.get("/ombor/").content.decode()
