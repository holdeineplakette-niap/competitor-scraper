"""Grid, fetch client and run modes. No network: fake query functions and fake HTTP sessions only."""

import json
from datetime import datetime

import pandas as pd
import pytest
import requests

from core import grid, paths
from gtue import adapter as gtue
from core.grid import GridResult, QueryResult, SimulatedFinder, run_grid


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def run_folders(root, mode, slug="gtue"):
    """The run folders of a mode under root, oldest name first."""
    return sorted(p for p in paths.output_dir(slug, mode, root).glob("*") if p.is_dir())


def raw_lines(folder):
    """(file name, response) of every raw.jsonl line in a run folder."""
    return [(r["file"], r["response"]) for r in map(json.loads, (folder / paths.RAW_FILE).read_text(encoding="utf-8").splitlines())]


def point_grid(lat_min, lat_max, lng_min, lng_max, step=0.02) -> pd.DataFrame:
    """Synthetic query points every ~2 km, so no square is skipped."""
    rows = []
    lat = lat_min
    while lat <= lat_max:
        lng = lng_min
        while lng <= lng_max:
            rows.append({"zip": f"{len(rows):05d}", "city": "Test", "lat": lat, "lng": lng})
            lng += step
        lat += step
    return pd.DataFrame(rows)


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Returns (or raises) the queued responses in order and records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def ok_payload(count=0, max_count=0):
    return {
        "Paging": {"Count": count, "MaxCount": max_count},
        "SearchData": {"Location": {"XCoord": "11.5", "YCoord": "48.1"}},
        "AddressItems": [],
        "Exceptions": [],
        "InternalError": None,
    }


def client(responses):
    session = FakeSession(responses)
    sleeps = []
    c = gtue.GtueClient("test-key", session=session, sleep=sleeps.append, pace_s=0, jitter_s=0)
    return c, session, sleeps


def fake_client_class(locations, centroids, foreign_ids=(), reference_extra=0, fail_at=None):
    """Stands in for GtueClient in sample/full runs: simulated responses, same hard request
    limit. Items in foreign_ids get Address.Country "A" (like entries abroad).
    reference_extra is added to the reference query's MaxCount (a count that doesn't match what
    was collected); fail_at=n makes request n raise ApiError."""

    class FakeClient(gtue.FakeGtueApi):
        def __init__(self, api_key, max_requests=None):
            super().__init__(locations, centroids, seed=1)
            self.max_requests = max_requests
            for i in foreign_ids:
                self.items[i]["BasicData"]["Address"]["Country"] = "A"

        def query(self, zip, city, radius_m):
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise gtue.RequestLimitReached("limit")
            if fail_at is not None and self.requests_made + 1 == fail_at:
                self.requests_made += 1
                raise gtue.ApiError(f"GTÜ API returned HTTP 500 (PLZ {zip}, attempt 3)")
            data = super().query(zip, city, radius_m)
            if radius_m == gtue.REFERENCE_RADIUS_KM * 1000:
                data["Paging"]["MaxCount"] += reference_extra
            return data

    return FakeClient


# --------------------------------------------------------------------------
# Grid
# --------------------------------------------------------------------------

def test_cluster_stress_simulation_finds_all_but_fails(centroids, tmp_path):
    result = gtue.run(mode="simulated", root=tmp_path, centroids=centroids, verbose=False, sim_clusters=grid.BIG_CITIES)
    s = result.summary
    # the dense clusters exceed what the smallest squares can resolve -> FAIL,
    # although every location is found through overlapping neighbour queries
    assert s["status"] == "FAIL"
    assert any("coverage warnings" in r for r in s["status_reasons"])
    assert s["reference"]["start"]["max_count"] == s["count_check"]["n_circle"] == 1200  # (b) holds, (a) fails
    assert s["unique_locations"] == 1200
    assert s["unique_german_locations"] == 1200
    assert s["countries"] == {"D": 1200}

    locations = gtue.simulated_locations(centroids, seed=42, clusters=grid.BIG_CITIES)
    found = set(result.df["source_id"])
    assert found == set(locations["id"])
    # the dense clusters (60 within 10 km of each big city) are all there
    for city, group in locations[locations["cluster"].notna()].groupby("cluster"):
        assert set(group["id"]) <= found, city
    assert [(f / paths.RUN_SUMMARY_JSON).exists() for f in run_folders(tmp_path, "simulated")] == [True]


