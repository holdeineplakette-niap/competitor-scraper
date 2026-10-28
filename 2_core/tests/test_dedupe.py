import pytest

from core import dedupe
from core import master_schema as ms
from core import normalise as nm


def rec(slug, lid, street="", zip_="", city="", lat=None, lng=None, sign="", **kw):
    r = {"slug": slug, "family": ms.family_of(slug), "rank": ms.rank(slug), "location_id": lid, "street": street, "zip": zip_,
         "city": city, "lat": lat, "lng": lng, "sign": sign, "veto": "", "booking_url": "", "phone": "", "email": "",
         "operator_name": "", "name": "", "flags": []}
    r["akey"] = nm.address_key(street, zip_) if nm.has_street(street) and zip_ else None
    r.update(kw)
    return r


def ordered(*recs):
    return sorted(recs, key=lambda r: (r["rank"], r["location_id"]))


def groups(dd):
    return sorted(sorted(dd.recs[i]["location_id"] for i in c) for c in dd.comps if len(c) > 1)


def same(a, b):
    return {"location_id_a": a, "location_id_b": b, "decision": "same", "decided_by": "t", "date": "", "note": "n"}


def different(a, b):
    return {**same(a, b), "decision": "different"}


# ---- step 1: location_sign ---------------------------------------------------------------------------------------------
def test_sign_merges_only_when_one_to_one_on_both_sides():
    s = rec("tuev_sued", "S1", "Hauptstr. 5", "35037", "Marburg", sign="008")
    h = rec("tuev_hessen", "H1", "Hauptstraße 5", "35037", "Marburg", sign="008")
    dd = dedupe.build(ordered(s, h), [])
    assert groups(dd) == [["H1", "S1"]]
    assert [e.rule for e in dd.edges] == ["location_sign"]  # one edge per pair: the stronger rule, the address agrees
    assert "also address" in dd.edges[0].note


def test_a_sign_shared_by_two_rows_is_not_used_biedenkopf_style():
    """Hessen uid 71 (Biedenkopf) carries Worms' sign 712: neither Hessen row may merge through it."""
    worms_s = rec("tuev_sued", "S-WORMS", "Rheinstr. 1", "80331", "Worms", sign="712")
    worms_h = rec("tuev_hessen", "H-463", "Rheinstr. 1", "80331", "Worms", sign="712")
    bied_h = rec("tuev_hessen", "H-71", "Marktplatz 3", "35037", "Biedenkopf", sign="712")
    bied_s = rec("tuev_sued", "S-BIED", "Marktplatz 3", "35037", "Biedenkopf", sign="500")
    dd = dedupe.build(ordered(worms_s, worms_h, bied_h, bied_s), [])
    assert groups(dd) == [["H-463", "S-WORMS"], ["H-71", "S-BIED"]]  # both found by address, none by the shared sign
    assert {e.rule for e in dd.edges} == {"address"}
    assert dd.info["sign_not_1to1"][0]["sign"] == "712"


def test_sign_pair_with_different_addresses_is_flagged():
    s = rec("tuev_sued", "S1", "Hauptstr. 5", "35037", "Marburg", sign="9")
    h = rec("tuev_hessen", "H1", "Nebenstr. 7", "35037", "Marburg", sign="9")
    dd = dedupe.build(ordered(s, h), [])
    assert groups(dd) == [["H1", "S1"]]
    assert all("sign_address_mismatch" in dd.flags[i] for i in (0, 1))


def test_signs_are_text_leading_zeros_matter():
    s = rec("tuev_sued", "S1", "A-Str. 1", "35037", "Marburg", sign="008")
    h = rec("tuev_hessen", "H1", "B-Str. 2", "35037", "Marburg", sign="8")
    assert groups(dedupe.build(ordered(s, h), [])) == []


