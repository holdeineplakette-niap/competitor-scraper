"""Orchestration of the master aggregator: select inputs, load, dedupe, resolve, geocode, validate, write.

    .venv\\Scripts\\python.exe -m core.master_run dev
    .venv\\Scripts\\python.exe -m core.master_run official --confirm

`dev` writes 3_master_output/_dev/YYYY-MM-DD_HHMM/; `official` writes 3_master_output/YYYY-MM/ and refuses to run
if that folder exists. Logic lives in the other master_* modules; this file only orders the steps.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from core import dedupe, diff, geocode, geo, hdp_removal, paths
from core import hubspot_export, master_ids, master_inputs as mi, master_lifecycle, master_records as mr, master_resolve as rs, master_schema as ms
from core import normalise as nm

MAX_CONTRIBUTION_DROP = 0.10
LOG_COLUMNS = ["master_id", "location_ids", "action", "rule", "field", "chosen_source", "chosen_value", "dropped", "note"]


class MasterRunError(Exception):
    pass


@dataclass
class RunResult:
    status: str
    out_dir: Path | None
    master: pd.DataFrame | None
    summary: dict
    diff: pd.DataFrame | None = None
    groups: list = field(default_factory=list)
    recs: list = field(default_factory=list)


# ---- small helpers ------------------------------------------------------------------------------------------------
def _clean_cells(df: pd.DataFrame) -> pd.DataFrame:
    """No line breaks inside any field (they would make the CSV row count differ from the record count)."""
    out = df.copy()
    for col in out.columns:  # pandas 3 uses a `str` dtype, older versions `object`: test both
        if pd.api.types.is_string_dtype(out[col]) or pd.api.types.is_object_dtype(out[col]):
            out[col] = out[col].fillna("").astype(str).str.replace(r"[\r\n]+", " ", regex=True)
    return out


def write_csv(df: pd.DataFrame, path: Path) -> None:
    _clean_cells(df).to_csv(path, sep=";", index=False, encoding="utf-8-sig", lineterminator="\n")


def read_master(path: Path) -> pd.DataFrame:
    return mi.read_source_csv(path)


def load_hdp_file(root=None) -> tuple[set[str] | None, str]:
    """location_ids listed in 4_reference/hdp/hdp_sites.csv (columns location_id and/or adressid), or None."""
    p = paths.hdp_sites_path(root)
    if not p.exists():
        return None, "4_reference/hdp/hdp_sites.csv does not exist: is_hdp_site is left empty"
    ids: set[str] = set()
    with open(p, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if (r.get("location_id") or "").strip():
                ids.add(r["location_id"].strip())
            if (r.get("adressid") or "").strip():
                ids.add(f"KUES-{r['adressid'].strip()}")
    return ids, f"{len(ids)} HDP site id(s) read from hdp_sites.csv"


def _check(name: str, ok: bool, detail: str, severity: str = "hard") -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": severity}


def _counts(series) -> dict:
    return {str(k): int(v) for k, v in Counter(series).most_common()}


# ---- the run ------------------------------------------------------------------------------------------------------
def run(mode: str, root=None, started_at: datetime | None = None, confirm: bool = False, geocoder: geocode.Geocoder | None = None,
        live_geocoding: bool = True, pins: dict | None = None, include_out_of_scope: bool | None = None,
        merge_across_uo: bool | None = None, centroids: pd.DataFrame | None = None, check_volume: bool = True,
        hdp_removal_enabled: bool = True, accept_retirements: bool = False) -> RunResult:
    """`hdp_removal_enabled=False` exists for the tests of the other aggregator steps only; the command line has no such switch.
    `accept_retirements=True` (CLI: --accept-retirements) turns the mass-retirement check into a review item: use it only when
    a source really lost that many sites."""
    if mode not in ("dev", "official"):
        raise ValueError("mode must be 'dev' or 'official'")
    started_at = started_at or datetime.now()
    include = ms.INCLUDE_OUT_OF_SCOPE if include_out_of_scope is None else include_out_of_scope
    across = ms.MERGE_ACROSS_UO if merge_across_uo is None else merge_across_uo
    month = f"{started_at:%Y-%m}"
    run_date = f"{started_at:%Y-%m-%d}"
    if mode == "official":
        if not confirm:
            raise MasterRunError("official run needs confirm=True (CLI: --confirm)")
        if paths.master_output_dir(month, root).exists():
            raise MasterRunError(f"{paths.master_output_dir(month, root)} already exists; an official month is never overwritten "
                                 "(move a failed one to 9_archive/ first)")

    later = [f.parent.name for f in paths.official_masters(root) if f.parent.name > month]
    if mode == "official" and later:
        raise MasterRunError(f"official masters of later months exist ({', '.join(later)}); an older month is never rebuilt "
                             "(master_ids would be reissued)")
    if centroids is None:
        if not live_geocoding and not (paths.geonames_dir(root) / "DE.txt").exists():
            raise MasterRunError("offline run, but 4_reference/geonames/DE.txt is missing (it would be downloaded)")
        centroids = geo.load_plz_centroids(geo.ensure_geonames(paths.geonames_dir(root)))
    cent = geo.centroid_lookup(centroids)
    known = set(cent)
    states = {str(z): s for z, s in zip(centroids["zip"], centroids["state"]) if s}

    summary: dict = {"mode": mode, "started_at": started_at.isoformat(timespec="seconds"), "month": month, "checks": [], "warnings": []}
    try:
        selected = {slug: mi.select_run(slug, root, pins) for slug in mi.SOURCE_ORDER}
        recs, infos, extras = mr.load_all(selected, include, known)
    except (LookupError, mr.SchemaDrift, ValueError) as exc:
        return _fail_early(mode, month, started_at, root, summary, f"{type(exc).__name__}: {exc}")

    # newer runs that were rejected, and how old the chosen ones are: never a silent fallback to older data
    input_notes = {}
    for slug, c in selected.items():
        newer = [f"{x.run_dir.name} ({x.why})" for x in mi.candidates(slug, root) if x.started > (c.root_started or c.started) and not x.qualifies]
        input_notes[slug] = {"newer_not_qualifying": newer, "age_days": (started_at - (c.root_started or c.started)).days,
                             "data_started": (c.root_started or c.started).isoformat(timespec="seconds"),
                             "pinned": bool((pins if pins is not None else mi.PINS).get(slug))}

    prev_path = paths.latest_official_master(root, before_month=month if mode == "official" else None)
    try:
        prev = read_master(prev_path) if prev_path else None
        if prev is not None and prev.empty:
            raise ValueError(f"{prev_path.name} has no rows: it cannot be the baseline of the volume checks")
        if prev is not None and not {"master_id", "source_ids", "first_seen", "listed_by"} <= set(prev.columns):
            raise ValueError(f"{prev_path.name} lacks master_id / source_ids / first_seen / listed_by")
        overrides = dedupe.load_overrides(paths.overrides_path(root))
    except (ValueError, OSError) as exc:
        return _fail_early(mode, month, started_at, root, summary, f"{type(exc).__name__}: {exc}")
    # the highest id ever issued: read from EVERY official master (a vanished id stays in the month it last appeared in),
    # so a missing or broken summary file can never make an old id come back
    try:
        max_issued = master_ids.max_id(i for f in paths.official_masters(root, before_month=month if mode == "official" else None)
                                       for i in read_master(f)["master_id"])
    except (ValueError, OSError, KeyError) as exc:
        return _fail_early(mode, month, started_at, root, summary, f"an official master could not be read for the id registry: {exc}")
    hdp_file_ids, hdp_file_note = load_hdp_file(root)

    # drop out-of-scope rows only when the switch is off (counted, never silent)
    excluded = Counter(r["slug"] for r in recs if not r["in_scope"] and not include)
    recs = [r for r in recs if r["in_scope"] or include]

    # HDP's own sites leave the competitor data before any merging. The HDP run is mandatory: no run, a failed, empty or stale
    # one stops the aggregator (it never falls back to removing nothing).
    hdp_in = hdp_match = None
    hdp_removed: list[dict] = []
    if hdp_removal_enabled:
        try:
            hdp_in = hdp_removal.load_hdp_input(root, started_at)
        except hdp_removal.HdpInputError as exc:
            return _fail_early(mode, month, started_at, root, summary, f"HDP own-site input: {exc}")
        hdp_match = hdp_removal.match_sites(hdp_in.sites, [r for r in recs if r["slug"] == hdp_removal.KUES_SLUG])
        recs, hdp_removed = hdp_removal.split_recs(recs, hdp_match)

    gc = geocoder
    if gc is None:
        gc = geocode.Geocoder(paths.geocode_cache_path(root), contact=geocode.contact_from_env(root), live=live_geocoding)
    extra_same: list[tuple[str, str, str]] = []
    try:
        try:
            for _pass in range(2):  # a second pass only if the coordinate stage found two sources' rows to be the same site
                dd, groups, unresolved, rejected, auto_same = _build_groups(recs, overrides, across, extra_same, cent, states, known, gc)
                if not auto_same:
                    break
                extra_same = [(groups[p["a"]].recs[0]["location_id"], groups[p["b"]].recs[0]["location_id"],
                               f"same street and number in the same city, {p['km'] * 1000:.0f} m apart, PLZ differs") for p in auto_same]
            unresolved = unresolved + auto_same  # still open after the second pass: a human decides
        finally:
            cache_written = gc.save()  # live answers are kept even if a later step fails
    except (dedupe.MergeConflict, ValueError) as exc:
        return _fail_early(mode, month, started_at, root, summary, f"{type(exc).__name__}: {exc}")
    geocoded = sum(1 for g in groups if g.coord_ok_from == "nominatim")

    cand_reasons: dict[int, list[str]] = defaultdict(list)
    candidate_log = []
    # pairs the rules refuse to decide (a sign shared by two places, several unproven rows at one address, no house number)
    loc_group = {r["location_id"]: g.gid for g in groups for r in g.recs}
    for loc, why in sorted(dd.info["forced_review"].items()):
        gid = loc_group[loc]
        cand_reasons[gid].append("match_candidate_unresolved")
        candidate_log.append({"gid": gid, "location_ids": loc, "action": "candidate", "rule": "candidate_unresolved", "field": "",
                              "chosen_source": "", "chosen_value": "", "dropped": "", "note": why})
    # KÜS rows that stay in the master but may be HDP's own (several candidates for one HDP site, or claimed twice): flagged, not failed
    for lid, hdp_ids in (hdp_removal.review_targets(hdp_match).items() if hdp_match is not None else ()):
        gid = loc_group[lid]
        cand_reasons[gid].append("hdp_match_unresolved")
        groups[gid].agg_flags.append(f"hdp_match_unresolved:{'|'.join(hdp_ids)}")
        candidate_log.append({"gid": gid, "location_ids": lid, "action": "candidate", "rule": "hdp_match_unresolved", "field": "",
                              "chosen_source": "", "chosen_value": "", "dropped": "",
                              "note": f"may be the own site of HDP {', '.join(hdp_ids)}; not removed, a human decides"})
    # KÜS rows that are HDP's own site but stay in the master by decision (second row at an HDP address): flagged, kept out of HubSpot
    for lid, hdp_id in (hdp_removal.kept_rows([r for r in recs if r["slug"] == hdp_removal.KUES_SLUG]).items() if hdp_match is not None else ()):
        gid = loc_group[lid]
        cand_reasons[gid].append(hdp_removal.KEPT_FLAG)
        groups[gid].agg_flags.append(f"{hdp_removal.KEPT_FLAG}:{hdp_id}")
        candidate_log.append({"gid": gid, "location_ids": lid, "action": "candidate", "rule": hdp_removal.KEPT_FLAG, "field": "",
                              "chosen_source": "", "chosen_value": "", "dropped": "",
                              "note": f"HDP's own site ({hdp_id}), kept in the master by decision; never imported to HubSpot"})
    # the same coordinates on rows with different addresses: one of them is wrong (Bad Camberg / Oberursel pattern)
    for grp in geo.shared_coordinate_groups(
            [g for g in groups if g.row["lat"]], lambda g: (float(g.row["lat"]), float(g.row["lng"])),
            lambda g: nm.address_key(g.row["street"], g.row["zip"])):
        for g in grp:
            other = next(o for o in grp if o is not g)
            g.agg_flags.append(f"coords_shared_with_other_address:{other.recs[0]['location_id']}")
    for p in unresolved:
        for me, other in ((p["a"], p["b"]), (p["b"], p["a"])):
            cand_reasons[me].append("match_candidate_unresolved")
            groups[me].agg_flags.append(f"possible_duplicate_of:{groups[other].recs[0]['location_id']}")
        candidate_log.append({"gid": p["a"], "location_ids": f"{groups[p['a']].recs[0]['location_id']}|{groups[p['b']].recs[0]['location_id']}",
                              "action": "candidate", "rule": "candidate_unresolved", "field": "", "chosen_source": "",
                              "chosen_value": "", "dropped": "", "note": f"{p['km'] * 1000:.0f} m apart, could be the same house"})
    for p in rejected:
        candidate_log.append({"gid": p["a"], "location_ids": f"{groups[p['a']].recs[0]['location_id']}|{groups[p['b']].recs[0]['location_id']}",
                              "action": "candidate", "rule": "candidate_rejected", "field": "", "chosen_source": "",
                              "chosen_value": "", "dropped": "", "note": f"{p['km'] * 1000:.0f} m apart, different street or house number"})

    # ---- finish rows, ids, first/last seen
    for g in groups:
        is_hdp = bool(hdp_file_ids) and any(r["location_id"] in hdp_file_ids for r in g.recs)
        rs.finish(g, cand_reasons.get(g.gid, []), known, is_hdp)

    prev_ids = {r["master_id"]: [x for x in r["source_ids"].split("|") if x] for r in prev.to_dict("records")} if prev is not None else {}
    ids, id_events = master_ids.assign([[r["location_id"] for r in g.recs] for g in groups], prev_ids, max_issued)
    summary["max_master_id_issued"] = master_ids.fmt(max(master_ids.max_id(ids), max_issued))
    first_seen = dict(zip(prev["master_id"], prev["first_seen"])) if prev is not None else {}
    for g, mid in zip(groups, ids):
        g.row["master_id"] = mid
        g.row["first_seen"] = first_seen.get(mid) or run_date
        g.row["last_seen"] = run_date
    master = pd.DataFrame([g.row for g in groups], columns=ms.MASTER_COLUMNS).sort_values("master_id", kind="stable").reset_index(drop=True)
    mid_of = {g.gid: mid for g, mid in zip(groups, ids)}
    # lifecycle: rows of the previous master that no group claims are retired (or kept, if their input is stale); `master` stays
    # the rows of THIS run (the conservation checks count them), `master_all` is what is written
    fresh = master_lifecycle.fresh_sources(input_notes, MAX_INPUT_AGE_DAYS)
    life = master_lifecycle.apply(master, prev, run_date, fresh)
    master_all = life.master

    # ---- logs
    log_rows: list[dict] = []
    for e in dd.edges:
        gid = dd.comp_of[e.a]
        log_rows.append({"master_id": mid_of[gid], "location_ids": f"{recs[e.a]['location_id']}|{recs[e.b]['location_id']}", "action": "merge",
                         "rule": e.rule, "field": "", "chosen_source": "", "chosen_value": "", "dropped": "", "note": e.note})
    for g in groups:
        for l in g.logs:
            log_rows.append({"master_id": mid_of[l["gid"]], **{k: v for k, v in l.items() if k != "gid"}})
    for l in candidate_log:
        log_rows.append({"master_id": mid_of[l["gid"]], **{k: v for k, v in l.items() if k != "gid"}})
    for ev in id_events:
        log_rows.append({"master_id": ev["master_id"], "location_ids": "", "action": "id",
                         "rule": "id_retired" if ev["event"] == "retired" else "id_split_new", "field": "", "chosen_source": "",
                         "chosen_value": ev.get("merged_into", ev.get("from", "")), "dropped": "", "note": ev["event"]})
    log_rows += life.events
    merge_log = pd.DataFrame(log_rows, columns=LOG_COLUMNS).sort_values(["master_id", "action", "rule", "field", "location_ids"], kind="stable").reset_index(drop=True)

    # ---- extra files (columns without a master home), keyed by master_id + location_id
    row_master = {r["location_id"]: mid_of[dd.comp_of[i]] for i, r in enumerate(recs)}
    extra_files: dict[str, pd.DataFrame] = {}
    for slug, df in extras.items():
        e = df[df["location_id"].isin(row_master)].copy()
        e.insert(0, "master_id", e["location_id"].map(row_master))
        extra_files[slug] = e.sort_values(["master_id", "location_id"], kind="stable")

    change = diff.diff_masters(prev, master_all)

    # ---- validation
    checks = _validate(recs, groups, dd, master, infos, selected, excluded, prev, gc, include, mode=mode, input_notes=input_notes,
                       overrides=overrides, row_master=row_master, known=known, check_volume=check_volume,
                       hdp_removed=len(hdp_removed), accept_retirements=accept_retirements)
    checks += _lifecycle_checks(master_all, prev, life)
    hdp_frame = None
    if hdp_in is not None:  # the strict HDP check: R (rows actually removed) must equal N (sites the HDP scraper returned)
        hdp_checks = hdp_removal.count_checks(hdp_in, hdp_match, len(hdp_removed))
        hdp_checks.append(hdp_removal.final_check(hdp_removed, [x for s in master["source_ids"] for x in s.split("|") if x]))
        checks += hdp_checks
        hdp_frame = (hdp_removal.removed_frame(hdp_match, hdp_removed), hdp_removal.review_frame(hdp_match, row_master))
    summary.update(_summary(mode, started_at, month, selected, infos, recs, groups, dd, master, excluded, gc, geocoded, cache_written,
                            overrides, hdp_file_note, hdp_file_ids, extras, change, prev_path, unresolved, rejected, id_events, checks, include, across,
                            master_all=master_all))
    summary["lifecycle"] = {**life.info, "fresh_sources": fresh}
    summary["master_rows_total"] = len(master_all)
    summary["input_notes"] = input_notes
    status = "PASS" if all(c["ok"] for c in checks if c["severity"] == "hard") else "FAIL"
    summary["status"] = status
    summary["hdp_own_sites"] = (hdp_removal.section(hdp_in, hdp_match, hdp_removed, recs, all(c["ok"] for c in hdp_checks))
                                if hdp_in is not None else {"enabled": False, "note": "disabled (tests only)"})

    out_dir = _write(mode, month, started_at, root, status, master_all, change, merge_log, extra_files, selected, infos, summary, hdp_frame,
                     prev=prev)
    return RunResult(status, out_dir, master_all, summary, change, groups, recs)


def _build_groups(recs, overrides, across, extra_same, cent, states, known, gc):
    """Dedupe, resolve every group, geocode what still has no usable coordinates, then look for coordinate candidates."""
    dd = dedupe.build(recs, overrides, across, extra_same, cent)
    groups: list[rs.Group] = []
    for gid, members in enumerate(dd.comps):
        agg: list[str] = []
        for i in members:
            agg += dd.flags.get(i, [])
        groups.append(rs.resolve_group(gid, [recs[i] for i in members], list(dict.fromkeys(agg)), cent, states, known))
    for g in groups:
        if not g.need_geo:
            continue
        row = g.row

        def accept(hit, row=row):
            return rs.check_coords(hit["lat"], hit["lng"], row["zip"], cent, known)[0]

        hit, note = gc.lookup(row["street"], row["zip"], row["city"], accept)
        if hit:
            _, dist, _ = rs.check_coords(hit["lat"], hit["lng"], row["zip"], cent, known)
            row["lat"], row["lng"], row["coord_source"], row["plz_distance_km"] = f"{hit['lat']:.6f}", f"{hit['lng']:.6f}", "nominatim", f"{dist:.2f}"
            g.coord_ok_from = "nominatim"
            rs._log(g, "lat/lng", "nominatim", f"{row['lat']},{row['lng']}", [], "no source coordinates passed the PLZ check", rule="geocode")
        else:
            # a source coordinate that missed the 15 km PLZ rule (a large rural PLZ) but that Nominatim independently
            # puts within 500 m: two sources agree, so the coordinate is kept and the doubt is flagged
            confirmed = next(((r, h) for h in gc.last_rejected for r in g.recs if r["lat"] is not None and geo.in_germany(r["lat"], r["lng"])
                              and float(geo.haversine_km(r["lat"], r["lng"], h["lat"], h["lng"])) <= 0.5), None)
            if confirmed is not None and row["zip"] in cent:
                r, h = confirmed
                km = geo.plz_distance_km(r["lat"], r["lng"], row["zip"], cent)
                row["lat"], row["lng"], row["plz_distance_km"] = f"{r['lat']:.6f}", f"{r['lng']:.6f}", f"{km:.2f}"
                row["coord_source"] = "nominatim" if r["coord_hint"] == "nominatim" else r["slug"]
                g.coord_ok_from = "geocoder-confirmed"
                g.agg_flags.append(f"coords_far_from_plz_confirmed_by_geocoder:{km:.1f}km")
                rs._log(g, "lat/lng", row["coord_source"], f"{row['lat']},{row['lng']}", [], f"{km:.1f} km from the PLZ centroid, confirmed by Nominatim within 500 m",
                        rule="coords_confirmed_by_geocoder")
            else:
                rs._log(g, "lat/lng", "", "", [], note, rule="geocode_failed")
    cgroups = []
    for g in groups:
        cgroups.append({"gid": g.gid, "family": g.family, "slugs": g.slugs, "lat": float(g.row["lat"]) if g.row["lat"] else None,
                        "lng": float(g.row["lng"]) if g.row["lng"] else None, "street": g.row["street"], "zip": g.row["zip"],
                        "city": g.row["city"], "akeys": g.akeys, "uo": g.row["uo"]})
    unresolved, rejected, auto_same = dedupe.proximity_candidates(cgroups, known)
    return dd, groups, unresolved, rejected, auto_same


# ---- validation ---------------------------------------------------------------------------------------------------
MAX_INPUT_AGE_DAYS = 45  # an input run older than this is a stale source
TIGHT_TOLERANCE = 0.05  # sources whose scraper writes no run_summary (no own count to compare with)
VOLUME_TOLERANCE = 0.15  # first master only: rows per source vs the count in scraper_status.md


def _input_checks(mode, selected, infos, include, prev, check_volume, input_notes, excluded=None) -> list[dict]:
    """Inputs must be fresh, complete and not silently replaced by an older run. Hard in an official run, a review item in dev."""
    sev = "hard" if mode == "official" else "review"
    out = []
    newer = {s: n["newer_not_qualifying"] for s, n in input_notes.items() if n["newer_not_qualifying"] and not n["pinned"]}
    out.append(_check("no newer run of a source was rejected (the master never silently falls back to an older run)", not newer,
                      "; ".join(f"{s}: {', '.join(v)}" for s, v in newer.items()) or "ok (a pin accepts a choice on purpose)", sev))
    stale = {s: n["age_days"] for s, n in input_notes.items() if n["age_days"] > MAX_INPUT_AGE_DAYS and not n["pinned"]}
    out.append(_check(f"every input run is at most {MAX_INPUT_AGE_DAYS} days old", not stale,
                      "; ".join(f"{s}: {d} days" for s, d in stale.items()) or "ok", sev))
    contaminated = {s: n for s, n in (excluded or {}).items() if n and not include}
    out.append(_check("the in-scope exports contain no out-of-scope rows (they would be dropped; volume gates count file rows)",
                      not contaminated, "; ".join(f"{s}: {n} row(s) with in_scope false" for s, n in contaminated.items()) or "ok"))
    mismatch, off = [], []
    for slug, c in selected.items():
        n = infos[slug].rows
        own = mi.summary_in_scope(c.summary) if not include else None
        if own is not None and own != n:
            mismatch.append(f"{slug}: file {n} vs its run_summary {own}")
        if prev is None and check_volume:
            anchor = mi.SOURCES[slug].status_count
            tol = TIGHT_TOLERANCE if mi.SOURCES[slug].proof == "filename" else VOLUME_TOLERANCE
            if not include and abs(n - anchor) > tol * anchor:
                off.append(f"{slug}: {n} rows vs {anchor} in scraper_status.md")
    out.append(_check("rows read equal the in-scope count in the source's own run_summary", not mismatch, "; ".join(mismatch) or "ok"))
    out.append(_check(f"first master: every source within {VOLUME_TOLERANCE:.0%} of the count in scraper_status.md" if prev is None
                      else "volume vs status file (skipped: the previous official master is the baseline)", not off, "; ".join(off) or "ok",
                      "hard" if prev is None and check_volume else "info"))
    return out


def _validate(recs, groups, dd, master, infos, selected, excluded, prev, gc, include, mode="dev", input_notes=None,
              overrides=(), row_master=None, known=None, check_volume=True, hdp_removed=0, accept_retirements=False) -> list[dict]:
    sev_override = "hard" if mode == "official" else "review"
    checks = [_check("every source has a qualifying input run", len(selected) == len(mi.SOURCE_ORDER),
                     f"{len(selected)} of {len(mi.SOURCE_ORDER)}")]
    checks += _input_checks(mode, selected, infos, include, prev, check_volume, excluded=excluded, input_notes=input_notes or {s: {"newer_not_qualifying": [], "age_days": 0, "pinned": False} for s in selected})
    rows_in = sum(i.rows for i in infos.values())
    merged_away = sum(len(c) - 1 for c in dd.comps)
    n_excl = sum(excluded.values())
    checks.append(_check("row conservation: input rows = master rows + merged-away rows (+ out-of-scope excluded, + HDP own sites removed)",
                         rows_in == len(master) + merged_away + n_excl + hdp_removed,
                         f"{rows_in} in = {len(master)} master + {merged_away} merged away + {n_excl} excluded + {hdp_removed} HDP own sites removed"))
    listed = [x for s in master["source_ids"] for x in s.split("|") if x]
    all_ids = [r["location_id"] for r in recs]
    checks.append(_check("every input location_id is in exactly one master row", sorted(listed) == sorted(all_ids),
                         f"{len(listed)} listed vs {len(all_ids)} read; missing {sorted(set(all_ids) - set(listed))[:5]}, "
                         f"duplicated {sorted(x for x, c in Counter(listed).items() if c > 1)[:5]}"))
    checks.append(_check("no duplicate master_id", master["master_id"].is_unique, f"{int(master['master_id'].duplicated().sum())} duplicates"))
    bad_uo = master[~master["uo"].isin(ms.UO_VALUES)]
    checks.append(_check("every row has a valid uo", bad_uo.empty, f"{len(bad_uo)} rows without a valid uo"))
    bad_st = master[~master["site_type"].isin(("own_station", "partner", "unknown"))]
    checks.append(_check("every row has a valid site_type", bad_st.empty, f"{len(bad_st)} rows"))
    bad_psp = master[~master["psp"].isin(ms.PSP_VALUES)]
    checks.append(_check("every row has psp Yes or No", bad_psp.empty, f"{len(bad_psp)} rows"))
    clashes = [o for o in overrides if o["decision"] == "different" and o["location_id_a"] in (row_master or {}) and o["location_id_b"] in (row_master or {})
               and row_master[o["location_id_a"]] == row_master[o["location_id_b"]]]
    unused = dd.info["overrides_unused"]
    checks.append(_check("every manual decision still refers to rows in the inputs (else: stale, remove or archive it)", not unused,
                         "; ".join(unused) or "ok", sev_override))
    checks.append(_check("no 'different' decision ended up inside one merged row", not clashes,
                         "; ".join(f"{o['location_id_a']} / {o['location_id_b']}" for o in clashes) or "ok"))
    zip_bad = master[(master["zip"] != "") & pd.Series([not nm.is_valid_plz(z, known) for z in master["zip"]], index=master.index, dtype=bool)]
    unflagged = zip_bad[pd.Series([("plz_invalid" not in r.split("; ")) for r in zip_bad["doubt_flags"]], index=zip_bad.index, dtype=bool)]
    checks.append(_check("every zip is a 5-digit PLZ known to GeoNames, or flagged plz_invalid", unflagged.empty,
                         f"{len(zip_bad)} invalid, {len(unflagged)} of them not flagged"))
    if prev is not None:
        prev_active = master_lifecycle.with_lifecycle_columns(prev)
        prev_active = prev_active[prev_active["status"] == "active"]
        dropped = []
        for slug in mi.SOURCE_ORDER:
            now = sum(1 for s in master["listed_by"] if slug in s.split("|"))
            before = sum(1 for s in prev_active["listed_by"] if slug in s.split("|"))
            if before and now < before * (1 - MAX_CONTRIBUTION_DROP):
                dropped.append(f"{slug}: {before} -> {now}")
        checks.append(_check("no source loses more than 10 % of its active master rows in one run (mass retirement; --accept-retirements "
                             "makes it a review item)", not dropped, "; ".join(dropped) or "ok", "review" if accept_retirements else "hard"))
        grown = []
        for slug in mi.SOURCE_ORDER:
            now = sum(1 for s in master["listed_by"] if slug in s.split("|"))
            before = sum(1 for s in prev_active["listed_by"] if slug in s.split("|"))
            if before and now > before * (1 + MAX_CONTRIBUTION_DROP * 1.5):
                grown.append(f"{slug}: {before} -> {now}")
        checks.append(_check("no source contributes more than 15 % more master rows than in the previous official master (double export?)",
                             not grown, "; ".join(grown) or "ok", "review"))
    else:
        checks.append(_check("contribution drop check", True, "no previous official master (first master)", "info"))
    if gc.capped:
        checks.append(_check("geocoding stayed within the request cap", True, f"cap of {gc.cap} reached; remaining rows left without coordinates", "review"))
    if gc.blocked:
        checks.append(_check("Nominatim answered", False, "blocked or rate-limited: " + "; ".join(gc.errors[-3:]), "review"))
    return checks


def _lifecycle_checks(master_all: pd.DataFrame, prev, life) -> list[dict]:
    """Rules of master_lifecycle: nothing is ever dropped from the master, statuses are consistent, needs_review is only a status change."""
    out = []
    out.append(_check("no duplicate master_id (all rows, retired ones included)", master_all["master_id"].is_unique,
                      f"{int(master_all['master_id'].duplicated().sum())} duplicates"))
    if prev is not None:
        lost = sorted(set(prev["master_id"]) - set(master_all["master_id"]))
        out.append(_check("every master_id of the previous official master is still in the master (retired, never dropped)", not lost,
                          f"lost: {lost[:5]}" if lost else "ok"))
    bad_status = master_all[~master_all["status"].isin(ms.STATUS_VALUES)]
    out.append(_check("every row has status active or retired", bad_status.empty, f"{len(bad_status)} rows"))
    r = master_all["status"] == "retired"
    bad_ret = master_all[(r & (master_all["retired_on"] == "")) | (~r & (master_all["retired_on"] != ""))]
    out.append(_check("retired_on is set exactly on retired rows", bad_ret.empty, f"{len(bad_ret)} rows"))
    reasons_ok = all(set(x.split("; ")) <= set(ms.REVIEW_REASONS) for x in master_all["needs_review_reason"] if x)
    flag_ok = ((master_all["needs_review"] == "True") == (master_all["needs_review_reason"] != "")).all()
    out.append(_check("needs_review is True only with reason retired or reactivated (a status change of this run)", reasons_ok and bool(flag_ok),
                      ", ".join(ms.REVIEW_REASONS)))
    changed = int((master_all["needs_review"] == "True").sum())
    out.append(_check("needs_review rows = retired + reactivated rows of this run", changed == len(life.info["retired"]) + len(life.info["reactivated"]),
                      f"{changed} flagged = {len(life.info['retired'])} retired + {len(life.info['reactivated'])} reactivated"))
    if life.info.get("not_refreshed"):
        out.append(_check("rows kept active because their source input is stale or was replaced", False,
                          f"{len(life.info['not_refreshed'])} row(s): " + "; ".join(f"{x['master_id']} ({','.join(x['sources'])})" for x in life.info['not_refreshed'][:5]),
                          "review"))
    return out


# ---- summary ------------------------------------------------------------------------------------------------------
def _summary(mode, started_at, month, selected, infos, recs, groups, dd, master, excluded, gc, geocoded, cache_written, overrides,
             hdp_file_note, hdp_file_ids, extras, change, prev_path, unresolved, rejected, id_events, checks, include, across,
             master_all=None) -> dict:
    master_all = master if master_all is None else master_all
    review = master_all[master_all["needs_review"] == "True"]
    by_reason: dict[str, dict] = {}
    for reason in ms.REVIEW_REASONS:
        sub = review[pd.Series([reason in s.split("; ") for s in review["needs_review_reason"]], index=review.index, dtype=bool)]
        src = Counter(s for lb in sub["listed_by"] for s in lb.split("|"))
        by_reason[reason] = {"rows": len(sub), "by_source": dict(sorted(src.items()))}
    doubt_by_reason: dict[str, dict] = {}
    for reason in ms.DOUBT_REASONS:  # the former review reasons: doubt flags now, never needs_review
        sub = master[pd.Series([reason in s.split("; ") for s in master["doubt_flags"]], index=master.index, dtype=bool)]
        src = Counter(s for lb in sub["listed_by"] for s in lb.split("|"))
        doubt_by_reason[reason] = {"rows": len(sub), "by_source": dict(sorted(src.items()))}
    shared = master[master["doubt_flags"].str.contains("shared_address_other_uo", regex=False)]
    examples = [{"address": f"{r.street}, {r.zip} {r.city}", "listed_by": r.listed_by,
                 "flags": [f for f in r.doubt_flags.split("; ") if f.startswith("shared_address_other_uo")]} for r in shared.head(10).itertuples()]
    hdp_licence_candidates = []
    kdf = extras.get("kues")
    if kdf is not None and "kues_licensee" in kdf.columns:
        hdp_licence_candidates = sorted(kdf.loc[kdf["kues_licensee"].str.contains("I1122", regex=False), "location_id"])
    return {
        "inputs": {s: {"run_folder": c.run_dir.name, "mode": c.mode, "pass_evidence": c.why, "file": infos[s].file} for s, c in selected.items()},
        "rows_in": {s: infos[s].rows for s in mi.SOURCE_ORDER},
        "rows_in_total": sum(i.rows for i in infos.values()),
        "excluded_out_of_scope": dict(excluded),
        "master_rows": len(master),
        "merged_away": sum(len(c) - 1 for c in dd.comps),
        "merges": {"edges_by_rule": dd.info["edges_by_rule"], "merged_away_by_family": dd.info["merged_away_by_family"],
                   "similar_merges": [list(x) for x in dd.info["similar_merges"]],
                   "sign_not_1to1": dd.info["sign_not_1to1"], "suppressed_by_override": dd.info["suppressed"],
                   "no_plz_ambiguous": dd.info["no_plz_ambiguous"]},
        "kept_apart_same_source_address": [list(p) for p in dd.info["weak_pairs"]],
        "master_by_uo": _counts(master["uo"]),
        "master_by_site_type": _counts(master["site_type"]),
        "master_by_psp": _counts(master["psp"]),
        "master_by_state": _counts(master["state"].replace("", "(none)")),
        "needs_review_rows": len(review),
        "needs_review_by_reason": by_reason,
        "doubt_flags_by_reason": doubt_by_reason,
        "candidates": {"unresolved_pairs": len(unresolved), "rejected_pairs": len(rejected)},
        "geocoding": {"rows_geocoded": geocoded, "live_requests": gc.requests_made, "cache_hits": gc.cache_hits, "cache_entries_written": cache_written,
                      "cap": gc.cap, "cap_reached": gc.capped, "blocked": gc.blocked, "errors": gc.errors[:10], "attribution": geocode.ATTRIBUTION,
                      "rows_without_coordinates": int((master["lat"] == "").sum())},
        "plz_corrections": [c for i in infos.values() for c in i.plz_corrections],
        "plz_padded_4_to_5_digits": sum(i.plz_padded for i in infos.values()),
        "cross_family_shared_addresses": {"count_by_family_pair": dd.info["shared_other_uo"], "rows_flagged": len(shared), "examples": examples,
                                          "merge_across_uo": across},
        "overrides": {"applied": dd.info["overrides_applied"], "unused": dd.info["overrides_unused"], "decisions_in_file": len(overrides)},
        "id_events": id_events,
        "previous_official_master": str(prev_path) if prev_path else None,
        "diff": dict(Counter(change["change"])) if len(change) else {"note": "no previous official master: headers only" if prev_path is None else "no changes"},
        "known_coverage_gaps": {s: list(mi.SOURCES[s].known_gaps) for s in mi.SOURCE_ORDER},
        "is_hdp_site": {"note": hdp_file_note, "rows_true": int((master["is_hdp_site"] == "True").sum()),
                          "kues_licence_I1122_candidates_info_only": hdp_licence_candidates},
        "include_out_of_scope": include,
        "warnings": [w for i in infos.values() for w in i.warnings],
        "checks": checks,
    }


def render_md(s: dict) -> str:
    L = [f"# Master summary {s['month']} ({s['mode']}): {s['status']}", ""]
    failed = [c for c in s["checks"] if not c["ok"]]
    if failed:
        L += ["**Failed checks**", *[f"- {c['check']}: {c['detail']} ({c['severity']})" for c in failed], ""]
    if s.get("error"):
        L += [f"**Run stopped:** {s['error']}", ""]
        return "\n".join(L)
    L += ["## Inputs", "| source | run folder | rows in |", "|---|---|---|"]
    L += [f"| {k} | {v['run_folder']} ({v['mode']}) | {s['rows_in'][k]} |" for k, v in s["inputs"].items()]
    L += ["", f"Rows in {s['rows_in_total']} -> master {s['master_rows']} (merged away {s['merged_away']}, "
          f"excluded out of scope {sum(s['excluded_out_of_scope'].values())}).", ""]
    L += ["## Merges", f"- by rule: {s['merges']['edges_by_rule']}", f"- merged-away rows per family: {s['merges']['merged_away_by_family']}",
          f"- kept apart, same address within one source: {len(s['kept_apart_same_source_address'])} pair(s)",
          f"- overrides applied {s['overrides']['applied']} of {s['overrides']['decisions_in_file']}; unused: {s['overrides']['unused'] or 'none'}", ""]
    L += ["## Master", f"- per uo: {s['master_by_uo']}", f"- per site_type: {s['master_by_site_type']}", f"- per psp: {s['master_by_psp']}", f"- per state: {s['master_by_state']}", ""]
    L += [f"## Rows to review (status changed in this run): {s['needs_review_rows']}"]
    L += [f"- {r}: {v['rows']} ({v['by_source']})" for r, v in s["needs_review_by_reason"].items()]
    lc = s.get("lifecycle")
    if lc:
        L += ["", "## Lifecycle", f"- rows in the master file {s.get('master_rows_total')}: active {lc['active_total']}, retired {lc['retired_total']} "
              f"(retired earlier and carried: {lc['carried_retired']})",
              f"- this run: new {lc['new']}, retired {len(lc['retired'])} {lc['retired_by_source'] or ''}, reactivated {len(lc['reactivated'])}",
              f"- kept active because the source input is stale or was replaced: {len(lc['not_refreshed'])}",
              f"- possibly re-keyed (retired + new at one address, not merged): {len(lc['rekey'])}"]
    L += ["", "## Doubt flags (not needs_review)"]
    L += [f"- {r}: {v['rows']} ({v['by_source']})" for r, v in s.get("doubt_flags_by_reason", {}).items()]
    g = s["geocoding"]
    L += ["", "## Geocoding", f"- rows geocoded {g['rows_geocoded']}, live requests {g['live_requests']} of cap {g['cap']}, cache hits {g['cache_hits']}, "
          f"rows still without coordinates {g['rows_without_coordinates']}", f"- {g['attribution']}", ""]
    L += ["## PLZ", f"- explicit corrections: {s['plz_corrections'] or 'none'}", f"- 4-digit codes padded to 5: {s['plz_padded_4_to_5_digits']}", ""]
    x = s["cross_family_shared_addresses"]
    L += ["## Same address in different families (not merged)", f"- {x['count_by_family_pair']}, rows flagged {x['rows_flagged']}",
          *[f"- {e['address']}: {e['listed_by']}" for e in x["examples"]], ""]
    L += ["## Known coverage gaps (from scraper_status.md)"]
    L += [f"- {k}: {' '.join(v)}" for k, v in s["known_coverage_gaps"].items() if v]
    L += [""] + hdp_removal.render_section(s.get("hdp_own_sites") or {"enabled": False, "note": "not part of this summary"})
    L += [f"## is_hdp_site", f"- {s['is_hdp_site']['note']}",
          f"- KÜS licence I1122 candidates (info only): {len(s['is_hdp_site']['kues_licence_I1122_candidates_info_only'])}", ""]
    if s["warnings"]:
        L += ["## Warnings", *[f"- {w}" for w in s["warnings"]], ""]
    L += ["## Checks", *[f"- {'PASS' if c['ok'] else 'FAIL'} ({c['severity']}): {c['check']}: {c['detail']}" for c in s["checks"]]]
    return "\n".join(L) + "\n"


# ---- writing ------------------------------------------------------------------------------------------------------
def _write(mode, month, started_at, root, status, master, change, merge_log, extra_files, selected, infos, summary,
           hdp_frames: tuple[pd.DataFrame, pd.DataFrame] | None = None, prev: pd.DataFrame | None = None) -> Path:
    out = paths.master_output_dir(month, root) if mode == "official" else paths.new_master_dev_dir(started_at, root)
    if mode == "official":
        out.parent.mkdir(parents=True, exist_ok=True)
        out.mkdir(exist_ok=False)
    tag = "" if status == "PASS" else "_unverified"
    write_csv(master, out / f"pruefstellen_master{tag}_{month}.csv")
    write_csv(change, out / f"diff_{month}.csv")
    for name, frame in hubspot_export.files_for_run(prev, master, month, tag).items():  # HubSpot import files (see hubspot_export)
        hubspot_export.write_hubspot_csv(frame, out / name)
    write_csv(merge_log, out / "merge_log.csv")
    if hdp_frames is not None:
        write_csv(hdp_frames[0], out / hdp_removal.REMOVED_FILE)
        write_csv(hdp_frames[1], out / hdp_removal.REVIEW_FILE)
    for slug, e in extra_files.items():
        write_csv(e, out / f"pruefstellen_extra_{slug}_{month}.csv")
    manifest = mi.input_manifest(selected, {s: i.rows for s, i in infos.items()}, root)
    write_csv(manifest, out / "input_manifest.csv")
    (out / "master_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "master_summary.md").write_text(render_md(summary), encoding="utf-8")
    return out


def _fail_early(mode, month, started_at, root, summary, error: str) -> RunResult:
    summary.update(status="FAIL", error=error, checks=[_check("run could be built", False, error)])
    out = None
    if mode == "dev":
        out = paths.new_master_dev_dir(started_at, root)
        (out / "master_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "master_summary.md").write_text(render_md(summary), encoding="utf-8")
    return RunResult("FAIL", out, None, summary)


def spot_check(master_id: str, run_dir, root=None) -> pd.DataFrame:
    """All contributing source rows of one master row side by side (fields x rows), read from the run's own files."""
    run_dir = Path(run_dir)
    month = next(run_dir.glob("pruefstellen_master*_*.csv")).stem[-7:]
    master = read_master(next(run_dir.glob("pruefstellen_master*_*.csv")))
    row = master[master["master_id"] == master_id]
    if row.empty:
        raise KeyError(f"{master_id} is not in {run_dir.name}")
    cols: dict[str, pd.Series] = {"MASTER " + master_id: row.iloc[0]}
    manifest = mi.read_source_csv(run_dir / "input_manifest.csv")
    base = paths.project_root() if root is None else Path(root)
    for lid in [x for x in row.iloc[0]["source_ids"].split("|") if x]:
        slug = next(s for s in mi.SOURCE_ORDER if lid.startswith(s.upper() + "-"))
        file = base / manifest.loc[manifest["slug"] == slug, "file"].iloc[0]
        df = mi.read_source_csv(file)
        src = df[df["location_id"] == lid]
        if not src.empty:
            cols[lid] = src.iloc[0]
    out = pd.DataFrame(cols).fillna("")
    return out[(out != "").any(axis=1)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.master_run", description="Build the Prüfstellen master from the per-source exports.")
    ap.add_argument("mode", choices=["dev", "official"])
    ap.add_argument("--confirm", action="store_true", help="required for an official run")
    ap.add_argument("--offline", action="store_true", help="geocode from the cache only, no live Nominatim requests")
    ap.add_argument("--accept-retirements", action="store_true",
                    help="a source really lost more than 10 %% of its sites: the mass-retirement check becomes a review item")
    a = ap.parse_args(argv)
    res = run(a.mode, confirm=a.confirm, live_geocoding=not a.offline, accept_retirements=a.accept_retirements)
    sys.stdout.reconfigure(encoding="utf-8")
    print(render_md(res.summary))
    print(f"output: {res.out_dir}")
    return 0 if res.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
