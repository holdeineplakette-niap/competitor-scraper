"""Lifecycle of master rows (retired / reactivated, needs_review only for status changes) and the HubSpot import files."""

from datetime import datetime

import pandas as pd
import pytest

from core import hubspot_export as hx
from core import master_lifecycle as ml
from core import master_run, paths
from core import master_schema as ms
from master_fixtures import source_row, write_source
from test_master_run import T1, T2, go, rows, world

FRESH = {"gtue": True, "kues": True, "dekra": True}


def mrow(mid, **kw) -> dict:
    base = {c: "" for c in ms.MASTER_COLUMNS}
    base.update(master_id=mid, uo="GTÜ", listed_by="gtue", source_ids=f"GTUE-{mid}", name=f"Name {mid}", street=f"Weg {mid[-1]}", zip="01067",
            city="Dresden", state="Sachsen", country="DE", status="active", needs_review="False", first_seen="2026-10-06", last_seen="2026-10-06",
            site_type="unknown", psp="No", phone="0351 1", scraped_at="2026-10-06T10:00:00")
    base.update(kw)
    return base


def frame(*rs) -> pd.DataFrame:
    return pd.DataFrame(list(rs), columns=ms.MASTER_COLUMNS)


def by_id(df) -> dict:
    return {r["master_id"]: r for r in df.to_dict("records")}


# ---- master_lifecycle.apply ------------------------------------------------------------------------------------------------------
def test_a_row_that_no_group_claims_is_retired_at_once_with_its_last_values():
    prev = frame(mrow("PS-000001"), mrow("PS-000002"))
    cur = frame(mrow("PS-000001"))
    life = ml.apply(cur, prev, "2026-11-03", FRESH)
    r = by_id(life.master)
    assert set(r) == {"PS-000001", "PS-000002"}  # nothing is dropped
    gone = r["PS-000002"]
    assert (gone["status"], gone["retired_on"], gone["needs_review"], gone["needs_review_reason"]) == ("retired", "2026-11-03", "True", "retired")
    assert gone["name"] == "Name PS-000002" and gone["phone"] == "0351 1" and gone["last_seen"] == "2026-10-06"  # last known values
    assert r["PS-000001"]["status"] == "active" and r["PS-000001"]["needs_review"] == "False"
    assert life.info["retired"] == ["PS-000002"] and life.info["retired_by_source"] == {"gtue": 1}


def test_a_stale_source_never_retires_its_rows_they_stay_active_and_flagged():
    prev = frame(mrow("PS-000001"), mrow("PS-000002", listed_by="gtue|kues"))
    life = ml.apply(frame(), prev, "2026-11-03", {"gtue": True, "kues": False})
    r = by_id(life.master)
    assert r["PS-000001"]["status"] == "retired"  # its only source is fresh
    kept = r["PS-000002"]  # one of its two sources is stale: not a closure
    assert kept["status"] == "active" and kept["needs_review"] == "False" and "not_refreshed:kues" in kept["doubt_flags"]
    assert life.info["not_refreshed"] == [{"master_id": "PS-000002", "sources": ["kues"]}]
    assert ml.apply(frame(), prev, "2026-11-03", {}).info["retired"] == []  # a source without any input note counts as stale


def test_an_earlier_retirement_is_carried_unchanged_and_its_flag_is_gone():
    prev = frame(mrow("PS-000001", status="retired", retired_on="2026-10-06", needs_review="True", needs_review_reason="retired"))
    life = ml.apply(frame(), prev, "2026-11-03", FRESH)
    row = by_id(life.master)["PS-000001"]
    assert (row["status"], row["retired_on"], row["needs_review"], row["needs_review_reason"]) == ("retired", "2026-10-06", "False", "")
    assert life.info["retired"] == [] and life.info["carried_retired"] == 1


def test_a_retired_id_that_comes_back_is_active_again_and_flagged_reactivated_for_one_run():
    prev = frame(mrow("PS-000001", status="retired", retired_on="2026-10-06"))
    cur = frame(mrow("PS-000001"))
    row = by_id(ml.apply(cur, prev, "2026-11-03", FRESH).master)["PS-000001"]
    assert (row["status"], row["retired_on"], row["needs_review"], row["needs_review_reason"]) == ("active", "", "True", "reactivated")
    nxt = by_id(ml.apply(cur, ml.apply(cur, prev, "2026-11-03", FRESH).master, "2026-12-03", FRESH).master)["PS-000001"]
    assert nxt["needs_review"] == "False" and nxt["needs_review_reason"] == ""  # cleared in the next month


