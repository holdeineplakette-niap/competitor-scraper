"""core.radius_cover: lattice, snapping, rules A-D, fill-in and the generic simulator."""

import math

import numpy as np
import pandas as pd
import pytest

from core import radius_cover as rc
from core.geo import haversine_km

CFG = rc.CoverConfig()


def points(*rows):
    return pd.DataFrame(rows, columns=["zip", "city", "lat", "lng"])


def obs(key, centre, ids, max_dist=None):
    return rc.Observation(key, centre, frozenset(ids), len(ids), max_dist)


# ---- config

def test_config_defaults_match_the_50_km_design():
    assert (CFG.radius_km, CFG.cover_km, CFG.fringe_km, CFG.consistency_km) == (50, 25, 45, 48)
    assert CFG.lattice_spacing_km == pytest.approx(0.85 * math.sqrt(3) * 25)


def test_config_for_other_radius_scales_the_proportions():
    cfg = rc.CoverConfig.for_radius(100)
    assert (cfg.cover_km, cfg.fringe_km, cfg.consistency_km) == (50, 90, 96)


# ---- lattice and snapping

def test_hex_lattice_rows_and_spacing():
    lattice = rc.hex_lattice((50.0, 52.0, 8.0, 10.0), spacing_km=40)
    assert len(lattice) > 4
    # neighbours in a row are one spacing apart (projected km, so a great-circle check allows a few % east-west)
    same_row = lattice[np.isclose(lattice[:, 0], lattice[0, 0])]
    gap = float(haversine_km(same_row[0, 0], same_row[0, 1], same_row[1, 0], same_row[1, 1]))
    assert 36 < gap < 44
    assert lattice[:, 0].min() >= 49.9 and lattice[:, 1].min() >= 7.9


def test_snap_skips_lattice_points_without_a_plz_nearby_and_dedupes():
    pts = points(("10000", "A", 50.0, 8.0), ("20000", "B", 51.0, 9.0))
    lattice = np.array([[50.01, 8.01], [50.02, 8.02], [53.0, 9.0], [51.0, 9.0]])  # two snap to A, one is far away
    snapped = rc.snap_to_points(lattice, pts, snap_max_km=15)
    assert snapped["zip"].tolist() == ["10000", "20000"]


def test_plan_initial_on_real_plz_is_a_few_hundred_queries(centroids):
    plan = rc.plan_initial(centroids, CFG)
    assert 250 < len(plan) < 450
    assert plan["zip"].is_unique


# ---- rule A

def test_uncovered_mask_uses_cover_km_from_the_actual_centres():
    pts = points(("1", "a", 50.0, 8.0), ("2", "b", 50.0, 8.0 + 30 / 71.5), ("3", "c", 50.0, 8.0 + 20 / 71.5))
    centres = np.array([[50.0, 8.0]])
    assert rc.uncovered_mask(pts, centres, 25).tolist() == [False, True, False]
    assert rc.uncovered_mask(pts, np.empty((0, 2)), 25).all()


# ---- rule B

def test_fringe_sites_are_those_beyond_fringe_km_of_every_centre():
    sites = pd.DataFrame({"id": ["near", "rim"], "lat": [50.0, 50.0], "lng": [8.0, 8.0 + 47 / 71.5]})
    out = rc.fringe_sites(sites, np.array([[50.0, 8.0]]), 45)
    assert out["id"].tolist() == ["rim"]


# ---- rule C

def test_appearance_misses_lists_a_centre_that_should_have_returned_the_site():
    sites = pd.DataFrame({"id": ["s1"], "lat": [50.0], "lng": [8.0]})
    centre_near = (50.0, 8.0 + 10 / 71.5)
    centre_boundary = (50.0, 8.0 + 49 / 71.5)  # 48-50 km away: not checked
    ok = obs("1", centre_near, ["s1"])
    miss = obs("2", centre_near, [])
    boundary = obs("3", centre_boundary, [])
    empty = obs("4", None, [])
    assert rc.appearance_misses(sites, [ok, boundary, empty], 48) == []
    result = rc.appearance_misses(sites, [ok, miss], 48)
    assert len(result) == 1 and result[0]["query"] == "2" and result[0]["site_id"] == "s1"


# ---- rule D

