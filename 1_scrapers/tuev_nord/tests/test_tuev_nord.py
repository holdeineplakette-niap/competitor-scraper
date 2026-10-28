"""Offline tests for the TÜV NORD adapter (no network): parser, classifier, checks, run modes, negative tests."""

import json
from datetime import datetime

import pandas as pd
import pytest
import requests
from core import paths

from tuev_nord import checks, fetch, parse, run as nord_run
from tuev_nord.simulate import FAULTS, SITES, TRUTH, detail_html, list_html, record, simulated_outcome, stations_html

NOW = datetime(2026, 10, 6, 12, 0)


@pytest.fixture
def plz():
    """PLZ centroids that match the simulated sites exactly (so only injected errors are 'far')."""
    return pd.DataFrame({"zip": [s[3] for s in SITES], "lat": [s[5] for s in SITES], "lng": [s[6] for s in SITES]})


def do_run(tmp_path, plz, mode="simulated", **kw):
    return nord_run.run(mode, root=tmp_path, now=kw.pop("now", NOW), centroids=plz, **kw)


def failed_checks(res):
    return [c["check"] for c in res.summary["checks"] if c["severity"] == "hard" and not c["ok"]]


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("name,city,site_type,operator", [
    ("Ahaus", "Ahaus", "own_station_probable", ""),
    ("Achern (CarControl GmbH)", "Achern", "partner_station", "CarControl GmbH"),
    ("Zeil am Main (Ingenieurbüro Giesbrecht)", "Zeil am Main", "partner_station", "Ingenieurbüro Giesbrecht"),
    ("Mühldorf a.Inn  (KFZ-Ingenieurbüro ALDASORO)", "Mühldorf a. Inn", "partner_station", "KFZ-Ingenieurbüro ALDASORO"),  # double space
    ("Altensteig, Ingenieurbüro Binder", "Altensteig", "partner_station", "Ingenieurbüro Binder"),
    ("Rosenheim (K&M)", "Rosenheim", "partner_station", "K&M"),
    ("Sulingen 2 (Raven Landtechnik)", "Sulingen", "partner_station", "Raven Landtechnik"),
    ("Dortmund (Fa. Rosier ÜO)", "Dortmund", "partner_station", "Fa. Rosier ÜO"),
    ("Halle (Saale)", "Halle (Saale)", "own_station_probable", ""),  # the bracket is part of the city name
    ("Kreuztal (Hilchenbach-Dahlbruch)", "Hilchenbach-Dahlbruch", "own_station_probable", ""),  # bracket repeats the city field
    ("Dortmund (Wambel)", "Dortmund", "unclassified", "Wambel"),  # district or surname: not guessed
    ("Hofweier (Gerber)", "Hofweier", "unclassified", "Gerber"),
    ("Berlin, Mitte", "Berlin", "unclassified", "Mitte"),
    ("Ingenieurbüro Akermann", "Bad Rappenau", "unclassified", ""),  # plain name with company words
    ("HK Ingenieure GmbH & Co. KG", "Goch", "unclassified", ""),
    ("Kfz-Natzius Rostock", "Rostock", "unclassified", ""),
    ("Bad Oeynhausen", "Bad Oeynhausen", "own_station_probable", ""),
    ("Mülheim/Ruhr", "Mülheim", "own_station_probable", ""),
    ("Berlin-Alt-Treptow", "Berlin", "own_station_probable", ""),
    ("", "", "unclassified", ""),
])
def test_split_name(name, city, site_type, operator):
    r = parse.split_name(name, city)
    assert (r["site_type"], r["operator_name"]) == (site_type, operator)


def test_normalise_address_treats_strasse_spellings_alike():
    n = parse.normalise_address
    assert n("Grünberger Straße 76", "36304") == n("Grünberger Str. 76", "36304") == n("Grünberger Strasse 76 ", "36304")
    assert n("Geisenheimer Str, 59", "65385") == n("Geisenheimer Str. 59", "65385")
    assert n("A 1", "36304") != n("A 1", "36305") and n("A 1", "36304") != n("A 2", "36304")


