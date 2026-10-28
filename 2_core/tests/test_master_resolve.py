import pytest

from core import geo
from core import master_resolve as rs
from core import master_schema as ms
from core import normalise as nm
from master_fixtures import CENTROIDS

CENT = geo.centroid_lookup(CENTROIDS)
KNOWN = set(CENT)
STATES = dict(zip(CENTROIDS["zip"], CENTROIDS["state"]))


def rec(slug, lid, **kw):
    r = {"slug": slug, "family": ms.family_of(slug), "rank": ms.rank(slug), "location_id": lid, "name": "", "street": "", "zip": "",
         "city": "", "lat": None, "lng": None, "phone": "", "email": "", "website": "", "booking_url": "", "profile_url": "",
         "opening_hours": "", "services": "", "operator_name": "", "operator_group": "", "psp": "No", "uo": "TÜV SÜD", "site_type": "unknown",
         "site_type_evidence": f"{slug}:x", "in_scope": True, "scope_reason": "", "scraped_at": "2026-10-06T10:00:00", "flags": [],
         "coord_hint": "", "sign": "", "veto": ""}
    r.update(kw)
    r["akey"] = nm.address_key(r["street"], r["zip"]) if nm.has_street(r["street"]) and r["zip"] else None
    return r


def resolve(*recs, agg=()):
    g = rs.resolve_group(0, list(recs), list(agg), CENT, STATES, KNOWN)
    rs.finish(g, [], KNOWN)
    return g


def doubts(g) -> list[str]:
    return g.row["doubt_flags"].split("; ")


def test_bad_camberg_style_wrong_hessen_coordinates_lose_to_tuev_sued():
    """Hessen uid 433 carries Oberursel's coordinates (22 km off its own PLZ): the SÜD coordinates must win."""
    s = rec("tuev_sued", "S433", street="Am Bahnhof 1", zip="65520", city="Bad Camberg", lat=50.2960, lng=8.2650)
    h = rec("tuev_hessen", "H433", street="Am Bahnhof 1", zip="65520", city="Bad Camberg", lat=50.2000, lng=8.5800,
            flags=["coords_far_from_plz", "coords_shared_with_other_address"], uo="TÜV Hessen", site_type="own_station")
    g = resolve(s, h)
    assert (g.row["lat"], g.row["lng"]) == ("50.296000", "8.265000") and g.row["coord_source"] == "tuev_sued"
    assert float(g.row["plz_distance_km"]) < 1
    assert "coords_far_from_plz" not in g.row["doubt_flags"]  # the merge resolved it: no flag left
    assert any(l["field"] == "lat/lng" and "tuev_hessen" in l["dropped"] for l in g.logs)  # the dropped values stay in the log
    assert g.row["needs_review"] == "False"


def test_good_hessen_coordinates_are_used_if_sued_has_the_bad_ones():
    s = rec("tuev_sued", "S1", street="Am Bahnhof 1", zip="65520", city="Bad Camberg", lat=50.2000, lng=8.5800)
    h = rec("tuev_hessen", "H1", street="Am Bahnhof 1", zip="65520", city="Bad Camberg", lat=50.2960, lng=8.2650)
    assert resolve(s, h).row["coord_source"] == "tuev_hessen"


def test_coordinates_outside_germany_or_without_a_checkable_plz_never_pass():
    r = rec("dekra", "D1", street="Hauptstr. 5", zip="01067", city="Dresden", lat=48.85, lng=2.35)
    g = resolve(r)
    assert g.row["lat"] == "" and g.need_geo and "coords_unresolved" in doubts(g)
    r2 = rec("dekra", "D2", street="Hauptstr. 5", zip="99999", city="Nirgendwo", lat=51.0, lng=10.0)
    g2 = resolve(r2)
    assert g2.row["lat"] == "" and [x for x in doubts(g2) if x in ms.DOUBT_REASONS] == ["plz_invalid"]  # one root cause, one reason


def test_a_vuek_style_nominatim_source_coordinate_is_labelled_nominatim():
    r = rec("vuek", "V1", street="Sylbacher Str. 1", zip="32791", city="Lage", lat=51.99, lng=8.79, coord_hint="nominatim")
    assert resolve(r).row["coord_source"] == "nominatim"


def test_listed_name_beats_a_constructed_one():
    s = rec("tuev_sued", "S1", name="TÜV SÜD Service-Center Marburg", street="A-Str. 1", zip="35037", city="Marburg")
    h = rec("tuev_hessen", "H1", name="TÜV Hessen Service-Center Marburg", street="A-Str. 1", zip="35037", city="Marburg")
    g = resolve(s, h)
    assert g.row["name"] == "TÜV Hessen Service-Center Marburg"
    assert any(l["field"] == "name" and "tuev_sued" in l["dropped"] for l in g.logs)
    only = resolve(s)
    assert only.row["name"] == "TÜV SÜD Service-Center Marburg"


