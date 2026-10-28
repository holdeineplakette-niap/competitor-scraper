"""KÜS classification: the vorort rule and the conflict flags (flags never overrule the rule)."""

import json

import pandas as pd
import pytest

from core import paths
from kues import classify, parse

FIXTURE = paths.fixtures_dir("kues") / "response_muenchen_80636_2026-10-05.json"


def make(**overrides):
    rec = {
        "adressid": 1, "lizenznehmer": "I0001", "name": "Ingenieurbüro Muster", "zusatz": None, "plz": "80636", "ort": "München",
        "strasse": "Teststraße 1", "tel1": "0000-000000", "fax": None, "email": None, "http": "www.example.invalid", "lat": "48.15", "lon": "11.55",
        "inbreite": "48.15", "inlaenge": "11.58", "pruefungvorortmoeglich": "1", "keinestrasse": "0", "entfernung": "1.0", "plzorder": 2,
    }
    rec.update(overrides)
    return rec


def classified(*records):
    df = parse.build_frame(list(records), "t")
    df["needs_review"] = False
    return classify.classify_frame(df)


def test_vorort_1_is_a_pruefstelle_in_scope():
    r = classified(make()).iloc[0]
    assert (r["site_type"], r["in_scope"], r["site_type_evidence"]) == ("pruefstelle", True, "vorort:1")
    assert r["scope_reason"] is None and r["doubt_flags"] is None and not r["needs_review"]
    assert r["psp"] == "No"


def test_vorort_0_is_a_partner_office_in_scope_tagged_psp_yes():
    # 2026-10-07 (management via Nikos): the vorort = 0 records are included, tagged psp = Yes
    r = classified(make(pruefungvorortmoeglich="0")).iloc[0]
    assert (r["site_type"], r["in_scope"], r["site_type_evidence"]) == ("partner_office", True, "vorort:0")
    assert r["psp"] == "Yes" and r["scope_reason"] is None and not r["needs_review"]


@pytest.mark.parametrize("value", [None, "", "2", "yes"])
def test_a_missing_or_invalid_flag_is_unknown_and_needs_review(value):
    r = classified(make(pruefungvorortmoeglich=value)).iloc[0]
    assert (r["site_type"], r["in_scope"], r["psp"]) == ("unknown", False, None)
    assert r["doubt_flags"] == classify.FLAG_VORORT_INVALID and r["needs_review"]


@pytest.mark.parametrize("name, addon", [
    ("KÜS-Team München Mitte", None), ("Kfz-Prüfstelle Muster", None), ("Prüfzentrum Nord", None),
    ("SV TEAM AG", "KÜS-Team München Süd"), ("Herrn", "Kfz-Prüfstelle Muster"),
])
def test_name_says_pruefstelle_but_flag_says_no_is_kept_and_flagged_for_review(name, addon):
    # 2026-10-07: no certain cause to exclude (the name contradicts the flag), so the record stays in scope
    r = classified(make(name=name, zusatz=addon, pruefungvorortmoeglich="0")).iloc[0]
    assert (r["site_type"], r["in_scope"], r["site_type_evidence"]) == ("unknown", True, "vorort:0")
    assert r["scope_reason"] is None and r["psp"] == "Yes"
    assert r["doubt_flags"].split("; ")[0] == classify.FLAG_NAME_PRUEFSTELLE_VORORT0  # (a station word plus the website also adds the info flag)
    assert r["needs_review"] and r["needs_review_reason"] == classify.FLAG_NAME_PRUEFSTELLE_VORORT0


@pytest.mark.parametrize("adressid", sorted(classify.VERIFIED_NOT_PRUEFSTELLE))
def test_a_record_nikos_checked_by_hand_stays_out_with_that_reason(adressid):
    r = classified(make(adressid=int(adressid), name="Kfz-Prüfstelle Muster", pruefungvorortmoeglich="0")).iloc[0]
    assert (r["site_type"], r["in_scope"]) == ("partner_office", False)
    assert r["scope_reason"] == classify.REASON_VERIFIED_NOT_PRUEFSTELLE
    assert r["doubt_flags"].split("; ")[0] == classify.FLAG_NAME_PRUEFSTELLE_VORORT0 and r["needs_review"]


