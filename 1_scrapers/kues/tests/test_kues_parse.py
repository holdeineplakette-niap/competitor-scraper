"""KÜS parsing against the anonymised 80636 fixture (no personal data)."""

import json

import pandas as pd
import pytest

from core import paths
from kues import parse

FIXTURE = paths.fixtures_dir("kues") / "response_muenchen_80636_2026-10-05.json"


@pytest.fixture(scope="module")
def records():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def frame(records, centroids):
    df = parse.build_frame(records, "2026-10-05T12:00:00")
    return parse.enrich_german(parse.add_counts(df), centroids)


def row(frame, adressid):
    return frame[frame["source_id"] == str(adressid)].iloc[0]


def test_fixture_keeps_the_structure_of_the_real_response(records):
    assert len(records) == 83 and len({r["adressid"] for r in records}) == 83
    assert [r["pruefungvorortmoeglich"] for r in records].count("1") == 33
    assert [r["keinestrasse"] for r in records].count("1") == 7
    assert {"adressid", "lizenznehmer", "inbreite", "inlaenge", "entfernung", "plzorder"} <= set(records[0])
    assert all("@example" in r["email"] or "@kues.example" in r["email"] or "@mail.example" in r["email"] for r in records if r["email"])


def test_columns_and_ids(frame):
    assert list(frame.columns) == parse.COLUMNS
    assert "fax" not in frame.columns
    r = row(frame, 24795)
    assert r["location_id"] == "KUES-24795" and r["source_org"] == "KÜS" and r["source_id"] == "24795"
    assert r["kues_licensee"] == "I0828" and r["lat"] == pytest.approx(48.1487, abs=0.01)


def test_salutation_names_get_a_display_name(frame):
    assert row(frame, 24795)["name"] == "Herrn"  # raw name stays
    assert row(frame, 24795)["display_name"] == "Dipl.-Ing. (FH) Max Muster3"
    assert row(frame, 11418)["name"] == "Herrn Dipl.-Ing. (FH)"
    assert row(frame, 11418)["display_name"] == "Dipl.-Ing. (FH) Max Muster34"
    assert row(frame, 13938)["display_name"] == row(frame, 13938)["name"]  # a real name is used as it is
    assert (frame["display_name"].str.strip() != "Herrn").all()


@pytest.mark.parametrize("name, addon, expected", [
    ("Herrn", "Dipl.-Ing. (FH) Max Muster", "Dipl.-Ing. (FH) Max Muster"),
    ("Frau", "Erika Muster", "Erika Muster"),
    ("Herrn Dr.", "Hans Muster", "Dr. Hans Muster"),
    ("Herrn", None, "Herrn"),
    ("Ingenieurbüro Muster", "Dipl.-Ing. X", "Ingenieurbüro Muster"),
    ("Herrnhut Autohaus", "x", "Herrnhut Autohaus"),
])
def test_display_name_rules(name, addon, expected):
    assert parse.display_name(name, addon) == expected


def test_keinestrasse_rows_are_plz_only_without_coordinates(frame):
    no_street = frame[frame["location_precision"] == "plz_only"]
    assert len(no_street) == 7
    assert no_street["lat"].isna().all() and no_street["lng"].isna().all() and no_street["street"].isna().all()
    assert (frame.loc[frame["location_precision"] == "address", "lat"].notna()).all()


def test_zip_is_padded_and_country_and_state_come_from_geonames(frame):
    assert parse.zip_of(" 1067") == "01067" and parse.zip_of("80636") == "80636" and parse.zip_of(None) is None
    assert (frame["zip"].str.len() == 5).all()
    assert (frame["country"] == "DE").all() and (frame["state"] == "Bayern").all()
    assert frame["plz_distance_km"].max() < 25


