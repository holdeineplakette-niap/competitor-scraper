"""DEKRA adapter: parsing, scope rule, flags, completeness check, client errors and the run folder.

All offline. Network access is replaced by FakeDekraApi or a patched requests.post. The records are synthetic.
"""

import json
from datetime import datetime

import pytest
import requests

from dekra import adapter as dekra

AUTO = {"id": dekra.AUTO_TYPE_ID, "key": "automotive_testing", "name": "Automobil Prüfwesen und Gutachten"}
AT = {"id": "9de4a8cc-f470-445b-a69e-06d233562a95", "key": dekra.AT_TYPE_KEY, "name": "Automobil Prüfwesen"}
WHG = {"id": dekra.WHG_TYPE_ID, "key": "whg_location", "name": "WHG Standort"}
INDUSTRY = {"id": "68a4d9fa-9295-4a62-8f2b-db5dcfc11756", "key": "x", "name": "Industrie, Bau und Immobilien"}
T = datetime(2026, 10, 6, 9, 0)


def rec(**overrides) -> dict:
    base = {
        "id": "11111111-1111-4111-8111-111111111111",
        "name": "Station Teststadt",
        "name2": "DEKRA Automobil GmbH",
        "street": "Teststr.",
        "houseNumber": "5",
        "zipCode": "10553",
        "city": "Berlin",
        "latitude": 52.5,
        "longitude": 13.3,
        "countryResolved": {"code": "DE"},
        "locationTypesResolved": [AUTO],
        "suitableForCommercialVehicles": False,
        "specialServicesList": [],
        "openingHoursAreas": [],
        "freeText1": {"type": "RichText", "elements": []},
        "currentInformation": {"type": "RichText", "elements": []},
        "linkWebsite": {"type": "Link", "to": "https://www.dekra.de/de/test"},
        "linkOnlineAppointment": None,
    }
    return base | overrides


def text(s: str) -> dict:
    return {"type": "RichText", "elements": [{"type": "paragraph", "content": [{"type": "string", "content": [s]}]}]}


# ---------------------------------------------------------------- scope

@pytest.mark.parametrize(
    "overrides, in_scope, reason_start, site_type",
    [
        ({}, True, "", "pruefstelle"),
        ({"locationTypesResolved": [AUTO, INDUSTRY]}, True, "", "pruefstelle"),
        ({"locationTypesResolved": [], "name": "Standort Versmold"}, True, "", "unknown"),
        ({"locationTypesResolved": [AT], "name": "Station Bietigheim"}, True, "", "unknown"),
        ({"locationTypesResolved": [], "name": "DEKRA Akademie Riesa"}, False, "untyped or AT-typed", "unknown"),
        ({"locationTypesResolved": [], "name": "DEKRA Automobil GmbH"}, True, "", "unknown"),  # "automobil" is a vehicle word
        ({"locationTypesResolved": [WHG]}, False, "WHG site only", "unknown"),
        ({"locationTypesResolved": [INDUSTRY]}, False, "type not vehicle-related", "unknown"),
        ({"countryResolved": {"code": "FR"}}, False, "outside Germany", "unknown"),
        ({"countryResolved": None}, False, "country missing", "unknown"),
    ],
)
def test_scope_rule(overrides, in_scope, reason_start, site_type):
    ok, reason, kind, _evidence = dekra.classify(rec(**overrides))
    assert (ok, kind) == (in_scope, site_type)
    assert reason.startswith(reason_start)


def test_whg_combined_with_a_vehicle_type_stays_in_scope():
    ok, *_ = dekra.classify(rec(locationTypesResolved=[AUTO, WHG]))
    assert ok


# ---------------------------------------------------------------- flags (they never exclude)

def test_flags_mark_doubt_but_do_not_change_scope():
    thin = rec(linkWebsite=None)
    assert dekra.FLAG_THIN in dekra.flags_of(thin)
    assert dekra.parse_record(thin, "t")["in_scope"] is True  # still included
    assert dekra.parse_record(thin, "t")["needs_review"] is True

    psp = rec(name="Standort Rodenkirchen (KIA Levy)")
    assert dekra.FLAG_PSP in dekra.flags_of(psp)

    partner = rec(name2="Sachverständigenbüro Test GmbH")
    flags = dekra.flags_of(partner)
    assert dekra.FLAG_NON_DEKRA_OPERATOR in flags
    assert not dekra.parse_record(partner, "t")["needs_review"]  # informational only

    disclaimer = rec(freeText1=text("Bitte beachten Sie, dass dieser Standort ein Gutachtenbüro ist (keine Kfz-Prüfstelle)."))
    assert dekra.FLAG_DISCLAIMER in dekra.flags_of(disclaimer)

    untyped = rec(locationTypesResolved=[], name="Standort X")
    assert dekra.FLAG_UNTYPED in dekra.flags_of(untyped)
    assert dekra.FLAG_AT_TYPE in dekra.flags_of(rec(locationTypesResolved=[AT], name="Station X"))


