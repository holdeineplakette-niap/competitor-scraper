"""Reference query (Niederdorla, 500 km), status rule C4 and early stop rule E. No network."""

import json

import pytest

from core import grid, paths
from gtue import adapter as gtue
from core.grid import GridResult, QueryResult
from test_grid import FakeResponse, client, fake_client_class, ok_payload, run_folders

LAT0, LNG0 = gtue.REFERENCE_CENTRE
PARIS = (48.86, 2.35)  # ~640 km from the reference centre
VIENNA = (48.21, 16.37)  # ~540 km


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def item(i, lat, lng, country="D"):
    return {
        "BasicData": {
            "Identifiers": {"ProviderForeignKey": str(i)},
            "Geo": {"YCoord": str(lat), "XCoord": str(lng)},
            "Address": {"Country": country},
        }
    }


def items_of(inside=0, abroad_inside=0, outside=0):
    """Raw items: `inside` German rows and `abroad_inside` foreign rows near the reference centre,
    `outside` foreign rows beyond 500 km (Paris/Vienna)."""
    out, n = {}, 0
    for _ in range(inside):
        n += 1
        out[str(n)] = item(n, LAT0 + n * 0.001, LNG0)
    for _ in range(abroad_inside):
        n += 1
        out[str(n)] = item(n, LAT0 + n * 0.001, LNG0, country="CH")
    for k in range(outside):
        n += 1
        out[str(n)] = item(n, *(PARIS if k % 2 == 0 else VIENNA), country="F")
    return out


def all_complete():
    result = GridResult()
    result.log = [{"status": s} for s in ("reference_check", "split", "complete", "complete")]
    return result


def result_with(items, max_count, centre=gtue.REFERENCE_CENTRE):
    return QueryResult(min(20, max_count), max_count, centre[0], centre[1], items)


# --------------------------------------------------------------------------
# The reference query itself
# --------------------------------------------------------------------------

def test_reference_constants():
    assert (gtue.REFERENCE_ZIP, gtue.REFERENCE_CITY, gtue.REFERENCE_RADIUS_KM) == ("99986", "Niederdorla", 500)
    assert gtue.REFERENCE_CENTRE == (51.15844, 10.44595)
    assert gtue.REFERENCE_CENTRE_TOLERANCE_KM == 5
    assert gtue.REFERENCE_MAX_BORDER_KM == 454 < gtue.REFERENCE_RADIUS_KM == gtue.SERVER_RADIUS_CAP_KM


def test_client_sends_exactly_the_reference_parameters():
    c, session, _ = client([FakeResponse(200, ok_payload())])
    c.query(gtue.REFERENCE_ZIP, gtue.REFERENCE_CITY, gtue.REFERENCE_RADIUS_KM * 1000)
    assert session.calls[0]["params"] == {
        "Top": 20, "MaxRadius": 500000, "Branches": "GTUE", "City": "Niederdorla",
        "Zip": "99986", "Street": " ", "IsoCountryCode": "DE",
    }


def test_no_national_check_left():
    assert not hasattr(gtue, "NATIONAL_CHECK_ZIP") and not hasattr(gtue, "NATIONAL_CHECK_RADIUS_KM")
    assert not hasattr(grid, "national_check") and hasattr(grid, "reference_check")


# --------------------------------------------------------------------------
# Scripted fake client: control what the reference (and the cap probe) answer
# --------------------------------------------------------------------------

def scripted_client(locations, centroids, reference=None, probe=None, foreign_ids=()):
    """reference(call_no, data) may change the reference response, probe(radius_m, data) the cap probe's."""
    base = fake_client_class(locations, centroids, foreign_ids=foreign_ids)

    class Scripted(base):
        reference_calls = 0

        def query(self, zip, city, radius_m):
            data = super().query(zip, city, radius_m)
            if zip == gtue.REFERENCE_ZIP and radius_m == gtue.REFERENCE_RADIUS_KM * 1000:
                type(self).reference_calls += 1
                if reference:
                    reference(type(self).reference_calls, data)
            elif zip == gtue.CAP_PROBE_ZIP and radius_m in tuple(km * 1000 for km in gtue.CAP_PROBE_RADII_KM) and probe:
                probe(radius_m, data)
            return data

    return Scripted


