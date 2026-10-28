"""TÜV Thüringen: own test stations from the Shopware / Acris Store Locator group "Prüfanlagen".

The page is server-rendered; every store appears three times (map overlay, desktop table, mobile table) and the
three views are merged per data-store-id. Flow: fetch -> parse -> classify -> check -> export.
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from core import enrich, export, geo, paths

SLUG = "tuev_thueringen"
SOURCE_ORG = "TÜV Thüringen"
BASE = "https://tuev-thueringen.de"
MAIN_URL = f"{BASE}/store-locator/08a232c58b2f4816939b7d6327804b88/store-group"
CROSSCHECK_URL = f"{BASE}/store-locator/Pruefanlagen"
SCAN_URLS = {
    "Service-Center": f"{BASE}/store-locator/2e603dda3ff6451fa0a9b67a1807ae3d/store-group",
    "Kunden-Center": f"{BASE}/store-locator/57d58ae52107443895c5f9d66f938e6e/store-group",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
}
TABLE_ID = "acrisStoreLocatorGroupTable"
REQUEST_LIMIT = 5  # live requests per Python session (kernel); the brief allows 5 in total
TIMEOUT_S = 30
GERMANY_BOX = (47.2, 55.1, 5.8, 15.1)  # lat_min, lat_max, lng_min, lng_max
MAX_DROP = 0.10
PLZ_FLAG_KM = 15
EXPECTED_FIRST_RUN = 12

_requests_made = 0


class FetchError(RuntimeError):
    """403, challenge page or a page without the store table: stop, no bypass."""


def fetch(url: str, require_table: bool = True) -> str:
    """One polite GET (2-4 s pause, 30 s timeout). Raises FetchError instead of retrying around a block."""
    global _requests_made
    if _requests_made >= REQUEST_LIMIT:
        raise FetchError(f"request limit of {REQUEST_LIMIT} reached for this session")
    if _requests_made:
        time.sleep(random.uniform(2, 4))
    _requests_made += 1
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_S)
    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code} for {url}")
    if require_table and TABLE_ID not in response.text:
        raise FetchError(f"no store table on {url} (challenge page or changed layout)")
    return response.text


# ---------------------------------------------------------------- parse
def _text(el) -> str:
    return " ".join(el.get_text(" ").split()) if el is not None else ""


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _contact(cell) -> dict:
    """Phone, e-mail, website, booking link, opening hours (text + raw HTML) from a Kontakt cell (desktop or mobile)."""
    out = {"phone": "", "email": "", "website": "", "booking_url": ""}
    for a in cell.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("tel:"):
            out["phone"] = out["phone"] or _text(a) or href[4:]
        elif a.has_attr("data-mail") or href.startswith("mailto:"):
            out["email"] = out["email"] or (a.get("data-mail") or href[7:]).replace("(at)", "@").strip()
        elif "terminvereinbarung" in _text(a).lower():
            out["booking_url"] = out["booking_url"] or href
        elif not urlparse(href).netloc.endswith("tuev-thueringen.de"):
            out["website"] = out["website"] or href
    rest = BeautifulSoup(str(cell), "html.parser")
    for a in rest.find_all("a"):
        a.decompose()
    out["opening_hours"] = "; ".join(t for t in (" ".join(s.split()) for s in rest.stripped_strings) if t)
    out["opening_hours_raw"] = " ".join(rest.find().decode_contents().split())
    return out


def _address(text: str) -> tuple[str, str, str]:
    m = re.match(r"^(.*?)\s+(\d{5})\s+(.+?)(?:\s+Germany)?$", " ".join(text.split()))
    return (m.group(1), m.group(2), m.group(3)) if m else (" ".join(text.split()), "", "")


def parse(html: str) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Merge overlay (a), desktop table (b) and mobile table (c) per data-store-id. Returns (rows, ids per view)."""
    soup = BeautifulSoup(html, "html.parser")
    overlay, table, mobile = {}, {}, {}
    for d in soup.select("div.acris-store-locator-map-overlay__store[data-store-id]"):
        link = d.select_one("a[href*='/store-locator/detail/']")
        overlay[d["data-store-id"]] = {
            "name": _text(d.select_one("button")), "lat": _num(d.get("data-lat")), "lng": _num(d.get("data-lng")),
            "profile": link["href"] if link else "",
        }
    for tr in soup.select(f"#{TABLE_ID} tr[data-store-id]"):
        table[tr["data-store-id"]] = dict(zip(("Name", "Adresse", "Kontakt", "Details"), tr.find_all("td", recursive=False)))
    for e in soup.select(".acris-store-locator-group-mobile-table-entry[data-store-id]"):
        rows = [(tr.find("th"), tr.find("td")) for tr in e.select("tr")]
        mobile[e["data-store-id"]] = {_text(th).replace(":", "").strip(): td for th, td in rows if th and td}
    ids = {view: [x["data-store-id"] for x in found] for view, found in (
        ("overlay", soup.select("div.acris-store-locator-map-overlay__store[data-store-id]")),
        ("table", soup.select(f"#{TABLE_ID} tr[data-store-id]")),
        ("mobile", soup.select(".acris-store-locator-group-mobile-table-entry[data-store-id]")))}
    rows = []
    for sid in dict.fromkeys(ids["table"] + ids["mobile"] + ids["overlay"]):
        cells, ov = table.get(sid) or mobile.get(sid) or {}, overlay.get(sid, {})
        street, zip_, city = _address(_text(cells.get("Adresse")))
        details = cells.get("Details")
        link = (details.find("a", href=True) if details is not None else None)
        contact = _contact(cells["Kontakt"]) if cells.get("Kontakt") is not None else _contact(BeautifulSoup("<td></td>", "html.parser"))
        rows.append({
            "location_id": f"{SLUG.upper()}-{sid}", "source_org": SOURCE_ORG, "source_id": sid,
            "name": _text(cells.get("Name")) or ov.get("name", ""), "name_overlay": ov.get("name", ""),
            "street": street, "zip": zip_, "city": city, "state": None, "lat": ov.get("lat"), "lng": ov.get("lng"),
            "phone": contact["phone"], "email": contact["email"], "website": contact["website"],
            "booking_url": contact["booking_url"],
            "source_profile_url": urljoin(BASE, link["href"] if link else ov.get("profile", "")),
            "opening_hours": contact["opening_hours"], "opening_hours_raw": contact["opening_hours_raw"],
        })
    return pd.DataFrame(rows), ids


