"""The notebook helpers: loading a run, the tables and the maps."""

from datetime import datetime

import pandas as pd
import pytest

from tuev_sued import report
from tuev_sued import run as tuev_run

START = datetime(2026, 10, 5, 12, 0)


@pytest.fixture(scope="module")
def result(centroids, tmp_path_factory):
    root = tmp_path_factory.mktemp("tuev_report")
    return tuev_run.run(mode="simulated", root=root, started_at=START, centroids=centroids, verbose=False, exports=tuev_run.EXPORT_KEYS), root


def test_load_run_returns_the_summary_and_the_frame(result):
    built, root = result
    loaded = report.load_run(built.run_folder.name, root=root)
    assert loaded.run_folder == built.run_folder and loaded.summary == built.summary
    assert len(loaded.frame) == built.summary["rows_total"] and loaded.frame["location_sign"].str.len().min() >= 3  # text columns stay text
    assert loaded.frame["zip"].str.len().eq(5).all()


def test_tables(result):
    built, _ = result
    s = built.summary
    rules = report.rule_table(s)
    assert rules["result"].tolist() == ["ok", "ok"] and rules["issues"].sum() == 0
    counts = report.counts_table(built)
    assert counts["collected (unique Service-Center)"] == 350 and counts["passes"] == len(s["passes"])
    states = report.per_state(built.frame)
    assert states["all"].sum() == 350 and states["in_scope"].sum() == s["in_scope"]
    assert len(report.pass_table(s)) == len(s["passes"]) and report.published_comparison(s)["our_in_scope"].eq(s["in_scope"]).all()
    fill = report.key_fill_table(s)
    assert fill.loc["tscId", "rate"] == 1.0 and fill["rate"].is_monotonic_decreasing


def test_rule_table_shows_failures():
    summary = {"coverage": {"cover_margin_km": 15.0, "uncovered": {"items": ["1"], "total": 1},
                            "premise": {"ok": False, "misses": {"items": [], "total": 2}, "unsorted_answers": {"items": [], "total": 1}}}}
    rules = report.rule_table(summary)
    assert rules["result"].tolist() == ["FAIL", "FAIL"] and rules["issues"].tolist() == [1, 3]


def test_review_tables(result):
    built, _ = result
    tables = report.review_tables(built.frame)
    assert len(tables["uo_notice"]) == 1
    assert tables["by_operator"]["all"].sum() == built.summary["rows_total"] and tables["by_operator"]["in_scope"].sum() == built.summary["in_scope"]
    assert tables["needs_review_by_reason"]["rows"].sum() >= built.summary["needs_review"]
    assert report.review_tables(pd.DataFrame()) == {}


def test_maps_draw_without_error(result, centroids):
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    built, _ = result
    first = tuev_run.estimate(centroids)["planned_points"]
    for fig in (report.planned_map(centroids, first), report.coverage_map(built, centroids), report.sites_map(built.frame)):
        assert fig.axes
        matplotlib.pyplot.close(fig)
