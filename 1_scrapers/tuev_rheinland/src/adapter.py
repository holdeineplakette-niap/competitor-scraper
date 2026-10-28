"""TÜV Rheinland adapter: one JSON file holds every TÜV location worldwide; we keep German VIS (HU stations).

Source: https://www.tuv.com/germany/de/locationfinder/locationfinder-config.json (one GET, no key).
The finder filters this file client-side, so there is no query grid. See the control notebook for the run.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

from core import enrich, paths
from core import export as core_export

logger = logging.getLogger(__name__)

SLUG = "tuev_rheinland"
SOURCE_URL = "https://www.tuv.com/germany/de/locationfinder/locationfinder-config.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9",
}
TIMEOUT_S = 60
ATTEMPTS = 2  # 1 fetch + 1 retry, only for network errors and 5xx; a 403 or non-JSON answer stops at once


class FetchError(RuntimeError):
    """The source did not answer with the JSON file. Do not work around it; report it."""


def fetch(url: str = SOURCE_URL) -> tuple[object, bytes]:
    """GET the config file. Returns (parsed JSON, original bytes). Raises FetchError on anything unexpected."""
    last = "no attempt made"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            logger.warning("attempt %d failed: %s", attempt, last)
            continue
        if response.status_code >= 500:
            last = f"HTTP {response.status_code}"
            logger.warning("attempt %d failed: %s", attempt, last)
            continue
        if response.status_code != 200:
            raise FetchError(f"HTTP {response.status_code} (content-type {response.headers.get('content-type')}); stopping, no bypass")
        try:
            return response.json(), response.content
        except ValueError:
            head = response.text[:80].replace("\n", " ")
            raise FetchError(f"not JSON (content-type {response.headers.get('content-type')}, starts {head!r}); stopping, no bypass")
    raise FetchError(f"{ATTEMPTS} attempts failed, last: {last}")


LANDING_BASE = "https://www.tuv.com/germany/de/lp/"
LANDING_PAGES = {  # region -> URL, exactly as given (the Saarland URL is upper-case percent-encoded, the others lower-case)
    "saarland": LANDING_BASE + "mobilit%C3%A4t/lp-t%C3%BCv-rheinland-pr%C3%BCfstellen/navigation/saarland/",
    "berlin": LANDING_BASE + "mobilit%c3%a4t/lp-t%c3%bcv-rheinland-pr%c3%bcfstellen/navigation/berlin/",
    "pfalz": LANDING_BASE + "mobilit%c3%a4t/lp-t%c3%bcv-rheinland-pr%c3%bcfstellen/navigation/pfalz/",
}


def fetch_page(url: str) -> bytes:
    """One GET of an HTML landing page, no retry (the budget is one request per page). Raises FetchError unless it is a 200 page."""
    try:
        response = requests.get(url, headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"}, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}")
    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code} for {url}; stopping, no bypass")
    return response.content


def save_raw(content: bytes, started_at: datetime | None = None, mode: str = "sample", root=None):
    """Write the response as raw.json into a new run folder (dev copy; test_output/ for sample)."""
    started_at = started_at or datetime.now()
    folder = paths.new_run_dir(SLUG, mode, started_at, root)
    (folder / "raw.json").write_bytes(content)
    return folder / "raw.json"


def latest_raw(root=None):
    """The newest dev copy raw.json in test_output/, or None."""
    found = sorted(paths.output_dir(SLUG, "sample", root).glob("*/raw.json"))
    return found[-1] if found else None


# ---------------------------------------------------------------- parse

BASE_URL = "https://www.tuv.com"
KNOWN_FIELDS = ["id", "title", "type", "address", "lat", "long", "phone", "opening_times", "county", "vic_id", "external_db",
                "appointment_available", "appointment_link", "detail_url", "directions_link", "email_link", "headquarter",
                "external", "company"]
GERMANY_BOX = (47.2, 55.1, 5.8, 15.1)  # lat_min, lat_max, lng_min, lng_max
VIC_RE = re.compile(r"\s*(\d+)_([A-Za-z]{2})-([A-Za-z]+)\s*")
ZIP_LINE_RE = re.compile(r"^(\d{4,5})[,\s]+(\S.*)$")
FOREIGN_PC_RE = re.compile(r"\b[A-Z]{1,3}-\d{4,5}\b")  # e.g. Luxembourg 'L-5326'
COLUMNS = ["location_id", "source_org", "source_id", "name", "street", "zip", "city", "state", "lat", "lng", "phone", "booking_url",
           "source_profile_url", "opening_hours", "site_type", "site_type_evidence", "operator_group", "in_scope", "needs_review", "needs_review_reason",
           "plz_distance_km", "scraped_at", "vic_id", "external_db", "appointment_available"]


def _text(value) -> str:
    return "" if value is None or str(value).strip().lower() in ("none", "null", "nan") else str(value).strip()


def _num(value) -> float | None:
    try:
        return float(value) if _text(value) else None
    except ValueError:
        return None


def _url(value) -> str:
    return urljoin(BASE_URL, _text(value)) if _text(value) else ""


def parse_address(html) -> tuple[str, str, str]:
    """Address HTML -> (street, zip, city). The zip line is the last line starting with 4-5 digits; zip is zero-padded to 5."""
    soup = BeautifulSoup(_text(html).replace("<br/>", "\n").replace("<br>", "\n").replace("</p>", "\n"), "html.parser")
    lines = [re.sub(r"\s+", " ", line).strip(" ,") for line in soup.get_text("\n").splitlines()]
    lines = [line for line in lines if line]
    for i in range(len(lines) - 1, -1, -1):
        match = ZIP_LINE_RE.match(lines[i])
        if match:
            return ", ".join(lines[:i]), match.group(1).zfill(5), match.group(2)
    return ", ".join(lines), "", ""


def format_hours(value) -> str:
    """[{label: [days, hours]}, ...] -> 'Mo.-Do. 07:30-12:30 | 13:00-16:30; Fr. ...'."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value.strip()
    pairs = [v for item in (value if isinstance(value, list) else []) if isinstance(item, dict)
             for v in item.values() if isinstance(v, list) and len(v) == 2]
    return "; ".join(f"{_text(days)} {_text(hours)}".strip() for days, hours in pairs)