def test_cap_findings_hard_limit_and_plateau():
    assert rc.cap_findings([obs(str(i), None, range(n)) for i, n in enumerate([5, 80, 120])], CFG) == []
    plateau = [obs(str(i), None, range(100)) for i in range(3)] + [obs("x", None, range(10))]
    assert "cap signature" in rc.cap_findings(plateau, CFG)[0]
    assert "response has" in rc.cap_findings([obs("big", None, range(500))], CFG)[0]
    assert rc.cap_findings([obs(str(i), None, range(5)) for i in range(5)], CFG) == []  # small counts repeat harmlessly


# ---- evaluate and fill-in

def test_evaluate_reports_assumed_empty_separately_from_uncovered():
    pts = points(("1", "a", 50.0, 8.0), ("2", "b", 54.0, 12.0), ("3", "c", 52.0, 10.0))
    observations = [obs("1", (50.0, 8.0), ["x"]), obs("2", None, [])]
    result = rc.evaluate(pts, observations, pd.DataFrame(columns=["id", "lat", "lng"]), CFG, empty_zips={"2"})
    assert result["rule_a"]["assumed_empty"] == ["2"]
    assert result["rule_a"]["uncovered"] == ["3"]
    assert not result["rule_a"]["ok"]


def test_next_fill_queries_prefers_rule_b_then_the_best_cover_candidate():
    pts = points(("1", "a", 50.0, 8.0), ("2", "b", 50.0, 9.0), ("3", "c", 50.0, 9.05), ("4", "d", 50.0, 9.1))
    observations = [obs("1", (50.0, 8.0), [])]
    sites = pd.DataFrame({"id": ["rim"], "lat": [50.0], "lng": [8.0 + 47 / 71.5]})
    batch = rc.next_fill_queries(pts, observations, sites, {"rim": "2"}, CFG, queried={"1"})
    assert batch == [("2", "fill_b")]
    none_left = rc.next_fill_queries(pts, observations, pd.DataFrame(columns=["id", "lat", "lng"]), {}, CFG, queried={"1"})
    assert none_left and none_left[0][1] == "fill_a" and none_left[0][0] in {"2", "3", "4"}
    covered = [obs("1", (50.0, 8.0), []), obs("3", (50.0, 9.05), [])]
    assert rc.next_fill_queries(pts, covered, pd.DataFrame(columns=["id", "lat", "lng"]), {}, CFG, queried={"1", "3"}) == []


# ---- variable radius (nearest-N finders)

NN = rc.NearestNConfig()
KM_LNG = 71.5  # km per degree of longitude at 50 N, as in the tests above


def nn_obs(key, centre, ids, radius):
    return rc.Observation(key, centre, frozenset(ids), len(ids), radius_km=radius)


def test_proven_radius_is_last_distance_minus_the_tie_margin():
    assert rc.proven_radius_km(64.2, 0.5) == pytest.approx(63.7)
    assert rc.proven_radius_km(0.3, 0.5) == 0.0
    assert rc.proven_radius_km(None, 0.5) == 0.0  # an empty answer proves nothing


def test_uncovered_mask_radii_applies_the_margin_per_query():
    pts = points(("1", "a", 50.0, 8.0), ("2", "b", 50.0, 8.0 + 40 / KM_LNG), ("3", "c", 50.0, 8.0 + 60 / KM_LNG))
    o = [nn_obs("q", (50.0, 8.0), ["x"], 60.0)]  # effective radius 60 - 15 = 45 km
    assert rc.uncovered_mask_radii(pts, o, 15).tolist() == [False, False, True]
    small = [nn_obs("q", (50.0, 8.0), ["x"], 30.0)]  # 30 - 15 = 15 km
    assert rc.uncovered_mask_radii(pts, small, 15).tolist() == [False, True, True]
    zero = [nn_obs("q", (50.0, 8.0), [], 0.0), nn_obs("none", None, [], 80.0), rc.Observation("fixed", (50.0, 8.0), frozenset(), 0)]
    assert rc.uncovered_mask_radii(pts, zero, 15).all()  # radius 0, no centre and no radius cover nothing
    assert rc.uncovered_mask_radii(pts, [], 15).all()


