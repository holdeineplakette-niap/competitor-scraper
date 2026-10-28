"""Offline tests for the TÜV Hessen adapter (no network, no browser): parser, checks, run modes, negative tests."""

import json
from datetime import datetime

import pandas as pd
import pytest
from core import paths

from tuev_hessen import checks, fetch, parse, run as hessen_run
from tuev_hessen.simulate import FAULTS, SITES, make_hit, make_payload, simulated_outcome

NOW = datetime(2026, 10, 6, 12, 0)


@pytest.fixture
def plz():
    """PLZ centroids that match the simulated sites exactly (so only injected errors are 'far')."""
    return pd.DataFrame({"zip": [s[4] for s in SITES], "lat": [s[6] for s in SITES], "lng": [s[7] for s in SITES]})


def do_run(tmp_path, plz, mode="simulated", **kw):
    return hessen_run.run(mode, root=tmp_path, now=kw.pop("now", NOW), centroids=plz, **kw)


# ---------------------------------------------------------------- fetch helpers
def test_parse_multipart_reads_fields_in_order_and_keeps_empty_values():
    body = "\r\n".join([
        "------WebKitFormBoundaryAbC", 'Content-Disposition: form-data; name="language"', "", "0",
        "------WebKitFormBoundaryAbC", 'Content-Disposition: form-data; name="fixedTags[]"', "", "Kfz Prüfstelle",
        "------WebKitFormBoundaryAbC", 'Content-Disposition: form-data; name="address"', "", "",
        "------WebKitFormBoundaryAbC", 'Content-Disposition: form-data; name="tags[]"', "", "",
        "------WebKitFormBoundaryAbC--", ""])
    assert fetch.parse_multipart(body) == [("language", "0"), ("fixedTags[]", "Kfz Prüfstelle"), ("address", ""), ("tags[]", "")]
    assert fetch.parse_multipart(None) == [] and fetch.parse_multipart("a=b&c=d") == []


@pytest.mark.parametrize("status,headers,body,blocked", [
    (200, {}, b'{"hits": {}}', False),
    (403, {}, b"<html>forbidden</html>", True),
    (200, {"cf-mitigated": "challenge"}, b"x", True),
    (200, {}, b"<title>Just a moment...</title>", True),
    (503, {}, b"", True),
    (200, {}, "<html>Cloudflare cf-chl-opt</html>", True),
])
def test_looks_blocked(status, headers, body, blocked):
    assert bool(fetch.looks_blocked(status, headers, body)) is blocked


@pytest.mark.parametrize("body,ctype,status,ok", [
    (b"", "application/json", 200, False),
    (b"<html>Just a moment</html>", "text/html", 403, False),
    (b"<html>hello</html>", "text/html", 200, False),
    (b"not json", "application/json", 200, False),
    (b'{"hits": []}', "application/json", 200, False),
    (b'{"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}', "application/json", 200, True),
])
def test_decode_payload_only_accepts_the_expected_shape(body, ctype, status, ok):
    payload, err = checks.decode_payload(body, ctype, status)
    assert (payload is not None) is ok and bool(err) is not ok


def test_fetch_all_works_inside_a_running_event_loop_like_a_notebook(monkeypatch):
    import asyncio
    import threading

    seen = {}

    def fake_session(headless, outcome, detail_urls, rng):
        try:
            asyncio.get_running_loop()
            seen["loop"] = True
        except RuntimeError:
            seen["loop"] = False
        seen["thread"] = threading.current_thread() is not threading.main_thread()
    monkeypatch.setattr(fetch, "_session", fake_session)

    async def in_loop():  # what a Jupyter cell looks like to the code
        return fetch.fetch_all(seed=1)
    outcome = asyncio.run(in_loop())
    assert seen == {"loop": False, "thread": True} and outcome.access["blocked"] is False
    fetch.fetch_all(seed=1)  # without a loop it runs directly in the calling thread
    assert seen["thread"] is False