def parse_vic(vic) -> tuple[str | None, str | None, bool]:
    """vic_id '<n>_de-FSP' -> (country 'de', pool 'FSP', canonical). Not parseable -> (None, None, False)."""
    match = VIC_RE.fullmatch(_text(vic)) if _text(vic) else None
    if not match:
        return None, None, False
    country, pool = match.group(2), match.group(3)
    return country.lower(), pool.upper(), (country, pool) in (("de", "DE"), ("de", "FSP"))


def in_germany(lat, lng) -> bool:
    return lat is not None and lng is not None and GERMANY_BOX[0] <= lat <= GERMANY_BOX[1] and GERMANY_BOX[2] <= lng <= GERMANY_BOX[3]


# ---------------------------------------------------------------- classify

def classify(records: list, scraped_at: str) -> pd.DataFrame:
    """One row per VIS record. in_scope = vic_id country is 'de' and PLZ/coordinates are not foreign, or no vic_id but German-looking
    with external_db tuev/fsp (review flag). Other types are dropped here."""
    rows = []
    for rec in (r for r in records if isinstance(r, dict) and r.get("type") == "VIS"):
        street, zip_, city = parse_address(rec.get("address"))
        lat, lng = _num(rec.get("lat")), _num(rec.get("long"))
        country, pool, canonical = parse_vic(rec.get("vic_id"))
        ext = _text(rec.get("external_db"))
        looks_german = bool(re.fullmatch(r"\d{5}", zip_)) and (in_germany(lat, lng) or lat is None or lng is None)
        reasons = []

        # completeness first: a German-looking VIS without any vic_id (external_db tuev/fsp) is included, flagged for review
        no_vic_german = country is None and looks_german and ext in ("tuev", "fsp")
        # pool: own stations are external_db "" or "tuev" with vic pool DE; FSP partners are external_db "fsp" with pool FSP
        if country == "de" and ext in ("", "tuev") and pool == "DE" or no_vic_german and ext == "tuev":
            site_type = "own_station"
        elif country == "de" and ext == "fsp" and pool == "FSP" or no_vic_german and ext == "fsp":
            site_type = "fsp_partner"
        else:
            site_type = "unknown"
            if country == "de":
                reasons.append(f"site_type unknown: external_db={ext!r} vs vic_id pool {pool}")
        # vic_id 'de' is the scope rule; a foreign postcode or coordinates outside Germany override it (not exported)
        foreign = country == "de" and (bool(FOREIGN_PC_RE.search(_text(rec.get("address")))) or (lat is not None and lng is not None and not in_germany(lat, lng)))
        if foreign:
            reasons.append("vic_id says de but postcode/coordinates are foreign (excluded)")
        elif country == "de":
            if not canonical:
                reasons.append(f"vic_id format odd: {_text(rec.get('vic_id'))!r}")
            if not zip_:
                reasons.append("no PLZ in address" + (" - to be filled from the FSP list in the aggregator" if site_type == "fsp_partner" else ""))
        elif no_vic_german:
            reasons.append("no vic_id; included because PLZ/coordinates look German")
        elif looks_german:
            reasons.append(f"German-looking VIS but vic_id is {_text(rec.get('vic_id'))!r} (not exported)")
        if lat is None or lng is None:
            reasons.append("coordinates missing")
        rows.append({
            "location_id": f"TUEV_RHEINLAND-{_text(rec.get('id'))}", "source_org": "TÜV Rheinland", "source_id": _text(rec.get("id")),
            "name": _text(rec.get("title")), "street": street, "zip": zip_, "city": city, "state": pd.NA, "lat": lat, "lng": lng,
            "phone": _text(rec.get("phone")), "booking_url": _url(rec.get("appointment_link")),
            "source_profile_url": _url(rec.get("detail_url")), "opening_hours": format_hours(rec.get("opening_times")),
            "site_type": site_type, "site_type_evidence": f"external_db={ext!r}; vic_id={_text(rec.get('vic_id'))!r}",
            "operator_group": {"own_station": "tuev_rheinland", "fsp_partner": "fsp"}.get(site_type, ""),
            "in_scope": (country == "de" and not foreign) or no_vic_german, "needs_review": bool(reasons),
            "needs_review_reason": enrich.REASON_SEPARATOR.join(reasons), "plz_distance_km": pd.NA, "scraped_at": scraped_at,
            "vic_id": _text(rec.get("vic_id")), "external_db": ext, "appointment_available": _text(rec.get("appointment_available")),
            "address_raw": _text(rec.get("address")),
        })
    return pd.DataFrame(rows)


