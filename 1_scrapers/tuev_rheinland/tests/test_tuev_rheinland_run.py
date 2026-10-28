"""Offline tests for tuev_rheinland.run against the simulated tuv.com, including runs that must NOT pass."""

from __future__ import annotations

import csv
import json
from datetime import datetime

import pytest

from tuev_rheinland import run as tr_run
from tuev_rheinland.adapter import FetchError
from tuev_rheinland.simulate import EXPECTED_IN_SCOPE, SimulatedTuv

T0 = datetime(2026, 10, 6, 12, 0)
T1 = datetime(2026, 11, 2, 9, 0)


def sim(tmp_path, centroids, fault=None, mode="simulated", now=T0, **kw):
    kw.setdefault("client", SimulatedTuv(fault))
    if mode == "full":
        kw.setdefault("confirm_full_run", True)
    return tr_run.run(mode=mode, root=tmp_path, now=now, centroids=centroids, **kw)


def failed(res, severity="hard"):
    return [c["check"] for c in res.summary["checks"] if not c["ok"] and c["severity"] == severity]


def read(path):
    return list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))


# ---------------------------------------------------------------------------------------------- clean runs
def test_simulated_passes_with_known_truth(tmp_path, centroids):
    res = sim(tmp_path, centroids)
    assert res.outcome == "PASS", res.summary["failed_hard"]
    assert res.summary["in_scope"] == EXPECTED_IN_SCOPE
    assert failed(res, "review") == []
    rows = read(res.written["pruefstellen_csv"][0])
    assert len(rows) == EXPECTED_IN_SCOPE
    by_id = {r["source_id"]: r for r in rows}
    assert by_id["45371"]["street"] == "Saarwiesenstraße 7" and by_id["45371"]["operator_group"] == "tuev_saarland"
    assert any(r["street"] == "Saarbrücker Straße 221" and r["operator_group"] == "tuev_saarland" for r in rows)
    assert not any(r["source_id"] in ("90001", "90002") for r in rows)  # foreign VIS and non-VIS stay out
    assert res.summary["requests_made"] == 4
    assert res.summary["landing"]["known_unmatched"][0]["standort"] == "Wadern-Lockweiler"


def test_run_folder_layout(tmp_path, centroids):
    res = sim(tmp_path, centroids)
    names = {p.name for p in res.run_dir.iterdir()}
    assert {"raw", "query_log.csv", "run_summary.json", "run_summary.md"} <= names
    assert {p.name for p in (res.run_dir / "raw").iterdir()} == {"raw.json", "landing_saarland.html", "landing_berlin.html",
                                                                 "landing_pfalz.html"}
    assert json.loads((res.run_dir / "run_summary.json").read_text(encoding="utf-8"))["status"] == "PASS"
    assert res.run_dir.parent.name == "test_output"


def test_full_needs_confirmation(tmp_path, centroids):
    with pytest.raises(RuntimeError):
        tr_run.run(mode="full", root=tmp_path, centroids=centroids, client=SimulatedTuv())


def test_rebuild_reproduces_the_full_run(tmp_path, centroids):
    full = sim(tmp_path, centroids, mode="full")
    assert full.outcome == "PASS" and full.run_dir.parent.name == "output"
    rebuilt = tr_run.run(mode="rebuild", root=tmp_path, now=T1, centroids=centroids, source_runs=[full.run_dir])
    assert rebuilt.outcome == "PASS" and rebuilt.summary["requests_made"] == 0
    a, b = read(full.written["pruefstellen_csv"][0]), read(rebuilt.written["pruefstellen_csv"][0])
    strip = lambda rows: [{k: v for k, v in r.items() if k != "scraped_at"} for r in rows]  # noqa: E731
    assert strip(a) == strip(b)


# ---------------------------------------------------------------------------------------------- runs that must fail
@pytest.mark.parametrize("fault", ["json_403", "not_json"])
def test_blocked_or_non_json_stops(tmp_path, centroids, fault):
    with pytest.raises(FetchError):
        sim(tmp_path, centroids, fault)
    summary = json.loads(next(tmp_path.rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL" and "FetchError" in summary["error"]
    assert not list(tmp_path.rglob("*_pruefstellen_*.csv"))


@pytest.mark.parametrize("fault, check", [
    ("saarland_23", "Saarland landing page: 24 rows and the text still says 24"),
    ("saarland_page_down", "landing page saarland could be fetched"),
    ("missing_field", "known fields exist"),
    ("duplicate_id", "ids unique"),
])
def test_hard_faults_fail_and_mark_the_csv(tmp_path, centroids, fault, check):
    res = sim(tmp_path, centroids, fault)
    assert res.outcome == "FAIL" and check in failed(res)
    assert "unverified" in res.written["pruefstellen_csv"][0].name


def test_drop_of_more_than_10_percent_fails(tmp_path, centroids):
    assert sim(tmp_path, centroids, mode="full").outcome == "PASS"
    res = sim(tmp_path, centroids, "drop_half", mode="full", now=T1)
    assert res.outcome == "FAIL" and tr_run.DROP_CHECK in failed(res)


# ---------------------------------------------------------------------------------------------- review items, run still passes
def test_new_landing_row_is_check_now_not_fail(tmp_path, centroids):
    res = sim(tmp_path, centroids, "new_landing_row")
    assert res.outcome == "PASS"
    assert [r["standort"] for r in res.summary["check_now"]] == ["Rehlingen"]
    assert "every landing row matches a finder VIS, a decision or a known gap" in failed(res, "review")
    assert any("add" in c for c in failed(res, "review"))  # Losheim is gone from the page: its decision no longer applies


def test_decision_that_no_longer_applies_is_reported(tmp_path, centroids):
    res = sim(tmp_path, centroids, "losheim_in_json")  # Losheim now in the finder JSON: the add decision is skipped, no duplicate
    assert res.outcome == "PASS"
    assert any("add" in c for c in failed(res, "review"))
    rows = read(res.written["pruefstellen_csv"][0])
    assert sum(r["street"] == "Saarbrücker Straße 221" for r in rows) == 1


def test_berlin_page_down_is_review_only(tmp_path, centroids):
    res = sim(tmp_path, centroids, "berlin_page_down")
    assert res.outcome == "PASS"
    assert "landing page berlin could be fetched" in failed(res, "review")