def test_a_retired_row_and_a_new_row_at_one_address_are_flagged_and_not_merged():
    prev = frame(mrow("PS-000001", street="Hauptstr. 5"), mrow("PS-000002", street="Nebenstr. 9"))
    cur = frame(mrow("PS-000002", street="Nebenstr. 9"), mrow("PS-000003", street="Hauptstraße 5", source_ids="GTUE-NEW"))
    life = ml.apply(cur, prev, "2026-11-03", FRESH)
    r = by_id(life.master)
    assert len(r) == 3 and r["PS-000001"]["status"] == "retired" and r["PS-000003"]["status"] == "active"
    assert "possibly_rekeyed:PS-000003" in r["PS-000001"]["doubt_flags"] and "possibly_rekeyed:PS-000001" in r["PS-000003"]["doubt_flags"]
    assert r["PS-000003"]["needs_review"] == "False"  # a doubt flag, not a review flag
    assert [(x["retired"], x["new"]) for x in life.info["rekey"]] == [(["PS-000001"], ["PS-000003"])]
    assert "possibly_rekeyed" not in r["PS-000002"]["doubt_flags"]


def test_the_first_master_has_no_lifecycle_changes_and_an_old_master_counts_as_active():
    cur = frame(mrow("PS-000001"))
    assert ml.apply(cur, None, "2026-10-06", FRESH).master["status"].tolist() == ["active"]
    old = frame(mrow("PS-000001")).drop(columns=["status", "retired_on"])  # a master from before 2026-10-08
    life = ml.apply(cur, old, "2026-11-03", FRESH)
    assert life.master["status"].tolist() == ["active"] and life.info["retired"] == []


# ---- through master_run ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def old_inputs_are_fresh(monkeypatch):
    monkeypatch.setattr(master_run, "MAX_INPUT_AGE_DAYS", 1000)  # the synthetic sources stay from October


def test_retire_and_reactivate_across_three_months_keep_the_id_and_flag_one_run_only(tmp_path, old_inputs_are_fresh):
    world(tmp_path, gtue=rows("G", 3))
    m1 = go(tmp_path, "official", confirm=True)
    assert m1.status == "PASS" and (m1.master["needs_review"] == "False").all() and set(m1.master["status"]) == {"active"}
    vanished = next(i for s, i in zip(m1.master["source_ids"], m1.master["master_id"]) if s == "G-2")
    write_source(tmp_path, "gtue", rows("G", 2), run="2026-11-02_1000_full")
    m2 = go(tmp_path, "official", at=T2, confirm=True, accept_retirements=True)
    row = m2.master.set_index("master_id").loc[vanished]
    assert m2.status == "PASS" and (row["status"], row["retired_on"], row["needs_review_reason"]) == ("retired", "2026-11-03", "retired")
    assert (m2.master["needs_review"] == "True").sum() == 1 and m2.summary["lifecycle"]["retired"] == [vanished]
    assert (m2.diff["change"] == "retired").sum() == 1 and "disappeared" not in set(m2.diff["change"])
    write_source(tmp_path, "gtue", rows("G", 3), run="2026-12-02_1000_full")  # it is listed again
    m3 = go(tmp_path, "official", at=datetime(2026, 12, 3, 9, 0), confirm=True)
    back = m3.master.set_index("master_id").loc[vanished]
    assert (back["status"], back["retired_on"], back["needs_review_reason"]) == ("active", "", "reactivated")
    assert back["first_seen"] == "2026-10-06" and set(m3.master["master_id"]) == set(m1.master["master_id"])
    assert (m3.diff["change"] == "reactivated").sum() == 1
    write_source(tmp_path, "gtue", rows("G", 3), run="2027-01-02_1000_full")
    m4 = go(tmp_path, "official", at=datetime(2027, 1, 3, 9, 0), confirm=True)
    assert (m4.master["needs_review"] == "False").all() and set(m4.master["status"]) == {"active"}