# ---- step 2: address ---------------------------------------------------------------------------------------------------
def test_address_merge_inside_a_family_ignores_spelling_variants():
    s = rec("tuev_sued", "S1", "Robert-Bosch-Str. 16 A", "35037", "Marburg")
    h = rec("tuev_hessen", "H1", "Robert Bosch Straße 16a", "35037", "Marburg")
    dd = dedupe.build(ordered(s, h), [])
    assert groups(dd) == [["H1", "S1"]] and dd.edges[0].rule == "address"


def test_no_automatic_merge_across_families_only_a_flag():
    d = rec("dekra", "D1", "Hauptstr. 5", "01067", "Dresden")
    k = rec("kues", "K1", "Hauptstraße 5", "01067", "Dresden")
    dd = dedupe.build(ordered(d, k), [])
    assert groups(dd) == []
    assert any(f.startswith("shared_address_other_uo:kues") for f in dd.flags[0])
    assert any(f.startswith("shared_address_other_uo:dekra") for f in dd.flags[1])
    assert dd.info["shared_other_uo"] == {"dekra|kues": 1}


def test_merge_across_uo_switch_merges_the_shared_address():
    d = rec("dekra", "D1", "Hauptstr. 5", "01067", "Dresden")
    k = rec("kues", "K1", "Hauptstraße 5", "01067", "Dresden")
    dd = dedupe.build(ordered(d, k), [], merge_across_uo=True)
    assert groups(dd) == [["D1", "K1"]] and not dd.flags


def test_row_without_plz_matches_street_and_city_only_if_unique():
    t = rec("tuev_rheinland", "T1", "Woldegker Str. 42", "", "Neubrandenburg")
    f = rec("fsp", "F1", "Woldegker Straße 42", "17036", "Neubrandenburg")
    dd = dedupe.build(ordered(t, f), [])
    assert groups(dd) == [["F1", "T1"]] and dd.edges[0].rule == "address_city"
    f2 = rec("fsp", "F2", "Woldegker Straße 42", "17034", "Neubrandenburg")
    dd = dedupe.build(ordered(t, f, f2), [])
    assert groups(dd) == [] and dd.info["no_plz_ambiguous"] == ["T1"]


def test_row_with_neither_plz_nor_city_matches_on_street_if_unique_and_unmatched():
    t = rec("tuev_rheinland", "T1", "Davenstedter Straße 95", "", "", lat=52.35, lng=9.70)
    f = rec("fsp", "F1", "Davenstedter Str. 95", "30453", "Hannover")
    assert groups(dedupe.build(ordered(t, f), [])) == [["F1", "T1"]]
    # an FSP row already taken by a Rheinland row with PLZ is not offered again
    t2 = rec("tuev_rheinland", "T2", "Davenstedter Str. 95", "30453", "Hannover")
    dd = dedupe.build(ordered(t, t2, f), [])
    assert groups(dd) == [["F1", "T2"]]


def test_two_rows_without_street_never_match():
    a = rec("fsp", "F1", "", "17036", "Neubrandenburg")
    b = rec("fsp", "F2", "", "17036", "Neubrandenburg")
    assert groups(dedupe.build(ordered(a, b), [])) == []


# ---- within one source ---------------------------------------------------------------------------------------------------
def test_within_source_merge_needs_strong_evidence():
    a = rec("dekra", "D1", "Handwerkstr. 15", "70565", "Stuttgart", booking_url="https://b/1")
    b = rec("dekra", "D2", "Handwerkstr. 15", "70565", "Stuttgart", booking_url="https://b/1")
    dd = dedupe.build(ordered(a, b), [])
    assert groups(dd) == [["D1", "D2"]] and dd.edges[0].rule == "within_source_address" and dd.edges[0].note == "same booking_url"


def test_same_address_without_evidence_stays_apart_and_is_flagged():
    a = rec("dekra", "D1", "Handwerkstr. 15", "70565", "Stuttgart", name="DEKRA Automobil", operator_name="DEKRA A")
    b = rec("dekra", "D2", "Handwerkstr. 15", "70565", "Stuttgart", name="DEKRA Zentrale", operator_name="DEKRA B")
    dd = dedupe.build(ordered(a, b), [])
    assert groups(dd) == [] and dd.info["weak_pairs"] == [("D1", "D2")]
    assert "shared_address_same_source" in dd.flags[0] and "shared_address_same_source" in dd.flags[1]


