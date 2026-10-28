"""Offline tests for fsp.run (production modes) against the simulated fsp.de, including runs that must NOT pass."""

from __future__ import annotations

import csv
import gzip
import json
from datetime import datetime

import pytest

from fsp import run as fsp_run
from fsp.fetch import BlockedError
from fsp.recon import PaginationError
from fsp.robots import RobotsDisallowedError
from fsp.simulate import EXPECTED_IN_SCOPE, SimulatedFsp

T0 = datetime(2026, 10, 6, 12, 0)


def sim_run(tmp_path, fault=None, now=T0, **kw):
    kw.setdefault("allow_robots_disallowed", True)
    return fsp_run.run(mode="simulated", root=tmp_path, now=now, client=SimulatedFsp(fault), **kw)


def read(path):
    return list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))


def test_simulated_run_passes_and_finds_every_known_site(tmp_path):
    res = sim_run(tmp_path)
    assert res.outcome == "PASS", res.summary["checks"]
    assert len(res.frame) == EXPECTED_IN_SCOPE == 31
    rows = read(res.written["pruefstellen_csv"][0])
    assert res.written["pruefstellen_csv"][0].name == "fsp_pruefstellen_2026-10-06.csv"
    ids = {r["source_id"] for r in rows}
    assert "kfz-pruefstuetzpunkt-sim" in ids  # sitemap-only, HU on its page: counts
    assert "kfz-sachverstaendigenbuero-sim-nohu" not in ids  # sitemap-only, no HU: out
    assert "kfz-sachverstaendigenbuero-sim-x" not in ids  # Prüfstelle badge but not in the HU list
    assert not any("Gutachtenbüro" == r["type_badges"] for r in rows)
    by = {r["source_id"]: r for r in rows}
    assert by["kfz-pruefstuetzpunkt-sim"]["scope_reason"] == "detail_page_hu"
    assert "not_in_locator" in by["kfz-pruefstuetzpunkt-sim"]["doubt_flags"]
    assert by["pruefstelle-sim-01"]["location_id"] == "FSP-pruefstelle-sim-01"
    assert by["pruefstelle-sim-01"]["scope_reason"] == "hu_list" and by["pruefstelle-sim-01"]["doubt_flags"] == ""


def test_simulated_run_writes_only_the_minimum(tmp_path):
    res = sim_run(tmp_path)
    names = sorted(p.name for p in res.run_dir.iterdir())
    assert names == ["fsp_pruefstellen_2026-10-06.csv", "query_log.csv", "raw", "run_summary.json", "run_summary.md"]
    assert "test_output" in str(res.run_dir)
    assert sorted(p.name for p in (res.run_dir / "raw").iterdir()) == ["detail", "pages", "robots.txt", "sitemap"]
    with gzip.open(res.run_dir / "raw" / "pages" / "hu_page_01.html.gz", "rt", encoding="utf-8") as f:
        assert "location card" in f.read()  # list pages are stored gzipped


def test_save_raw_false_writes_no_raw(tmp_path):
    res = sim_run(tmp_path, save_raw=False)
    assert not (res.run_dir / "raw").exists() and res.outcome == "PASS"


