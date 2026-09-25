from decimal import Decimal

import pytest

from crm import geo

LAT, LNG = Decimal("41.311081"), Decimal("69.240562")


@pytest.mark.parametrize("text", [
    "41.311081, 69.240562",
    "41.311081 69.240562",
    "41.311081,69.240562",
    "41,311081 69,240562",
    "41,311081, 69,240562",
    "geo:41.311081,69.240562?z=17",
    "https://www.google.com/maps?q=41.311081,69.240562",
    "https://maps.google.com/?q=41.311081,69.240562",
    "https://www.google.com/maps/search/41.311081,+69.240562",
    "https://www.google.com/maps/place/41%C2%B018'39.9%22N/@41.311081,69.240562,17z",
    "https://www.google.com/maps/place/Chorsu/@41.3,69.2,15z/data=!4m6!3m5!3d41.311081!4d69.240562",
    "https://yandex.uz/maps/?ll=69.1,41.1&pt=69.240562,41.311081&z=17",
    "https://yandex.ru/maps/10335/tashkent/?ll=69.240562%2C41.311081&z=16",
    "https://yandex.uz/maps/?whatshere%5Bpoint%5D=69.240562%2C41.311081&whatshere%5Bzoom%5D=17",
    "https://2gis.uz/tashkent/geo/69.240562,41.311081",
    "https://2gis.uz/tashkent?m=69.240562%2C41.311081%2F16",
    "https://maps.apple.com/?ll=41.311081,69.240562&q=Pin",
    "https://www.openstreetmap.org/?mlat=41.311081&mlon=69.240562#map=17/41.3/69.2",
    "https://www.openstreetmap.org/#map=17/41.311081/69.240562",
    "Мы здесь: https://yandex.uz/maps/?pt=69.240562,41.311081&z=17",
])
def test_parse_location_reads_every_format(text):
    assert geo.parse_location(text) == (LAT, LNG)


def test_parse_location_reads_dms():
    lat, lng = geo.parse_location("41°18'39.9\"N 69°14'26.0\"E")
    assert (round(lat, 4), round(lng, 4)) == (Decimal("41.3111"), Decimal("69.2406"))


def test_parse_location_southern_western_dms():
    lat, lng = geo.parse_location("33°52'S 151°12'W")
    assert lat < 0 and lng < 0


@pytest.mark.parametrize("text", [
    "", "Toshkent, Chilonzor", "91, 69", "41, 181", "0, 0",
    "https://www.google.com/maps/place/Chorsu+Bazaar",
    "https://example.com/?q=41.3,69.2",
])
def test_parse_location_refuses_what_is_not_a_point(text):
    assert geo.parse_location(text) is None


def test_resolve_follows_short_link_between_map_hosts(monkeypatch):
    hops = {
        "https://maps.app.goo.gl/abc": "https://www.google.com/maps/place/X/@41.311081,69.240562,17z",
    }
    monkeypatch.setattr(geo, "_next_hop", lambda url: hops.get(url))
    assert geo.resolve_location("https://maps.app.goo.gl/abc") == (LAT, LNG)


def test_resolve_yandex_short_link(monkeypatch):
    monkeypatch.setattr(geo, "_next_hop",
                        lambda url: "https://yandex.uz/maps/?pt=69.240562,41.311081&z=17")
    assert geo.resolve_location("https://yandex.uz/maps/-/CDqZaB~3") == (LAT, LNG)


def test_resolve_never_leaves_map_hosts(monkeypatch):
    asked = []

    def hop(url):
        asked.append(url)
        return "http://10.0.0.1/admin"

    monkeypatch.setattr(geo, "_next_hop", hop)
    with pytest.raises(geo.LocationError):
        geo.resolve_location("https://maps.app.goo.gl/abc")
    assert asked == ["https://maps.app.goo.gl/abc"]


def test_resolve_network_failure_is_an_operator_message(monkeypatch):
    def boom(url):
        raise OSError("timed out")

    monkeypatch.setattr(geo, "_next_hop", boom)
    with pytest.raises(geo.LocationError, match="Qisqa havola"):
        geo.resolve_location("https://maps.app.goo.gl/abc")


def test_resolve_empty_is_none_and_garbage_raises():
    assert geo.resolve_location("  ") is None
    with pytest.raises(geo.LocationError):
        geo.resolve_location("Chilonzor 5-kvartal")


@pytest.mark.parametrize("text, expected", [
    # A route: the start is where the sender stood, the client is where it ends.
    ("https://yandex.com/navi?rtext=41.262539,69.213128~41.262545,69.213117&rtt=auto",
     (Decimal("41.262545"), Decimal("69.213117"))),
    ("https://yandex.uz/maps/?rtext=~41.311081%2C69.240562&rtt=auto",
     (LAT, LNG)),
    ("https://yandex.uz/maps/?rtext=41.1,69.1~41.2,69.2~41.311081,69.240562",
     (LAT, LNG)),
    ("yandexnavi://build_route_on_map?lat_to=41.311081&lon_to=69.240562",
     (LAT, LNG)),
    ("https://yandex.ru/navi/?whatshere%5Bpoint%5D=69.240562%2C41.311081",
     (LAT, LNG)),
])
def test_parse_yandex_routes_take_the_destination(text, expected):
    assert geo.parse_location(text) == expected