def test_a_different_licensee_vetoes_the_merge_even_with_the_same_phone():
    a = rec("kues", "K1", "Krokusstr. 89", "12357", "Berlin", phone="030 1", veto="k1")
    b = rec("kues", "K2", "Krokusstr. 89", "12357", "Berlin", phone="030 1", veto="k2")
    assert groups(dedupe.build(ordered(a, b), [])) == []
    c = rec("kues", "K3", "Krokusstr. 89", "12357", "Berlin", phone="030 1", veto="k1")
    assert groups(dedupe.build(ordered(a, c), [])) == [["K1", "K3"]]


def test_same_phone_digits_count_as_evidence():
    a = rec("gtue", "G1", "Hauptstr. 5", "01067", "Dresden", phone="0351 / 123")
    b = rec("gtue", "G2", "Hauptstr. 5", "01067", "Dresden", phone="0351123")
    assert groups(dedupe.build(ordered(a, b), [])) == [["G1", "G2"]]


def test_two_unproven_rows_of_one_source_are_not_pulled_together_through_another_source():
    """Review M2: S1 and S2 share an address with H1. H1 could belong to either, so nothing merges and a human decides."""
    s1 = rec("tuev_sued", "S1", "Hauptstr. 5", "35037", "Marburg")
    s2 = rec("tuev_sued", "S2", "Hauptstr. 5", "35037", "Marburg")
    h = rec("tuev_hessen", "H1", "Hauptstr. 5", "35037", "Marburg")
    dd = dedupe.build(ordered(s1, s2, h), [])
    assert groups(dd) == []
    assert set(dd.info["forced_review"]) == {"S1", "S2", "H1"}
    assert all(any(f.startswith("match_ambiguous") for f in dd.flags[i]) for i in range(3))


def test_two_proven_rows_of_one_source_plus_another_source_merge_into_one():
    s1 = rec("tuev_sued", "S1", "Hauptstr. 5", "35037", "Marburg", phone="1")
    s2 = rec("tuev_sued", "S2", "Hauptstr. 5", "35037", "Marburg", phone="1")
    h = rec("tuev_hessen", "H1", "Hauptstr. 5", "35037", "Marburg")
    dd = dedupe.build(ordered(s1, s2, h), [])
    assert groups(dd) == [["H1", "S1", "S2"]] and not dd.info["forced_review"]


def test_a_sign_shared_by_two_different_places_does_not_merge_and_goes_to_review():
    """Review M1: the same booking sign at 600 km distance is a copy-paste error, not one site."""
    s = rec("tuev_sued", "S1", "Hauptstr. 5", "11111", "Alpha", sign="1", lat=48.0, lng=11.0)
    h = rec("tuev_hessen", "H1", "Nebenstr. 7", "22222", "Beta", sign="1", lat=53.5, lng=10.0)
    dd = dedupe.build(ordered(s, h), [])
    assert groups(dd) == [] and dd.info["sign_rejected"] == [("S1", "H1", "1")]
    assert set(dd.info["forced_review"]) == {"S1", "H1"}
    near = rec("tuev_hessen", "H2", "Nebenstr. 7", "11111", "Beta", sign="1")  # same PLZ: a typo-level difference, merged and flagged
    assert groups(dedupe.build(ordered(s, near), [])) == [["H2", "S1"]]


def test_a_numberless_address_does_not_merge_without_agreeing_coordinates():
    t = rec("tuev_rheinland", "T1", "Hauptstr.", "12345", "Alpha")
    f = rec("fsp", "F1", "Hauptstraße", "12345", "Alpha")
    dd = dedupe.build(ordered(t, f), [])
    assert groups(dd) == [] and set(dd.info["forced_review"]) == {"T1", "F1"}
    t2 = rec("tuev_rheinland", "T1", "Hauptstr.", "12345", "Alpha", lat=50.0, lng=8.0)
    f2 = rec("fsp", "F1", "Hauptstraße", "12345", "Alpha", lat=50.0001, lng=8.0001)
    assert groups(dedupe.build(ordered(t2, f2), [])) == [["F1", "T1"]]


