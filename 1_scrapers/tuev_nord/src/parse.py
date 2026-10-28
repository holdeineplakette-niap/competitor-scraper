"""Parsers for the TÜV NORD station finder. Pure functions, no network.

Sources (all plain HTML, TYPO3):
  /de/stationen/                          map div `[data-station-map="station-results"]`, attribute `data-locations` = JSON list,
                                          plus the text "<N> Stationen gefunden"
  /de/stationen/liste-aller-stationen/    A-Z list: `a[data-list-teaser]` per station page (name + link, nothing else)
  /de/stationen/<slug>/                   detail page (address, opening hours, services, partner note)
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

BASE = "https://www.tuev-nord.de"
STATIONS_PATH = "/de/stationen/"
LIST_PATH = "/de/stationen/liste-aller-stationen/"
LISTED_BY = "TÜV NORD"
SOURCE = "TUEV_NORD"

EXPECTED_FIELDS = ("id", "name", "street", "plz", "city", "latitude", "longitude", "serviceType", "link", "isOffice")
SERVICE_TYPES = ("HU", "HU-UNG", "UNG")  # the three values seen; the page must contain all of them
HU_SERVICE_TYPES = ("HU", "HU-UNG")
GERMANY_BOX = (47.2, 55.1, 5.8, 15.1)  # lat_min, lat_max, lng_min, lng_max

SITE_TYPES = ("office", "partner_station", "own_station_probable", "unclassified")

# words that make a bracket / comma part or a plain name look like a company, an engineering office or an operator
COMPANY_RX = re.compile(
    r"(?<!\w)(?:gmbh|mbh|kg|gbr|egbr|ug|ag|e\.?\s?k\.?|co\.?|ingenieur\w*|ing\.?|dipl\.?|b(?:ü|ue)ro|sachverst(?:ä|ae)ndig\w*|gutachte\w*|"
    r"autohaus|kfz|fa\.|(?:ü|ue)o|pr(?:ü|ue)f\w*|center|centre|zentrum|\w*technik|fahrzeug\w*|experts?|sv|automobil\w*|"
    r"st(?:ü|ue)tzpunkt|gel(?:ä|ae)nde|werkstatt|partner|bei|mobility|auto\w*)(?!\w)|&",
    re.IGNORECASE)
# stricter list for plain names (a plain name is "own" unless it carries one of these)
PLAIN_COMPANY_RX = re.compile(
    r"(?<!\w)(?:gmbh|mbh|kg|gbr|egbr|ug|ag|co\.?|ingenieur\w*|ing\.?|dipl\.?|b(?:ü|ue)ro|sachverst(?:ä|ae)ndig\w*|gutachte\w*|"
    r"autohaus|kfz|fa\.|(?:ü|ue)o|pr(?:ü|ue)fzentrum|pr(?:ü|ue)fstelle|center|mobility)(?!\w)",
    re.IGNORECASE)
PSP_RX = re.compile(r"(?<!\w)(?:autohaus|werkstatt|st(?:ü|ue)tzpunkt|gel(?:ä|ae)nde|bei|fa\.|(?:ü|ue)o|landtechnik|auto\w*)(?!\w)", re.IGNORECASE)
BRACKET_RX = re.compile(r"^(?P<place>[^()]*?)\s*\((?P<inner>.*)\)\s*(?P<rest>.*)$")
STATION_LINK_RX = re.compile(r"^/de/stationen/(?:(?P<variant>stationsseiten)/)?(?P<slug>[^/?#]+)/?$")
NON_STATION_SLUGS = {"liste-aller-stationen", "kontakt-aufnehmen", "station-search"}
COUNTER_RX = re.compile(r"(\d[\d.]*)\s+Stationen\s+gefunden")


class PageError(ValueError):
    """The page does not have the structure the adapter depends on. Never turned into '0 stations'."""


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalise_text(text: str) -> str:
    """Casefolded, umlauts and ß transliterated, only letters and digits left (for matching names)."""
    t = clean(text).casefold().replace("ß", "ss")
    t = t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    return re.sub(r"[^a-z0-9]", "", _strip_accents(t))


def normalise_street(street: str) -> str:
    t = normalise_text(street)
    return re.sub(r"(strasse|str)(?=\d|$)", "str", t)


def normalise_address(street: str, plz: str) -> str:
    """`<street without spelling variants>|<plz>`; Straße / Strasse / Str. and punctuation do not matter."""
    return f"{normalise_street(street)}|{clean(plz)}"


# ------------------------------------------------------------------------------------------------ stations page
@dataclass
class StationsPage:
    records: list = field(default_factory=list)
    counters: list = field(default_factory=list)  # every "<N> Stationen gefunden" found, as int
    map_attrs: dict = field(default_factory=dict)  # attributes of the map div except data-locations
    n_map_elements: int = 0
    locations_chars: int = 0


def parse_stations_page(html: str) -> StationsPage:
    soup = BeautifulSoup(html, "html.parser")
    maps = soup.select('[data-station-map="station-results"]')
    if len(maps) != 1:
        raise PageError(f"expected exactly one [data-station-map=\"station-results\"] element, found {len(maps)}")
    el = maps[0]
    raw = el.get("data-locations")
    if raw is None:
        raise PageError("the map element has no data-locations attribute")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PageError(f"data-locations is not valid JSON ({exc})") from exc
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise PageError("data-locations is not a list of objects")
    if not data:
        raise PageError("data-locations is an empty list (an empty answer is an error, not '0 stations')")
    counters = [int(m.replace(".", "")) for m in COUNTER_RX.findall(soup.get_text(" ", strip=True))]
    attrs = {k: (" ".join(v) if isinstance(v, list) else v) for k, v in el.attrs.items() if k != "data-locations"}
    return StationsPage(records=data, counters=counters, map_attrs=attrs, n_map_elements=len(maps), locations_chars=len(raw))


# ------------------------------------------------------------------------------------------------ list page
@dataclass
class ListPage:
    items: list = field(default_factory=list)  # {"slug", "path", "variant", "name", "letter"}
    other_links: list = field(default_factory=list)  # teaser anchors that are not /de/stationen/<slug>/
    letters: list = field(default_factory=list)


def parse_list_page(html: str) -> ListPage:
    soup = BeautifulSoup(html, "html.parser")
    out = ListPage(letters=[b.get("data-anchor-target", "") for b in soup.select("[data-anchor-target]")])
    teasers = soup.select("a[data-list-teaser]")
    if not teasers:
        raise PageError("the list page has no a[data-list-teaser] anchors")
    for a in teasers:
        href = a.get("href", "")
        path = re.sub(r"[?#].*$", "", href)
        m = STATION_LINK_RX.match(path)
        block = a.find_parent(attrs={"data-anchor-target": True})
        if not m or m.group("slug") in NON_STATION_SLUGS:
            out.other_links.append(href)
            continue
        out.items.append({"slug": m.group("slug"), "path": path if path.endswith("/") else path + "/", "variant": m.group("variant") or "",
                          "name": clean(a.get_text(" ", strip=True)), "letter": block.get("data-anchor-target", "") if block else ""})
    return out


# ------------------------------------------------------------------------------------------------ classification
def split_name(name: str, city: str = "") -> dict:
    """Place, operator and the evidence for one non-office station name.

    brackets  'City (Operator)'          -> partner_station, operator = bracket text
    comma     'City, Ingenieurbüro X'    -> partner_station when the trailing part looks like a company, else unclassified
    plain     'City'                     -> own_station_probable (plain names with company words -> unclassified)
    A bracket that repeats the record's own city field ('Halle (Saale)') or has no company word stays honest: see `reason`.
    """
    n = clean(name)
    m = BRACKET_RX.match(n)
    if m and m.group("inner"):
        inner, place, rest = clean(m.group("inner")), clean(m.group("place")), clean(m.group("rest"))
        city_n = normalise_text(city)
        if normalise_text(f"{place} ({inner})") == city_n or normalise_text(inner) == city_n:
            return dict(site_type="own_station_probable", operator_name="", place_name=n, reason="bracket repeats the record's city field (place name, not an operator)")
        if rest:
            return dict(site_type="unclassified", operator_name=inner, place_name=place, reason=f"text after the bracket: {rest!r}")
        if COMPANY_RX.search(inner):
            return dict(site_type="partner_station", operator_name=inner, place_name=place, reason="operator in brackets (company / office word)")
        return dict(site_type="unclassified", operator_name=inner, place_name=place, reason="bracket text has no company word (surname, district or place name?)")
    if "," in n:
        place, _, tail = (clean(x) for x in n.partition(","))
        if tail and COMPANY_RX.search(tail):
            return dict(site_type="partner_station", operator_name=tail, place_name=place, reason="operator after a comma (company / office word)")
        return dict(site_type="unclassified", operator_name=tail, place_name=place, reason="comma, but the trailing part has no company word")
    if not n:
        return dict(site_type="unclassified", operator_name="", place_name="", reason="empty name")
    if PLAIN_COMPANY_RX.search(n):
        return dict(site_type="unclassified", operator_name="", place_name=n, reason="plain name that contains company words (no brackets)")
    return dict(site_type="own_station_probable", operator_name="", place_name=n, reason="plain name")


def station_link(link: str) -> dict:
    """slug / detail_url / variant from a record's `link`; offices (contact link with a volatile token) have none."""
    path = re.sub(r"[?#].*$", "", link or "")
    m = STATION_LINK_RX.match(path)
    if not m or m.group("slug") in NON_STATION_SLUGS:
        return {"station_slug": "", "detail_url": "", "link_variant": "", "link_path": path}
    return {"station_slug": m.group("slug"), "detail_url": BASE + (path if path.endswith("/") else path + "/"),
            "link_variant": m.group("variant") or "standard", "link_path": path}


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_record(rec: dict, scraped_at: str = "") -> dict:
    """One `data-locations` object -> a flat row: every raw field kept, plus the enrichment columns."""
    is_office = rec.get("isOffice") is True
    name_raw = rec.get("name")
    service = clean(rec.get("serviceType"))
    link = station_link(rec.get("link") if isinstance(rec.get("link"), str) else "")
    if is_office:
        split = dict(site_type="office", operator_name="", place_name="", reason="isOffice = true")
    else:
        split = split_name(name_raw, rec.get("city"))
    street, plz = clean(rec.get("street")), clean(rec.get("plz"))
    sid = rec.get("id")
    lat, lng = _to_float(rec.get("latitude")), _to_float(rec.get("longitude"))
    row = {
        "source": SOURCE, "source_id": "" if sid is None else str(sid), "scraped_at": scraped_at,
        "name": name_raw if isinstance(name_raw, str) else "", "name_clean": clean(name_raw),
        "place_name": split["place_name"], "operator_name": split["operator_name"], "site_type": split["site_type"],
        "site_type_reason": split["reason"], "listed_by": LISTED_BY, "service_type": service,
        "is_hu_site": service in HU_SERVICE_TYPES, "is_office": is_office,
        "street": street, "zip": plz, "city": clean(rec.get("city")), "latitude": lat, "longitude": lng,
        "station_slug": link["station_slug"], "detail_url": link["detail_url"], "link_variant": link["link_variant"],
        "link_raw": link["link_path"] if is_office else (rec.get("link") or ""),
        "address_norm": normalise_address(street, plz),
        "psp_suspect": bool(split["operator_name"] and PSP_RX.search(split["operator_name"])),
        "unknown_source_fields": "; ".join(sorted(set(rec) - set(EXPECTED_FIELDS))),
        "_raw_types_ok": isinstance(sid, int) and not isinstance(sid, bool) and isinstance(rec.get("isOffice"), bool)
        and isinstance(rec.get("name"), str) and isinstance(rec.get("plz"), str),
        "_missing_fields": [f for f in EXPECTED_FIELDS if f not in rec],
    }
    row["source_key"] = f"{row['source_id']}|{row['address_norm']}"
    return row


