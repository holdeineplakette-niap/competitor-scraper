"""Offline tests for vuek.parse against synthetic pages (vuek.simulate): the real pages hold contact data and are not fixtures."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from vuek import checks, parse, simulate

LAGE = simulate.STATIONS[0]
PATH = "/kontakt/pruefstelle-lage/"


def station(variant="", fault=None, st=LAGE):
    return parse.parse_station(simulate.station_html(st, variant, fault), f"/kontakt/pruefstelle-{st[0]}/")


# ------------------------------------------------------------------------------------------------ links
@pytest.mark.parametrize("href,expected", [
    ("/kontakt/pruefstelle-lage/", "/kontakt/pruefstelle-lage/"),
    ("/kontakt/pruefstelle-lage", "/kontakt/pruefstelle-lage/"),
    ("https://www.vuek-gmbh.de/kontakt/pruefstelle-lage/?x=1#top", "/kontakt/pruefstelle-lage/"),
    ("http://vuek-gmbh.de/Kontakt/Pruefstelle-Lage/", "/kontakt/pruefstelle-lage/"),
    ("kontakt/pruefstelle-lage/", "/kontakt/pruefstelle-lage/"),
    ("https://example.org/kontakt/pruefstelle-lage/", None),
    ("mailto:info@example.org", None),
    ("tel:0123", None),
    ("javascript:window.CookieControl.showCookieSettings();", None),
    ("#top", None),
    ("", None),
    (None, None),
])
def test_norm_path(href, expected):
    assert parse.norm_path(href) == expected


@pytest.mark.parametrize("path,is_station", [
    ("/kontakt/pruefstelle-lage/", True),
    ("/kontakt/neue-adresse/", True),  # anything under /kontakt/ counts, so a station with another URL pattern is not missed
    ("/pruefstelle-herford/", True),  # 'pruefstelle' anywhere in the path counts
    ("/kontakt/", False),  # the hub itself
    ("/hauptuntersuchung/", False),
    ("/stellenangebote/kfz-pruefingenieur/", False),
])
def test_is_station_path(path, is_station):
    assert parse.is_station_path(path) is is_station


def test_slug_and_url():
    assert parse.slug_of("/kontakt/pruefstelle-bad-oeynhausen/") == "bad-oeynhausen"
    assert parse.slug_of("/kontakt/neue-adresse/") == "neue-adresse"
    assert parse.station_url("/kontakt/pruefstelle-lage/") == "https://www.vuek-gmbh.de/kontakt/pruefstelle-lage/"


# ------------------------------------------------------------------------------------------------ stated number
@pytest.mark.parametrize("text,values", [
    ("Heute finden Sie uns an fünf eigenen Standorten: in Minden", [5]),
    ("an 5 eigenen Standorten", [5]),
    ("Wir haben sechs eigene Standorte", [6]),
    ("zwölf eigenen Prüfstellen", [12]),
    ("an einem eigenen Standort", [1]),
    ("Heute finden Sie uns an eigenen Standorten", [None]),  # token 'an': not a number -> unreadable
    ("an mehreren eigenen Standorten", [None]),
    ("nichts über Standorte", []),
])
def test_stated_counts(text, values):
    assert [s["value"] for s in parse.stated_counts(text)] == values


# ------------------------------------------------------------------------------------------------ hours
def test_normalize_hours_plain_and_notes():
    text, unparsed = parse.normalize_hours(["Montag bis Freitag", "09.00 bis 12.00 Uhr", "12.30 bis 17.00 Uhr", "Samstag",
                                            "08.00 bis 12.00 Uhr"])
    assert text == "Mo-Fr 09:00-12:00, 12:30-17:00; Sa 08:00-12:00" and unparsed == []
    text, unparsed = parse.normalize_hours(["Montag bis Freitag", "09.00 bis 12.30 Uhr (außer Do)", "13.00 bis 17.00 Uhr", "Donnerstag",
                                            "09.00 bis 12.00 Uhr", "13.00 bis 17.00 Uhr"])
    assert text == "Mo-Fr 09:00-12:30 (außer Do), 13:00-17:00; Do 09:00-12:00, 13:00-17:00" and unparsed == []


def test_normalize_hours_keeps_what_it_cannot_read():
    text, unparsed = parse.normalize_hours(["Montag bis Freitag", "nach Vereinbarung"])
    assert unparsed == ["nach Vereinbarung", "Mo-Fr"]  # the line and the label that stayed empty
    _, unparsed = parse.normalize_hours(["09.00 bis 12.00 Uhr"])  # times before any day label
    assert unparsed == ["09.00 bis 12.00 Uhr"]
    _, unparsed = parse.normalize_hours(["Montag", "25.00 bis 12.00 Uhr"])
    assert "25.00 bis 12.00 Uhr" in unparsed
    text, unparsed = parse.normalize_hours(["Mo.-Fr.", "8:00 - 17:30", "Sa", "geschlossen"])
    assert text == "Mo-Fr 08:00-17:30; Sa geschlossen" and unparsed == []


# ------------------------------------------------------------------------------------------------ station page
def test_parse_station_fields():
    p = station()
    assert p["name"] == "VÜK-Prüfstelle Lage" and p["slug"] == "lage"
    assert (p["street"], p["zip"], p["city"]) == ("Musterweg 1", "32791", "Lage") and p["address_ok"]
    assert p["phone"] == "05200 100001" and p["email"] == "lage@example.org" and p["fax"] == ""
    assert p["opening_hours"] == "Mo-Fr 09:00-12:00, 12:30-17:00; Sa 08:00-12:00"
    assert p["opening_hours_raw"].startswith("Montag bis Freitag | 09.00 bis 12.00 Uhr")
    assert p["hours_heading"] == "Öffnungszeiten (ohne Termin)"
    assert p["services"] == "Hauptuntersuchung (HU); Änderungsabnahme; Weitere Fahrzeugprüfungen"
    assert p["hu_on_page"] and p["warnings"] == [] and p["unparsed_lines"] == []
    assert "­" not in p["services_text"]  # soft hyphens are dropped
    assert p["map_query"] == "Musterweg 1, Lage, Deutschland"
    assert p["page_lat"] is None


def test_red_text_before_the_hours_is_a_notice_and_not_part_of_the_address():
    p = station()
    assert p["notice"] == "Bitte beachten: Sondertag 99.99."
    assert p["phone"] == "05200 100001" and p["street"] == "Musterweg 1"
    assert station(st=simulate.STATIONS[1])["notice"] == ""


def test_red_text_inside_the_hours_stays_in_the_hours():
    html = simulate.station_html(LAGE).replace(
        "09.00 bis 12.00 Uhr</p>", '09.00 bis <span style="color: #ff0000;">11.00 Uhr </span></p>', 1)
    p = parse.parse_station(html, PATH)
    assert p["hours_highlight"] == "11.00 Uhr"
    assert p["opening_hours"].startswith("Mo-Fr 09:00-11:00, 12:30-17:00")
    assert checks.station_flags(p, {}, {}).count("hours_highlighted") == 1


def test_missing_address_is_not_ok_and_says_why():
    p = station(fault="no_address", st=simulate.STATIONS[3])
    assert not p["address_ok"] and p["zip"] == "" and any("PLZ" in w for w in p["warnings"])
    p = station(fault="no_street", st=simulate.STATIONS[3])
    assert not p["address_ok"] and p["street"] == "" and (p["zip"], p["city"]) == ("32427", "Minden")


def test_a_page_without_a_content_area_is_not_ok():
    p = parse.parse_station("<html><body><p>Wartungsarbeiten</p></body></html>", PATH)
    assert not p["address_ok"] and p["warnings"] == ["no content area (div#content_area) on the page"]


def test_unknown_lines_are_kept_and_flagged():
    html = simulate.station_html(LAGE).replace("<p>Mail:", "<p>Fax: 05200 555</p><p>Zentrale Rufnummer im Notfall</p><p>Mail:")
    p = parse.parse_station(html, PATH)
    assert p["fax"] == "05200 555" and p["unparsed_lines"] == ["Zentrale Rufnummer im Notfall"]
    assert "unparsed_lines" in checks.station_flags(p, {}, {})


def test_email_decoding_and_problems():
    html = simulate.station_html(LAGE).replace('data-cfemail="', 'data-cfemail="zz', 1)
    p = parse.parse_station(html, PATH)
    assert p["email"] == "" and any("e-mail decode failed" in w for w in p["warnings"])
    html = re.sub(r'<a class="__cf_email__".*?</a>', '<a href="mailto:direkt@example.org">direkt@example.org</a>', simulate.station_html(LAGE))
    assert parse.parse_station(html, PATH)["email"] == "direkt@example.org"
    html = re.sub(r'<a class="__cf_email__".*?</a>', '<a class="__cf_email__" href="/cdn-cgi/l/email-protection">[email protected]</a>',
                  simulate.station_html(LAGE))
    p = parse.parse_station(html, PATH)
    assert p["email"] == "" and p["warnings"] == ["an obfuscated e-mail link without hex data"]


def test_content_hash_ignores_the_per_request_email_key_but_sees_real_changes():
    a, b = station(), station()
    assert a["content_hash"] == b["content_hash"]  # cf_encode uses a fresh random key per call
    assert station("v2")["content_hash"] != a["content_hash"]
    assert station(st=simulate.STATIONS[1])["content_hash"] != a["content_hash"]


def test_page_coordinates_are_read_when_present_and_ignored_outside_germany():
    p = station(fault="page_coords")
    assert (p["page_lat"], p["page_lng"]) == (51.99, 8.79) and p["page_coord_source"] == "map link @lat,lng"
    far = simulate.station_html(LAGE, fault="page_coords").replace("@51.990000,8.790000", "@-33.868800,151.209300")
    assert parse.parse_station(far, PATH)["page_lat"] is None


# ------------------------------------------------------------------------------------------------ flags
def test_station_flags():
    p = station()
    assert checks.station_flags(p, {"05200100001": 1}, {}) == ["notice_on_page"]
    shared = checks.station_flags(p, {"05200100001": 5}, {parse.normalize_street("Musterweg 132791"): 2})
    assert "phone_shared_with_other_stations" in shared and "duplicate_address" in shared
    q = station(fault="no_hu", st=simulate.STATIONS[3])
    assert "hu_not_on_page" in checks.station_flags(q, {}, {}) and "hu_not_on_page" in checks.REVIEW_FLAGS
    r = dict(p, title="VÜK-Prüfstelle Lage, Andere Straße 9 - Ihre VÜK in OWL", map_query="Andere Straße 9, Lage, Deutschland")
    assert {"title_street_mismatch", "map_address_mismatch"} <= set(checks.station_flags(r, {}, {}))
    assert parse.normalize_street("Alfred-Krupp-Str. 2a") == parse.normalize_street("Alfred-Krupp-Straße 2a")


# ------------------------------------------------------------------------------------------------ /ueber-uns/ and sitemaps
def test_parse_about_reads_menu_footer_body_and_the_stated_number():
    a = parse.parse_about(simulate.about_html())
    five = [f"/kontakt/pruefstelle-{s[0]}/" for s in simulate.STATIONS]
    assert a["menu_set"] == sorted(five) and a["footer_set"] == sorted(five) and a["body_set"] == sorted(five)
    assert len(a["menus"]) == 2 and a["footer_blocks"] == 1  # desktop and mobile navigation; the sub-navigation has no Kontakt menu
    assert [s["value"] for s in a["stated_meta"]] == [5] and [s["value"] for s in a["stated_body"]] == [5, 5]
    assert "Partnerwerkstätten" in a["partner_workshop_text"]


@pytest.mark.parametrize("fault,field,expected_missing", [
    ("footer_missing", "footer_set", "/kontakt/pruefstelle-kirchlengern/"),
    ("body_missing", "body_set", "/kontakt/pruefstelle-kirchlengern/"),
])
def test_parse_about_sees_a_dropped_link(fault, field, expected_missing):
    assert expected_missing not in parse.parse_about(simulate.about_html(fault))[field]


def test_parse_about_menu_six_and_no_footer_block():
    assert "/kontakt/pruefstelle-herford/" in parse.parse_about(simulate.about_html("menu_six"))["menu_set"]
    assert parse.parse_about(simulate.about_html("no_footer_block"))["footer_blocks"] == 0


def test_parse_sitemap_xml():
    urls = parse.parse_sitemap_xml(simulate.sitemap_xml())
    assert len(urls) == 11 and parse.station_paths_of_urls(urls) == sorted(f"/kontakt/pruefstelle-{s[0]}/" for s in simulate.STATIONS)
    with pytest.raises(ET.ParseError):
        parse.parse_sitemap_xml("<html><body>not a sitemap")
    with pytest.raises(ValueError, match="sitemap index"):
        parse.parse_sitemap_xml('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>x</loc></sitemap></sitemapindex>')


def test_parse_link_page_and_hu_page():
    sp = parse.parse_link_page(simulate.sitemap_page_html())
    assert sp["content_found"] and len(sp["station_set"]) == 5 and "/kontakt/" in sp["paths"]
    assert parse.parse_link_page("<html><body>x</body></html>")["content_found"] is False
    assert parse.parse_hu_page(simulate.hu_html())["mentions_hu"] is True
    assert parse.parse_hu_page("<html><body><div id='content_area'>nichts</div></body></html>")["mentions_hu"] is False


def test_discovery_checks_name_the_difference():
    about = parse.parse_about(simulate.about_html("footer_missing"))
    out, expected = checks.discovery_checks(about, parse.parse_sitemap_xml(simulate.sitemap_xml()), parse.parse_link_page(simulate.sitemap_page_html()), None)
    bad = {c["check"]: c["detail"] for c in out if not c["ok"] and c["severity"] == "hard"}
    assert list(bad) == ["menu == footer"] and "kirchlengern" in bad["menu == footer"] and "only in footer: -" in bad["menu == footer"]
    assert len(expected) == 5
