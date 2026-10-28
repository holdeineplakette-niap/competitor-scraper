"""core.export: no-overwrite exports, baseline run and drop check."""

import json

import pandas as pd
import pytest

from core import export, paths


@pytest.fixture
def frame():
    return pd.DataFrame(
        {"location_id": ["X-1", "X-2"], "source_id": ["007", "008"], "zip": ["01067", "80636"], "in_scope": [True, False], "lat": [51.0, None]}
    )


def test_export_writes_all_and_in_scope_files_without_overwriting(frame, tmp_path):
    first = export.export(frame, tmp_path, "kues", "2026-10-05")
    assert first["all_csv"][1] == 2 and first["pruefstellen_csv"][1] == 1
    assert (tmp_path / "kues_all_2026-10-05.csv").exists() and (tmp_path / "kues_pruefstellen_2026-10-05.xlsx").exists()
    renamed: list = []
    second = export.export(frame, tmp_path, "kues", "2026-10-05", renamed, only=("all_csv",))
    assert second["all_csv"][0].name == "kues_all_2026-10-05_2.csv" and renamed


def test_export_tag_and_only_subset(frame, tmp_path):
    result = export.export(frame, tmp_path, "kues", "2026-10-05", tag="unverified", only=("pruefstellen_csv",))
    assert list(result) == ["pruefstellen_csv"]
    assert result["pruefstellen_csv"][0].name == "kues_pruefstellen_2026-10-05_unverified.csv"
    with pytest.raises(ValueError):
        export.export(frame, tmp_path, "kues", only=("nope",))


def test_xlsx_keeps_ids_and_plz_as_text(frame, tmp_path):
    path = export.export(frame, tmp_path, "kues", "2026-10-05", only=("all_xlsx",))["all_xlsx"][0]
    back = pd.read_excel(path, dtype=object)
    assert back["zip"].tolist() == ["01067", "80636"] and back["source_id"].tolist() == ["007", "008"]


def _summary(folder, **fields):
    folder.mkdir(parents=True)
    (folder / paths.RUN_SUMMARY_JSON).write_text(json.dumps(fields), encoding="utf-8")


def test_baseline_is_the_newest_earlier_passing_full_run(tmp_path):
    out = tmp_path / "output"
    _summary(out / "2026-10-01_0900_full", mode="full", status="PASS", in_scope=100, run_date="2026-10-01")
    _summary(out / "2026-10-02_0900_full", mode="full", status="FAIL", in_scope=5)
    _summary(out / "2026-10-03_0900_rebuild", mode="rebuild", status="PASS", in_scope=7)
    _summary(out / "2026-10-04_0900_full", mode="full", status="PASS", in_scope=90, run_date="2026-10-04")
    assert export.baseline_run(out)["run_folder"] == "2026-10-04_0900_full"
    assert export.baseline_run(out, exclude=out / "2026-10-04_0900_full")["in_scope"] == 100
    assert export.baseline_run(tmp_path / "missing") is None


def test_drop_check_alerts_below_minus_ten_percent():
    base = {"run_folder": "r", "in_scope": 100}
    assert export.drop_check(95, base)[1] is False
    text, alert = export.drop_check(89, base)
    assert alert and text.startswith("ALERT")
    assert export.drop_check(50, None) == ("no baseline (no earlier passing full run)", False)
