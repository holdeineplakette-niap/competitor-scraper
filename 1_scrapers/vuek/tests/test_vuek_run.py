"""Offline tests for vuek.run against the simulated vuek-gmbh.de, including runs that must NOT pass."""

from __future__ import annotations

import csv
import json
from datetime import datetime

import pandas as pd
import pytest

from vuek import run as vuek_run
from vuek.fetch import BlockedError, FetchError
from vuek.simulate import EXTRA, SIM_COORDS, STATIONS, SimulatedGeocoder, SimulatedVuek

T0 = datetime(2026, 10, 6, 12, 0)
T1 = datetime(2026, 11, 3, 9, 0)


def centroids():
    """PLZ centroids of the simulated stations (a few hundred metres from where the simulated geocoder puts them)."""
    return pd.DataFrame([{"zip": s[4], "city": s[5], "state": "Nordrhein-Westfalen", "lat": s[8][0] + 0.004, "lng": s[8][1] - 0.004}
                         for s in STATIONS + [EXTRA]])


def sim(tmp_path, fault=None, variant="", mode="simulated", now=T0, geocoder_fault=None, **kw):
    kw.setdefault("client", SimulatedVuek(fault, variant))
    kw.setdefault("geocoder", SimulatedGeocoder(geocoder_fault))
    kw.setdefault("centroids", centroids())
    if mode == "full":
        kw.setdefault("confirm_full_run", True)
    return vuek_run.run(mode=mode, root=tmp_path, now=now, **kw)


def failed(res, severity="hard"):
    return [c["check"] for c in res.summary["checks"] if not c["ok"] and c["severity"] == severity]


def read(path):
    return list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))


# ------------------------------------------------------------------------------------------------ the good run
def test_simulated_run_passes_and_exports_the_candidate_schema(tmp_path):
    res = sim(tmp_path)
    assert res.outcome == "PASS" and failed(res) == [], res.summary["checks"]
    assert len(res.frame) == 5 and res.summary["in_scope"] == 5
    path = res.written["pruefstellen_csv"][0]
    assert path.name == "vuek_pruefstellen_2026-10-06.csv" and "test_output" in str(res.run_dir)
    rows = {r["source_id"]: r for r in read(path)}
    assert list(rows) == ["bad-oeynhausen", "bielefeld", "kirchlengern", "lage", "minden"]
    lage = rows["lage"]
    assert lage["location_id"] == "VUEK-lage" and lage["source_org"] == "VÜK" and lage["site_type"] == "pruefstelle"
    assert lage["in_scope"] == "True" and lage["scope_reason"] == "own_station" and lage["country"] == "DE"
    assert lage["state"] == "Nordrhein-Westfalen" and lage["coord_source"] == "nominatim" and float(lage["plz_distance_km"]) < 1
    assert (lage["street"], lage["zip"], lage["city"]) == ("Musterweg 1", "32791", "Lage")
    assert lage["opening_hours"].startswith("Mo-Fr 09:00-12:00") and lage["opening_hours_raw"].startswith("Montag bis Freitag")
    assert lage["hu_evidence"] == "station page names Hauptuntersuchung; HU page names the station"
    assert lage["doubt_flags"] == "notice_on_page" and lage["needs_review"] == "False" and len(lage["content_hash"]) == 64
    assert list(read(path)[0]) == vuek_run.COLUMNS


def test_simulated_run_writes_only_the_minimum_and_raw_per_page(tmp_path):
    res = sim(tmp_path)
    assert sorted(p.name for p in res.run_dir.iterdir()) == ["query_log.csv", "raw", "run_summary.json", "run_summary.md",
                                                           "vuek_pruefstellen_2026-10-06.csv"]
    raw = res.run_dir / "raw"
    assert sorted(p.name for p in raw.iterdir()) == ["hauptuntersuchung.html", "kontakt.html", "nominatim", "robots.txt",
                                                    "sitemap.xml", "sitemap_page.html", "stations", "ueber-uns.html"]
    assert len(list((raw / "stations").glob("*.html"))) == 5 and len(list((raw / "nominatim").glob("*.json"))) == 5
    nom = json.loads((raw / "nominatim" / "lage.json").read_text(encoding="utf-8"))
    assert "OpenStreetMap" in nom["attribution"] and nom["attempts"][0]["query"]["postalcode"] == "32791"
    assert "OpenStreetMap" in (res.run_dir / "run_summary.md").read_text(encoding="utf-8")


