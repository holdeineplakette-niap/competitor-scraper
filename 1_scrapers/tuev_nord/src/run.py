"""TÜV NORD run: the station page (all locations in one HTML attribute), the A-Z list as cross-check, 3 detail pages, checks, one folder per run.

Modes (see 0_instructions/storage_and_naming_convention.md):
  simulated  offline, fake tuev-nord.de with known truth, must PASS (faults can be injected in tests)    -> test_output/
  full       live, plain HTTP, no cookies: stations page + list page + 3 detail pages (+ up to 8 pages that explain a mismatch) -> output/
  rebuild    offline: re-parses and re-checks the saved raw pages of an earlier run folder (source_run)    -> output/

Command line (from the project folder, Windows):
  .venv\\Scripts\\python.exe -m tuev_nord.run simulated
  .venv\\Scripts\\python.exe -m tuev_nord.run full --confirm
  .venv\\Scripts\\python.exe -m tuev_nord.run rebuild --source 1_scrapers\\tuev_nord\\output\\<run folder>

PASS needs every `hard` check to hold (see checks.py). A run that is not a PASS writes its CSVs with `unverified` in the name,
or no CSV at all when there is no usable data. A block, an error page or an empty answer is a FAIL, never "0 stations".
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from core import paths
from core.export import baseline_run, counts, drop_check, relative, safe_path

from tuev_nord import checks, fetch, parse
from tuev_nord.simulate import simulated_outcome

SLUG = "tuev_nord"
RUN_MODES = ("simulated", "full", "rebuild")
STATIONS_FILE, LIST_FILE = "stationen.html", "liste_aller_stationen.html"
SPOT_CHECK_N = 5

EXPORT_COLUMNS = [
    "location_id", "source", "source_id", "source_key", "scraped_at", "name", "name_clean", "place_name", "operator_name", "site_type",
    "site_type_reason", "listed_by", "in_scope", "is_hu_site", "is_office", "service_type", "street", "zip", "city", "latitude", "longitude",
    "plz_distance_km", "station_slug", "detail_url", "link_variant", "link_raw", "in_list_page", "same_id_count", "same_address_count",
    "psp_suspect", "dq_flags", "needs_review", "unknown_source_fields",
]
LOG_COLUMNS = ["time", "what", "url", "status", "seconds", "note"]


@dataclass
class RunResult:
    run_dir: Path
    mode: str
    status: str
    summary: dict
    frames: dict
    written: dict


# ------------------------------------------------------------------------------------------------ choosing detail pages
def _rows_of(outcome: fetch.FetchOutcome):
    page = parse.parse_stations_page(outcome.stations.text)
    return parse.parse_records(page.records)


def pick_details(outcome: fetch.FetchOutcome) -> list[tuple[str, str]]:
    """One own station, one partner station, one HU-only station (serviceType 'HU'): the lowest id of each, all with a page."""
    try:
        rows = sorted((r for r in _rows_of(outcome) if r["station_slug"] and r["is_hu_site"]), key=lambda r: int(r["source_id"]) if r["source_id"].isdigit() else 0)
    except parse.PageError:
        return []
    picked: list[tuple[str, dict]] = []
    wanted = (("own station", lambda r: r["site_type"] == "own_station_probable" and r["service_type"] == "HU-UNG"),
              ("partner station", lambda r: r["site_type"] == "partner_station" and r["service_type"] == "HU-UNG"),
              ("HU-only station", lambda r: r["service_type"] == "HU"))
    for role, rule in wanted:
        row = next((r for r in rows if rule(r) and all(r is not p[1] for p in picked)), None)
        if row is not None:
            picked.append((role, row))
    return [(role, r["detail_url"]) for role, r in picked]


def make_extra_picker(state: dict):
    """Pages to explain stations that are in only one of map and list; `state` receives all candidates (the fetch caps them)."""
    def pick(outcome: fetch.FetchOutcome) -> list[tuple[str, str]]:
        try:
            rows = _rows_of(outcome)
            listing = parse.parse_list_page(outcome.list_page.text)
        except parse.PageError:
            return []
        _, cross = checks.cross_check(rows, listing)
        urls = [("only in the map page", parse.BASE + f"/de/stationen/{x['slug']}/") for x in cross.get("only_in_map", [])]
        urls += [("only in the list page", parse.BASE + it["path"]) for x in cross.get("only_in_list", []) for it in listing.items if it["slug"] == x["slug"]]
        state["candidates"] = len(urls)
        return urls
    return pick


# ------------------------------------------------------------------------------------------------ analysis
def analyze(outcome: fetch.FetchOutcome, centroids, scraped_at: str, prev: dict | None = None, prev_name: str = "") -> dict:
    """All checks, parsed rows and findings for one fetch outcome. No file or network access."""
    res: dict = {"checks": [], "rows": [], "findings": {}, "cross": {}, "previous": {}, "details": [], "extra": [], "page": None, "snapshot": None}
    add = res["checks"].append
    try:
        page = parse.parse_stations_page(outcome.stations.text)
    except parse.PageError as exc:
        add(checks.check("stations page has the expected structure", False, str(exc)))
        return res
    res["page"] = page
    rows = parse.parse_records(page.records, scraped_at)
    for r in rows:
        r["in_list_page"] = ""
        r["in_scope"] = r["is_hu_site"]
    res["checks"] += checks.check_stations_page(outcome.stations.status, page, rows)
    res["findings"] = checks.data_quality(rows, centroids)
    res["checks"] += checks.quality_checks(res["findings"])
    res["rows"] = rows
    res["snapshot"] = checks.snapshot(page)

    listing = None
    try:
        listing = parse.parse_list_page(outcome.list_page.text) if outcome.list_page is not None else None
    except parse.PageError as exc:
        add(checks.check("A-Z list page has the expected structure", False, str(exc)))
    cross_checks, res["cross"] = checks.cross_check(rows, listing) if listing is not None else ([], {})
    res["checks"] += cross_checks

    prev_checks, res["previous"] = checks.compare_previous(prev, res["snapshot"], prev_name)
    res["checks"] += prev_checks

    by_slug = {r["station_slug"]: r for r in rows if r["station_slug"]}
    for d in outcome.details:
        slug = [p for p in d["url"].split("/") if p][-1]
        row = by_slug.get(slug)
        desc = parse.describe_detail(d["html"]) if d["status"] == 200 and d["html"] else {}
        res["details"].append({"role": d["role"], "url": d["url"], "status": d["status"], "description": desc, "location_id": row["location_id"] if row else ""})
        res["checks"] += checks.check_detail(d["role"], d["url"], d["status"], desc, row, row["is_hu_site"] if row else None)
    for d in outcome.extra:
        slug = [p for p in d["url"].split("/") if p][-1]
        desc = parse.describe_detail(d["html"]) if d["status"] == 200 and d["html"] else {}
        exists = d["status"] == 200 and bool(desc) and not desc.get("is_finder_page")
        res["extra"].append({"role": d["role"], "url": d["url"], "status": d["status"], "slug": slug, "page_exists": exists,
                             "h1": desc.get("h1", "") if exists else "", "lists_hu_service": desc.get("lists_hu_service") if exists else None,
                             "address_lines": desc.get("address_lines", []) if exists else [], "partner_note": desc.get("partner_note", "") if exists else "",
                             "map_coords": desc.get("map_coords", []) if exists else [], "booking_station_ids": desc.get("booking_station_ids", []) if exists else [],
                             "service_headings": desc.get("service_headings", []) if exists else [],
                             "note": "" if exists else "HTTP 200, but the server answered with the station finder page: this station page does not exist"})
    return res


def _spot_check(rows: list[dict], run_date: str) -> list[dict]:
    hu = sorted((r for r in rows if r["is_hu_site"]), key=lambda r: r["location_id"])
    pick = random.Random(f"tuev_nord-{run_date}").sample(hu, min(SPOT_CHECK_N, len(hu)))
    return [{"location_id": r["location_id"], "name": r["name_clean"], "street": r["street"], "zip": r["zip"], "city": r["city"],
             "service_type": r["service_type"], "site_type": r["site_type"], "latitude": r["latitude"], "longitude": r["longitude"],
             "detail_url": r["detail_url"]} for r in pick]


def _breakdowns(rows: list[dict]) -> dict:
    s = pd.DataFrame(rows)
    hu = s[s["is_hu_site"]]
    region = lambda f: counts(f["zip"].str[:1].where(f["zip"].str.len() == 5, "?"))  # noqa: E731
    return {
        "service_type": counts(s["service_type"]),
        "is_office": {"true": int(s["is_office"].sum()), "false": int((~s["is_office"]).sum())},
        "site_type": {"all": counts(s["site_type"]), "hu": counts(hu["site_type"])},
        "service_by_site_type": {k: counts(g["service_type"]) for k, g in s.groupby("site_type")},
        "office_service_type": counts(s[s["is_office"]]["service_type"]),
        "hu_by_plz_region": {t: region(hu[hu["site_type"] == t]) for t in sorted(hu["site_type"].unique())} | {"all": region(hu)},
        "hu": int(len(hu)), "hu_with_station_page": int((hu["station_slug"] != "").sum()),
        "psp_suspect": {"all": int(s["psp_suspect"].sum()), "hu": int(hu["psp_suspect"].sum())},
        "operators_with_several_hu_sites": {k: v for k, v in counts(hu[hu["operator_name"] != ""]["operator_name"]).items() if v > 1},
    }


# ------------------------------------------------------------------------------------------------ files
def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")[:40]


def write_raw(run_dir: Path, outcome: fetch.FetchOutcome) -> dict:
    """raw/stationen.html, raw/liste_aller_stationen.html, raw/detail/*.html, raw/extra/*.html (bytes as received). Never overwritten."""
    raw = run_dir / "raw"
    raw.mkdir(parents=True)
    files: dict = {"detail": [], "extra": []}
    if outcome.stations is not None:
        (raw / STATIONS_FILE).write_bytes(outcome.stations.content)
    if outcome.list_page is not None:
        (raw / LIST_FILE).write_bytes(outcome.list_page.content)
    for key, pages in (("detail", outcome.details), ("extra", outcome.extra)):
        if not pages:
            continue
        folder = raw / key
        folder.mkdir()
        for i, p in enumerate(pages, 1):
            slug = [x for x in p["url"].split("/") if x][-1]
            name = f"{key}_{i}_{_safe_name(p['role'])}_{slug}.html"
            (folder / name).write_text(p["html"], encoding="utf-8")
            files[key].append({"file": f"raw/{key}/{name}", "role": p["role"], "url": p["url"], "status": p["status"]})
    return files


def load_outcome(source_run: Path) -> fetch.FetchOutcome:
    """Rebuild a fetch outcome from an earlier run folder (raw/*.html + run_summary.json)."""
    summary = json.loads((source_run / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
    raw = source_run / "raw"

    def resp(url: str, name: str) -> fetch.Response:
        content = (raw / name).read_bytes()
        return fetch.Response(url=url, status=200, text=content.decode("utf-8"), content=content, elapsed_s=0.0, attempts=1, content_type="text/html")

    out = fetch.FetchOutcome(access=summary.get("access", {}))
    out.stations, out.list_page = resp(fetch.STATIONS_URL, STATIONS_FILE), resp(fetch.LIST_URL, LIST_FILE)
    for key, target in (("detail", out.details), ("extra", out.extra)):
        for f in summary.get("raw_files", {}).get(key, []):
            target.append({"role": f["role"], "url": f["url"], "status": f["status"], "elapsed_s": 0.0,
                           "html": (source_run / f["file"]).read_text(encoding="utf-8") if (source_run / f["file"]).exists() else ""})
    return out


def previous_snapshot(output_dir: Path, before: str) -> tuple[dict | None, str]:
    """Structure of the raw stations page of the newest official run older than `before` (a run folder name)."""
    if not output_dir.is_dir():
        return None, ""
    for folder in sorted(output_dir.iterdir(), reverse=True):
        f = folder / "raw" / STATIONS_FILE
        if folder.name >= before or not f.exists():
            continue
        try:
            return checks.snapshot(parse.parse_stations_page(f.read_text(encoding="utf-8"))), folder.name
        except (parse.PageError, OSError, UnicodeDecodeError):
            continue
    return None, ""


def write_query_log(run_dir: Path, log: list[dict]) -> None:
    with open(run_dir / paths.QUERY_LOG_FILE, "x", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(log)


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{c: r.get(c, "") for c in EXPORT_COLUMNS} for r in rows], columns=EXPORT_COLUMNS)


def write_exports(run_dir: Path, frames: dict, run_date: str, tag: str | None) -> dict:
    written, renamed = {}, []
    suffix = f"_{tag}" if tag else ""
    for key in ("all", "hu"):
        frame = frames.get(key)
        if frame is None or not len(frame):
            continue
        path = safe_path(run_dir / f"{SLUG}_{key}_{run_date}{suffix}.csv", renamed)
        frame.to_csv(path, sep=";", encoding="utf-8-sig", index=False)
        written[key] = {"file": path.name, "rows": len(frame)}
    return written


# ------------------------------------------------------------------------------------------------ run
def run(mode: str = "simulated", confirm_full_run: bool = False, root=None, now: datetime | None = None, source_run=None,
        outcome: fetch.FetchOutcome | None = None, centroids=None, fault: str | None = None, client: fetch.NordClient | None = None) -> RunResult:
    """Run one mode. `outcome` (a prepared FetchOutcome) replaces the fetch step, for tests."""
    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {RUN_MODES}, got {mode!r}")
    if mode == "full" and not confirm_full_run and outcome is None:
        raise RuntimeError("A full run makes live requests to tuev-nord.de (stations page, list page, 3 detail pages, up to 8 more). Pass confirm_full_run=True to start it.")
    if mode == "rebuild" and not source_run:
        raise ValueError("mode 'rebuild' needs source_run: the run folder that holds raw/ and run_summary.json")
    started = now or datetime.now()
    t0 = time.time()
    project = paths.project_root() if root is None else Path(root)
    run_dir = paths.new_run_dir(SLUG, mode, started, root)
    run_date, scraped_at = f"{started:%Y-%m-%d}", started.isoformat(timespec="seconds")
    summary: dict = {"mode": mode, "status": "FAIL", "outcome": "FAIL", "run_date": run_date, "started": scraped_at, "run_dir": relative(run_dir, project),
                     "source_run": str(source_run) if source_run else "", "error": "", "in_scope": None, "access": {}}
    extra_state: dict = {}

    # ---- fetch
    try:
        if outcome is None:
            if mode == "simulated":
                outcome = simulated_outcome(fault)
            elif mode == "rebuild":
                outcome = load_outcome(Path(source_run))
            else:
                outcome = fetch.fetch_all(client, pick_details, make_extra_picker(extra_state))
    except fetch.FetchError as exc:
        return _failed(summary, run_dir, str(exc), getattr(exc, "outcome", None), mode, t0)
    except (OSError, ValueError, KeyError) as exc:
        return _failed(summary, run_dir, f"{type(exc).__name__}: {exc}", None, mode, t0)
    summary["access"] = outcome.access
    summary["requests_made"] = outcome.requests_made
    if extra_state.get("candidates", 0) > len(outcome.extra):
        summary["extra_pages_not_fetched"] = extra_state["candidates"] - len(outcome.extra)

    if mode != "rebuild":
        summary["raw_files"] = write_raw(run_dir, outcome)
        write_query_log(run_dir, outcome.log)
    else:
        summary["raw_files"] = json.loads((Path(source_run) / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8")).get("raw_files", {})

    # ---- analyse
    if centroids is None:
        try:
            from core.geo import ensure_geonames, load_plz_centroids
            centroids = load_plz_centroids(ensure_geonames(paths.geonames_dir(root)))
        except Exception as exc:  # no GeoNames file and no network: the PLZ check is skipped and says so
            summary.setdefault("warnings", []).append(f"PLZ centroids unavailable ({type(exc).__name__}): PLZ plausibility check skipped")
    out_dir = paths.output_dir(SLUG, "full", root)
    prev, prev_name = previous_snapshot(out_dir, Path(source_run).name if source_run else run_dir.name)
    res = analyze(outcome, centroids, scraped_at, prev, prev_name)
    all_checks = res["checks"]
    rows = res["rows"]
    n_hu = sum(1 for r in rows if r["is_hu_site"])
    base = baseline_run(out_dir, exclude=run_dir) if mode != "simulated" else None  # fake data is never compared with real runs
    text, alert = drop_check(n_hu, base) if rows else ("not applicable (no rows)", False)
    all_checks.append(checks.check("HU count has not dropped more than 10 % against the last passing full run", not alert, text))

    failed = [c for c in all_checks if c["severity"] == "hard" and not c["ok"]]
    status = "FAIL" if failed or not rows else "PASS"
    frames = {"all": _frame(rows) if rows else None, "hu": _frame([r for r in rows if r["is_hu_site"]]) if rows else None}
    written = write_exports(run_dir, frames, run_date, None if status == "PASS" else "unverified")
    summary.update(status=status, outcome="PASS" if status == "PASS" else f"FAIL: {len(failed)} hard check(s) failed" if failed else "FAIL: no usable data",
                   in_scope=n_hu if rows else None, baseline=base, drop_check=text, checks=all_checks, failed_hard=[c["check"] for c in failed],
                   review=[c for c in all_checks if c["severity"] == "review" and not c["ok"]], findings=res["findings"], cross=res["cross"],
                   previous=res["previous"], written=written, detail_pages=res["details"], extra_pages=res["extra"])
    if rows:
        page = res["page"]
        summary.update(counts={"all": len(rows), "hu": n_hu, "counter": page.counters, "records_on_page": len(page.records)},
                       breakdowns=_breakdowns(rows), spot_check=_spot_check(rows, run_date), map_attributes=page.map_attrs,
                       fields={"record_fields": sorted({k for r in page.records for k in r}),
                               "export_fill": {c: int(frames["all"][c].map(lambda v: v not in ("", None) and not (isinstance(v, float) and pd.isna(v))).sum())
                                               for c in EXPORT_COLUMNS}})
    _finish(summary, run_dir, t0, run_date)
    return RunResult(run_dir, mode, status, summary, {k: v for k, v in frames.items() if v is not None}, written)


def _failed(summary: dict, run_dir: Path, error: str, outcome, mode: str, t0: float) -> RunResult:
    summary.update(status="FAIL", outcome=f"FAIL: {error}", error=error)
    if outcome is not None:
        summary["access"] = outcome.access
        if outcome.log:
            write_query_log(run_dir, outcome.log)
    _finish(summary, run_dir, t0, summary["run_date"])
    return RunResult(run_dir, mode, "FAIL", summary, {}, {})


def _finish(summary: dict, run_dir: Path, t0: float, run_date: str) -> None:
    summary["seconds"] = round(time.time() - t0, 1)
    (run_dir / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run_dir / f"{SLUG}_run_report_{run_date}.md").write_text(render_markdown(summary), encoding="utf-8")


# ------------------------------------------------------------------------------------------------ report
def _table(header: list[str], rows: list[list]) -> list[str]:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]


def _addr(x: dict) -> str:
    return x.get("address") or ""


def render_markdown(s: dict) -> str:
    L = [f"# TÜV NORD run report: {s['status']}", "", f"- **Result:** {s['outcome']}",
         f"- **Mode / date:** {s['mode']} / {s['run_date']} (started {s['started']}, {s.get('seconds', '?')} s, {s.get('requests_made', 0)} live requests)",
         f"- **Run folder:** `{s['run_dir']}`"]
    if s.get("source_run"):
        L.append(f"- **Rebuilt from:** `{s['source_run']}` (no network)")
    for w in s.get("warnings", []):
        L.append(f"- **Warning:** {w}")
    if s.get("error"):
        L += ["", f"**ERROR:** {s['error']}", "", "No data was exported. Nothing here counts as a result."]
    acc = s.get("access", {})
    L += ["", "## 1. Method", "",
          f"- `GET {fetch.STATIONS_URL}`: TYPO3 page; every location is in the attribute `data-locations` (HTML-escaped JSON list) of the map div `[data-station-map=\"station-results\"]`; "
          "the page also prints \"<N> Stationen gefunden\".",
          f"- Access: {acc.get('method', '?')}; **fresh session, no cookies kept or sent** (server cookie names seen, never stored: {', '.join(acc.get('set_cookie_names', [])) or '-'}); "
          f"browser-like headers; server header `{acc.get('server_header', '?')}`; no Cloudflare, no challenge; Playwright not needed and not used; 2-4 s between requests.",
          f"- Cross-check: `GET {fetch.LIST_URL}` (A-Z list, one anchor per station page, compared by page slug).",
          "- Detail pages: 3 stations (one own, one partner, one HU-only), plus the pages needed to explain stations that are in only one of the two sources "
          "(section 4); nothing is crawled beyond that.",
          "- The raw HTML of every page fetched is in `raw/` of the run folder and is never edited. The `link` of an office carries a volatile token (JWT + cHash) that changes on every load; "
          "the CSV keeps only its path, the token stays in the raw HTML and is never used as an id."]
    c = s.get("counts")
    if c:
        b = s["breakdowns"]
        L += ["", "## 2. Counts", "", f"**HU sites (serviceType HU or HU-UNG): {c['hu']}** of {c['all']} records on the page; the page's own counter says {c['counter']}.", ""]
        L += _table(["serviceType", "records"], [[k, v] for k, v in b["service_type"].items()])
        L += ["", f"isOffice = true: {b['is_office']['true']} (serviceType of the offices: {b['office_service_type']}); isOffice = false: {b['is_office']['false']}.", ""]
        keys = list(dict.fromkeys(list(b["site_type"]["all"]) + list(b["site_type"]["hu"])))
        L += _table(["site_type", "all", "HU (in scope)"] + [f"{st}" for st in s["breakdowns"]["service_type"]],
                    [[k, b["site_type"]["all"].get(k, 0), b["site_type"]["hu"].get(k, 0)] + [b["service_by_site_type"].get(k, {}).get(st, 0) for st in b["service_type"]] for k in keys])
        L += ["", f"HU sites with their own station page: {b['hu_with_station_page']} of {b['hu']}. `psp_suspect` (operator text mentions Autohaus / Werkstatt / Stützpunkt / Gelände / bei / Fa. / ÜO): "
              f"{b['psp_suspect']['all']} in all, {b['psp_suspect']['hu']} HU.", "",
              "HU sites per PLZ region (first digit):", ""]
        regions = sorted({d for v in b["hu_by_plz_region"].values() for d in v})
        L += _table(["group"] + regions, [[g] + [v.get(d, 0) for d in regions] for g, v in b["hu_by_plz_region"].items()])
        if b["operators_with_several_hu_sites"]:
            L += ["", "Operators with several HU sites: " + json.dumps(b["operators_with_several_hu_sites"], ensure_ascii=False)]
    L += ["", "## 3. Checks", ""]
    L += _table(["severity", "ok", "check", "detail"], [[x["severity"], "yes" if x["ok"] else "**NO**", x["check"], x["detail"]] for x in s.get("checks", [])])
    if s.get("failed_hard"):
        L += ["", "**Failed hard checks:** " + "; ".join(s["failed_hard"])]
    cr = s.get("cross")
    L += ["", "## 4. Cross-check with the A-Z list", ""]
    if cr:
        L += [f"- Map page: {cr['map_stations']} stations with a page ({cr['map_unique_slugs']} unique slugs) + {cr['offices_without_page']} offices (no page, so they cannot be in the list). "
              f"List page: {cr['list_items']} station links ({cr['list_unique_slugs']} unique slugs), letters {''.join(cr['list_letters'])}, other teaser links: {cr['list_other_links']}.",
              f"- **In both: {cr['both']}** (serviceTypes of those: {cr['service_type_of_both']}) · **only in the map: {len(cr['only_in_map'])}** · **only in the list: {len(cr['only_in_list'])}**.",
              f"- Same slug, different name text: {len(cr['name_differs'])}; same slug, different path variant (`/stationsseiten/`): {len(cr['path_variant_differs'])}. "
              "The list carries no serviceType, so it cannot confirm which stations do HU."]
        if cr["only_in_map"]:
            L += ["", "Only in the map page:", ""]
            L += _table(["slug", "name", "serviceType", "site_type", "address", "same name in the list under slug"],
                        [[x["slug"], x["name"], x["service_type"], x["site_type"], x["address"], x["same_name_in_list_as"] or "none"] for x in cr["only_in_map"]])
        if cr["only_in_list"]:
            L += ["", "Only in the list page:", ""]
            L += _table(["slug", "name (list)", "path variant", "letter", "same name in the map under slug"],
                        [[x["slug"], x["name"], x["variant"], x["letter"], x["same_name_in_map_as"] or "none"] for x in cr["only_in_list"]])
        if cr["name_differs"]:
            L += ["", "Same slug, different name: " + json.dumps(cr["name_differs"][:20], ensure_ascii=False)]
    if s.get("extra_pages"):
        L += ["", "Pages fetched to explain the one-source-only stations:", ""]
        L += _table(["role", "slug", "HTTP", "own page exists", "page heading", "lists HU service", "address", "map coordinates", "partner note / remark"],
                    [[x["role"], x["slug"], x["status"], "yes" if x["page_exists"] else "**NO**", x["h1"] or "-", x["lists_hu_service"], " / ".join(x["address_lines"]) or "-",
                      ", ".join(f"{c.get('lat')}, {c.get('lng')}" for c in x["map_coords"]) or "-", x["partner_note"] or x["note"]] for x in s["extra_pages"]])
        L += ["", "Reading: a map slug whose page does not exist is still a record of the map data (it stays in the CSVs, flagged `not_in_list_page`); list-only pages are **not** in the CSVs "
              "(they have no serviceType and no id in the map data) and are not counted in the HU total. \"Lists HU service\" only says that the page's service headings include "
              "'Hauptuntersuchung (HU)'; it does not prove HU is done on site (compare the address and coordinates of pages that look like product pages)."]
    if s.get("extra_pages_not_fetched"):
        L += ["", f"**{s['extra_pages_not_fetched']} one-source-only page(s) were not fetched (cap of {fetch.MAX_EXTRA_PAGES}).**"]
    f = s.get("findings", {})
    L += ["", "## 5. Flags and findings (nothing is dropped; every record keeps its row)", ""]
    if f:
        L += [f"- **Unclassified site_type ({len(f['unclassified'])}):**"]
        L += [f"  - `{u['location_id']}` · {u['service_type']} · `{u['name']}` · {u['reason']}" for u in f["unclassified"]]
        L += [f"- **Ids used for several addresses ({len(f['ids_reused_with_other_address'])}):** " + json.dumps(f["ids_reused_with_other_address"], ensure_ascii=False),
              f"- **Duplicate keys (id + normalised address) ({len(f['duplicate_source_keys'])}):** {json.dumps(f['duplicate_source_keys'], ensure_ascii=False)}",
              f"- **Addresses listed under several ids ({len(f['addresses_shared_by_several_ids'])}):**"]
        L += [f"  - {x['address']}: " + "; ".join(f"{y['location_id']} {y['service_type']}{' (office)' if y['is_office'] else ''} `{y['name']}`" for y in x["locations"])
              for x in f["addresses_shared_by_several_ids"]]
        L += [f"- **Coordinates shared by records with different PLZ ({len(f['coords_shared_across_plz'])} group(s)):** " + json.dumps(f["coords_shared_across_plz"], ensure_ascii=False),
              f"- Coordinates shared by different addresses inside one PLZ (info): {f['coords_shared_same_plz_other_address']} group(s).",
              f"- **Coordinates outside the Germany box:** {f['coords_outside_germany']} (missing: {f['coords_missing']})",
              f"- **Coordinates far from the PLZ centroid:** {f['coords_far_from_plz']} (threshold {f.get('plz_distance', {}).get('threshold_km', '-')} km; distances {f.get('plz_distance', {})}; check {f['plz_check']})",
              f"- **PLZ not 5 digits / not in GeoNames:** {f['plz_not_5_digits']} / {f['plz_not_in_geonames']}",
              f"- **Records without a street ({len(f['street_missing'])}):** " + json.dumps(f["street_missing"], ensure_ascii=False),
              f"- **Duplicate station slugs:** {f['duplicate_station_slugs']}",
              f"- **HU bounding box:** {f['hu_bbox']}",
              f"- Row flags (records carrying each): {json.dumps(f['flag_counts'], ensure_ascii=False)}"]
    L += ["", "## 6. Comparison with the previous run (structure only)", ""]
    p = s.get("previous")
    L += [json.dumps(p, ensure_ascii=False)] if p else ["- no previous run with a saved stations page (this is the first)."]
    fr = s.get("fields", {})
    L += ["", "## 7. Available fields", "",
          f"- **List level (`data-locations`):** {', '.join(f'`{k}`' for k in fr.get('record_fields', []))}. No phone, e-mail, opening hours, operator or services in the list data.",
          "- **Derived by the adapter:** `is_hu_site` (serviceType HU or HU-UNG), `site_type` (office / partner_station / own_station_probable / unclassified) with `site_type_reason`, `operator_name`, "
          "`place_name`, `station_slug`, `detail_url`, `source_key` (id + normalised address), `location_id`, `listed_by`, `in_list_page`, `same_id_count`, `same_address_count`, `psp_suspect`, `dq_flags`, `needs_review`.",
          "- Non-empty values per CSV column: " + ", ".join(f"{k} {v}" for k, v in fr.get("export_fill", {}).items())]
    L += ["", "## 8. Detail pages (not crawled; the pages below only show what exists)", ""]
    for d in s.get("detail_pages", []):
        desc = d["description"]
        L += [f"- **{d['role']}** `{d['url']}` HTTP {d['status']} ({d['location_id']}):"]
        if desc:
            L += [f"  - heading `{desc.get('h1')}`; address lines {desc.get('address_lines')}; opening hours {desc.get('opening_hours_lines')} (times found: {desc.get('has_opening_hours')}, note \"Hinweis\": {desc.get('has_hinweis')})",
                  f"  - service headings: {desc.get('service_headings')}; lists HU: {desc.get('lists_hu_service')}",
                  f"  - partner block: {desc.get('has_partner_block')}; partner note: {desc.get('partner_note') or '-'}",
                  f"  - phones {desc.get('phones')}; e-mails {desc.get('emails')}; booking links {desc.get('booking_links')}; map coordinates {desc.get('map_coords')}; breadcrumbs {desc.get('breadcrumbs')}; JSON-LD {desc.get('jsonld_types')}"]
    if not s.get("detail_pages"):
        L += ["- none"]
    sc = s.get("spot_check")
    if sc:
        L += ["", f"## 9. Spot-check sample ({len(sc)} random HU sites, seeded by the run date)", ""]
        L += _table(["location_id", "name", "address", "serviceType", "site_type", "lat, lng", "page"],
                    [[x["location_id"], x["name"], f"{x['street']}, {x['zip']} {x['city']}", x["service_type"], x["site_type"], f"{x['latitude']}, {x['longitude']}", x["detail_url"]] for x in sc])
    L += ["", "## 10. Fragile points", "",
          "- The whole result depends on one HTML attribute: `[data-station-map=\"station-results\"][data-locations]`. A redesign, a lazy-loaded list or a changed attribute name fails the run with a clear message (no '0 stations').",
          "- The counter is the page's own number: it proves our parse equals what the page claims, not that the page lists every station. The independent evidence is the A-Z list (section 4); a station in neither stays invisible.",
          "- The A-Z list has names and links only (no serviceType, no address), so it can confirm that a page exists, not that a station does HU.",
          "- `site_type` is a heuristic on the name ('City (Operator)', comma form, plain city name). Names that fit neither are `unclassified`, never guessed. A plain name is only 'probably own'.",
          "- Offices' contact links change on every load (JWT with a timestamp): they are never used as ids. TÜV NORD reuses an id for several offices, so the key is id + normalised address.",
          "- Detail pages are about 400 KB each (the navigation is included), so a full crawl of ~500 pages would be ~200 MB; not done.",
          "- HU at workshop Prüfstützpunkte is probably not listed; offices are appraisal (UNG) only."]
    if s.get("written"):
        L += ["", "## 11. Files", ""] + [f"- `{v['file']}`: {v['rows']} rows" for v in s["written"].values()]
        L += ["- `raw/` (stationen.html, liste_aller_stationen.html, detail/, extra/), `query_log.csv`, `run_summary.json`, this report"]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tuev_nord.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=RUN_MODES)
    ap.add_argument("--confirm", action="store_true", help="needed for 'full': makes live requests")
    ap.add_argument("--source", help="rebuild: the run folder to rebuild from")
    args = ap.parse_args(argv)
    res = run(args.mode, confirm_full_run=args.confirm, source_run=args.source)
    print(f"\n{res.status}: {res.summary['outcome']}")
    print(f"run folder: {res.run_dir}")
    if res.summary.get("counts"):
        print(f"counts: {res.summary['counts']}")
    for c in res.summary.get("checks", []):
        if not c["ok"] and c["severity"] != "info":
            print(f"  [{c['severity']}] {c['check']}: {c['detail']}")
    if res.summary.get("error"):
        print(f"ERROR: {res.summary['error']}")
    return 0 if res.status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