def test_simulated_run_passes(centroids, tmp_path):
    result = gtue.run(mode="simulated", root=tmp_path, centroids=centroids, verbose=False)
    s = result.summary
    assert s["status"] == "PASS", s["status_reasons"]
    assert s["status_reasons"] == [] and s["alerts"] == []
    assert s["squares"]["warning"] == s["squares"]["error"] == s["squares"]["not_queried"] == 0
    assert s["unique_locations"] == 1200
    assert set(result.df["source_id"]) == set(gtue.simulated_locations(centroids, seed=42, clusters={})["id"])


def test_shifted_centre_gets_split():
    """Without the re-query (requery_uncovered=False) a shifted centre splits the square."""
    points = point_grid(48.0, 48.3, 11.3, 11.7)
    bbox = (48.05, 48.2, 11.4, 11.6)  # fits one 20 km square
    calls = []

    def query_fn(point, radius_km):
        calls.append(point)
        if len(calls) == 1:
            # everything "returned", but the server centred the search 10 km away
            return QueryResult(0, 0, point.lat + 10 / 110.57, point.lng, {})
        return QueryResult(0, 0, point.lat, point.lng, {})

    result = run_grid(query_fn, points, bbox=bbox, top_size_km=20, min_size_km=5, requery_uncovered=False)
    first = result.log[0]
    assert first["status"] == "split"
    assert first["corner_dist_km"] > first["radius_km"]
    assert first["center_offset_km"] == pytest.approx(10, abs=0.1)
    assert "actual centre" in first["note"]
    assert result.log[1]["center_offset_km"] == 0
    assert len(calls) == 5  # the square plus its 4 children
    assert all(row["status"] == "complete" for row in result.log[1:])
    outliers = grid.geocoding_outliers(result.log, 5)
    assert [o["seq"] for o in outliers] == [1]
    assert outliers[0]["zip"] == calls[0].zip
    assert outliers[0]["square_km"] == first["square_km"]  # the square it covered
    assert outliers[0]["lat_min"] == first["lat_min"] and outliers[0]["lng_max"] == first["lng_max"]


def test_cluster_at_one_address_warns_and_terminates():
    points = point_grid(50.8, 51.1, 6.8, 7.2)
    bbox = (50.85, 51.05, 6.85, 7.15)
    same_address = [{"id": f"c{i}", "lat": 50.94, "lng": 6.96} for i in range(25)]
    others = [{"id": f"o{i}", "lat": 50.9 + i * 0.01, "lng": 6.9} for i in range(5)]
    finder = SimulatedFinder(pd.DataFrame(same_address + others), center_offset_km=0.5, seed=1)

    result = run_grid(finder.query, points, bbox=bbox, top_size_km=20, min_size_km=1)
    assert result.coverage_warnings, "expected a coverage warning for the 25-location cluster"
    assert all("MaxCount" in w["reason"] for w in result.coverage_warnings)
    assert min(row["square_km"] for row in result.log) >= 1
    assert len(result.log) < 500  # terminated, no endless loop
    assert len([i for i in result.items if i.startswith("c")]) == 20  # capped at 20 per query


def test_401_stops_the_run():
    c, session, _ = client([FakeResponse(401), FakeResponse(200, ok_payload())])
    points = point_grid(48.0, 48.3, 11.3, 11.7)
    result = GridResult()
    with pytest.raises(gtue.KeyRejectedError, match="key rejected, probably rotated, update .env"):
        run_grid(gtue.make_query_fn(c), points, bbox=(48.05, 48.25, 11.4, 11.6), top_size_km=20, result=result)
    assert len(session.calls) == 1
    assert [row["status"] for row in result.log] == ["error"]  # the failed query is logged
    assert "key rejected" in result.log[0]["note"]


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

