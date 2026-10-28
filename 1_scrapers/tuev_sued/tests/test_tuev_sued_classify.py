"""TÜV SÜD classification: scope rule, flags, rows are kept."""

import json
from datetime import date

import pandas as pd
import pytest

from core import enrich, paths
from tuev_sued import classify, parse

RUN_DATE = date(2026, 10, 5)


def frame(*specs):
    """One parsed row per spec dict (location fields); enriched with a tiny centroid table like the run does."""
    items = []
    for i, spec in enumerate(specs):
        loc = {"tscId": f"T{i}", "lat": "48.15", "lon": "11.54", "street": "Hauptstr.", "streetnumber": 1, "zipcode": "80636", "town": "München",
               "huau": 1, "tscStatus": 1, "companyId": "500", "countryId": parse.GERMANY_COUNTRY_ID}
        loc.update(spec)
        items.append({"tscId": f"T{i}", "distance": "1.0", "sortdistance": 1.0, "location": loc})
    df, _ = parse.build_frame(items, "2026-10-05T12:00:00", RUN_DATE)
    centroids = pd.DataFrame({"zip": ["80636"], "city": ["München"], "state": ["Bayern"], "lat": [48.15], "lng": [11.54]})
    return classify.classify_frame(enrich.enrich(df, centroids, check=df["country"] == "DE"))


def flags(df):
    """doubt_flags as a list with None for no flag (pandas stores the missing ones as NaN)."""
    return [None if pd.isna(v) else v for v in df["doubt_flags"]]


def test_in_scope_needs_huau_status_and_germany():
    df = frame({}, {"huau": 0}, {"tscStatus": 0}, {"countryId": "42"}, {"huau": 0, "tscStatus": 0})
    assert df["in_scope"].tolist() == [True, False, False, False, False]
    assert df["site_type"].tolist() == ["pruefstelle", "unknown", "unknown", "unknown", "unknown"]
    assert df.loc[0, "site_type_evidence"] == "huau:1;tscStatus:1;country:DE" and pd.isna(df.loc[0, "scope_reason"])
    assert df.loc[1, "scope_reason"] == classify.SCOPE_REASONS[classify.FLAG_HUAU_NOT_1]
    assert df.loc[4, "doubt_flags"] == "huau_not_1; tsc_status_not_1"
    assert len(df) == 5  # nothing is dropped


def test_a_clean_in_scope_row_has_no_flags_and_needs_no_review():
    df = frame({})
    assert pd.isna(df.loc[0, "doubt_flags"]) and not df.loc[0, "needs_review"]


def test_rows_outside_the_rule_need_review():
    df = frame({"huau": 0}, {"countryId": "42"})
    assert df["needs_review"].all()
    assert all(r.startswith("huau_not_1") or r.startswith("not_germany") for r in df["needs_review_reason"])


def test_missing_scope_fields_are_not_in_scope():
    df = frame({"huau": None, "tscStatus": None})
    assert not df.loc[0, "in_scope"] and df.loc[0, "needs_review"]


def test_uo_notice_flags_only_a_current_notice_and_keeps_the_row_in_scope():
    notice = "Prüfstelle der Überwachungsorganisation – Leistungen und Entgelte weichen ab"
    expired = {"notificationText": notice, "notificationText_start": 1567029600, "notificationText_end": 1567129600}
    df = frame({"notificationText": notice, "notificationText_start": 0, "notificationText_end": 0}, expired, {"notificationText": "pruefstelle der ueberwachungsorganisation"})
    assert flags(df) == ["uo_notice", None, "uo_notice"]
    assert df["in_scope"].tolist() == [True, True, True] and df["needs_review"].tolist() == [True, False, True]
    assert "uo_notice" in df.loc[0, "needs_review_reason"]


def test_other_operators_are_prüfstellen_without_a_flag_or_review():
    """Partner sites and TÜV Hessen / TÜV Hanse stations in the finder count like the own Service-Center (decision 2026-10-06)."""
    own, hanse, hessen, hessen_partner, partner, unknown_id, missing = "500", "501", "502", "505", "1255511964821267510075", "999", None
    df = frame(*({"companyId": c} for c in (own, hanse, hessen, hessen_partner, partner, unknown_id, missing)))
    assert flags(df) == [None] * 7
    assert df["in_scope"].all() and not df["needs_review"].any() and set(df["site_type"]) == {"pruefstelle"}
    assert df["operator_group"].tolist()[:6] == ["own", "tuev_hanse", "tuev_hessen", "tuev_hessen_partner", "tuev_sued_partner", "other_999"]
    assert pd.isna(df.loc[6, "operator_group"])


def test_coordinate_far_from_the_plz_becomes_a_flag():
    df = frame({}, {"lat": "49.5", "lon": "11.54"})  # ~150 km from the centroid of 80636
    assert flags(df) == [None, "coordinate_far_from_plz"]
    assert df.loc[1, "needs_review"] and enrich.REASON_FAR_FROM_PLZ in df.loc[1, "needs_review_reason"]
    assert df.loc[1, "in_scope"]  # a flag, never a reason to drop
    assert df.loc[1, "needs_review_reason"].count(enrich.REASON_FAR_FROM_PLZ) == 1


def test_several_flags_on_one_row():
    df = frame({"companyId": "501", "huau": 0, "notificationText": "Prüfstelle der Überwachungsorganisation"})
    assert df.loc[0, "doubt_flags"] == "huau_not_1; uo_notice"


def test_empty_frame_is_returned_unchanged():
    empty = pd.DataFrame(columns=parse.COLUMNS)
    assert classify.classify_frame(empty).empty


def test_the_fixture_is_all_in_scope():
    answer = json.loads((paths.fixtures_dir("tuev_sued") / "response_muenchen_80636_synthetic_2026-10-05.json").read_text(encoding="utf-8"))
    items = {i["tscId"]: i for i in answer["finaltsc"] if i["location"]["huau"] == 1 and i["location"]["tscStatus"] == 1 and i["location"]["companyId"] == "500"}
    df, _ = parse.build_frame(list(items.values()), "t", RUN_DATE)
    centroids = pd.DataFrame({"zip": df["zip"].unique(), "city": "x", "state": "Bayern", "lat": 48.1, "lng": 11.5})
    out = classify.classify_frame(enrich.enrich(df, centroids, check=df["country"] == "DE"))
    assert out["in_scope"].all() and set(out["site_type"]) == {"pruefstelle"}

