"""master_continuity: a site that is still there keeps its master_id (CHANGED), only a site that is gone is RETIRED.

The cases are synthetic look-alikes of the November 2026 review (no real operator names, e-mails or phones in git):
re-keyed at one address (also with a changed PLZ), moved within the town (shared rare domain / same name), a new listing at
an address the same source already has, and the true closures that must stay unmatched.
"""

from datetime import datetime

import pandas as pd

from core import master_continuity as mc
from core import master_run
from core import master_schema as ms
from master_fixtures import source_row, write_source
from test_master_lifecycle import frame, mrow
from test_master_run import T2, go, rows, world


def pairs(prev, cur, **kw):
    return {(p["old"], p["new"]): p["verdict"] for p in mc.match(prev, cur, **kw)}


# ---- the November cases --------------------------------------------------------------------------------------------------
def test_a_new_source_id_at_the_same_address_keeps_the_id_even_when_the_plz_changed():
    prev = frame(mrow("PS-000001", name="Sachverständigenbüro Alpha", street="Papenweg 14", zip="49152", city="Bad Essen"),
                 mrow("PS-000002", name="GTÜ Kfz-Prüfstelle", street="Felix-Weg-Straße 3", zip="26127", city="Oldenburg"))
    cur = frame(mrow("PS-000101", name="Ing. & Sachverständigenbüro Alpha", street="Papenweg 14", zip="49152", city="Bad Essen", source_ids="G-NEW1"),
                mrow("PS-000102", name="Ingenieurbüro Beta GmbH", street="Felix-Weg-Str. 3", zip="26125", city="Oldenburg", source_ids="G-NEW2"))
    assert pairs(prev, cur) == {("PS-000001", "PS-000101"): "rekeyed", ("PS-000002", "PS-000102"): "rekeyed"}


def test_a_move_within_the_town_keeps_the_id_on_a_rare_domain_a_phone_or_the_same_name():
    prev = frame(mrow("PS-000001", name="Ingenieurbüro für das Kfz-Wesen", street="Im Graben 11", lat="49.06306", lng="9.29374",
                      email="ort@gamma-ing.de", phone=""),
                 mrow("PS-000002", name="Ingenieurbüro Delta", street="Diesel-Str. 2", lat="53.46386", lng="9.77488", phone=""),
                 mrow("PS-000003", name="Station Neustadt", uo="DEKRA", listed_by="dekra", street="Landstr. 47", lat="51.2023", lng="6.7053", phone=""))
    cur = frame(mrow("PS-000101", name="Ing.-büro f. d. Kraftfahrzeugwesen", street="Talweg 1", lat="49.07879", lng="9.29086",
                     email="kontakt@gamma-ing.de", website="https://www.gamma-ing.de", phone="", source_ids="G-N1"),
                mrow("PS-000102", name="Ingenieurbüro Delta", street="Lilienstr. 39", lat="53.46476", lng="9.78355", phone="", source_ids="G-N2"),
                mrow("PS-000103", name="Station Neustadt-Süd", uo="DEKRA", listed_by="dekra", street="Tuchweg 11", lat="51.1514", lng="6.7633",
                     phone="", source_ids="D-N3"))
    assert pairs(prev, cur) == {("PS-000001", "PS-000101"): "moved", ("PS-000002", "PS-000102"): "moved", ("PS-000003", "PS-000103"): "moved"}


def test_true_closures_and_true_openings_stay_unmatched():
    prev = frame(mrow("PS-000001", name="Ingenieurbüro", street="Linden-Str. 8", lat="50.8", lng="8.9", phone="", email="a@gmx.de"),
                 mrow("PS-000002", name="Prüfbüro Epsilon", street="Bahnweg 2", lat="51.0", lng="13.0", phone=""),
                 mrow("PS-000003", name="Büro Zeta", street="Ring 4", lat="52.0", lng="9.0", phone="", email="x@dekra.com"))
    cur = frame(mrow("PS-000101", name="Ingenieurbüro", street="Mühlweg 15", lat="50.81", lng="8.91", phone="", email="b@gmx.de", source_ids="N1"),
                mrow("PS-000102", name="Prüfbüro Epsilon", street="Bahnweg 2", lat="51.0", lng="13.0", phone="", uo="KÜS", source_ids="N2"),
                mrow("PS-000103", name="Büro Eta", street="Feld 9", lat="52.01", lng="9.01", phone="", email="y@dekra.com", source_ids="N3"),
                *[mrow(f"PS-00020{i}", email=f"z{i}@dekra.com", street=f"Weg {i}", source_ids=f"D{i}") for i in range(3)])
    # a generic name and a free-mail domain prove nothing; another ÜO is another row; a network domain on > 3 rows proves nothing
    assert pairs(prev, cur) == {}


def test_coordinates_alone_never_make_a_rekey_and_each_side_is_used_once():
    prev = frame(mrow("PS-000001", name="Büro Theta", street="A-Weg 1", lat="51.06", lng="13.74", phone=""),
                 mrow("PS-000002", name="Büro Iota", street="B-Weg 2", lat="51.06", lng="13.74", phone="0351 777777"))
    cur = frame(mrow("PS-000101", name="Büro Kappa", street="C-Weg 3", lat="51.06", lng="13.74", phone="", source_ids="N1"),
                mrow("PS-000102", name="Büro Lambda", street="D-Weg 4", lat="51.06", lng="13.74", phone="0351 777777", source_ids="N2"))
    assert pairs(prev, cur) == {("PS-000002", "PS-000102"): "rekeyed"}  # same coordinates + same phone; Theta / Kappa share nothing
    assert pairs(prev, cur, exclude={"PS-000002"}) == {}  # an id merged into another row is no candidate