def test_client_sends_expected_request():
    c, session, _ = client([FakeResponse(200, ok_payload())])
    c.query("80686", "München", 50000)
    call = session.calls[0]
    assert call["url"] == gtue.API_URL
    assert call["params"] == {
        "Top": 20, "MaxRadius": 50000, "Branches": "GTUE", "City": "München",
        "Zip": "80686", "Street": " ", "IsoCountryCode": "DE",
    }
    assert call["headers"]["x-api-key"] == "test-key"
    assert call["headers"]["Origin"] == "https://gtue-finder.smartmaps.app"
    assert call["headers"]["Referer"] == "https://gtue-finder.smartmaps.app/"
    assert "User-Agent" not in call["headers"]  # requests' default, no browser impersonation
    assert "test-key" not in repr(c)


def test_5xx_and_timeouts_are_retried():
    c, session, sleeps = client([FakeResponse(503), requests.Timeout(), FakeResponse(200, ok_payload())])
    assert c.query("80686", "München", 1000)["Paging"]["Count"] == 0
    assert len(session.calls) == 3
    assert sleeps == [5.0, 10.0]  # exponential backoff


def test_5xx_three_times_stops():
    c, _, _ = client([FakeResponse(500)] * 3)
    with pytest.raises(gtue.ApiError, match="HTTP 500"):
        c.query("80686", "München", 1000)


def test_429_waits_60s_then_stops_if_repeated():
    c, session, sleeps = client([FakeResponse(429), FakeResponse(429)])
    with pytest.raises(gtue.ApiError, match="429"):
        c.query("80686", "München", 1000)
    assert sleeps == [60]
    assert len(session.calls) == 2


def test_hard_request_limit():
    c, _, _ = client([FakeResponse(200, ok_payload())] * 5)
    c.max_requests = 3
    for _ in range(3):
        c.query("80686", "München", 1000)
    with pytest.raises(gtue.RequestLimitReached):
        c.query("80686", "München", 1000)


def test_unexpected_response_stops():
    with pytest.raises(gtue.ApiError, match="Unexpected GTÜ response format"):
        gtue.to_query_result({"Paging": {"Count": 0, "MaxCount": 0}, "SearchData": {"Location": None}, "AddressItems": []})


def test_raw_store_never_overwrites(tmp_path):
    first = gtue.RawStore(tmp_path / "raw", tmp_path)
    assert first.save({"a": 1}) == "raw/raw.jsonl#0001.json"
    second = gtue.RawStore(tmp_path / "raw", tmp_path)  # a second store on the same run folder
    assert second.save({"a": 2}) == "raw/raw.jsonl#0002.json"
    assert raw_lines(tmp_path / "raw") == [("0001.json", {"a": 1}), ("0002.json", {"a": 2})]


# --------------------------------------------------------------------------
# Run modes
# --------------------------------------------------------------------------