def test_contact_winner_is_the_source_with_most_filled_fields_and_gaps_are_filled():
    s = rec("tuev_sued", "S1", street="A-Str. 1", zip="35037", city="Marburg", phone="111", email="ksc@x.de")
    h = rec("tuev_hessen", "H1", street="A-Str. 1", zip="35037", city="Marburg", phone="222", email="", website="https://h", booking_url="https://b")
    g = resolve(s, h)
    assert g.row["phone"] == "222" and g.row["website"] == "https://h" and g.row["booking_url"] == "https://b"
    assert g.row["email"] == "ksc@x.de"  # gap filled from the other source
    assert any(l["field"] == "phone" and "tuev_sued=111" in l["dropped"] for l in g.logs)


def test_contact_ties_break_by_the_fixed_source_order():
    s = rec("tuev_sued", "S1", street="A-Str. 1", zip="35037", city="Marburg", phone="111")
    h = rec("tuev_hessen", "H1", street="A-Str. 1", zip="35037", city="Marburg", phone="222")
    assert resolve(h, s).row["phone"] == "111"


def test_missing_plz_is_filled_from_the_matched_fsp_row_and_leaves_no_flag():
    t = rec("tuev_rheinland", "T1", street="Davenstedter Straße 95", zip="", city="", uo="TÜV Rheinland",
            flags=["plz_missing", "plz_unknown_in_source"])
    f = rec("fsp", "F1", street="Davenstedter Str. 95", zip="30453", city="Hannover", uo="TÜV Rheinland")
    g = resolve(t, f)
    assert g.row["zip"] == "30453" and g.row["city"] == "Hannover" and g.row["state"] == "Niedersachsen"
    assert "plz_missing" not in g.row["doubt_flags"] and "address_incomplete" not in doubts(g)


def test_missing_plz_without_a_match_is_address_incomplete():
    t = rec("tuev_rheinland", "T1", street="Davenstedter Straße 95", zip="", city="", uo="TÜV Rheinland", flags=["plz_missing"])
    g = resolve(t)
    assert [x for x in doubts(g) if x in ms.DOUBT_REASONS] == ["address_incomplete"]
    assert "tuev_rheinland:plz_missing" in g.row["doubt_flags"]


def test_a_plz_unknown_to_geonames_is_plz_invalid_and_no_street_is_address_incomplete():
    g = resolve(rec("fsp", "F1", street="B62 2", zip="36569", city="Bad Salzungen", uo="TÜV Rheinland"))
    assert "plz_invalid" in doubts(g) and g.row["state"] == ""
    g2 = resolve(rec("kues", "K1", street="", zip="80331", city="München", uo="KÜS"))
    assert "address_incomplete" in doubts(g2)


def test_old_review_reasons_are_doubt_flags_and_needs_review_is_only_for_status_changes():
    r = rec("dekra", "D1", street="A-Str. 1", zip="01067", city="Dresden", lat=51.06, lng=13.74, uo="DEKRA",
            flags=["psp_suspect", "thin", "uo_assumed:x"])
    g = resolve(r, agg=["shared_address_other_uo:kues"])
    assert g.row["needs_review"] == "False" and g.row["needs_review_reason"] == ""
    assert g.row["doubt_flags"].split("; ") == ["dekra:psp_suspect", "dekra:thin", "uo_assumed:x", "shared_address_other_uo:kues"]
    assert g.row["status"] == "active" and g.row["retired_on"] == ""
    assert set(ms.DOUBT_REASONS) == {"match_candidate_unresolved", "coords_unresolved", "address_incomplete", "plz_invalid",
                                     "hdp_match_unresolved", "hdp_own_site_kept"}
    assert set(ms.REVIEW_REASONS) == {"retired", "reactivated"}
    street_less = resolve(rec("kues", "K2", street="", zip="80331", city="München", uo="KÜS"))  # a former review reason: still not needs_review
    assert street_less.row["needs_review"] == "False" and "address_incomplete" in doubts(street_less)


def test_services_are_the_union_in_a_stable_order():
    a = rec("tuev_sued", "S1", street="A-Str. 1", zip="35037", city="Marburg", services="HU; AU")
    b = rec("tuev_hessen", "H1", street="A-Str. 1", zip="35037", city="Marburg", services="AU; Gutachten")
    assert resolve(a, b).row["services"] == "HU; AU; Gutachten"


