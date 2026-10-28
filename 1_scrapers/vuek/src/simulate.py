"""A fake vuek-gmbh.de for `mode="simulated"` and for tests: same Jimdo markup as the real site, synthetic data, no network.

Known truth: 5 own stations (real PLZ, invented streets, phone numbers and example.org addresses), stated count "fünf".
`fault` switches on a defect that a run must NOT pass:
  menu_six            the menu (both navigations) shows a sixth station link
  footer_missing      the footer list lacks one station
  body_missing        the body text of /ueber-uns/ lacks one station link
  stated_sechs        the meta description says "sechs eigenen Standorten" (and the body still says fünf)
  stated_unreadable   the meta description has no number ("an eigenen Standorten")
  stated_body_sechs   the meta description says fünf, the body text says sechs
  no_footer_block     the "VÜK Standorte" footer block is gone
  sitemap_xml_extra   the XML sitemap lists a sixth station page
  sitemap_xml_missing the XML sitemap lacks one station page
  sitemap_page_extra  the HTML sitemap lists a sixth station page
  sitemap_xml_broken  the XML sitemap is not XML
  no_address          one station page has no PLZ/city line
  no_street           one station page has PLZ and city but no street
  station_404         one station page answers 404
  blocked             a Cloudflare challenge page from the third request on
  no_hu               one station page does not mention the Hauptuntersuchung (a review finding, not a FAIL)
  page_coords         the station pages carry coordinates in the map link (coord_source "page")
  sixth_consistent    a sixth station that is listed everywhere and counted ("sechs"): all sources agree, only the
                      last passing full run differs
  (the Simulated geocoder has its own faults: no_result, fallback, far, plz_mismatch)
`variant="v2"` changes Minden's opening hours and Lage's phone number (for change detection).
"""

from __future__ import annotations

import random
from datetime import datetime

from vuek.fetch import BASE, BlockedError, FetchError, Response

ROBOTS = "User-agent: *\nDisallow: /app/\nDisallow: /j/\nCrawl-Delay: 5\nSitemap: https://www.vuek-gmbh.de/sitemap.xml\n"

# slug, h1 city, title city, street, PLZ, city line, phone, mail, (lat, lng) of the simulated geocoder
STATIONS = [
    ("lage", "Lage", "Lage", "Musterweg 1", "32791", "Lage", "05200 100001", "lage@example.org", (51.99, 8.79)),
    ("bielefeld", "Bielefeld", "Bielefeld-Brackwede", "Probestraße 2", "33647", "Bielefeld-Brackwede", "05200 100002",
     "bielefeld@example.org", (51.98, 8.52)),
    ("bad-oeynhausen", "Bad Oeynhausen", "Bad Oeynhausen", "Beispiel-Allee 3", "32547", "Bad Oeynhausen-Rehme",
     "05200 100003", "oeynhausen@example.org", (52.21, 8.80)),
    ("minden", "Minden", "Minden", "Simulationsring 4", "32427", "Minden", "05200 100004", "minden@example.org", (52.29, 8.92)),
    ("kirchlengern", "Kirchlengern", "Kirchlengern", "Testgasse 5a", "32278", "Kirchlengern", "05200 100005",
     "kirchlengern@example.org", (52.16, 8.63)),
]
EXTRA = ("herford", "Herford", "Herford", "Fakestraße 6", "32052", "Herford", "05200 100006", "herford@example.org", (52.11, 8.67))
EXPECTED_STATIONS = len(STATIONS)
SIM_COORDS = {s[4]: s[8] for s in STATIONS + [EXTRA]}


def cf_encode(text: str, key: int | None = None) -> str:
    """Cloudflare e-mail obfuscation with a random key, as on the real site (the hex differs on every request)."""
    key = random.randrange(1, 256) if key is None else key
    return (bytes([key]) + bytes(b ^ key for b in text.encode())).hex()


def _path(slug: str) -> str:
    return f"/kontakt/pruefstelle-{slug}/"


def _menu_html(slugs: list[str]) -> str:
    items = "".join(f'<li class="jmd-nav__list-item-1"><a href="{_path(s)}" data-link-title="Prüfstelle {s}">Prüfstelle {s}</a></li>'
                    for s in slugs)
    return ('<div data-container="navigation"><div class="j-nav-variant-nested"><ul class="cc-nav-level-0">'
            '<li><a href="/" data-link-title="Home">Home</a></li>'
            '<li class="j-nav-has-children"><a href="/leistungen/">Leistungen</a>'
            '<ul class="cc-nav-level-1"><li><a href="/hauptuntersuchung/">Hauptuntersuchung</a></li></ul></li>'
            f'<li class="j-nav-has-children"><a href="/kontakt/" data-link-title="Kontakt">Kontakt</a><ul class="cc-nav-level-1">{items}</ul></li>'
            "</ul></div></div>")


def _footer_html(slugs: list[str]) -> str:
    items = "".join(f'<li><a href="{_path(s)}" title="Prüfstelle {s}">{s}</a></li>' for s in slugs)
    return f'<div class="j-module n j-text"><p><strong>VÜK Standorte</strong></p><ul>{items}</ul></div>'


