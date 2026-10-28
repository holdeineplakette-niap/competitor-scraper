"""TÜV SÜD coverage proof and pass planning (sources/tuev_sued.md §4 and §5). The geometry lives in the shared
core.radius_cover ("Variable radius"); this module turns parsed answers into observations and applies the numbers.

Each answer proves a disk around the server's centre with radius r_q = distance of its last record - 0.5 km. A target
(a regular GeoNames PLZ centroid) is covered when dist(target, centre) <= r_q - M, M = 15 km. PASS = every target is
covered and the premise holds (answers sorted by distance, every collected site inside a disk was listed by it).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core import radius_cover as rc
from core.grid import GERMANY_BBOX
from tuev_sued import parse

CFG = rc.NearestNConfig()
EXPECTED_COUNT = 50  # the finder returns the 50 nearest; fewer than this is a warning (a radius limit would change the picture)
MAX_PASSES = 4  # per run, then it ends as FAIL with the uncovered targets listed
MAX_PLZ_PER_PASS = 200  # the snippet's hard ceiling
SITE_PLZ_WARN_KM = 10.0  # a collected site this far from every PLZ centroid weakens the proof's assumption
LIST_LIMIT = 25  # long lists in the summary are cut to this many entries (+ the total)


def observations(answers: list[parse.Answer]) -> list[rc.Observation]:
    return [
        rc.Observation(key=f"p{a.pass_no:02d}:{a.zip}", centre=a.centre, ids=a.ids, count=a.count, max_dist_km=a.last_km, radius_km=a.radius_km)
        for a in answers
    ]


def _cut(items: list) -> dict:
    return {"items": items[:LIST_LIMIT], "total": len(items)}


def evaluate(targets: pd.DataFrame, answers: list[parse.Answer], cfg: rc.NearestNConfig = CFG) -> dict:
    """The coverage proof over all answers of all passes so far.

    targets: regular PLZ centroids (zip, lat, lng). Returns ok, the uncovered target PLZ, the premise result and
    warnings (things that are not failures but change the picture: answers below the cap, empty answers, a site far from
    every PLZ centroid ...).
    """
    obs = observations(answers)
    items, _ = parse.merge_items(answers)
    sites = parse.located_sites(items)
    unc = rc.uncovered_mask_radii(targets, obs, cfg.cover_margin_km) if answers else np.ones(len(targets), dtype=bool)
    uncovered = targets.loc[unc, "zip"].tolist()
    misses = rc.radius_misses(sites, obs)
    unsorted = [a.file for a in answers if not a.sorted_ok]
    premise_ok = not misses and not unsorted

    below = [{"zip": a.zip, "pass": a.pass_no, "count": a.count, "last_km": a.last_km} for a in answers if a.count < EXPECTED_COUNT]
    empty = [a.zip for a in answers if a.count == 0]
    deviates = [{"zip": a.zip, "pass": a.pass_no, "deviation_km": round(a.distance_dev_km, 2)} for a in answers
                if a.distance_dev_km is not None and a.distance_dev_km > parse.DISTANCE_DEVIATION_KM]
    problems = [{"file": a.file, "problems": a.problems} for a in answers if a.problems and a.sorted_ok]
    far_sites, max_far = [], None
    if len(sites):
        d = rc._dist(sites["lat"].to_numpy(), sites["lng"].to_numpy(), targets["lat"].to_numpy(), targets["lng"].to_numpy()).min(axis=1)
        max_far = round(float(d.max()), 2)
        far_sites = [{"id": sid, "km": round(float(km), 2)} for sid, km in zip(sites["id"], d) if km > SITE_PLZ_WARN_KM]
    radii = [a.radius_km for a in answers if a.count]
    return {
        "ok": bool(answers) and not uncovered and premise_ok,
        "queries": len(answers),
        "targets": int(len(targets)),
        "cover_margin_km": cfg.cover_margin_km,
        "uncovered": uncovered,
        "premise": {"ok": premise_ok, "misses": misses, "unsorted_answers": unsorted},
        "radius_km": {"min": round(min(radii), 1), "median": round(float(np.median(radii)), 1), "max": round(max(radii), 1)} if radii else None,
        "max_site_to_plz_centroid_km": max_far,
        "warnings": {
            "answers_below_expected_count": below,
            "empty_answers": empty,
            "distance_deviates": deviates,
            "answer_problems": problems,
            "sites_far_from_plz_centroid": far_sites,
        },
    }


def compact(cov: dict) -> dict:
    """The coverage dict as stored in run_summary.json: long lists are cut to their first entries plus the total."""
    out = {k: v for k, v in cov.items() if k not in ("uncovered", "premise", "warnings")}
    out["uncovered"] = _cut(cov["uncovered"])
    out["premise"] = {"ok": cov["premise"]["ok"], "misses": _cut(cov["premise"]["misses"]), "unsorted_answers": _cut(cov["premise"]["unsorted_answers"])}
    out["warnings"] = {k: (_cut(v) if isinstance(v, list) else v) for k, v in cov["warnings"].items() if v}
    return out


def first_pass_queries(targets: pd.DataFrame, previous_zips: list[str] | None = None, cfg: rc.NearestNConfig = CFG) -> list[tuple[str, str]]:
    """Pass 1 as (zip, reason). With the query list of the last passing run: that list ("repeat", first MAX_PLZ_PER_PASS
    of it, only PLZ that are still regular). Otherwise a hex lattice (spacing 60 km) snapped to regular PLZ ("plan")."""
    if previous_zips:
        known = set(targets["zip"])
        zips = [z for z in dict.fromkeys(previous_zips) if z in known]
        if zips:
            return [(z, "repeat") for z in zips[:MAX_PLZ_PER_PASS]]
    lattice = rc.hex_lattice(GERMANY_BBOX, cfg.lattice_spacing_km)
    return [(z, "plan") for z in rc.snap_to_points(lattice, targets, cfg.snap_max_km)["zip"].tolist()[:MAX_PLZ_PER_PASS]]


def fill_in_queries(targets: pd.DataFrame, answers: list[parse.Answer], cfg: rc.NearestNConfig = CFG) -> list[tuple[str, str]]:
    """The next fill-in pass: greedy over the targets that are still uncovered, never a PLZ that was answered already."""
    return rc.plan_fill_radii(targets, observations(answers), {a.zip for a in answers}, cfg)
