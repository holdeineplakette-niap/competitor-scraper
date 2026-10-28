"""TÜV SÜD parsing: one finder answer -> per-query facts, one `finaltsc` item -> one row of the candidate schema.

Pure functions, no network. The answers come from the browser snippet (browser.py); classification (site_type,
in_scope, flags) is in classify.py. Field names follow the HU finder answer as recorded in
0_instructions/scraper_status.md (section TÜV SÜD) and 0_instructions/sources/tuev_sued.md §8. Lookups of
`location` fields are case-insensitive and accept the spellings seen in the corporate locator (same CMS), because no
HU-finder answer is stored in the repo yet; the run summary lists every key seen with its fill rate.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from core.geo import haversine_km

SLUG = "tuev_sued"
SOURCE_ORG = "TÜV SÜD"
ID_PREFIX = "TUEV_SUED-"
HOME_COUNTRY = "DE"
GERMANY_COUNTRY_ID = "1206022065302211750256"  # `countryId` of Germany (a string in the API; compared as text)
OWN_COMPANY_ID = "500"  # `companyId` of the TÜV SÜD Service-Center in the HU finder
# Who runs a site, from `companyId` (labels inferred from the full run of 2026-10-06: email domains and places). All of them are
# Prüfstellen and in scope (decision 2026-10-06, Nikos): the group is information for filtering and for the later cross-ÜO
# dedupe, since TÜV Hessen and TÜV Hanse sites are in this finder as well. It is not a flag and never sets needs_review.
OPERATOR_GROUPS = {"500": "own", "501": "tuev_hanse", "502": "tuev_hessen", "505": "tuev_hessen_partner"}
PARTNER_GROUP = "tuev_sued_partner"  # the one long companyId (about a third of the sites): partner-operated
TIE_MARGIN_KM = 0.5  # r_q = distance of the last record - this
DISTANCE_DEVIATION_KM = 1.0  # reported distance vs great-circle distance from the centre: more is a warning
NOTICE_SLOTS = ("", "2", "3")

COLUMNS = [
    "location_id",
    "source_org",
    "source_id",
    "name",
    "street",
    "zip",
    "city",
    "state",
    "country",
    "lat",
    "lng",
    "plz_distance_km",
    "phone",
    "email",
    "website",
    "source_profile_url",
    "booking_url",
    "opening_hours",
    "services",
    "heavy_vehicles",
    "location_sign",
    "aspro_id",
    "market_area_id",
    "federal_state_id",
    "company_id",
    "operator_group",
    "tsc_status",
    "huau",
    "current_notices",
    "currently_closed",
    "site_type",
    "site_type_evidence",
    "in_scope",
    "scope_reason",
    "doubt_flags",
    "needs_review",
    "needs_review_reason",
    "scraped_at",
]

SERVICE_KEYS = ("huau", "oldtimer", "sga", "dkgk")  # plus every key ending in "service" (the real record has ~40 of them) and SERVICE_PREFIXES
SERVICE_PREFIXES = ("trailer", "camper", "tractor")
DAYS = {1: "Mo", 2: "Di", 3: "Mi", 4: "Do", 5: "Fr", 6: "Sa", 7: "So"}

ANSWER_FILE = re.compile(r"^pass(\d{2,})_q(\d{3,})_(\d{5})\.json$")
SUMMARY_FILE = re.compile(r"^browser_summary(?:_pass(\d{2,}))?\.json$")
_TIME = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)")


class ShapeError(ValueError):
    """An answer that is not the expected finder JSON."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def clean(value) -> str | None:
    """Strip, collapse inner whitespace; empty strings and None become None."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None


def to_int(value) -> int | None:
    number = to_float(value)
    return None if number is None else int(number)


def truthy(value) -> bool:
    return value is True or to_float(value) == 1.0 or (isinstance(value, str) and value.strip().lower() in ("true", "yes", "ja"))


def ci_map(location: dict) -> dict:
    """Lower-case key -> value (the first spelling wins)."""
    out: dict = {}
    for key, value in location.items():
        out.setdefault(str(key).lower(), value)
    return out


def first(lc: dict, *names):
    """The first non-empty value among the (case-insensitive) names."""
    for name in names:
        value = lc.get(name.lower())
        if value is not None and value != "" and value != []:
            return value
    return None


def text_of(lc: dict, *names) -> str | None:
    return clean(first(lc, *names))


def zip_of(value) -> str | None:
    z = clean(value)
    return z.zfill(5) if z and z.isdigit() and len(z) < 5 else z


def answer_name(pass_no: int, seq: int, plz: str) -> str:
    return f"pass{pass_no:02d}_q{seq:03d}_{plz}.json"


def parse_answer_name(name: str) -> tuple[int, int, str] | None:
    m = ANSWER_FILE.match(name)
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else None


# --------------------------------------------------------------------------
# One answer
# --------------------------------------------------------------------------

def item_id(item: dict) -> str | None:
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    return clean(item.get("tscId")) or clean(loc.get("tscId")) or clean(ci_map(loc).get("tscid"))


def item_distance(item: dict) -> float | None:
    for key in ("sortdistance", "distance"):
        value = to_float(item.get(key))
        if value is not None:
            return value
    return None


def check_shape(data) -> dict:
    """The answer, unchanged, if it has the expected shape: a dict with a `finaltsc` list, the server's centre
    (`lat`, `lon`) and, per item, a `location` dict, an id and a distance. Anything else raises ShapeError."""
    if not isinstance(data, dict):
        raise ShapeError(f"unexpected answer (not an object): {str(data)[:200]!r}")
    items = data.get("finaltsc")
    if not isinstance(items, list):
        raise ShapeError(f"no finaltsc list in the answer; keys seen: {sorted(data)[:20]}")
    if to_float(data.get("lat")) is None or to_float(data.get("lon")) is None:
        raise ShapeError("the answer carries no search centre (top-level lat/lon)")
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("location"), dict):
            raise ShapeError(f"finaltsc[{i}] is not an item with a location")
        if item_id(item) is None:
            raise ShapeError(f"finaltsc[{i}] has no tscId")
        if item_distance(item) is None:
            raise ShapeError(f"finaltsc[{i}] has no distance")
    return data


@dataclass
class Answer:
    """One answered query: the 50 (or fewer) nearest Service-Center to the server's centre."""

    file: str
    pass_no: int
    seq: int
    zip: str
    centre: tuple[float, float]
    items: list[dict]
    first_km: float | None
    last_km: float | None
    radius_km: float  # the radius this answer proves: last distance - TIE_MARGIN_KM (0 for an empty answer)
    sorted_ok: bool
    distance_dev_km: float | None  # largest |reported distance - great-circle distance from the centre|
    reason: str | None = None  # why this PLZ was queried (plan / repeat / fill / sample), from the pass file
    locationname: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def ids(self) -> frozenset:
        return frozenset(item_id(i) for i in self.items)