def test_a_clean_record_has_no_flags():
    assert dekra.flags_of(rec()) == []


# ---------------------------------------------------------------- parsing

def test_parse_record_fields():
    row = dekra.parse_record(
        rec(
            zipCode="6886 ",
            houseNumber="12a",
            linkWebsite={"type": "Link", "to": "/de/moabit"},
            linkOnlineAppointment={"type": "Link", "to": " https://booking.dekra.com/book/x "},
            freeText1=text("Für Hauptuntersuchungen Online-Termin buchen."),
            specialServicesList=[{"type": "Option", "key": "k", "value": "LapID"}, {"type": "Option", "key": "j", "value": "Fahrerkarte"}],
            suitableForCommercialVehicles=True,
            phone1=" 030 123 ",
        ),
        "2026-10-06T09:00:00",
    )
    assert row["location_id"] == "DEKRA-11111111-1111-4111-8111-111111111111"
    assert row["zip"] == "06886"  # lost leading zero, trailing space
    assert row["street"] == "Teststr. 12a"
    assert row["dekra_profile_url"] == "https://www.dekra.de/de/moabit"
    assert row["booking_url"] == "https://booking.dekra.com/book/x"
    assert row["hu_mention"] == "freeText1"
    assert row["services"] == "LapID; Fahrerkarte"
    assert row["nfz"] is True and row["phone"] == "030 123"
    assert row["dekra_types"] == "Automobil Prüfwesen und Gutachten"
    assert set(row) == set(dekra.COLUMNS)


def test_street_does_not_repeat_a_house_number_already_in_the_street():
    assert dekra.parse_record(rec(street="Enderstraße 92b", houseNumber="92b"), "t")["street"] == "Enderstraße 92b"
    assert dekra.parse_record(rec(street="Enderstraße", houseNumber=None), "t")["street"] == "Enderstraße"


def test_foreign_postcodes_are_not_padded():
    row = dekra.parse_record(rec(countryResolved={"code": "SK"}, zipCode="949 01"), "t")
    assert row["zip"] == "949 01"


def test_opening_hours_are_grouped_by_identical_times():
    def hours(day, start, end):
        return {"data": {"from": {"date": f"1970-01-01T{start}:00.000Z"}, "to": {"date": f"1970-01-01T{end}:00.000Z"},
                         "weekdays": [{"key": day}]}}

    area = {"data": {"headline": "Fahrzeugprüfungen", "openingHours": [
        hours("monday", "08:00", "18:00"), hours("tuesday", "08:00", "18:00"), hours("wednesday", "08:00", "18:00"),
        hours("friday", "08:00", "16:00"), hours("saturday", "09:00", "12:00")]}}
    assert dekra.opening_hours(rec(openingHoursAreas=[area])) == (
        "Fahrzeugprüfungen: Mo–Mi 08:00–18:00, Fr 08:00–16:00, Sa 09:00–12:00"
    )
    assert dekra.opening_hours(rec()) is None


def test_same_address_count_includes_the_row_itself():
    a = rec(id="a", street="Handwerkstraße", houseNumber="15", zipCode="70565")
    b = rec(id="b", street="Handwerkstr.", houseNumber="15", zipCode="70565")
    c = rec(id="c", street="Anderweg", houseNumber="1", zipCode="70565")
    df = dekra.parse_records([a, b, c], "t")
    assert list(df["same_address_count"]) == [2, 2, 1]


def test_records_without_street_or_zip_still_parse():
    df = dekra.parse_records(
        [rec(id="a", street=None, houseNumber=None, zipCode=None), rec(id="b", street="", houseNumber="", zipCode="")], "t"
    )
    assert list(df["same_address_count"]) == [1, 1]  # an unknown address is not a shared address
    assert df["street"].isna().all() and df["zip"].isna().all()


def test_enrich_fills_state_from_the_plz(centroids):
    df = dekra.enrich_german(dekra.parse_records([rec(zipCode="10553")], "t"), centroids)
    assert df.loc[0, "state"] == "Berlin"


# ---------------------------------------------------------------- completeness

