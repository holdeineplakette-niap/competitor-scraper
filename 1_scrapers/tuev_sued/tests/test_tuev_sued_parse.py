"""TÜV SÜD parsing: answer shape, per-query facts (radius rule), records, notices, opening hours, merge."""

import copy
import json
from datetime import date

import pandas as pd
import pytest

from core import paths
from tuev_sued import parse

FIXTURE = paths.fixtures_dir("tuev_sued") / "response_muenchen_80636_synthetic_2026-10-05.json"
RUN_DATE = date(2026, 10, 5)
NAME = "pass01_q007_80636.json"


@pytest.fixture(scope="module")
def answer_json():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def response(answer_json):
    return copy.deepcopy(answer_json)


def item(distance=1.0, tsc_id="1", **location):
    loc = {"tscId": tsc_id, "lat": "48.1", "lon": "11.5", "street": "Hauptstr.", "streetnumber": 7, "zipcode": "80636", "town": "München"}
    loc.update(location)
    return {"tscId": tsc_id, "distance": f"{distance:.3f}", "sortdistance": distance, "times": [], "location": loc}


def answer_with(items, **top):
    return {"lat": 48.1, "lon": 11.5, "finaltsc": items, **top}


# ---- shape

def test_fixture_has_the_documented_shape(answer_json):
    assert parse.check_shape(answer_json) is answer_json
    assert len(answer_json["finaltsc"]) == 50 and set(answer_json) >= {"lat", "lon", "locationname", "finaltsc"}


@pytest.mark.parametrize("bad", [
    [],
    {"lat": 1, "lon": 2},
    {"finaltsc": [], "lat": 1},
    {"finaltsc": [], "lat": "x", "lon": 2},
    {"finaltsc": ["no item"], "lat": 1, "lon": 2},
    {"finaltsc": [{"tscId": "1", "distance": "1", "location": "no dict"}], "lat": 1, "lon": 2},
    {"finaltsc": [{"distance": "1", "location": {}}], "lat": 1, "lon": 2},
    {"finaltsc": [{"tscId": "1", "location": {}}], "lat": 1, "lon": 2},
])
def test_check_shape_rejects_anything_else(bad):
    with pytest.raises(parse.ShapeError):
        parse.check_shape(bad)


def test_an_empty_answer_with_a_centre_is_valid():
    ans = parse.to_answer(NAME, answer_with([]))
    assert ans.count == 0 and ans.radius_km == 0.0 and ans.first_km is None and ans.last_km is None


# ---- one answer: the facts the coverage proof uses

def test_answer_facts_from_the_fixture(answer_json):
    ans = parse.to_answer(NAME, answer_json)
    assert (ans.pass_no, ans.seq, ans.zip) == (1, 7, "80636")
    assert ans.count == 50 and ans.sorted_ok and not ans.problems
    assert ans.centre == pytest.approx((answer_json["lat"], answer_json["lon"]))
    assert ans.radius_km == pytest.approx(ans.last_km - 0.5) and ans.first_km <= ans.last_km
    assert ans.distance_dev_km < 0.05  # reported distances are the great-circle distances from the centre
    assert len(ans.ids) == 50


def test_the_radius_is_the_last_distance_minus_the_tie_margin():
    ans = parse.to_answer(NAME, answer_with([item(2.7, "a"), item(64.2, "b")]))
    assert ans.radius_km == pytest.approx(63.7)
    assert parse.to_answer(NAME, answer_with([item(0.3, "a")])).radius_km == 0.0


def test_unsorted_distances_and_distance_deviation_are_reported():
    unsorted = parse.to_answer(NAME, answer_with([item(5.0, "a"), item(3.0, "b")]))
    assert not unsorted.sorted_ok and any("not sorted" in p for p in unsorted.problems)
    far = parse.to_answer(NAME, answer_with([item(5.0, "a")]))  # the record lies on the centre, the answer says 5 km
    assert far.distance_dev_km == pytest.approx(5.0) and any("great-circle" in p for p in far.problems)


def test_distance_comes_from_sortdistance_or_the_text_with_a_comma():
    assert parse.item_distance({"sortdistance": 2.5, "distance": "9"}) == 2.5
    assert parse.item_distance({"distance": "2,741"}) == pytest.approx(2.741)
    assert parse.item_distance({}) is None


def test_answer_names():
    assert parse.answer_name(1, 7, "80636") == NAME
    assert parse.parse_answer_name(NAME) == (1, 7, "80636")
    assert parse.parse_answer_name("pass1_q7_80636.json") is None
    with pytest.raises(parse.ShapeError):
        parse.to_answer("something.json", answer_with([]))


