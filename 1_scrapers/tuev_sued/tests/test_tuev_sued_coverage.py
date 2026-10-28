"""TÜV SÜD coverage proof and pass planning on small hand-made worlds and on the simulated one."""

import numpy as np
import pandas as pd
import pytest

from core import radius_cover as rc
from tuev_sued import coverage, parse, simulate

KM_LNG = 71.5  # km per degree of longitude at 50 N


def targets(*lngs_km, lat=50.0):
    return pd.DataFrame({"zip": [f"{10000 + i}" for i in range(len(lngs_km))], "city": "x", "lat": lat, "lng": [8.0 + d / KM_LNG for d in lngs_km]})


def answer(plz, centre_km, site_kms, lat=50.0, pass_no=1, seq=1, ids=None, last=None):
    """An answer whose centre is centre_km east of lng 8.0 and whose sites lie at site_kms (east of lng 8.0), nearest first."""
    centre = (lat, 8.0 + centre_km / KM_LNG)
    items = []
    for i, km in enumerate(site_kms):
        tsc_id = (ids or {}).get(km, f"s{km}")
        d = abs(km - centre_km)
        items.append({"tscId": tsc_id, "distance": f"{d:.3f}", "sortdistance": d,
                      "location": {"tscId": tsc_id, "lat": str(lat), "lon": str(8.0 + km / KM_LNG)}})
    items.sort(key=lambda it: it["sortdistance"])
    return parse.to_answer(parse.answer_name(pass_no, seq, plz), {"lat": centre[0], "lon": centre[1], "finaltsc": items})


# ---- the proof

def test_targets_inside_a_disk_with_the_margin_are_covered():
    t = targets(0, 30, 60)
    a = answer("10000", 0, [10, 20, 80])  # last site 80 km away -> r = 79.5, effective 64.5
    result = coverage.evaluate(t, [a])
    assert result["ok"] and result["uncovered"] == [] and result["queries"] == 1
    assert result["radius_km"]["max"] == pytest.approx(79.5)


def test_a_target_within_the_margin_of_the_rim_is_not_covered():
    t = targets(0, 70)
    a = answer("10000", 0, [10, 80])  # r 79.5 -> effective 64.5 km: the target at 70 km is inside the disk but not 15 km inside
    result = coverage.evaluate(t, [a])
    assert not result["ok"] and result["uncovered"] == ["10001"]


def test_no_answers_cover_nothing():
    result = coverage.evaluate(targets(0, 10), [])
    assert not result["ok"] and result["uncovered"] == ["10000", "10001"] and result["queries"] == 0


def test_an_empty_answer_covers_nothing_and_is_flagged():
    a = answer("10000", 0, [])
    result = coverage.evaluate(targets(0), [a])
    assert not result["ok"] and result["warnings"]["empty_answers"] == ["10000"]


def test_several_answers_together_cover():
    t = targets(0, 100)
    a1 = answer("10000", 0, [10, 70], seq=1)
    a2 = answer("10001", 100, [90, 70, 170], seq=2)  # lists the site at 70 km too: it lies inside this disk as well
    assert coverage.evaluate(t, [a1, a2])["ok"]
    assert not coverage.evaluate(t, [a1])["ok"]


def test_the_premise_fails_when_a_site_inside_the_disk_was_not_listed():
    t = targets(0)
    honest = answer("10000", 0, [10, 20, 80], seq=1)
    assert coverage.evaluate(t, [honest])["premise"]["ok"]
    # a second answer from a centre 5 km away lists the far site but not the one at 10 km, which lies well inside its disk
    liar = answer("10001", 5, [20, 80], seq=2, ids={20: "s20", 80: "s80"})
    result = coverage.evaluate(t, [honest, liar])
    assert not result["premise"]["ok"] and not result["ok"]
    assert result["premise"]["misses"][0]["site_id"] == "s10" and result["premise"]["misses"][0]["query"] == "p01:10001"


def test_the_premise_fails_for_answers_not_sorted_by_distance():
    a = answer("10000", 0, [10, 20])
    a.items.reverse()
    a2 = parse.to_answer(a.file, {"lat": 50.0, "lon": 8.0, "finaltsc": a.items})
    result = coverage.evaluate(targets(0), [a2])
    assert not result["premise"]["ok"] and result["premise"]["unsorted_answers"] == [a2.file] and not result["ok"]