def _responses(records):
    box = {name: [r for r in records if dekra.in_box(r, b)] for name, b in dekra.FULL_REQUESTS.items()}
    return box


def test_completeness_passes_for_consistent_responses():
    records = dekra.simulated_records(60)
    result = dekra.check_completeness(_responses(records))
    assert result["passed"], result


def test_completeness_fails_when_a_quarter_misses_a_record():
    responses = _responses(dekra.simulated_records(60))
    responses["q2"] = responses["q2"][1:]
    result = dekra.check_completeness(responses)
    assert not result["passed"] and not result["checks"]["quarters_equal_germany_box"]


def test_completeness_fails_when_germany_box_misses_a_world_record_inside_it():
    responses = _responses(dekra.simulated_records(60))
    responses["de_box"] = responses["de_box"][1:]
    result = dekra.check_completeness(responses)
    assert not result["passed"] and not result["checks"]["world_in_box_equals_germany_box"]


def test_completeness_fails_on_duplicate_ids():
    responses = _responses(dekra.simulated_records(60))
    responses["world"] = responses["world"] + responses["world"][:1]
    assert not dekra.check_completeness(responses)["checks"]["no_duplicate_ids"]


# ---------------------------------------------------------------- shape and client

def test_check_shape_rejects_bad_shapes_and_isnextlocation():
    assert dekra.check_shape({"isNextLocation": False, "locations": []}) == []
    with pytest.raises(dekra.ApiError):
        dekra.check_shape({"locations": []})
    with pytest.raises(dekra.ApiError):
        dekra.check_shape([])
    with pytest.raises(dekra.ApiError, match="isNextLocation"):
        dekra.check_shape({"isNextLocation": True, "locations": []})


def test_request_body_has_no_type_filter():
    body = dekra.build_body(dekra.BOX_GERMANY)
    assert body["types"] == [] and body["boundaries"] == dekra.BOX_GERMANY
    assert body["commercialVehicles"] is False and body["locationsWithoutType"] is False


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text
        self.content = json.dumps(payload).encode() if payload is not None else text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_client_stops_on_http_error(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(403, text="forbidden"))
    with pytest.raises(dekra.ApiError, match="HTTP 403"):
        dekra.DekraClient(sleep=lambda s: None).post({})


def test_client_stops_on_non_json(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, text="<html>"))
    with pytest.raises(dekra.ApiError, match="not JSON"):
        dekra.DekraClient(sleep=lambda s: None).post({})


def test_client_retries_once_on_timeout_then_gives_up(monkeypatch):
    calls = []

    def post(*a, **k):
        calls.append(1)
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(dekra.ApiError, match="2 attempts"):
        dekra.DekraClient(sleep=lambda s: None).post({})
    assert len(calls) == 2


def test_client_succeeds_after_one_timeout_and_paces_requests(monkeypatch):
    answers = iter([requests.ConnectionError("x"), _Resp(200, {"isNextLocation": False, "locations": []})])

    def post(*a, **k):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    sleeps = []
    monkeypatch.setattr(requests, "post", post)
    client = dekra.DekraClient(pace_s=5, jitter_s=0, sleep=sleeps.append)
    data, info = client.post({})
    assert data["locations"] == [] and info["http_status"] == 200
    assert sleeps == [5.0]  # only the pause before the retry; the first request is not delayed


# ---------------------------------------------------------------- run folder

CONVENTION_FILES = {
    "query_log.csv", "run_summary.json", "run_summary.md",
    "dekra_all_2026-10-06.csv", "dekra_all_2026-10-06.xlsx",
    "dekra_pruefstellen_2026-10-06.csv", "dekra_pruefstellen_2026-10-06.xlsx",
}


def test_simulated_run_is_a_pass_in_test_output_without_raw(tmp_path, centroids):
    result = dekra.run(mode="simulated", root=tmp_path, centroids=centroids, started_at=T, verbose=False)
    folder = tmp_path / "1_scrapers" / "dekra" / "test_output" / "2026-10-06_0900_simulated"
    assert result.run_folder == folder
    assert {p.name for p in folder.iterdir()} == CONVENTION_FILES
    s = result.summary
    assert s["status"] == "PASS" and s["requests_made"] == 6
    # 100 fake records: 60 Automobil-typed + 10 untyped "Standort" in DE are in scope; WHG, Akademie and SK are not
    assert (s["rows_total"], s["rows_germany"], s["in_scope"]) == (100, 90, 70)
    assert s["site_types"] == {"pruefstelle": 60, "unknown": 10}
    assert s["flags"].get("untyped") == 10
    assert (folder / "run_summary.md").read_text(encoding="utf-8").startswith("# Run summary: 2026-10-06_0900_simulated")


