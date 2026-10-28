"""KÜS runs end to end against the offline fake API: PASS, the negative tests, modes, labels and warnings."""

import json
import shutil
from datetime import datetime, timedelta

import pandas as pd
import pytest

from core import paths
from core import radius_cover as rc
from kues import fetch, run, simulate

FIXTURE = paths.fixtures_dir("kues") / "response_muenchen_80636_2026-10-05.json"
FAST = rc.CoverConfig(cover_km=30)  # ~240 instead of ~350 queries: for the tests that are not about the default design
START = datetime(2026, 10, 5, 12, 0)


@pytest.fixture(scope="module")
def sites(centroids):
    return simulate.simulated_sites(centroids, n_total=1500, seed=0)


@pytest.fixture(scope="module")
def small_sites(centroids):
    return simulate.simulated_sites(centroids, n_total=500, per_cluster=20, seed=3)


@pytest.fixture(scope="module")
def passing(centroids, sites, tmp_path_factory):
    root = tmp_path_factory.mktemp("kues_pass")
    return run.run(mode="simulated", root=root, started_at=START, centroids=centroids, sites=sites, verbose=False, save_raw=True), root


def simulated(centroids, sites, root, **kwargs):
    kwargs.setdefault("cfg", FAST)
    api_kwargs = kwargs.pop("api_kwargs", {})
    api_kwargs.setdefault("radius_km", kwargs["cfg"].radius_km)  # a test may make the fake server's radius differ from the expected one
    api = simulate.FakeKuesApi(sites, centroids, **api_kwargs)
    return run.run(mode=kwargs.pop("mode", "simulated"), root=root, started_at=kwargs.pop("started_at", START), centroids=centroids,
                   api=api, verbose=False, **kwargs), api


# ---- the simulated run must PASS and find everything

def test_simulated_run_passes_and_finds_every_site(passing, sites):
    result, root = passing
    s = result.summary
    assert s["status"] == "PASS" and s["status_reasons"] == [] and not s["aborted"]
    assert s["rows_total"] == len(sites) == 1500  # 100 % of the sites
    assert set(result.frame["source_id"]) == set(sites["id"])
    assert all(s["coverage"][k]["ok"] for k in ("rule_a", "rule_b", "rule_c", "rule_d", "rule_c_technik", "rule_d_technik"))
    assert s["cover_config"]["radius_km"] == 100 and s["cover_config"]["cover_km"] == 50  # the production default
    assert s["plan"]["not_queried"] == 0 and 80 < s["plan"]["queries"] < 130  # live full run: 91
    assert s["requests_made"] == 2 * s["plan"]["queries"]  # both lists