def test_sample_run_is_reference_query_plus_cap_probe_plus_two_squares(tmp_path, centroids, monkeypatch):
    assert gtue.SAMPLE_MAX_REQUESTS == 5  # reference 1 + cap probe 2 + grid squares 2
    locations = gtue.simulated_locations(centroids, seed=1)
    monkeypatch.setattr(gtue, "load_api_key", lambda root: "test-key")
    monkeypatch.setattr(gtue, "GtueClient", fake_client_class(locations, centroids))

    at = datetime(2026, 9, 28, 12, 0)
    result = gtue.run(mode="sample", root=tmp_path, centroids=centroids, started_at=at, verbose=False)
    s = result.summary
    assert s["status"] == "FAIL"  # a sample never covers every square
    assert s["status_reasons"][0].startswith("sample mode: reference query + cap probe + 2 grid squares")
    assert any("squares not queried" in r for r in s["status_reasons"])
    assert s["drop_check"] == "n/a (only full runs are compared)"
    assert s["requests_made"] == 5
    assert list(result.query_log["status"]) == ["reference_check", "cap_probe", "cap_probe", "split", "split"]
    assert result.query_log.loc[0, "zip"] == "99986"
    assert result.query_log.loc[0, "city"] == "Niederdorla"
    assert result.query_log.loc[0, "radius_km"] == 500
    assert list(result.query_log.loc[1:2, "radius_km"]) == [455, 460]
    assert set(result.query_log.loc[1:2, "zip"]) == {"80686"}
    assert len(result.query_log) == 5  # no end query in a sample
    assert s["reference"]["start"]["max_count"] == 1200
    assert s["reference"]["end"] is None
    probe = s["early_stop"]["cap_probe"]  # recorded in every sample, the early stop itself stays off
    assert probe["radii_km"] == [455, 460] and s["early_stop"]["requested"] is False
    n = s["squares"]["not_queried"]
    assert s["outcome"] == f"FAIL ({n} squares open: 0 coverage warnings, {n} not queried)"
    assert "count gap" not in s["outcome"]  # a sample is expected to fall short of the MaxCount
    assert s["reference"]["countries_returned"] == {"D": 20}
    printed = gtue.format_run_summary(s)
    assert "Reference query (99986 Niederdorla, 500 km):" in printed
    assert "MaxCount at start 1200 (at " in printed
    assert "Address.Country of the 20 items it returned: D: 20" in printed
    first_folder = tmp_path / s["files"]["run_folder"]
    assert first_folder == paths.run_dir("gtue", "sample", at, tmp_path)  # 1_scrapers/gtue/test_output/2026-09-28_1200_sample
    assert [name for name, _ in raw_lines(first_folder)] == [f"000{i}.json" for i in range(1, 6)]
    assert s["renamed_files"] == []

    # a second run in the same minute gets a folder of its own (_2) and leaves the first one untouched
    first_log = (first_folder / paths.QUERY_LOG_FILE).read_bytes()
    first_raw = (first_folder / paths.RAW_FILE).read_bytes()
    second = gtue.run(mode="sample", root=tmp_path, centroids=centroids, started_at=at, verbose=False)
    second_folder = tmp_path / second.summary["files"]["run_folder"]
    assert second_folder.name == "2026-09-28_1200_sample_2"
    assert [name for name, _ in raw_lines(second_folder)] == [f"000{i}.json" for i in range(1, 6)]
    assert (first_folder / paths.QUERY_LOG_FILE).read_bytes() == first_log
    assert (first_folder / paths.RAW_FILE).read_bytes() == first_raw
    assert second.summary["files"]["query_log"].endswith("2026-09-28_1200_sample_2/query_log.csv")
    assert second.summary["renamed_files"] == []
    assert len(run_folders(tmp_path, "sample")) == 2


def test_export_never_overwrites(tmp_path, centroids, caplog):
    df = gtue.run(mode="simulated", root=tmp_path, centroids=centroids, verbose=False).df.head(5)
    out = tmp_path / "exports"
    first = gtue.export(df, out, run_date="2026-09-28")
    original = first["all_csv"][0].read_bytes()
    renamed = []
    with caplog.at_level("WARNING", logger="gtue.adapter"):
        second = gtue.export(df.head(2), out, run_date="2026-09-28", renamed=renamed)
    assert first["all_csv"][0].read_bytes() == original
    assert second["all_csv"][0].name == "gtue_all_2026-09-28_2.csv"
    assert second["pruefstellen_xlsx"][0].name == "gtue_pruefstellen_2026-09-28_2.xlsx"
    assert len(renamed) == 4
    assert "already exists" in caplog.text
    third = gtue.export(df, out, run_date="2026-09-28")
    assert third["all_csv"][0].name == "gtue_all_2026-09-28_3.csv"


def test_full_run_needs_confirmation(tmp_path):
    with pytest.raises(RuntimeError, match="not confirmed"):
        gtue.run(mode="full", root=tmp_path)


def _full_run(tmp_path, centroids, monkeypatch, locations, day, **client_kwargs):
    """Offline full run with the fake client."""
    monkeypatch.setattr(gtue, "load_api_key", lambda root: "test-key")
    monkeypatch.setattr(gtue, "GtueClient", fake_client_class(locations, centroids, **client_kwargs))
    return gtue.run(mode="full", confirm_full_run=True, root=tmp_path, centroids=centroids, run_date=day, verbose=False)


