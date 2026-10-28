"""Request savers: re-query from the actual centre (A) and the same-day response cache (C), plus the
grid's stop hook. No network."""

import json
import math
from datetime import datetime

import pandas as pd
import pytest

from core import grid, paths
from gtue import adapter as gtue
from core.grid import GridResult, QueryResult, SimulatedFinder, run_grid
from test_grid import fake_client_class, point_grid, raw_lines, run_folders

POINTS = point_grid(48.0, 48.3, 11.3, 11.7)
T0 = datetime(2026, 10, 1, 9, 0)  # start time that names the run folders of the run tests below
BBOX = (48.05, 48.2, 11.4, 11.6)  # fits one 20 km square


# --------------------------------------------------------------------------
# A: re-query from the actual centre
# --------------------------------------------------------------------------

def _shifted(first_answer_short=False):
    """query_fn: the first query point is geocoded 10 km off (every time, like a real geocoder);
    all other points are centred on themselves."""
    calls = []

    def query_fn(point, radius_km):
        calls.append((point, radius_km))
        if point == calls[0][0]:
            if first_answer_short and len(calls) == 1:
                return QueryResult(20, 25, point.lat + 10 / 110.57, point.lng, {})
            return QueryResult(0, 0, point.lat + 10 / 110.57, point.lng, {})
        return QueryResult(0, 0, point.lat, point.lng, {})

    return query_fn, calls


def test_uncovered_but_complete_is_requeried_not_split():
    query_fn, calls = _shifted()
    result = run_grid(query_fn, POINTS, bbox=BBOX, top_size_km=20, min_size_km=5)
    assert [r["status"] for r in result.log] == ["requery", "complete"]
    assert len(calls) == 2  # not the 5 of the split path
    first, second = result.log
    assert calls[1][0] == calls[0][0]  # same query point (same PLZ, hence the same geocoded centre)
    assert calls[1][1] == second["radius_km"] == math.ceil(first["corner_dist_km"] + grid.RADIUS_MARGIN_KM)
    assert second["radius_km"] > first["radius_km"]
    assert "re-queried" in first["note"] and "re-queried" in second["note"]
    assert result.coverage_warnings == [] and result.squares_not_queried == 0
    # the requeried square's PLZ is listed once as a geocoding outlier (its final query), not twice
    outliers = grid.geocoding_outliers(result.log, 5)
    assert [o["seq"] for o in outliers] == [2] and outliers[0]["status"] == "complete"


def test_requery_that_still_overflows_splits():
    # the wider query finds more than the server returns -> the square is split as before
    def query_fn(point, radius_km):
        if query_fn.calls == 0:
            query_fn.calls += 1
            return QueryResult(0, 0, point.lat + 10 / 110.57, point.lng, {})
        query_fn.calls += 1
        if radius_km > 25:
            return QueryResult(20, 40, point.lat + 10 / 110.57, point.lng, {})
        return QueryResult(0, 0, point.lat, point.lng, {})

    query_fn.calls = 0
    result = run_grid(query_fn, POINTS, bbox=BBOX, top_size_km=20, min_size_km=5)
    statuses = [r["status"] for r in result.log]
    assert statuses[:2] == ["requery", "split"]
    assert query_fn.calls == 2 + 4 + 0  # requery, then the 4 children (no requery for them: complete)


def test_count_short_is_split_without_requery():
    query_fn, calls = _shifted(first_answer_short=True)
    result = run_grid(query_fn, POINTS, bbox=BBOX, top_size_km=20, min_size_km=5)
    assert result.log[0]["status"] == "split"
    assert "requery" not in [r["status"] for r in result.log]
    assert len(calls) == 5


