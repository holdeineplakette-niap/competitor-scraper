"""TÜV SÜD runs end to end against the offline fake finder and the fake browser: PASS, the negative tests, modes, labels."""

import json
import socket
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from core import paths
from tuev_sued import browser, coverage, parse, simulate
from tuev_sued import run as tuev_run

START = datetime(2026, 10, 5, 12, 0)
QUIET = lambda msg: None  # noqa: E731


@pytest.fixture(scope="module")
def sites(centroids):
    return simulate.simulated_sites(centroids)


def drive(root, centroids, api, mode="full", started=START, options=None):
    """plan -> fake browser -> check, until no further pass is planned. Returns (run folder, [CheckResult])."""
    p = tuev_run.plan(mode, root=root, started_at=started, centroids=centroids)
    folder, clock, results = p.run_folder, started, []
    with tempfile.TemporaryDirectory() as tmp:
        while p is not None:
            opts = options(p.pass_no) if callable(options) else (options or {})
            download = Path(tmp) / f"pass{p.pass_no}.jsonl"
            download.write_text(simulate.browser_download(p, api, clock, **opts), encoding="utf-8")
            result = tuev_run.check(folder, download, centroids=centroids, root=root, say=QUIET)
            results.append(result)
            p, clock = result.next_plan, clock + timedelta(minutes=20)
    return folder, results


def simulated(centroids, sites, root, api=None, **kwargs):
    api = api or simulate.FakeTuevApi(sites, centroids)
    return tuev_run.run(mode="simulated", root=root, started_at=kwargs.pop("started_at", START), centroids=centroids, api=api, verbose=False, **kwargs)


@pytest.fixture(scope="module")
def passing(centroids, sites, tmp_path_factory):
    root = tmp_path_factory.mktemp("tuev_pass")
    return simulated(centroids, sites, root, exports=tuev_run.EXPORT_KEYS), root  # every export, to test them all; the default is one CSV


def found_all(result, sites):
    return set(result.frame["source_id"]) == set(sites["id"])


# ---- the simulated run must PASS and find everything

def test_simulated_run_passes_and_finds_every_site(passing, sites):
    result, _ = passing
    s = result.summary
    assert s["status"] == "PASS" and s["status_reasons"] == []
    assert len(sites) == 350 and s["rows_total"] == 350 and found_all(result, sites)  # 100 % of the fake sites
    assert s["coverage"]["uncovered"]["total"] == 0 and s["coverage"]["premise"]["ok"] and s["coverage"]["queries"] == s["requests_made"]
    assert 1 <= len(s["passes"]) <= coverage.MAX_PASSES and all(p["stopped_reason"] is None for p in s["passes"])
    assert 100 <= s["passes"][0]["planned"] <= 200  # the lattice pass
    assert not s["warnings"].get("address_conflicts") and not s["warnings"].get("passes_never_imported")