def test_full_run_foreign_rows_and_drop_check(tmp_path, centroids, monkeypatch):
    """Offline full runs: foreign rows, no baseline, then a >10 % drop -> alert (status stays PASS)."""
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    foreign = list(locations["id"][:20])

    # 1st run: no baseline yet; 20 entries abroad stay in the all-file but out of scope
    first = _full_run(tmp_path, centroids, monkeypatch, locations, "2026-10-01", foreign_ids=foreign)
    s = first.summary
    assert s["status"] == "PASS", s["status_reasons"]
    assert s["baseline"] is None
    assert s["unique_locations"] == 1200
    assert s["countries"] == {"D": 1180, "A": 20}
    assert s["unique_german_locations"] == 1180
    abroad = first.df[first.df["country"] == "A"]
    assert not abroad["in_scope"].any()
    assert (abroad["needs_review_reason"] == "outside Germany").all()
    assert abroad["state"].isna().all()  # no PLZ lookup for foreign postcodes
    assert s["needs_review_by_reason"] == {"outside Germany": 20}
    prs = pd.read_csv(tmp_path / s["files"]["pruefstellen_csv"], sep=";", encoding="utf-8-sig", dtype=str)
    assert not set(prs["source_id"]) & set(foreign)

    # 2nd run: 15 % fewer locations (-13.6 % German) -> alert; the crawl itself is complete
    second = _full_run(tmp_path, centroids, monkeypatch, locations.iloc[:1020], "2026-11-01").summary
    assert second["status"] == "PASS"
    assert second["drop_check"] == "ALERT" and second["german_drop_alert"]
    assert second["baseline"]["run_date"] == "2026-10-01"
    assert second["german_change_pct"] == pytest.approx(-13.6, abs=0.1)
    assert "dropped" in second["alerts"][0]
    assert "ALERTS:" in gtue.format_run_summary(second)

    # 3rd run: the alerted run is not a baseline, so it compares against the 1st run again
    third = _full_run(tmp_path, centroids, monkeypatch, locations.iloc[:1100], "2026-12-01").summary
    assert third["baseline"]["run_date"] == "2026-10-01"
    assert third["drop_check"] == "ok" and third["alerts"] == []  # -6.8 %: within the limit
    assert third["status"] == "PASS"


# --------------------------------------------------------------------------
# Run status (PASS / FAIL)
# --------------------------------------------------------------------------

def test_all_squares_complete_but_count_gap_fails(tmp_path, centroids, monkeypatch):
    """Rule C4: every square complete is not enough; N_circle must equal the reference MaxCount
    (the four outcomes and the start/end tolerance are in test_gtue_reference.py)."""
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    s = _full_run(tmp_path, centroids, monkeypatch, locations, "2026-10-01", reference_extra=230).summary
    assert s["squares"]["not_queried"] == s["squares"]["warning"] == 0
    assert s["status"] == "FAIL"
    assert s["status_reasons"] == ["N_circle 1200 < reference MaxCount 1430 (gap 230)"]
    assert s["reference"]["start"]["max_count"] == 1430 and s["unique_locations"] == 1200