def test_a_vorort_0_record_without_a_prufstelle_name_is_a_psp_in_scope_with_the_evidence_flag():
    r = classified(make(name="KÜS Ingenieurbüro Muster", pruefungvorortmoeglich="0", email="info@muster-kfz.invalid")).iloc[0]
    assert (r["site_type"], r["in_scope"], r["psp"]) == ("partner_office", True, "Yes")
    assert classify.FLAG_STRONG_EVIDENCE in r["doubt_flags"] and classify.FLAG_NAME_PRUEFSTELLE_VORORT0 not in r["doubt_flags"]


def test_the_same_name_with_vorort_1_is_not_a_conflict():
    r = classified(make(name="KÜS-Team München Mitte")).iloc[0]
    assert r["doubt_flags"] is None


def test_vorort_1_without_street_or_coordinates_is_flagged_but_stays_in_scope():
    no_street = classified(make(strasse=None, lat=None, lon=None, keinestrasse="1")).iloc[0]
    assert no_street["site_type"] == "pruefstelle" and no_street["in_scope"]
    assert no_street["doubt_flags"] == classify.FLAG_VORORT1_NO_ADDRESS and no_street["needs_review"]
    no_coords = classified(make(lat=None, lon=None)).iloc[0]  # has a street, the server has no coordinates
    assert no_coords["doubt_flags"] == classify.FLAG_VORORT1_NO_ADDRESS
    assert classified(make(strasse=None, lat=None, lon=None, keinestrasse="1", pruefungvorortmoeglich="0")).iloc[0]["doubt_flags"] is None


def test_review_reasons_add_to_existing_ones():
    df = parse.build_frame([make(name="Kfz-Prüfstelle", pruefungvorortmoeglich="0")], "t")
    df["needs_review"] = True
    df["needs_review_reason"] = "coordinate far from PLZ"
    out = classify.classify_frame(df)
    assert out.iloc[0]["needs_review_reason"] == f"coordinate far from PLZ; {classify.FLAG_NAME_PRUEFSTELLE_VORORT0}"


# ---- derived columns and info flags (pattern analysis 2026-10-05): evidence only, no scope effect, no needs_review

@pytest.mark.parametrize("name, addon, expected", [
    ("Herrn", "Dipl.-Ing. (FH) Max Muster", "herrn"), ("Frau Dipl.-Ing.", "Erika Muster", "herrn"),
    ("KÜS-Kfz-Prüfstelle Dachau", None, "pruefstelle"), ("Ingenieurbüro Muster", "Kfz-Prüfzentrum", "pruefstelle"),
    ("SV TEAM AG", None, "team"), ("KÜS-Team München Mitte", None, "team"), ("KÜS Akademie GmbH", None, "kues_other"),
    ("Ingenieurbüro Muster", None, "ingenieurbuero"), ("Ing.-Büro Muster", None, "ingenieurbuero"), ("Ing.Büro Muster", None, "ingenieurbuero"),
    ("Ingenieurbüo Muster", None, "ingenieurbuero"), ("Ing.-und Kfz.-Sachverständigenbüro", None, "ingenieurbuero"),
    ("SV-Büro Muster", None, "sv_buero"), ("Kfz-Sachverständiger", "A.O. Büro", "sv_buero"), ("Dipl.-Ing. (FH) Max Muster", None, "person_title"),
    ("Muster Technik GmbH", None, "company"), ("Muster", None, "other"),
])
def test_name_class(name, addon, expected):
    row = parse.parse_record(make(name=name, zusatz=addon), "t")
    assert classify.name_class(row) == expected
    assert classified(make(name=name, zusatz=addon)).iloc[0]["name_class"] == expected


