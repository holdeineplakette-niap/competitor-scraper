"""Offline tests for tuev_thueringen.run against the simulated tuev-thueringen.de, including runs that must NOT pass."""

from __future__ import annotations

import csv
import json
from datetime import datetime

import pytest

from tuev_thueringen import run as tt_run
from tuev_thueringen.adapter import FetchError
from tuev_thueringen.simulate import STORES, SimulatedThueringen

T0 = datetime(2026, 10, 6, 12, 0)
T1 = datetime(2026, 11, 2, 9, 0)


def sim(tmp_path, centroids, fault=None, mode="simulated", now=T0, **kw):
    kw.setdefault("client", SimulatedThueringen(fault))
    if mode == "full":
        kw.setdefault("confirm_full_run", True)
    return tt_run.run(mode=mode, root=tmp_path, now=now, centroids=centroids, **kw)


def failed(res):
    return [c["check"] for c in res.summary["checks"] if not c["ok"]]


def read(path):
    return list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))


# ---------------------------------------------------------------------------------------------- clean runs
def test_simulated_passes_with_known_truth(tmp_path, centroids):
    res = sim(tmp_path, centroids)
    assert res.outcome == "PASS", res.summary["failed_hard"]
    assert res.summary["in_scope"] == len(STORES) == 12
    rows = read(res.written["pruefstellen_csv"][0])
    assert {r["source_id"] for r in rows} == {s[0] for s in STORES}
    jena = next(r for r in rows if r["city"] == "Jena")
    assert (jena["street"], jena["zip"], jena["email"]) == ("Ernst-Ruska-Ring 6", "07745", STORES[0][7])
    assert jena["state"] == "Thüringen" and jena["site_type"] == "own_station"
    assert res.summary["requests_made"] == 2


def test_run_folder_layout(tmp_path, centroids):
    res = sim(tmp_path, centroids)
    assert {"raw", "query_log.csv", "run_summary.json", "run_summary.md"} <= {p.name for p in res.run_dir.iterdir()}
    assert {p.name for p in (res.run_dir / "raw").iterdir()} == {"main.html", "cross.html"}
    assert json.loads((res.run_dir / "run_summary.json").read_text(encoding="utf-8"))["status"] == "PASS"
    assert res.run_dir.parent.name == "test_output"


def test_full_needs_confirmation(tmp_path, centroids):
    with pytest.raises(RuntimeError):
        tt_run.run(mode="full", root=tmp_path, centroids=centroids, client=SimulatedThueringen())


def test_rebuild_reproduces_the_full_run(tmp_path, centroids):
    full = sim(tmp_path, centroids, mode="full")
    assert full.outcome == "PASS" and full.run_dir.parent.name == "output"
    rebuilt = tt_run.run(mode="rebuild", root=tmp_path, now=T1, centroids=centroids, source_runs=[full.run_dir])
    assert rebuilt.outcome == "PASS" and rebuilt.summary["requests_made"] == 0
    strip = lambda rows: [{k: v for k, v in r.items() if k != "scraped_at"} for r in rows]  # noqa: E731
    assert strip(read(full.written["pruefstellen_csv"][0])) == strip(read(rebuilt.written["pruefstellen_csv"][0]))


# ---------------------------------------------------------------------------------------------- runs that must fail
@pytest.mark.parametrize("fault", ["http_403", "no_table"])
def test_blocked_or_changed_page_stops(tmp_path, centroids, fault):
    with pytest.raises(FetchError):
        sim(tmp_path, centroids, fault)
    summary = json.loads(next(tmp_path.rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL" and "FetchError" in summary["error"]
    assert not list(tmp_path.rglob("*_pruefstellen_*.csv"))


@pytest.mark.parametrize("fault, check", [
    ("view_mismatch", "ids identical in overlay / table / mobile"),
    ("cross_differs", "cross-check URL yields the same ids"),
    ("cross_down", "cross-check URL yields the same ids"),
    ("bad_address", "street, 5-digit PLZ, city, coordinates in Germany"),
    ("duplicate_id", "store-ids unique"),
])
def test_faults_fail_and_mark_the_csv(tmp_path, centroids, fault, check):
    res = sim(tmp_path, centroids, fault)
    assert res.outcome == "FAIL" and check in failed(res)
    assert "unverified" in res.written["pruefstellen_csv"][0].name


def test_drop_of_more_than_10_percent_fails(tmp_path, centroids):
    assert sim(tmp_path, centroids, mode="full").outcome == "PASS"
    res = sim(tmp_path, centroids, "drop_half", mode="full", now=T1)
    assert res.outcome == "FAIL" and any(c.startswith("count vs last PASS CSV") for c in failed(res))