def test_radius_misses_flags_a_site_inside_the_radius_that_was_not_listed():
    sites = pd.DataFrame({"id": ["in", "edge", "out"], "lat": [50.0] * 3, "lng": [8.0 + d / KM_LNG for d in (10, 59, 70)]})
    listed = nn_obs("q", (50.0, 8.0), ["in", "edge"], 60.0)
    assert rc.radius_misses(sites, [listed]) == []
    missing = nn_obs("q", (50.0, 8.0), ["in"], 60.0)
    result = rc.radius_misses(sites, [missing])
    assert [m["site_id"] for m in result] == ["edge"] and result[0]["query"] == "q"
    assert rc.radius_misses(sites, [nn_obs("e", None, [], 60.0), nn_obs("z", (50.0, 8.0), [], 0.0)]) == []


def test_estimate_uses_the_smallest_radius_of_the_nearest_centres_or_the_prior():
    centres = np.array([[50.0, 8.0], [50.0, 9.0], [50.0, 12.0]])
    eff = np.array([10.0, 30.0, 90.0])
    est = rc.estimate_effective_radii([50.0, 50.0], [8.1, 11.9], centres, eff, neighbours=2, prior_km=40)
    assert est.tolist() == [10.0, 30.0]  # nearest two of the first point: 10 and 30; of the second: 90 and 30
    assert rc.estimate_effective_radii([50.0], [8.0], np.empty((0, 2)), np.empty(0), 3, 40).tolist() == [40.0]


def test_plan_fill_radii_picks_the_point_that_covers_most_and_never_a_queried_one():
    lngs = [8.0 + d / KM_LNG for d in (0, 100, 130, 160, 300)]
    pts = points(*[(str(i), f"c{i}", 50.0, g) for i, g in enumerate(lngs)])
    first = [nn_obs("0", (50.0, 8.0), ["x"], 60.0)]  # point 0 covered, the rest not
    batch = rc.plan_fill_radii(pts, first, {"0"}, NN)
    zips = [z for z, _ in batch]
    assert batch[0] == ("2", "fill")  # the middle of the 100-160 km cluster reaches all three within the estimated 45 km
    assert "0" not in zips and len(zips) == len(set(zips))
    assert "4" in zips and len(zips) == 2  # the far point needs a query of its own
    queried = {str(i) for i in range(5)}
    assert rc.plan_fill_radii(pts, first, queried, NN) == []  # nothing left to pick, even though points are uncovered


def test_plan_fill_radii_respects_the_per_pass_cap():
    pts = points(*[(str(i), f"c{i}", 50.0, 8.0 + i * 200 / KM_LNG) for i in range(10)])  # 200 km apart: each needs its own query
    cfg = rc.NearestNConfig(max_fill_per_pass=4)
    assert len(rc.plan_fill_radii(pts, [], set(), cfg)) == 4


# ---- simulator

def make_finder(**kwargs):
    sites = pd.DataFrame({"id": [str(i) for i in range(6)], "lat": [50.0] * 6, "lng": [8.0 + i * 12 / 71.5 for i in range(6)]})
    return rc.SimulatedRadiusFinder(sites, centre_offset_km=0.0, **kwargs)


def test_simulated_finder_returns_sites_within_radius_nearest_first():
    centre, found = make_finder().search("z", 50.0, 8.0)
    assert centre == (50.0, 8.0)
    ids = [i for i, _ in found]
    assert ids == ["0", "1", "2", "3", "4"]  # 0, 12, 24, 36, 48 km; site 5 is 60 km away
    assert [d for _, d in found] == sorted(d for _, d in found)


def test_simulated_finder_radius_override_cap_drop_and_unknown_zip():
    f = make_finder()
    assert len(f.search("z", 50.0, 8.0, radius_km=100)[1]) == 6
    assert [i for i, _ in make_finder(cap=2).search("z", 50.0, 8.0)[1]] == ["0", "1"]
    dropped = make_finder(drop=lambda zip_code, site_id: site_id == "1").search("z", 50.0, 8.0)[1]
    assert "1" not in [i for i, _ in dropped]
    assert make_finder(unknown_zips={"z"}).search("z", 50.0, 8.0) == (None, [])


def test_simulated_centre_offset_is_deterministic_per_zip_and_bounded():
    sites = pd.DataFrame({"id": ["0"], "lat": [50.0], "lng": [8.0]})
    f = rc.SimulatedRadiusFinder(sites, centre_offset_km=5.0, seed=3)
    a, b = f.centre_for("12345", 50.0, 8.0), f.centre_for("12345", 50.0, 8.0)
    assert a == b
    assert 0 <= float(haversine_km(50.0, 8.0, *a)) <= 5.05