# ---- records

def test_parse_record_maps_the_candidate_fields():
    it = item(2.7, "T9", townPart="Schwabing", locationSign="008", asproId="008", tscStatus=1, companyId="500", marketAreaId=7,
              federalStateId=111, countryId=parse.GERMANY_COUNTRY_ID, tscUrl="europe/germany/muenchen/x-str-7", huau=1,
              carService=1, truckService=0, sga=1, dkgk=0, oldtimer=1, phone="089 1", emailtsc="a@example.invalid", currentlyClosed=0)
    row, stats = parse.parse_record(it, "2026-10-05T12:00:00", RUN_DATE)
    assert row["location_id"] == "TUEV_SUED-T9" and row["source_id"] == "T9" and row["source_org"] == "TÜV SÜD"
    assert row["name"] == "TÜV SÜD Service-Center Schwabing"
    assert (row["street"], row["zip"], row["city"], row["country"]) == ("Hauptstr. 7", "80636", "München", "DE")
    assert (row["lat"], row["lng"]) == (48.1, 11.5)
    assert row["location_sign"] == "008" and row["aspro_id"] == "008"  # text: the leading zeros stay
    assert row["booking_url"] is None and row["source_profile_url"] is None  # not needed: left empty on purpose
    assert row["email"] == "a@example.invalid" and row["phone"] == "089 1" and row["website"] is None
    assert row["services"] == "huau; oldtimer; sga; carService" and row["heavy_vehicles"] is False
    assert (row["company_id"], row["tsc_status"], row["huau"], row["currently_closed"]) == ("500", 1, 1, False)
    assert stats == {"opening_hours_unparsed": 0, "notice_window_missing": 0}


def test_name_falls_back_to_town_and_a_bare_label():
    assert parse.parse_record(item(1, "a", town="Ulm"), "t", RUN_DATE)[0]["name"] == "TÜV SÜD Service-Center Ulm"
    assert parse.parse_record(item(1, "a", town=""), "t", RUN_DATE)[0]["name"] == "TÜV SÜD Service-Center"


def test_street_number_is_not_repeated_and_zip_is_padded():
    assert parse.parse_record(item(1, "a", street="Hauptstr. 7", streetnumber=7), "t", RUN_DATE)[0]["street"] == "Hauptstr. 7"
    assert parse.parse_record(item(1, "a", zipcode="1067"), "t", RUN_DATE)[0]["zip"] == "01067"
    assert parse.parse_record(item(1, "a", zipcode=None, plz=80636), "t", RUN_DATE)[0]["zip"] == "80636"  # corporate-locator spelling


def test_other_countries_keep_their_country_id():
    assert parse.parse_record(item(1, "a", countryId="42"), "t", RUN_DATE)[0]["country"] == "42"


def test_location_keys_are_matched_case_insensitively():
    row = parse.parse_record(item(1, "a", HUAU=1, TscStatus="1", ZipCode="80636"), "t", RUN_DATE)[0]
    assert row["huau"] == 1 and row["tsc_status"] == 1


def test_heavy_vehicles_from_truck_service_or_trailer_size():
    assert parse.heavy_vehicles(parse.ci_map({"truckService": 1}))
    assert parse.heavy_vehicles(parse.ci_map({"truckService": 0, "trailer_biggest": 3}))
    assert not parse.heavy_vehicles(parse.ci_map({"truckService": 0, "trailer_biggest": 0}))
    assert parse.services_of({"trailerBig": 1, "trailer_biggest": 3}) == "trailerBig"  # the size is not a service
    real = {"nphuService": 1, "hudbservice": "1", "twentyfourhourservice": 0, "serviceEce": 1, "huau": 1, "sgamail": "x@example.invalid"}
    assert parse.services_of(real) == "huau; nphuService; hudbservice"  # any key ending in "service", only when set
    assert parse.services_of({"trailer_biggest": 3}) is None


# ---- notices

def test_current_notices_use_the_time_window():
    ts = lambda y, m, d: int(pd.Timestamp(y, m, d, 12, tz="UTC").timestamp())  # noqa: E731
    lc = parse.ci_map({
        "notificationText": "<p>Heute <b>gültig</b></p>", "notificationText_start": ts(2026, 10, 1), "notificationText_end": ts(2026, 10, 31),
        "notificationText2": "abgelaufen", "notificationText_start2": ts(2019, 8, 1), "notificationText_end2": ts(2019, 9, 1),
        "notificationText3": "noch nicht", "notificationText_start3": ts(2026, 11, 1), "notificationText_end3": "0",
    })
    texts, missing = parse.current_notices(lc, RUN_DATE)
    assert texts == ["Heute gültig"] and missing == 0
    assert parse.current_notices(lc, date(2026, 11, 2))[0] == ["noch nicht"]  # 0 = no end