def test_no_previous_run_reports_no_baseline(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    s = _full_run(tmp_path, centroids, monkeypatch, locations, "2026-10-01").summary
    assert s["status"] == "PASS"
    assert s["drop_check"] == "no baseline"
    assert s["baseline"] is None and s["alerts"] == []
    assert "no baseline" in gtue.format_run_summary(s)


def test_errored_query_fails_the_run(tmp_path, centroids, monkeypatch):
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    with pytest.raises(gtue.ApiError):
        _full_run(tmp_path, centroids, monkeypatch, locations, "2026-10-01", fail_at=5)
    (folder,) = run_folders(tmp_path, "full")
    s = pd.read_json(folder / paths.RUN_SUMMARY_JSON, typ="series")
    assert s["status"] == "FAIL"
    assert s["aborted"]
    assert "1 queries errored" in s["status_reasons"]
    assert s["drop_check"] == "not run (run aborted)"


def _grid_result(statuses, warnings=0, not_queried=0):
    result = GridResult()
    result.log = [{"status": st} for st in statuses]
    result.coverage_warnings = [{"reason": "Count 20 < MaxCount 25"}] * warnings
    result.squares_not_queried = not_queried
    return result


def test_run_status_rules():
    ok = ["reference_check", "split", "complete", "complete", "complete", "complete"]
    assert gtue.run_status("full", _grid_result(ok)) == ("PASS", [])

    status, reasons = gtue.run_status("full", _grid_result(ok + ["warning"], warnings=1))
    assert status == "FAIL" and reasons == ["1 squares still incomplete at minimum size (coverage warnings)"]

    status, reasons = gtue.run_status("full", _grid_result(ok, not_queried=3))
    assert status == "FAIL" and reasons == ["3 squares not queried"]

    status, reasons = gtue.run_status("full", _grid_result(ok + ["error"]), gtue.ApiError("HTTP 500"))
    assert status == "FAIL"
    assert reasons == ["run aborted: ApiError: HTTP 500", "1 queries errored"]

    status, reasons = gtue.run_status("full", _grid_result(["reference_check"]))
    assert status == "FAIL" and reasons == ["no grid square was queried"]


def test_incomplete_square_fails_the_run(tmp_path, centroids):
    """Run level: one dense spot (60 locations within 10 km of Köln) leaves squares
    incomplete at minimum size; everything else is complete."""
    cluster = {"Köln": grid.BIG_CITIES["Köln"]}
    result = gtue.run(mode="simulated", root=tmp_path, centroids=centroids, verbose=False, sim_clusters=cluster)
    s = result.summary
    assert s["status"] == "FAIL"
    assert s["squares"]["warning"] >= 1
    assert any("coverage warnings" in r for r in s["status_reasons"])
    assert s["unique_locations"] == 1200  # found, but not provably complete


def test_foreign_rows_out_of_scope_and_counted(tmp_path, centroids, monkeypatch):
    """Country != "D" -> in_scope False whatever the site type; counted per country."""
    locations = gtue.simulated_locations(centroids, seed=3, clusters={})
    foreign = list(locations.loc[locations["herkunft"] == "E", "id"][:15])  # would be Prüfstellen
    s = _full_run(tmp_path, centroids, monkeypatch, locations, "2026-10-01", foreign_ids=foreign)
    df, summary = s.df, s.summary
    abroad = df[df["source_id"].isin(foreign)]
    assert (abroad["site_type"] == "pruefstelle").all()  # classification unchanged ...
    assert not abroad["in_scope"].any()  # ... but out of scope
    assert (abroad["needs_review_reason"] == "outside Germany").all()
    assert not abroad["needs_review"].any()
    assert summary["countries"] == {"D": 1185, "A": 15}
    german = df[df["country"] == "D"]
    assert summary["german_site_type_counts"] == {
        "pruefstelle": int((german["site_type"] == "pruefstelle").sum()),
        "partner_office": int((german["site_type"] == "partner_office").sum()),
        "unknown": 0,
    }
    # scope rule 2026-10-07: German in-scope rows = E Prüfstellen + unknown + G offices that list HU
    assert summary["in_scope"] == int(german["in_scope"].sum())
    assert summary["in_scope"] == sum(summary["in_scope_by_tier"].values()) == int(german["scope_tier"].isin(gtue.IN_SCOPE_TIERS).sum())
    assert summary["in_scope_by_tier"]["pruefstelle"] == summary["german_site_type_counts"]["pruefstelle"]
    printed = gtue.format_run_summary(summary)
    assert "Per Address.Country: D: 1185, A: 15" in printed
    assert f"In scope:           {summary['in_scope']} German rows (" in printed


def test_summary_lists_centre_offsets_over_5_km(tmp_path, centroids):
    s = gtue.run(mode="simulated", root=tmp_path, centroids=centroids, verbose=False).summary
    assert s["geocoding_outliers"] == []  # simulated offsets are at most 2 km
    summary = dict(s, geocoding_outliers=[
        {"seq": 3, "status": "split", "zip": "88086", "city": "Immenstaad am Bodensee", "center_offset_km": 15.26,
         "square_km": 100.0, "lat_min": 47.2, "lat_max": 48.1044, "lng_min": 8.65486, "lng_max": 10.08229},
    ])
    printed = gtue.format_run_summary(summary)
    assert "1 queries with centre offset > 5 km" in printed
    assert "PLZ 88086 Immenstaad am Bodensee: 15.26 km, 100.0 km square lat 47.2-48.1044 lng 8.65486-10.08229 (query 3)" in printed


def test_sample_run_without_key_fails_before_any_request(tmp_path, centroids, monkeypatch):
    monkeypatch.delenv(gtue.API_KEY_ENV, raising=False)
    with pytest.raises(grid.StopRun, match="GTUE_API_KEY is not set"):
        gtue.run(mode="sample", root=tmp_path, centroids=centroids, verbose=False)
