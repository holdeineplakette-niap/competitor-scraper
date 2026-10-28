"""Offline tests for hdp.run: PASS and every hard-fail condition, run folder files, baseline warning, rebuild, command line."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest
from core import paths

from hdp import fetch, run as hdp_run
from hdp.simulate import SimulatedHdp, sample_location, sample_payload

T1 = datetime(2026, 10, 7, 9, 0)


def go(tmp_path, payload=None, mode="full", at=T1, **kw):
    client = kw.pop("client", None) or SimulatedHdp(payload)
    return hdp_run.run(mode, confirm_full_run=True, root=tmp_path, now=at, client=client, **kw)


def summary_of(res):
    return json.loads((res.run_dir / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))


def test_pass_writes_the_site_csv_raw_log_and_summary(tmp_path):
    res = go(tmp_path)
    assert res.outcome == "PASS" and len(res.frame) == 4
    names = sorted(p.name for p in res.run_dir.iterdir())
    assert names == ["hdp_sites_2026-10-07.csv", "query_log.csv", "raw.jsonl", "run_summary.json", "run_summary.md"]
    s = summary_of(res)
    assert s["status"] == "PASS" and s["n_sites"] == 4 and s["in_scope"] == 4 and s["written"]["sites_csv"]["rows"] == 4
    assert json.loads((res.run_dir / "raw.jsonl").read_text(encoding="utf-8").splitlines()[0])["file"] == "getAllPublicLocations"
    assert res.run_dir.parent == paths.output_dir("hdp", "full", tmp_path)


def test_a_full_run_needs_the_confirm_flag_and_sends_nothing_without_it(tmp_path):
    client = SimulatedHdp()
    with pytest.raises(RuntimeError, match="confirm_full_run"):
        hdp_run.run("full", root=tmp_path, client=client)
    assert client.requests_made == 0 and not (tmp_path / "1_scrapers").exists()


@pytest.mark.parametrize("payload, failed", [
    ({"result": {"success": True, "locations": []}}, "result.locations is a non-empty list"),  # the scraper returns 0 sites
    ({"result": {"success": True}}, "result.locations is a non-empty list"),
    ({"result": {"success": False, "locations": [sample_location()]}}, "result.success is true"),
    ({"error": {"message": "internal"}}, "result.success is true"),
])
def test_an_unusable_answer_fails_the_run_and_writes_no_site_csv(tmp_path, payload, failed):
    res = go(tmp_path, payload)
    assert res.outcome == "FAIL" and failed in res.summary["failed_hard"]
    assert res.written == {} and not list(res.run_dir.glob("hdp_sites*.csv"))
    assert summary_of(res)["status"] == "FAIL"  # the summary is what the aggregator reads: it must say FAIL


def test_a_location_without_postcode_or_coordinates_fails_the_run_and_marks_the_csv(tmp_path):
    for minute, (bad, check) in enumerate(((sample_location(1, postcode=""), "every location has a postcode"),
                                           (sample_location(1, latitude=None), "every location has numeric coordinates"))):
        res = go(tmp_path, {"result": {"success": True, "locations": [sample_location(0), bad]}}, at=datetime(2026, 10, 7, 9, minute))
        assert res.outcome.startswith("FAIL") and check in res.summary["failed_hard"]
        assert [p.name for p in res.run_dir.glob("hdp_sites*.csv")] == ["hdp_sites_unverified_2026-10-07.csv"]


def test_a_failed_request_fails_the_run_with_the_reason_and_no_csv(tmp_path):
    res = go(tmp_path, client=SimulatedHdp(error=fetch.FetchError("failed after 3 retries (HTTP 503)")))
    assert res.outcome.startswith("FAIL") and "after 3 retries" in res.summary["error"] and res.written == {}
    s = summary_of(res)
    assert s["status"] == "FAIL" and s["n_sites"] is None


def test_a_failed_run_logs_an_error_and_the_command_line_exits_non_zero(tmp_path, caplog, monkeypatch, capsys):
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    monkeypatch.setattr(hdp_run, "HdpClient", lambda: SimulatedHdp({"result": {"success": True, "locations": []}}))
    with caplog.at_level(logging.ERROR, logger="hdp"):
        code = hdp_run.main(["full", "--confirm"])
    assert code == 1 and any(r.levelname == "ERROR" for r in caplog.records)
    assert "ERROR:" in capsys.readouterr().out


def test_the_command_line_exits_zero_on_pass_and_two_when_not_started(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    monkeypatch.setattr(hdp_run, "HdpClient", lambda: SimulatedHdp())
    assert hdp_run.main(["full", "--confirm"]) == 0
    assert hdp_run.main(["full"]) == 2


def test_a_count_that_differs_from_the_last_passing_run_is_only_a_warning(tmp_path):
    first = go(tmp_path, sample_payload(4))
    assert first.outcome == "PASS" and first.summary["warnings"] == []
    second = go(tmp_path, sample_payload(3), at=datetime(2026, 10, 8, 9, 0))
    assert second.outcome == "PASS" and len(second.summary["warnings"]) == 1
    assert "4 in " in second.summary["warnings"][0] and "-> 3 now" in second.summary["warnings"][0]
    third = go(tmp_path, sample_payload(3), at=datetime(2026, 10, 9, 9, 0))
    assert third.summary["warnings"] == []  # 3 -> 3: nothing to warn about, and 19 is never hard-coded


def test_the_count_is_never_a_pass_condition(tmp_path):
    for n, day in ((1, 1), (4, 2)):
        assert go(tmp_path, sample_payload(n), at=datetime(2026, 10, day, 9, 0)).outcome == "PASS"


def test_a_simulated_run_goes_to_test_output(tmp_path):
    res = hdp_run.run("simulated", root=tmp_path, now=T1)
    assert res.outcome == "PASS" and res.run_dir.parent == paths.output_dir("hdp", "simulated", tmp_path)


def test_rebuild_reproduces_the_rows_offline_and_keeps_the_age_of_the_data(tmp_path):
    src = go(tmp_path)
    later = datetime(2026, 10, 20, 9, 0)
    rb = hdp_run.run("rebuild", root=tmp_path, now=later, source_runs=[src.run_dir])
    assert rb.outcome == "PASS" and rb.frame.drop(columns="scraped_at").equals(src.frame.drop(columns="scraped_at"))
    s = summary_of(rb)
    assert s["requests_made"] == 0 and s["data_started"].startswith("2026-10-07") and s["started"].startswith("2026-10-20")
    assert "raw.jsonl" not in [p.name for p in rb.run_dir.iterdir()]


def test_two_runs_in_the_same_minute_never_share_a_folder(tmp_path):
    a, b = go(tmp_path), go(tmp_path)
    assert a.run_dir != b.run_dir and b.run_dir.name.endswith("_2")