def test_a_site_far_from_its_plz_loses_the_country_and_is_flagged(records, centroids):
    moved = [dict(r) for r in records[1:4]]  # records[0] has no coordinates
    moved[0]["lat"] = str(float(moved[0]["lat"]) + 2.0)  # ~220 km north of its PLZ
    df = parse.enrich_german(parse.build_frame(moved, "t"), centroids)
    assert pd.isna(df.loc[0, "country"]) and df.loc[0, "needs_review"]
    assert "country unverified" in df.loc[0, "needs_review_reason"] and "coordinate far from PLZ" in df.loc[0, "needs_review_reason"]
    assert df.loc[1, "country"] == "DE"


def test_same_address_two_records_are_kept_and_counted(frame):
    pair = frame[frame["zip"] == "85221"]
    assert len(pair) == 2 and (pair["same_address_count"] == 2).all()  # Prüfstelle + Ingenieurbüro, never deduped
    assert (frame.loc[frame["street"].isna(), "same_address_count"] == 1).all()


def test_licensee_site_count_for_multi_site_licensees(frame):
    for code in ("I0500", "I0119"):  # SV TEAM AG runs sites under two licence codes
        rows = frame[frame["kues_licensee"] == code]
        assert len(rows) == 5 and (rows["licensee_site_count"] == 5).all()
    assert (frame.loc[frame["kues_licensee"] == "I0828", "licensee_site_count"] == 1).all()


def test_contact_fields(frame):
    r = row(frame, 24795)
    assert r["phone"] == "0000-000000" and r["email"] == "test24795@mail.example"
    assert pd.isna(row(frame, 31572)["email"])
    assert pd.isna(row(frame, 22663)["email"])  # "" in the source becomes empty
    assert parse.clean("  a   b ") == "a b" and parse.clean("") is None


def test_partner_alias_becomes_a_profile_url():
    rec = {"adressid": 1, "plz": "80636", "ort": "München", "partneralias": "muster", "pruefungvorortmoeglich": "1", "keinestrasse": "0"}
    assert parse.parse_record(rec, "t")["source_profile_url"] == "https://partner.kues.de/muster"


# ---- HDP hook

def test_hdp_column_is_empty_without_the_file(frame, tmp_path):
    assert parse.load_hdp_rules(root=tmp_path) is None
    assert parse.mark_hdp(frame, None)["is_hdp_site"].isna().all()


def test_hdp_rules_match_on_all_non_empty_columns(frame, tmp_path):
    path = tmp_path / "hdp_sites.csv"
    path.write_text(
        "kues_licensee,adressid,address\n"
        "I0500,13938,\n"            # by id (and licensee)
        "I0119,,Teststraße 4 81671\n"  # licensee + address (4651's street is "Teststraße 4")
        "I9999,24795,\n"            # licensee differs from the record: no match
        ",,\n",                     # empty row: ignored
        encoding="utf-8",
    )
    rules = parse.load_hdp_rules(path)
    assert len(rules) == 3
    marked = parse.mark_hdp(frame, rules)
    hits = set(marked.loc[marked["is_hdp_site"], "source_id"])
    assert hits == {"13938", "4651"}


# ---- dedupe by adressid

def test_merge_records_dedupes_and_flags_content_conflicts(records):
    from kues import fetch, run

    base = fetch.to_response(fetch.LIST_KUES, "80636", records[:3])
    again = [dict(r) for r in records[:3]]
    again[0]["entfernung"] = "99.0"  # query-dependent: no conflict
    again[0]["inbreite"] = "1.0"
    again[1]["tel1"] = "changed"  # content: conflict
    second = fetch.to_response(fetch.LIST_TECHNIK, "80636", again)
    merged, lists, conflicts = run.merge_records([base, second])
    assert len(merged) == 3
    assert conflicts == [{"adressid": str(records[1]["adressid"]), "fields": ["tel1"]}]
    assert merged[str(records[1]["adressid"])]["tel1"] != "changed"  # first seen wins
    assert {run.kues_list_value(v) for v in lists.values()} == {"both"}
    only_technik = run.merge_records([fetch.to_response(fetch.LIST_TECHNIK, "1", records[:1])])[1]
    assert run.kues_list_value(only_technik[str(records[0]["adressid"])]) == "kues_technik"
