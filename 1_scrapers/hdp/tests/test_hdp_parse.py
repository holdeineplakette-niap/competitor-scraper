"""Offline tests for hdp.parse and hdp.checks, including the saved real answer of 2026-10-07 (19 sites, no personal data)."""

from __future__ import annotations

import json

import pytest
from core import paths

from hdp import checks, parse
from hdp.simulate import sample_location, sample_payload

FIXTURE = paths.fixtures_dir("hdp") / "response_all_public_locations_2026-10-07.json"


@pytest.fixture(scope="module")
def real():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_real_answer_gives_one_row_per_location_id_not_per_slug(real):
    _, locs = parse.envelope(real)
    df = parse.to_frame(locs, "2026-10-07T10:00:00")
    assert len(locs) == 19 and len(df) == 19 and df["location_id"].is_unique
    assert sum(len(l["slugs"]) for l in locs) > 19  # the aliases exist and are ignored as sites
    assert set(df["source_org"]) == {"HDP"} and df["in_scope"].all()


def test_the_real_answer_passes_every_hard_check(real):
    _, locs = parse.envelope(real)
    allc = checks.response_checks(real) + checks.location_checks(locs)
    assert [c for c in allc if c["severity"] == "hard" and not c["ok"]] == []


def test_the_address_not_the_name_is_the_identity_of_a_site(real):
    _, locs = parse.envelope(real)
    df = parse.to_frame(locs, "t").set_index("source_id")
    assert (df.loc["Fürth", "zip"], df.loc["Fürth", "city"]) == ("90431", "Nürnberg")
    assert (df.loc["Schwabach", "zip"], df.loc["Schwabach", "city"], df.loc["Schwabach", "house_number"]) == ("91186", "Büchenbach", "15a")
    assert df.loc["Kohlscheid", "city"] == "Herzogenrath"


def test_a_missing_public_name_keeps_the_row_and_is_flagged(real):
    _, locs = parse.envelope(real)
    df = parse.to_frame(locs, "t").set_index("source_id")
    assert df.loc["Regensburg", "name"] == "" and "no_public_name" in df.loc["Regensburg", "doubt_flags"]
    assert df.loc["Regensburg", "street"] == "Kirchmeierstraße 20"


def test_opening_hours_are_compact_and_slot_data_is_not_exported():
    row = parse.to_row(sample_location(0, openingHours={"monday": {"isOpen": True, "open": "9:00", "close": "17:00"}, "sunday": {"isOpen": False}}), "t")
    assert row["opening_hours"] == "Mo 9:00-17:00; So closed"
    assert "slot" not in " ".join(parse.COLUMNS).lower()


def test_envelope_of_an_unexpected_shape_is_none_not_an_exception():
    assert parse.envelope({"error": {"message": "x"}}) == (None, None)
    assert parse.envelope({"result": []}) == (None, None)
    with pytest.raises(parse.ParseError):
        parse.parse_payload("[1, 2]")
    with pytest.raises(parse.ParseError):
        parse.parse_payload("nope")


def _failed(cs):
    return [c["check"] for c in cs if c["severity"] == "hard" and not c["ok"]]


def test_response_checks_fail_on_success_false_missing_or_empty_locations():
    assert _failed(checks.response_checks(sample_payload())) == []
    assert _failed(checks.response_checks({"result": {"success": False, "locations": [sample_location()]}})) == ["result.success is true"]
    assert _failed(checks.response_checks({"result": {"success": True}})) == ["result.locations is a non-empty list"]
    assert _failed(checks.response_checks({"result": {"success": True, "locations": []}})) == ["result.locations is a non-empty list"]
    assert len(_failed(checks.response_checks({"error": {}}))) == 2


def test_location_checks_fail_on_a_missing_postcode_or_coordinates_or_a_bad_id():
    ok = sample_location(0)
    assert _failed(checks.location_checks([ok])) == []
    assert _failed(checks.location_checks([ok, sample_location(1, postcode="")])) == ["every location has a postcode"]
    assert _failed(checks.location_checks([ok, sample_location(1, postcode=None)])) == ["every location has a postcode"]
    assert _failed(checks.location_checks([ok, sample_location(1, latitude=None)])) == ["every location has numeric coordinates"]
    assert _failed(checks.location_checks([ok, sample_location(1, longitude="n/a")])) == ["every location has numeric coordinates"]
    assert _failed(checks.location_checks([ok, sample_location(1, latitude=0.0, longitude=0.0)])) == ["every coordinate lies in Germany"]
    assert _failed(checks.location_checks([ok, sample_location(1, id="")])) == ["every location is an object with a unique, non-empty id"]
    assert _failed(checks.location_checks([ok, sample_location(0)])) == ["every location is an object with a unique, non-empty id"]


def test_a_changed_count_is_a_warning_text_never_a_check():
    assert checks.count_warning(19, {"in_scope": 19, "run_folder": "x"}) is None
    assert checks.count_warning(19, None) is None
    assert "18 in x -> 19 now" in checks.count_warning(19, {"in_scope": 18, "run_folder": "x"})