def to_answer(file: str, response, reason: str | None = None) -> Answer:
    parsed = parse_answer_name(file)
    if parsed is None:
        raise ShapeError(f"unexpected answer name {file!r} (expected passNN_qKKK_PLZ.json)")
    data = check_shape(response)
    centre = (to_float(data["lat"]), to_float(data["lon"]))
    items = data["finaltsc"]
    dists = [item_distance(i) for i in items]
    problems: list[str] = []
    sorted_ok = all(a <= b + 1e-9 for a, b in zip(dists, dists[1:]))
    if not sorted_ok:
        problems.append("distances are not sorted ascending")
    deviations = []
    for item, dist in zip(items, dists):
        lc = ci_map(item["location"])
        lat, lng = to_float(lc.get("lat")), to_float(lc.get("lon"))
        if lat is not None and lng is not None:
            deviations.append(abs(float(haversine_km(centre[0], centre[1], lat, lng)) - dist))
    dev = max(deviations) if deviations else None
    if dev is not None and dev > DISTANCE_DEVIATION_KM:
        problems.append(f"reported distance differs from the great-circle distance by up to {dev:.2f} km")
    if clean(data.get("errormsg")):
        problems.append(f"errormsg: {clean(data.get('errormsg'))[:100]}")
    last = dists[-1] if dists else None
    return Answer(
        file=file, pass_no=parsed[0], seq=parsed[1], zip=parsed[2], centre=centre, items=items,
        first_km=dists[0] if dists else None, last_km=last,
        radius_km=max(0.0, last - TIE_MARGIN_KM) if last is not None else 0.0,
        sorted_ok=sorted_ok, distance_dev_km=dev, reason=reason, locationname=clean(data.get("locationname")), problems=problems,
    )


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

def strip_html(value) -> str | None:
    text = clean(value)
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", text))) if text else None


