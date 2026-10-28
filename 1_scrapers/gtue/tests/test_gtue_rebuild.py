"""Abort bookkeeping of the grid and the offline rebuild from raw. No network."""

import json
from datetime import datetime

import pandas as pd
import pytest

from core import grid, paths
from gtue import adapter as gtue
from core.grid import GridResult, SimulatedFinder, StopRun, run_grid
from test_grid import point_grid

BBOX = (50.85, 51.05, 6.85, 7.15)


def _finder():
    rows = [{"id": f"o{i}", "lat": 50.86 + i * 0.006, "lng": 6.9 + i * 0.007} for i in range(30)]
    return SimulatedFinder(pd.DataFrame(rows), center_offset_km=0.5, seed=1)


@pytest.mark.parametrize("abort_at", [1, 2, 7])
def test_abort_counts_the_pending_squares(abort_at):
    """An abort on the n-th query leaves the same squares pending as stopping after n-1 queries
    (max_queries), plus none lost: the failed square counts as pending, the queue too."""
    points = point_grid(50.8, 51.1, 6.8, 7.2)
    finder = _finder()
    calls = []

    def query_fn(point, radius_km):
        calls.append(point)
        if len(calls) == abort_at:
            raise StopRun("limit")
        return finder.query(point, radius_km)

    aborted = GridResult()
    with pytest.raises(StopRun):
        run_grid(query_fn, points, bbox=BBOX, top_size_km=20, min_size_km=1, result=aborted)
    stopped = run_grid(finder.query, points, bbox=BBOX, top_size_km=20, min_size_km=1, max_queries=abort_at - 1)

    assert aborted.log[-1]["status"] == "error"
    assert aborted.squares_not_queried > 0
    assert aborted.squares_not_queried == stopped.squares_not_queried


def test_abort_on_first_query_leaves_all_top_squares_pending():
    points = point_grid(50.8, 51.1, 6.8, 7.2)

    def query_fn(point, radius_km):
        raise StopRun("key rejected")

    result = GridResult()
    with pytest.raises(StopRun):
        run_grid(query_fn, points, bbox=BBOX, top_size_km=20, result=result)
    index = grid.PointIndex(points)
    expected = sum(index.any_near(s, grid.SKIP_BUFFER_KM) for s in grid.initial_squares(BBOX, 20))
    assert result.squares_not_queried == expected > 1


def test_squares_that_would_be_skipped_are_not_pending():
    # query points only near the first top-level square: the other 3 are skipped, not pending;
    # after the first query its 4 children are what is left
    points = point_grid(50.85, 50.9, 6.85, 6.9)
    result = run_grid(_finder().query, points, bbox=BBOX, top_size_km=20, max_queries=1)
    assert result.squares_skipped == 3
    assert result.squares_not_queried == 4


# --------------------------------------------------------------------------
# Rebuild from raw
# --------------------------------------------------------------------------

def _response(items, max_count=None, centre=(48.1, 11.5)):
    n = len(items)
    return {
        "Paging": {"Count": n, "MaxCount": max_count if max_count is not None else n},
        "SearchData": {"Location": {"XCoord": str(centre[1]), "YCoord": str(centre[0])}},
        "AddressItems": items,
        "Exceptions": [],
        "InternalError": None,
    }


def _source_run(root, name, responses):
    """A finished run's folder with a raw.jsonl: {"file": "<nnnn>.json", "response": ...} per request."""
    folder = paths.output_dir("gtue", "full", root) / name
    folder.mkdir(parents=True)
    lines = [json.dumps({"file": f"{i:04d}.json", "response": r}) for i, r in enumerate(responses, start=1)]
    (folder / paths.RAW_FILE).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return folder


@pytest.fixture
def raw_run(tmp_path, centroids):
    locations = gtue.simulated_locations(centroids, n_total=12, seed=3, clusters={})
    items = [gtue._fake_item(row) for row in locations.itertuples(index=False)]
    # 0001 = national check (all 12), 0002/0003 overlap it, 0004 is broken
    raw = _source_run(
        tmp_path, "2026-09-29_1019_full",
        [_response(items, 12), _response(items[:5]), _response(items[4:9]), "{not json"],
    )
    return tmp_path, raw, locations