def _hours(variant: str, slug: str) -> str:
    if variant == "v2" and slug == "minden":
        return "<em><br/>Montag bis Freitag</em><br/>08.00 bis 12.00 Uhr</p><p>13.00 bis 18.00 Uhr"
    return "<em><br/>Montag bis Freitag</em><br/>09.00 bis 12.00 Uhr</p><p>12.30 bis 17.00 Uhr</p><p></p><p><em>Samstag</em><br/>08.00 bis 12.00 Uhr"


def station_html(st: tuple, variant: str = "", fault: str | None = None) -> str:
    slug, city_h1, city_title, street, plz, city_line, phone, mail, _ = st
    if variant == "v2" and slug == "lage":
        phone = "05200 999999"
    address = f"<p>{street}</p><p>{plz} {city_line}</p>"
    if fault == "no_address" and slug == "minden":
        address = ""
    if fault == "no_street" and slug == "minden":
        address = f"<p>{plz} {city_line}</p>"
    hu = "" if (fault == "no_hu" and slug == "minden") else '<a href="/hauptuntersuchung/" title="Hauptuntersuchung">Hauptuntersuchungen (HU)</a> '
    notice = '<span style="color: #cc0000;">Bitte beachten: Sondertag 99.99.</span>' if slug == "lage" else ""
    coords = ""
    if fault == "page_coords":
        lat, lng = st[8]
        coords = f'<a href="https://www.google.com/maps/@{lat:.6f},{lng:.6f},17z">Karte</a>'
    return (
        f'<html><head><title>VÜK-Prüfstelle {city_title}, {street} - Ihre VÜK in OWL</title>'
        '<meta name="description" content="In unserer VÜK-Prüfstelle führen wir Hauptuntersuchungen durch."/></head><body>'
        f'<div data-container="navigation">{_menu_html([s[0] for s in STATIONS])}</div>'
        '<div data-container="content" id="content_area"><div class="j-module n j-spacing"></div>'
        f'<div class="j-module n j-header"><h1>VÜK-Prüfstelle {city_h1}</h1></div>'
        f'<div class="j-module n j-text"><p>In unserer VÜK-Prüfstelle in {city_h1} führen wir {hu}genauso durch, wie die '
        "Abnahme von Fahrzeug­änderungen sowie weitere Fahrzeug­prüfungen.</p><p></p>"
        f"{address}<p>Tel.: {phone}{notice}</p>"
        f'<p>Mail: <a class="__cf_email__" data-cfemail="{cf_encode(mail)}" href="/cdn-cgi/l/email-protection">[email protected]</a></p>'
        '<h3><span style="font-size: 24px;">Öffnungszeiten (ohne Termin)</span></h3>'
        f"<p>{_hours(variant, slug)}</p></div>"
        f'<div class="j-module n j-header"><h2>Anfahrt</h2></div>{coords}'
        '<div class="j-module n j-googlemaps"><div class="cc-map-wrapper">'
        f'<iframe class="cc-map-embed-iframe" data-src="https://www.google.com/maps/embed/v1/place?key=SIMKEY&amp;q={street.replace(" ", "+")}%2C+{city_h1.replace(" ", "+")}%2C+Deutschland&amp;maptype=roadmap"></iframe>'
        '<div class="cc-map-embed-placeholder"><span>Inhalte von Google Maps werden nicht angezeigt.</span></div></div></div>'
        "</div></body></html>"
    )


def about_html(fault: str | None = None) -> str:
    six = fault == "sixth_consistent"
    slugs = [s[0] for s in STATIONS] + ([EXTRA[0]] if six else [])
    menu_slugs = slugs + ([EXTRA[0]] if fault == "menu_six" else [])
    footer_slugs = slugs[:-1] if fault == "footer_missing" else slugs
    body_slugs = slugs[:-1] if fault == "body_missing" else slugs
    stated = {"stated_sechs": "sechs", "stated_unreadable": "", "stated_body_sechs": "fünf"}.get(fault, "sechs" if six else "fünf")
    stated_body = "sechs" if fault == "stated_body_sechs" or six else "fünf"
    meta = f"Heute finden Sie uns an {stated + ' ' if stated else ''}eigenen Standorten: in Lage und Minden."
    body_links = ", ".join(f'<a href="{_path(s)}" title="Prüfstelle {s}">{s}</a>' for s in body_slugs)
    footer = "" if fault == "no_footer_block" else _footer_html(footer_slugs)
    return (
        f'<html><head><title>Fahrzeugprüfungen seit 1968 - Ihre VÜK in OWL</title><meta name="description" content="{meta}"/></head><body>'
        f"{_menu_html(menu_slugs)}{_menu_html(menu_slugs)}"
        '<div data-container="navigation"><div class="j-nav-variant-nested"><ul><li><a href="/impressum/">Impressum</a></li></ul></div></div>'
        '<div data-container="content" id="content_area"><div class="j-module n j-text">'
        f"<p>Die VÜK ist in Ostwestfalen-Lippe zu Hause. Heute finden Sie uns an {stated_body} eigenen Standorten: in {body_links}. "
        "Zudem sind wir in zahlreichen Partnerwerkstätten in OWL vertreten.</p>"
        f"<p>Sie finden uns an {stated_body} eigenen Standorten und in vielen Partnerwerkstätten in OWL.</p></div></div>"
        f"{footer}</body></html>"
    )


