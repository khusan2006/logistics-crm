"""A mijoz's joylashuv read out of whatever the operator has to hand.

People send a location as a Google or Yandex Maps link, a 2GIS or Apple link, a
`geo:` URI, or the bare "41.311081, 69.240562" a map app copies. Rather than
teaching the operator one format, `parse_location` takes any of them and returns
the point — or None when there is no point in it to be found.

Short links (maps.app.goo.gl, yandex.uz/maps/-/…) carry no coordinates at all;
they are a redirect to a link that does. `resolve_location` follows that redirect,
one hop at a time and only between map hosts, reading each Location header without
ever downloading the page behind it.
"""

import http.client
import re
import urllib.error
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import parse_qs, unquote, urljoin, urlparse

#: Six places is ~11 cm — finer than any phone's GPS, and what the columns hold.
PLACES = Decimal("0.000001")

_NUM = r"-?\d{1,3}(?:\.\d+)?"
_PAIR = re.compile(rf"({_NUM})\s*,[\s+]*({_NUM})")
#: "41.31, 69.24", "41.31 69.24", "41.31;69.24"
_PLAIN_DOT = re.compile(rf"^\s*({_NUM})\s*[,;\s]\s*({_NUM})\s*$")
#: "41,311081 69,240562" — a comma is the decimal mark here, so a phone keyboard
#: may well produce this.
_PLAIN_COMMA = re.compile(r"^\s*(-?\d{1,3}),(\d+)\s*[,;\s]\s*(-?\d{1,3}),(\d+)\s*$")
#: 41°18'39.9"N 69°14'26.0"E — Google's own "copy coordinates" in DMS mode.
_DMS = re.compile(
    r"(\d{1,3})\s*°\s*(?:(\d{1,2})\s*['′]\s*)?(?:(\d{1,2}(?:[.,]\d+)?)\s*[\"″]\s*)?([NSEW])",
    re.IGNORECASE)
_GOOGLE_PLACE = re.compile(rf"!3d({_NUM})!4d({_NUM})")
_GOOGLE_AT = re.compile(rf"@({_NUM}),({_NUM})")

#: Hosts whose links are only a redirect to the real one.
SHORT_HOSTS = ("maps.app.goo.gl", "goo.gl", "go.2gis.com")
#: Hosts a redirect is allowed to hop through. Nothing else is ever requested, so a
#: pasted link cannot make the server fetch an arbitrary address.
MAP_HOSTS = SHORT_HOSTS + ("google.com", "google.uz", "google.ru", "yandex.uz",
                           "yandex.ru", "yandex.com", "yandex.kz", "2gis.uz",
                           "2gis.ru", "2gis.kz")
#: Each hop may wait TIMEOUT seconds inside a request; three keeps the worst case
#: well under gunicorn's 30 s worker timeout.
MAX_HOPS = 3
TIMEOUT = 5

NOT_FOUND = ("Joylashuv tanilmadi. Xaritadan tanlang yoki koordinatalarni yozing "
             "(masalan 41.311081, 69.240562).")
SHORT_LINK_FAILED = ("Qisqa havolani ochib bo'lmadi. Xaritadan tanlang yoki havolani "
                     "xarita ilovasida ochib, to'liq havolasini nusxalang.")


class LocationError(ValueError):
    """The text was a location we could not read — the message is for the operator."""


def _point(lat, lng):
    """The pair as Decimals, or None when it is not a point on Earth.

    (0, 0) is refused along with the out-of-range: it is in the Gulf of Guinea, and
    what it really means is a map app that had no fix yet."""
    try:
        lat, lng = Decimal(str(lat)), Decimal(str(lng))
    except ArithmeticError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return (lat.quantize(PLACES, ROUND_HALF_UP), lng.quantize(PLACES, ROUND_HALF_UP))


def _pair(text, lng_first=False):
    """The first "a,b" in `text` as a point. Yandex and 2GIS write longitude first."""
    match = _PAIR.search(text or "")
    if not match:
        return None
    a, b = match.groups()
    return _point(b, a) if lng_first else _point(a, b)


def _host_is(host, *names):
    return any(host == n or host.endswith("." + n) for n in names)


def _from_plain(text):
    match = _PLAIN_DOT.match(text)
    if match:
        return _point(*match.groups())
    match = _PLAIN_COMMA.match(text)
    if match:
        a, af, b, bf = match.groups()
        return _point(f"{a}.{af}", f"{b}.{bf}")
    return _from_dms(text)


def _from_dms(text):
    parts = _DMS.findall(text)
    if len(parts) != 2:
        return None
    values = {}
    for deg, minutes, seconds, hemi in parts:
        value = (Decimal(deg) + Decimal(minutes or 0) / 60
                 + Decimal((seconds or "0").replace(",", ".")) / 3600)
        hemi = hemi.upper()
        if hemi in "SW":
            value = -value
        values["lat" if hemi in "NS" else "lng"] = value
    if set(values) != {"lat", "lng"}:
        return None
    return _point(values["lat"], values["lng"])