def test_real_chromium_starts_inside_a_jupyter_style_selector_loop():
    """Jupyter on Windows runs the selector event loop, which cannot start subprocesses; Playwright needs the proactor
    loop. Reproduces the notebook setup with a real (offline, about:blank) browser start."""
    import asyncio
    import sys

    pytest.importorskip("playwright")
    if sys.platform != "win32":
        pytest.skip("the selector-loop problem is Windows only")

    def start_and_stop_browser():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    return "no-chromium"
                raise
            page = browser.new_page()
            page.goto("about:blank")
            browser.close()
        return "ok"

    async def in_notebook_like_loop():
        return fetch._call_outside_event_loop(start_and_stop_browser)

    old = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # what ipykernel sets on Windows
    try:
        result = asyncio.run(in_notebook_like_loop())
    finally:
        asyncio.set_event_loop_policy(old)
    if result == "no-chromium":
        pytest.skip("run: python -m playwright install chromium")
    assert result == "ok"
    assert type(asyncio.get_event_loop_policy()) is type(old)  # the kernel's policy is put back


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("title,site_type,operator", [
    ("TÜV Hessen Service-Center Alsfeld", "own_station", "TÜV Hessen"),
    ("  tüv  hessen service-center Kassel ", "own_station", "TÜV Hessen"),
    ("TÜV Hessen Partner Autohaus X", "partner", "Partner"),
    ("TÜV SÜD Auto Partner GmbH Hanau", "tuev_sued_auto_partner", "TÜV SÜD Auto Partner"),
    ("TÜV SÜD Auto Partner Hanau", "unclassified", ""),  # no 'GmbH': listed for a human, not guessed
    ("TÜV Hessen LKW Service-Center Friedberg Süd", "unclassified", ""),
    ("TÜV Hessen - MPU Hanau - Begutachtungsstelle für Fahreignung", "unclassified", ""),
    ("TÜV Hessen Darmstadt", "unclassified", ""),
    ("", "unclassified", ""),
])
def test_classify_title(title, site_type, operator):
    assert parse.classify_title(title) == (site_type, operator)


def test_tag_title_mismatch_reports_both_directions():
    assert parse.tag_title_mismatch("own_station", ["Kfz Prüfstelle", "TÜH Service-Center"]) == []
    assert parse.tag_title_mismatch("own_station", ["Kfz Prüfstelle"]) == ["own_station without tag 'TÜH Service-Center'"]
    assert parse.tag_title_mismatch("partner", ["Auto-Partner", "TÜH Service-Center"]) == ["tag 'TÜH Service-Center' on a partner title"]
    assert parse.tag_title_mismatch("unclassified", ["MPU Begutachtung"]) == []


def test_normalise_address_treats_strasse_spellings_alike():
    assert parse.normalise_address("Grünberger Straße 76", "36304") == parse.normalise_address("Grünberger Str. 76", "36304")
    assert parse.normalise_address("A 1", "36304") != parse.normalise_address("A 1", "36305")


# ---------------------------------------------------------------- parse_hit
def test_parse_hit_reads_fax_hours_booking_and_hu_evidence():
    row = parse.parse_hit(make_hit(SITES[0]), "2026-10-06T12:00:00")
    assert (row["location_id"], row["source_id"], row["site_type"], row["operator"], row["listed_by"]) == (
        "TUEV_HESSEN-1", "1", "own_station", "TÜV Hessen", "TÜV Hessen")
    assert row["is_hu_site"] and not row["is_lkw_site"]
    assert row["fax"] == "06631 111112" and row["phone"] == "06631 111111"
    assert row["opening_hours"] == "MO - FR: 8:00 - 17:00 Uhr; SA: 8:00 - 13:00 Uhr"
    assert row["location_sign"] == "701" and row["booking_url"].endswith("locationSign=701")
    assert row["detail_url"] == "https://www.tuev-hessen.de/287/standort/site-1/"
    assert "infotext" in row["hu_evidence"] and "booking_link_hu" in row["hu_evidence"]
    assert (row["latitude"], row["longitude"]) == (50.7426, 9.258)
    assert row["unknown_source_fields"] == ""


def test_parse_hit_keeps_unknown_fields_visible():
    hit = make_hit(SITES[0])
    hit["_source"]["new_field"] = "x"
    hit["_source"]["searchable_meta"]["extra"] = "y"
    hit["extra_hit_key"] = 1
    row = parse.parse_hit(hit)
    assert row["unknown_source_fields"] == "hit.extra_hit_key; new_field; searchable_meta.extra"


def test_parse_hit_without_phone_fax_or_coordinates_does_not_crash():
    hit = make_hit(SITES[3])
    hit["_source"]["latitude"] = ""
    del hit["_source"]["coordinates"]
    row = parse.parse_hit(hit)
    assert row["phone"] == "" and row["fax"] == "" and row["latitude"] is None