def test_site_type_uo_and_operator_come_from_the_source_that_names_the_operator():
    s = rec("tuev_sued", "S1", street="A-Str. 1", zip="35037", city="Marburg", uo="TÜV Hessen", site_type="own_station", operator_group="tuev_hessen")
    h = rec("tuev_hessen", "H1", street="A-Str. 1", zip="35037", city="Marburg", uo="TÜV Hessen", site_type="partner", operator_group="tuev_hessen_partner",
            operator_name="Partner GmbH")
    g = resolve(s, h)
    assert g.row["site_type"] == "partner" and g.row["operator_group"] == "tuev_hessen_partner" and g.row["operator_name"] == "Partner GmbH"
    assert g.row["listed_by"] == "tuev_hessen|tuev_sued"
    assert g.row["source_ids"] == "H1|S1"
    unknown_h = rec("tuev_hessen", "H2", street="A-Str. 1", zip="35037", city="Marburg", uo="TÜV Hessen", site_type="unknown")
    assert resolve(s, unknown_h).row["site_type"] == "own_station"  # a known type beats unknown


def test_psp_is_yes_only_if_every_merged_record_says_yes():
    yes = rec("kues", "K1", street="A-Str. 1", zip="35037", city="Marburg", psp="Yes", uo="KÜS")
    no = rec("kues", "K2", street="A-Str. 1", zip="35037", city="Marburg", psp="No", uo="KÜS")
    assert resolve(yes).row["psp"] == "Yes"
    assert resolve(no).row["psp"] == "No"
    mixed = resolve(yes, no)
    assert mixed.row["psp"] == "No" and any(l["field"] == "psp" for l in mixed.logs)  # an on-site record wins, and it is logged
    other = rec("tuev_sued", "S1", street="A-Str. 1", zip="35037", city="Marburg")
    assert resolve(other).row["psp"] == "No"


def test_conflicting_uo_values_are_flagged():
    a = rec("tuev_sued", "S1", street="A-Str. 1", zip="35037", city="Marburg", uo="TÜV SÜD")
    b = rec("tuev_hessen", "H1", street="A-Str. 1", zip="35037", city="Marburg", uo="TÜV Hessen")
    g = resolve(a, b)
    assert g.row["uo"] == "TÜV Hessen" and "uo_conflict:TÜV Hessen/TÜV SÜD" in g.row["doubt_flags"]


def test_scope_and_scraped_at():
    a = rec("kues", "K1", street="A-Str. 1", zip="35037", city="Marburg", in_scope=False, scope_reason="no on-site inspection", scraped_at="2026-10-01")
    b = rec("kues", "K2", street="A-Str. 1", zip="35037", city="Marburg", in_scope=True, scraped_at="2026-10-05")
    g = resolve(a, b)
    assert g.row["in_scope"] == "True" and g.row["scraped_at"] == "2026-10-05" and g.row["scope_reason"] == ""


def test_finish_refuses_a_reason_outside_the_closed_list():
    g = resolve(rec("dekra", "D1", street="A-Str. 1", zip="01067", city="Dresden", lat=51.06, lng=13.74, uo="DEKRA"))
    with pytest.raises(AssertionError):
        rs.finish(g, ["something_else"], KNOWN)


def test_coordinates_of_merged_sources_that_disagree_by_more_than_a_kilometre_are_flagged():
    s = rec("tuev_sued", "S1", street="Am Bahnhof 1", zip="65520", city="Bad Camberg", lat=50.2960, lng=8.2650)
    h = rec("tuev_hessen", "H1", street="Am Bahnhof 1", zip="65520", city="Bad Camberg", lat=50.3200, lng=8.2650)  # 2.7 km, both pass the PLZ check
    g = resolve(s, h)
    assert g.row["coord_source"] == "tuev_sued" and "coords_differ_between_sources:tuev_hessen:2.7km" in g.row["doubt_flags"]
    near = rec("tuev_hessen", "H2", street="Am Bahnhof 1", zip="65520", city="Bad Camberg", lat=50.2962, lng=8.2652)
    assert "coords_differ" not in resolve(s, near).row["doubt_flags"]


def test_sources_that_disagree_on_the_plz_take_the_one_nearest_the_coordinate_and_keep_the_flag():
    t = rec("tuev_rheinland", "T1", street="Österreicher Straße 95", zip="01309", city="Dresden", uo="TÜV Rheinland", lat=51.04, lng=13.80)
    f = rec("fsp", "F1", street="Österreicher Straße 95", zip="01279", city="Dresden", uo="TÜV Rheinland")
    g = resolve(t, f)
    assert g.row["zip"] == "01279" and "zip_conflict:01279/01309" in g.row["doubt_flags"]  # the PLZ nearest to the coordinate wins