def test_query_log_has_one_line_per_request(tmp_path):
    client, geocoder = SimulatedVuek(), SimulatedGeocoder()
    res = sim(tmp_path, client=client, geocoder=geocoder)
    rows = read(res.run_dir / "query_log.csv")
    assert len(rows) == client.requests_made + len(geocoder.calls) == 11 + 5
    assert [r["label"] for r in rows[:2]] == ["about", "robots"]
    assert res.summary["requests_made"] == {"vuek": 11, "nominatim": 5}


def test_robots_are_recorded_and_the_crawl_delay_is_not_ignored(tmp_path):
    rb = sim(tmp_path).summary["robots"]
    assert rb["crawl_delay"] == 5.0 and rb["our_urls_disallowed"] == [] and "/app/" in rb["disallow"]


def test_known_gap_is_in_the_summary(tmp_path):
    res = sim(tmp_path)
    assert any("Partner workshops" in g for g in res.summary["known_gaps"])
    assert "Partnerwerkstätten" in res.summary["discovery"]["partner_workshop_text"]
    assert "Partner workshops" in (res.run_dir / "run_summary.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ runs that must FAIL
@pytest.mark.parametrize("fault,check,mentions", [
    ("menu_six", "menu == footer", "herford"),  # the menu shows 6
    ("footer_missing", "menu == footer", "kirchlengern"),  # the footer lacks one
    ("body_missing", "menu == body text of /ueber-uns/", "kirchlengern"),
    ("stated_sechs", "stated number (meta description) == number of stations", "sechs"),  # stated count "sechs"
    ("stated_unreadable", "stated number of stations is readable (meta description)", "an eigenen Standorten"),
    ("stated_body_sechs", "stated number in the body text is readable and equals the number of stations", "sechs"),
    ("no_footer_block", "footer: exactly one block 'VÜK Standorte'", "0 block"),
    ("sitemap_xml_extra", "menu == sitemap.xml", "herford"),
    ("sitemap_xml_missing", "menu == sitemap.xml", "kirchlengern"),
    ("sitemap_page_extra", "menu == HTML sitemap page", "herford"),
    ("sitemap_xml_broken", "sitemap.xml is a valid sitemap (urlset)", "ParseError"),
    ("no_address", "every station page yields street + PLZ + city", "minden"),  # a page without address
    ("no_street", "every station page yields street + PLZ + city", "minden"),
    ("station_404", "every station page could be read", "minden"),
])
def test_every_inconsistency_fails_loudly_and_marks_the_export(tmp_path, fault, check, mentions):
    res = sim(tmp_path, fault)
    assert res.outcome.startswith("FAIL") and res.summary["status"] == "FAIL"
    assert check in failed(res), res.summary["checks"]
    detail = next(c["detail"] for c in res.summary["checks"] if c["check"] == check)
    assert mentions in detail
    for p in res.run_dir.glob("vuek_pruefstellen*.csv"):
        assert "unverified" in p.name  # a failed run never writes a file that looks like a result
    assert "FAIL" in (res.run_dir / "run_summary.md").read_text(encoding="utf-8")


def test_menu_six_also_fails_the_count_and_the_known_set(tmp_path):
    res = sim(tmp_path, "menu_six", mode="full")
    names = failed(res)
    assert "menu == footer" in names and "menu == body text of /ueber-uns/" in names and "menu == sitemap.xml" in names
    assert "stated number (meta description) == number of stations" in names
    assert any(n.startswith("station set equals") for n in names)


def test_a_stated_count_that_differs_never_passes_even_if_all_lists_agree(tmp_path):
    res = sim(tmp_path, "stated_sechs")
    assert failed(res) == ["stated number (meta description) == number of stations"]
    detail = next(c["detail"] for c in res.summary["checks"] if not c["ok"] and c["severity"] == "hard")
    assert "the site says 6" in detail and "the links give 5" in detail


def test_block_stops_the_run_with_a_fail_summary_and_no_export(tmp_path):
    with pytest.raises(BlockedError):
        sim(tmp_path, "blocked")
    summary = json.loads(next(tmp_path.rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL" and "BlockedError" in summary["error"]
    assert not list(tmp_path.rglob("vuek_pruefstellen*.csv"))


def test_unreadable_about_page_stops_the_run(tmp_path):
    class NoAbout(SimulatedVuek):
        def get(self, url, params=None):
            if url.endswith("/ueber-uns/"):
                raise FetchError(f"{url} returned HTTP 500")
            return super().get(url, params)

    with pytest.raises(FetchError):
        sim(tmp_path, client=NoAbout())
    summary = json.loads(next(tmp_path.rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL" and "FetchError" in summary["error"]
    assert not list(tmp_path.rglob("vuek_pruefstellen*.csv"))


def test_a_page_without_hu_is_a_review_finding_not_a_failure_and_the_station_stays_in_scope(tmp_path):
    res = sim(tmp_path, "no_hu")
    assert res.outcome == "PASS" and len(res.frame) == 5
    assert failed(res, "review") == ["every station page says it offers the Hauptuntersuchung"]
    minden = res.frame[res.frame["source_id"] == "minden"].iloc[0]
    assert minden["in_scope"] and minden["needs_review"] and "hu_not_on_page" in minden["doubt_flags"]
    assert "hu_not_on_page" in minden["needs_review_reason"]


# ------------------------------------------------------------------------------------------------ coordinates
def test_nominatim_fallback_queries(tmp_path):
    g = SimulatedGeocoder("fallback")  # the first query for a hyphenated city finds nothing
    res = sim(tmp_path, geocoder=g)
    assert res.outcome == "PASS" and set(res.frame["coord_source"]) == {"nominatim"}
    assert ("Probestraße 2", "33647", "Bielefeld-Brackwede") in g.calls and ("Probestraße 2", "33647", "Bielefeld") in g.calls
    assert res.summary["stations"]["bielefeld"]["geocode_note"].startswith("attempt 2")


def test_no_coordinates_is_flagged_and_listed_not_hidden(tmp_path):
    res = sim(tmp_path, geocoder_fault="no_result")
    assert res.outcome == "PASS"  # a geocoder problem is not a completeness failure ...
    assert "every station has coordinates" in failed(res, "review")  # ... but it is reported loudly
    assert res.frame["lat"].isna().all() and res.frame["needs_review"].all()
    assert "coords_missing" in res.frame.iloc[0]["doubt_flags"]


def test_coordinates_far_from_the_plz_are_flagged(tmp_path):
    res = sim(tmp_path, geocoder_fault="far")
    assert res.outcome == "PASS" and any("km of the PLZ centroid" in n for n in failed(res, "review"))
    assert (res.frame["plz_distance_km"] > 100).all() and res.frame["needs_review"].all()
    assert "coordinate far from PLZ" in res.frame.iloc[0]["needs_review_reason"]


def test_a_geocoder_answer_for_another_postcode_is_flagged(tmp_path):
    res = sim(tmp_path, geocoder_fault="plz_mismatch")
    assert "geocoder answer names the station's PLZ" in failed(res, "review")
    assert "coords_postcode_mismatch" in res.frame.iloc[0]["doubt_flags"]


def test_coordinates_from_the_page_win_and_no_geocoder_request_is_made(tmp_path):
    g = SimulatedGeocoder()
    res = sim(tmp_path, "page_coords", geocoder=g)
    assert g.calls == [] and set(res.frame["coord_source"]) == {"page"} and res.outcome == "PASS"
    assert not (res.run_dir / "raw" / "nominatim").exists()


def test_without_centroids_the_plz_check_is_reported_as_not_done(tmp_path):
    res = sim(tmp_path, centroids=None)  # no GeoNames file under tmp_path, simulated mode never downloads
    assert any("PLZ centroids unavailable" in w for w in res.summary["warnings"])
    assert "coordinates within 25 km of the PLZ centroid (plz_distance_km)" in failed(res, "review")


# ------------------------------------------------------------------------------------------------ modes
def test_full_run_needs_confirmation_and_sends_nothing_without_it(tmp_path):
    fake = SimulatedVuek()
    with pytest.raises(RuntimeError, match="confirm_full_run=True"):
        vuek_run.run(mode="full", root=tmp_path, client=fake)
    assert fake.requests_made == 0 and not list(tmp_path.rglob("*"))


def test_full_goes_to_output_sample_to_test_output(tmp_path):
    full = sim(tmp_path, mode="full")
    assert full.outcome == "PASS" and full.run_dir.parent.name == "output" and full.run_dir.name == "2026-10-06_1200_full"
    assert full.summary["in_scope"] == 5 and full.summary["status"] == "PASS"
    assert not any(n.startswith("station set") for n in failed(full))


def test_sample_reads_ueber_uns_and_one_station_and_never_reports_pass(tmp_path):
    client, g = SimulatedVuek(), SimulatedGeocoder()
    res = sim(tmp_path, mode="sample", client=client, geocoder=g)
    assert res.outcome == "SAMPLE" and res.run_dir.parent.name == "test_output" and len(res.frame) == 1
    assert [u.removeprefix("https://www.vuek-gmbh.de") for u in client.urls] == ["/ueber-uns/", "/kontakt/pruefstelle-bad-oeynhausen/"]
    assert len(g.calls) == 1
    assert res.summary["counts"]["stations_in_menu"] == 5 and res.summary["counts"]["stations_crawled"] == 1


def test_sample_still_fails_on_a_hard_problem(tmp_path):
    assert sim(tmp_path, "footer_missing", mode="sample").outcome.startswith("FAIL")
    assert sim(tmp_path, "stated_sechs", mode="sample").outcome.startswith("FAIL")


def test_rebuild_reproduces_the_run_offline(tmp_path):
    live = sim(tmp_path, mode="full")
    rb = vuek_run.run(mode="rebuild", root=tmp_path, now=T1, source_runs=[live.run_dir], centroids=centroids())
    assert rb.outcome == "PASS" and rb.summary["requests_made"] == {"vuek": 0, "nominatim": 0}
    assert rb.run_dir.parent.name == "output" and not (rb.run_dir / "raw").exists()
    pd.testing.assert_frame_equal(rb.frame.drop(columns="scraped_at"), live.frame.drop(columns="scraped_at"))
    assert rb.summary["source_runs"] == [str(live.run_dir)]
    assert rb.summary["changes"]["unchanged"] == ["bad-oeynhausen", "bielefeld", "kirchlengern", "lage", "minden"]


def test_rebuild_fails_when_a_raw_file_is_missing(tmp_path):
    live = sim(tmp_path, mode="full")
    (live.run_dir / "raw" / "stations" / "minden.html").unlink()
    rb = vuek_run.run(mode="rebuild", root=tmp_path, now=T1, source_runs=[live.run_dir], centroids=centroids())
    assert rb.outcome.startswith("FAIL") and "every station page could be read" in failed(rb)


def test_rebuild_needs_a_source(tmp_path):
    with pytest.raises(ValueError, match="source_runs"):
        vuek_run.run(mode="rebuild", root=tmp_path)


def test_save_raw_false_writes_no_raw(tmp_path):
    res = sim(tmp_path, save_raw=False)
    assert not (res.run_dir / "raw").exists() and res.outcome == "PASS"


def test_never_writes_into_an_existing_run_folder(tmp_path):
    a, b = sim(tmp_path), sim(tmp_path)
    assert a.run_dir != b.run_dir and b.run_dir.name.endswith("_2")


# ------------------------------------------------------------------------------------------------ change detection
def test_change_detection_reports_address_phone_and_hours_changes(tmp_path):
    first = sim(tmp_path, mode="full")
    assert first.summary["changes"]["note"] == "no earlier passing full run" and first.summary["drop_check"].startswith("no earlier")
    second = sim(tmp_path, variant="v2", mode="full", now=T1)
    ch = second.summary["changes"]
    assert second.outcome == "PASS" and ch["baseline"] and ch["new"] == [] and ch["removed"] == []
    assert ch["changed"] == {"lage": ["phone"], "minden": ["hours"]}
    assert ch["unchanged"] == ["bad-oeynhausen", "bielefeld", "kirchlengern"]  # the e-mail re-keying is not a change
    assert "Changed: {'lage': ['phone'], 'minden': ['hours']}" in (second.run_dir / "run_summary.md").read_text(encoding="utf-8")


def test_unchanged_site_reports_no_change_although_cloudflare_rekeys_the_emails(tmp_path):
    sim(tmp_path, mode="full")
    again = sim(tmp_path, mode="full", now=T1)
    assert again.summary["changes"]["changed"] == {} and len(again.summary["changes"]["unchanged"]) == 5


def test_compare_with_baseline_unit():
    base = {"a": {"street": "X 1", "zip": "1", "city": "A", "phone": "1", "content_hash": "h1"},
            "b": {"street": "Y 1", "zip": "2", "city": "B", "phone": "2", "content_hash": "h2"},
            "c": {"street": "Z 1", "zip": "3", "city": "C", "phone": "3", "content_hash": "h3"}}
    now = {"a": dict(base["a"], street="X 2"), "b": dict(base["b"], content_hash="other"), "c": dict(base["c"]),
           "d": {"street": "N 1", "content_hash": "h4"}}
    ch = vuek_run.compare_with_baseline(now, base)
    assert ch["changed"] == {"a": ["address"], "b": ["other page content"]} and ch["new"] == ["d"] and ch["unchanged"] == ["c"]
    assert vuek_run.compare_with_baseline({"a": {}}, {"a": {}, "z": {}})["removed"] == ["z"]
    assert vuek_run.compare_with_baseline(now, None)["baseline"] is None


def test_a_consistent_new_station_still_fails_until_the_change_is_accepted(tmp_path):
    first = sim(tmp_path, mode="full")
    assert first.outcome == "PASS"
    second = sim(tmp_path, "sixth_consistent", mode="full", now=T1)  # menu, footer, body, sitemaps and the stated number all say six
    names = failed(second)
    assert second.outcome.startswith("FAIL") and len(names) == 1 and names[0].startswith("station set equals the last passing full run")
    assert "herford" in next(c["detail"] for c in second.summary["checks"] if c["check"] == names[0])
    third = sim(tmp_path, "sixth_consistent", mode="full", now=datetime(2026, 11, 4, 9, 0), accept_station_change=True)
    assert third.outcome == "PASS" and len(third.frame) == 6 and third.summary["accept_station_change"]
    assert third.summary["changes"]["new"] == ["herford"]
    assert any("CHANGE ACCEPTED" in c["detail"] for c in third.summary["checks"])
    # and the accepted run is the new baseline: the same six stations now pass without the switch
    fourth = sim(tmp_path, "sixth_consistent", mode="full", now=datetime(2026, 11, 5, 9, 0))
    assert fourth.outcome == "PASS"


def test_the_first_full_run_is_measured_against_the_five_stations_of_the_brief(tmp_path):
    res = sim(tmp_path, "sixth_consistent", mode="full")
    assert res.outcome.startswith("FAIL") and failed(res)[0].startswith("station set equals the five stations known at build time")


def test_drop_check_reports_against_the_last_passing_full_run(tmp_path):
    first = sim(tmp_path, mode="full")
    s = json.loads((first.run_dir / "run_summary.json").read_text(encoding="utf-8"))
    s["in_scope"] = 100
    (first.run_dir / "run_summary.json").write_text(json.dumps(s), encoding="utf-8")
    second = sim(tmp_path, mode="full", now=T1)
    assert second.summary["drop_check"].startswith("ALERT: 100 -> 5")
    assert second.summary["baseline"]["run_folder"] == first.run_dir.name


def test_a_failed_full_run_is_never_the_baseline(tmp_path):
    good = sim(tmp_path, mode="full")
    sim(tmp_path, "footer_missing", mode="full", now=T1)
    nxt = sim(tmp_path, mode="full", now=datetime(2026, 11, 4, 9, 0))
    assert nxt.summary["baseline"]["run_folder"] == good.run_dir.name


def test_run_modes_and_cli_arguments():
    with pytest.raises(ValueError, match="mode must be one of"):
        vuek_run.run(mode="nonsense")
    assert vuek_run.main(["full"]) == 2  # no --confirm: nothing is sent
