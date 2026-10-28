"""Remove HDP's (HolDeinePlakette) own sites from the competitor dataset, with a strict count check.

All HDP sites run under KÜS, so every one of them must exist in the KÜS rows. The aggregator reads the newest HDP run
(1_scrapers/hdp/output/), matches each HDP site to exactly one KÜS row, removes those rows before any merging, and checks
that the number of rows actually removed (R) equals the number of sites the HDP scraper returned (N). A difference is a
review item of the build (the unresolved KÜS rows get the doubt flag `hdp_match_unresolved`), not a failure.

Matching (a KÜS row is only ever matched by the HDP site's own address, never by a name):
  1. address: same normalised PLZ + street + house number (Str./Straße/strasse, case, umlauts/ß, "15a" = "15 a");
  2. fallback, only if (1) found nothing: same PLZ and coordinates within MATCH_RADIUS_M. Every fallback match is listed.
Each HDP site must match exactly one KÜS row; a KÜS row may be claimed by at most one HDP site. A site with no match, with
several matches, or a KÜS row claimed twice is a review item (not a failure), and none of the ambiguous rows is removed.
Decided cases (Nikos) override this: PINNED_MATCHES fixes the KÜS row of a site, KNOWN_NOT_LISTED sites are not matched and
not counted in N, and KEPT_OWN_ROWS stay in the master flagged KEPT_FLAG (kept out of HubSpot).

The HDP input is never optional: no HDP run, a failed or empty newest run, or one older than MAX_HDP_AGE_DAYS stops the
aggregator. It never falls back to removing nothing, and never to an older passing run behind a newer failed one.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from core import geo, paths
from core import master_inputs as mi
from core import normalise as nm

HDP_SLUG = "hdp"
KUES_SLUG = "kues"
MAX_HDP_AGE_DAYS = 7  # N must describe the HDP sites as they are now: scrape them right before the aggregator runs
MATCH_RADIUS_M = 150.0
NEAREST_N = 3
REMOVED_FILE = "removed_hdp_own_sites.csv"
REVIEW_FILE = "hdp_review.csv"
REVIEW_COLUMNS = ["hdp_id", "hdp_address", "problem", "match_method", "kues_location_id", "master_id", "kues_address", "distance_m",
                  "kues_row_flagged_in_master"]
REMOVED_COLUMNS = ["hdp_id", "hdp_location_id", "hdp_address", "match_method", "distance_m", "kues_location_id", "kues_street",
                   "kues_zip", "kues_city", "kues_name", "kues_licensee"]
HDP_LICENCE = "I1122"  # the KÜS licence code believed to be HDP's own (info only, never used to remove a row)

# Nikos's decisions on the two review items of the 2026-10 build (2026-10-21, 2026-10-26). They only apply while the rows exist:
# a pinned KÜS row that is gone falls back to the normal matching (and so to a review item again).
PINNED_MATCHES = {"Dortmund": "KUES-30052"}  # HDP id -> the KÜS row that is removed as this site (two KÜS rows at Wambeler Hellweg)
KEPT_OWN_ROWS = {"KUES-58908": "Dortmund"}  # KÜS row of an HDP site that stays in the master as a second row (Has4), never imported
KNOWN_NOT_LISTED = {"Fürth": "confirmed HDP site that KÜS does not list"}  # not matched, not counted in N
KEPT_FLAG = "hdp_own_site_kept"  # doubt token of a KEPT_OWN_ROWS row; the HubSpot export keeps it out


class HdpInputError(Exception):
    """The HDP run the aggregator needs is missing, failed, empty, stale or unreadable."""


@dataclass
class HdpInput:
    run_folder: str
    mode: str
    file: str
    n_found: int
    data_started: str
    age_days: float
    sites: list[dict]


@dataclass
class Matching:
    n: int
    sites: list[dict] = field(default_factory=list)  # one entry per HDP site, in input order
    claimed_twice: list[dict] = field(default_factory=list)  # {"kues_location_id", "hdp_ids"}
    removed_ids: set[str] = field(default_factory=set)  # KÜS location_ids that may be removed

    @property
    def unmatched(self) -> list[dict]:
        return [s for s in self.sites if s["status"] == "unmatched"]

    @property
    def multiple(self) -> list[dict]:
        return [s for s in self.sites if s["status"] == "multiple"]

    @property
    def known_exceptions(self) -> list[dict]:
        return [s for s in self.sites if s["status"] == "known_exception"]

    @property
    def fallback(self) -> list[dict]:
        return [s for s in self.sites if s["status"] in ("matched", "claimed_twice") and s["method"] == "coordinates"]


# ---- reading the HDP run --------------------------------------------------------------------------------------------
def _float(v) -> float | None:
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def load_hdp_input(root=None, now: datetime | None = None) -> HdpInput:
    """The newest HDP run folder, which must be a PASS with a readable site file. Raises HdpInputError otherwise."""
    now = now or datetime.now()
    out = paths.output_dir(HDP_SLUG, "full", root)
    folders = sorted((d for d in out.iterdir() if d.is_dir()), reverse=True) if out.is_dir() else []
    if not folders:
        raise HdpInputError(f"no HDP run in {out}: run `.venv\\Scripts\\python.exe -m hdp.run full --confirm` first")
    run = folders[0]  # the NEWEST run decides: a failed run is never skipped in favour of an older passing one
    f = run / paths.RUN_SUMMARY_JSON
    if not f.exists():
        raise HdpInputError(f"the newest HDP run {run.name} has no {paths.RUN_SUMMARY_JSON} (an unfinished run?)")
    try:
        s = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HdpInputError(f"{run.name}/{paths.RUN_SUMMARY_JSON} is unreadable: {exc}") from exc
    if s.get("status") != "PASS":
        why = s.get("error") or "; ".join(s.get("failed_hard") or []) or "no reason recorded"
        raise HdpInputError(f"the newest HDP run {run.name} did not pass ({s.get('outcome', s.get('status'))}): {why}. "
                            "The aggregator never runs without the HDP site list; rerun the HDP scraper")
    if s.get("mode") not in ("full", "rebuild"):
        raise HdpInputError(f"the newest HDP run {run.name} is a {s.get('mode')} run, not full or rebuild")
    meta = (s.get("written") or {}).get("sites_csv")
    if not meta:
        raise HdpInputError(f"{run.name} passed but names no site file in its summary")
    csv_path = run / meta["file"]
    if not csv_path.exists():
        raise HdpInputError(f"{csv_path.name} named in the summary of {run.name} does not exist")
    df = mi.read_source_csv(csv_path)
    n = s.get("n_sites")
    if not n or len(df) != n:
        raise HdpInputError(f"{run.name}: the summary says {n} site(s), the file has {len(df)} row(s)"
                            + (" (0 sites: nothing to remove is not an acceptable result)" if not n else ""))
    try:
        started = datetime.fromisoformat(s.get("data_started") or s["started"])
    except (KeyError, ValueError) as exc:
        raise HdpInputError(f"{run.name}: no readable start time in the summary") from exc
    age = (now - started).total_seconds() / 86400
    if age > MAX_HDP_AGE_DAYS:
        raise HdpInputError(f"the newest HDP run {run.name} is {age:.1f} days old (limit {MAX_HDP_AGE_DAYS}); rerun the HDP scraper")
    sites = []
    for r in df.to_dict("records"):
        site = {"id": r["source_id"], "location_id": r["location_id"], "street": r["street"], "zip": r["zip"].strip(), "city": r["city"],
                "lat": _float(r["lat"]), "lng": _float(r["lng"])}
        if not site["id"] or not site["zip"] or site["lat"] is None or site["lng"] is None:
            raise HdpInputError(f"{run.name}: site {site['location_id'] or '<no id>'} lacks an id, a postcode or coordinates")
        sites.append(site)
    if len({x["id"] for x in sites}) != len(sites):
        raise HdpInputError(f"{run.name}: duplicate site ids in {csv_path.name}")
    return HdpInput(run.name, s["mode"], csv_path.name, int(n), started.isoformat(timespec="seconds"), round(age, 2), sites)


# ---- matching ---------------------------------------------------------------------------------------------------------
def _dist_m(site: dict, rec: dict) -> float | None:
    if rec["lat"] is None or rec["lng"] is None:
        return None
    return float(geo.haversine_km(site["lat"], site["lng"], rec["lat"], rec["lng"])) * 1000


def _kues_summary(rec: dict, dist: float | None) -> dict:
    return {"location_id": rec["location_id"], "street": rec["street"], "zip": rec["zip"], "city": rec["city"],
            "distance_m": None if dist is None else round(dist)}


def match_sites(sites: list[dict], kues_recs: list[dict], radius_m: float = MATCH_RADIUS_M, pinned: dict[str, str] | None = None,
                not_listed: dict[str, str] | None = None, kept: dict[str, str] | None = None) -> Matching:
    """Match every HDP site to KÜS rows (see the module docstring). `kues_recs` are the loaded KÜS records.
    pinned / not_listed / kept default to PINNED_MATCHES / KNOWN_NOT_LISTED / KEPT_OWN_ROWS (Nikos's decisions); a kept row is
    never a match target, so it is never removed, not even if the pinned row disappears."""
    pinned = PINNED_MATCHES if pinned is None else pinned
    not_listed = KNOWN_NOT_LISTED if not_listed is None else not_listed
    kept = KEPT_OWN_ROWS if kept is None else kept
    kues_recs = [r for r in kues_recs if r["location_id"] not in kept]
    by_key: dict[str, list[dict]] = defaultdict(list)
    by_id = {r["location_id"]: r for r in kues_recs}
    for r in kues_recs:
        if r["akey"]:
            by_key[r["akey"]].append(r)
    m = Matching(n=len(sites))
    for site in sites:
        res = {"hdp_id": site["id"], "hdp_location_id": site["location_id"], "address": f"{site['street']}, {site['zip']} {site['city']}",
               "method": "", "status": "unmatched", "kues": [], "distance_m": None, "nearest": []}
        if site["id"] in not_listed:
            res.update(status="known_exception", method=not_listed[site["id"]])
            m.sites.append(res)
            continue
        if pinned.get(site["id"]) in by_id:
            found = [by_id[pinned[site["id"]]]]
            res["method"] = "pinned"
        elif nm.has_number(site["street"]):  # an address without a house number cannot identify a site
            found = by_key.get(nm.address_key(site["street"], site["zip"]), [])
            if found:
                res["method"] = "address"
        else:
            found = []
        if not found:
            near = [(d, r) for r in kues_recs if r["zip"] == site["zip"] and (d := _dist_m(site, r)) is not None and d <= radius_m]
            if near:
                found, res["method"] = [r for _, r in sorted(near, key=lambda x: x[0])], "coordinates"
        if len(found) == 1:
            res.update(status="matched", kues=[_kues_summary(found[0], _dist_m(site, found[0]))],
                       distance_m=None if _dist_m(site, found[0]) is None else round(_dist_m(site, found[0])))
        elif len(found) > 1:
            res.update(status="multiple", kues=[_kues_summary(r, _dist_m(site, r)) for r in found])
        else:
            ranked = sorted(((d, r) for r in kues_recs if (d := _dist_m(site, r)) is not None), key=lambda x: x[0])[:NEAREST_N]
            res["nearest"] = [_kues_summary(r, d) for d, r in ranked]
        m.sites.append(res)
    claims: dict[str, list[str]] = defaultdict(list)
    for s in m.sites:
        if s["status"] == "matched":
            claims[s["kues"][0]["location_id"]].append(s["hdp_id"])
    for lid, ids in sorted(claims.items()):
        if len(ids) > 1:
            m.claimed_twice.append({"kues_location_id": lid, "hdp_ids": ids})
    twice = {c["kues_location_id"] for c in m.claimed_twice}
    for s in m.sites:
        if s["status"] == "matched" and s["kues"][0]["location_id"] in twice:
            s["status"] = "claimed_twice"
    m.removed_ids = {s["kues"][0]["location_id"] for s in m.sites if s["status"] == "matched"}
    return m


def kept_rows(kues_recs: list[dict], kept: dict[str, str] | None = None) -> dict[str, str]:
    """{KÜS location_id: HDP id} of the KEPT_OWN_ROWS that are present: HDP's own site, kept in the master, flagged KEPT_FLAG."""
    kept = KEPT_OWN_ROWS if kept is None else kept
    present = {r["location_id"] for r in kues_recs}
    return {lid: hid for lid, hid in kept.items() if lid in present}


def split_recs(recs: list[dict], matching: Matching) -> tuple[list[dict], list[dict]]:
    """(kept, removed): the removed list is what R is counted from, independently of the matching."""
    removed = [r for r in recs if r["slug"] == KUES_SLUG and r["location_id"] in matching.removed_ids]
    keep = [r for r in recs if not (r["slug"] == KUES_SLUG and r["location_id"] in matching.removed_ids)]
    return keep, removed


def removed_frame(matching: Matching, removed: list[dict]) -> pd.DataFrame:
    """One line per removed row: the matched HDP id and how it was matched (the audit trail of every removal)."""
    by_kues = {s["kues"][0]["location_id"]: s for s in matching.sites if s["status"] == "matched"}
    rows = []
    for r in removed:
        s = by_kues[r["location_id"]]
        rows.append({"hdp_id": s["hdp_id"], "hdp_location_id": s["hdp_location_id"], "hdp_address": s["address"],
                     "match_method": s["method"], "distance_m": "" if s["distance_m"] is None else s["distance_m"],
                     "kues_location_id": r["location_id"], "kues_street": r["street"], "kues_zip": r["zip"], "kues_city": r["city"],
                     "kues_name": r["name"], "kues_licensee": r["src"].get("kues_licensee", "")})
    return pd.DataFrame(rows, columns=REMOVED_COLUMNS).sort_values("hdp_id", kind="stable").reset_index(drop=True)


# ---- checks and report ------------------------------------------------------------------------------------------------
def _check(name: str, ok: bool, detail: str, severity: str = "hard") -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": severity}


def _kdesc(k: dict) -> str:
    return f"{k['location_id']} ({k['street']}, {k['zip']} {k['city']}" + ("" if k["distance_m"] is None else f", {k['distance_m']} m") + ")"


def count_checks(inp: HdpInput, matching: Matching, n_removed: int) -> list[dict]:
    """The strict check: every site matched exactly once, no KÜS row claimed twice, and R == N."""
    bad = matching.unmatched + matching.multiple
    known = matching.known_exceptions
    # Decision (Nikos, 2026-10-07): a site that cannot be matched exactly once is a REVIEW item, not a failed build. The
    # affected KÜS rows carry the doubt flag `hdp_match_unresolved` in the master (never needs_review, which is only for retired /
    # reactivated rows); the HDP sites are listed in REVIEW_FILE and the HubSpot export keeps those rows out.
    out = [
        _check("HDP own sites: the newest HDP run passed and is fresh", True,
               f"{inp.run_folder} ({inp.mode}), {inp.n_found} site(s), {inp.age_days} days old (limit {MAX_HDP_AGE_DAYS})"),
        _check("HDP own sites: every HDP site matches exactly one KÜS row", not bad,
               f"{len(matching.sites) - len(bad) - len(known)} of {len(matching.sites) - len(known)} matched once; unmatched: "
               f"{[s['hdp_id'] for s in matching.unmatched] or 'none'}; several matches: {[s['hdp_id'] for s in matching.multiple] or 'none'}",
               "review"),
        _check("HDP own sites: no KÜS row is claimed by two HDP sites", not matching.claimed_twice,
               "; ".join(f"{c['kues_location_id']} <- {c['hdp_ids']}" for c in matching.claimed_twice) or "none", "review"),
        _check("HDP own sites: rows removed (R) equal HDP sites found (N)", n_removed == inp.n_found - len(known),
               f"N = {inp.n_found}" + (f" minus {len(known)} known exception(s)" if known else "") + f", R = {n_removed}", "review"),
    ]
    out.append(_check("HDP own sites: known exceptions (KÜS does not list them; decided, not matched)", True,
                      "; ".join(f"{s['hdp_id']} ({s['address']}): {s['method']}" for s in known) or "none", "info"))
    out.append(_check("HDP own sites: pinned matches (decided)", True,
                      "; ".join(f"{s['hdp_id']} -> {s['kues'][0]['location_id']}" for s in matching.sites
                                if s["method"] == "pinned" and s["kues"]) or "none", "info"))
    out.append(_check("HDP own sites: coordinate fallback matches (review each one)", True,
                      "; ".join(f"{s['hdp_id']} -> {s['kues'][0]['location_id']} ({s['distance_m']} m)" for s in matching.fallback) or "none",
                      "info"))
    return out


def review_targets(matching: Matching) -> dict[str, list[str]]:
    """{KÜS location_id: [HDP ids]} of the KÜS rows that stay in the master but are in question: every candidate of an HDP site
    with several matches, and every KÜS row claimed by two HDP sites. They get the doubt flag `hdp_match_unresolved`.
    (The nearest rows of an unmatched site are not in question: they are other addresses.)"""
    out: dict[str, list[str]] = defaultdict(list)
    for s in matching.multiple:
        for k in s["kues"]:
            out[k["location_id"]].append(s["hdp_id"])
    for c in matching.claimed_twice:
        out[c["kues_location_id"]] += c["hdp_ids"]
    return {k: sorted(set(v)) for k, v in out.items()}


def review_frame(matching: Matching, master_id_of: dict[str, str]) -> pd.DataFrame:
    """One line per HDP site that needs a human (and per KÜS row in question): the review list of this build."""
    rows = []
    flagged = review_targets(matching)
    for s in matching.sites:
        if s["status"] in ("matched", "known_exception"):
            continue
        problem = {"unmatched": "no KÜS row found", "multiple": "several KÜS rows match", "claimed_twice": "KÜS row also claimed by another HDP site"}[s["status"]]
        cands = s["kues"] if s["status"] != "unmatched" else s["nearest"]
        for k in cands:
            rows.append({"hdp_id": s["hdp_id"], "hdp_address": s["address"], "problem": problem,
                         "match_method": s["method"], "kues_location_id": k["location_id"], "master_id": master_id_of.get(k["location_id"], ""),
                         "kues_address": f"{k['street']}, {k['zip']} {k['city']}", "distance_m": "" if k["distance_m"] is None else k["distance_m"],
                         "kues_row_flagged_in_master": k["location_id"] in flagged})
        if not cands:
            rows.append({"hdp_id": s["hdp_id"], "hdp_address": s["address"], "problem": problem, "match_method": s["method"],
                         "kues_location_id": "", "master_id": "", "kues_address": "", "distance_m": "", "kues_row_flagged_in_master": False})
    return pd.DataFrame(rows, columns=REVIEW_COLUMNS)


def final_check(removed: list[dict], master_source_ids: list[str]) -> dict:
    """After merging: none of the removed rows may still be in the master."""
    left = sorted({r["location_id"] for r in removed} & set(master_source_ids))
    return _check("HDP own sites: no removed row is left in the master", not left, f"still in the master: {left or 'none'}")


def section(inp: HdpInput, matching: Matching, removed: list[dict], remaining_kues: list[dict], passed: bool) -> dict:
    """The 'HDP own-site removal' block of the master summary (JSON-serialisable)."""
    other = sorted(r["location_id"] for r in remaining_kues
                   if r["slug"] == KUES_SLUG and HDP_LICENCE in r["src"].get("kues_licensee", ""))
    return {"enabled": True, "status": "PASS" if passed else "REVIEW", "hdp_run": inp.run_folder, "hdp_mode": inp.mode, "hdp_file": inp.file,
            "hdp_age_days": inp.age_days, "n_found": inp.n_found, "r_removed": len(removed),
            "matches": [{"hdp_id": s["hdp_id"], "hdp_address": s["address"], "status": s["status"], "method": s["method"],
                         "distance_m": s["distance_m"], "kues": [k["location_id"] for k in s["kues"]]} for s in matching.sites],
            "fallback_matches": [{"hdp_id": s["hdp_id"], "kues_location_id": s["kues"][0]["location_id"], "distance_m": s["distance_m"]}
                                 for s in matching.fallback],
            "unmatched": [{"hdp_id": s["hdp_id"], "address": s["address"], "nearest_kues": s["nearest"]} for s in matching.unmatched],
            "multiple": [{"hdp_id": s["hdp_id"], "address": s["address"], "method": s["method"], "kues": s["kues"]} for s in matching.multiple],
            "claimed_twice": matching.claimed_twice,
            "kues_rows_flagged_for_review": sorted(review_targets(matching)),
            "sites_to_review": sorted({s["hdp_id"] for s in matching.sites if s["status"] not in ("matched", "known_exception")}),
            "known_exceptions": [{"hdp_id": s["hdp_id"], "address": s["address"], "why": s["method"]} for s in matching.known_exceptions],
            "kues_licence_I1122_not_removed_info_only": other}


def render_section(h: dict) -> list[str]:
    """Markdown lines of the section (used by master_run.render_md)."""
    if not h.get("enabled"):
        return ["## HDP own-site removal", f"- {h.get('note', 'not run')}", ""]
    L = ["## HDP own-site removal",
         f"- **{h['status']}**: N = {h['n_found']} HDP site(s) found, R = {h['r_removed']} row(s) removed "
         f"(HDP run {h['hdp_run']}, {h['hdp_mode']}, {h['hdp_age_days']} days old)"]
    if h["status"] != "PASS":
        L += [f"- The build is NOT failed by this. {len(h['sites_to_review'])} HDP site(s) need a human: {', '.join(h['sites_to_review'])} "
              f"(listed in `{REVIEW_FILE}`). KÜS rows left in the master and flagged `hdp_match_unresolved`: "
              f"{', '.join(h['kues_rows_flagged_for_review']) or 'none'}."]
    L += [f"- {m['hdp_id']} -> {', '.join(m['kues']) or '-'}: {m['status']}" + (f" by {m['method']}" if m["method"] else "")
          + ("" if m["distance_m"] is None else f" ({m['distance_m']} m)") for m in h["matches"]]
    L += [f"- Coordinate fallback matches ({len(h['fallback_matches'])}): "
          + ("; ".join(f"{f['hdp_id']} -> {f['kues_location_id']} ({f['distance_m']} m)" for f in h["fallback_matches"]) or "none")]
    for k in h.get("known_exceptions", []):
        L += [f"- Known exception {k['hdp_id']} ({k['address']}): {k['why']}; not matched, not counted in N"]
    for u in h["unmatched"]:
        L += [f"- **UNMATCHED** {u['hdp_id']} ({u['address']}); nearest KÜS rows: " + "; ".join(_kdesc(k) for k in u["nearest_kues"])]
    for u in h["multiple"]:
        L += [f"- **SEVERAL MATCHES** {u['hdp_id']} ({u['address']}, by {u['method']}): " + "; ".join(_kdesc(k) for k in u["kues"])]
    for c in h["claimed_twice"]:
        L += [f"- **KÜS ROW CLAIMED TWICE** {c['kues_location_id']} by {c['hdp_ids']}"]
    L += [f"- Other KÜS rows with licence {HDP_LICENCE} that were not removed (info only): "
          f"{', '.join(h['kues_licence_I1122_not_removed_info_only']) or 'none'}",
          f"- Removed rows with the matched HDP id and method: `{REMOVED_FILE}`", ""]
    return L