def test_street_only_match_checks_the_rows_coordinates_against_the_candidates_plz():
    t = rec("tuev_rheinland", "T1", "Bahnhofstr. 1", "", "", lat=48.1, lng=11.5)   # near München
    f = rec("fsp", "F1", "Bahnhofstr. 1", "20095", "Hamburg")                    # another city
    cent = {"20095": (53.55, 10.0), "80331": (48.137, 11.575)}
    assert groups(dedupe.build(ordered(t, f), [], cent=cent)) == []
    f_ok = rec("fsp", "F2", "Bahnhofstr. 1", "80331", "München")
    assert groups(dedupe.build(ordered(t, f_ok), [], cent=cent)) == [["F2", "T1"]]


# ---- overrides -------------------------------------------------------------------------------------------------------------
def test_override_same_merges_what_no_rule_finds():
    t = rec("tuev_rheinland", "T1", "Am Grenzberg 7", "17367", "Eggesin")
    f = rec("fsp", "F1", "Am Grenzberg 7c", "17367", "Eggesin")
    dd = dedupe.build(ordered(t, f), [same("T1", "F1")])
    assert groups(dd) == [["F1", "T1"]]
    assert dd.info["overrides_applied"] == 1 and dd.edges[0].rule == "override"


def test_override_different_removes_an_automatic_edge():
    a = rec("tuev_sued", "S1", "Hauptstr. 5", "35037", "Marburg")
    b = rec("tuev_hessen", "H1", "Hauptstr. 5", "35037", "Marburg")
    dd = dedupe.build(ordered(a, b), [different("S1", "H1")])
    assert groups(dd) == [] and dd.info["suppressed"]


def test_transitivity_conflict_fails_with_a_message():
    """A = B and B = C by address, but A != C by decision: the run must fail, not pick a side."""
    a = rec("tuev_sued", "S1", "Hauptstr. 5", "35037", "Marburg")
    b = rec("tuev_hessen", "H1", "Hauptstr. 5", "35037", "Marburg", booking_url="x")
    c = rec("tuev_hessen", "H2", "Hauptstr. 5", "35037", "Marburg", booking_url="x")  # H1 = H2 (proven), S1 = H1 and S1 = H2 by address
    with pytest.raises(dedupe.MergeConflict, match="H2 and S1.*different"):
        dedupe.build(ordered(a, b, c), [different("S1", "H2")])


def test_override_with_unknown_id_is_reported_not_silently_dropped():
    a = rec("tuev_sued", "S1", "Hauptstr. 5", "35037", "Marburg")
    dd = dedupe.build([a], [same("S1", "GONE-1")])
    assert dd.info["overrides_unused"] and "GONE-1" in dd.info["overrides_unused"][0]