def test_warnings_below_the_expected_count_and_far_sites():
    a = answer("10000", 0, [10, 20])
    result = coverage.evaluate(targets(0), [a])
    below = result["warnings"]["answers_below_expected_count"]
    assert below == [{"zip": "10000", "pass": 1, "count": 2, "last_km": 20.0}]
    far = coverage.evaluate(targets(0), [answer("10000", 0, [10, 40])])  # the site at 40 km is 40 km from the only target
    assert far["max_site_to_plz_centroid_km"] == pytest.approx(40, abs=0.5)
    assert far["warnings"]["sites_far_from_plz_centroid"][0]["id"] == "s40"
    near = coverage.evaluate(targets(0, 10, 20), [answer("10000", 0, [5, 15])])
    assert near["warnings"]["sites_far_from_plz_centroid"] == []


def test_a_full_cap_gives_no_below_count_warning():
    sites = list(range(1, 51))
    result = coverage.evaluate(targets(0), [answer("10000", 0, sites)])
    assert result["warnings"]["answers_below_expected_count"] == []


def test_deviating_distances_and_server_messages_are_warned_about():
    a = answer("10000", 0, [10, 20])
    a.distance_dev_km = 3.0
    a.problems.append("errormsg: x")
    result = coverage.evaluate(targets(0), [a])
    assert result["warnings"]["distance_deviates"] == [{"zip": "10000", "pass": 1, "deviation_km": 3.0}]
    assert result["warnings"]["answer_problems"][0]["problems"] == ["errormsg: x"]


def test_compact_cuts_long_lists_and_keeps_the_totals():
    t = targets(*range(0, 3000, 10))
    cov = coverage.evaluate(t, [answer("10000", 0, [10, 20])])
    short = coverage.compact(cov)
    assert short["uncovered"]["total"] == len(cov["uncovered"]) > coverage.LIST_LIMIT
    assert len(short["uncovered"]["items"]) == coverage.LIST_LIMIT and short["ok"] is False
    assert short["premise"]["ok"] and short["premise"]["misses"] == {"items": [], "total": 0}


# ---- planning

def test_first_pass_is_a_snapped_lattice_of_a_few_dozen_to_a_few_hundred_plz(centroids):
    queries = coverage.first_pass_queries(centroids)
    zips = [z for z, _ in queries]
    assert 100 <= len(zips) <= 200 and len(set(zips)) == len(zips) and {r for _, r in queries} == {"plan"}
    assert set(zips) <= set(centroids["zip"])


def test_first_pass_reuses_the_last_passing_query_list(centroids):
    previous = ["80686", "24937", "01067", "00000", "80686"]  # 00000 is not a regular PLZ, 80686 is listed twice
    assert coverage.first_pass_queries(centroids, previous) == [("80686", "repeat"), ("24937", "repeat"), ("01067", "repeat")]
    assert coverage.first_pass_queries(centroids, ["00000"])[0][1] == "plan"  # nothing usable left: back to the lattice
    many = centroids["zip"].tolist()[:300]
    assert len(coverage.first_pass_queries(centroids, many)) == coverage.MAX_PLZ_PER_PASS


def test_fill_in_never_repeats_an_answered_plz_and_stays_within_the_cap():
    t = targets(*range(0, 2000, 10))
    a = answer("10000", 0, [10, 20, 60])
    plan = coverage.fill_in_queries(t, [a])
    zips = [z for z, _ in plan]
    assert 0 < len(zips) <= coverage.CFG.max_fill_per_pass and "10000" not in zips and len(set(zips)) == len(zips)
    assert {r for _, r in plan} == {"fill"}


def test_fill_in_is_empty_when_everything_is_covered():
    t = targets(0, 10)
    assert coverage.fill_in_queries(t, [answer("10000", 0, [10, 80])]) == []


# ---- on the simulated world

def test_a_lattice_pass_on_the_simulated_world_covers_most_targets_and_the_rest_is_planned(centroids):
    sites = simulate.simulated_sites(centroids)
    api = simulate.FakeTuevApi(sites, centroids)
    queries = coverage.first_pass_queries(centroids)
    answers = [parse.to_answer(parse.answer_name(1, i, z), api.query(z), r) for i, (z, r) in enumerate(queries, 1)]
    result = coverage.evaluate(centroids, answers)
    assert result["premise"]["ok"] and 0 < len(result["uncovered"]) < 0.1 * len(centroids)
    nxt = coverage.fill_in_queries(centroids, answers)
    assert 0 < len(nxt) <= coverage.CFG.max_fill_per_pass and not {z for z, _ in nxt} & {z for z, _ in queries}
    assert isinstance(np.asarray(result["uncovered"]), np.ndarray) and isinstance(rc.NearestNConfig(), rc.NearestNConfig)