@pytest.mark.parametrize("email, expected", [
    (None, False), ("", False), ("x@t-online.de", False), ("x@gmx.de; y@web.de", False), ("x@kues.de", True),
    ("x@gmail.com; info@ibag-kues.de", True), ("x@kues-muenchen.de", True), (" stefan@kues-mobil.de", True),
])
def test_business_email(email, expected):
    assert classify.has_business_email(email) is expected


def flags_of(**overrides):
    r = classified(make(**overrides)).iloc[0]
    return set((r["doubt_flags"] or "").split("; ")) - {""}, r


def test_weak_evidence_flags_an_in_scope_row_with_no_station_word_website_or_business_email():
    flags, r = flags_of(http=None, email="x@t-online.de")
    assert flags == {classify.FLAG_WEAK_EVIDENCE} and r["in_scope"] and not r["needs_review"]  # information only
    assert flags_of(http=None, email="x@kues.de")[0] == set()  # business email
    assert flags_of(http=None, email="x@t-online.de", zusatz="KÜS-Prüfstelle")[0] == set()  # station word
    assert flags_of(pruefungvorortmoeglich="0", http=None, email="x@t-online.de")[0] == set()  # only for in-scope rows


def test_strong_evidence_flags_a_psp_row_with_two_signals():
    flags, r = flags_of(pruefungvorortmoeglich="0", http="www.example.invalid", email="x@kues.de")
    assert flags == {classify.FLAG_STRONG_EVIDENCE} and r["psp"] == "Yes" and not r["needs_review"]
    assert flags_of(pruefungvorortmoeglich="0", http="www.example.invalid", email="x@gmx.de")[0] == set()  # one signal


def test_contact_missing_only_for_in_scope_rows_without_phone_email_or_website():
    flags, r = flags_of(tel1=None, http=None, email=None, name="KÜS-Prüfstelle")
    assert flags == {classify.FLAG_CONTACT_MISSING} and not r["needs_review"]
    assert flags_of(tel1=None, http=None, email="x@gmx.de", name="KÜS-Prüfstelle")[0] == set()
    assert flags_of(pruefungvorortmoeglich="0", tel1=None, http=None, email=None)[0] == set()


def test_psp_like_names_in_scope_are_flagged_for_review():
    flags, r = flags_of(name="Autohaus Muster")
    assert flags == {classify.FLAG_NAME_PSP_LIKE} and r["in_scope"] and r["needs_review"]
    assert flags_of(name="Opel Werkstatt Muster", pruefungvorortmoeglich="0")[0] == set()  # a PSP is expected to look like a workshop
    assert flags_of(name="Ingenieurbüro für Fahrzeugtechnik")[0] == set()  # engineering offices are not PSPs


def test_missing_values_in_a_mixed_frame_count_as_missing_not_as_present():
    """Regression: in a frame where some rows have a website and others none, the missing ones are NaN, and bool(NaN) is True."""
    df = classified(
        make(adressid=1, strasse="Weg 1", http="www.example.invalid", email="x@kues.de"),
        make(adressid=2, strasse="Weg 2", http=None, email="x@t-online.de", tel1=None, name="Ingenieurbüro Muster"),
        make(adressid=3, strasse="Weg 3", http=None, email=None, tel1=None, name="Ingenieurbüro Muster"),
        make(adressid=4, strasse="Weg 4", pruefungvorortmoeglich="0", http=None, email=None, zusatz=None),
    ).set_index("source_id")
    assert df["website"].isna().sum() == 3  # NaN, not None, in this mixed frame
    flags = df["doubt_flags"].fillna("")
    assert flags["1"] == ""
    assert classify.FLAG_WEAK_EVIDENCE in flags["2"] and classify.FLAG_CONTACT_MISSING not in flags["2"]  # still has an email
    assert classify.FLAG_WEAK_EVIDENCE in flags["3"] and classify.FLAG_CONTACT_MISSING in flags["3"]
    assert flags["4"] == ""


