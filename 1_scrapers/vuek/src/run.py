"""VÜK run: discover the stations, prove the list complete, read each station page, locate it, one folder per run.

Modes (see 0_instructions/storage_and_naming_convention.md):
  simulated  offline, fake vuek-gmbh.de with known truth, must PASS (faults can be injected in tests)      -> test_output/
  sample     live: /ueber-uns/ + the first station page (+ 1 Nominatim request); reports SAMPLE, never PASS -> test_output/
  full       live: robots.txt, /ueber-uns/, both sitemaps, /kontakt/, /hauptuntersuchung/, 5 station pages,
             5 Nominatim requests; about 11 + 5 requests, ~2 minutes; needs confirm_full_run=True            -> output/
  rebuild    offline: re-parses and re-checks the raw files of an earlier run folder (source_runs)          -> output/

Command line (from the project folder, Windows):
  .venv\\Scripts\\python.exe -m vuek.run simulated
  .venv\\Scripts\\python.exe -m vuek.run sample
  .venv\\Scripts\\python.exe -m vuek.run full --confirm
  .venv\\Scripts\\python.exe -m vuek.run rebuild --source 1_scrapers\\vuek\\output\\<run folder>

PASS needs every `hard` check to hold (checks.py). A run that is not a PASS writes its CSV with `unverified` in the name,
or no CSV at all when no station page could be read. A block, an error page or an unreadable page is a FAIL, never "0 stations".
Coordinates: © OpenStreetMap contributors (ODbL), via Nominatim, when `coord_source` is `nominatim`.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from core import enrich, paths
from core.export import baseline_run, drop_check, relative, safe_path
from fsp.robots import disallowing_rule, parse_rules

from vuek import checks, parse
from vuek.fetch import BASE, BlockedError, FetchError, NominatimClient, Response, VuekClient
from vuek.simulate import SimulatedGeocoder, SimulatedVuek

SLUG = "vuek"
SOURCE_ORG = "VÜK"
ID_PREFIX = "VUEK-"
RUN_MODES = ("simulated", "sample", "full", "rebuild")
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL 1.0), geocoded with Nominatim"

URLS = {
    "robots": f"{BASE}/robots.txt", "about": f"{BASE}/ueber-uns/", "sitemap_page": f"{BASE}/sitemap/",
    "sitemap_xml": f"{BASE}/sitemap.xml", "kontakt": f"{BASE}/kontakt/", "hu": f"{BASE}/hauptuntersuchung/",
}
RAW_FILES = {"robots": "robots.txt", "about": "ueber-uns.html", "sitemap_page": "sitemap_page.html",
             "sitemap_xml": "sitemap.xml", "kontakt": "kontakt.html", "hu": "hauptuntersuchung.html"}
# The five stations the brief listed (2026-10-06). The first full run is measured against them; afterwards the last passing
# full run is the baseline.
KNOWN_AT_BUILD = ["/kontakt/pruefstelle-lage/", "/kontakt/pruefstelle-bielefeld/", "/kontakt/pruefstelle-bad-oeynhausen/",
                  "/kontakt/pruefstelle-minden/", "/kontakt/pruefstelle-kirchlengern/"]

COLUMNS = [
    "location_id", "source_org", "source_id", "name", "street", "zip", "city", "state", "country", "lat", "lng",
    "coord_source", "plz_distance_km", "phone", "mobile", "fax", "email", "website", "source_profile_url",
    "opening_hours", "opening_hours_raw", "notice", "services", "hu_evidence", "site_type", "site_type_evidence",
    "in_scope", "scope_reason", "doubt_flags", "needs_review", "needs_review_reason", "content_hash", "is_hdp_site",
    "scraped_at",
]
LOG_COLUMNS = ["time", "label", "url", "status", "bytes", "elapsed_s", "attempts", "note"]
# fields compared for change detection (what the user asked for first: address, phone, hours)
WATCHED = {"address": ("street", "zip", "city"), "phone": ("phone",), "fax": ("fax",), "email": ("email",),
           "hours": ("opening_hours_raw",), "services": ("services",), "notice": ("notice",), "name": ("name",)}
KNOWN_GAPS = [
    "Partner workshops (PSPs) where VÜK also inspects are not listed on the site and are out of scope. They cannot be found "
    "with this adapter; the site only says that VÜK is present in 'zahlreichen Partnerwerkstätten in OWL'.",
]


@dataclass
class RunResult:
    run_dir: Path
    mode: str
    outcome: str
    frame: pd.DataFrame
    summary: dict
    written: dict


def say(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------------------------------------ offline sources
class OfflineClient:
    """Serves the raw files of an earlier run folder with the client interface: re-parse without a request."""

    requests_made = 0

    def __init__(self, source_runs):
        self.dirs = [Path(d) for d in source_runs]

    def get(self, url: str, params=None) -> Response:
        if url.endswith("/robots.txt"):
            rel = RAW_FILES["robots"]
        elif url.endswith("/sitemap.xml"):
            rel = RAW_FILES["sitemap_xml"]
        else:
            path = parse.norm_path(url)
            key = next((k for k, u in URLS.items() if k not in ("robots", "sitemap_xml") and parse.norm_path(u) == path), None)
            rel = RAW_FILES[key] if key else f"stations/{parse.slug_of(path)}.html"
        for d in self.dirs:
            f = d / "raw" / rel
            if f.exists():
                return Response(url=url, status=200, text=f.read_text(encoding="utf-8"), elapsed_s=0.0, attempts=1,
                                content_type="text/html")
        raise FetchError(f"{rel} is not in the raw folder of the source run(s) {[str(d) for d in self.dirs]}")


class OfflineGeocoder:
    """Replays the Nominatim answers saved by an earlier run (raw/nominatim/*.json): no request."""

    requests_made = 0

    def __init__(self, source_runs):
        self.answers: dict[tuple[str, str, str], list] = {}
        for d in source_runs:
            for f in sorted((Path(d) / "raw" / "nominatim").glob("*.json")):
                for att in json.loads(f.read_text(encoding="utf-8")).get("attempts", []):
                    q = att["query"]
                    self.answers[(q.get("street", ""), q.get("postalcode", ""), q.get("city", ""))] = att["hits"]

    def geocode(self, street: str, plz: str, city: str) -> dict:
        if (street, plz, city) not in self.answers:
            raise FetchError(f"no saved Nominatim answer for {street!r}, {plz} {city!r} in the source run(s)")
        hits = self.answers[(street, plz, city)]
        if not hits:
            return {"lat": None, "lng": None, "display_name": "", "raw": []}
        h = hits[0]
        return {"lat": float(h["lat"]), "lng": float(h["lon"]), "display_name": h.get("display_name", ""), "raw": hits}


# ------------------------------------------------------------------------------------------------ logging and raw files
class RunLog:
    """query_log.csv: one line per request (rewritten after every request so a stopped run keeps it)."""

    def __init__(self, path: Path | None):
        self.path, self.rows = path, []

    def add(self, **kw) -> None:
        self.rows.append({k: kw.get(k, "") for k in LOG_COLUMNS})
        if self.path is None:
            return
        with self.path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=LOG_COLUMNS, delimiter=";")
            w.writeheader()
            w.writerows(self.rows)


def fetch_logged(client, log: RunLog, label: str, url: str, raw_path: Path | None = None) -> Response:
    resp = client.get(url)
    log.add(time=datetime.now().isoformat(timespec="seconds"), label=label, url=resp.url, status=resp.status,
            bytes=len(resp.text.encode("utf-8")), elapsed_s=resp.elapsed_s, attempts=resp.attempts)
    if raw_path is not None:  # raw before parsing
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(resp.text, encoding="utf-8")
    return resp


def _try_fetch(client, log, label, url, raw_path):
    """(response, error text). A block is not an error to carry on from: it propagates."""
    try:
        return fetch_logged(client, log, label, url, raw_path), ""
    except BlockedError:
        raise
    except FetchError as exc:
        log.add(time=datetime.now().isoformat(timespec="seconds"), label=label, url=url, note=str(exc))
        return None, str(exc)


# ------------------------------------------------------------------------------------------------ coordinates
def locate(row: dict, geocoder, attempts_out: list) -> dict:
    """Coordinates for one station: from the page if it carries any, else Nominatim (structured query, then fallbacks:
    the city without a district suffix, then without a city). Every attempt is appended to attempts_out (raw)."""
    out = {"lat": None, "lng": None, "coord_source": "", "display_name": "", "note": ""}
    if row["page_lat"] is not None:
        return {**out, "lat": row["page_lat"], "lng": row["page_lng"], "coord_source": "page", "note": row["page_coord_source"]}
    if geocoder is None:
        return {**out, "note": "no geocoder"}
    street, plz, city = row["street"], row["zip"], row["city"]
    base = city.split("-")[0].strip()
    queries = [(street, plz, city)] + ([(street, plz, base)] if base and base != city else []) + [(street, plz, "")]
    for q in queries:
        try:
            res = geocoder.geocode(*q)
        except FetchError as exc:
            attempts_out.append({"query": {"street": q[0], "postalcode": q[1], "city": q[2]}, "hits": [], "error": str(exc)})
            out["note"] = f"geocoder error: {exc}"
            continue
        attempts_out.append({"query": {"street": q[0], "postalcode": q[1], "city": q[2]}, "hits": res["raw"]})
        if res["lat"] is not None:
            nums = re.findall(r"\b\d{5}\b", res["display_name"])
            return {**out, "lat": res["lat"], "lng": res["lng"], "coord_source": "nominatim", "display_name": res["display_name"],
                    "note": f"attempt {len(attempts_out)}" + ("" if q[2] == city else f" (city {q[2]!r})"),
                    "postcode_mismatch": bool(nums) and plz not in nums}
    out["note"] = out["note"] or "Nominatim found nothing"
    return out


# ------------------------------------------------------------------------------------------------ change detection
def station_snapshot(p: dict) -> dict:
    return {k: p.get(k, "") for k in ("path", "name", "street", "zip", "city", "phone", "fax", "email", "opening_hours_raw",
                                       "services", "notice", "content_hash")}


def compare_with_baseline(now: dict[str, dict], before: dict[str, dict] | None) -> dict:
    """Which stations are new, gone, changed (address / phone / hours / ...) or unchanged since the baseline."""
    if before is None:
        return {"baseline": None, "new": [], "removed": [], "changed": {}, "unchanged": [], "note": "no earlier passing full run"}
    changed, unchanged = {}, []
    for slug, cur in now.items():
        old = before.get(slug)
        if old is None:
            continue
        fields = [name for name, keys in WATCHED.items() if any(cur.get(k, "") != old.get(k, "") for k in keys)]
        if cur.get("content_hash") != old.get("content_hash") and not fields:
            fields = ["other page content"]
        (changed.__setitem__(slug, fields) if fields else unchanged.append(slug))
    return {"baseline": True, "new": sorted(set(now) - set(before)), "removed": sorted(set(before) - set(now)),
            "changed": changed, "unchanged": sorted(unchanged)}


def _baseline_stations(base: dict | None, root) -> dict | None:
    if not base:
        return None
    f = paths.output_dir(SLUG, "full", root) / base["run_folder"] / paths.RUN_SUMMARY_JSON
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("stations")
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------ rows
def _centroids(root, live: bool):
    from core.geo import ensure_geonames, load_plz_centroids

    d = paths.geonames_dir(root)
    if (d / "DE.txt").exists():
        return load_plz_centroids(d / "DE.txt")
    return load_plz_centroids(ensure_geonames(d)) if live else None


def build_frame(parsed: list[dict], locs: dict[str, dict], flags: dict[str, list[str]], hu_text: str | None,
                scraped_at: str, centroids, ok: bool) -> pd.DataFrame:
    rows = []
    for p in parsed:
        loc = locs.get(p["slug"], {})
        city = re.sub(r"^VÜK-Prüfstelle\s+", "", p["name"]).strip()
        hu_ev = []
        if p["hu_on_page"]:
            hu_ev.append("station page names Hauptuntersuchung")
        if hu_text and city and city.lower() in hu_text.lower():
            hu_ev.append("HU page names the station")
        rows.append({
            "location_id": f"{ID_PREFIX}{p['slug']}", "source_org": SOURCE_ORG, "source_id": p["slug"], "name": p["name"],
            "street": p["street"], "zip": p["zip"], "city": p["city"], "state": None, "country": None,
            "lat": loc.get("lat"), "lng": loc.get("lng"), "coord_source": loc.get("coord_source", ""), "plz_distance_km": None,
            "phone": p["phone"], "mobile": p["mobile"], "fax": p["fax"], "email": p["email"], "website": f"{BASE}/",
            "source_profile_url": p["source_profile_url"], "opening_hours": p["opening_hours"],
            "opening_hours_raw": p["opening_hours_raw"], "notice": p["notice"], "services": p["services"],
            "hu_evidence": "; ".join(hu_ev), "site_type": "pruefstelle",
            "site_type_evidence": "own station: linked in menu, footer, body text and sitemaps" if ok
            else "own station (completeness NOT proven in this run)",
            "in_scope": True, "scope_reason": "own_station", "doubt_flags": "; ".join(flags.get(p["slug"], [])),
            "needs_review": bool(set(flags.get(p["slug"], [])) & checks.REVIEW_FLAGS), "needs_review_reason": None,
            "content_hash": p["content_hash"], "is_hdp_site": None, "scraped_at": scraped_at,
        })
    df = pd.DataFrame(rows, columns=COLUMNS)
    if len(df) and centroids is not None:
        df = enrich.enrich(df, centroids, max_distance_km=checks.PLZ_FAR_KM)
    else:
        df["plz_distance_km"] = None
    df["country"] = df["zip"].map(lambda z: "DE" if isinstance(z, str) and re.fullmatch(r"\d{5}", z) else None)
    for i, r in df.iterrows():  # our own review flags join the enrich reasons
        own = [f for f in flags.get(r["source_id"], []) if f in checks.REVIEW_FLAGS]
        reasons = enrich.split_reasons(r.get("needs_review_reason")) + own
        df.at[i, "needs_review_reason"] = enrich.REASON_SEPARATOR.join(dict.fromkeys(reasons)) or None
        df.at[i, "needs_review"] = bool(r["needs_review"]) or bool(reasons)
    return df.reindex(columns=COLUMNS)


# ------------------------------------------------------------------------------------------------ run
def run(mode: str = "simulated", confirm_full_run: bool = False, root=None, now: datetime | None = None, client=None,
        geocoder=None, centroids=None, source_runs=None, save_raw: bool = True, accept_station_change: bool = False,
        geocode: bool = True) -> RunResult:
    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {RUN_MODES}, got {mode!r}")
    if mode == "full" and not confirm_full_run:
        raise RuntimeError("A full run sends about 16 live requests (11 to vuek-gmbh.de, 5 to Nominatim). "
                           "Pass confirm_full_run=True to start it.")
    if mode == "rebuild" and not source_runs:
        raise ValueError("mode 'rebuild' needs source_runs: the run folder(s) that hold the raw files")
    live = mode in ("full", "sample")
    if client is None:
        client = {"simulated": SimulatedVuek, "rebuild": lambda: OfflineClient(source_runs)}.get(mode, VuekClient)()
    if geocoder is None and geocode:
        geocoder = {"simulated": SimulatedGeocoder, "rebuild": lambda: OfflineGeocoder(source_runs)}.get(mode, NominatimClient)()
    started = now or datetime.now()
    run_dir = paths.new_run_dir(SLUG, mode, started, root)
    raw_dir = None
    if save_raw and mode != "rebuild":
        raw_dir = run_dir / "raw"
        raw_dir.mkdir()
    date = f"{started:%Y-%m-%d}"
    scraped_at = started.isoformat(timespec="seconds")
    log = RunLog(run_dir / paths.QUERY_LOG_FILE if mode != "rebuild" else None)
    summary: dict = {
        "mode": mode, "status": "FAIL", "outcome": "FAIL", "run_date": date, "started": scraped_at,
        "run_dir": relative(run_dir, paths.project_root() if root is None else Path(root)),
        "source_runs": [str(s) for s in (source_runs or [])], "in_scope": None, "checks": [], "warnings": [], "counts": {},
        "discovery": {}, "stations": {}, "changes": {}, "known_gaps": KNOWN_GAPS, "robots": {}, "error": "",
        "accept_station_change": accept_station_change,
    }
    say(f"[{mode}] run folder: {run_dir}")
    frame = pd.DataFrame(columns=COLUMNS)
    try:
        def raw(name):
            return raw_dir / name if raw_dir else None

        # ---- /ueber-uns/ (a failure here stops the run: there is nothing to prove the list with)
        say("Discovery: /ueber-uns/")
        about_resp = fetch_logged(client, log, "about", URLS["about"], raw(RAW_FILES["about"]))
        about = parse.parse_about(about_resp.text)
        d = summary["discovery"] = {
            "menu": about["menu_set"], "menu_containers": about["menus"], "footer": about["footer_set"],
            "body": about["body_set"], "stated_meta": about["stated_meta"], "stated_body": about["stated_body"],
            "meta_description": about["meta_description"], "partner_workshop_text": about["partner_workshop_text"],
            "sitemap_xml_station_urls": None, "sitemap_page_station_urls": None, "kontakt_hub": None,
        }
        fetch_problems: list[dict] = []
        sitemap_xml = sitemap_page = hub = hu = None
        hu_text = None
        if mode != "sample":
            robots, err = _try_fetch(client, log, "robots", URLS["robots"], raw(RAW_FILES["robots"]))
            if robots:
                disallow, allow, _ = parse_rules(robots.text)
                crawl = re.search(r"(?im)^\s*crawl-delay\s*:\s*(\d+(?:\.\d+)?)", robots.text)
                ours = [u for u in list(URLS.values()) if disallowing_rule(parse.norm_path(u) or "/", disallow, allow)]
                summary["robots"] = {"disallow": disallow, "crawl_delay": float(crawl.group(1)) if crawl else None,
                                     "our_urls_disallowed": ours, "note": "management decision: robots.txt need not be obeyed; "
                                     "the Crawl-Delay is honoured anyway and no disallowed path is requested"}
            else:
                summary["robots"] = {"error": err}
            say("Discovery: sitemap page, sitemap.xml, /kontakt/, /hauptuntersuchung/")
            resp, err = _try_fetch(client, log, "sitemap_page", URLS["sitemap_page"], raw(RAW_FILES["sitemap_page"]))
            if resp:
                sitemap_page = parse.parse_link_page(resp.text)
                d["sitemap_page_station_urls"] = sitemap_page["station_set"]
            else:
                fetch_problems.append(checks.check("HTML sitemap page could be fetched", False, err))
            resp, err = _try_fetch(client, log, "sitemap_xml", URLS["sitemap_xml"], raw(RAW_FILES["sitemap_xml"]))
            if resp:
                try:
                    sitemap_xml = parse.parse_sitemap_xml(resp.text)
                    d["sitemap_xml_station_urls"] = parse.station_paths_of_urls(sitemap_xml)
                    d["sitemap_xml_urls"] = len(sitemap_xml)
                except (ET.ParseError, ValueError) as exc:
                    fetch_problems.append(checks.check("sitemap.xml is a valid sitemap (urlset)", False, f"{type(exc).__name__}: {exc}"))
            else:
                fetch_problems.append(checks.check("sitemap.xml could be fetched", False, err))
            resp, err = _try_fetch(client, log, "kontakt", URLS["kontakt"], raw(RAW_FILES["kontakt"]))
            if resp:
                hub = parse.parse_link_page(resp.text)
                d["kontakt_hub"] = hub["station_set"]
            else:
                fetch_problems.append(checks.check("/kontakt/ page could be fetched", False, err, "review"))
            resp, err = _try_fetch(client, log, "hu", URLS["hu"], raw(RAW_FILES["hu"]))
            if resp:
                hu = parse.parse_hu_page(resp.text)
                hu_text = hu["text"]
                fetch_problems.append(checks.check("the HU page (/hauptuntersuchung/) mentions the Hauptuntersuchung", hu["mentions_hu"],
                                                   "yes" if hu["mentions_hu"] else "no mention", "review"))
            else:
                fetch_problems.append(checks.check("/hauptuntersuchung/ page could be fetched", False, err, "review"))

        # ---- baseline: the station set and the stored snapshot of the last passing full run
        base = baseline = None
        before = None
        if mode in ("full", "rebuild"):
            base = baseline_run(paths.output_dir(SLUG, "full", root), exclude=run_dir)
            before = _baseline_stations(base, root)
            baseline = [v["path"] for v in before.values() if v.get("path")] if before else KNOWN_AT_BUILD
        label = f"the last passing full run ({base['run_folder']})" if before else "the five stations known at build time"
        summary["baseline"] = base
        all_checks, expected = checks.discovery_checks(about, sitemap_xml, sitemap_page, hub, baseline, label, accept_station_change)
        all_checks += fetch_problems

        # ---- station pages
        crawl_paths = expected[:1] if mode == "sample" else expected
        parsed: list[dict] = []
        failed: dict[str, str] = {}
        for i, path in enumerate(crawl_paths, 1):
            slug = parse.slug_of(path)
            say(f"Station {i}/{len(crawl_paths)}: {slug}")
            resp, err = _try_fetch(client, log, f"station_{slug}", parse.station_url(path), raw(f"stations/{slug}.html"))
            if resp is None:
                failed[path] = err
                continue
            parsed.append(parse.parse_station(resp.text, path))
        all_checks += checks.station_checks(parsed, crawl_paths, failed)

        # ---- coordinates (page first, then Nominatim)
        locs: dict[str, dict] = {}
        for p in parsed:
            attempts: list[dict] = []
            if not p["address_ok"]:
                locs[p["slug"]] = {"lat": None, "lng": None, "coord_source": "", "note": "no complete address"}
                continue
            say(f"Coordinates: {p['slug']}")
            locs[p["slug"]] = locate(p, geocoder, attempts)
            if attempts:
                if raw_dir:
                    (raw_dir / "nominatim").mkdir(exist_ok=True)
                    (raw_dir / "nominatim" / f"{p['slug']}.json").write_text(json.dumps(
                        {"slug": p["slug"], "attribution": OSM_ATTRIBUTION, "attempts": attempts}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                for a in attempts:
                    log.add(time=datetime.now().isoformat(timespec="seconds"), label=f"nominatim_{p['slug']}",
                            url="https://nominatim.openstreetmap.org/search", status=200 if "error" not in a else "",
                            note=f"{a['query']} -> {len(a['hits'])} hit(s)" + (f"; {a['error']}" if "error" in a else ""))

        # ---- flags, frame, enrichment
        phones = checks.counts_of([p for p in parsed if p["phone"]], lambda r: re.sub(r"\D", "", r["phone"]))
        addresses = checks.counts_of(parsed, lambda r: parse.normalize_street(f"{r['street']}{r['zip']}"))
        flags: dict[str, list[str]] = {}
        for p in parsed:
            f = checks.station_flags(p, phones, addresses)
            loc = locs[p["slug"]]
            if loc.get("lat") is None:
                f.append("coords_missing")
            if loc.get("postcode_mismatch"):
                f.append("coords_postcode_mismatch")
            flags[p["slug"]] = f
        if centroids is None:
            try:
                centroids = _centroids(root, live)
                if centroids is None:
                    summary["warnings"].append("PLZ centroids unavailable (no GeoNames file; not downloaded in this mode): "
                                               "plz_distance_km not computed")
            except Exception as exc:  # no GeoNames file and no network: the PLZ check is skipped and says so
                summary["warnings"].append(f"PLZ centroids unavailable ({type(exc).__name__}): plz_distance_km not computed")
        failed_hard_so_far = [c for c in all_checks if c["severity"] == "hard" and not c["ok"]]
        frame = build_frame(parsed, locs, flags, hu_text, scraped_at, centroids, ok=not failed_hard_so_far)
        def num(v):
            return None if v is None or pd.isna(v) else float(v)

        coord_rows = [{"slug": r["source_id"], "lat": num(r["lat"]), "lng": num(r["lng"]), "plz_distance_km": num(r["plz_distance_km"]),
                       "flags": flags.get(r["source_id"], [])} for _, r in frame.iterrows()]
        all_checks += checks.coordinate_checks(coord_rows)

        # ---- change detection and drop check
        stations = {p["slug"]: station_snapshot(p) for p in parsed}
        for p in parsed:
            stations[p["slug"]].update(flags=flags[p["slug"]], coord_source=locs[p["slug"]].get("coord_source", ""),
                                       lat=locs[p["slug"]].get("lat"), lng=locs[p["slug"]].get("lng"),
                                       geocode_note=locs[p["slug"]].get("note", ""),
                                       geocode_display_name=locs[p["slug"]].get("display_name", ""),
                                       hu_on_page=p["hu_on_page"], map_query=p["map_query"], title=p["title"])
        summary["stations"] = stations
        summary["changes"] = compare_with_baseline(stations, before) if mode in ("full", "rebuild") else {
            "baseline": None, "note": f"not applicable in {mode} mode"}
        if base:
            text, alert = drop_check(len(frame), base)
            summary["drop_check"] = text
            all_checks.append(checks.check("in-scope count has not dropped more than 10 % against the last passing full run",
                                           not alert, text, "info"))
        else:
            summary["drop_check"] = "no earlier passing full run" if mode in ("full", "rebuild") else f"not applicable in {mode} mode"

        failed_hard = [c for c in all_checks if c["severity"] == "hard" and not c["ok"]]
        summary["checks"] = all_checks
        summary["failed_hard"] = [c["check"] for c in failed_hard]
        status = "FAIL" if failed_hard else ("SAMPLE" if mode == "sample" else "PASS")
        summary.update(status=status, outcome=status if not failed_hard else f"FAIL: {len(failed_hard)} hard check(s) failed",
                       in_scope=int(frame["in_scope"].sum()) if len(frame) else 0)
        summary["counts"] = {
            "stations_in_menu": len(about["menu_set"]), "stations_crawled": len(crawl_paths), "stations_read": len(parsed),
            "in_scope": summary["in_scope"], "needs_review": int(frame["needs_review"].sum()) if len(frame) else 0,
            "stated_by_site": [s["value"] for s in about["stated_meta"]],
            "coord_source": checks.counts_of(coord_rows, lambda r: locs[r["slug"]].get("coord_source") or "none"),
        }
    except Exception as exc:  # a block, an unreadable /ueber-uns/ or a bug: the folder keeps what was fetched, the summary says why
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["outcome"] = f"FAIL: {summary['error']}"
        say(f"STOPPED: {summary['error']}")
        _finish(run_dir, summary, client, geocoder)
        raise

    written = {}
    if len(frame):
        tag = "" if summary["status"] in ("PASS", "SAMPLE") else "unverified_"
        path = safe_path(run_dir / f"vuek_pruefstellen_{tag}{date}.csv")
        frame.to_csv(path, sep=";", encoding="utf-8-sig", index=False)
        written["pruefstellen_csv"] = (path, len(frame))
    summary["written"] = {k: {"file": v[0].name, "rows": v[1]} for k, v in written.items()}
    _finish(run_dir, summary, client, geocoder)
    say(f"[{mode}] {summary['outcome']}: {len(frame)} station(s) -> {', '.join(v[0].name for v in written.values()) or 'no CSV'}")
    return RunResult(run_dir, mode, summary["status"], frame, summary, written)


# ------------------------------------------------------------------------------------------------ summary files
def _finish(run_dir: Path, summary: dict, client, geocoder) -> None:
    summary["requests_made"] = {"vuek": getattr(client, "requests_made", 0), "nominatim": getattr(geocoder, "requests_made", 0)}
    (run_dir / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (run_dir / paths.RUN_SUMMARY_MD).write_text(render_markdown(summary), encoding="utf-8")


def _table(header: list[str], rows: list[list]) -> list[str]:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]


def render_markdown(s: dict) -> str:
    L = [f"# VÜK run report: {s['status']}", "", f"- **Result:** {s['outcome']}",
         f"- **Mode / date:** {s['mode']} / {s['run_date']} (started {s['started']}) · requests: {s.get('requests_made', '-')}",
         f"- **Run folder:** `{s['run_dir']}`"]
    if s.get("source_runs"):
        L.append("- **Rebuilt offline from:** " + ", ".join(f"`{x}`" for x in s["source_runs"]))
    if s.get("error"):
        L += ["", f"**STOPPED: {s['error']}**", "", "Nothing here counts as a result."]
    for w in s.get("warnings", []):
        L += ["", f"**{w}**"]
    if s.get("accept_station_change"):
        L += ["", "**The caller accepted a change of the station set (`accept_station_change=True`).**"]
    c = s.get("counts", {})
    d = s.get("discovery", {})
    if c:
        L += ["", f"**Stations in scope: {c.get('in_scope')}** (menu {c.get('stations_in_menu')}, crawled {c.get('stations_crawled')}, "
              f"read {c.get('stations_read')}) · the site states: {c.get('stated_by_site')} · needs review: {c.get('needs_review')} · "
              f"coordinate source: {c.get('coord_source')}"]
    if d:
        L += ["", "## Completeness: the station sets", ""]
        L += _table(["source", "stations"], [
            ["menu (desktop and mobile)", len(d.get("menu", []))], ["footer 'VÜK Standorte'", len(d.get("footer", []))],
            ["body text of /ueber-uns/", len(d.get("body", []))],
            ["sitemap.xml", "-" if d.get("sitemap_xml_station_urls") is None else len(d["sitemap_xml_station_urls"])],
            ["HTML sitemap page", "-" if d.get("sitemap_page_station_urls") is None else len(d["sitemap_page_station_urls"])],
            ["/kontakt/ page (review only)", "-" if d.get("kontakt_hub") is None else len(d["kontakt_hub"])]])
        L += ["", f"Station links: {', '.join(d.get('menu', [])) or 'none'}", "",
              f"Meta description: _{d.get('meta_description', '')}_  \nBody text: _{d.get('partner_workshop_text') or 'no sentence about partner workshops found'}_"]
    L += ["", "## Checks", ""]
    verdict = {"hard": "**NO**", "review": "**NO** (review)", "info": "no (info only)"}
    L += _table(["severity", "ok", "check", "detail"],
                [[k["severity"], "yes" if k["ok"] else verdict[k["severity"]], k["check"], k["detail"]] for k in s.get("checks", [])])
    if s.get("failed_hard"):
        L += ["", "**Failed hard checks:** " + "; ".join(s["failed_hard"])]
    st = s.get("stations", {})
    if st:
        L += ["", "## Stations", ""]
        L += _table(["station", "address", "phone", "hours", "coordinates", "flags"], [
            [k, f"{v['street']}, {v['zip']} {v['city']}", v["phone"], v["opening_hours_raw"],
             f"{v.get('coord_source') or 'none'} ({v.get('lat')}, {v.get('lng')})", "; ".join(v.get("flags", []))] for k, v in st.items()])
        if any(v.get("coord_source") == "nominatim" for v in st.values()):
            L += ["", f"Coordinates: {OSM_ATTRIBUTION}."]
    ch = s.get("changes", {})
    L += ["", "## Changes since the last passing full run", ""]
    if ch.get("baseline"):
        L += [f"- Baseline: {(s.get('baseline') or {}).get('run_folder')}",
              f"- New: {ch['new'] or 'none'} · removed: {ch['removed'] or 'none'}",
              f"- Changed: {ch['changed'] or 'none'}", f"- Unchanged: {ch['unchanged'] or 'none'}"]
    else:
        L += [f"- {ch.get('note', 'not available')}"]
    L += ["", "## Known gaps", ""] + [f"- {g}" for g in s.get("known_gaps", [])]
    rb = s.get("robots") or {}
    if rb:
        L += ["", "## robots.txt", "", f"- Crawl-Delay {rb.get('crawl_delay')}, Disallow {rb.get('disallow')}, our URLs disallowed: {rb.get('our_urls_disallowed')}. {rb.get('note', '')}"]
    if s.get("written"):
        L += ["", "## Files", ""] + [f"- `{v['file']}`: {v['rows']} rows" for v in s["written"].values()]
        L += [f"- `{paths.QUERY_LOG_FILE}`, `{paths.RUN_SUMMARY_JSON}`, `{paths.RUN_SUMMARY_MD}`, `raw/` (raw HTML per page, Nominatim answers)"]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vuek.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=RUN_MODES)
    ap.add_argument("--confirm", action="store_true", help="needed for 'full': sends live requests")
    ap.add_argument("--source", nargs="+", help="rebuild: the run folder(s) that hold the raw files")
    ap.add_argument("--accept-station-change", action="store_true",
                    help="the station set changed on purpose (new or closed station): do not fail on it")
    args = ap.parse_args(argv)
    try:
        res = run(args.mode, confirm_full_run=args.confirm, source_runs=args.source, accept_station_change=args.accept_station_change)
    except (BlockedError, FetchError) as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        return 1
    except (RuntimeError, ValueError) as exc:  # e.g. 'full' without --confirm: nothing was sent
        print(f"\nNOT STARTED: {exc}")
        return 2
    print(f"\n{res.outcome}")
    print(f"run folder: {res.run_dir}")
    print(f"counts: {res.summary['counts']}")
    for k in res.summary["checks"]:
        if not k["ok"] and k["severity"] != "info":
            print(f"  [{k['severity']}] {k['check']}: {k['detail']}")
    return 0 if res.outcome in ("PASS", "SAMPLE") else 1


if __name__ == "__main__":
    sys.exit(main())