def test_a_stale_input_keeps_the_vanished_rows_active_through_the_run(tmp_path, monkeypatch, old_inputs_are_fresh):
    world(tmp_path, gtue=rows("G", 3))
    go(tmp_path, "official", confirm=True)
    write_source(tmp_path, "gtue", rows("G", 2), run="2026-11-02_1000_full")
    monkeypatch.setattr(master_run.master_lifecycle, "fresh_sources", lambda notes, age: {s: s != "gtue" for s in notes})
    m2 = go(tmp_path, "official", at=T2, confirm=True, accept_retirements=True)
    kept = m2.master[m2.master["source_ids"] == "G-2"].iloc[0]
    assert kept["status"] == "active" and kept["needs_review"] == "False" and "not_refreshed:gtue" in kept["doubt_flags"]
    assert (m2.master["status"] == "retired").sum() == 0 and len(m2.summary["lifecycle"]["not_refreshed"]) == 1
    assert any("stale or was replaced" in c["check"] and c["severity"] == "review" for c in m2.summary["checks"])


def test_mass_retirement_fails_the_run_unless_it_is_accepted(tmp_path, old_inputs_are_fresh):
    world(tmp_path, gtue=rows("G", 10))
    go(tmp_path, "official", confirm=True)
    write_source(tmp_path, "gtue", rows("G", 5), run="2026-11-02_1000_full")
    blocked = go(tmp_path, "dev", at=T2)
    assert blocked.status == "FAIL" and any("mass retirement" in c["check"] and not c["ok"] and c["severity"] == "hard" for c in blocked.summary["checks"])
    accepted = go(tmp_path, "dev", at=T2, accept_retirements=True)
    assert accepted.status == "PASS" and len(accepted.summary["lifecycle"]["retired"]) == 5
    assert any("mass retirement" in c["check"] and not c["ok"] and c["severity"] == "review" for c in accepted.summary["checks"])


def test_the_former_review_reasons_never_set_needs_review_in_a_run(tmp_path):
    street_less = source_row("kues", "KUES-9", display_name="K", street="", zip="99999", city="Nirgendwo")
    world(tmp_path, kues=[street_less])
    res = go(tmp_path)
    assert res.status == "PASS" and (res.master["needs_review"] == "False").all() and (res.master["needs_review_reason"] == "").all()
    assert res.summary["needs_review_rows"] == 0 and res.summary["doubt_flags_by_reason"]["address_incomplete"]["rows"] >= 1


# ---- HubSpot files ---------------------------------------------------------------------------------------------------------------
def test_the_first_import_has_only_active_rows_with_hubspot_headers_and_constants():
    master = frame(mrow("PS-000001", street="", doubt_flags="address_incomplete", website="https://x.de", lat="51.123456", lng="13.7",
                        email="a@b.de", uo="TÜV SÜD", psp="Yes"),
                   mrow("PS-000002", status="retired", retired_on="2026-10-20"))
    imp, excluded = hx.first_import(master)
    assert list(imp.columns) == hx.HEADERS and imp[hx.ID].tolist() == ["PS-000001"] and excluded.empty
    assert hx.ID == "prufstellenid_datascraper" and hx.DATASOURCE_PROPERTY == "company_datasource"
    r = imp.iloc[0]
    assert r["company_datasource"] == "competitor_scraper" and r["country"] == "Germany" and r["zip"] == "01067"
    assert r["compscraper_adresse_unvollstandig"] == "true" and r["stutzpunkt"] == "Ja" and r["compscraper_business_status"] == "aktiv"
    assert r["compscraper_closedondate"] == "" and "compscraper_closedondate" in imp.columns  # active: no date
    assert r["stutzpunkt_pruforganisation"] == "TÜV"  # TÜV Süd's option has the internal name "TÜV"
    assert (r["geo_lat"], r["geo_lng"]) == ("51,123456", "13,7")  # German decimal comma
    assert r["website"] == "https://x.de" and r["unternehmens_e_mail"] == "a@b.de" and r["phone"] == "0351 1" and r["address"] == ""
    assert "domain" not in imp.columns
    for internal in ("listed_by", "source_ids", "site_type_evidence", "scope_reason", "doubt_flags", "is_hdp_site", "needs_review", "retired_on"):
        assert internal not in imp.columns


def test_every_uo_has_a_hubspot_option_and_an_unknown_value_stops_the_export():
    assert set(hx.UO_OPTIONS) == set(ms.UO_VALUES)
    assert hx.UO_OPTIONS["TÜV NORD"] == "TÜV Nord" and hx.UO_OPTIONS["VÜK"] == "VÜK" and hx.UO_OPTIONS["TÜV Saarland"] == "TÜV Saarland"
    with pytest.raises(ValueError, match="without a HubSpot value"):
        hx.first_import(frame(mrow("PS-000001", uo="Neue ÜO")))
    assert hx.first_import(frame(mrow("PS-000001", psp="No")))[0].iloc[0]["stutzpunkt"] == ""  # never Nein: HubSpot keeps its value


