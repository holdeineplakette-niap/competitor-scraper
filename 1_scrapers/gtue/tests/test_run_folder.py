"""The run-folder writer: one folder per run, named per mode, never written into twice, raw.jsonl with one
line per request, same-day cache limited to the same area, PASS-only drop-check baseline. No network."""

import json
from datetime import datetime

import pandas as pd
import pytest

from core import paths
from gtue import adapter as gtue

from test_grid import fake_client_class, raw_lines, run_folders

T = datetime(2026, 10, 1, 9, 0)
RUN_FILES = {
    "query_log.csv", "run_summary.json", "run_summary.md",
    "gtue_all_2026-10-01.csv", "gtue_all_2026-10-01.xlsx",
    "gtue_pruefstellen_2026-10-01.csv", "gtue_pruefstellen_2026-10-01.xlsx",
}


def _live(tmp_path, centroids, monkeypatch, mode, locations, **kwargs):
    monkeypatch.setattr(gtue, "load_api_key", lambda root: "test-key")
    monkeypatch.setattr(gtue, "GtueClient", fake_client_class(locations, centroids))
    return gtue.run(
        mode=mode, confirm_full_run=True, root=tmp_path, centroids=centroids, started_at=T, verbose=False, **kwargs
    )


@pytest.fixture(scope="module")
def locations(centroids):
    return gtue.simulated_locations(centroids, seed=3, clusters={})


def test_full_run_folder_is_in_output_with_the_convention_files(tmp_path, centroids, monkeypatch, locations):
    result = _live(tmp_path, centroids, monkeypatch, "full", locations)
    folder = paths.run_dir("gtue", "full", T, tmp_path)
    assert folder == tmp_path / "1_scrapers" / "gtue" / "output" / "2026-10-01_0900_full"
    assert {p.name for p in folder.iterdir()} == RUN_FILES | {"raw.jsonl"}
    assert result.summary["files"]["run_folder"] == "1_scrapers/gtue/output/2026-10-01_0900_full"
    assert not (tmp_path / "output").exists() and not (tmp_path / "raw").exists()
    md = (folder / "run_summary.md").read_text(encoding="utf-8")
    assert md.startswith("# Run summary: 2026-10-01_0900_full") and "PASS" in md


def test_sample_run_folder_is_in_test_output(tmp_path, centroids, monkeypatch, locations):
    _live(tmp_path, centroids, monkeypatch, "sample", locations)
    folder = tmp_path / "1_scrapers" / "gtue" / "test_output" / "2026-10-01_0900_sample"
    assert folder.is_dir() and (folder / "raw.jsonl").exists() and (folder / "run_summary.json").exists()
    assert not (tmp_path / "1_scrapers" / "gtue" / "output").exists()


def test_simulated_run_folder_is_in_test_output_and_has_no_raw(tmp_path, centroids):
    result = gtue.run(mode="simulated", root=tmp_path, centroids=centroids, started_at=T, verbose=False)
    folder = tmp_path / "1_scrapers" / "gtue" / "test_output" / "2026-10-01_0900_simulated"
    assert {p.name for p in folder.iterdir()} == RUN_FILES  # fake responses are not kept
    assert result.summary["status"] == "PASS" and result.summary["unique_locations"] == 1200


def test_recon_run_folder_is_in_test_output(tmp_path, centroids):
    df = gtue.run(mode="simulated", root=tmp_path, centroids=centroids, started_at=T, verbose=False).df.head(5)
    folder, written = gtue.write_recon_run(df, tmp_path / "fixture.json", root=tmp_path, started_at=T)
    assert folder == tmp_path / "1_scrapers" / "gtue" / "test_output" / "2026-10-01_0900_recon"
    assert {p.name for p in folder.iterdir()} == {
        "gtue_all_2026-10-01.csv", "gtue_all_2026-10-01.xlsx", "gtue_pruefstellen_2026-10-01.csv",
        "gtue_pruefstellen_2026-10-01.xlsx", "run_summary.json", "run_summary.md",
    }
    assert json.loads((folder / "run_summary.json").read_text(encoding="utf-8"))["mode"] == "recon"
    assert set(written) == {"all_csv", "all_xlsx", "pruefstellen_csv", "pruefstellen_xlsx"}


def test_a_run_never_writes_into_an_existing_folder(tmp_path, centroids):
    existing = paths.run_dir("gtue", "simulated", T, tmp_path)
    existing.mkdir(parents=True)
    (existing / "query_log.csv").write_text("precious\n", encoding="utf-8")
    before = sorted(p.name for p in existing.iterdir())

    result = gtue.run(mode="simulated", root=tmp_path, centroids=centroids, started_at=T, verbose=False)

    assert sorted(p.name for p in existing.iterdir()) == before
    assert (existing / "query_log.csv").read_text(encoding="utf-8") == "precious\n"
    new = tmp_path / result.summary["files"]["run_folder"]
    assert new.name == "2026-10-01_0900_simulated_2" and (new / "run_summary.json").exists()


