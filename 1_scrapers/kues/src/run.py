"""KÜS run orchestration: plan the queries, collect, prove coverage (rules A-D), classify, export.

    from kues import run as kues_run
    kues_run.run(mode="simulated")                    # offline fake API, must PASS
    kues_run.run(mode="sample")                       # live, at most 5 requests, never PASS
    kues_run.run(mode="recon", source_file=...)       # offline parse of a saved response
    kues_run.run(mode="rebuild", source_run=...)      # offline rebuild from an earlier run's raw.jsonl
    kues_run.run(mode="full", confirm_full_run=True)  # live, whole country

Coverage design (details and numbers: 0_instructions/sources/kues.md, shared code: core/radius_cover.py): the finder
answers "every Partner within 50 km of this PLZ" and reports no total, so completeness is proved by four rules:
A every PLZ centroid within COVER_KM of an actual server centre, B every collected site within 45 km of a centre,
C every centre within 48 km of a site returned that site, D no count cap. Every run writes one folder
(core/paths.py, 0_instructions/storage_and_naming_convention.md).
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from core import enrich, export as core_export, paths
from core import radius_cover as rc
from core.geo import ensure_geonames, haversine_km, load_plz_centroids
from core.grid import StopRun
from kues import classify, fetch, parse, simulate

SLUG = parse.SLUG
SOURCE_ORG = parse.SOURCE_ORG
MODES = ("simulated", "sample", "recon", "rebuild", "full")
SAMPLE_PLZ = "80636"
SAMPLE_MAX_REQUESTS = 5
FULL_RUN_NOT_CONFIRMED = (
    "A full KÜS run makes live requests (about 90 queries, 180 requests with both lists, roughly 13 minutes at 3 s + jitter). "
    "Pass confirm_full_run=True to start it."
)
EXPORT_KEYS = core_export.EXPORT_KEYS
MAX_DROP = core_export.MAX_DROP

DEFAULT_RADIUS_KM = 100  # the production crawl: 91 queries instead of ~370, identical result to 50 km (2026-10-05 cross-check)
DEFAULT_COVER_KM = 50


def default_cfg() -> rc.CoverConfig:
    return rc.CoverConfig.for_radius(DEFAULT_RADIUS_KM, cover_km=DEFAULT_COVER_KM)


CENTRE_OUTLIER_KM = 8.0  # a server centre this far from the GeoNames centroid is listed (recon: 0.6-5.2 km)
RADIUS_SHRUNK_FRACTION = 0.6  # a response with >= RADIUS_CHECK_MIN records whose farthest site is nearer than this share of the radius
RADIUS_CHECK_MIN = 20
RADIUS_TOLERANCE_KM = 1.0  # entfernung above radius + this = the radius grew
RAW_SEP = "__"  # raw.jsonl file names: <list>__<zip>.json
LOG_COLUMNS = [
    "seq", "list", "zip", "city", "reason", "status", "http_status", "bytes", "elapsed_s", "records", "max_entfernung_km",
    "centre_lat", "centre_lng", "centre_offset_km", "time",
]
SAVED_SAMPLE_GLOB = "partnersuche_{plz}_*.json"  # in 4_reference/api_samples/kues/


@dataclass
class RunResult:
    run_folder: Path
    summary: dict
    frame: pd.DataFrame
    files: dict


# --------------------------------------------------------------------------
# Responses -> observations, sites, merged records
# --------------------------------------------------------------------------

def observation(resp: fetch.Response) -> rc.Observation:
    return rc.Observation(
        key=resp.zip,
        centre=resp.centre,
        ids=frozenset(str(r["adressid"]) for r in resp.records),
        count=len(resp.records),
        max_dist_km=resp.max_entfernung_km,
    )


def located_sites(responses: list[fetch.Response]) -> pd.DataFrame:
    """Unique sites (first seen) with coordinates, as (id, lat, lng). Sites without coordinates are not listed."""
    seen: dict = {}
    for resp in responses:
        for r in resp.records:
            lat, lng = parse.to_float(r.get("lat")), parse.to_float(r.get("lon"))
            if lat is None or lng is None:
                continue
            seen.setdefault(str(r["adressid"]), (lat, lng))
    return pd.DataFrame([(k, v[0], v[1]) for k, v in seen.items()], columns=["id", "lat", "lng"])


def merge_records(responses: list[fetch.Response]):
    """Unique records by adressid (first seen wins), which lists returned each, and content conflicts.

    A conflict is the same adressid with different content in two responses (ignoring the query-dependent fields).
    Returns (records {id: record}, lists {id: set of list names}, conflicts [{adressid, fields}]).
    """
    records: dict = {}
    lists: dict = {}
    conflicts: dict = {}
    for resp in responses:
        for r in resp.records:
            key = str(r["adressid"])
            lists.setdefault(key, set()).add(resp.list_name)
            if key not in records:
                records[key] = r
                continue
            diff = sorted(
                f for f in set(records[key]) | set(r)
                if f not in parse.QUERY_DEPENDENT_FIELDS and records[key].get(f) != r.get(f)
            )
            if diff:
                conflicts.setdefault(key, set()).update(diff)
    return records, lists, [{"adressid": k, "fields": sorted(v)} for k, v in conflicts.items()]


def kues_list_value(names: set) -> str:
    if names == {fetch.LIST_KUES, fetch.LIST_TECHNIK}:
        return "both"
    return "partnersuche" if names == {fetch.LIST_KUES} else "kues_technik"


# --------------------------------------------------------------------------
# Collecting
# --------------------------------------------------------------------------

class Collector:
    """Runs queries (one PLZ = one request per list), keeps the responses, the log and the raw file."""

    def __init__(self, api, points: pd.DataFrame, cfg: rc.CoverConfig, lists, raw_file=None, say=print, radius_km=None):
        self.api, self.points, self.cfg, self.lists = api, points, cfg, tuple(lists)
        self.raw_file, self.say, self.radius_km = raw_file, say, radius_km
        self.city = dict(zip(points["zip"], points["city"]))
        self.centroid = points.set_index("zip")[["lat", "lng"]]
        self.responses: list[fetch.Response] = []
        self.log: list[dict] = []
        self.queried: set = set()
        self.obs: dict[str, list] = {name: [] for name in self.lists}
        self.site_zip: dict = {}
        self._sites: dict = {}

    def query(self, zip_code: str, reason: str) -> None:
        """All lists for one PLZ. A StopRun from the API is logged as an error row and re-raised."""
        for name in self.lists:
            row = {"seq": len(self.log) + 1, "list": name, "zip": zip_code, "city": self.city.get(zip_code), "reason": reason,
                   "time": datetime.now().isoformat(timespec="seconds")}
            try:
                data, info = self.api.get(name, zip_code, self.radius_km)
            except StopRun as exc:
                self.log.append({**row, "status": f"error: {type(exc).__name__}: {exc}"})
                raise
            raw_name = f"{name}{RAW_SEP}{zip_code}.json"
            resp = fetch.to_response(name, zip_code, data, info, raw_name)
            self.responses.append(resp)
            if self.raw_file:
                self.raw_file.write(json.dumps({"file": raw_name, "response": data}, ensure_ascii=False) + "\n")
                self.raw_file.flush()
            self.obs[name].append(observation(resp))
            if name == fetch.LIST_KUES:
                self._remember_sites(resp)
            self.log.append({**row, **response_log_fields(resp, self.centroid)})
        self.queried.add(zip_code)

    def _remember_sites(self, resp: fetch.Response) -> None:
        """Located sites and the PLZ to query if one turns out to be a fringe site (rule B)."""
        known = set(self.points["zip"])
        for r in resp.records:
            key = str(r["adressid"])
            lat, lng = parse.to_float(r.get("lat")), parse.to_float(r.get("lon"))
            if lat is None or lng is None or key in self._sites:
                continue
            self._sites[key] = (lat, lng)
            own = parse.zip_of(r.get("plz"))
            if own not in known:  # not a regular PLZ: use the nearest one to the coordinates
                d = haversine_km(lat, lng, self.points["lat"].to_numpy(), self.points["lng"].to_numpy())
                own = str(self.points["zip"].iloc[int(np.argmin(d))])
            self.site_zip[key] = own

    def sites_frame(self) -> pd.DataFrame:
        return pd.DataFrame([(k, v[0], v[1]) for k, v in self._sites.items()], columns=["id", "lat", "lng"])

    def fill_in(self) -> int:
        """Rule A/B fill-in queries until nothing is left to cover or the bound is reached. Returns the number of queries."""
        main = self.obs[fetch.LIST_KUES]
        done = 0
        while done < self.cfg.max_fill_requests:
            batch = rc.next_fill_queries(self.points, main, self.sites_frame(), self.site_zip, self.cfg, self.queried)
            if not batch:
                break
            for zip_code, reason in batch:
                if done >= self.cfg.max_fill_requests:
                    break
                self.query(zip_code, reason)
                done += 1
        return done


def response_log_fields(resp: fetch.Response, centroid: pd.DataFrame) -> dict:
    offset = None
    if resp.centre is not None and resp.zip in centroid.index:
        c = centroid.loc[resp.zip]
        offset = round(float(haversine_km(resp.centre[0], resp.centre[1], c["lat"], c["lng"])), 2)
    return {
        "status": "ok" if resp.records else "empty",
        "http_status": resp.http_status,
        "bytes": resp.bytes,
        "elapsed_s": resp.elapsed_s,
        "records": len(resp.records),
        "max_entfernung_km": round(resp.max_entfernung_km, 2) if resp.max_entfernung_km is not None else None,
        "centre_lat": resp.centre[0] if resp.centre else None,
        "centre_lng": resp.centre[1] if resp.centre else None,
        "centre_offset_km": offset,
    }


# --------------------------------------------------------------------------
# Rules and warnings
# --------------------------------------------------------------------------

def check_rules(points: pd.DataFrame, responses: list[fetch.Response], cfg: rc.CoverConfig) -> dict:
    """Rules A-D on the main list; rules C and D again on the technik list (if it was collected)."""
    main = [r for r in responses if r.list_name == fetch.LIST_KUES]
    tech = [r for r in responses if r.list_name == fetch.LIST_TECHNIK]
    rules = rc.evaluate(points, [observation(r) for r in main], located_sites(main), cfg, {r.zip for r in main if not r.records})
    if tech:
        obs = [observation(r) for r in tech]
        c = rc.appearance_misses(located_sites(tech), obs, cfg.consistency_km)
        d = rc.cap_findings(obs, cfg)
        rules["rule_c_technik"] = {"ok": not c, "misses": c}
        rules["rule_d_technik"] = {"ok": not d, "findings": d}
    return rules


def collect_warnings(responses: list[fetch.Response], points: pd.DataFrame, cfg: rc.CoverConfig, conflicts: list, lists: dict) -> dict:
    """Things that are not failures but should be looked at (unattended runs: nobody watches the console)."""
    centroid = points.set_index("zip")[["lat", "lng"]]
    main = [r for r in responses if r.list_name == fetch.LIST_KUES]
    sites = located_sites(main)
    empty, far, shrunk, grown, mixed = [], [], [], [], []
    for r in main:
        if not r.records:
            populated = None
            if r.zip in centroid.index and len(sites):
                d = haversine_km(centroid.loc[r.zip, "lat"], centroid.loc[r.zip, "lng"], sites["lat"].to_numpy(), sites["lng"].to_numpy())
                populated = bool((d <= cfg.radius_km).any())
            empty.append({"zip": r.zip, "sites_within_radius_of_other_responses": populated})
            continue
        offset = response_log_fields(r, centroid)["centre_offset_km"]
        if offset is not None and offset > CENTRE_OUTLIER_KM:
            far.append({"zip": r.zip, "centre_offset_km": offset})
        if r.max_entfernung_km is not None:
            if len(r.records) >= RADIUS_CHECK_MIN and r.max_entfernung_km < RADIUS_SHRUNK_FRACTION * cfg.radius_km:
                shrunk.append({"zip": r.zip, "records": len(r.records), "max_entfernung_km": round(r.max_entfernung_km, 2)})
            if r.max_entfernung_km > cfg.radius_km + RADIUS_TOLERANCE_KM:
                grown.append({"zip": r.zip, "max_entfernung_km": round(r.max_entfernung_km, 2)})
        if r.mixed_centres:
            mixed.append(r.zip)
    technik_only = sorted(k for k, v in lists.items() if v == {fetch.LIST_TECHNIK})
    return {
        "empty_responses": empty,
        "centre_far_from_geonames": far,
        "radius_smaller_than_expected": shrunk,
        "radius_larger_than_expected": grown,
        "mixed_centres_in_a_response": mixed,
        "content_conflicts": conflicts,
        "technik_only_ids": technik_only,
    }


def status_of(mode: str, aborted: str | None, not_queried: int, rules: dict, drop_alert: bool) -> tuple[str, list[str]]:
    reasons = []
    if aborted:
        reasons.append(f"run aborted: {aborted}")
    if not_queried:
        reasons.append(f"{not_queried} planned queries were not made")
    names = {"rule_a": "A coverage", "rule_b": "B fringe", "rule_c": "C appearance consistency", "rule_d": "D cap detector",
             "rule_c_technik": "C (technik list)", "rule_d_technik": "D (technik list)"}
    for key, label in names.items():
        if key in rules and not rules[key]["ok"]:
            reasons.append(f"rule {label} failed")
    if drop_alert:
        reasons.append("drop check alert")
    if mode in ("sample", "recon"):
        return mode.upper(), reasons
    return ("FAIL" if reasons else "PASS"), reasons


def export_tag(status: str, aborted, not_queried: int, rules: dict) -> str | None:
    """Exports of a run that is not a PASS carry a label so nobody mistakes them for a verified crawl."""
    if status in ("PASS", "SAMPLE", "RECON"):
        return None
    if aborted or not_queried or not rules.get("rule_a", {}).get("ok", True) or not rules.get("rule_b", {}).get("ok", True):
        return "grid-incomplete"
    return "unverified"


# --------------------------------------------------------------------------
# Processing (shared by every mode)
# --------------------------------------------------------------------------

def build_frame(responses: list[fetch.Response], centroids: pd.DataFrame, scraped_at: str, hdp_rules=None):
    """records -> parsed, enriched, classified frame. Returns (frame, conflicts, lists)."""
    records, lists, conflicts = merge_records(responses)
    df = parse.build_frame(list(records.values()), scraped_at)
    if df.empty:
        return df, conflicts, lists
    df["kues_list"] = [kues_list_value(lists[str(i)]) for i in df["source_id"]]
    df = parse.add_counts(df)
    df = parse.enrich_german(df, centroids)
    df = classify.classify_frame(df)
    df = parse.mark_hdp(df, hdp_rules)
    return df, conflicts, lists


def _csv_write(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "x", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def saved_sample_comparison(responses: list[fetch.Response], plz: str, root: Path) -> dict | None:
    """Sample mode: compare the live answer with the saved recon response of the same PLZ (4_reference/api_samples/kues/)."""
    folder = paths.reference_dir(root) / "api_samples" / SLUG
    candidates = sorted(folder.glob(SAVED_SAMPLE_GLOB.format(plz=plz))) if folder.is_dir() else []
    live = next((r for r in responses if r.list_name == fetch.LIST_KUES and r.zip == plz), None)
    if not candidates or live is None:
        return None
    saved = json.loads(candidates[0].read_text(encoding="utf-8"))
    by_saved = {str(r["adressid"]): r for r in saved}
    by_live = {str(r["adressid"]): r for r in live.records}
    changed = sorted(
        k for k in set(by_saved) & set(by_live)
        if any(by_saved[k].get(f) != by_live[k].get(f) for f in set(by_saved[k]) | set(by_live[k]))
    )
    return {
        "saved_file": candidates[0].name,
        "saved_records": len(saved),
        "live_records": len(live.records),
        "same_ids": set(by_saved) == set(by_live),
        "same_order": list(by_saved) == list(by_live),
        "only_in_saved": sorted(set(by_saved) - set(by_live)),
        "only_live": sorted(set(by_live) - set(by_saved)),
        "records_with_changed_content": changed,
    }


def process(
    *,
    mode: str,
    run_folder: Path,
    root: Path,
    started: datetime,
    t0: float,
    responses: list[fetch.Response],
    log_rows: list[dict],
    centroids: pd.DataFrame,
    cfg: rc.CoverConfig,
    aborted: str | None = None,
    plan: dict | None = None,
    exports=EXPORT_KEYS,
    keep_raw: bool = False,
    extra: dict | None = None,
    sample_plz: str | None = None,
    say=print,
) -> RunResult:
    scraped_at = started.isoformat(timespec="seconds")
    run_date = started.date().isoformat()
    if log_rows:
        _csv_write(run_folder / paths.QUERY_LOG_FILE, log_rows, LOG_COLUMNS)

    df, conflicts, lists = build_frame(responses, centroids, scraped_at, parse.load_hdp_rules(root=root))
    rules = check_rules(centroids, responses, cfg) if mode in ("full", "simulated", "rebuild") and responses else {}
    warnings = collect_warnings(responses, centroids, cfg, conflicts, lists)
    in_scope = df[df["in_scope"]] if len(df) else df

    baseline = core_export.baseline_run(paths.output_dir(SLUG, "full", root), run_folder) if mode in ("full", "rebuild") else None
    drop_text, drop_alert = "not run", False
    if mode in ("full", "simulated", "rebuild"):
        drop_text, drop_alert = core_export.drop_check(len(in_scope), baseline)
        if mode == "simulated":
            drop_text, drop_alert = "not run (simulated)", False
    not_queried = (plan or {}).get("not_queried", 0)
    status, reasons = status_of(mode, aborted, not_queried, rules, drop_alert)
    outcome = {"PASS": "PASS (coverage rules A-D hold)", "FAIL": "FAIL", "SAMPLE": "SAMPLE (no coverage proof)", "RECON": "RECON (offline parse)"}[status]

    renamed: list = []
    tag = export_tag(status, aborted, not_queried, rules)
    written = core_export.export(df, run_folder, SLUG, run_date, renamed, tag, only=exports) if len(df) else {}

    flags: dict = {}
    for value in df["doubt_flags"].dropna() if len(df) else []:
        for f in value.split("; "):
            flags[f] = flags.get(f, 0) + 1
    summary = {
        "source": SOURCE_ORG,
        "mode": mode,
        "status": status,
        "outcome": outcome,
        "status_reasons": reasons,
        "aborted": aborted,
        "run_date": run_date,
        "started_at": scraped_at,
        "runtime_s": round(time.time() - t0, 1),
        "requests_made": len(log_rows),
        "plan": plan,
        "coverage": {k: _compact_rule(v) for k, v in rules.items()},
        "centres": rules.get("centres"),
        "warnings": {k: v for k, v in warnings.items() if v},
        "rows_total": int(len(df)),
        "in_scope": int(len(in_scope)),
        "site_types": core_export.counts(df["site_type"]) if len(df) else {},
        "in_scope_psp": core_export.counts(in_scope["psp"].fillna("unknown")) if len(df) else {},
        "kues_list": core_export.counts(df["kues_list"]) if len(df) else {},
        "flags": flags,
        "needs_review": int(df["needs_review"].sum()) if len(df) else 0,
        "review_reasons": enrich.reason_counts(df) if len(df) else {},
        "plz_only": int((df["location_precision"] == "plz_only").sum()) if len(df) else 0,
        "in_scope_by_state": core_export.counts(in_scope["state"].fillna("unknown")) if len(df) else {},
        "is_hdp_site": None if not len(df) or df["is_hdp_site"].isna().all() else int(df["is_hdp_site"].sum()),
        "drop_check": drop_text,
        "drop_alert": drop_alert,
        "baseline": baseline,
        "save_raw": keep_raw,
        "cover_config": {k: getattr(cfg, k) for k in ("radius_km", "cover_km", "fringe_km", "consistency_km", "lattice_factor")},
        "exports": list(exports),
        "files": {"run_folder": core_export.relative(run_folder, root)}
        | {key: core_export.relative(path, root) for key, (path, _rows) in written.items()},
    }
    if mode == "sample" and sample_plz:
        summary["sample_comparison"] = saved_sample_comparison(responses, sample_plz, root)
    summary.update(extra or {})
    text = format_run_summary(summary)
    (run_folder / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run_folder / paths.RUN_SUMMARY_MD).write_text(f"# Run summary: {run_folder.name}\n\n```text\n{text}\n```\n", encoding="utf-8")
    say(text)
    return RunResult(run_folder=run_folder, summary=summary, frame=df, files=written)


def _compact_rule(rule):
    """Keep run_summary.json readable: long lists are cut to their first entries plus the total."""
    if not isinstance(rule, dict):
        return rule
    out = {}
    for k, v in rule.items():
        if isinstance(v, list) and len(v) > 25:
            out[k] = v[:25]
            out[f"{k}_total"] = len(v)
        else:
            out[k] = v
    return out


def _issue_count(key: str, rule: dict) -> int:
    field = {"rule_a": "uncovered", "rule_b": "fringe_sites", "rule_c": "misses", "rule_d": "findings"}[key.replace("_technik", "")]
    return rule.get(f"{field}_total", len(rule.get(field, [])))


def format_run_summary(s: dict) -> str:
    lines = [f"KÜS run summary ({s['mode']}): {s['outcome']}", "=" * 50,
             f"Run date:           {s['run_date']}  (started {s['started_at']}, {s['runtime_s']} s)",
             f"Requests made:      {s['requests_made']}"]
    plan = s.get("plan")
    if plan:
        lines.append(
            f"Queries:            {plan['queries']} PLZ ({plan['planned']} planned + {plan['fill_a']} fill-in A + {plan['fill_b']} fill-in B);"
            f" {plan['not_queried']} planned not made; lists: {', '.join(plan['lists'])}"
        )
    if s["status_reasons"]:
        lines += ["FAIL because:" if s["status"] == "FAIL" else "Notes:"] + [f"  ! {r}" for r in s["status_reasons"]]
    cov = s.get("coverage") or {}
    if cov:
        lines += ["", f"Coverage rules ({s.get('centres')} server centres):"]
        for key, label in (("rule_a", "A every PLZ within COVER_KM of a centre"), ("rule_b", "B every site within 45 km of a centre"),
                           ("rule_c", "C every centre within 48 km returned the site"), ("rule_d", "D no count cap"),
                           ("rule_c_technik", "C, technik list"), ("rule_d_technik", "D, technik list")):
            if key in cov:
                lines.append(f"  {'ok  ' if cov[key]['ok'] else 'FAIL'} {label}" + ("" if cov[key]["ok"] else f"  ({_issue_count(key, cov[key])} issues)"))
        if cov.get("rule_a", {}).get("assumed_empty"):
            lines.append(f"  note: {len(cov['rule_a']['assumed_empty'])} uncovered PLZ were queried and answered empty (assumed empty region)")
    lines += ["",
              f"Collected:          {s['rows_total']} unique records",
              f"In scope:           {s['in_scope']} (pruefungvorortmoeglich = 1 and = 0; the vorort = 0 records carry psp = Yes)",
              f"In scope by psp:    {s['in_scope_psp']}",
              f"Site types:         {s['site_types']}",
              f"Lists:              {s['kues_list']}",
              f"Needs review:       {s['needs_review']}  {s['review_reasons']}",
              f"PLZ-only location:  {s['plz_only']} (no street / coordinates)",
              f"Flags:              {s['flags']}"]
    if s.get("is_hdp_site") is not None:
        lines.append(f"HDP sites:          {s['is_hdp_site']}")
    sc = s.get("sample_comparison")
    if sc:
        lines.append(f"Saved sample {sc['saved_file']}: {sc['saved_records']} saved vs {sc['live_records']} live records, same ids {sc['same_ids']}, same order {sc['same_order']}, changed content {len(sc['records_with_changed_content'])}")
    for key, items in (s.get("warnings") or {}).items():
        lines.append(f"Warning {key}: {len(items)}  {items[:5]}")
    lines.append(f"Drop check:         {s['drop_check']}")
    if s.get("baseline"):
        lines.append(f"Baseline:           {s['baseline']['run_folder']} ({s['baseline']['in_scope']} in scope)")
    if not s.get("save_raw") and s["mode"] in ("full", "sample"):
        lines.append("Raw responses:      not saved (save_raw=False); this run cannot be rebuilt offline")
    lines += ["", "Files:"] + [f"  {k:<18}{v}" for k, v in s["files"].items()]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Raw files
# --------------------------------------------------------------------------

def read_raw(raw_jsonl: str | Path) -> list[fetch.Response]:
    """Responses of an earlier run, in request order, from its raw.jsonl."""
    out = []
    with open(raw_jsonl, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            item = json.loads(line)
            list_name, _, rest = item["file"].partition(RAW_SEP)
            data = fetch.check_shape(item["response"])
            out.append(fetch.to_response(list_name, rest.removesuffix(".json"), data, None, item["file"]))
    return out


def _find_run(source_run: str | Path, root: Path) -> Path:
    p = Path(source_run)
    if p.is_dir():
        return p
    for mode in ("full", "sample", "simulated"):
        candidate = paths.output_dir(SLUG, mode, root) / str(source_run)
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"run folder not found: {source_run}")


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def run(
    mode: str = "simulated",
    *,
    confirm_full_run: bool = False,
    root: str | Path | None = None,
    started_at: datetime | None = None,
    api=None,
    sites: pd.DataFrame | None = None,
    centroids: pd.DataFrame | None = None,
    cfg: rc.CoverConfig | None = None,
    sample_plz: str = SAMPLE_PLZ,
    fetch_technik: bool = True,
    max_requests: int | None = None,
    exports=EXPORT_KEYS,
    save_raw: bool | None = None,
    source_run: str | Path | None = None,
    source_file: str | Path | None = None,
    verbose: bool = True,
) -> RunResult:
    """One run = one folder. See the module docstring for the modes.

    cfg: the coverage numbers (default: 100 km radius, COVER_KM 50, see default_cfg). A radius other than 50 sends the `km` parameter.
    max_requests: hard limit of requests for this run (default: 5 for sample, fetch.FULL_MAX_REQUESTS for full; none for simulated).
    fetch_technik: also query the technik list (a subset of the main list; recon 2026-10-05). Doubles the requests.
    save_raw: keep every response in raw.jsonl (default: yes for sample and full, no for simulated).
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "full" and not confirm_full_run:
        raise RuntimeError(FULL_RUN_NOT_CONFIRMED)
    if mode == "rebuild" and source_run is None:
        raise ValueError("mode='rebuild' needs source_run (a run folder or its name)")
    if mode == "recon" and source_file is None:
        raise ValueError("mode='recon' needs source_file (a saved partnersuche response)")
    exports = tuple(exports)
    if set(exports) - set(EXPORT_KEYS):
        raise ValueError(f"unknown export key(s) {sorted(set(exports) - set(EXPORT_KEYS))}; choose from {EXPORT_KEYS}")
    root = Path(root) if root is not None else paths.project_root()
    cfg = cfg or default_cfg()
    started = started_at or datetime.now()
    t0 = time.time()
    centroids = centroids if centroids is not None else load_plz_centroids(ensure_geonames(paths.geonames_dir(root)))
    lists = (fetch.LIST_KUES, fetch.LIST_TECHNIK) if fetch_technik else (fetch.LIST_KUES,)

    def say(msg):
        if verbose:
            print(msg, flush=True)

    # ---- offline modes
    if mode == "recon":
        data = fetch.check_shape(json.loads(Path(source_file).read_text(encoding="utf-8")))
        plz = _plz_from_name(Path(source_file).name) or sample_plz
        folder = paths.new_run_dir(SLUG, mode, started, root)
        resp = fetch.to_response(fetch.LIST_KUES, plz, data, None, Path(source_file).name)
        return process(mode=mode, run_folder=folder, root=root, started=started, t0=t0, responses=[resp], log_rows=[], centroids=centroids,
                       cfg=cfg, exports=exports, extra={"source_file": core_export.relative(Path(source_file), root)}, say=say)
    if mode == "rebuild":
        src = _find_run(source_run, root)
        responses = read_raw(src / paths.RAW_FILE)
        folder = paths.new_run_dir(SLUG, mode, started, root)
        centroid = centroids.set_index("zip")[["lat", "lng"]]
        city = dict(zip(centroids["zip"], centroids["city"]))
        log = [{"seq": i, "list": r.list_name, "zip": r.zip, "city": city.get(r.zip), "reason": "rebuild", "time": None,
                **response_log_fields(r, centroid)} for i, r in enumerate(responses, 1)]
        plan = {"planned": 0, "fill_a": 0, "fill_b": 0, "queries": len({r.zip for r in responses}), "not_queried": 0,
                "lists": sorted({r.list_name for r in responses})}
        return process(mode=mode, run_folder=folder, root=root, started=started, t0=t0, responses=responses, log_rows=log, centroids=centroids,
                       cfg=cfg, plan=plan, exports=exports, extra={"source_run": core_export.relative(src, root)}, say=say)

    # ---- live and simulated
    if mode == "simulated":
        api = api or simulate.FakeKuesApi(
            sites if sites is not None else simulate.simulated_sites(centroids), centroids, radius_km=cfg.radius_km, max_requests=max_requests
        )
    elif api is None:
        default_limit = SAMPLE_MAX_REQUESTS if mode == "sample" else fetch.FULL_MAX_REQUESTS
        api = fetch.KuesClient(max_requests=default_limit if max_requests is None else max_requests)
    keep_raw = (mode != "simulated") if save_raw is None else bool(save_raw)
    folder = paths.new_run_dir(SLUG, mode, started, root)
    radius_param = None if cfg.radius_km == fetch.DEFAULT_RADIUS_KM else int(cfg.radius_km)
    raw_file = open(folder / paths.RAW_FILE, "x", encoding="utf-8") if keep_raw else None
    col = Collector(api, centroids, cfg, lists, raw_file, say, radius_param)
    aborted = None
    planned: list[str] = [sample_plz] if mode == "sample" else rc.plan_initial(centroids, cfg)["zip"].tolist()
    say(f"{mode}: {len(planned)} planned queries x {len(lists)} list(s)")
    try:
        for n, z in enumerate(planned, 1):
            col.query(z, "sample" if mode == "sample" else "plan")
            if n % 25 == 0:
                say(f"  {n}/{len(planned)}")
        if mode != "sample":
            col.fill_in()
    except StopRun as exc:
        aborted = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # unattended: still write the summary and the labelled partial exports, raw is already on disk
        aborted = f"unexpected {type(exc).__name__}: {exc}"
    finally:
        if raw_file:
            raw_file.close()
    answered = [r for r in col.log if r["list"] == fetch.LIST_KUES and r["status"] in ("ok", "empty")]
    done = {r["zip"] for r in answered if r["reason"] in ("plan", "sample")}
    plan_info = {
        "planned": len(planned),
        "fill_a": sum(r["reason"] == "fill_a" for r in answered),
        "fill_b": sum(r["reason"] == "fill_b" for r in answered),
        "queries": len(answered),
        "not_queried": len(set(planned) - done),
        "lists": list(lists),
    }
    return process(mode=mode, run_folder=folder, root=root, started=started, t0=t0, responses=col.responses, log_rows=col.log,
                   centroids=centroids, cfg=cfg, aborted=aborted, plan=plan_info, exports=exports, keep_raw=keep_raw,
                   sample_plz=sample_plz if mode == "sample" else None, say=say)