def test_in_scope_export_holds_exactly_the_in_scope_rows(tmp_path, centroids):
    import pandas as pd

    result = dekra.run(mode="simulated", root=tmp_path, centroids=centroids, started_at=T, verbose=False)
    out = pd.read_csv(result.run_folder / "dekra_pruefstellen_2026-10-06.csv", sep=";", dtype=str, encoding="utf-8-sig")
    assert len(out) == 70 and set(out["in_scope"]) == {"True"}
    all_rows = pd.read_csv(result.run_folder / "dekra_all_2026-10-06.csv", sep=";", dtype=str, encoding="utf-8-sig")
    assert len(all_rows) == 100


def test_a_second_run_in_the_same_minute_gets_a_suffix(tmp_path, centroids):
    first = dekra.run(mode="simulated", root=tmp_path, centroids=centroids, started_at=T, verbose=False)
    second = dekra.run(mode="simulated", root=tmp_path, centroids=centroids, started_at=T, verbose=False)
    assert second.run_folder.name == first.run_folder.name + "_2"
    assert {p.name for p in first.run_folder.iterdir()} == CONVENTION_FILES


def test_recon_export_writes_a_run_folder_in_test_output_without_requests(tmp_path, centroids):
    source = tmp_path / "saved_response.json"
    source.write_text("{}", encoding="utf-8")
    df = dekra.enrich_german(dekra.parse_records(dekra.simulated_records(30), "t"), centroids)
    folder, written = dekra.write_recon_run(df, source, root=tmp_path, run_date="2026-10-06", started_at=T)
    assert folder == tmp_path / "1_scrapers" / "dekra" / "test_output" / "2026-10-06_0900_recon"
    assert {p.name for p in folder.iterdir()} == (CONVENTION_FILES - {"query_log.csv"})
    summary = json.loads((folder / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "recon" and summary["rows"] == 30 and summary["in_scope"] == int(df["in_scope"].sum())
    again, _ = dekra.write_recon_run(df, source, root=tmp_path, run_date="2026-10-06", started_at=T)
    assert again.name == folder.name + "_2"  # never written into twice


def test_full_run_needs_confirmation(tmp_path):
    with pytest.raises(RuntimeError, match="confirm_full_run"):
        dekra.run(mode="full", root=tmp_path, api=dekra.FakeDekraApi([]), started_at=T, verbose=False)
    assert not (tmp_path / "1_scrapers").exists()


def test_full_run_goes_to_output_and_keeps_raw(tmp_path, centroids):
    api = dekra.FakeDekraApi(dekra.simulated_records(40))
    result = dekra.run(mode="full", confirm_full_run=True, root=tmp_path, api=api, centroids=centroids, started_at=T, verbose=False)
    folder = tmp_path / "1_scrapers" / "dekra" / "output" / "2026-10-06_0900_full"
    assert result.run_folder == folder and (folder / "raw.jsonl").exists()
    lines = [json.loads(line) for line in (folder / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["file"] for x in lines] == ["world.json", "de_box.json", "q1.json", "q2.json", "q3.json", "q4.json"]
    assert all("locations" in x["response"] for x in lines)
    assert len(api.calls) == 6 and all(c["types"] == [] for c in api.calls)
    assert result.summary["status"] == "PASS"


def test_minimal_full_run_writes_one_export_and_no_raw_but_still_runs_every_check(tmp_path, centroids):
    api = dekra.FakeDekraApi(dekra.simulated_records(40))
    result = dekra.run(
        mode="full", confirm_full_run=True, root=tmp_path, api=api, centroids=centroids, started_at=T, verbose=False,
        exports=("pruefstellen_csv",), save_raw=False,
    )
    assert {p.name for p in result.run_folder.iterdir()} == {
        "query_log.csv", "run_summary.json", "run_summary.md", "dekra_pruefstellen_2026-10-06.csv"}
    assert len(api.calls) == 6 and result.summary["status"] == "PASS"
    assert result.summary["completeness"]["passed"] and result.summary["requests_made"] == 6
    assert result.summary["save_raw"] is False and result.summary["exports"] == ["pruefstellen_csv"]
    assert "not saved" in (result.run_folder / "run_summary.md").read_text(encoding="utf-8")


def test_failed_minimal_run_still_tags_its_single_export(tmp_path, centroids):
    api = _DropsOneFromQ2(dekra.simulated_records(60))
    result = dekra.run(
        mode="full", confirm_full_run=True, root=tmp_path, api=api, centroids=centroids, started_at=T, verbose=False,
        exports=("pruefstellen_csv",), save_raw=False,
    )
    assert result.summary["status"] == "FAIL"
    assert "dekra_pruefstellen_2026-10-06_unverified.csv" in {p.name for p in result.run_folder.iterdir()}


def test_unknown_export_key_is_rejected_before_any_request(tmp_path, centroids):
    api = dekra.FakeDekraApi([])
    with pytest.raises(ValueError, match="unknown export"):
        dekra.run(mode="simulated", root=tmp_path, api=api, centroids=centroids, started_at=T, verbose=False, exports=("pruefstellen_pdf",))
    assert api.calls == [] and not (tmp_path / "1_scrapers").exists()  # no run folder either


def test_sample_run_makes_one_request_and_is_not_a_pass(tmp_path, centroids):
    api = dekra.FakeDekraApi(dekra.simulated_records(40))
    result = dekra.run(mode="sample", root=tmp_path, api=api, centroids=centroids, started_at=T, verbose=False)
    assert len(api.calls) == 1 and api.calls[0]["boundaries"] == dekra.QUARTERS["q4"]
    assert result.summary["status"] == "SAMPLE" and result.summary["completeness"] is None
    assert (tmp_path / "1_scrapers" / "dekra" / "test_output" / "2026-10-06_0900_sample" / "raw.jsonl").exists()


class _DropsOneFromQ2(dekra.FakeDekraApi):
    def post(self, body):
        data, info = super().post(body)
        if body["boundaries"] == dekra.QUARTERS["q2"] and data["locations"]:
            data = {**data, "locations": data["locations"][1:]}
        return data, info


def test_inconsistent_responses_fail_and_tag_the_exports(tmp_path, centroids):
    api = _DropsOneFromQ2(dekra.simulated_records(60))
    result = dekra.run(mode="full", confirm_full_run=True, root=tmp_path, api=api, centroids=centroids, started_at=T, verbose=False)
    assert result.summary["status"] == "FAIL"
    assert any("quarters_equal_germany_box" in r for r in result.summary["status_reasons"])
    names = {p.name for p in result.run_folder.iterdir()}
    assert "dekra_all_2026-10-06_unverified.csv" in names and "dekra_all_2026-10-06.csv" not in names


class _FailsOnThird(dekra.FakeDekraApi):
    def post(self, body):
        if len(self.calls) == 2:
            self.calls.append(body)
            raise dekra.ApiError("HTTP 500: boom")
        return super().post(body)


def test_an_error_aborts_the_run_and_is_reported(tmp_path, centroids):
    api = _FailsOnThird(dekra.simulated_records(40))
    result = dekra.run(mode="full", confirm_full_run=True, root=tmp_path, api=api, centroids=centroids, started_at=T, verbose=False)
    s = result.summary
    assert s["status"] == "FAIL" and s["aborted"] and s["requests_made"] == 3
    assert "q1: HTTP 500" in s["status_reasons"][0]
    log = (result.run_folder / "query_log.csv").read_text(encoding="utf-8").splitlines()
    assert len(log) == 4 and log[-1].endswith("error")  # header + 2 ok + 1 error


def test_records_outside_germanys_box_are_kept_via_the_world_request(tmp_path, centroids):
    inside = rec(id="in", latitude=51.0, longitude=10.0)
    outside = rec(id="out", name="Station Drüben", latitude=45.5, longitude=4.8)  # DE-coded but coordinates in France
    api = dekra.FakeDekraApi([inside, outside])
    result = dekra.run(mode="full", confirm_full_run=True, root=tmp_path, api=api, centroids=centroids, started_at=T, verbose=False)
    assert result.summary["status"] == "PASS"
    assert result.summary["in_scope"] == 2 and result.summary["outside_box"] == 1


def test_drop_check_uses_the_last_passing_full_run(tmp_path, centroids):
    big = dekra.FakeDekraApi(dekra.simulated_records(100))
    dekra.run(mode="full", confirm_full_run=True, root=tmp_path, api=big, centroids=centroids, started_at=T, verbose=False)
    small = dekra.FakeDekraApi(dekra.simulated_records(100)[:50])
    later = datetime(2026, 11, 6, 9, 0)
    result = dekra.run(mode="full", confirm_full_run=True, root=tmp_path, api=small, centroids=centroids, started_at=later, verbose=False)
    assert result.summary["baseline"]["in_scope"] == 70
    assert result.summary["german_drop_alert"] is True and result.summary["drop_check"].startswith("ALERT")