def test_simulated_run_writes_one_folder_with_the_expected_files(passing):
    result, root = passing
    folder = result.run_folder
    assert folder.parent == root / "1_scrapers" / "tuev_sued" / "test_output" and folder.name == "2026-10-05_1200_simulated"
    names = {p.name for p in folder.iterdir()}
    n_passes = len(result.summary["passes"])
    assert {"tuev_sued_all_2026-10-05.csv", "tuev_sued_all_2026-10-05.xlsx", "tuev_sued_pruefstellen_2026-10-05.csv", "tuev_sued_pruefstellen_2026-10-05.xlsx",
            "raw.jsonl", "query_log.csv", "run_summary.json", "run_summary.md"} <= names
    assert {f"tuev_sued_pass{n:02d}.js" for n in range(1, n_passes + 1)} <= names and {f"raw_pass{n:02d}.jsonl" for n in range(1, n_passes + 1)} <= names
    assert "open_targets.csv" not in names  # nothing was left open
    summary = json.loads((folder / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS" and summary["in_scope"] == result.summary["in_scope"]
    log = pd.read_csv(folder / "query_log.csv", dtype={"zip": str})
    assert list(log.columns) == tuev_run.LOG_COLUMNS and len(log) == result.summary["requests_made"]
    assert set(log["reason"]) <= {"plan", "fill"} and (log["pass"].diff().dropna() >= 0).all() and log["zip"].str.len().eq(5).all()
    assert (log["radius_km"] >= 0).all() and log["centre_offset_km"].max() <= 5.1  # the fake server centres lie up to 5 km off


def test_by_default_only_the_in_scope_csv_is_exported(centroids, sites, tmp_path):
    """No `all` file and no Excel unless asked for (Nikos, 2026-10-06)."""
    assert tuev_run.DEFAULT_EXPORTS == ("pruefstellen_csv",)
    result = simulated(centroids, sites, tmp_path)
    names = {p.name for p in result.run_folder.iterdir()}
    assert "tuev_sued_pruefstellen_2026-10-05.csv" in names
    assert not any("_all_" in n or n.endswith(".xlsx") for n in names)
    assert list(result.files) == ["pruefstellen_csv"] and result.summary["exports"] == ["pruefstellen_csv"]
    assert len(result.frame) == 350  # the frame in memory and the summary still cover every record
    rows = pd.read_csv(result.run_folder / "tuev_sued_pruefstellen_2026-10-05.csv", sep=";", encoding="utf-8-sig", dtype=str)
    assert len(rows) == result.summary["in_scope"] and set(rows["site_type"]) == {"pruefstelle"}


def test_raw_jsonl_is_the_pass_files_in_order_with_renamed_summaries(passing):
    result, _ = passing
    folder = result.run_folder
    with open(folder / "raw.jsonl", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    assert all(set(item) == {"file", "response"} for item in lines)
    names = [item["file"] for item in lines]
    summaries = [n for n in names if n.startswith("browser_summary")]
    assert summaries == [f"browser_summary_pass{n:02d}.json" for n in range(1, len(result.summary["passes"]) + 1)]
    assert len(names) - len(summaries) == result.summary["requests_made"]
    passes = [parse.parse_answer_name(n)[0] for n in names if parse.parse_answer_name(n)]
    assert passes == sorted(passes)
    raw_pass = [json.loads(line)["file"] for line in (folder / "raw_pass01.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert raw_pass[:-1] == names[: len(raw_pass) - 1] and raw_pass[-1] == "browser_summary.json"


def test_exports_filter_by_in_scope_and_keep_every_record(passing):
    result, _ = passing
    folder = result.run_folder
    all_rows = pd.read_csv(folder / "tuev_sued_all_2026-10-05.csv", sep=";", encoding="utf-8-sig", dtype=str)
    in_scope = pd.read_csv(folder / "tuev_sued_pruefstellen_2026-10-05.csv", sep=";", encoding="utf-8-sig", dtype=str)
    assert len(all_rows) == 350 and len(in_scope) == result.summary["in_scope"] < 350
    assert set(in_scope["site_type"]) == {"pruefstelle"} and set(all_rows["site_type"]) == {"pruefstelle", "unknown"}
    assert all_rows["location_id"].str.startswith("TUEV_SUED-").all() and list(all_rows.columns) == parse.COLUMNS
    assert all_rows["location_sign"].str.startswith("0").any()  # leading zeros survive (text)
    xlsx = pd.read_excel(folder / "tuev_sued_pruefstellen_2026-10-05.xlsx", dtype=str)
    assert xlsx["location_sign"].str.len().min() >= 3 and set(xlsx["zip"].str.len()) == {5}


def test_summary_counts_and_flags_are_consistent(passing):
    s = passing[0].summary
    assert sum(s["site_types"].values()) == s["rows_total"] and sum(s["in_scope_by_state"].values()) == s["in_scope"]
    assert s["flags"]["uo_notice"] == 1  # the simulated Wolnzach notice
    assert "company_not_500" not in s["flags"] and s["needs_review"] >= s["flags"]["uo_notice"]
    assert set(s["in_scope_by_operator"]) == {"own", "tuev_sued_partner"} and sum(s["in_scope_by_operator"].values()) == s["in_scope"]
    fill = s["location_key_fill"]
    assert fill["tscId"] == {"filled": 350, "rate": 1.0} and "locationSign" in fill and "notificationText_start" in fill
    assert s["drop_check"] == "not run (simulated)" and s["baseline"] is None


def test_snippets_in_the_folder_match_the_logged_queries(passing):
    result, _ = passing
    log = pd.read_csv(result.run_folder / "query_log.csv", dtype={"zip": str})
    for n in range(1, len(result.summary["passes"]) + 1):
        plan = browser.read_plan(result.run_folder / browser.pass_js_name(n))
        assert plan.pass_no == n and plan.mode == "simulated"
        assert log.loc[log["pass"] == n, "zip"].tolist() == plan.zips  # nothing dropped, same order
        assert log.loc[log["pass"] == n, "reason"].tolist() == [r for _, r in plan.queries]


def test_the_run_summary_markdown_names_the_outcome(passing):
    text = (passing[0].run_folder / "run_summary.md").read_text(encoding="utf-8")
    assert "PASS (every target covered, premise holds)" in text and "Coverage:" in text and "Premise:" in text


def test_the_simulated_flow_makes_no_network_call(centroids, sites, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert simulated(centroids, sites, tmp_path).summary["status"] == "PASS"


# ---- the generic rule must hold for other finders too

def test_a_cap_of_40_still_passes_and_finds_everything(centroids, sites, tmp_path):
    result = simulated(centroids, sites, tmp_path, api=simulate.FakeTuevApi(sites, centroids, cap=40))
    assert result.summary["status"] == "PASS" and found_all(result, sites)
    assert result.summary["warnings"]["answers_below_expected_count"]["total"] == result.summary["requests_made"]  # all answers have 40 < 50


@pytest.mark.parametrize("limit", [80.0])
def test_a_radius_limit_never_gives_a_false_pass(centroids, sites, tmp_path, limit):
    result = simulated(centroids, sites, tmp_path, api=simulate.FakeTuevApi(sites, centroids, radius_limit_km=limit))
    s = result.summary
    assert "answers_below_expected_count" in s["warnings"]  # sparse areas return fewer than 50 within the limit
    assert len(s["passes"]) <= coverage.MAX_PASSES
    if s["status"] == "PASS":
        assert found_all(result, sites)  # a PASS must mean that nothing was missed
    else:  # FAIL cleanly: reasons, open targets listed, exports labelled
        assert s["status"] == "FAIL" and any("not covered" in r for r in s["status_reasons"])
        assert (result.run_folder / "open_targets.csv").exists()
        assert any(p.name.endswith("_grid-incomplete.csv") for p in result.run_folder.iterdir())
        open_targets = pd.read_csv(result.run_folder / "open_targets.csv", dtype={"zip": str})
        assert len(open_targets) == s["coverage"]["uncovered"]["total"] > 0 and list(open_targets.columns) == ["zip", "city", "state", "lat", "lng"]


def test_a_dropped_answer_leaves_targets_uncovered_and_is_not_a_pass(centroids, sites, tmp_path):
    api = simulate.FakeTuevApi(sites, centroids)
    p = tuev_run.plan("full", root=tmp_path, started_at=START, centroids=centroids)
    # the answers of the whole southern lattice are lost: the targets there are uncovered, no matter what else came back
    lost = {z for z in p.zips if centroids.set_index("zip").at[z, "lat"] < 49.0}
    assert lost
    download = tmp_path / "d.jsonl"
    download.write_text(simulate.browser_download(p, api, START, drop_answers=lost), encoding="utf-8")
    checked = tuev_run.check(p.run_folder, download, centroids=centroids, root=tmp_path, say=QUIET)
    assert checked.status == "INCOMPLETE" and checked.coverage["uncovered"] and not checked.coverage["ok"]
    assert checked.next_plan.pass_no == 2 and len(checked.next_plan.queries) <= coverage.CFG.max_fill_per_pass
    built = tuev_run.build(p.run_folder, centroids=centroids, root=tmp_path, say=QUIET)  # the run is given up here
    assert built.summary["status"] == "FAIL" and any("not covered" in r for r in built.summary["status_reasons"])
    assert built.summary["warnings"]["passes_never_imported"] == [2]
    assert (p.run_folder / "open_targets.csv").exists() and any(f.name.endswith("_grid-incomplete.csv") for f in p.run_folder.iterdir())
    assert len(built.summary["passes"]) == 1 and built.summary["passes"][0]["done"] == len(p.zips) - len(lost)


def test_a_site_missing_from_one_answer_fails_the_premise(centroids, sites, tmp_path):
    first = coverage.first_pass_queries(centroids)[0][0]
    clean = simulate.FakeTuevApi(sites, centroids)
    point = centroids.set_index("zip").loc[first]
    _, found = clean.finder.search(first, point["lat"], point["lng"])
    site_id = found[len(found) // 2][0]  # in the middle of the answer: well inside its disk
    api = simulate.FakeTuevApi(sites, centroids, drop=lambda z, i: z == first and i == site_id)
    result = simulated(centroids, sites, tmp_path, api=api)
    s = result.summary
    assert s["status"] == "FAIL" and not s["coverage"]["premise"]["ok"]
    assert any(m["site_id"] == site_id and m["query"] == f"p01:{first}" for m in s["coverage"]["premise"]["misses"]["items"])
    assert any("premise" in r for r in s["status_reasons"])
    tag = "grid-incomplete" if s["coverage"]["uncovered"]["total"] else "unverified"
    assert any(p.name.endswith(f"_{tag}.csv") for p in result.run_folder.iterdir())


def test_a_site_far_from_every_plz_centroid_is_a_warning_and_a_review_flag(centroids, tmp_path):
    sites = simulate.simulated_sites(centroids, extra=[("99999", 55.0, 7.0)])  # in the North Sea, far from any centroid
    result = simulated(centroids, sites, tmp_path)
    far = result.summary["warnings"]["sites_far_from_plz_centroid"]
    assert any(f["id"] == "99999" and f["km"] > 20 for f in far["items"])
    row = result.frame[result.frame["source_id"] == "99999"].iloc[0]
    assert row["needs_review"] and "coordinate_far_from_plz" in row["doubt_flags"] and row["in_scope"]


def test_a_blocked_pass_stops_the_run_and_is_reported(centroids, sites, tmp_path):
    stop = {"reason": "invalid_answer", "plz": "24937", "status": 403, "content_type": "text/html", "first_200_chars": "<title>Just a moment...</title>"}
    result = simulated(centroids, sites, tmp_path, browser_kwargs={"stop_after": 5, "stopped_reason": stop})
    s = result.summary
    assert s["status"] == "FAIL" and any("blocked" in r for r in s["status_reasons"])
    assert len(s["passes"]) == 1 and s["passes"][0]["stopped_reason"]["status"] == 403 and s["requests_made"] == 5
    assert not (result.run_folder / "tuev_sued_pass02.js").exists()  # no next pass after a block: no escalation


def test_a_user_stop_keeps_what_was_collected_and_plans_on(centroids, sites, tmp_path):
    api = simulate.FakeTuevApi(sites, centroids)
    folder, results = drive(tmp_path, centroids, api, options=lambda n: {"stop_after": 20} if n == 1 else {})
    first = results[0]
    assert first.status == "INCOMPLETE" and "stopped early (user_stop)" in first.message and len(first.imported.answers) == 20
    assert first.next_plan is not None


# ---- sample mode

def test_sample_run_is_never_a_pass_and_reports_the_flensburg_question(centroids, sites, tmp_path):
    api = simulate.FakeTuevApi(sites, centroids)
    p = tuev_run.plan("sample", root=tmp_path, started_at=START, centroids=centroids)
    assert p.zips == list(tuev_run.SAMPLE_PLZ) == ["80686", "24937", "01067"] and p.run_folder.parent.name == "test_output" and p.run_folder.name.endswith("_sample")
    download = tmp_path / "sample.jsonl"
    download.write_text(simulate.browser_download(p, api, START), encoding="utf-8")
    checked = tuev_run.check(p.run_folder, download, centroids=centroids, root=tmp_path, say=QUIET)
    assert checked.status == "SAMPLE" and checked.next_plan is None
    built = tuev_run.build(p.run_folder, centroids=centroids, root=tmp_path, say=QUIET)
    s = built.summary
    assert s["status"] == "SAMPLE" and s["mode"] == "sample" and "SAMPLE" in s["outcome"] and s["requests_made"] == 3
    findings = s["sample_findings"]
    assert findings["flensburg_count"] == 50 and findings["flensburg_exactly_50"] is True and findings["all_exactly_50"] is True
    assert [a["zip"] for a in findings["answers"]] == ["80686", "24937", "01067"]
    assert not any(n.name.endswith(("_grid-incomplete.csv", "_unverified.csv")) for n in p.run_folder.iterdir())  # not labelled, just not a crawl
    assert "Flensburg 24937: 50 sites" in (p.run_folder / "run_summary.md").read_text(encoding="utf-8")


def test_sample_reports_a_flensburg_with_fewer_than_50(centroids, sites, tmp_path):
    api = simulate.FakeTuevApi(sites, centroids, radius_limit_km=60)  # sparse north: fewer than 50 within the limit
    p = tuev_run.plan("sample", root=tmp_path, started_at=START, centroids=centroids)
    download = tmp_path / "sample.jsonl"
    download.write_text(simulate.browser_download(p, api, START), encoding="utf-8")
    tuev_run.check(p.run_folder, download, centroids=centroids, root=tmp_path, say=QUIET)
    findings = tuev_run.build(p.run_folder, centroids=centroids, root=tmp_path, say=QUIET).summary["sample_findings"]
    assert findings["flensburg_count"] < 50 and findings["flensburg_exactly_50"] is False and findings["all_exactly_50"] is False


# ---- full runs over several months, rebuild

@pytest.fixture(scope="module")
def months(centroids, sites, tmp_path_factory):
    root = tmp_path_factory.mktemp("tuev_months")
    api = simulate.FakeTuevApi(sites, centroids)
    folder1, results1 = drive(root, centroids, api, started=START)
    built1 = tuev_run.build(folder1, centroids=centroids, root=root, say=QUIET)
    folder2, results2 = drive(root, centroids, api, started=START + timedelta(days=30))
    built2 = tuev_run.build(folder2, centroids=centroids, root=root, say=QUIET)
    return {"root": root, "first": (folder1, results1, built1), "second": (folder2, results2, built2)}


def test_a_full_run_goes_to_output_and_passes_after_fill_in_passes(months):
    folder1, results1, built1 = months["first"]
    assert folder1.parent == months["root"] / "1_scrapers" / "tuev_sued" / "output" and folder1.name == "2026-10-05_1200_full"
    assert [r.status for r in results1][-1] == "PASS" and all(r.status == "INCOMPLETE" for r in results1[:-1])
    assert built1.summary["status"] == "PASS" and built1.summary["mode"] == "full" and len(results1) == len(built1.summary["passes"])
    assert built1.summary["drop_check"].startswith("no baseline")
    assert all(len(r.next_plan.queries) <= coverage.CFG.max_fill_per_pass for r in results1[:-1])


def test_the_next_month_repeats_the_last_query_list_and_needs_one_pass(months):
    folder1, _, built1 = months["first"]
    folder2, results2, built2 = months["second"]
    first_zips = pd.read_csv(folder1 / "query_log.csv", dtype={"zip": str})["zip"].tolist()
    log2 = pd.read_csv(folder2 / "query_log.csv", dtype={"zip": str})
    assert len(results2) == 1 and results2[0].status == "PASS"
    assert log2["zip"].tolist() == first_zips and set(log2["reason"]) == {"repeat"}
    assert built2.summary["status"] == "PASS" and built2.summary["in_scope"] == built1.summary["in_scope"]
    assert built2.summary["baseline"]["run_folder"] == folder1.name and built2.summary["drop_alert"] is False
    assert built2.summary["drop_check"].startswith("ok:")


def test_a_drop_below_the_baseline_fails_the_run(months, centroids, sites, tmp_path):
    root = months["root"]
    fewer = sites.sample(frac=0.6, random_state=1)  # 40 % of the Service-Center vanished from the finder
    api = simulate.FakeTuevApi(fewer, centroids)
    folder, _ = drive(root, centroids, api, started=START + timedelta(days=60))
    built = tuev_run.build(folder, centroids=centroids, root=root, say=QUIET)
    assert built.summary["drop_alert"] is True and built.summary["status"] == "FAIL" and "drop check alert" in built.summary["status_reasons"]
    assert any(p.name.endswith("_unverified.csv") for p in folder.iterdir())


def test_rebuild_reproduces_a_run_from_its_raw_file(months, centroids):
    root = months["root"]
    folder1, _, built1 = months["first"]
    rebuilt = tuev_run.run(mode="rebuild", source_run=folder1.name, root=root, started_at=START + timedelta(days=1), centroids=centroids, verbose=False)
    s = rebuilt.summary
    assert rebuilt.run_folder.parent == folder1.parent and rebuilt.run_folder.name == "2026-10-06_1200_rebuild"
    assert not (rebuilt.run_folder / "raw.jsonl").exists()  # no copy of the raw file
    assert s["source_run"].endswith(folder1.name) and s["mode"] == "rebuild"
    assert s["rows_total"] == built1.summary["rows_total"] and s["in_scope"] == built1.summary["in_scope"] and s["status"] == "PASS"
    assert [p["pass"] for p in s["passes"]] == [p["pass"] for p in built1.summary["passes"]] and s["requests_made"] == built1.summary["requests_made"]
    log = pd.read_csv(rebuilt.run_folder / "query_log.csv")
    assert set(log["reason"]) == {"rebuild"}
    assert set(rebuilt.frame["source_id"]) == set(built1.frame["source_id"])


# ---- guard rails

def test_steps_that_do_not_fit_the_state_of_the_folder_say_so(centroids, sites, tmp_path):
    api = simulate.FakeTuevApi(sites, centroids)
    p = tuev_run.plan("sample", root=tmp_path, started_at=START, centroids=centroids)
    with pytest.raises(tuev_run.RunError, match="no pass was imported"):
        tuev_run.build(p.run_folder, centroids=centroids, root=tmp_path, say=QUIET)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("garbage\n", encoding="utf-8")
    with pytest.raises(browser.ImportProblem):
        tuev_run.check(p.run_folder, bad, centroids=centroids, root=tmp_path, say=QUIET)
    assert browser.passes_imported(p.run_folder) == []  # a bad file changes nothing
    good = tmp_path / "good.jsonl"
    good.write_text(simulate.browser_download(p, api, START), encoding="utf-8")
    tuev_run.check(p.run_folder, good, centroids=centroids, root=tmp_path, say=QUIET)
    with pytest.raises(tuev_run.RunError, match="no pass is waiting"):
        tuev_run.check(p.run_folder, good, centroids=centroids, root=tmp_path, say=QUIET)
    tuev_run.build(p.run_folder, centroids=centroids, root=tmp_path, say=QUIET)
    with pytest.raises(tuev_run.RunError, match="built already"):
        tuev_run.build(p.run_folder, centroids=centroids, root=tmp_path, say=QUIET)


def test_a_file_of_the_wrong_pass_is_refused(centroids, sites, tmp_path):
    api = simulate.FakeTuevApi(sites, centroids)
    p = tuev_run.plan("full", root=tmp_path, started_at=START, centroids=centroids)
    other = tuev_run.plan("sample", root=tmp_path, started_at=START + timedelta(minutes=5), centroids=centroids)
    wrong = tmp_path / "wrong.jsonl"
    wrong.write_text(simulate.browser_download(other, api, START), encoding="utf-8")  # the sample pass (3 PLZ) for a full run (pass 1 of ~130)
    with pytest.raises(browser.ImportProblem, match="planned 3"):
        tuev_run.check(p.run_folder, wrong, centroids=centroids, root=tmp_path, say=QUIET)
    assert browser.passes_imported(p.run_folder) == []


def test_modes_and_arguments_are_checked(tmp_path):
    for mode in ("full", "sample"):
        with pytest.raises(ValueError, match="step by step"):
            tuev_run.run(mode=mode, root=tmp_path)
    with pytest.raises(ValueError, match="mode must be one of"):
        tuev_run.run(mode="recon", root=tmp_path)
    with pytest.raises(ValueError, match="source_run"):
        tuev_run.run(mode="rebuild", root=tmp_path, centroids=pd.DataFrame(columns=["zip", "city", "state", "lat", "lng"]))
    with pytest.raises(ValueError):
        tuev_run.plan("rebuild", root=tmp_path)
    with pytest.raises(FileNotFoundError):
        tuev_run.find_run("2000-01-01_0000_full", tmp_path)
    with pytest.raises(tuev_run.RunError, match="not a run folder"):
        tuev_run._mode_of(tmp_path)


def test_a_run_never_writes_into_an_existing_folder(centroids, sites, tmp_path):
    first = tuev_run.plan("sample", root=tmp_path, started_at=START, centroids=centroids)
    second = tuev_run.plan("sample", root=tmp_path, started_at=START, centroids=centroids)  # same minute
    assert first.run_folder.name == "2026-10-05_1200_sample" and second.run_folder.name == "2026-10-05_1200_sample_2"
    assert (second.run_folder / "tuev_sued_pass01.js").exists()
    api = simulate.FakeTuevApi(sites, centroids)
    download = tmp_path / "d.jsonl"
    download.write_text(simulate.browser_download(second, api, START), encoding="utf-8")
    assert tuev_run.check(second.run_folder, download, centroids=centroids, root=tmp_path, say=QUIET).status == "SAMPLE"
    assert tuev_run.find_run(second.run_folder.name, tmp_path) == second.run_folder


def test_the_estimate_reports_passes_and_queries_of_the_simulated_world(centroids):
    est = tuev_run.estimate(centroids)
    assert est["simulated_ok"] and 1 <= est["expected_passes"] <= coverage.MAX_PASSES
    assert est["expected_queries"] == sum(est["queries_per_pass"]) and est["planned_queries"] == est["queries_per_pass"][0]
    assert len(est["planned_points"]) == est["planned_queries"] and len(est["expected_minutes_per_pass"]) == est["expected_passes"]
    assert est["expected_minutes_per_pass"][0] > 5  # ~130 PLZ at 5-7 s each


def test_the_paths_come_from_core_paths():
    assert paths.output_dir("tuev_sued", "full").name == "output" and paths.output_dir("tuev_sued", "sample").name == "test_output"
    assert paths.fixtures_dir("tuev_sued").is_dir()