def test_requery_disabled_or_at_query_limit_splits():
    query_fn, calls = _shifted()
    off = run_grid(query_fn, POINTS, bbox=BBOX, top_size_km=20, min_size_km=5, requery_uncovered=False)
    assert off.log[0]["status"] == "split" and len(calls) == 5

    query_fn, calls = _shifted()
    capped = run_grid(query_fn, POINTS, bbox=BBOX, top_size_km=20, min_size_km=5, max_queries=1)
    assert [r["status"] for r in capped.log] == ["split"]  # no room for the second query
    assert len(calls) == 1 and capped.squares_not_queried == 4


# --------------------------------------------------------------------------
# Grid hook for the early stop (the rule itself is in test_gtue_reference.py)
# --------------------------------------------------------------------------

def _finder(n=30):
    rows = [{"id": f"o{i}", "lat": 50.86 + i * 0.006, "lng": 6.9 + i * 0.007} for i in range(n)]
    return SimulatedFinder(pd.DataFrame(rows), center_offset_km=0.5, seed=1)


def test_stop_when_stops_the_grid_and_records_pending():
    points = point_grid(50.8, 51.1, 6.8, 7.2)
    bbox = (50.85, 51.05, 6.85, 7.15)
    full = run_grid(_finder().query, points, bbox=bbox, top_size_km=20, min_size_km=1)
    early = run_grid(
        _finder().query, points, bbox=bbox, top_size_km=20, min_size_km=1, stop_when=lambda r: len(r.items) >= 30
    )
    assert early.stopped_early and not full.stopped_early
    assert set(early.items) == set(full.items) and len(early.items) == 30
    assert len(early.log) < len(full.log)
    assert early.squares_not_queried > 0 and len(early.pending_squares) == early.squares_not_queried


def test_stop_when_already_true_sends_nothing():
    calls = []
    result = run_grid(lambda p, r: calls.append(p), POINTS, bbox=BBOX, top_size_km=20, stop_when=lambda r: True)
    assert calls == [] and result.stopped_early


def _grid_result(statuses, warnings=0, not_queried=0, stopped_early=False):
    result = GridResult()
    result.log = [{"status": st} for st in statuses]
    result.coverage_warnings = [{"reason": "Count 20 < MaxCount 25"}] * warnings
    result.squares_not_queried = not_queried
    result.stopped_early = stopped_early
    return result


def test_open_squares_lists_warnings_and_pending():
    result = GridResult()
    result.coverage_warnings = [{"square_km": 1.5, "lat_min": 1, "lat_max": 2, "lng_min": 3, "lng_max": 4, "zip": "10115",
                                 "reason": "Count 20 < MaxCount 30"}]
    result.pending_squares = [{"square_km": 3.1, "lat_min": 5, "lat_max": 6, "lng_min": 7, "lng_max": 8}]
    kinds = [s["kind"] for s in result.open_squares()]
    assert kinds == ["coverage warning", "not queried"]


def _run(tmp_path, centroids, monkeypatch, locations, day="2026-10-01", **kwargs):
    monkeypatch.setattr(gtue, "load_api_key", lambda root: "test-key")
    client = fake_client_class(locations, centroids)
    monkeypatch.setattr(gtue, "GtueClient", client)
    return gtue.run(
        mode="full", confirm_full_run=True, root=tmp_path, centroids=centroids, run_date=day, verbose=False, **kwargs
    )