def test_a_notice_without_window_fields_counts_as_current_and_is_counted():
    texts, missing = parse.current_notices(parse.ci_map({"notificationText": "Hinweis"}), RUN_DATE)
    assert texts == ["Hinweis"] and missing == 1
    assert parse.current_notices(parse.ci_map({"notificationText": "  "}), RUN_DATE) == ([], 0)


def test_unix_date_handles_zero_text_and_milliseconds():
    assert parse.unix_date(0) is None and parse.unix_date("") is None and parse.unix_date(None) is None
    assert parse.unix_date("1567029600") == date(2019, 8, 28)
    assert parse.unix_date(1567029600000) == date(2019, 8, 28)


# ---- opening hours

def test_opening_hours_from_separate_range_keys():
    times = [
        {"dayId": 2, "morningFrom": "07:30", "morningTo": "12:00", "afternoonFrom": "13:00", "afternoonTo": "16:00", "isSpecialTime": 0},
        {"dayId": 1, "morningFrom": "7:30", "morningTo": "12:00", "afternoonFrom": "00:00", "afternoonTo": "00:00"},
        {"dayId": 6, "morningFrom": "", "morningTo": ""},
    ]
    assert parse.flatten_times(times) == ("Mo 07:30–12:00; Di 07:30–12:00, 13:00–16:00", True)


def test_opening_hours_from_one_string_per_range_and_nested_values():
    assert parse.flatten_times([{"dayId": 5, "morning": "08:00-12:00", "afternoon": "13:00 - 15:30", "isSpecialTime": 1}])[0] == "Fr 08:00–12:00, 13:00–15:30 (special)"
    assert parse.flatten_times([{"dayId": 3, "morning": {"from": "08:00", "to": "12:00"}}])[0] == "Mi 08:00–12:00"


def test_unreadable_or_missing_opening_hours():
    assert parse.flatten_times([]) == (None, True)
    assert parse.flatten_times(None) == (None, True)
    assert parse.flatten_times([{"dayId": 1, "open": "yes"}]) == (None, False)
    row, stats = parse.parse_record({**item(1, "a"), "times": [{"dayId": 1, "open": "yes"}]}, "t", RUN_DATE)
    assert row["opening_hours"] is None and stats["opening_hours_unparsed"] == 1


# ---- across answers

def test_merge_dedupes_by_tsc_id_and_reports_address_conflicts():
    a1 = parse.to_answer("pass01_q001_80636.json", answer_with([item(1, "a"), item(2, "b")]))
    moved = item(1, "b", street="Andere Str.")
    a2 = parse.to_answer("pass01_q002_80686.json", answer_with([item(1, "b", street="Andere Str."), item(2, "c")]))
    items, conflicts = parse.merge_items([a1, a2])
    assert set(items) == {"a", "b", "c"} and items["b"]["location"]["street"] == "Hauptstr."  # first seen wins
    assert [c["tsc_id"] for c in conflicts] == ["b"] and len(conflicts[0]["addresses"]) == 2
    assert parse.merge_items([a1, a1])[1] == []
    assert moved["location"]["street"] == "Andere Str."


def test_located_sites_and_key_fill():
    ans = parse.to_answer(NAME, answer_with([item(1, "a"), item(2, "b", lat="")]))
    items, _ = parse.merge_items([ans])
    sites = parse.located_sites(items)
    assert sites["id"].tolist() == ["a"] and sites["lat"].tolist() == [48.1]
    fill = parse.key_fill(items)
    assert fill["tscId"] == {"filled": 2, "rate": 1.0} and fill["lat"] == {"filled": 1, "rate": 0.5}


def test_the_fixture_parses_into_a_frame_with_the_declared_columns(answer_json):
    ans = parse.to_answer(NAME, answer_json)
    items, _ = parse.merge_items([ans])
    df, stats = parse.build_frame(list(items.values()), "2026-10-05T12:00:00", RUN_DATE)
    assert list(df.columns) == parse.COLUMNS and len(df) == 50
    assert stats == {"opening_hours_unparsed": 0, "notice_window_missing": 0}
    assert df["opening_hours"].notna().all() and df["location_sign"].map(type).eq(str).all()
    assert df["email"].str.endswith("@example.invalid").all()  # synthetic: no personal data