def test_station_link_slug_variants_and_office_token():
    assert parse.station_link("/de/stationen/achern-carcontrol-gmbh/")["station_slug"] == "achern-carcontrol-gmbh"
    v = parse.station_link("/de/stationen/stationsseiten/rheinstetten-ingenieurbuero-topcu/")
    assert (v["station_slug"], v["link_variant"]) == ("rheinstetten-ingenieurbuero-topcu", "stationsseiten")
    assert v["detail_url"] == "https://www.tuev-nord.de/de/stationen/stationsseiten/rheinstetten-ingenieurbuero-topcu/"
    office = parse.station_link("/de/stationen/kontakt-aufnehmen/?token=abc.def&cHash=1")
    assert office["station_slug"] == "" and office["detail_url"] == "" and office["link_path"] == "/de/stationen/kontakt-aufnehmen/"


# ---------------------------------------------------------------- parse_record
def test_parse_record_keeps_raw_fields_and_enriches():
    row = parse.parse_record(record(SITES[2]), "2026-10-06T12:00:00")
    assert (row["source_id"], row["name"], row["site_type"], row["operator_name"], row["listed_by"]) == ("103", "Charlie (Ingenieurbüro Muster)", "partner_station", "Ingenieurbüro Muster", "TÜV NORD")
    assert row["is_hu_site"] and not row["is_office"] and row["service_type"] == "HU-UNG"
    assert row["station_slug"] == "charlie-ingenieurbuero-muster" and row["detail_url"].endswith("/de/stationen/charlie-ingenieurbuero-muster/")
    assert (row["latitude"], row["longitude"], row["zip"]) == (53.0793, 8.8017, "28195")
    assert row["unknown_source_fields"] == "" and row["_missing_fields"] == []


def test_parse_record_office_has_no_slug_and_never_keeps_the_token():
    row = parse.parse_record(record(SITES[10]))
    assert row["site_type"] == "office" and row["is_office"] and not row["is_hu_site"]
    assert row["station_slug"] == "" and row["detail_url"] == "" and "token" not in row["link_raw"] and row["link_raw"].endswith("/kontakt-aufnehmen/")


def test_parse_record_unknown_and_missing_fields_stay_visible():
    rec = record(SITES[0])
    rec["newField"] = 1
    del rec["plz"]
    row = parse.parse_record(rec)
    assert row["unknown_source_fields"] == "newField" and row["_missing_fields"] == ["plz"]


def test_location_ids_are_unique_even_when_the_id_is_reused():
    rows = parse.parse_records([record(s) for s in SITES])
    ids = [r["location_id"] for r in rows]
    assert len(set(ids)) == len(ids) == len(SITES)
    assert {r["location_id"] for r in rows if r["source_id"] == "202"} == {"TUEV_NORD-202-1", "TUEV_NORD-202-2"}
    assert len({r["source_key"] for r in rows}) == len(rows)  # id + address is unique


# ---------------------------------------------------------------- pages
def test_parse_stations_page_reads_records_and_counter():
    page = parse.parse_stations_page(stations_html())
    assert len(page.records) == len(SITES) and page.counters == [len(SITES)] and page.n_map_elements == 1


@pytest.mark.parametrize("fault,message", [("no_map", "exactly one"), ("empty_locations", "empty list"), ("bad_json", "not valid JSON")])
def test_parse_stations_page_raises_on_a_changed_page(fault, message):
    with pytest.raises(parse.PageError, match=message):
        parse.parse_stations_page(stations_html(fault))


def test_parse_list_page_reads_slugs_variants_and_letters():
    lp = parse.parse_list_page(list_html())
    slugs = {i["slug"]: i for i in lp.items}
    assert "alpha" in slugs and slugs["special-page"]["variant"] == "stationsseiten" and slugs["alpha"]["letter"] == "A"
    assert lp.other_links == []
    with pytest.raises(parse.PageError):
        parse.parse_list_page("<html><body>nothing</body></html>")


def test_describe_detail_reads_hours_services_partner_and_coordinates():
    d = parse.describe_detail(detail_html(SITES[2]))
    assert d["h1"] == "Charlie (Ingenieurbüro Muster)" and d["address_lines"] == ["Weg 3", "28195 Bremen"]
    assert d["opening_hours_lines"] == ["Montag–Freitag", "09:00–17:00"] and d["has_opening_hours"]
    assert d["lists_hu_service"] and d["has_partner_block"] and "Partnerunternehmen" in d["partner_note"]
    assert d["map_coords"][0]["lat"] == 53.0793 and d["phones"] == ["0800 80 70 600"]
    assert not parse.describe_detail(detail_html(SITES[8]))["lists_hu_service"]  # UNG-only page
    assert d["booking_station_ids"] == ["103"] and not d["is_finder_page"]