def test_raw_jsonl_has_one_line_per_request(tmp_path, centroids, monkeypatch, locations):
    result = _live(tmp_path, centroids, monkeypatch, "full", locations)
    folder = tmp_path / result.summary["files"]["run_folder"]
    text = (folder / "raw.jsonl").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert text.endswith("\n") and len(lines) == result.summary["requests_made"] > 100
    records = [json.loads(line) for line in lines]
    assert [r["file"] for r in records] == [f"{i:04d}.json" for i in range(1, len(lines) + 1)]
    assert all(set(r) == {"file", "response"} and "Paging" in r["response"] for r in records)
    # every live query of the log points at its own line
    live = result.query_log[~result.query_log["cached"]]
    assert list(live["raw_file"]) == [f"{result.summary['files']['raw_jsonl']}#{r['file']}" for r in records]


def test_raw_store_content_is_the_response_as_received(tmp_path):
    store = gtue.RawStore(tmp_path / "run", tmp_path)
    data = {"AddressItems": [{"name": "Größe ü"}], "Paging": {"Count": 1}}
    store.save(data)
    line = (tmp_path / "run" / "raw.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert "Größe ü" in line  # UTF-8, not \u-escaped, like the old per-file raw responses
    assert raw_lines(tmp_path / "run") == [("0001.json", data)]


def test_cache_reuses_only_the_same_day_in_the_same_area(tmp_path, centroids, monkeypatch, locations):
    first = _live(tmp_path, centroids, monkeypatch, "full", locations)
    first_folder = tmp_path / first.summary["files"]["run_folder"]
    area_output = paths.output_dir("gtue", "full", tmp_path)
    area_test = paths.output_dir("gtue", "sample", tmp_path)

    same = gtue.ResponseCache(tmp_path)
    assert same.load_runs(area_output, "2026-10-01") > 0
    assert list(same.loaded_from) == [first_folder.name]
    assert gtue.ResponseCache(tmp_path).load_runs(area_output, "2026-10-02") == 0  # another day
    assert gtue.ResponseCache(tmp_path).load_runs(area_test, "2026-10-01") == 0  # another area (test_output)
    assert gtue.ResponseCache(tmp_path).load_runs(area_output, "2026-10-01", exclude=first_folder) == 0  # not itself


def _fake_summary(area, name, **fields):
    folder = area / name
    folder.mkdir(parents=True)
    (folder / "run_summary.json").write_text(json.dumps(fields), encoding="utf-8")
    return folder


def test_baseline_is_the_most_recent_pass_and_failed_runs_never_count(tmp_path):
    area = paths.output_dir("gtue", "full", tmp_path)
    ok = dict(mode="full", status="PASS", aborted=False, german_drop_alert=False)
    _fake_summary(area, "2026-09-01_0900_full", **ok, run_date="2026-09-01", started_at="2026-09-01T09:00:00", unique_german_locations=1000)
    _fake_summary(area, "2026-09-15_0900_full", **ok, run_date="2026-09-15", started_at="2026-09-15T09:00:00", unique_german_locations=1100)
    # newer, but FAIL (incomplete grid), aborted, or a rebuild: never a baseline
    _fake_summary(area, "2026-09-20_0900_full", mode="full", status="FAIL", aborted=False, german_drop_alert=False, run_date="2026-09-20", started_at="2026-09-20T09:00:00", unique_german_locations=1300)
    _fake_summary(area, "2026-09-21_0900_full", mode="full", status="FAIL", aborted=True, german_drop_alert=False, run_date="2026-09-21", started_at="2026-09-21T09:00:00", unique_german_locations=None)
    _fake_summary(area, "2026-09-22_0900_rebuild", mode="rebuild", run_date="2026-09-22", started_at="2026-09-22T09:00:00", unique_german_locations=1200)
    best = gtue._baseline(area)
    assert best["_file"] == "2026-09-15_0900_full" and best["unique_german_locations"] == 1100
    assert gtue._baseline(area, exclude=area / "2026-09-15_0900_full")["_file"] == "2026-09-01_0900_full"


def test_no_pass_run_means_no_baseline_and_the_run_does_not_fail(tmp_path, centroids, monkeypatch, locations):
    area = paths.output_dir("gtue", "full", tmp_path)
    _fake_summary(area, "2026-09-20_0900_full", mode="full", status="FAIL", aborted=False, german_drop_alert=False, run_date="2026-09-20", started_at="2026-09-20T09:00:00", unique_german_locations=1300)
    assert gtue._baseline(area) is None
    s = _live(tmp_path, centroids, monkeypatch, "full", locations).summary
    assert s["baseline"] is None and s["drop_check"] == "no baseline"
    assert s["status"] == "PASS" and s["alerts"] == []