def _from_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    path = unquote(parsed.path)

    if (_host_is(host, "yandex.uz", "yandex.ru", "yandex.com", "yandex.kz", "yandex.by")
            or parsed.scheme in ("yandexnavi", "yandexmaps")):
        # The pin before the map centre: `ll` is only where the map was scrolled to.
        for key in ("whatshere[point]", "pt"):
            point = _pair(query.get(key), lng_first=True)
            if point:
                return point
        # A route (Navigator's "share", or Maps' "Marshrut"): the stops run from~to,
        # and the mijoz is where it ENDS — the start is only where the sender stood.
        # Unlike every other Yandex parameter, rtext writes latitude first.
        stops = [s for s in (query.get("rtext") or "").split("~") if s.strip()]
        if stops:
            point = _pair(stops[-1])
            if point:
                return point
        if "lat_to" in query and "lon_to" in query:
            return _point(query["lat_to"], query["lon_to"])
        return _pair(query.get("ll"), lng_first=True)

    if _host_is(host, "2gis.uz", "2gis.ru", "2gis.kz", "2gis.com"):
        point = _pair(query.get("m"), lng_first=True)
        if point:
            return point
        match = re.search(rf"/geo/({_NUM}),({_NUM})", path)
        return _point(match.group(2), match.group(1)) if match else None

    if _host_is(host, "openstreetmap.org"):
        if "mlat" in query and "mlon" in query:
            return _point(query["mlat"], query["mlon"])
        match = re.search(rf"map=\d+/({_NUM})/({_NUM})", parsed.fragment)
        return _point(*match.groups()) if match else None

    if _host_is(host, "maps.apple.com"):
        for key in ("coordinate", "ll", "q", "sll", "daddr"):
            point = _pair(query.get(key))
            if point:
                return point
        return None

    if "google." in host:
        # The place's own pin before the viewport's `@` — `@` is where the map was
        # looking, and on a place page that can be streets away from the place.
        match = _GOOGLE_PLACE.search(url)
        if match:
            return _point(*match.groups())
        for key in ("q", "query", "ll", "center", "destination", "daddr"):
            point = _pair(query.get(key))
            if point:
                return point
        match = _GOOGLE_AT.search(url)
        if match:
            return _point(*match.groups())
        return _pair(path)

    return None


def parse_location(text):
    """(lat, lng) as Decimals from any link or coordinates the operator pasted, or
    None when there is no point in it. Makes no network calls — a short link is
    `resolve_location`'s job."""
    text = (text or "").strip()
    if not text:
        return None
    # A link is often shared with a line of text before it ("Мы здесь: https://…").
    link = re.search(r"(?:https?://|yandex(?:navi|maps)://|geo:)\S+", text, re.IGNORECASE)
    if link:
        found = link.group(0)
        if found.lower().startswith("geo:"):
            return _pair(found[4:])
        return _from_url(found)
    return _from_plain(text)


def is_short_link(text):
    link = re.search(r"https?://\S+", text or "")
    if not link:
        return False
    host = (urlparse(link.group(0)).hostname or "").lower()
    if _host_is(host, *SHORT_HOSTS):
        return True
    return host.startswith("yandex.") and urlparse(link.group(0)).path.startswith("/maps/-/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _next_hop(url):
    """Where `url` redirects to, or None. Only the headers are looked at."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GranulaLog"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            location = response.headers.get("Location")
    except urllib.error.HTTPError as err:
        location = err.headers.get("Location") if 300 <= err.code < 400 else None
    return urljoin(url, location) if location else None


def follow_short_link(url):
    """The point behind a short link, hopping redirects between map hosts only."""
    for _ in range(MAX_HOPS):
        host = (urlparse(url).hostname or "").lower()
        if not _host_is(host, *MAP_HOSTS):
            return None
        url = _next_hop(url)
        if not url:
            return None
        point = parse_location(url)
        if point:
            return point
    return None


def resolve_location(text):
    """(lat, lng) for what the operator typed; None for an empty box.

    Raises LocationError with a message for the operator when the text is not
    empty but no point could be read from it."""
    text = (text or "").strip()
    if not text:
        return None
    point = parse_location(text)
    if point:
        return point
    if is_short_link(text):
        url = re.search(r"https?://\S+", text).group(0)
        try:
            point = follow_short_link(url)
        except (OSError, ValueError, http.client.HTTPException):
            point = None
        if point:
            return point
        raise LocationError(SHORT_LINK_FAILED)
    raise LocationError(NOT_FOUND)