def test_a_missing_station_page_is_recognised_as_the_finder_page():
    finder = ('<html><head><title>TÜV NORD Stationssuche</title><link rel="canonical" href="https://www.tuev-nord.de/de/stationen/"/></head>'
              "<body><main><h1>TÜV NORD Stationssuche – Prüfstelle &amp; Sachverständige finden</h1></main></body></html>")
    assert parse.describe_detail(finder)["is_finder_page"] is True


def test_detail_checks_compare_stationid_and_flag_a_finder_page():
    row = parse.parse_record(record(SITES[2]))
    good = parse.describe_detail(detail_html(SITES[2]))
    assert all(c["ok"] for c in checks.check_detail("partner", "u", 200, good, row, True))
    wrong = dict(good, booking_station_ids=["999"])
    assert [c["check"] for c in checks.check_detail("partner", "u", 200, wrong, row, True) if not c["ok"]] == ["detail page (partner): booking stationId == id in the list data"]
    finder = dict(good, is_finder_page=True)
    assert any(not c["ok"] and "own page" in c["check"] for c in checks.check_detail("partner", "u", 200, finder, row, True))


# ---------------------------------------------------------------- fetch
class FakeResp:
    def __init__(self, status=200, text="<html>ok</html>", headers=None):
        self.status_code, self.text, self.content, self.headers = status, text, text.encode(), headers or {}
        self.url, self.raw = "https://www.tuev-nord.de/x/", None


def client_with(responses):
    c = fetch.NordClient(sleep=lambda s: None, clock=lambda: 0.0, delay_range=(0, 0))
    it = iter(responses)
    c.session.get = lambda url, timeout=None: next(it)
    return c


def test_client_accepts_no_cookies():
    from email.message import Message
    from urllib.request import Request

    c = fetch.NordClient()
    headers = Message()
    headers["Set-Cookie"] = "NSC_TEST=abc; path=/; secure; httponly"
    reply = type("R", (), {"info": lambda self: headers})()
    c.session.cookies.extract_cookies(reply, Request("https://www.tuev-nord.de/de/stationen/"))
    assert len(c.session.cookies) == 0  # a server cookie is never stored, so none is sent back
    assert c.session.headers["Accept-Language"].startswith("de-DE")


@pytest.mark.parametrize("status,headers,text,blocked", [
    (200, {}, "<html>fine</html>", False), (403, {}, "forbidden", True), (200, {"cf-mitigated": "challenge"}, "x", True),
    (200, {}, "<title>Just a moment...</title>", True), (503, {}, "", True), (200, {}, "<html>Access Denied</html>", True),
])
def test_detect_block(status, headers, text, blocked):
    assert bool(fetch.detect_block(status, headers, text)) is blocked


def test_client_retries_server_errors_and_gives_up():
    c = client_with([FakeResp(500), FakeResp(502), FakeResp(200)])
    assert c.get("https://www.tuev-nord.de/x/", "t").attempts == 3
    c = client_with([FakeResp(500)] * 4)
    with pytest.raises(fetch.FetchError, match="failed after"):
        c.get("https://www.tuev-nord.de/x/", "t")


def test_client_blocked_and_unexpected_status_raise_but_404_can_be_allowed():
    with pytest.raises(fetch.BlockedError):
        client_with([FakeResp(403, "<html>forbidden</html>")]).get("https://www.tuev-nord.de/x/", "t")
    with pytest.raises(fetch.FetchError, match="HTTP 404"):
        client_with([FakeResp(404)]).get("https://www.tuev-nord.de/x/", "t")
    assert client_with([FakeResp(404)]).get("https://www.tuev-nord.de/x/", "t", ok_statuses=(200, 404)).status == 404


def test_fetch_all_attaches_the_partial_outcome_to_the_error():
    c = client_with([FakeResp(200, stations_html()), FakeResp(403, "x")])
    with pytest.raises(fetch.BlockedError) as exc:
        fetch.fetch_all(c)
    assert exc.value.outcome.stations is not None and exc.value.outcome.requests_made == 2 and len(exc.value.outcome.log) == 2


def test_pickers_choose_own_partner_and_hu_only_and_the_one_source_only_pages():
    out = simulated_outcome()
    picked = nord_run.pick_details(out)
    assert [r for r, _ in picked] == ["own station", "partner station", "HU-only station"]
    assert all(u.startswith("https://www.tuev-nord.de/de/stationen/") for _, u in picked)
    state = {}
    extra = nord_run.make_extra_picker(state)(out)
    assert state["candidates"] == len(extra) == 2 and {u.rsplit("/", 2)[-2] for _, u in extra} == {"only-in-list", "special-page"}
    assert dict(extra)["only in the list page"].startswith("https://www.tuev-nord.de/de/stationen/")