def run_full(tmp_path, centroids, monkeypatch, client_class, day="2026-10-01", mode="full", **kwargs):
    monkeypatch.setattr(gtue, "load_api_key", lambda root: "test-key")
    monkeypatch.setattr(gtue, "GtueClient", client_class)
    return gtue.run(
        mode=mode, confirm_full_run=True, root=tmp_path, centroids=centroids, run_date=day, verbose=False, **kwargs
    )


@pytest.fixture(scope="module")
def locations(centroids):
    return gtue.simulated_locations(centroids, seed=3, clusters={})


def move_centre(km_north):
    def change(_n, data):
        data["SearchData"]["Location"]["YCoord"] = f"{LAT0 + km_north / 111.195:.5f}"
    return change


@pytest.mark.parametrize("mode", ["sample", "full"])
def test_reference_centre_more_than_5_km_off_stops_the_run(tmp_path, centroids, monkeypatch, locations, mode):
    client_class = scripted_client(locations, centroids, reference=move_centre(6))
    with pytest.raises(gtue.ReferenceCentreError, match="coverage proof depends on this centre"):
        run_full(tmp_path, centroids, monkeypatch, client_class, mode=mode)
    (folder,) = run_folders(tmp_path, mode)  # 1_scrapers/gtue/<output|test_output>/2026-10-01_<time>_<mode>
    s = json.loads((folder / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
    assert s["aborted"] and s["status"] == "FAIL"
    assert s["requests_made"] == 1  # nothing after the reference query
    assert s["squares"]["complete"] == s["squares"]["split"] == 0
    assert "km from the expected 51.15844, 10.44595 (limit 5 km)" in s["status_reasons"][0]
    assert s["reference"]["centre_offset_km"] == pytest.approx(6, abs=0.1)


def test_sample_probe_is_information_only(tmp_path, centroids, monkeypatch, locations):
    """A sample runs the cap probe so its result is on record; equal counts don't change the sample's
    outcome (no early stop is requested), they are only reported."""
    equal = lambda radius_m, data: data["Paging"].__setitem__("MaxCount", 900)
    client_class = scripted_client(locations, centroids, probe=equal)
    result = run_full(tmp_path, centroids, monkeypatch, client_class, mode="sample")
    s = result.summary
    assert s["requests_made"] == 5 and not s["aborted"]
    assert s["early_stop"]["cap_probe"]["equal"] is True and s["early_stop"]["disabled_reason"] is None
    text = gtue.format_run_summary(s)
    assert "Early stop: off" in text
    assert "Cap probe München 80686: MaxCount 900 at 455 km, 900 at 460 km (EQUAL: the cap is below what covers Germany" in text


def test_reference_centre_within_5_km_is_accepted(tmp_path, centroids, monkeypatch, locations):
    client_class = scripted_client(locations, centroids, reference=move_centre(4))
    s = run_full(tmp_path, centroids, monkeypatch, client_class, mode="sample").summary
    assert not s["aborted"] and s["reference"]["centre_offset_km"] == pytest.approx(4, abs=0.1)


# --------------------------------------------------------------------------
# N_circle
# --------------------------------------------------------------------------

def test_n_circle_excludes_collected_rows_beyond_500_km():
    items = items_of(inside=3, outside=4)
    assert len(items) == 7  # all collected
    inside, outside, missing = gtue.circle_split(items, gtue.REFERENCE_CENTRE)
    assert (len(inside), len(outside), missing) == (3, 4, 0)
    count = gtue.count_check(items, gtue.REFERENCE_CENTRE, [3])
    assert count["n_circle"] == 3 and count["verdict"] == "match"
    assert count["abroad_inside"] == 0 and count["abroad_outside"] == 4  # Paris/Vienna rows: outside, foreign
    # the same 7 rows against a MaxCount of 7 would be a gap: 4 of them are not in the circle
    assert gtue.count_check(items, gtue.REFERENCE_CENTRE, [7])["verdict"] == "gap"


def test_entry_without_coordinates_is_not_counted():
    items = items_of(inside=2)
    items["x"] = {"BasicData": {"Identifiers": {"ProviderForeignKey": "x"}, "Address": {"Country": "D"}}}
    count = gtue.count_check(items, gtue.REFERENCE_CENTRE, [2])
    assert count["n_circle"] == 2 and count["items_without_coordinates"] == 1 and count["verdict"] == "match"


# --------------------------------------------------------------------------
# Rule C4: the four outcomes
# --------------------------------------------------------------------------

def evaluate(items, observations):
    count = gtue.count_check(items, gtue.REFERENCE_CENTRE, observations)
    status, reasons = gtue.run_status("full", all_complete(), None, count)
    return status, reasons, gtue.outcome_label(status, all_complete(), None, count), count


def test_c4_match_without_foreign_rows_is_pass_and_maxcount_is_the_german_total():
    status, reasons, label, count = evaluate(items_of(inside=100), [100])
    assert (status, reasons) == ("PASS", [])
    assert label == "PASS (grid complete + count match)"
    assert count["german_total"] == 100 == count["reference_low"] and count["abroad_inside"] == 0


def test_c4_match_with_foreign_rows_inside_is_pass_with_a_note():
    status, reasons, label, count = evaluate(items_of(inside=95, abroad_inside=5), [100])
    assert (status, reasons) == ("PASS", [])
    assert label.startswith("PASS (grid complete + count match) with note:")
    assert "5 rows with Country != D inside the circle" in label and "German total 95" in label
    assert count["german_total"] == 95 and count["n_circle"] == 100


def test_c4_gap_is_fail_and_reports_the_gap():
    status, reasons, label, count = evaluate(items_of(inside=98), [100])
    assert status == "FAIL"
    assert reasons == ["N_circle 98 < reference MaxCount 100 (gap 2)"]
    assert "count gap 2" in label and count["gap"] == 2


def test_c4_excess_is_fail_investigate():
    status, reasons, label, count = evaluate(items_of(inside=102), [100])
    assert status == "FAIL"
    assert len(reasons) == 1 and reasons[0].startswith("N_circle 102 > reference MaxCount 100: investigate")
    assert "data change" in reasons[0] and count["excess"] == 2


def test_c4_start_and_end_maxcount_differ_n_circle_may_be_anywhere_in_between():
    for n, expected in ((100, "PASS"), (101, "PASS"), (102, "PASS"), (99, "FAIL"), (103, "FAIL")):
        status, reasons, _label, count = evaluate(items_of(inside=n), [100, 102])
        assert status == expected, (n, reasons)
        assert (count["reference_low"], count["reference_high"]) == (100, 102)
    _s, reasons, _l, _c = evaluate(items_of(inside=99), [100, 102])
    assert reasons == ["N_circle 99 < reference MaxCount 100 (gap 1)"]
    _s, reasons, _l, _c = evaluate(items_of(inside=103), [100, 102])
    assert reasons[0].startswith("N_circle 103 > reference MaxCount 102")


def test_c4_grid_incomplete_fails_even_when_the_count_matches():
    result = all_complete()
    result.coverage_warnings = [{"reason": "x"}]
    count = gtue.count_check(items_of(inside=100), gtue.REFERENCE_CENTRE, [100])
    status, reasons = gtue.run_status("full", result, None, count)
    assert status == "FAIL" and reasons == ["1 squares still incomplete at minimum size (coverage warnings)"]


@pytest.mark.parametrize("country, in_scope", [("D", True), ("CH", False), (None, False)])
def test_rows_with_country_other_than_d_are_never_in_scope(centroids, country, in_scope):
    """A Prüfstelle-type row (Herkunft E + GTUP) is in scope only in Germany; a missing country counts as abroad."""
    locations = gtue.simulated_locations(centroids, n_total=30, seed=1, clusters={})
    raw = []
    for row in locations.itertuples():
        it = gtue._fake_item(row)
        it["BasicData"]["Address"]["Country"] = country
        raw.append(it)
    df = gtue.parse_response({"AddressItems": raw}, "2026-10-01T00:00:00")
    pruef = df[df["site_type"] == "pruefstelle"]
    assert len(pruef) > 0  # the classification itself is unchanged
    assert pruef["in_scope"].eq(in_scope).all()


# --------------------------------------------------------------------------
# C4 at run level (fake client)
# --------------------------------------------------------------------------

def test_full_run_queries_the_reference_at_start_and_end_with_timestamps(tmp_path, centroids, monkeypatch, locations):
    s = run_full(tmp_path, centroids, monkeypatch, scripted_client(locations, centroids), use_cache=False).summary
    ref = s["reference"]
    assert ref["start"]["max_count"] == ref["end"]["max_count"] == 1200
    assert len(ref["start"]["at"]) == 19 and ref["end"]["at"] >= ref["start"]["at"]  # ISO timestamps
    assert s["status"] == "PASS" and s["outcome"] == "PASS (grid complete + count match)"
    assert s["count_check"]["n_circle"] == 1200 and s["count_check"]["german_total"] == 1200
    assert s["alerts"] == [] and s["early_stop"]["cap_probe"] is None
    text = gtue.format_run_summary(s)
    assert f"MaxCount at start 1200 (at {ref['start']['at']}) · at end 1200 (at {ref['end']['at']})" in text
    assert "N_circle 1200" in text and "Rows with Country != D: 0 inside the circle" in text
    assert "German total (Country == D): 1200 (= MaxCount)" in text and "Open squares:       0" in text


def test_run_level_foreign_rows_inside_give_pass_with_note(tmp_path, centroids, monkeypatch, locations):
    foreign = list(locations["id"][:20])
    client_class = scripted_client(locations, centroids, foreign_ids=foreign)
    s = run_full(tmp_path, centroids, monkeypatch, client_class, use_cache=False).summary
    assert s["status"] == "PASS"
    assert "with note: 20 rows with Country != D inside the circle, German total 1180" in s["outcome"]
    assert s["count_check"]["abroad_inside"] == 20 and s["count_check"]["german_total"] == 1180


def test_run_level_gap_fails_although_every_square_is_complete(tmp_path, centroids, monkeypatch, locations):
    def three_more(_n, data):
        data["Paging"]["MaxCount"] += 3  # the server counts 3 more than we can find

    s = run_full(tmp_path, centroids, monkeypatch, scripted_client(locations, centroids, reference=three_more), use_cache=False).summary
    assert s["squares"]["not_queried"] == s["squares"]["warning"] == 0  # criterion (a) holds
    assert s["status"] == "FAIL" and s["status_reasons"] == ["N_circle 1200 < reference MaxCount 1203 (gap 3)"]
    assert "count gap 3" in s["outcome"] and "count-mismatch" in s["files"]["all_csv"]


def test_run_level_start_end_differ(tmp_path, centroids, monkeypatch, locations):
    # start 1200, end 1202: N_circle 1200 is inside the range -> PASS, with an alert
    def reference(n, data):
        if n == 2:
            data["Paging"]["MaxCount"] += 2

    s = run_full(tmp_path, centroids, monkeypatch, scripted_client(locations, centroids, reference=reference), use_cache=False).summary
    assert s["status"] == "PASS" and s["reference"]["start"]["max_count"] == 1200 and s["reference"]["end"]["max_count"] == 1202
    assert "1200 -> 1202" in s["alerts"][0] and "between 1200 and 1202" in s["alerts"][0]

    # start 1150, end 1160: N_circle 1200 is above the whole range -> FAIL (excess)
    def lower(n, data):
        data["Paging"]["MaxCount"] -= 50 if n == 1 else 40

    s2 = run_full(tmp_path, centroids, monkeypatch, scripted_client(locations, centroids, reference=lower), "2026-10-02", use_cache=False).summary
    assert s2["status"] == "FAIL"
    assert s2["status_reasons"][0].startswith("N_circle 1200 > reference MaxCount 1160: investigate")

    # start 1250, end 1240: N_circle 1200 is below the whole range -> FAIL (gap 40)
    def raise_(n, data):
        data["Paging"]["MaxCount"] += 50 if n == 1 else 40

    s3 = run_full(tmp_path, centroids, monkeypatch, scripted_client(locations, centroids, reference=raise_), "2026-10-03", use_cache=False).summary
    assert s3["status"] == "FAIL" and s3["status_reasons"] == ["N_circle 1200 < reference MaxCount 1240 (gap 40)"]
    assert "GTÜ run summary (full): FAIL (" in gtue.format_run_summary(s3).splitlines()[0]


def test_reference_centre_moved_by_the_end_is_a_fail(tmp_path, centroids, monkeypatch, locations):
    client_class = scripted_client(locations, centroids, reference=lambda n, d: move_centre(8)(n, d) if n == 2 else None)
    s = run_full(tmp_path, centroids, monkeypatch, client_class, use_cache=False).summary
    assert s["status"] == "FAIL" and "moved" in s["status_reasons"][0]


# --------------------------------------------------------------------------
# Rule E: early stop
# --------------------------------------------------------------------------

def stub_live(answers, calls):
    """Live query function returning the queued MaxCounts (or an exception) for each recheck."""
    def live_fn(point, radius_km):
        calls.append((point.zip, radius_km))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        max_count, centre = answer if isinstance(answer, tuple) else (answer, gtue.REFERENCE_CENTRE)
        return QueryResult(20, max_count, centre[0], centre[1], {})
    return live_fn


def early(target, answers, calls):
    point = grid.QueryPoint(gtue.REFERENCE_ZIP, gtue.REFERENCE_CITY, *gtue.REFERENCE_CENTRE)
    return gtue.EarlyStop(stub_live(answers, calls), point, result_with({}, target))


def test_early_stop_counts_n_circle_not_total_collected():
    calls = []
    stop = early(5, [5], calls)
    result = GridResult(items=items_of(inside=3, outside=4))  # 7 collected >= 5, but only 3 in the circle
    assert stop(result) is False and calls == [] and stop.n_circle(result) == 3
    for n in range(100, 102):
        result.items[str(n)] = item(n, LAT0 + n * 0.0001, LNG0)  # two more inside: N_circle 5
    assert stop(result) is True  # N_circle reached 5: reference queried once, unchanged
    assert calls == [(gtue.REFERENCE_ZIP, 500)] and stop.triggered and not stop.overshoot
    assert stop.rechecks == 1


def test_early_stop_requery_changed_the_run_continues_against_the_new_value():
    calls = []
    stop = early(5, [6, 6], calls)
    result = GridResult(items=items_of(inside=5))
    assert stop(result) is False  # reference now says 6: continue
    assert stop.target == 6 and stop.target_changes == [(5, 6)] and not stop.triggered
    result.items["99"] = item(99, LAT0 + 0.01, LNG0)
    assert stop(result) is True and stop.triggered  # N_circle 6 == 6, unchanged on the second recheck
    assert len(calls) == 2


def test_early_stop_overshoot_stops_and_is_flagged():
    calls = []
    stop = early(5, [5], calls)
    result = GridResult(items=items_of(inside=7))  # N_circle jumped past the MaxCount
    assert stop(result) is True and stop.overshoot and not stop.triggered


def test_early_stop_off_after_a_failed_recheck_or_a_moved_centre():
    calls = []
    stop = early(3, [gtue.ApiError("boom")], calls)
    result = GridResult(items=items_of(inside=3))
    assert stop(result) is False and "recheck failed" in stop.error
    assert stop(result) is False and len(calls) == 1  # not asked again

    calls = []
    moved = early(3, [(3, (LAT0 + 0.1, LNG0))], calls)  # 11 km off
    assert moved(GridResult(items=items_of(inside=3))) is False and "moved" in moved.error


def test_early_stop_flag_off_runs_the_full_grid(tmp_path, centroids, monkeypatch, locations):
    s = run_full(tmp_path, centroids, monkeypatch, scripted_client(locations, centroids), use_cache=False).summary
    assert s["early_stop"]["requested"] is False
    assert s["completion"] == "grid complete" and s["squares"]["not_queried"] == 0
    assert s["outcome"] == "PASS (grid complete + count match)"


def test_cap_probe_equal_disables_early_stop_and_runs_the_full_grid(tmp_path, centroids, monkeypatch, locations):
    equal = lambda radius_m, data: data["Paging"].__setitem__("MaxCount", 900)  # 900 at 455 km and at 460 km
    client_class = scripted_client(locations, centroids, probe=equal)
    result = run_full(tmp_path, centroids, monkeypatch, client_class, use_cache=False, early_stop=True)
    s = result.summary
    assert s["early_stop"]["requested"] and "cap probe" in s["early_stop"]["disabled_reason"]
    assert s["early_stop"]["cap_probe"] == {"radii_km": [455, 460], "max_counts": [900, 900], "equal": True}
    assert s["completion"] == "grid complete" and s["squares"]["not_queried"] == 0  # full grid ran
    assert s["outcome"] == "PASS (grid complete + count match)"
    assert list(result.query_log["status"]).count("cap_probe") == 2
    assert "reference_recheck" not in set(result.query_log["status"])
    assert "REQUESTED BUT DISABLED" in gtue.format_run_summary(s)


def test_early_stop_triggers_and_reports_pass_with_open_squares_listed(tmp_path, centroids, monkeypatch, locations):
    counts = {455000: 800, 460000: 810}
    client_class = scripted_client(locations, centroids, probe=lambda r, d: d["Paging"].__setitem__("MaxCount", counts[r]))
    full = run_full(tmp_path, centroids, monkeypatch, scripted_client(locations, centroids), "2026-10-02", use_cache=False)
    result = run_full(tmp_path, centroids, monkeypatch, client_class, "2026-10-01", use_cache=False, early_stop=True)
    s = result.summary
    assert s["status"] == "PASS" and s["outcome"] == "PASS (early stop, count match)"
    assert s["early_stop"]["triggered"] and s["early_stop"]["cap_probe"]["equal"] is False
    assert s["early_stop"]["rechecks"] == 1
    assert s["count_check"]["n_circle"] == s["reference"]["start"]["max_count"] == 1200
    # the grid stopped: fewer queries than the full grid, squares left over are listed, not hidden
    assert len(result.query_log) < len(full.query_log)
    assert s["squares"]["not_queried"] > 0 and s["open_squares"] >= s["squares"]["not_queried"]
    import pandas as pd

    open_csv = pd.read_csv(tmp_path / s["files"]["open_squares"], sep=";", encoding="utf-8-sig")
    assert (open_csv["kind"] == "not queried").sum() == s["squares"]["not_queried"]
    assert "grid-incomplete" not in s["files"]["all_csv"]  # a PASS is not tagged
    assert s["completion"].startswith("early stop")
    # end-of-run reference query still happens after the stop
    assert list(result.query_log["status"])[-1] == "reference_end"
    assert "Early stop: on, triggered, 1 rechecks" in gtue.format_run_summary(s)


def test_early_stop_with_a_count_that_is_never_reached_runs_the_whole_grid(tmp_path, centroids, monkeypatch, locations):
    counts = {455000: 800, 460000: 810}
    client_class = scripted_client(
        locations, centroids,
        reference=lambda n, d: d["Paging"].__setitem__("MaxCount", d["Paging"]["MaxCount"] + 5),
        probe=lambda r, d: d["Paging"].__setitem__("MaxCount", counts[r]),
    )
    s = run_full(tmp_path, centroids, monkeypatch, client_class, use_cache=False, early_stop=True).summary
    assert not s["early_stop"]["triggered"] and s["squares"]["not_queried"] == 0
    assert s["status"] == "FAIL" and "gap 5" in s["status_reasons"][0]
    assert "count-mismatch" in s["files"]["all_csv"]  # FAIL without open squares: tagged as unverified