@pytest.mark.parametrize("text,found", [
    ("Haupt- und Abgasuntersuchen nach §29 StVZO", True),
    ("Hauptuntersuchung (HU) & Abgasuntersuchung (AU)", True),
    ("Termin zur HU buchen", True),
    ("KFZ-Prüf- und Schätzstelle", False),
    ("MPU Beratung und Begutachtung", False),
])
def test_hu_wording_regex(text, found):
    assert bool(parse.HU_TEXT.search(text)) is found


def test_betreiber_and_websites_are_extracted_best_effort():
    hit = make_hit(SITES[2])
    hit["_source"]["infotext"] = ("TÜV Hessen Partner Service-Center X, Weg 9, 99999 X, Betreiber: Ing.-Büro Beispiel "
                                  "Vertiefende Informationen unter https://sv-beispiel.de/ * An unserem Partner erhalten Sie Hauptuntersuchung.")
    row = parse.parse_hit(hit)
    assert row["betreiber_text"] == "Ing.-Büro Beispiel" and row["websites"] == "https://sv-beispiel.de/"
    assert parse.parse_hit(make_hit(SITES[0]))["betreiber_text"] == ""


def test_notices_are_read_from_nested_infotext_contents():
    hit = make_hit(SITES[0])
    hit["_source"]["infotext_contents"] = [{"uid": 1, "header": "Hinweis", "bodytext": "<p>Am 24.12. <b>geschlossen</b>.</p>"}, [], {"uid": 2}]
    assert parse.parse_hit(hit)["notices"] == "Hinweis | Am 24.12. geschlossen ."


# ---------------------------------------------------------------- checks
def test_data_quality_flags_known_problems(plz):
    rows = [parse.parse_hit(make_hit(s, shared_coords_with=(50.2994, 8.2681) if s[0] == 8 else None)) for s in SITES]
    f = checks.data_quality(rows, plz)
    assert [g["uids"] for g in f["coords_shared_with_other_address"]] == [[7, 8]]
    assert [u for u, _ in f["coords_far_from_plz"]] == [8]
    assert f["missing_phone"] == [4]
    by = {r["uid"]: r for r in rows}
    assert "coords_shared_with_other_address" in by[7]["dq_flags"] and by[7]["needs_review"]
    assert "tag_title_mismatch" in by[2]["dq_flags"] and not by[2]["needs_review"]  # own station without the TÜH tag: reported only
    assert "unclassified" in by[6]["dq_flags"] and "lkw_in_title" in by[6]["dq_flags"]


def test_shared_coordinate_findings_keep_uids_and_addresses_aligned(plz):
    rows = [parse.parse_hit(make_hit(s, shared_coords_with=(50.2994, 8.2681) if s[0] == 8 else None)) for s in SITES]
    f = checks.data_quality(list(reversed(rows)), plz)  # row order must not matter
    group = f["coords_shared_with_other_address"][0]
    assert group["uids"] == [7, 8]
    assert "Bad Camberg" in group["addresses"][0] and "Oberursel" in group["addresses"][1]


def test_shared_booking_location_sign_is_flagged(plz):
    rows = [parse.parse_hit(make_hit(s)) for s in SITES[:3]]
    rows[2]["location_sign"] = rows[0]["location_sign"]
    f = checks.data_quality(rows, plz)
    assert f["shared_location_signs"] == {"701": [1, 3]}
    assert all("location_sign_shared" in r["dq_flags"] and r["needs_review"] for r in (rows[0], rows[2]))
    assert "location_sign_shared" not in rows[1]["dq_flags"]


def test_data_quality_flags_coordinates_outside_germany_and_duplicates(plz):
    rows = [parse.parse_hit(make_hit(s)) for s in SITES[:3]]
    rows[0]["latitude"] = 10.0
    rows.append(dict(rows[1]))  # duplicate uid and address
    f = checks.data_quality(rows, plz)
    assert f["coords_outside_germany"] == [1]
    assert f["duplicate_uids"] == [2]
    assert f["address_shared"][0]["uids"] == [2, 2]


def test_data_quality_without_centroids_says_so():
    f = checks.data_quality([parse.parse_hit(make_hit(SITES[0]))], None)
    assert f["plz_check"].startswith("skipped") and f["coords_far_from_plz"] == []


