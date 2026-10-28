"""Notebook helpers: loading a run and comparing two runs."""

import pandas as pd

from kues import report
from kues import run as kues_run


def result(rows):
    frame = pd.DataFrame(rows)
    return kues_run.RunResult(run_folder=None, summary={}, frame=frame, files={})


BASE = [
    {"source_id": "1", "name": "A", "zip": "80636", "lat": 48.1, "in_scope": True, "kues_list": "both"},
    {"source_id": "2", "name": "B", "zip": "44135", "lat": None, "in_scope": False, "kues_list": "partnersuche"},
]


def test_identical_runs_agree(capsys):
    out = report.compare_runs(result(BASE), result([dict(r) for r in BASE]), "50 km", "100 km")
    assert out["same"] and out["only_in_a"] == [] and out["only_in_b"] == [] and out["differences"].empty
    assert "agree completely" in capsys.readouterr().out


def test_missing_records_and_changed_content_are_listed():
    other = [dict(BASE[0], name="A changed"), {"source_id": "3", "name": "C", "zip": "10115", "lat": 52.5, "in_scope": True, "kues_list": "both"}]
    out = report.compare_runs(result(BASE), result(other), "50", "100")
    assert not out["same"] and out["only_in_a"] == ["2"] and out["only_in_b"] == ["3"]
    assert out["differences"].to_dict("records") == [{"source_id": "1", "field": "name", "50": "A", "100": "A changed"}]