def _plz_from_name(name: str) -> str | None:
    m = re.search(r"(\d{5})", name)
    return m.group(1) if m else None


def estimate(centroids: pd.DataFrame, cfg: rc.CoverConfig | None = None, fetch_technik: bool = True, seed: int = 0) -> dict:
    """Before a full run: the planned queries, the expected total including fill-in (measured on a simulated crawl
    with server-centre offsets like the real ones) and the runtime at the live pace. Offline, a few seconds."""
    cfg = cfg or default_cfg()
    planned = rc.plan_initial(centroids, cfg)
    fill = _simulate_counts(centroids, cfg, seed)
    queries = len(planned) + fill
    lists = 2 if fetch_technik else 1
    per_request_s = fetch.PACE_S + fetch.JITTER_S / 2 + 0.8
    return {
        "planned_queries": int(len(planned)),
        "expected_fill_in": int(fill),
        "expected_queries": int(queries),
        "lists": lists,
        "expected_requests": int(queries * lists),
        "request_limit": fetch.FULL_MAX_REQUESTS,
        "expected_runtime_min": round(queries * lists * per_request_s / 60, 1),
        "planned_points": planned,
    }


def _simulate_counts(centroids: pd.DataFrame, cfg: rc.CoverConfig, seed: int) -> int:
    """Fill-in queries of one simulated crawl (no folder, no files)."""
    api = simulate.FakeKuesApi(simulate.simulated_sites(centroids, seed=seed), centroids, radius_km=cfg.radius_km, seed=seed)
    col = Collector(api, centroids, cfg, (fetch.LIST_KUES,), None, lambda m: None)
    for z in rc.plan_initial(centroids, cfg)["zip"]:
        col.query(z, "plan")
    return col.fill_in()