def test_the_name_comparison_ignores_generic_words():
    assert mc.name_similarity("Ingenieurbüro", "Ingenieurbüro") == 0.0
    assert mc.name_similarity("Station Neuss", "Station Neuss-Uedesheim") == 1.0
    assert mc.name_similarity("GTÜ Prüfstelle Alpha", "Kfz-Prüfstelle Alpha GmbH") == 1.0
    assert mc.phone_key("07062") == "" and mc.phone_key("+49 7062 62794") == mc.phone_key("07062 62794")
    assert mc.domain("info@gmx.de") == "" and mc.domain("https://www.gamma-ing.de/kontakt") == "gamma-ing.de"


def test_a_new_listing_at_an_address_its_source_already_has_joins_that_row_unless_the_licensee_differs():
    prev = frame(mrow("PS-000002", source_ids="G-B"), mrow("PS-000001", source_ids="G-A"), mrow("PS-000009", source_ids="K-A", listed_by="kues"))
    rec = lambda loc, slug="gtue", veto="", street="Hauptstr. 5": {"location_id": loc, "slug": slug, "street": street,
                                                                  "akey": f"{street}@01067", "veto": veto}
    recs = [rec("G-A"), rec("G-B"), rec("G-NEW"), rec("K-A", "kues", "k1"), rec("K-NEW", "kues", "k2"), rec("G-OTHER", street="Nebenweg 1")]
    out = mc.join_pairs(recs, prev)
    assert [(a, b) for a, b, _ in out] == [("G-A", "G-NEW")]  # the lowest master_id; KÜS with another licensee stays apart
    prev2 = frame(mrow("PS-000001", source_ids="G-A|G-NEW"), mrow("PS-000002", source_ids="G-B"))
    assert [(a, b) for a, b, _ in mc.join_pairs(recs, prev2)] == [("G-A", "G-NEW")]  # joined last month: stays joined
    assert mc.join_pairs(recs, None) == []


# ---- through master_run --------------------------------------------------------------------------------------------------
def test_a_rekeyed_site_and_a_second_listing_are_changed_not_retired_and_new(tmp_path, monkeypatch):
    monkeypatch.setattr(master_run, "MAX_INPUT_AGE_DAYS", 1000)
    base = rows("G", 2)
    alpha = source_row("gtue", "G-ALPHA", name="Büro Alpha", street="Papenweg 14", zip="01067", city="Dresden", lat="51.05", lng="13.73",
                       phone="0351 4444444")
    world(tmp_path, gtue=base + [alpha])
    m1 = go(tmp_path, "official", confirm=True)
    alpha_id = m1.master.loc[m1.master["source_ids"] == "G-ALPHA", "master_id"].iloc[0]
    g0_id = m1.master.loc[m1.master["source_ids"] == "G-0", "master_id"].iloc[0]
    rekeyed = source_row("gtue", "G-ALPHA-2", name="Büro Alpha GmbH", street="Papenweg 14", zip="01067", city="Dresden", lat="51.05",
                         lng="13.73", phone="0351 4444444")
    second = source_row("gtue", "G-SECOND", name="Ingenieurbüro Mu", street=base[0]["street"],
                        zip="01067", city="Dresden", lat="51.06", lng="13.74")
    write_source(tmp_path, "gtue", base + [rekeyed, second], run="2026-11-02_1000_full")
    m2 = go(tmp_path, "official", at=T2, confirm=True)
    assert m2.status == "PASS", [(c["check"], c["detail"]) for c in m2.summary["checks"] if not c["ok"]]
    by_src = dict(zip(m2.master["source_ids"], m2.master["master_id"]))
    assert by_src["G-ALPHA-2"] == alpha_id  # the new GTÜ id keeps the master_id
    assert [c["verdict"] for c in m2.summary["lifecycle"]["continued"]] == ["rekeyed"]
    assert set(m2.master["master_id"]) == set(m1.master["master_id"])  # nothing new, nothing retired
    assert (m2.master["status"] == "active").all() and (m2.master["missed_runs"] == "0").all()
    joined = next(s for s in m2.master["source_ids"] if "G-SECOND" in s)
    assert joined == "G-0|G-SECOND" and by_src[joined] == g0_id  # the second listing joined the row at its address
    ov = pd.read_csv(m2.out_dir / "hubspot_changes_overview_2026-11.csv", dtype=str, keep_default_na=False)
    assert set(ov["change"]) <= {"changed"} and alpha_id in set(ov["prufstellenid_datascraper"])  # name changed: CHANGED
    write_source(tmp_path, "gtue", base + [rekeyed, second], run="2026-12-02_1000_full")
    m3 = go(tmp_path, "official", at=datetime(2026, 12, 3, 9, 0), confirm=True)
    assert dict(zip(m3.master["source_ids"], m3.master["master_id"]))["G-0|G-SECOND"] == g0_id  # stays joined
    assert ms.retire_after() == 1  # the two-run safety is off by default