def test_rebuild_dedupes_labels_and_never_overwrites(raw_run, centroids, monkeypatch):
    import requests

    def no_network(*a, **k):
        raise AssertionError("rebuild must not touch the network")

    monkeypatch.setattr(requests.Session, "request", no_network)

    root, raw, locations = raw_run
    at = datetime(2026, 9, 29, 16, 19)
    result = gtue.rebuild_from_raw(raw, centroids, root=root, verbose=False, started_at=at)
    s = result.summary

    assert s["grid_status"] == "incomplete"
    assert s["raw_files_read"] == 3 and s["raw_files_unreadable"] == ["0004.json"]
    assert s["items_seen"] == 12 + 5 + 5
    assert s["unique_locations"] == 12 == len(result.df)
    # 0001 is centred on München, not on the reference: there is no circle to count in
    assert s["first_response"]["max_count"] == 12 and not s["first_response"]["is_reference_query"]
    assert s["count_check"]["evaluated"] is False
    assert s["in_scope"] == int(result.df["in_scope"].sum()) > 0
    assert s["countries"] == {"D": 12}

    # a run folder of its own (mode rebuild), next to its source run in output/, without a copy of raw.jsonl
    folder = root / s["files"]["run_folder"]
    assert folder.parent == raw.parent and folder.name.endswith("_rebuild")
    assert s["mode"] == "rebuild" and s["source_run"] == raw.name
    for key in ("all_csv", "all_xlsx", "pruefstellen_csv", "pruefstellen_xlsx"):
        path = root / s["files"][key]
        assert path.exists() and path.parent == folder
        assert "grid-incomplete" in path.name
    assert (folder / paths.RUN_SUMMARY_MD).exists() and not (folder / paths.RAW_FILE).exists()
    assert json.loads((folder / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))["source_run"] == raw.name
    assert raw.name in (folder / paths.RUN_SUMMARY_MD).read_text(encoding="utf-8")

    text = gtue.format_rebuild_summary(s)
    assert "GRID INCOMPLETE" in text and "In scope" in text and "Needs review" in text

    # second call in the same minute: nothing is overwritten, the new run folder gets _2
    first_csv = root / s["files"]["all_csv"]
    before = first_csv.read_bytes()
    again = gtue.rebuild_from_raw(raw, centroids, root=root, verbose=False, started_at=at).summary
    assert first_csv.read_bytes() == before
    assert again["files"]["all_csv"].endswith("2026-09-29_1619_rebuild_2/gtue_all_2026-09-29_grid-incomplete.csv")
    assert again["renamed_files"] == []
    assert sorted(f.name for f in raw.parent.glob("*_rebuild*")) == ["2026-09-29_1619_rebuild", "2026-09-29_1619_rebuild_2"]


def test_rebuild_counts_in_the_reference_circle_when_0001_is_the_reference(tmp_path, centroids):
    locations = gtue.simulated_locations(centroids, n_total=12, seed=3, clusters={})
    items = [gtue._fake_item(row) for row in locations.itertuples(index=False)]
    raw = _source_run(
        tmp_path, "2026-10-05_0900_full",
        [_response(items, 12, centre=gtue.REFERENCE_CENTRE), _response(items[:4])],
    )
    s = gtue.rebuild_from_raw(raw, centroids, root=tmp_path, verbose=False).summary
    assert s["first_response"]["is_reference_query"]
    c = s["count_check"]
    assert c["evaluated"] and c["n_circle"] == 12 and c["verdict"] == "match" and c["german_total"] == 12
    assert "N_circle 12" in gtue.format_rebuild_summary(s)

    (raw / paths.RAW_FILE).unlink()
    (raw / paths.RAW_FILE).write_text(
        "".join(
            json.dumps({"file": f"{i:04d}.json", "response": r}) + "\n"
            for i, r in enumerate([_response(items, 15, centre=gtue.REFERENCE_CENTRE), _response(items[:4])], start=1)
        ),
        encoding="utf-8",
    )
    again = gtue.rebuild_from_raw(raw, centroids, root=tmp_path, verbose=False).summary
    assert again["count_check"]["verdict"] == "gap" and again["count_check"]["gap"] == 3


def test_rebuild_missing_folder_or_empty(tmp_path, centroids):
    with pytest.raises(FileNotFoundError):
        gtue.rebuild_from_raw(tmp_path / "nope", centroids, root=tmp_path, verbose=False)
    empty = _source_run(tmp_path, "2026-01-01_0900_full", [])
    with pytest.raises(ValueError, match="No usable locations"):
        gtue.rebuild_from_raw(empty, centroids, root=tmp_path, verbose=False)