def test_full_run_completes_the_grid_by_default(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    complete = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-02", use_cache=False)
    c = complete.summary
    assert c["completion"] == "grid complete" and c["status"] == "PASS"
    assert c["outcome"] == "PASS (grid complete + count match)"
    assert c["unique_locations"] == 1200 and c["squares"]["not_queried"] == 0 and c["open_squares"] == 0
    assert "open_squares" not in c["files"] and "grid-incomplete" not in c["files"]["all_csv"]
    assert c["early_stop"]["requested"] is False
    assert "GTÜ run summary (full): PASS (grid complete + count match)" in gtue.format_run_summary(c)


def test_abort_lists_the_open_squares(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    monkeypatch.setattr(gtue, "FULL_MAX_REQUESTS", 30)
    with pytest.raises(gtue.RequestLimitReached):
        _run(tmp_path, centroids, monkeypatch, locations, "2026-10-03", use_cache=False)
    s = _summary_of(tmp_path, "2026-10-03")
    assert s["aborted"] and s["squares"]["not_queried"] > 0  # not 0 any more
    assert s["outcome"].startswith("FAIL (run aborted; ")
    open_csv = pd.read_csv(tmp_path / s["files"]["open_squares"], sep=";", encoding="utf-8-sig")
    assert len(open_csv) == s["squares"]["not_queried"] == s["open_squares"]


# --------------------------------------------------------------------------
# C: same-day response cache
# --------------------------------------------------------------------------

def test_second_full_run_same_day_sends_only_the_reference_end_query(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    first = _run(tmp_path, centroids, monkeypatch, locations, started_at=T0)
    # no cache entries yet: every logged query was a live request (repeats inside the run are cache hits)
    assert first.summary["requests_made"] == (~first.query_log["cached"]).sum()
    assert first.summary["requests_made"] + first.summary["cache_hits"] == len(first.query_log)
    first_folder = tmp_path / first.summary["files"]["run_folder"]
    raw_files = [name for name, _ in raw_lines(first_folder)]

    second = _run(tmp_path, centroids, monkeypatch, locations, started_at=T0)
    s = second.summary
    # everything from today's raw files except the two reference queries, which are always live
    assert s["requests_made"] == 2
    live = second.query_log[~second.query_log["cached"]]
    assert list(live["status"]) == ["reference_check", "reference_end"]
    assert s["cache_hits"] == len(second.query_log) - 2 > 0
    assert s["cache_entries_loaded"] > 0
    assert [name for name, _ in raw_lines(first_folder)] == raw_files  # the first run's raw.jsonl is untouched
    second_folder = tmp_path / s["files"]["run_folder"]
    assert len(raw_lines(second_folder)) == 2  # only the two reference responses are new
    # the summary says where the cached answers came from
    assert s["cache_hits_by_run"] == {first_folder.name: s["cache_hits"]}
    assert s["cache_entries_loaded_from"] == {first_folder.name: s["cache_entries_loaded"]}
    assert first_folder.name in gtue.format_run_summary(s)
    assert set(second.df["source_id"]) == set(first.df["source_id"])
    assert s["status"] == "PASS"
    # cached rows still point at the raw file that holds the response
    assert second.query_log["raw_file"].notna().all()
    assert second_folder.name == "2026-10-01_0900_full_2"
    assert s["files"]["query_log"].endswith("2026-10-01_0900_full_2/query_log.csv")


def test_cache_is_per_date_and_can_be_switched_off(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    _run(tmp_path, centroids, monkeypatch, locations, "2026-10-01")
    other_day = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-02")
    assert other_day.summary["cache_entries_loaded"] == 0
    assert other_day.summary["requests_made"] > 2
    off = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-01", use_cache=False)
    assert off.summary["cache_hits"] == 0 and off.summary["requests_made"] > 2


def test_request_limit_counts_live_requests_only(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    first = _run(tmp_path, centroids, monkeypatch, locations)
    monkeypatch.setattr(gtue, "FULL_MAX_REQUESTS", 2)  # a limit the first run's query count would exceed
    second = _run(tmp_path, centroids, monkeypatch, locations)
    # all grid queries come from the cache; the only live requests are the two reference queries
    assert second.summary["status"] == "PASS" and second.summary["requests_made"] == 2
    assert len(second.query_log) == len(first.query_log)


def test_in_run_repeat_is_answered_from_memory(centroids):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    api = gtue.FakeGtueApi(locations, centroids, seed=1)
    cache = gtue.ResponseCache(".")
    query_fn = gtue.make_query_fn(api, None, cache)
    point = grid.QueryPoint("80686", "München", 48.13, 11.52)
    a, b = query_fn(point, 20), query_fn(point, 20)
    assert api.requests_made == 1 and cache.hits == 1
    assert not a.cached and b.cached
    assert (a.count, a.max_count, a.items.keys()) == (b.count, b.max_count, b.items.keys())
    query_fn(point, 21)  # another radius is another query
    assert api.requests_made == 2


def test_cache_ignores_unusable_log_entries(tmp_path):
    area = paths.output_dir("gtue", "full", tmp_path)
    run = area / "2026-10-01_0900_full"
    run.mkdir(parents=True)
    ref = "1_scrapers/gtue/output/2026-10-01_0900_full/raw.jsonl"
    (run / paths.RAW_FILE).write_text(
        json.dumps({"file": "0001.json", "response": {"Paging": {}}}) + "\n"
        + '{"file": "0002.json", "response": {broken' + "\n",  # a line that is not valid JSON
        encoding="utf-8",
    )
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(json.dumps({"file": "0001.json", "response": {}}) + "\n", encoding="utf-8")
    rows = [
        ["1", "80686", "München", "50000", "200", f"{ref}#0001.json"],  # usable
        ["2", "80687", "München", "50000", "200", f"{ref}#0002.json"],  # line is broken
        ["3", "80688", "München", "50000", "500", f"{ref}#0001.json"],  # not a 200
        ["4", "80689", "München", "50000", "200", ""],  # error row, no raw file
        ["5", "80690", "München", "50000", "200", "elsewhere.jsonl#0001.json"],  # outside output/
    ]
    columns = ["seq", "zip", "city", "radius_m", "http_status", "raw_file"]
    pd.DataFrame(rows, columns=columns).to_csv(run / paths.QUERY_LOG_FILE, sep=";", index=False, encoding="utf-8-sig")
    # a run of another date is never used, even with a usable entry
    other = area / "2026-10-02_0900_full"
    other.mkdir()
    (other / paths.RAW_FILE).write_text(json.dumps({"file": "0001.json", "response": {"Paging": {}}}) + "\n", encoding="utf-8")
    other_ref = "1_scrapers/gtue/output/2026-10-02_0900_full/raw.jsonl#0001.json"
    pd.DataFrame([["1", "99999", "München", "50000", "200", other_ref]], columns=columns).to_csv(
        other / paths.QUERY_LOG_FILE, sep=";", index=False, encoding="utf-8-sig"
    )

    cache = gtue.ResponseCache(tmp_path)
    assert cache.load_runs(area, "2026-10-01") == 2  # the usable one and the one with the broken line
    key = gtue.ResponseCache.key
    assert cache.get(key("80686", "München", 50000)) is not None
    assert cache.get(key("80687", "München", 50000)) is None  # unreadable -> miss, forgotten
    for zip in ("80688", "80689", "80690", "99999"):
        assert cache.get(key(zip, "München", 50000)) is None
    assert cache.hits == 1
    assert cache.hits_by_run == {"2026-10-01_0900_full": 1}


# --------------------------------------------------------------------------
# use_cache=False (fresh run) and max_requests
# --------------------------------------------------------------------------

def _snapshot(folder):
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in folder.iterdir()}


def test_use_cache_false_reads_nothing_from_the_cache(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    first = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-01", started_at=T0)  # fills today's cache
    old_raw = tmp_path / first.summary["files"]["run_folder"]
    before = _snapshot(old_raw)

    reads = []
    for name in ("__init__", "load_runs", "get", "put"):
        original = getattr(gtue.ResponseCache, name)

        def spy(self, *a, _name=name, _original=original, **kw):
            reads.append(_name)
            return _original(self, *a, **kw)

        monkeypatch.setattr(gtue.ResponseCache, name, spy)

    off = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-01", use_cache=False, started_at=T0)
    s = off.summary
    assert reads == []  # not even a cache object was created, no log was read
    assert s["cache"] == "off" and s["cache_hits"] == 0 and s["cache_entries_loaded"] == 0
    assert not off.query_log["cached"].any()
    # every logged query was a live request: nothing answered from the cache, not even repeats in the run
    assert s["requests_made"] == len(off.query_log) > 2
    assert s["requests_made"] > first.summary["requests_made"] - 1  # the same queries again, all live
    assert "cache: off" in gtue.format_run_summary(s)
    assert s["status"] == "PASS"

    # a run folder of its own (_2); the earlier one is untouched
    new_raw = tmp_path / s["files"]["run_folder"]
    assert s["files"]["raw_jsonl"] == "1_scrapers/gtue/output/2026-10-01_0900_full_2/raw.jsonl"
    assert len(raw_lines(new_raw)) == s["requests_made"]
    assert _snapshot(old_raw) == before
    assert off.query_log["raw_file"].str.startswith("1_scrapers/gtue/output/2026-10-01_0900_full_2/raw.jsonl#").all()

    third = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-01", use_cache=False, started_at=T0)
    assert third.summary["files"]["raw_jsonl"] == "1_scrapers/gtue/output/2026-10-01_0900_full_3/raw.jsonl"
    assert len(raw_lines(new_raw)) == s["requests_made"]  # the second fresh run's folder is untouched too


def test_first_fresh_run_of_a_day_uses_the_plain_date_folder(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    s = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-04", use_cache=False, started_at=datetime(2026, 10, 4, 9, 0)).summary
    assert s["files"]["raw_jsonl"] == "1_scrapers/gtue/output/2026-10-04_0900_full/raw.jsonl"


def test_cache_on_summary_says_so(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    s = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-05").summary
    assert s["cache"] == "on" and "cache: on" in gtue.format_run_summary(s)
    assert s["max_requests"] == gtue.FULL_MAX_REQUESTS and s["request_limit_reached"] is False


def _summary_of(tmp_path, day):
    folder = sorted(f for f in run_folders(tmp_path, "full") if f.name.startswith(day))[-1]
    return json.loads((folder / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))


def test_max_requests_limit_stops_the_run_and_never_passes(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    with pytest.raises(gtue.RequestLimitReached, match=r"max_requests=25\).*INCOMPLETE.*never PASS"):
        _run(tmp_path, centroids, monkeypatch, locations, "2026-10-06", use_cache=False, max_requests=25)
    s = _summary_of(tmp_path, "2026-10-06")
    assert s["status"] == "FAIL" and s["aborted"] and s["request_limit_reached"] is True
    assert s["max_requests"] == 25 and s["requests_made"] == 25
    assert s["outcome"].startswith("FAIL (run aborted")
    assert any("REQUEST LIMIT REACHED" in a for a in s["alerts"])
    assert not any(k in s["files"] for k in ("all_csv", "all_xlsx"))  # no results file that looks complete


def test_limit_hit_only_by_the_final_reference_recheck_still_fails(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    full = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-07", use_cache=False)
    needed = full.summary["requests_made"]  # grid + reference + the end recheck
    # exactly enough for everything but the end recheck: the grid is complete, the recheck is swallowed
    # by grid.reference_recheck, and the run must still not PASS
    with pytest.raises(gtue.RequestLimitReached):
        _run(tmp_path, centroids, monkeypatch, locations, "2026-10-08", use_cache=False, max_requests=needed - 1)
    s = _summary_of(tmp_path, "2026-10-08")
    assert s["status"] == "FAIL" and s["request_limit_reached"] is True and s["requests_made"] == needed - 1
    # exactly enough for all of it: no limit hit, PASS
    ok = _run(tmp_path, centroids, monkeypatch, locations, "2026-10-09", use_cache=False, max_requests=needed)
    assert ok.summary["status"] == "PASS" and ok.summary["request_limit_reached"] is False


def test_max_requests_default_is_the_full_limit_and_is_validated(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    assert gtue.FULL_MAX_REQUESTS == 2000
    for bad in (0, -5, 2.5, True, "2000"):
        with pytest.raises(ValueError, match="max_requests"):
            _run(tmp_path, centroids, monkeypatch, locations, "2026-10-10", max_requests=bad)