def sitemap_xml(fault: str | None = None) -> str:
    slugs = [s[0] for s in STATIONS]
    if fault in ("sitemap_xml_extra", "sixth_consistent"):
        slugs.append(EXTRA[0])
    if fault == "sitemap_xml_missing":
        slugs = slugs[:-1]
    urls = ["/", "/leistungen/", "/hauptuntersuchung/", "/ueber-uns/", "/kontakt/", *[_path(s) for s in slugs], "/about/"]
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{BASE}{u}</loc></url>" for u in urls) + "</urlset>")


def sitemap_page_html(fault: str | None = None) -> str:
    slugs = [s[0] for s in STATIONS] + ([EXTRA[0]] if fault in ("sitemap_page_extra", "sixth_consistent") else [])
    urls = ["/", "/leistungen/", "/hauptuntersuchung/", "/ueber-uns/", "/kontakt/", *[_path(s) for s in slugs]]
    links = "".join(f'<li><a href="{u}">{u}</a></li>' for u in urls)
    return f'<html><body><div data-container="content" id="content_area"><div class="j-module n j-sitemap"><ul>{links}</ul></div></div></body></html>'


def kontakt_html() -> str:
    links = "".join(f'<p><a href="{_path(s[0])}">{s[1]}</a></p>' for s in STATIONS)
    return f'<html><body><div data-container="content" id="content_area"><div class="j-module n j-text"><h1>Kontakt</h1>{links}</div></div></body></html>'


def hu_html() -> str:
    return ('<html><body><div data-container="content" id="content_area"><div class="j-module n j-text"><h1>Hauptuntersuchung</h1>'
            "<p>Die Hauptuntersuchung (HU) führen wir in allen Prüfstellen durch: Lage, Bielefeld, Bad Oeynhausen, Minden, Kirchlengern.</p>"
            "</div></div></body></html>")


class SimulatedVuek:
    """Same interface as VuekClient.get(url, params)."""

    def __init__(self, fault: str | None = None, variant: str = ""):
        self.fault, self.variant = fault, variant
        self.requests_made = 0
        self.urls: list[str] = []

    def _resp(self, url: str, text: str, ctype: str = "text/html; charset=UTF-8") -> Response:
        return Response(url=url, status=200, text=text, elapsed_s=0.0, attempts=1, content_type=ctype)

    def get(self, url: str, params: dict | None = None) -> Response:
        self.requests_made += 1
        self.urls.append(url)
        if self.fault == "blocked" and self.requests_made >= 3:
            raise BlockedError(f"{url} was blocked (HTTP 403; 'Just a moment' page)")
        path = url.removeprefix(BASE)
        if path == "/robots.txt":
            return self._resp(url, ROBOTS, "text/plain")
        if path == "/ueber-uns/":
            return self._resp(url, about_html(self.fault))
        if path == "/sitemap/":
            return self._resp(url, sitemap_page_html(self.fault))
        if path == "/sitemap.xml":
            if self.fault == "sitemap_xml_broken":
                return self._resp(url, "<html><body>this is not a sitemap", "text/xml")
            return self._resp(url, sitemap_xml(self.fault), "text/xml")
        if path == "/kontakt/":
            return self._resp(url, kontakt_html())
        if path == "/hauptuntersuchung/":
            return self._resp(url, hu_html())
        for st in STATIONS + [EXTRA]:
            if path == _path(st[0]):
                if self.fault == "station_404" and st[0] == "minden":
                    raise FetchError(f"{url} returned HTTP 404")
                return self._resp(url, station_html(st, self.variant, self.fault))
        raise FetchError(f"{url} returned HTTP 404")


class SimulatedGeocoder:
    """Same interface as NominatimClient.geocode(street, plz, city). Coordinates near the real PLZ areas."""

    def __init__(self, fault: str | None = None):
        self.fault = fault
        self.requests_made = 0
        self.calls: list[tuple[str, str, str]] = []

    def geocode(self, street: str, plz: str, city: str) -> dict:
        self.requests_made += 1
        self.calls.append((street, plz, city))
        lat_lng = SIM_COORDS.get(plz)
        if lat_lng is None or self.fault == "no_result" or (self.fault == "fallback" and city and "-" in city):
            return {"lat": None, "lng": None, "display_name": "", "raw": []}
        lat, lng = lat_lng
        if self.fault == "far":
            lat += 1.5
        shown = "00000" if self.fault == "plz_mismatch" else plz
        name = f"{street}, {city or 'Ort'}, {shown}, Deutschland"
        return {"lat": lat, "lng": lng, "display_name": name,
                "raw": [{"lat": str(lat), "lon": str(lng), "display_name": name, "simulated": True,
                         "stamp": datetime(2026, 10, 6).isoformat()}]}