def add_distance(df: pd.DataFrame, centroids: pd.DataFrame) -> pd.DataFrame:
    """State and plz_distance_km from GeoNames for in-scope rows; flags distances above 15 km (core.enrich)."""
    return enrich.enrich(df, centroids, max_distance_km=15, check=df["in_scope"])


# ---------------------------------------------------------------- checks

def last_pass_count(root=None) -> int | None:
    """Rows of the newest verified (not *_unverified) CSV in output/; None on a first run."""
    files = [f for f in sorted(paths.output_dir(SLUG, "full", root).glob(f"*/{SLUG}_pruefstellen_*.csv")) if "unverified" not in f.name]
    return len(pd.read_csv(files[-1], sep=";", encoding="utf-8-sig", dtype=str)) if files else None


def run_checks(records, df: pd.DataFrame, root=None) -> list[tuple[str, bool, str]]:
    """Hard checks; any False -> FAIL. Returns [(name, ok, detail)]."""
    scope = df[df["in_scope"]] if len(df) else df
    ids = [r.get("id") for r in records] if isinstance(records, list) else []
    checks = [("response is a JSON list", isinstance(records, list), type(records).__name__)]
    missing = [f for f in KNOWN_FIELDS if isinstance(records, list) and not any(isinstance(r, dict) and f in r for r in records)]
    checks.append(("known fields exist", not missing, f"missing: {missing}" if missing else "all 19"))
    checks.append(("ids unique", len(ids) == len(set(ids)) and None not in ids, f"{len(ids) - len(set(ids))} duplicates"))
    pools = scope["site_type"].value_counts().to_dict() if len(scope) else {}
    checks.append(("German VIS in each pool", pools.get("own_station", 0) > 0 and pools.get("fsp_partner", 0) > 0, str(pools)))
    # Missing coordinates and missing FSP PLZ are review flags only: the aggregator geocodes and fills PLZ from the FSP list.
    no_plz = ~scope["zip"].str.fullmatch(r"\d{5}") if len(scope) else scope["zip"] == ""
    bad = scope[(scope["street"] == "") | (no_plz & (scope["site_type"] != "fsp_partner"))]
    checks.append(("in-scope rows have street and 5-digit PLZ (FSP partners: PLZ may be filled later)", bad.empty,
                   f"{len(bad)} rows lack one: {list(bad['source_id'].head(5))}"))
    flagged = scope[no_plz | scope["lat"].isna() | scope["lng"].isna()]
    checks.append(("rows without PLZ or coordinates are all marked needs_review (aggregator fills them)", bool(flagged["needs_review"].all()),
                   f"{len(flagged)} rows, {int(flagged['needs_review'].sum())} marked"))
    baseline = last_pass_count(root)
    if baseline is None:
        checks.append(("in-scope count vs last PASS CSV", True, "skipped, first run"))
    else:
        checks.append(("in-scope count vs last PASS CSV", len(scope) >= baseline * 0.9, f"{baseline} -> {len(scope)} (max drop 10%)"))
    return checks