# ---------------------------------------------------------------- run modes
def test_simulated_run_passes_and_matches_the_truth(tmp_path, plz):
    res = do_run(tmp_path, plz)
    assert res.status == "PASS", failed_checks(res)
    assert res.summary["counts"]["all"] == TRUTH["records"] and res.summary["counts"]["hu"] == TRUTH["hu"] == res.summary["in_scope"]
    b = res.summary["breakdowns"]
    assert b["site_type"]["all"] == {"own_station_probable": TRUTH["own_probable"], "partner_station": TRUTH["partner"], "office": TRUTH["offices"], "unclassified": 1}
    assert b["is_office"] == {"true": TRUTH["offices"], "false": TRUTH["stations"]}
    assert [u["name"] for u in res.summary["findings"]["unclassified"]] == TRUTH["unclassified"]
    assert {x["slug"] for x in res.summary["cross"]["only_in_list"]} == set(TRUTH["list_only"]) and res.summary["cross"]["only_in_map"] == []
    assert (res.run_dir / "raw" / "stationen.html").exists() and (res.run_dir / "raw" / "detail").is_dir()
    assert (res.run_dir / "tuev_nord_run_report_2026-10-06.md").exists() and res.run_dir.parent.name == "test_output"


def test_nothing_is_dropped_and_known_quirks_are_flagged(tmp_path, plz):
    res = do_run(tmp_path, plz)
    f = res.summary["findings"]
    assert [x["id"] for x in f["ids_reused_with_other_address"]] == ["202"] and f["duplicate_source_keys"] == {}
    assert len(f["addresses_shared_by_several_ids"]) == 1 and "Hauptstr. 1" in f["addresses_shared_by_several_ids"][0]["address"]
    allrows = pd.read_csv(res.run_dir / res.written["all"]["file"], sep=";", dtype=str, keep_default_na=False)
    assert len(allrows) == TRUTH["records"] and allrows["location_id"].is_unique
    flags = dict(zip(allrows["location_id"], allrows["dq_flags"]))
    assert "id_reused_other_address" in flags["TUEV_NORD-202-1"] and "address_shared_with_other_id" in flags["TUEV_NORD-203"]
    assert allrows.loc[allrows["name"] == "Foxtrot (Wambel)", "needs_review"].item() == "True"


def test_exports_are_utf8_with_bom_and_the_hu_file_holds_only_hu_sites(tmp_path, plz):
    res = do_run(tmp_path, plz)
    assert set(res.written) == {"all", "hu"} and res.written["hu"]["rows"] == TRUTH["hu"]
    raw = (res.run_dir / res.written["hu"]["file"]).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    hu = pd.read_csv(res.run_dir / res.written["hu"]["file"], sep=";", dtype=str, keep_default_na=False)
    assert set(hu["service_type"]) <= {"HU", "HU-UNG"} and (hu["is_hu_site"] == "True").all() and (hu["in_scope"] == "True").all()
    assert res.written["all"]["file"] == "tuev_nord_all_2026-10-06.csv" and res.written["hu"]["file"] == "tuev_nord_hu_2026-10-06.csv"


@pytest.mark.parametrize("fault,expected", [
    ("counter_mismatch", "counter == number of parsed records"),
    ("filtered_page", "page is unfiltered: all three serviceTypes present"),
    ("unknown_type", "no serviceType value other than HU / HU-UNG / UNG"),
    ("missing_field", "every record has all ten expected fields"),
    ("list_missing_half", "map stations and list agree"),
])
def test_every_injected_fault_fails_the_run_and_marks_the_exports(tmp_path, plz, fault, expected):
    res = do_run(tmp_path, plz, fault=fault)
    assert res.status == "FAIL" and any(expected in c for c in failed_checks(res)), failed_checks(res)
    assert all("unverified" in v["file"] for v in res.written.values()) and res.written


@pytest.mark.parametrize("fault", ["no_map", "empty_locations", "bad_json"])
def test_a_changed_page_fails_without_any_csv(tmp_path, plz, fault):
    res = do_run(tmp_path, plz, fault=fault)
    assert res.status == "FAIL" and not res.written and not list(res.run_dir.glob("tuev_nord_*.csv"))
    assert "stations page has the expected structure" in failed_checks(res)