def test_licensee_has_inscope_site_looks_at_the_other_rows_of_the_licence_code():
    df = classified(
        make(adressid=1, lizenznehmer="A", pruefungvorortmoeglich="1", strasse="Weg 1"),
        make(adressid=2, lizenznehmer="A", pruefungvorortmoeglich="0", strasse="Weg 2"),
        make(adressid=3, lizenznehmer="B", pruefungvorortmoeglich="0", strasse="Weg 3"),
        make(adressid=4, lizenznehmer="C", pruefungvorortmoeglich="1", strasse="Weg 4"),
        make(adressid=5, lizenznehmer="C", pruefungvorortmoeglich="1", strasse="Weg 5"),
    ).set_index("source_id")
    assert df["licensee_has_inscope_site"].to_dict() == {"1": False, "2": True, "3": False, "4": True, "5": True}
    assert len(df) == 5 and df["doubt_flags"].isna().all()  # a sibling is a column, not a flag; no row dropped


def test_shared_address_flags_never_merge_or_drop_rows():
    df = classified(
        make(adressid=1, strasse="Karl-Benz-Straße 12", plz="85221", pruefungvorortmoeglich="1"),
        make(adressid=2, strasse="Karl-Benz-Str. 12", plz="85221", pruefungvorortmoeglich="0"),
        make(adressid=3, strasse="Hafenweg 3", plz="44135", pruefungvorortmoeglich="1", lizenznehmer="X"),
        make(adressid=4, strasse="Hafenweg 3", plz="44135", pruefungvorortmoeglich="1", lizenznehmer="Y"),
        make(adressid=5, strasse="Nur 1", plz="44135", pruefungvorortmoeglich="0"),
        make(adressid=6, strasse=None, plz="44135", pruefungvorortmoeglich="1", lat=None, lon=None, keinestrasse="1"),
        make(adressid=7, strasse=None, plz="44135", pruefungvorortmoeglich="0", lat=None, lon=None, keinestrasse="1"),
    ).set_index("source_id")
    flags = df["doubt_flags"].fillna("")
    assert classify.FLAG_SAME_ADDRESS_AS_INSCOPE in flags["2"] and classify.FLAG_SHARED_ADDRESS_INSCOPE not in flags["1"]
    assert classify.FLAG_SHARED_ADDRESS_INSCOPE in flags["3"] and classify.FLAG_SHARED_ADDRESS_INSCOPE in flags["4"]
    assert "shared" not in flags["5"] and "same_address" not in flags["5"] and "same_address" not in flags["7"]
    assert classify.FLAG_SHARED_ADDRESS_INSCOPE not in flags["6"]  # no street: no address to share
    assert len(df) == 7 and not df.loc[["2", "3", "4"], "needs_review"].any()


def test_info_flags_never_set_needs_review_or_change_scope(centroids):
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))
    df = classify.classify_frame(parse.enrich_german(parse.add_counts(parse.build_frame(records, "t")), centroids))
    info = df["doubt_flags"].fillna("").map(lambda v: set(v.split("; ")) & set(classify.INFO_FLAGS))
    assert info.map(bool).any()
    only_info = df["doubt_flags"].fillna("").map(lambda v: set(v.split("; ")) - {""} <= set(classify.INFO_FLAGS))
    assert not df.loc[only_info, "needs_review"].any()
    assert df["in_scope"].sum() == 83 and set(df["name_class"]) <= {"herrn", "pruefstelle", "team", "kues_other", "ingenieurbuero", "sv_buero", "person_title", "company", "other"}


def test_empty_frame_passes_through():
    assert classify.classify_frame(parse.build_frame([], "t")).empty


def test_fixture_split_matches_the_recon(centroids):
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))
    df = parse.enrich_german(parse.add_counts(parse.build_frame(records, "t")), centroids)
    df = classify.classify_frame(df)
    assert df["in_scope"].sum() == 83 and (df["site_type"] == "partner_office").sum() == 50 and (df["site_type"] == "unknown").sum() == 0
    assert (df["psp"] == "No").sum() == 33 and (df["psp"] == "Yes").sum() == 50
    assert set(df["site_type"]) <= set(classify.SITE_TYPES)
    assert (df["in_scope"] == df["psp"].notna()).all()