# ---------------------------------------------------------------- landing pages (cross-check only, never adds or drops rows)

LANDING_ADDRESS_RE = re.compile(r"^(.*?)[,\s]+(\d{5,6})\s+(\S.*)$")  # 6 digits happens (typo on the Pfalz page); kept as written
LANDING_CLAIM_RE = re.compile(r"(\d+)\s+(?:TÜV Rheinland\s+)?Prüfstellen")
HOUSE_NO_RE = re.compile(r"[\s,]*\d+[a-zA-Z]?(?:\s*[-–/]\s*\d+[a-zA-Z]?)*\s*$")
SAARLAND_EXPECTED = 24


def parse_landing(html, region: str) -> tuple[list[dict], list[int]]:
    """All table rows (Standort | Adresse | Website) of a landing page, plus every 'N Prüfstellen' number the page text states."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        address = cells[1].get_text(" ", strip=True)
        match = LANDING_ADDRESS_RE.match(address)
        street, zip_, city = (match.group(1).strip(" ,"), match.group(2), match.group(3)) if match else (address, "", "")
        anchor = cells[2].find("a", href=True) if len(cells) > 2 else None
        rows.append({"region": region, "standort": cells[0].get_text(" ", strip=True), "street": street, "zip": zip_, "city": city,
                     "link": _url(anchor["href"]) if anchor else ""})
    return rows, sorted({int(n) for n in LANDING_CLAIM_RE.findall(soup.get_text(" ", strip=True))})


def _street_name_key(value) -> str:
    return _norm_street(HOUSE_NO_RE.sub("", _text(value)))


def _similar(a: str, b: str) -> bool:
    """Spelling variants of one street key: one extends the other with non-digits ('...1a' vs '...1aamsaarbasar'), or ratio >= 0.85."""
    if len(a) < 6 or len(b) < 6:
        return False
    short, long_ = sorted((a, b), key=len)
    return (len(long_) > len(short) and long_.startswith(short) and not long_[len(short)].isdigit()) or SequenceMatcher(None, a, b).ratio() >= 0.85


def german_records(records: list) -> pd.DataFrame:
    """Every German record of the JSON, any type: VIS by vic_id country, other types by a 5-digit PLZ with German (or missing) coordinates."""
    rows = []
    for rec in (r for r in records if isinstance(r, dict)):
        street, zip_, _ = parse_address(rec.get("address"))
        lat, lng = _num(rec.get("lat")), _num(rec.get("long"))
        country = parse_vic(rec.get("vic_id"))[0]
        german = country == "de" if country else bool(re.fullmatch(r"\d{5}", zip_)) and (in_germany(lat, lng) or lat is None or lng is None)
        if german:
            rows.append({"id": _text(rec.get("id")), "type": _text(rec.get("type")), "title": _text(rec.get("title")), "street": street, "zip": zip_,
                         "key": (_norm_street(street), zip_), "name_key": (_street_name_key(street), zip_)})
    return pd.DataFrame(rows, columns=["id", "type", "title", "street", "zip", "key", "name_key"])


def match_landing(landing: list[dict], records: list) -> pd.DataFrame:
    """Per landing row: matched_vis / matched_other_type / not_in_json. Match = normalised street + PLZ; fallback = same PLZ and same
    street name without house number (the pages carry no coordinates, so a 300 m test is not possible). VIS wins over other types."""
    pool = german_records(records)
    out = []
    for row in landing:
        hit, how = None, ""
        street_key, name_key = _norm_street(row["street"]), _street_name_key(row["street"])
        zip_ok = bool(re.fullmatch(r"\d{5}", row["zip"]))
        for label, mask in (
            ("street+plz", [k == (street_key, row["zip"]) for k in pool["key"]]),
            ("plz+street name", [k == (name_key, row["zip"]) for k in pool["name_key"]]),
            # either side has no usable PLZ (JSON address without PLZ, or a typo on the page): street alone, full key with house number
            ("street only (PLZ missing)", [k[0] == street_key and (not zip_ok or not k[1]) for k in pool["key"]]),
            ("plz + similar street (check)", [zip_ok and k[1] == row["zip"] and _similar(k[0], street_key) for k in pool["key"]]),
        ):
            found = pool[mask].sort_values("type", key=lambda s: s != "VIS", kind="stable") if street_key else pool.iloc[0:0]
            if len(found):
                hit, how = found.iloc[0], label
                break
        same_plz = pool[pool["zip"] == row["zip"]] if row["zip"] else pool.iloc[0:0]
        out.append({**row, "status": "not_in_json" if hit is None else ("matched_vis" if hit["type"] == "VIS" else "matched_other_type"),
                    "matched_type": "" if hit is None else hit["type"], "matched_id": "" if hit is None else hit["id"],
                    "how": how, "same_plz_in_json": "; ".join(f"{r.id} {r.type} {r.street[:30]}" for r in same_plz.head(3).itertuples()),
                    # hint for Nikos only (never used for matching): records whose title contains the Standort text
                    "title_hint": "" if hit is not None else "; ".join(f"{r.id} {r.type} {r.title[:40]}" for r in pool[pool["title"].str.contains(row["standort"].split("-")[-1], case=False, regex=False)].head(4).itertuples())})
    return pd.DataFrame(out)


def saarland_check(landing: list[dict], claims: list[int]) -> tuple[bool, str]:
    """Hard assertion for the Saarland page: 24 rows parsed and the page text still says 24 Prüfstellen."""
    ok = len(landing) == SAARLAND_EXPECTED and SAARLAND_EXPECTED in claims
    return ok, f"{len(landing)} rows parsed, page states {claims}, expected {SAARLAND_EXPECTED}"


def set_saarland(df: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    """operator_group = tuev_saarland for own stations matched (as VIS) by a Saarland landing row. Rows are neither added nor dropped."""
    ids = set(matches.loc[(matches["region"] == "saarland") & (matches["status"] == "matched_vis"), "matched_id"])
    out = df.copy()
    out.loc[out["source_id"].isin(ids) & (out["site_type"] == "own_station"), "operator_group"] = "tuev_saarland"
    return out


def fill_from_landing(df: pd.DataFrame, matches: pd.DataFrame, fills: dict[str, tuple[str, str, str]]) -> pd.DataFrame:
    """Fill the empty address of a finder record from a landing-page row: {source_id: (region, zip, street)}. Never overwrites a filled address.
    Like `add_from_landing`, only the pairs Nikos chose are applied (the finder title names the street, the address field is empty)."""
    out = df.copy()
    for source_id, (region, zip_, street) in fills.items():
        hit = matches[(matches["region"] == region) & (matches["zip"] == zip_) & (matches["street"] == street)]
        idx = out.index[out["source_id"] == source_id]
        if hit.empty or len(idx) != 1 or out.loc[idx[0], "street"] or out.loc[idx[0], "zip"]:
            raise ValueError(f"cannot fill {source_id} from {(region, zip_, street)}: row missing, or its address is not empty")
        i = idx[0]
        out.loc[i, ["street", "zip", "city"]] = [street, zip_, hit.iloc[0]["city"]]
        stale = ("no PLZ in address", enrich.REASON_UNKNOWN_PLZ)
        reasons = [r for r in enrich.split_reasons(out.loc[i, "needs_review_reason"]) if r not in stale] + [f"address filled from landing page {region}"]
        out.loc[i, "needs_review_reason"] = enrich.REASON_SEPARATOR.join(reasons)
        if region == "saarland" and out.loc[i, "site_type"] == "own_station":
            out.loc[i, "operator_group"] = "tuev_saarland"
    return out


def add_from_landing(df: pd.DataFrame, matches: pd.DataFrame, picks: list[tuple[str, str, str]]) -> pd.DataFrame:
    """Append the explicitly chosen landing-page rows (region, zip, street) that the finder JSON lacks. Nothing is added automatically:
    `picks` is a decision of Nikos, kept in the notebook. Added rows have no coordinates and are flagged for review."""
    rows = []
    for region, zip_, street in picks:
        hit = matches[(matches["region"] == region) & (matches["zip"] == zip_) & (matches["street"] == street)]
        if hit.empty or hit.iloc[0]["status"] != "not_in_json":
            raise ValueError(f"pick {(region, zip_, street)} is not a not_in_json landing row")
        r = hit.iloc[0]
        rows.append({"location_id": f"TUEV_RHEINLAND-LP-{region}-{zip_}-{_norm_street(street)}", "source_org": "TÜV Rheinland",
                     "source_id": f"landing:{region}:{zip_}:{_norm_street(street)}", "name": f"TÜV Rheinland Prüfstelle {r['standort']}",
                     "street": street, "zip": zip_, "city": r["city"], "state": pd.NA, "lat": pd.NA, "lng": pd.NA, "phone": "", "booking_url": "",
                     "source_profile_url": r["link"], "opening_hours": "", "site_type": "own_station",
                     "site_type_evidence": f"landing page {region} only, not in finder JSON",
                     "operator_group": "tuev_saarland" if region == "saarland" else "tuev_rheinland", "in_scope": True, "needs_review": True,
                     "needs_review_reason": "not in finder JSON; added from landing page, no coordinates", "plz_distance_km": pd.NA,
                     "scraped_at": df["scraped_at"].iloc[0], "vic_id": "", "external_db": "", "appointment_available": "", "address_raw": ""})
    if not rows:
        return df
    out = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    out[["lat", "lng", "plz_distance_km"]] = out[["lat", "lng", "plz_distance_km"]].apply(pd.to_numeric, errors="coerce").astype(float)
    return out


# ---------------------------------------------------------------- export + cross-check

def export_csv(df: pd.DataFrame, passed: bool, mode: str, started_at: datetime | None = None, root=None) -> Path:
    """Write the single in-scope CSV into a new run folder; failed checks add the tag 'unverified'."""
    started_at = started_at or datetime.now()
    folder = paths.new_run_dir(SLUG, mode, started_at, root)
    out = core_export.export(df, folder, SLUG, run_date=f"{started_at:%Y-%m-%d}",
                             tag=None if passed else "unverified", only=("pruefstellen_csv",))
    return Path(out["pruefstellen_csv"][0])


def _norm_street(value) -> str:
    text = _text(value).lower().replace("ß", "ss")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", re.sub(r"strasse|str\.", "str", text))


def fsp_crosscheck(df: pd.DataFrame, root=None) -> dict:
    """Report only: fsp_partner rows here vs the newest FSP CSV, matched on normalised street + PLZ."""
    files = sorted(paths.output_dir("fsp", "full", root).glob("*/fsp_pruefstellen_*.csv"))
    if not files:
        return {"file": None}
    fsp = pd.read_csv(files[-1], sep=";", encoding="utf-8-sig", dtype=str).fillna("")
    here = df[df["in_scope"] & (df["site_type"] == "fsp_partner")]
    key_here = {(_norm_street(s), z) for s, z in zip(here["street"], here["zip"])}
    key_fsp = {(_norm_street(s), z.zfill(5)) for s, z in zip(fsp["street"], fsp["plz"])}
    left_here, left_fsp = key_here - key_fsp, key_fsp - key_here
    # loose second pass: one street contains the other (house-number suffix, 'B 42' vs 'Bundesstr.') and the PLZ agrees or is missing
    pairs = [(h, f) for h in left_here for f in left_fsp if h[0] and f[0] and (h[0] in f[0] or f[0] in h[0]) and (h[1] == f[1] or not h[1] or not f[1])]
    return {"file": files[-1].parent.name, "here": len(key_here), "fsp": len(key_fsp), "matched": len(key_here & key_fsp),
            "matched_loose": len({h for h, _ in pairs}),
            "only_here": sorted(left_here - {h for h, _ in pairs}), "only_fsp": sorted(left_fsp - {f for _, f in pairs})}