def test_hu_list_without_robots_override_is_refused_and_nothing_disallowed_is_requested(tmp_path):
    fake = SimulatedFsp()
    with pytest.raises(RobotsDisallowedError):
        fsp_run.run(mode="simulated", root=tmp_path, now=T0, client=fake)
    assert not any(p.get("d") for _, p in fake.urls)
    summary = json.loads(next(tmp_path.rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["outcome"] == "FAIL" and "disallowed by robots.txt" in summary["error"]


@pytest.mark.parametrize("fault,exc", [("same_page", PaginationError), ("blocked", BlockedError)])
def test_faulty_server_stops_the_run_with_a_fail_summary(tmp_path, fault, exc):
    with pytest.raises(exc):
        sim_run(tmp_path, fault)
    summary = json.loads(next(tmp_path.rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["outcome"] == "FAIL" and summary["error"]
    assert not list(tmp_path.rglob("fsp_pruefstellen*.csv"))  # no export from a stopped run


def test_a_silently_dropped_hu_page_fails_the_run_and_marks_the_export(tmp_path):
    res = sim_run(tmp_path, "drop_hu_page")
    assert res.outcome == "FAIL"
    failed = [c["check"] for c in res.summary["checks"] if not c["ok"]]
    assert "hu: pagination bar count equals pages returned" in failed
    assert res.written["pruefstellen_csv"][0].name == "fsp_pruefstellen_unverified_2026-10-06.csv"


def test_unreadable_sitemap_only_page_fails_the_run(tmp_path):
    res = sim_run(tmp_path, "no_sitemap_only_detail")
    assert res.outcome == "FAIL"
    assert "every sitemap-only page could be read" in [c["check"] for c in res.summary["checks"] if not c["ok"]]


def test_full_run_needs_confirmation_and_sends_nothing_without_it(tmp_path):
    fake = SimulatedFsp()
    with pytest.raises(RuntimeError, match="confirm_full_run=True"):
        fsp_run.run(mode="full", root=tmp_path, client=fake)
    assert fake.requests_made == 0 and not list(tmp_path.rglob("*"))


def test_full_run_goes_to_output_and_sample_to_test_output(tmp_path):
    full = fsp_run.run(mode="full", confirm_full_run=True, root=tmp_path, now=T0, client=SimulatedFsp(),
                       allow_robots_disallowed=True)
    assert full.run_dir.parent.name == "output" and full.run_dir.name == "2026-10-06_1200_full"
    sample = fsp_run.run(mode="sample", root=tmp_path, now=T0, client=SimulatedFsp(), allow_robots_disallowed=True)
    assert sample.outcome == "SAMPLE" and sample.run_dir.parent.name == "test_output"
    assert len(sample.frame) == 24  # 2 pages x 12; never PASS, never a completeness claim


def test_rebuild_reproduces_the_run_offline(tmp_path):
    live = sim_run(tmp_path)
    rb = fsp_run.run(mode="rebuild", root=tmp_path, now=datetime(2026, 10, 6, 13, 0), source_runs=[live.run_dir],
                     allow_robots_disallowed=True)
    assert rb.outcome == "PASS" and rb.summary["requests_made"] == 0
    assert rb.run_dir.parent.name == "output" and not (rb.run_dir / "raw").exists()
    assert list(rb.frame["source_id"]) == list(live.frame["source_id"])
    assert rb.summary["source_runs"] == [str(live.run_dir)]


def test_drop_check_alerts_against_the_last_passing_full_run(tmp_path):
    first = fsp_run.run(mode="full", confirm_full_run=True, root=tmp_path, now=T0, client=SimulatedFsp(),
                        allow_robots_disallowed=True)
    s = json.loads((first.run_dir / "run_summary.json").read_text(encoding="utf-8"))
    s["counts"]["in_scope"] = 100  # pretend the earlier passing run had many more sites
    (first.run_dir / "run_summary.json").write_text(json.dumps(s), encoding="utf-8")
    second = fsp_run.run(mode="full", confirm_full_run=True, root=tmp_path, now=datetime(2026, 11, 6, 12, 0),
                         client=SimulatedFsp(), allow_robots_disallowed=True)
    assert any("DROP ALERT" in w for w in second.summary["warnings"])
    assert second.summary["drop_check"]["baseline_in_scope"] == 100


def test_doubt_flags():
    f = fsp_run.doubt_flags({"plz": "553340", "street": "", "name": "Prüfstützpunkt X", "slug": "x", "phone": "", "email": ""})
    assert f == ["plz_not_5_digits", "no_street", "named_pruefstuetzpunkt", "no_contact"]
    assert fsp_run.doubt_flags({"plz": "12345", "street": "A 1", "name": "Prüfstelle Y", "slug": "y", "phone": "1", "email": ""}) == []