def test_a_blocked_site_is_a_failed_run_not_zero_stations(tmp_path, plz):
    res = do_run(tmp_path, plz, fault="blocked")
    assert res.status == "FAIL" and "blocked" in res.summary["error"] and not res.written


def test_a_full_run_needs_confirmation_and_unknown_faults_are_rejected(tmp_path, plz):
    with pytest.raises(RuntimeError, match="confirm_full_run"):
        do_run(tmp_path, plz, mode="full")
    with pytest.raises(ValueError):
        simulated_outcome("nonsense")
    with pytest.raises(ValueError):
        do_run(tmp_path, plz, mode="rebuild")
    assert set(FAULTS) >= {"counter_mismatch", "filtered_page", "blocked"}


def test_full_run_with_a_prepared_outcome_goes_to_output_and_rebuild_reproduces_it(tmp_path, plz):
    first = do_run(tmp_path, plz, mode="full", outcome=simulated_outcome())
    assert first.status == "PASS" and first.run_dir.parent.name == "output"
    again = do_run(tmp_path, plz, mode="rebuild", source_run=first.run_dir, now=datetime(2026, 10, 7, 9, 0))
    assert again.status == "PASS" and again.run_dir.parent.name == "output"
    assert again.summary["counts"] == first.summary["counts"] and again.summary["breakdowns"] == first.summary["breakdowns"]
    assert [d["description"]["h1"] for d in again.summary["detail_pages"]] == [d["description"]["h1"] for d in first.summary["detail_pages"]]
    assert not (again.run_dir / "raw").exists()  # no copy of the raw pages
    assert again.summary["baseline"]["run_folder"] == first.run_dir.name  # the HU count is compared with the full run


def test_a_big_hu_drop_against_the_last_passing_full_run_fails(tmp_path, plz):
    first = do_run(tmp_path, plz, mode="full", outcome=simulated_outcome())
    summary = json.loads((first.run_dir / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
    summary["in_scope"] = 1000  # pretend the earlier run found far more
    (first.run_dir / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary), encoding="utf-8")
    res = do_run(tmp_path, plz, mode="full", outcome=simulated_outcome(), now=datetime(2026, 10, 7, 9, 0))
    assert res.status == "FAIL" and any("dropped" in c for c in failed_checks(res))


def test_a_simulated_run_is_never_compared_with_a_real_full_run(tmp_path, plz):
    first = do_run(tmp_path, plz, mode="full", outcome=simulated_outcome())
    summary = json.loads((first.run_dir / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
    summary["in_scope"] = 1000  # a real full run with far more HU sites
    (first.run_dir / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary), encoding="utf-8")
    sim = do_run(tmp_path, plz, now=datetime(2026, 10, 7, 9, 0))
    assert sim.status == "PASS" and sim.summary["baseline"] is None


def test_previous_run_comparison_reports_structure_changes(tmp_path, plz):
    do_run(tmp_path, plz, mode="full", outcome=simulated_outcome())
    again = do_run(tmp_path, plz, mode="full", outcome=simulated_outcome(), now=datetime(2026, 10, 7, 9, 0))  # same page again: no change
    assert again.summary["previous"]["keys_added"] == 0 and again.summary["previous"]["keys_removed"] == 0
    out2 = simulated_outcome()
    text = stations_html().replace('&quot;serviceType&quot;', '&quot;serviceKind&quot;')
    out2.stations.text, out2.stations.content = text, text.encode()
    third = do_run(tmp_path, plz, mode="full", outcome=out2, now=datetime(2026, 10, 8, 9, 0))
    assert "serviceKind" in third.summary["previous"]["fields_added"]
    assert any("same record fields" in c["check"] for c in third.summary["review"])


def test_never_writes_into_an_existing_run_folder(tmp_path, plz):
    a, b = do_run(tmp_path, plz), do_run(tmp_path, plz)
    assert a.run_dir != b.run_dir and b.run_dir.name.endswith("_2")


def test_report_names_the_counter_check_and_the_cross_check(tmp_path, plz):
    res = do_run(tmp_path, plz)
    text = (res.run_dir / "tuev_nord_run_report_2026-10-06.md").read_text(encoding="utf-8")
    for needle in ("counter == number of parsed records", "Only in the list page", "special-page", "Spot-check sample", "Fragile points", "Foxtrot (Wambel)"):
        assert needle in text