# ---------------------------------------------------------------- classify
WORKSHOP = re.compile(r"autohaus|werkstatt", re.I)


def classify(df: pd.DataFrame) -> pd.DataFrame:
    """Every store of the Prüfanlagen group is an own station in scope; doubts only flag (needs_review), never exclude."""
    df = df.copy()
    df["site_type"], df["site_type_evidence"], df["in_scope"] = "own_station", "listed in store group Prüfanlagen", True
    flags = []
    for r in df.itertuples():
        f = {}
        if WORKSHOP.search(f"{r.name} {r.name_overlay} {r.street}"):
            f["psp_suspect"] = "Autohaus/Werkstatt in name or address"
        if r.website or not r.email.endswith("@tuev-thueringen.de"):
            f["possible_partner"] = f"external website {r.website}" if r.website else "no tuev-thueringen.de e-mail"
        if not r.email:
            f["missing_email"] = "no e-mail listed"
        if " ".join(r.name.split()) != " ".join(r.name_overlay.split()):
            f["name_mismatch"] = "overlay name differs from table name"
        flags.append(f)
    df["doubt_flags"] = [";".join(f) for f in flags]
    df["needs_review"] = [bool(f) for f in flags]
    df["needs_review_reason"] = [enrich.REASON_SEPARATOR.join(f"{k}: {v}" for k, v in f.items()) or None for f in flags]
    return df


def build(html: str, scraped_at: str | None = None, centroids: pd.DataFrame | None = None):
    """parse -> classify -> state and PLZ distance (flag above 15 km). Returns (rows, ids per view)."""
    df, ids = parse(html)
    df = classify(df)
    if centroids is None:
        centroids = geo.load_plz_centroids(geo.ensure_geonames(paths.geonames_dir()))
    df = enrich.enrich(df, centroids, max_distance_km=PLZ_FLAG_KM)
    far = df["plz_distance_km"] > PLZ_FLAG_KM
    df.loc[far, "doubt_flags"] = df.loc[far, "doubt_flags"].map(lambda v: ";".join([x for x in (v, "plz_far") if x]))
    df["scraped_at"] = scraped_at or datetime.now().isoformat(timespec="seconds")
    return df, ids


