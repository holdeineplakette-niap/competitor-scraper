"""The HDP own-site removal in the aggregator: matching, the strict N == R check, and the never-fall-back-to-nothing rules.

The HDP run folders are made by the real adapter (hdp.run with an injected fake API), the KÜS rows by the master fixtures.
No network, no real rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import pytest
from hdp import fetch, run as hdp_run
from hdp.simulate import SimulatedHdp, sample_location

from core import hdp_removal as hr
from core import master_run, paths
from master_fixtures import CENTROIDS, all_sources, source_row
from test_master_run import geocoder  # the fake Nominatim of the master tests

T = datetime(2026, 10, 7, 12, 0)


def hdp_site(sid, street, number, plz, city, lat, lng, **kw):
    return sample_location(0, id=sid, location=sid, publicName=sid, street=street, number=number, postcode=plz, city=city,
                           address=f"{street} {number}, {plz} {city}", latitude=lat, longitude=lng, slugs=[sid.lower(), sid.lower() + "-alias"], **kw)


# three HDP sites with their KÜS twins (different spellings) and two unrelated KÜS rows
HDP = [
    hdp_site("Dresden", "Hauptstraße", "5", "01067", "Dresden", 51.0600, 13.7400),
    hdp_site("Lage", "Lindenallee", "15a", "32791", "Lage", 51.9900, 8.7900),
    hdp_site("Berlin", "Königsweg", "12", "10115", "Berlin", 52.5320, 13.3850),
]
KUES = [
    source_row("kues", "KUES-100", name="KÜS Dresden", street="Hauptstr. 5", zip="01067", city="Dresden", lat="51.0601", lng="13.7401", kues_licensee="I1122"),
    source_row("kues", "KUES-101", name="KÜS Lage", street="Lindenallee 15 A", zip="32791", city="Lage", lat="51.9901", lng="8.7901", kues_licensee="I1122"),
    source_row("kues", "KUES-102", name="KÜS Berlin", street="Koenigsweg 12", zip="10115", city="Berlin", lat="52.5321", lng="13.3851", kues_licensee="I1122"),
    source_row("kues", "KUES-200", name="KÜS Hamburg", street="Hafenstr. 9", zip="20095", city="Hamburg", lat="53.55", lng="10.0", kues_licensee="K2"),
    source_row("kues", "KUES-201", name="KÜS München", street="Gewerbering 2", zip="80331", city="München", lat="48.137", lng="11.575", kues_licensee="K3"),
]


def payload_of(sites):
    return {"result": {"success": True, "locations": sites}}


def make_hdp(root, sites=HDP, at=T - timedelta(hours=1), payload=None, client=None):
    return hdp_run.run("full", confirm_full_run=True, root=root, now=at, client=client or SimulatedHdp(payload or payload_of(sites)))


def world(root, kues=KUES, hdp=HDP, **kw):
    all_sources(root, extra={"kues": kues})
    if hdp is not None:
        make_hdp(root, hdp, **kw)


def go(root, mode="dev", at=T, **kw):
    kw.setdefault("check_volume", False)
    return master_run.run(mode, root=root, started_at=at, centroids=CENTROIDS, geocoder=geocoder(root), **kw)


def failed(res):
    return [c["check"] for c in res.summary["checks"] if c["severity"] == "hard" and not c["ok"]]


def kues_ids(res):
    return {i for s in res.master["source_ids"] for i in s.split("|") if i.startswith("KUES-")}


def removed_file(res):
    return pd.read_csv(res.out_dir / hr.REMOVED_FILE, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")


# ---- the pass case ------------------------------------------------------------------------------------------------------
def test_pass_when_every_hdp_site_matches_one_kues_row_and_n_equals_r(tmp_path):
    world(tmp_path)
    res = go(tmp_path)
    assert res.status == "PASS", failed(res)
    h = res.summary["hdp_own_sites"]
    assert (h["n_found"], h["r_removed"], h["status"]) == (3, 3, "PASS")
    assert kues_ids(res) == {"KUES-200", "KUES-201"}  # our three sites are gone, the two competitors stay
    assert {m["hdp_id"]: m["kues"] for m in h["matches"]} == {"Dresden": ["KUES-100"], "Lage": ["KUES-101"], "Berlin": ["KUES-102"]}
    assert h["fallback_matches"] == [] and {m["method"] for m in h["matches"]} == {"address"}
    assert any(c["check"].startswith("HDP own sites: rows removed (R) equal") and c["ok"] and c["detail"] == "N = 3, R = 3" for c in res.summary["checks"])


def test_every_removal_is_written_to_an_audit_file_with_hdp_id_and_method(tmp_path):
    world(tmp_path)
    res = go(tmp_path)
    f = removed_file(res)
    assert list(f.columns) == hr.REMOVED_COLUMNS and len(f) == 3
    row = f.set_index("hdp_id").loc["Lage"]
    assert (row["kues_location_id"], row["match_method"], row["hdp_location_id"]) == ("KUES-101", "address", "HDP-Lage")
    assert row["kues_street"] == "Lindenallee 15 A" and row["kues_licensee"] == "I1122"


def test_the_summary_has_an_hdp_section_and_the_row_conservation_counts_the_removals(tmp_path):
    world(tmp_path)
    res = go(tmp_path)
    md = (res.out_dir / "master_summary.md").read_text(encoding="utf-8")
    assert "## HDP own-site removal" in md and "N = 3 HDP site(s) found, R = 3 row(s) removed" in md
    cons = next(c for c in res.summary["checks"] if c["check"].startswith("row conservation"))
    assert cons["ok"] and "3 HDP own sites removed" in cons["detail"]
    assert res.summary["rows_in_total"] == len(res.master) + res.summary["merged_away"] + 3
    assert "KUES-100" not in (res.out_dir / "pruefstellen_extra_kues_2026-10.csv").read_text(encoding="utf-8-sig")


def test_hdp_rows_never_pull_a_competitor_into_a_merge_or_hide_behind_one(tmp_path):
    # a DEKRA row at the same address as our own site stays a competitor row; only the KÜS row goes
    dekra = [source_row("dekra", "DEKRA-1", name="DEKRA Dresden", street="Hauptstr. 5", zip="01067", city="Dresden", lat="51.06", lng="13.74")]
    all_sources(tmp_path, extra={"kues": KUES, "dekra": dekra})
    make_hdp(tmp_path)
    res = go(tmp_path)
    assert res.status == "PASS", failed(res)
    dr = next(r for r in res.master.itertuples() if "DEKRA-1" in r.source_ids)
    assert dr.source_ids == "DEKRA-1" and dr.listed_by == "dekra"


# ---- the fallback -------------------------------------------------------------------------------------------------------
def test_a_coordinate_fallback_match_is_used_and_logged_explicitly(tmp_path):
    kues = [KUES[0], KUES[1], source_row("kues", "KUES-102", name="KÜS Berlin", street="Königsweg 10-12", zip="10115", city="Berlin",
                                         lat="52.53205", lng="13.38505", kues_licensee="I1122"), *KUES[3:]]
    world(tmp_path, kues=kues)
    res = go(tmp_path)
    assert res.status == "PASS", failed(res)
    h = res.summary["hdp_own_sites"]
    assert [f["hdp_id"] for f in h["fallback_matches"]] == ["Berlin"] and h["fallback_matches"][0]["kues_location_id"] == "KUES-102"
    assert 0 < h["fallback_matches"][0]["distance_m"] <= hr.MATCH_RADIUS_M
    assert removed_file(res).set_index("hdp_id").loc["Berlin", "match_method"] == "coordinates"
    assert "Coordinate fallback matches (1): Berlin -> KUES-102" in (res.out_dir / "master_summary.md").read_text(encoding="utf-8")


def test_the_fallback_is_not_used_beyond_the_radius_or_in_another_plz(tmp_path):
    far = source_row("kues", "KUES-102", name="KÜS Berlin", street="Anderer Weg 1", zip="10115", city="Berlin", lat="52.5345", lng="13.3850", kues_licensee="I1122")
    other_plz = source_row("kues", "KUES-103", name="KÜS Berlin 2", street="Anderer Weg 2", zip="20095", city="Hamburg", lat="52.5320", lng="13.3850")
    world(tmp_path, kues=[KUES[0], KUES[1], far, other_plz])
    res = go(tmp_path)
    assert [u["hdp_id"] for u in res.summary["hdp_own_sites"]["unmatched"]] == ["Berlin"]


# ---- unresolved sites are REVIEW items, not a failed build (Nikos, 2026-10-07) ---------------------------------------------
def reasons_of(res, location_id):
    row = next(r for r in res.master.itertuples() if location_id in r.source_ids.split("|"))
    return row.needs_review, row.needs_review_reason, row.doubt_flags


def test_an_hdp_site_without_a_kues_match_is_a_review_item_and_the_build_still_passes(tmp_path):
    world(tmp_path, kues=[k for k in KUES if k["location_id"] != "KUES-102"] + [
        source_row("kues", "KUES-300", name="KÜS nah", street="Nebenstr. 1", zip="10115", city="Berlin", lat="52.5350", lng="13.3850"),
        source_row("kues", "KUES-301", name="KÜS naher", street="Nebenstr. 2", zip="10117", city="Berlin", lat="52.5360", lng="13.3850"),
        source_row("kues", "KUES-302", name="KÜS fern", street="Nebenstr. 3", zip="10119", city="Berlin", lat="52.5400", lng="13.3850"),
        source_row("kues", "KUES-303", name="KÜS ferner", street="Nebenstr. 4", zip="10119", city="Berlin", lat="52.5800", lng="13.3850")])
    res = go(tmp_path)
    assert res.status == "PASS", failed(res)
    bad = {c["check"]: c for c in res.summary["checks"] if not c["ok"]}
    assert set(bad) == {"HDP own sites: every HDP site matches exactly one KÜS row", "HDP own sites: rows removed (R) equal HDP sites found (N)"}
    assert {c["severity"] for c in bad.values()} == {"review"}  # still reported, never hard
    h = res.summary["hdp_own_sites"]
    assert (h["n_found"], h["r_removed"], h["status"], h["sites_to_review"]) == (3, 2, "REVIEW", ["Berlin"])
    (u,) = h["unmatched"]
    assert u["hdp_id"] == "Berlin" and [k["location_id"] for k in u["nearest_kues"]] == ["KUES-300", "KUES-301", "KUES-302"]
    assert [k["distance_m"] for k in u["nearest_kues"]] == sorted(k["distance_m"] for k in u["nearest_kues"]) and u["nearest_kues"][0]["distance_m"] > 0
    md = (res.out_dir / "master_summary.md").read_text(encoding="utf-8")
    assert "**UNMATCHED** Berlin" in md and "KUES-300" in md and "N = 3, R = 2" in md and "NOT failed" in md.replace("The build is NOT failed", "NOT failed")
    assert md.startswith("# Master summary 2026-10 (dev): PASS")
    # the HDP site has no master row, so it is listed in hdp_review.csv; the neighbours are other addresses and stay unflagged
    rv = pd.read_csv(res.out_dir / hr.REVIEW_FILE, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert list(rv.columns) == hr.REVIEW_COLUMNS and set(rv["hdp_id"]) == {"Berlin"} and set(rv["problem"]) == {"no KÜS row found"}
    assert list(rv["kues_location_id"]) == ["KUES-300", "KUES-301", "KUES-302"] and set(rv["kues_row_flagged_in_master"]) == {"False"}
    assert all("hdp_match_unresolved" not in str(r) for r in res.master["doubt_flags"])


def test_a_build_with_unresolved_hdp_sites_is_a_normal_verified_file_also_officially(tmp_path):
    world(tmp_path, kues=KUES[:2] + KUES[3:])  # Berlin has no KÜS row
    res = go(tmp_path, mode="official", confirm=True)
    assert res.status == "PASS" and [p.name for p in res.out_dir.glob("pruefstellen_master*.csv")] == ["pruefstellen_master_2026-10.csv"]
    assert set(removed_file(res)["hdp_id"]) == {"Dresden", "Lage"}  # every removal stays auditable


def test_an_hdp_site_matching_two_kues_rows_flags_both_rows_in_the_master_and_removes_neither(tmp_path):
    twin = source_row("kues", "KUES-103", name="KÜS Dresden Zweit", street="Hauptstraße 5", zip="01067", city="Dresden", lat="51.0602", lng="13.7402", kues_licensee="K9")
    world(tmp_path, kues=KUES + [twin])
    res = go(tmp_path)
    assert res.status == "PASS", failed(res)
    (m,) = res.summary["hdp_own_sites"]["multiple"]
    assert m["hdp_id"] == "Dresden" and sorted(k["location_id"] for k in m["kues"]) == ["KUES-100", "KUES-103"]
    assert {"KUES-100", "KUES-103"} <= kues_ids(res)  # an ambiguous site removes nothing: a competitor may be among the rows
    assert res.summary["hdp_own_sites"]["r_removed"] == 2 and res.summary["hdp_own_sites"]["kues_rows_flagged_for_review"] == ["KUES-100", "KUES-103"]
    for lid in ("KUES-100", "KUES-103"):
        flag, reason, doubt = reasons_of(res, lid)
        assert flag == "False" and reason == ""  # a doubt flag, never needs_review
        assert "hdp_match_unresolved:Dresden" in doubt and "hdp_match_unresolved" in doubt.split("; ")
    assert reasons_of(res, "KUES-200")[0] == "False"  # the other competitors are untouched
    rv = pd.read_csv(res.out_dir / hr.REVIEW_FILE, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert set(rv["kues_location_id"]) == {"KUES-100", "KUES-103"} and set(rv["problem"]) == {"several KÜS rows match"}
    assert set(rv["kues_row_flagged_in_master"]) == {"True"} and all(rv["master_id"].str.startswith("PS-"))
    assert res.summary["doubt_flags_by_reason"]["hdp_match_unresolved"]["rows"] == 2 and res.summary["needs_review_rows"] == 0


def test_a_kues_row_claimed_by_two_hdp_sites_is_flagged_not_removed_and_the_build_passes(tmp_path):
    second = hdp_site("Dresden-Alias", "Hauptstr.", "5", "01067", "Dresden", 51.0600, 13.7400)
    world(tmp_path, hdp=HDP + [second])
    res = go(tmp_path)
    assert res.status == "PASS", failed(res)
    assert "HDP own sites: no KÜS row is claimed by two HDP sites" in [c["check"] for c in res.summary["checks"] if not c["ok"]]
    (c,) = res.summary["hdp_own_sites"]["claimed_twice"]
    assert c["kues_location_id"] == "KUES-100" and sorted(c["hdp_ids"]) == ["Dresden", "Dresden-Alias"]
    assert "KUES-100" in kues_ids(res) and res.summary["hdp_own_sites"]["r_removed"] == 2
    flag, reason, doubt = reasons_of(res, "KUES-100")
    assert flag == "False" and reason == "" and "hdp_match_unresolved:Dresden|Dresden-Alias" in doubt
    assert "KÜS ROW CLAIMED TWICE" in (res.out_dir / "master_summary.md").read_text(encoding="utf-8")


def test_n_not_equal_r_is_reported_as_a_review_check_even_if_the_matching_looks_fine(tmp_path, monkeypatch):
    world(tmp_path)
    real = hr.split_recs

    def lossy(recs, matching):  # a removal that drops one row too few is caught by the independent count
        keep, removed = real(recs, matching)
        return keep + removed[:1], removed[1:]

    monkeypatch.setattr(hr, "split_recs", lossy)
    res = go(tmp_path)
    c = next(c for c in res.summary["checks"] if c["check"].startswith("HDP own sites: rows removed"))
    assert not c["ok"] and c["severity"] == "review" and c["detail"] == "N = 3, R = 2" and res.status == "PASS"


def test_a_removed_row_that_is_still_in_the_master_still_fails_the_build():
    assert not hr.final_check([{"location_id": "KUES-1"}], ["KUES-1", "KUES-2"])["ok"]
    assert hr.final_check([{"location_id": "KUES-1"}], ["KUES-2"])["ok"] and hr.final_check([{"location_id": "KUES-1"}], [])["severity"] == "hard"

def test_a_removed_row_that_is_still_in_the_master_fails(tmp_path):
    inp = hr.HdpInput("x", "full", "f.csv", 1, "2026-10-07T10:00:00", 0.1, [])
    assert not hr.final_check([{"location_id": "KUES-1"}], ["KUES-1", "KUES-2"])["ok"]
    assert hr.final_check([{"location_id": "KUES-1"}], ["KUES-2"])["ok"] and inp.n_found == 1


# ---- the HDP input is mandatory -------------------------------------------------------------------------------------------
def test_the_scraper_returning_zero_sites_fails_the_aggregator_and_removes_nothing_silently(tmp_path):
    all_sources(tmp_path, extra={"kues": KUES})
    res0 = make_hdp(tmp_path, payload={"result": {"success": True, "locations": []}})
    assert res0.outcome.startswith("FAIL")
    res = go(tmp_path)
    assert res.status == "FAIL" and res.master is None
    assert "did not pass" in res.summary["error"] and "HDP own-site input" in res.summary["error"]
    assert "HDP own-site input" in (res.out_dir / "master_summary.md").read_text(encoding="utf-8")


def test_a_summary_that_claims_zero_sites_is_refused_even_if_it_says_pass(tmp_path):
    world(tmp_path)
    run = next((tmp_path / "1_scrapers" / "hdp" / "output").iterdir())
    s = json.loads((run / "run_summary.json").read_text(encoding="utf-8"))
    s["n_sites"] = 0
    (run / "run_summary.json").write_text(json.dumps(s), encoding="utf-8")
    with pytest.raises(hr.HdpInputError, match="0 sites"):
        hr.load_hdp_input(tmp_path, T)


def test_a_failed_hdp_scraper_run_fails_the_aggregator(tmp_path):
    all_sources(tmp_path, extra={"kues": KUES})
    make_hdp(tmp_path, client=SimulatedHdp(error=fetch.FetchError("failed after 3 retries (HTTP 503)")))
    res = go(tmp_path)
    assert res.status == "FAIL" and res.master is None and "did not pass" in res.summary["error"] and "after 3 retries" in res.summary["error"]


def test_a_newer_failed_hdp_run_is_never_skipped_in_favour_of_an_older_passing_one(tmp_path):
    world(tmp_path, at=T - timedelta(hours=3))
    make_hdp(tmp_path, at=T - timedelta(hours=1), payload={"result": {"success": False}})
    res = go(tmp_path)
    assert res.status == "FAIL" and res.master is None and "did not pass" in res.summary["error"]
    make_hdp(tmp_path, at=T - timedelta(minutes=5))  # a fresh passing run makes it work again
    assert go(tmp_path).status == "PASS"


def test_no_hdp_run_at_all_fails_the_aggregator_and_never_falls_back_to_removing_nothing(tmp_path):
    world(tmp_path, hdp=None)
    res = go(tmp_path)
    assert res.status == "FAIL" and res.master is None and "no HDP run" in res.summary["error"]
    off = go(tmp_path, hdp_removal_enabled=False)  # only the tests' switch can skip it; the command line has none
    assert off.status == "PASS" and off.summary["hdp_own_sites"]["enabled"] is False
    with pytest.raises(SystemExit):  # the command line has no way to switch the removal off
        master_run.main(["dev", "--no-hdp"])


def test_an_hdp_run_older_than_the_limit_fails_the_aggregator(tmp_path):
    world(tmp_path, at=T - timedelta(days=hr.MAX_HDP_AGE_DAYS + 1))
    res = go(tmp_path)
    assert res.status == "FAIL" and "days old" in res.summary["error"]


def test_an_hdp_rebuild_does_not_make_old_data_young(tmp_path):
    all_sources(tmp_path, extra={"kues": KUES})
    old = make_hdp(tmp_path, at=T - timedelta(days=30))
    hdp_run.run("rebuild", root=tmp_path, now=T - timedelta(hours=1), source_runs=[old.run_dir])
    assert go(tmp_path).status == "FAIL"


def test_a_site_file_that_does_not_match_the_summary_is_refused(tmp_path):
    world(tmp_path)
    run = next((tmp_path / "1_scrapers" / "hdp" / "output").iterdir())
    f = next(run.glob("hdp_sites_*.csv"))
    df = pd.read_csv(f, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.iloc[:-1].to_csv(f, sep=";", index=False, encoding="utf-8-sig")
    with pytest.raises(hr.HdpInputError, match="file has 2 row"):
        hr.load_hdp_input(tmp_path, T)


# ---- the normalisation of the address match -------------------------------------------------------------------------------
def rec(lid, street, zip_, lat=None, lng=None):
    from core import normalise as nm
    return {"slug": "kues", "location_id": lid, "street": street, "zip": zip_, "city": "X", "lat": lat, "lng": lng,
            "akey": nm.address_key(street, zip_) if nm.has_street(street) and zip_ else None, "name": "", "src": {}}


def site(sid="S", street="Am Hafen 5", zip_="12345", lat=50.0, lng=8.0):
    return {"id": sid, "location_id": f"HDP-{sid}", "street": street, "zip": zip_, "city": "X", "lat": lat, "lng": lng}


@pytest.mark.parametrize("hdp_street, kues_street", [
    ("Hauptstraße 5", "Hauptstr. 5"), ("Hauptstrasse 5", "HAUPTSTR 5"), ("Hauptstr. 5", "Hauptstraße 5"),
    ("Handwerkstraße 15a", "Handwerkstr. 15 a"), ("Handwerkstraße 15a", "Handwerkstraße 15A"),
    ("Königsweg 12", "Koenigsweg 12"), ("Müllerstr. 3", "Mueller-Str. 3"), ("Große Gasse 7", "Grosse Gasse 7"),
    ("Sankt-Peter-Weg 2", "St. Peter Weg 2"),
])
def test_street_spellings_that_are_the_same_house_match_by_address(hdp_street, kues_street):
    m = hr.match_sites([site(street=hdp_street)], [rec("KUES-1", kues_street, "12345")])
    assert m.sites[0]["status"] == "matched" and m.sites[0]["method"] == "address"


@pytest.mark.parametrize("kues_street, zip_", [("Hauptstr. 6", "12345"), ("Hauptstr. 5b", "12345"), ("Nebenstr. 5", "12345"), ("Hauptstr. 5", "54321")])
def test_a_different_house_street_or_postcode_is_not_an_address_match(kues_street, zip_):
    m = hr.match_sites([site(street="Hauptstr. 5")], [rec("KUES-1", kues_street, zip_)])
    assert m.sites[0]["status"] == "unmatched" and m.removed_ids == set()


def test_a_site_without_a_house_number_never_matches_by_address():
    m = hr.match_sites([site(street="Hauptstr.")], [rec("KUES-1", "Hauptstr.", "12345")])
    assert m.sites[0]["status"] == "unmatched"


def test_the_fallback_needs_the_same_plz_and_150_metres():
    near = rec("KUES-1", "Anderer Weg 1", "12345", 50.0005, 8.0)  # ~56 m
    just_out = rec("KUES-2", "Anderer Weg 2", "12345", 50.0014, 8.0)  # ~156 m
    m = hr.match_sites([site()], [near, just_out])
    assert m.sites[0]["status"] == "matched" and m.sites[0]["method"] == "coordinates" and m.sites[0]["kues"][0]["location_id"] == "KUES-1"
    assert hr.match_sites([site()], [just_out]).sites[0]["status"] == "unmatched"
    assert hr.match_sites([site()], [rec("KUES-3", "Anderer Weg 3", "99999", 50.0, 8.0)]).sites[0]["status"] == "unmatched"


def test_the_address_match_wins_over_a_closer_coordinate_neighbour():
    other = rec("KUES-2", "Anderer Weg 2", "12345", 50.0, 8.0)  # 0 m but a different address
    right = rec("KUES-1", "Am Hafen 5", "12345", 50.0009, 8.0)
    m = hr.match_sites([site()], [other, right])
    assert m.sites[0]["kues"][0]["location_id"] == "KUES-1" and m.sites[0]["method"] == "address"


def test_two_fallback_candidates_inside_the_radius_are_several_matches():
    m = hr.match_sites([site()], [rec("KUES-1", "A 1", "12345", 50.0003, 8.0), rec("KUES-2", "B 2", "12345", 50.0006, 8.0)])
    assert m.sites[0]["status"] == "multiple" and m.removed_ids == set()


# ---- Nikos's decisions on the 2026-10 review items (Dortmund pinned, KUES-58908 kept, Fürth not listed by KÜS) ---------------
DORTMUND = hdp_site("Dortmund", "Wambeler Hellweg", "52", "44143", "Dortmund", 51.5130, 7.5050)
FUERTH = hdp_site("Fürth", "Sigmundstraße", "113", "90431", "Nürnberg", 49.4400, 11.0300)
DORTMUND_KUES = [
    source_row("kues", "KUES-30052", name="KÜS Dortmund", street="Wambeler Hellweg 50-52", zip="44143", city="Dortmund",
               lat="51.51302", lng="7.50502", kues_licensee="I1122"),
    source_row("kues", "KUES-58908", name="Has4 Technologies", street="Wambeler Hellweg 50-52", zip="44143", city="Dortmund",
               lat="51.51301", lng="7.50501", kues_licensee="I1122"),
]


def test_the_decided_cases_leave_no_hdp_review_item(tmp_path):
    world(tmp_path, kues=KUES + DORTMUND_KUES, hdp=HDP + [DORTMUND, FUERTH])
    res = go(tmp_path)
    assert res.status == "PASS", failed(res)
    assert not [c["check"] for c in res.summary["checks"] if c["check"].startswith("HDP own sites") and not c["ok"]]
    h = res.summary["hdp_own_sites"]
    assert (h["n_found"], h["r_removed"], h["status"], h["sites_to_review"]) == (5, 4, "PASS", [])
    assert [k["hdp_id"] for k in h["known_exceptions"]] == ["Fürth"]
    assert {m["hdp_id"]: (m["kues"], m["method"]) for m in h["matches"]}["Dortmund"] == (["KUES-30052"], "pinned")
    assert "KUES-30052" not in kues_ids(res) and "KUES-58908" in kues_ids(res)
    flag, reason, doubt = reasons_of(res, "KUES-58908")
    assert flag == "False" and reason == "" and "hdp_own_site_kept:Dortmund" in doubt and "hdp_own_site_kept" in doubt.split("; ")
    assert "hdp_match_unresolved" not in doubt
    md = (res.out_dir / "master_summary.md").read_text(encoding="utf-8")
    assert "Known exception Fürth" in md and "N = 5 minus 1 known exception(s), R = 4" in str(res.summary["checks"])


def test_the_kept_row_is_never_imported_to_hubspot(tmp_path):
    from core import hubspot_export as hx
    world(tmp_path, kues=KUES + DORTMUND_KUES, hdp=HDP + [DORTMUND, FUERTH])
    res = go(tmp_path)
    rows, excluded = hx.exportable(res.master)
    kept_mid = next(r.master_id for r in res.master.itertuples() if "KUES-58908" in r.source_ids.split("|"))
    assert kept_mid not in set(rows["master_id"])
    assert excluded.set_index("master_id").loc[kept_mid, "reason"] == hx.EXCLUDE_REASONS[hx.KEPT_FLAG_PREFIX]


def test_a_kept_row_is_never_a_match_target_even_if_the_pinned_row_is_gone():
    dortmund = site("Dortmund", "Wambeler Hellweg 52", "44143", 51.5130, 7.5050)
    kept = rec("KUES-58908", "Wambeler Hellweg 52", "44143", 51.5130, 7.5050)
    m = hr.match_sites([dortmund], [kept])
    assert m.sites[0]["status"] == "unmatched" and m.removed_ids == set()  # back to a review item, nothing removed
    pinned = rec("KUES-30052", "Wambeler Hellweg 50-52", "44143", 51.5130, 7.5050)
    m = hr.match_sites([dortmund], [kept, pinned])
    assert m.sites[0]["method"] == "pinned" and m.removed_ids == {"KUES-30052"}
    assert hr.kept_rows([kept, pinned]) == {"KUES-58908": "Dortmund"} and hr.kept_rows([pinned]) == {}


def test_a_known_exception_is_not_matched_even_if_a_kues_row_is_near():
    fuerth = site("Fürth", "Sigmundstraße 113", "90431", 49.44, 11.03)
    m = hr.match_sites([fuerth], [rec("KUES-1", "Sigmundstraße 113", "90431", 49.44, 11.03)])
    assert m.sites[0]["status"] == "known_exception" and m.removed_ids == set() and m.known_exceptions[0]["hdp_id"] == "Fürth"


def test_the_decisions_can_be_switched_off_for_other_tests():
    dortmund = site("Dortmund", "Wambeler Hellweg 52", "44143", 51.5130, 7.5050)
    both = [rec("KUES-30052", "Wambeler Hellweg 52", "44143"), rec("KUES-58908", "Wambeler Hellweg 52", "44143")]
    assert hr.match_sites([dortmund], both, pinned={}, kept={}).sites[0]["status"] == "multiple"