def assign_location_ids(rows: list[dict]) -> None:
    """location_id = TUEV_NORD-<id>; an id that occurs more than once gets -1, -2 ... in order of normalised address.

    The unique key of a record is `source_key` (= id | normalised address), because TÜV NORD reuses an id for several offices.
    """
    by_id: dict[str, list[dict]] = {}
    for r in rows:
        by_id.setdefault(r["source_id"], []).append(r)
    for sid, group in by_id.items():
        if len(group) == 1:
            group[0]["location_id"] = f"{SOURCE}-{sid}"
            continue
        for n, r in enumerate(sorted(group, key=lambda r: (r["address_norm"], r["name_clean"])), 1):
            r["location_id"] = f"{SOURCE}-{sid}-{n}"


def parse_records(records: list[dict], scraped_at: str = "") -> list[dict]:
    rows = [parse_record(r, scraped_at) for r in records]
    assign_location_ids(rows)
    return rows


# ------------------------------------------------------------------------------------------------ detail page
def describe_detail(html: str) -> dict:
    """What a station detail page offers: title, address, opening hours, service headings, partner note, contact links."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main") or soup.body or soup
    h1 = main.select_one("h1")
    strings, started = [], False
    for el in main.descendants:
        if el is h1:
            started = True
        if started and isinstance(el, str) and el.strip():
            if "Finden Sie eine TÜV NORD Station" in el:
                break
            strings.append(clean(el))
    out: dict = {"title": clean(soup.title.string) if soup.title and soup.title.string else "", "h1": clean(h1.get_text(" ", strip=True)) if h1 else "",
                 "canonical": next((l.get("href") for l in soup.select("link[rel=canonical]")), ""),
                 "meta_description": next((m.get("content", "") for m in soup.select("meta[name=description]")), "")}
    # a missing station page is answered with HTTP 200 and the finder page itself (canonical = /de/stationen/)
    out["is_finder_page"] = out["canonical"].rstrip("/").endswith(STATIONS_PATH.rstrip("/"))
    out["address_lines"] = strings[1:3] if len(strings) >= 3 else []
    # opening hours: the strings between "Unsere Öffnungszeiten" and the TYPO3SEARCH marker (or the services heading), without the central service phone
    hours: list[str] = []
    if "Unsere Öffnungszeiten" in strings:
        for s in strings[strings.index("Unsere Öffnungszeiten") + 1:]:
            if s.startswith("TYPO3SEARCH") or s == "Unsere Leistungen":
                break
            if s != "Kostenloses Service-Telefon" and not re.fullmatch(r"[\d\s/+()-]{8,}", s):
                hours.append(s)
    out["opening_hours_lines"] = hours
    out["has_opening_hours"] = any(re.search(r"\d{1,2}:\d{2}", s) for s in hours)
    out["has_hinweis"] = any(s.startswith("Hinweis") for s in hours)
    sec = main.find(lambda t: t.name in ("h2", "h3") and clean(t.get_text()) == "Unsere Leistungen")
    headings: list[str] = []
    if sec is not None:
        for h in sec.find_all_next(["h2", "h3", "h4"]):
            text = clean(h.get_text(" ", strip=True))
            if text in ("Kontakt", "TÜV NORD Mobilität") or "Termin buchen" in text:
                break
            headings.append(text.replace("­", ""))
    out["service_headings"] = headings
    out["lists_hu_service"] = any(h.startswith("Hauptuntersuchung (HU)") for h in headings)
    out["partner_note"] = next((s for s in strings if "Partnerunternehmen" in s or "gehört als Partner" in s), "")
    out["has_partner_block"] = "TÜV NORD-Partner" in strings
    tel = sorted({a["href"][4:].strip() for a in main.select('a[href^="tel:"]')})
    mail = sorted({a["href"][7:].split("?")[0].strip() for a in main.select('a[href^="mailto:"]')})
    out["phones"], out["emails"] = tel, mail
    out["booking_links"] = sorted({a["href"] for a in main.select("a[href]") if re.search(r"termin", a.get_text(" ", strip=True), re.I)
                                   and a["href"].startswith(("http", "/"))})[:6]
    # the online booking link carries `stationId=<n>`, which equals the `id` of the list data (checked per page)
    ids = sorted({m.group(1) for a in main.select("a[href]") for m in [re.search(r"[?&]stationId=(\d+)", a["href"])] if m})
    out["booking_station_ids"] = ids
    gm = main.select_one("[data-googlemap-locations]")
    coords = []
    if gm is not None:
        try:
            coords = [{"name": x.get("name"), "lat": (x.get("coords") or {}).get("lat"), "lng": (x.get("coords") or {}).get("lng")}
                      for x in json.loads(gm["data-googlemap-locations"])]
        except (ValueError, AttributeError, TypeError):
            coords = []
    out["map_coords"] = coords
    out["breadcrumbs"] = [clean(b.get_text(" ", strip=True)) for b in main.select("[data-breadcrumbs-item]")]
    ld = []
    for x in soup.select('script[type="application/ld+json"]'):
        try:
            ld.append(json.loads(x.string or "").get("@type"))
        except (ValueError, AttributeError):
            ld.append("unparseable")
    out["jsonld_types"] = ld
    return out