def test_override_file_is_validated(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("location_id_a;location_id_b;decision;decided_by;date;note\nA;B;maybe;n;;x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="decision"):
        dedupe.load_overrides(p)
    p.write_text("location_id_a;location_id_b;decision;decided_by;date;note\nA;B;same;n;;one;two\n", encoding="utf-8")
    with pytest.raises(ValueError, match="more fields"):
        dedupe.load_overrides(p)
    p.write_text("location_id_a;location_id_b;decision;decided_by;date;note\nA;B;same;n;2026-10-06;ok\n", encoding="utf-8")
    assert dedupe.load_overrides(p)[0]["decision"] == "same"
    assert dedupe.load_overrides(tmp_path / "missing.csv") == []


# ---- step 4: similar street --------------------------------------------------------------------------------------------------
def test_a_one_letter_typo_merges_when_mutual_and_unique():
    s = rec("tuev_sued", "S1", "Willhelm-Dichmann-Str. 3", "35037", "Marburg", lat=50.81, lng=8.77)
    h = rec("tuev_hessen", "H1", "Wilhelm-Dichmann-Straße 3", "35037", "Marburg", lat=50.8101, lng=8.7701)
    dd = dedupe.build(ordered(s, h), [])
    assert groups(dd) == [["H1", "S1"]] and dd.edges[0].rule == "address_similar"


def test_similar_street_is_not_merged_if_two_candidates_compete():
    s = rec("tuev_sued", "S1", "Willhelm-Dichmann-Str. 3", "35037", "Marburg")
    h1 = rec("tuev_hessen", "H1", "Wilhelm-Dichmann-Str. 3", "35037", "Marburg")
    h2 = rec("tuev_hessen", "H2", "Wilhelm-Dichmann-Straße 3", "35037", "Marburg", booking_url="x")
    dd = dedupe.build(ordered(s, h1, h2), [])
    assert not any(e.rule == "address_similar" for e in dd.edges)


def test_similar_street_far_apart_by_coordinates_is_vetoed():
    s = rec("tuev_sued", "S1", "Willhelm-Dichmann-Str. 3", "35037", "Marburg", lat=50.81, lng=8.77)
    h = rec("tuev_hessen", "H1", "Wilhelm-Dichmann-Str. 3", "35037", "Marburg", lat=50.90, lng=8.90)
    assert groups(dedupe.build(ordered(s, h), [])) == []


def test_similar_street_never_crosses_a_plz():
    s = rec("tuev_sued", "S1", "Willhelm-Dichmann-Str. 3", "35037", "Marburg")
    h = rec("tuev_hessen", "H1", "Wilhelm-Dichmann-Str. 3", "35039", "Marburg")
    assert groups(dedupe.build(ordered(s, h), [])) == []


def test_house_number_letters_5a_and_5b_are_different_sites():
    s = rec("tuev_sued", "S1", "Hauptstr. 5a", "35037", "Marburg")
    h = rec("tuev_hessen", "H1", "Hauptstr. 5b", "35037", "Marburg")
    assert groups(dedupe.build(ordered(s, h), [])) == []


# ---- coordinates only propose --------------------------------------------------------------------------------------------------
def g(gid, fam, slugs, lat, lng, street, zip_, city):
    return {"gid": gid, "family": fam, "slugs": set(slugs), "lat": lat, "lng": lng, "street": street, "zip": zip_, "city": city,
            "akeys": {nm.address_key(street, zip_)}, "uo": "x"}


def test_oberursel_zeilweg_and_a_bad_homburg_site_24_m_apart_are_not_a_candidate():
    a = g(0, "sued_hessen", ["tuev_hessen"], 50.20000, 8.58000, "Zeilweg 6", "61440", "Oberursel")
    b = g(1, "sued_hessen", ["tuev_sued"], 50.20020, 8.58010, "Kirchgasse 12", "61348", "Bad Homburg")
    unresolved, rejected, auto = dedupe.proximity_candidates([a, b])
    assert unresolved == [] and len(rejected) == 1


def test_same_street_with_a_related_number_is_an_open_candidate():
    a = g(0, "sued_hessen", ["tuev_hessen"], 50.2000, 8.5800, "Dornhofstr. 26", "63263", "Neu-Isenburg")
    b = g(1, "sued_hessen", ["tuev_sued"], 50.2005, 8.5805, "Dornhofstr. 26-28", "63263", "Neu-Isenburg")
    unresolved, rejected, auto = dedupe.proximity_candidates([a, b])
    assert len(unresolved) == 1 and rejected == []


def test_same_street_and_number_in_one_city_with_another_plz_is_the_same_site_for_pair_families():
    """FSP says 01279 Dresden, TÜV Rheinland 01309 Dresden: same street, same number, 105 m apart."""
    a = g(0, "rheinland_fsp", ["tuev_rheinland"], 51.0400, 13.8000, "Österreicher Straße 95", "01309", "Dresden")
    b = g(1, "rheinland_fsp", ["fsp"], 51.0401, 13.8001, "Österreicher Str. 95", "01279", "Dresden")
    unresolved, rejected, auto = dedupe.proximity_candidates([a, b])
    assert len(auto) == 1 and unresolved == [] and rejected == []
    # not for a source that lists each site once, and not if the city differs
    c = g(2, "dekra", ["dekra"], 51.0400, 13.8000, "Österreicher Straße 95", "01309", "Dresden")
    d = g(3, "dekra", ["dekra", "kues"], 51.0401, 13.8001, "Österreicher Str. 95", "01279", "Dresden")
    assert dedupe.proximity_candidates([c, d])[2] == []
    e = g(4, "rheinland_fsp", ["fsp"], 51.0401, 13.8001, "Österreicher Str. 95", "01279", "Pirna")
    assert dedupe.proximity_candidates([a, e])[2] == []


def test_extra_same_pairs_from_the_coordinate_stage_become_address_proximity_edges():
    t = rec("tuev_rheinland", "T1", "Österreicher Straße 95", "01309", "Dresden")
    f = rec("fsp", "F1", "Österreicher Straße 95", "01279", "Dresden")
    dd = dedupe.build(ordered(t, f), [], extra_same=[("T1", "F1", "105 m apart")])
    assert groups(dd) == [["F1", "T1"]] and dd.edges[0].rule == "address_proximity"


def test_same_street_different_numbers_is_a_different_address():
    a = g(0, "dekra", ["dekra"], 48.7, 9.1, "Handwerkstr. 8", "70565", "Stuttgart")
    b = g(1, "dekra", ["dekra"], 48.7002, 9.1002, "Handwerkstr. 15", "70565", "Stuttgart")
    unresolved, rejected, auto = dedupe.proximity_candidates([a, b])
    assert unresolved == [] and len(rejected) == 1


def test_candidates_need_distance_under_150_m_and_a_family_that_has_duplicates():
    a = g(0, "dekra", ["dekra"], 48.7, 9.1, "Wallersheimer Weg 63-67", "56070", "Koblenz")
    far = g(1, "dekra", ["dekra"], 48.7, 9.2, "Wallersheimer Weg", "56070", "Koblenz")
    assert dedupe.proximity_candidates([a, far]) == ([], [], [])
    # same source, a source without known near-duplicates (VÜK): not examined
    v1 = g(2, "vuek", ["vuek"], 48.7, 9.1, "Hauptstr. 5", "56070", "Koblenz")
    v2 = g(3, "vuek", ["vuek"], 48.7001, 9.1001, "Hauptstr. 5a", "56070", "Koblenz")
    assert dedupe.proximity_candidates([v1, v2]) == ([], [], [])
    # different families are never compared
    k = g(4, "kues", ["kues"], 48.7001, 9.1001, "Wallersheimer Weg", "56070", "Koblenz")
    assert dedupe.proximity_candidates([a, k]) == ([], [], [])


# ---- review cycle 2 ----------------------------------------------------------------------------------------------------------------
def test_unproven_same_source_rows_cannot_be_joined_through_the_sign_path():
    s1 = rec("tuev_sued", "S1", "Alte Str. 1", "35037", "Marburg", name="a")
    s2 = rec("tuev_sued", "S2", "Neue Str. 2", "35037", "Marburg", name="b", sign="9")
    h = rec("tuev_hessen", "H1", "Alte Str. 1", "35037", "Marburg", sign="9")
    dd = dedupe.build(ordered(s1, s2, h), [])
    assert not any(len(c) == 3 for c in dd.comps)
    assert dd.info["forced_review"]


def test_unproven_same_source_rows_cannot_be_joined_through_the_address_city_path():
    t1 = rec("tuev_rheinland", "T1", "Hauptstr. 5", "", "Alpha", name="a")
    t2 = rec("tuev_rheinland", "T2", "Hauptstr. 5", "", "Alpha", name="b")
    f = rec("fsp", "F1", "Hauptstr. 5", "12345", "Alpha")
    dd = dedupe.build(ordered(t1, t2, f), [])
    assert not any(len(c) == 3 for c in dd.comps)


def test_unproven_same_source_rows_cannot_be_joined_through_a_proximity_edge():
    t1 = rec("tuev_rheinland", "T1", "Hauptstr. 5", "12345", "Alpha", name="a")
    t2 = rec("tuev_rheinland", "T2", "Hauptstr. 5", "12399", "Alpha", name="b")
    f = rec("fsp", "F1", "Hauptstr. 5", "12345", "Alpha")
    dd = dedupe.build(ordered(t1, t2, f), [], extra_same=[("T2", "F1", "14 m apart")])
    assert not any(len(c) == 3 for c in dd.comps)


def test_missing_data_is_never_agreement_for_the_sign_guard_or_the_street_only_rule():
    s = rec("tuev_sued", "S1", "Hauptstr. 5", "80331", "München", sign="3", lat=48.1, lng=11.5)
    h = rec("tuev_hessen", "H1", "", "", "", sign="3")  # no address, no coordinates: nothing shows the two are one site
    dd = dedupe.build(ordered(s, h), [])
    assert groups(dd) == [] and set(dd.info["forced_review"]) == {"S1", "H1"}
    t = rec("tuev_rheinland", "T1", "Bahnhofstr. 1", "", "")  # no PLZ, no city, no coordinates
    f = rec("fsp", "F1", "Bahnhofstr. 1", "20095", "Hamburg")
    dd = dedupe.build(ordered(t, f), [])
    assert groups(dd) == [] and "T1" in dd.info["forced_review"]


def test_equal_generic_names_are_no_evidence_for_gtue_and_kues():
    a = rec("gtue", "G1", "Hauptstr. 5", "01067", "Dresden", name="GTÜ-Prüfstelle")
    b = rec("gtue", "G2", "Hauptstr. 5", "01067", "Dresden", name="GTÜ-Prüfstelle")
    assert groups(dedupe.build(ordered(a, b), [])) == []
    c = rec("dekra", "D1", "Hauptstr. 5", "01067", "Dresden", name="DEKRA Automobil GmbH")
    d = rec("dekra", "D2", "Hauptstr. 5", "01067", "Dresden", name="DEKRA Automobil GmbH")
    assert groups(dedupe.build(ordered(c, d), [])) == [["D1", "D2"]]


# ---- review cycle 3 ----------------------------------------------------------------------------------------------------------------
def test_a_differing_operator_vetoes_a_name_only_match():
    a = rec("dekra", "D1", "Hauptstr. 5", "01067", "Dresden", name="DEKRA Automobil GmbH", operator_name="Betreiber A")
    b = rec("dekra", "D2", "Hauptstr. 5", "01067", "Dresden", name="DEKRA Automobil GmbH", operator_name="Betreiber B")
    assert groups(dedupe.build(ordered(a, b), [])) == []
    c = rec("dekra", "D3", "Hauptstr. 5", "01067", "Dresden", name="DEKRA Automobil GmbH", operator_name="Betreiber A")
    assert groups(dedupe.build(ordered(a, c), [])) == [["D1", "D3"]]


def test_an_implausible_override_is_refused_as_a_typo():
    a = rec("gtue", "G1", "Huttenstr. 50", "10553", "Berlin", lat=52.53, lng=13.33)
    b = rec("dekra", "D1", "Bahnhofstr. 1", "99974", "Mühlhausen", lat=51.2, lng=10.45)
    with pytest.raises(dedupe.MergeConflict, match="looks like a typo"):
        dedupe.build(ordered(a, b), [same("G1", "D1")])
    near = rec("dekra", "D2", "Hauptstr. 1", "10553", "Berlin", lat=52.54, lng=13.34)
    assert groups(dedupe.build(ordered(a, near), [same("G1", "D2")])) == [["D2", "G1"]]