def unix_date(value) -> date | None:
    """A Unix timestamp (seconds, or milliseconds) as a UTC date; 0 / empty / invalid = not set."""
    number = to_int(value)
    if not number or number <= 0:
        return None
    if number > 10**11:
        number //= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def current_notices(lc: dict, run_date: date) -> tuple[list[str], int]:
    """(texts of the notices valid on run_date, number of notices whose time window could not be read).
    Slots notificationText, 2 and 3 with notificationText_start<slot> / _end<slot> (Unix time; 0 = open)."""
    texts, no_window = [], 0
    for slot in NOTICE_SLOTS:
        text = strip_html(first(lc, f"notificationText{slot}"))
        if not text:
            continue
        start_raw, end_raw = first(lc, f"notificationText_start{slot}"), first(lc, f"notificationText_end{slot}")
        if f"notificationtext_start{slot}" not in lc and f"notificationtext_end{slot}" not in lc:
            no_window += 1
        start, end = unix_date(start_raw), unix_date(end_raw)
        if (start and run_date < start) or (end and run_date > end):
            continue
        texts.append(text)
    return texts, no_window


def _time_tokens(value, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, v in value.items():
            if str(key).lower() not in ("dayid", "isspecialtime"):
                _time_tokens(v, out)
    elif isinstance(value, list):
        for v in value:
            _time_tokens(v, out)
    elif isinstance(value, str):
        for h, m in _TIME.findall(value):
            if int(h) < 24 and int(m) < 60:
                out.append(f"{int(h):02d}:{m}")


def flatten_times(times) -> tuple[str | None, bool]:
    """(opening hours text, parsed). One entry per `dayId` (1 = Mo ... 7 = So); the HH:MM values of an entry are read
    in order and paired into ranges (morning, afternoon), whatever the key names are. Hours that are not parsable
    leave the text empty and parsed False; an empty list is parsed True (nothing to read)."""
    if not isinstance(times, list) or not times:
        return None, True
    parts, readable = [], False
    for entry in sorted((e for e in times if isinstance(e, dict)), key=lambda e: to_int(e.get("dayId")) or 99):
        day = DAYS.get(to_int(entry.get("dayId")))
        tokens: list[str] = []
        _time_tokens(entry, tokens)
        ranges = [f"{a}–{b}" for a, b in zip(tokens[0::2], tokens[1::2]) if a != b]
        if tokens:
            readable = True
        if day and ranges:
            parts.append(f"{day} {', '.join(ranges)}" + (" (special)" if truthy(entry.get("isSpecialTime")) else ""))
    return ("; ".join(parts) or None), readable


def services_of(location: dict) -> str | None:
    """The service flags that are set, by their key names (takes the original `location`, so the names keep their case)."""
    lc = ci_map(location)
    found = [key for key in SERVICE_KEYS if truthy(lc.get(key.lower()))]
    for key, value in location.items():
        low = str(key).lower()
        is_service = low.endswith("service") or low.startswith(SERVICE_PREFIXES)  # carService, nphuService, hudbservice, trailerBig ...
        if is_service and "biggest" not in low and truthy(value) and str(key) not in found:
            found.append(str(key))
    return "; ".join(found) or None


def heavy_vehicles(lc: dict) -> bool:
    biggest = lc.get("trailer_biggest")
    return truthy(lc.get("truckservice")) or (biggest not in (None, "", 0, "0", False))


def street_of(lc: dict) -> str | None:
    street, number = text_of(lc, "street", "strasse"), text_of(lc, "streetnumber")
    if street and number and not street.endswith(number):
        return f"{street} {number}"
    return street


def operator_group(company_id: str | None) -> str | None:
    if not company_id:
        return None
    if company_id in OPERATOR_GROUPS:
        return OPERATOR_GROUPS[company_id]
    return PARTNER_GROUP if len(company_id) > 10 else f"other_{company_id}"


def parse_record(item: dict, scraped_at: str, run_date: date) -> tuple[dict, dict]:
    """(row, stats). The row has no classification and no GeoNames enrichment yet; stats counts what could not be read."""
    loc = item["location"]
    lc = ci_map(loc)
    tsc_id = item_id(item)
    sign = text_of(lc, "locationSign")
    part, town = text_of(lc, "townPart", "town_part"), text_of(lc, "town")
    times = item.get("times") if item.get("times") else loc.get("times")
    hours, hours_parsed = flatten_times(times)
    notices, no_window = current_notices(lc, run_date)
    country_id = text_of(lc, "countryId")
    closed = item.get("currentlyclosed", item.get("currentlyClosed"))  # item level first (the finder's flag), else the location's
    if closed in (None, ""):
        closed = first(lc, "currentlyClosed")
    row = {
        "location_id": f"{ID_PREFIX}{tsc_id}",
        "source_org": SOURCE_ORG,
        "source_id": tsc_id,
        "name": f"TÜV SÜD Service-Center {part or town}" if (part or town) else "TÜV SÜD Service-Center",
        "street": street_of(lc),
        "zip": zip_of(first(lc, "zipcode", "plz", "postcode")),
        "city": town,
        "state": None,  # filled from the PLZ (core.enrich)
        "country": HOME_COUNTRY if country_id == GERMANY_COUNTRY_ID else country_id,
        "lat": to_float(lc.get("lat")),
        "lng": to_float(lc.get("lon")),
        "plz_distance_km": None,
        "phone": text_of(lc, "phone", "directcall"),
        "email": text_of(lc, "emailtsc"),
        "website": None,
        "source_profile_url": None,  # not needed (decision 2026-10-05); tscUrl is only a path slug, the page address is unknown
        "booking_url": None,  # not needed (decision 2026-10-05); no booking link in the record
        "opening_hours": hours,
        "services": services_of(loc),
        "heavy_vehicles": heavy_vehicles(lc),
        "location_sign": sign,  # text: leading zeros matter
        "aspro_id": text_of(lc, "asproId"),
        "market_area_id": text_of(lc, "marketAreaId"),
        "federal_state_id": text_of(lc, "federalStateId"),
        "company_id": text_of(lc, "companyId"),
        "operator_group": operator_group(text_of(lc, "companyId")),
        "tsc_status": to_int(first(lc, "tscStatus")),
        "huau": to_int(first(lc, "huau")),
        "current_notices": " | ".join(notices) or None,
        "currently_closed": truthy(closed),
        "site_type": "unknown",
        "site_type_evidence": None,
        "in_scope": False,
        "scope_reason": None,
        "doubt_flags": None,
        "needs_review": False,
        "needs_review_reason": None,
        "scraped_at": scraped_at,
    }
    stats = {"opening_hours_unparsed": int(bool(times) and not hours_parsed), "notice_window_missing": no_window}
    return row, stats


def build_frame(items: list[dict], scraped_at: str, run_date: date) -> tuple[pd.DataFrame, dict]:
    """Rows for unique items (see merge_items) and the summed stats of parse_record."""
    rows, totals = [], {"opening_hours_unparsed": 0, "notice_window_missing": 0}
    for item in items:
        row, stats = parse_record(item, scraped_at, run_date)
        rows.append(row)
        for k, v in stats.items():
            totals[k] += v
    return pd.DataFrame(rows, columns=COLUMNS), totals


# --------------------------------------------------------------------------
# Across answers
# --------------------------------------------------------------------------

def _address_key(item: dict) -> tuple:
    lc = ci_map(item["location"])
    return (street_of(lc), zip_of(first(lc, "zipcode", "plz", "postcode")), text_of(lc, "town"))


def merge_items(answers: list[Answer]) -> tuple[dict, list[dict]]:
    """Unique items by tscId (first seen wins) and the ids that carry different addresses in different answers."""
    items: dict = {}
    conflicts: dict = {}
    for ans in answers:
        for item in ans.items:
            key = item_id(item)
            if key not in items:
                items[key] = item
            elif _address_key(items[key]) != _address_key(item):
                conflicts.setdefault(key, {"tsc_id": key, "addresses": {_address_key(items[key])}})["addresses"].add(_address_key(item))
    return items, [{"tsc_id": c["tsc_id"], "addresses": [list(a) for a in sorted(c["addresses"], key=str)]} for c in conflicts.values()]


def located_sites(items: dict) -> pd.DataFrame:
    """Unique sites with coordinates as (id, lat, lng)."""
    rows = []
    for key, item in items.items():
        lc = ci_map(item["location"])
        lat, lng = to_float(lc.get("lat")), to_float(lc.get("lon"))
        if lat is not None and lng is not None:
            rows.append((key, lat, lng))
    return pd.DataFrame(rows, columns=["id", "lat", "lng"])


def key_fill(items: dict) -> dict:
    """Every key seen in `location` with the number of items that fill it (schema is not final: see the run summary)."""
    total = len(items)
    filled: dict = {}
    for item in items.values():
        for key, value in item["location"].items():
            filled.setdefault(key, 0)
            if value not in (None, "", [], {}):
                filled[key] += 1
    return {k: {"filled": v, "rate": round(v / total, 3) if total else None} for k, v in sorted(filled.items())}