def test_simulated_run_writes_one_folder_with_the_expected_files(passing):
    result, root = passing
    folder = result.run_folder
    assert folder.parent == root / "1_scrapers" / "kues" / "test_output" and folder.name == "2026-10-05_1200_simulated"
    names = {p.name for p in folder.iterdir()}
    assert {"kues_all_2026-10-05.csv", "kues_pruefstellen_2026-10-05.csv", "kues_pruefstellen_2026-10-05.xlsx", "kues_all_2026-10-05.xlsx",
            "query_log.csv", "run_summary.json", "run_summary.md", "raw.jsonl"} <= names
    summary = json.loads((folder / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS" and summary["in_scope"] == result.summary["in_scope"]
    log = pd.read_csv(folder / "query_log.csv")
    assert list(log.columns) == run.LOG_COLUMNS and len(log) == result.summary["requests_made"]
    lines = (folder / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(log) and set(json.loads(lines[0])) == {"file", "response"}


def test_exports_filter_by_in_scope_and_keep_every_record(passing):
    result, _ = passing
    folder = result.run_folder
    all_rows = pd.read_csv(folder / "kues_all_2026-10-05.csv", sep=";", encoding="utf-8-sig", dtype=str)
    in_scope = pd.read_csv(folder / "kues_pruefstellen_2026-10-05.csv", sep=";", encoding="utf-8-sig", dtype=str)
    assert len(all_rows) == 1500 and len(in_scope) == result.summary["in_scope"] <= 1500  # the simulated data holds no hand-checked (excluded) record
    # in scope: every vorort = 1 record (psp = No) and the vorort = 0 records (psp = Yes), except the hand-checked ones
    assert set(in_scope["psp"]) == {"Yes", "No"} and (in_scope["psp"] == "Yes").sum() == result.summary["in_scope_psp"]["Yes"]
    assert set(in_scope["site_type"]) <= {"pruefstelle", "partner_office", "unknown"} and "pruefstelle" in set(in_scope["site_type"])
    assert (in_scope.loc[in_scope["psp"] == "No", "site_type"] == "pruefstelle").all()
    odd = in_scope[in_scope["site_type"] == "unknown"]  # vorort = 0 with a Prüfstelle name
    assert odd["needs_review"].eq("True").all() and odd["doubt_flags"].str.contains("name_pruefstelle_vorort0").all()
    assert set(all_rows["site_type"]) <= {"pruefstelle", "partner_office", "unknown"} and {"pruefstelle", "partner_office"} <= set(all_rows["site_type"])
    assert set(all_rows.loc[all_rows["in_scope"] == "False", "psp"].dropna()) <= {"Yes"}  # only the hand-checked records stay out
    assert all_rows["location_id"].str.startswith("KUES-").all()


def test_summary_counts_are_consistent(passing):
    s = passing[0].summary
    assert sum(s["site_types"].values()) == s["rows_total"] and s["kues_list"]["both"] > 0 and s["kues_list"]["partnersuche"] > 0
    assert s["plz_only"] > 0 and s["needs_review"] > 0 and sum(s["in_scope_by_state"].values()) == s["in_scope"]
    assert s["is_hdp_site"] is None  # no 4_reference/hdp/hdp_sites.csv
    assert not s["warnings"].get("content_conflicts")


# ---- negative tests: the coverage rules must catch a broken server

def test_a_capped_api_fails_rule_c_or_d(centroids, small_sites, tmp_path):
    result, _ = simulated(centroids, small_sites, tmp_path, api_kwargs={"cap": 12})
    cov = result.summary["coverage"]
    assert result.summary["status"] == "FAIL"
    assert not cov["rule_c"]["ok"] or not cov["rule_d"]["ok"]
    assert "rule D cap detector failed" in result.summary["status_reasons"] or "rule C appearance consistency failed" in result.summary["status_reasons"]
    assert any(p.name.endswith("_unverified.csv") for p in result.run_folder.iterdir())


def test_a_site_missing_from_one_response_fails_rule_c(centroids, small_sites, tmp_path):
    clean = simulate.FakeKuesApi(small_sites, centroids, radius_km=50)
    plan = rc.plan_initial(centroids, FAST)["zip"].tolist()
    finder = clean.finders[fetch.LIST_KUES]
    hits: dict = {}
    for z in plan:
        p = centroids.set_index("zip").loc[z]
        centre, found = finder.search(z, p["lat"], p["lng"])
        if len(found) < 3:  # an emptied response has no centre to check against (that case is a warning, see the empty-response test)
            continue
        for site_id, dist in found:
            hits.setdefault(site_id, []).append((z, dist))
    # a site two planned centres return, the first of them within 45 km: dropping it there must be noticed
    site_id, zip_ = next((s, h[0][0]) for s, h in hits.items() if len(h) >= 2 and h[0][1] < 45 and small_sites.set_index("id").loc[s, "keinestrasse"] == "0")
    result, _ = simulated(centroids, small_sites, tmp_path, api_kwargs={"drop": lambda z, i: z == zip_ and i == site_id})
    assert result.summary["status"] == "FAIL"
    misses = result.summary["coverage"]["rule_c"]["misses"]
    assert any(m["query"] == zip_ and m["site_id"] == site_id for m in misses)
    assert any(p.name.endswith("_unverified.csv") for p in result.run_folder.iterdir())  # labelled, not mistaken for a verified crawl


def test_a_shrunken_radius_fails_and_warns(centroids, small_sites, tmp_path):
    result, _ = simulated(centroids, small_sites, tmp_path, api_kwargs={"radius_km": 30})
    assert result.summary["status"] == "FAIL" and not result.summary["coverage"]["rule_c"]["ok"]
    assert result.summary["warnings"]["radius_smaller_than_expected"]


def test_a_larger_radius_is_a_warning(centroids, small_sites, tmp_path):
    result, _ = simulated(centroids, small_sites, tmp_path, api_kwargs={"radius_km": 60})
    assert result.summary["warnings"]["radius_larger_than_expected"]


def test_server_centres_far_from_the_geonames_centroid_are_logged(centroids, small_sites, tmp_path):
    result, _ = simulated(centroids, small_sites, tmp_path, api_kwargs={"centre_offset_km": 25}, fetch_technik=False)
    far = result.summary["warnings"]["centre_far_from_geonames"]
    assert far and all(f["centre_offset_km"] > run.CENTRE_OUTLIER_KM for f in far)


def test_an_unknown_plz_answers_empty_and_is_covered_by_the_fill_in(centroids, small_sites, tmp_path):
    first = rc.plan_initial(centroids, FAST)["zip"].iloc[100]
    result, _ = simulated(centroids, small_sites, tmp_path, api_kwargs={"unknown_zips": {first}}, fetch_technik=False)
    assert any(w["zip"] == first for w in result.summary["warnings"]["empty_responses"])
    assert result.summary["status"] == "PASS" and result.summary["plan"]["fill_a"] >= 1


# ---- unattended-run failures

def test_a_request_limit_aborts_the_run_and_labels_the_exports(centroids, small_sites, tmp_path):
    result, _ = simulated(centroids, small_sites, tmp_path, api_kwargs={"max_requests": 40}, save_raw=True)
    s = result.summary
    assert s["status"] == "FAIL" and "RequestLimitReached" in s["aborted"] and s["plan"]["not_queried"] > 0
    assert any(p.name.endswith("_grid-incomplete.csv") for p in result.run_folder.iterdir())
    assert (result.run_folder / "raw.jsonl").exists() and len((result.run_folder / "raw.jsonl").read_text().splitlines()) == 40


def test_a_changed_response_shape_stops_the_run_but_keeps_what_was_collected(centroids, small_sites, tmp_path):
    api = simulate.FakeKuesApi(small_sites, centroids)
    real_get = api.get

    def broken(list_name, plz, radius_km=None):
        if api.requests_made >= 10:
            fetch.check_shape({"error": "changed"})
        return real_get(list_name, plz, radius_km)

    api.get = broken
    result = run.run(mode="simulated", root=tmp_path, started_at=START, centroids=centroids, api=api, cfg=FAST, verbose=False)
    assert result.summary["status"] == "FAIL" and "unexpected response shape" in result.summary["aborted"]
    assert result.summary["rows_total"] > 0
    assert result.summary["plan"]["not_queried"] > 0


def test_an_unexpected_exception_still_writes_the_summary_and_labelled_exports(centroids, small_sites, tmp_path):
    api = simulate.FakeKuesApi(small_sites, centroids)
    real_get = api.get

    def explode(list_name, plz, radius_km=None):
        if api.requests_made >= 12:
            raise RuntimeError("boom")
        return real_get(list_name, plz, radius_km)

    api.get = explode
    result = run.run(mode="simulated", root=tmp_path, started_at=START, centroids=centroids, api=api, cfg=FAST, verbose=False)
    assert result.summary["status"] == "FAIL" and result.summary["aborted"] == "unexpected RuntimeError: boom"
    assert (result.run_folder / "run_summary.json").exists()
    assert any(p.name.endswith("_grid-incomplete.csv") for p in result.run_folder.iterdir())


def test_a_run_that_fails_on_the_first_request_leaves_a_summary_and_no_exports(centroids, small_sites, tmp_path):
    result, _ = simulated(centroids, small_sites, tmp_path, api_kwargs={"max_requests": 0})
    s = result.summary
    assert s["status"] == "FAIL" and s["rows_total"] == 0 and s["plan"]["not_queried"] == s["plan"]["planned"] > 0
    assert {p.name for p in result.run_folder.iterdir()} == {"query_log.csv", "run_summary.json", "run_summary.md"}


# ---- modes

def test_full_run_needs_confirmation_before_anything_is_created(tmp_path):
    with pytest.raises(RuntimeError, match="confirm_full_run=True"):
        run.run(mode="full", root=tmp_path)
    assert not (tmp_path / "1_scrapers").exists()


def test_invalid_mode_and_missing_inputs(tmp_path):
    for kwargs in ({"mode": "nope"}, {"mode": "rebuild"}, {"mode": "recon"}):
        with pytest.raises(ValueError):
            run.run(root=tmp_path, **kwargs)


def test_recon_mode_parses_a_saved_response_offline(centroids, tmp_path):
    result = run.run(mode="recon", root=tmp_path, started_at=START, centroids=centroids, source_file=FIXTURE, verbose=False)
    s = result.summary
    assert s["status"] == "RECON" and s["rows_total"] == 83 and s["in_scope"] == 83 and s["in_scope_psp"] == {"No": 33, "Yes": 50} and s["requests_made"] == 0
    assert result.run_folder.parent.name == "test_output" and s["coverage"] == {}
    assert (result.run_folder / "kues_pruefstellen_2026-10-05.csv").exists()


def test_sample_mode_compares_with_the_saved_response(centroids, tmp_path):
    saved = paths.reference_dir(tmp_path) / "api_samples" / "kues"
    saved.mkdir(parents=True)
    shutil.copy(FIXTURE, saved / "partnersuche_80636_2026-10-05.json")
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))

    class OnePlzApi:
        calls = []

        def get(self, list_name, plz, radius_km=None):
            self.calls.append((list_name, plz))
            return (records if list_name == fetch.LIST_KUES else records[:5]), {"http_status": 200, "bytes": 1, "elapsed_s": 0.1}

    api = OnePlzApi()
    result = run.run(mode="sample", root=tmp_path, started_at=START, centroids=centroids, api=api, verbose=False)
    s = result.summary
    assert api.calls == [(fetch.LIST_KUES, "80636"), (fetch.LIST_TECHNIK, "80636")] and s["requests_made"] == 2
    assert s["status"] == "SAMPLE" and s["coverage"] == {} and s["rows_total"] == 83
    assert s["sample_comparison"]["same_ids"] and s["sample_comparison"]["same_order"] and not s["sample_comparison"]["records_with_changed_content"]
    assert (result.run_folder / "raw.jsonl").exists()  # live modes keep the raw responses by default
    assert not any("grid-incomplete" in p.name or "unverified" in p.name for p in result.run_folder.iterdir())


def test_rebuild_reproduces_a_run_from_its_raw_file(passing, centroids, tmp_path):
    source, root = passing
    result = run.run(mode="rebuild", root=root, started_at=START + timedelta(hours=1), centroids=centroids, source_run=source.run_folder, verbose=False)
    s = result.summary
    assert result.run_folder.parent == root / "1_scrapers" / "kues" / "output"
    assert s["source_run"].endswith("2026-10-05_1200_simulated") and s["status"] == "PASS"
    assert s["rows_total"] == source.summary["rows_total"] and s["in_scope"] == source.summary["in_scope"]
    assert not (result.run_folder / "raw.jsonl").exists()  # no copy of the raw file
    pd.testing.assert_frame_equal(result.frame.drop(columns="scraped_at"), source.frame.drop(columns="scraped_at"))


def test_full_run_drop_check_against_the_last_passing_full_run(centroids, sites, tmp_path):
    first, _ = simulated(centroids, sites, tmp_path, mode="full", confirm_full_run=True, started_at=START, fetch_technik=False)
    assert first.summary["status"] == "PASS" and first.run_folder.parent.name == "output" and first.summary["drop_check"].startswith("no baseline")
    same, _ = simulated(centroids, sites, tmp_path, mode="full", confirm_full_run=True, started_at=START + timedelta(days=1), fetch_technik=False)
    assert same.summary["status"] == "PASS" and same.summary["drop_check"].startswith("ok") and same.summary["baseline"]["run_folder"] == first.run_folder.name
    fewer = sites.iloc[: int(len(sites) * 0.6)]
    dropped, _ = simulated(centroids, fewer, tmp_path, mode="full", confirm_full_run=True, started_at=START + timedelta(days=2), fetch_technik=False)
    assert dropped.summary["drop_alert"] and dropped.summary["status"] == "FAIL" and "drop check alert" in dropped.summary["status_reasons"]
    assert any("_unverified" in p.name for p in dropped.run_folder.iterdir())


def test_two_runs_in_the_same_minute_never_share_a_folder(centroids, small_sites, tmp_path):
    a, _ = simulated(centroids, small_sites, tmp_path, fetch_technik=False)
    b, _ = simulated(centroids, small_sites, tmp_path, fetch_technik=False)
    assert a.run_folder != b.run_folder and b.run_folder.name == a.run_folder.name + "_2"


def test_estimate_gives_the_plan_before_a_full_run(centroids):
    est = run.estimate(centroids)
    assert 70 < est["planned_queries"] < 100 and est["expected_queries"] >= est["planned_queries"]  # live full run: 84 planned
    assert est["expected_requests"] == 2 * est["expected_queries"] < est["request_limit"]
    assert 8 < est["expected_runtime_min"] < 20
    est50 = run.estimate(centroids, rc.CoverConfig())  # the page's own 50 km design stays available
    assert 300 < est50["planned_queries"] < 400 and 30 < est50["expected_runtime_min"] < 60
    assert run.estimate(centroids, fetch_technik=False)["lists"] == 1