# ---------------------------------------------------------------- check
def last_pass_count(slug_dir: Path | None = None) -> tuple[str, int] | None:
    """Rows of the newest verified (not *_unverified*) CSV in output/, as (file name, rows)."""
    out = slug_dir or paths.output_dir(SLUG, "full")
    for f in sorted(out.glob(f"*/{SLUG}_pruefstellen_*.csv"), reverse=True):
        if "unverified" not in f.name:
            return f.name, len(pd.read_csv(f, sep=";", encoding="utf-8-sig", dtype=str))
    return None


def check(df: pd.DataFrame, ids: dict, cross_ids: list[str] | None = None, baseline=None) -> list[tuple[str, bool, str]]:
    """Hard checks as (name, ok, detail). Any ok == False means FAIL."""
    out = []
    sets = {k: set(v) for k, v in ids.items()}
    out.append(("ids identical in overlay / table / mobile", len({frozenset(s) for s in sets.values()}) == 1,
                ", ".join(f"{k}={len(v)}" for k, v in ids.items())))
    out.append(("cross-check URL yields the same ids", cross_ids is not None and set(cross_ids) == sets["table"],
                "not fetched" if cross_ids is None else f"{len(set(cross_ids))} ids"))
    dupes = sum(len(v) - len(set(v)) for v in ids.values())
    out.append(("store-ids unique", dupes == 0, f"{dupes} duplicates"))
    lat0, lat1, lng0, lng1 = GERMANY_BOX
    bad = df[~(df["street"].str.len().gt(0) & df["zip"].str.fullmatch(r"\d{5}") & df["city"].str.len().gt(0)
               & df["lat"].between(lat0, lat1) & df["lng"].between(lng0, lng1))]
    out.append(("street, 5-digit PLZ, city, coordinates in Germany", bad.empty,
                f"{len(bad)} bad rows" + (": " + ", ".join(bad["source_id"].str[:8]) if len(bad) else "")))
    if baseline is None:
        out.append((f"count vs last PASS CSV (first run, expect {EXPECTED_FIRST_RUN})", True,
                    f"{len(df)} rows" + ("" if len(df) == EXPECTED_FIRST_RUN else f" (differs from expected {EXPECTED_FIRST_RUN})")))
    else:
        text, alert = export.drop_check(len(df), {"in_scope": baseline[1]}, MAX_DROP)
        out.append((f"count vs last PASS CSV ({baseline[0]})", not alert, text))
    return out


def verdict(checks) -> str:
    return "FAIL" if any(not ok for _, ok, _ in checks) else "PASS"


# ---------------------------------------------------------------- export, scan
def export_run(df: pd.DataFrame, status: str, mode: str, started_at: datetime) -> Path:
    """One CSV in a new run folder (full -> output/, recon -> test_output/); FAIL gets the tag `unverified`."""
    folder = paths.new_run_dir(SLUG, mode, started_at)
    result = export.export(df, folder, SLUG, run_date=f"{started_at:%Y-%m-%d}",
                           tag="unverified" if status == "FAIL" else None, only=("pruefstellen_csv",))
    return result["pruefstellen_csv"][0]


HU_TERMS = re.compile(r"Hauptuntersuchung|\bHU\b|Prüfanlage|Prüfstelle")


def scan_group(html: str) -> list[dict]:
    """Report-only: entries of another store group whose text mentions HU / Prüfanlage / Prüfstelle."""
    soup = BeautifulSoup(html, "html.parser")
    hits = []
    for tr in soup.select(f"#{TABLE_ID} tr[data-store-id]"):
        text = _text(tr)
        terms = sorted(set(HU_TERMS.findall(text)))
        if terms:
            hits.append({"name": _text(tr.find("td")), "terms": terms, "text": text[:160]})
    return hits