def test_diff_datasets_finds_non_hu_sites():
    hits = [make_hit(s) for s in SITES]
    d = checks.diff_datasets(hits, [h for h, s in zip(hits, SITES) if s[-1]])
    assert d["only_in_all"] == [9, 10] and d["only_in_hu"] == [] and d["common"] == 8 and d["common_but_different"] == []


# ---------------------------------------------------------------- runs
def test_simulated_run_passes_and_writes_the_expected_files(tmp_path, plz):
    res = do_run(tmp_path, plz)
    assert res.status == "PASS", res.summary["failed_hard"]
    s = res.summary
    assert s["counts"] == {"all": 10, "hu": 8, "non_hu": 2} and s["in_scope"] == 8
    assert res.run_dir.parent.name == "test_output" and res.run_dir.name == "2026-10-06_1200_simulated"
    names = {p.name for p in res.run_dir.rglob("*") if p.is_file()}
    assert {"raw.jsonl", "query_log.csv", "run_summary.json", "run_summary.md", "tuev_hessen_hu_2026-10-06.csv", "tuev_hessen_all_2026-10-06.csv"} <= names
    assert len(list((res.run_dir / "raw" / "detail").glob("*.html"))) == 2
    raw = [json.loads(line) for line in (res.run_dir / paths.RAW_FILE).read_text(encoding="utf-8").splitlines()]
    assert [r["file"] for r in raw] == ["location_search_all_ui", "location_search_all_replay", "location_search_hu_ui", "location_search_hu_replay"]
    assert (res.run_dir / "tuev_hessen_hu_2026-10-06.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    hu = pd.read_csv(res.run_dir / "tuev_hessen_hu_2026-10-06.csv", sep=";", encoding="utf-8-sig", dtype=str, keep_default_na=False)
    assert len(hu) == 8 and set(hu["is_hu_site"]) == {"True"} and set(hu["in_scope"]) == {"True"}
    assert s["site_types"]["hu"] == {"partner": 4, "own_station": 2, "tuev_sued_auto_partner": 1, "unclassified": 1}
    assert [x["uid"] for x in s["findings"]["unclassified"] if x["hu"]] == [6]
    assert [x["uid"] for x in s["findings"]["non_hu_sites"]] == [9, 10]
    md = (res.run_dir / paths.RUN_SUMMARY_MD).read_text(encoding="utf-8")
    assert "TÜV Hessen run report: PASS" in md and "No size / limit / paging field" in md


@pytest.mark.parametrize("fault,expected_check", [
    ("drop_hit", "hits.total.value == len(hits.hits)"),
    ("gte_relation", "relation == 'eq'"),
    ("bad_counter", "UI counter"),
    ("blocked", "UI answer is HTTP 200 JSON"),
    ("empty", "at least one hit"),
    ("replay_differs", "replay answer == UI answer"),
    ("changed_payload", "captured request fields"),
    ("non_json", "UI answer is HTTP 200 JSON"),
])
def test_every_fault_makes_the_run_fail_loudly(tmp_path, plz, fault, expected_check):
    res = do_run(tmp_path, plz, fault=fault)
    assert res.status == "FAIL"
    assert any(expected_check in name for name in res.summary["failed_hard"]), res.summary["failed_hard"]
    assert res.summary["outcome"].startswith("FAIL")
    # nothing may look like a successful export
    for p in res.run_dir.glob("*.csv"):
        assert p.name == "query_log.csv" or "unverified" in p.name


def test_all_faults_are_covered_by_a_test():
    assert set(FAULTS) == {"drop_hit", "gte_relation", "bad_counter", "blocked", "empty", "replay_differs", "changed_payload", "non_json"}


def test_blocked_answer_is_an_error_not_zero_sites(tmp_path, plz):
    res = do_run(tmp_path, plz, fault="blocked")
    assert res.status == "FAIL" and res.summary["counts"]["all"] == 0
    assert not list(res.run_dir.glob("tuev_hessen_*"))  # no data at all, so no CSV, not even an empty one
    assert "blocked" in json.dumps(res.summary["checks"])
    assert "FAIL" in (res.run_dir / paths.RUN_SUMMARY_MD).read_text(encoding="utf-8")


def test_replay_failure_falls_back_to_the_ui_answer_with_a_review_note(tmp_path, plz):
    outcome = simulated_outcome()
    for cap in outcome.captures.values():
        cap.replay_body, cap.replay_status, cap.replay_error = b"", 0, "replay failed: network"
    res = do_run(tmp_path, plz, outcome=outcome)
    assert res.status == "PASS"
    assert res.summary["datasets"]["all"]["source_answer"] == "ui"
    assert any("replay" in c["check"] for c in res.summary["review"])


def test_full_needs_confirmation(tmp_path, plz):
    with pytest.raises(RuntimeError, match="confirm_full_run"):
        hessen_run.run("full", root=tmp_path, now=NOW, centroids=plz)
    assert not (tmp_path / "1_scrapers").exists()  # nothing created, nothing fetched


def test_full_run_with_a_fetch_error_fails_and_keeps_the_log(tmp_path, plz, monkeypatch):
    partial = fetch.FetchOutcome(log=[{"time": "t", "what": "page", "url": fetch.LOCATOR_URL, "status": 403, "seconds": 1, "note": ""}])

    def blocked(**kwargs):
        err = fetch.BlockedError("locator page blocked (HTTP 403)")
        err.outcome = partial
        raise err
    monkeypatch.setattr(fetch, "fetch_all", blocked)
    res = hessen_run.run("full", confirm_full_run=True, root=tmp_path, now=NOW, centroids=plz)
    assert res.status == "FAIL" and "blocked" in res.summary["error"]
    assert res.run_dir.parent.name == "output"
    assert not list(res.run_dir.glob("tuev_hessen_*"))
    assert (res.run_dir / paths.QUERY_LOG_FILE).exists() and (res.run_dir / paths.RUN_SUMMARY_JSON).exists()
    assert "ERROR" in (res.run_dir / paths.RUN_SUMMARY_MD).read_text(encoding="utf-8")


def test_a_run_never_writes_into_an_existing_folder(tmp_path, plz):
    first, second = do_run(tmp_path, plz), do_run(tmp_path, plz)
    assert first.run_dir != second.run_dir and second.run_dir.name.endswith("_2")


def test_rebuild_reproduces_a_run_from_its_raw_file(tmp_path, plz):
    live = do_run(tmp_path, plz, now=datetime(2026, 10, 6, 11, 0))
    # a rebuild is an official run: it goes to output/, next to its source (source here is a test_output folder, fine for the test)
    rebuilt = hessen_run.run("rebuild", root=tmp_path, now=NOW, centroids=plz, source_run=live.run_dir)
    assert rebuilt.status == "PASS" and rebuilt.run_dir.parent.name == "output"
    assert rebuilt.summary["counts"] == live.summary["counts"]
    assert not (rebuilt.run_dir / paths.RAW_FILE).exists()  # no copy of raw.jsonl
    a = pd.read_csv(live.run_dir / "tuev_hessen_all_2026-10-06.csv", sep=";", dtype=str, encoding="utf-8-sig", keep_default_na=False)
    b = pd.read_csv(rebuilt.run_dir / "tuev_hessen_all_2026-10-06.csv", sep=";", dtype=str, encoding="utf-8-sig", keep_default_na=False)
    pd.testing.assert_frame_equal(a.drop(columns="scraped_at"), b.drop(columns="scraped_at"))


def test_drop_against_the_last_passing_full_run_fails(tmp_path, plz):
    folder = paths.new_run_dir("tuev_hessen", "full", datetime(2026, 9, 1, 9, 0), tmp_path)
    (folder / paths.RUN_SUMMARY_JSON).write_text(json.dumps({"mode": "full", "status": "PASS", "in_scope": 100, "run_date": "2026-09-01"}), encoding="utf-8")
    res = do_run(tmp_path, plz)
    assert res.status == "FAIL" and any("dropped" in n for n in res.summary["failed_hard"])


def test_pick_detail_urls_takes_an_own_station_and_a_partner():
    urls = hessen_run.pick_detail_urls(simulated_outcome().captures)
    assert urls == ["https://www.tuev-hessen.de/287/standort/site-1/", "https://www.tuev-hessen.de/287/standort/site-3/"]


def test_make_payload_total_override_is_what_the_checks_see():
    payload = make_payload([make_hit(SITES[0])], total=5)
    assert payload["hits"]["total"]["value"] == 5 and len(payload["hits"]["hits"]) == 1