def test_a_possible_own_site_is_kept_out_of_the_import_and_listed():
    master = frame(mrow("PS-000001"), mrow("PS-000002", doubt_flags="hdp_match_unresolved:Dortmund; hdp_match_unresolved"))
    imp, excluded = hx.first_import(master)
    assert imp[hx.ID].tolist() == ["PS-000001"] and excluded["master_id"].tolist() == ["PS-000002"]
    old_style = frame(mrow("PS-000003", needs_review_reason="hdp_match_unresolved"))  # a master from before 2026-10-08
    assert hx.first_import(old_style)[0].empty


def test_the_monthly_file_has_new_changed_retired_and_reactivated_rows_only():
    prev = frame(mrow("PS-000001"), mrow("PS-000002"), mrow("PS-000003"), mrow("PS-000004", status="retired", retired_on="2026-10-20"), mrow("PS-000005"))
    cur = frame(mrow("PS-000001"),  # unchanged: not in the file
                mrow("PS-000002", phone="0351 999", city="Dresden"),  # changed: only the phone goes
                mrow("PS-000003", status="retired", retired_on="2026-11-03", needs_review="True", needs_review_reason="retired"),
                mrow("PS-000004", needs_review="True", needs_review_reason="reactivated"),
                mrow("PS-000005"),
                mrow("PS-000006", name="Neu"))
    imp, ov, _ = hx.monthly_changes(prev, cur)
    assert dict(zip(ov[hx.ID], ov["change"])) == {"PS-000006": "new", "PS-000004": "reactivated", "PS-000002": "changed", "PS-000003": "retired"}
    rows_ = {r[hx.ID]: r for r in imp.to_dict("records")}
    changed = rows_["PS-000002"]
    assert changed["phone"] == "0351 999" and changed["name"] == "" and changed["city"] == "" and changed["company_datasource"] == "competitor_scraper"  # sparse
    retired = rows_["PS-000003"]
    assert retired["compscraper_business_status"] == "scraper-geschlossen" and retired["phone"] == "" and retired["name"] == "" and retired["geo_lat"] == ""
    assert retired["compscraper_closedondate"] == "2026-11-03"  # the date goes with the status
    assert rows_["PS-000004"]["compscraper_closedondate"] == "" and changed["compscraper_closedondate"] == ""  # nothing else carries a date
    assert rows_["PS-000006"]["name"] == "Neu" and rows_["PS-000006"]["country"] == "Germany"  # a new row is complete
    assert rows_["PS-000004"]["compscraper_business_status"] == "aktiv" and rows_["PS-000004"]["name"] != ""
    assert "PS-000001" not in rows_ and "PS-000005" not in rows_


def test_a_run_writes_the_hubspot_files_next_to_the_master(tmp_path, old_inputs_are_fresh):
    world(tmp_path, gtue=rows("G", 3))
    m1 = go(tmp_path, "official", confirm=True)
    first = m1.out_dir / "hubspot_import_2026-10.csv"
    text = first.read_text(encoding="utf-8")
    assert not text.startswith("﻿") and text.splitlines()[0] == ",".join(hx.HEADERS)
    df = pd.read_csv(first, sep=",", dtype=str, keep_default_na=False)
    assert len(df) == len(m1.master) and set(df["company_datasource"]) == {"competitor_scraper"} and set(df["country"]) == {"Germany"}
    assert set(df["compscraper_business_status"]) == {"aktiv"} and set(df["stutzpunkt"]) <= {"Ja", ""}
    assert not (m1.out_dir / "hubspot_changes_2026-10.csv").exists()
    write_source(tmp_path, "gtue", rows("G", 2), run="2026-11-02_1000_full")
    m2 = go(tmp_path, "official", at=T2, confirm=True, accept_retirements=True)
    ch = pd.read_csv(m2.out_dir / "hubspot_changes_2026-11.csv", sep=",", dtype=str, keep_default_na=False)
    ov = pd.read_csv(m2.out_dir / "hubspot_changes_overview_2026-11.csv", sep=",", dtype=str, keep_default_na=False)
    assert ch["compscraper_business_status"].tolist() == ["scraper-geschlossen"] and ov["change"].tolist() == ["retired"]
    assert ch["compscraper_closedondate"].tolist() == ["2026-11-03"]
    assert not (m2.out_dir / "hubspot_import_2026-11.csv").exists()
