"""KÜS fetch layer: retries, stop rules, request limit and shape check, with a mocked HTTP session."""

import json

import pytest
import requests

from core import paths
from kues import fetch

FIXTURE = paths.fixtures_dir("kues") / "response_muenchen_80636_2026-10-05.json"
RECORDS = json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = RECORDS if body is None else body
        self.text = text if text is not None else json.dumps(self._body)
        self.content = self.text.encode()

    def json(self):
        if self.text.startswith("<"):
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Answers from a script: each item is a FakeResponse or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params), headers, timeout))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*script, **kwargs):
    sleeps = []
    c = fetch.KuesClient(session=FakeSession(*script), sleep=sleeps.append, pace_s=3, jitter_s=1, **kwargs)
    c.sleeps = sleeps
    return c


def test_get_returns_records_and_info_and_sends_only_the_plz():
    c = client(FakeResponse())
    data, info = c.get(fetch.LIST_KUES, "80636")
    assert len(data) == 83 and info["http_status"] == 200 and info["attempts"] == 1
    url, params, headers, timeout = c._session.calls[0]
    assert url == "https://webservice.kues.de/api/kues/partnersuche" and params == {"plz": "80636"} and timeout == fetch.TIMEOUT_S
    assert "Cookie" not in headers and "Authorization" not in headers


def test_technik_endpoint_and_km_parameter_only_when_not_the_default_radius():
    c = client(FakeResponse(), FakeResponse(), FakeResponse())
    c.get(fetch.LIST_TECHNIK, "80636")
    c.get(fetch.LIST_KUES, "80636", radius_km=50)
    c.get(fetch.LIST_KUES, "80636", radius_km=100)
    calls = c._session.calls
    assert calls[0][0].endswith("/partnersuche-kues-technik") and calls[0][1] == {"plz": "80636"}
    assert calls[1][1] == {"plz": "80636"}  # 50 km is the server default: no km parameter
    assert calls[2][1] == {"plz": "80636", "km": 100}


def test_pacing_pauses_between_requests_but_not_before_the_first():
    c = client(FakeResponse(), FakeResponse())
    c.get(fetch.LIST_KUES, "1")
    assert c.sleeps == []
    c.get(fetch.LIST_KUES, "2")
    assert len(c.sleeps) == 1 and 3 <= c.sleeps[0] <= 4


def test_timeouts_and_5xx_are_retried_with_backoff_then_succeed():
    c = client(requests.Timeout(), FakeResponse(503, text="busy"), FakeResponse())
    data, info = c.get(fetch.LIST_KUES, "80636")
    assert len(data) == 83 and info["attempts"] == 3 and c.requests_made == 3
    assert c.sleeps == [5.0, 10.0]


def test_other_request_exceptions_are_retried_too():
    c = client(requests.exceptions.ChunkedEncodingError("broken body"), FakeResponse())
    assert len(c.get(fetch.LIST_KUES, "80636")[0]) == 83 and c.requests_made == 2


def test_persistent_failure_stops_the_run():
    with pytest.raises(fetch.ApiError, match="unreachable"):
        client(requests.ConnectionError(), requests.ConnectionError(), requests.ConnectionError()).get(fetch.LIST_KUES, "1")
    with pytest.raises(fetch.ApiError, match="HTTP 500"):
        client(FakeResponse(500, text="x"), FakeResponse(500, text="x"), FakeResponse(500, text="x")).get(fetch.LIST_KUES, "1")


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_stop_immediately(status):
    c = client(FakeResponse(status, text="no"))
    with pytest.raises(fetch.RateLimited):
        c.get(fetch.LIST_KUES, "1")
    assert c.requests_made == 1


def test_one_429_waits_and_retries_but_two_in_a_row_stop():
    ok = client(FakeResponse(429, text="slow"), FakeResponse())
    assert len(ok.get(fetch.LIST_KUES, "1")[0]) == 83 and fetch.RATE_LIMIT_WAIT_S in ok.sleeps
    with pytest.raises(fetch.RateLimited, match="429"):
        client(FakeResponse(429, text="slow"), FakeResponse(429, text="slow")).get(fetch.LIST_KUES, "1")


def test_other_4xx_and_non_json_stop():
    with pytest.raises(fetch.ApiError, match="HTTP 404"):
        client(FakeResponse(404, text="gone")).get(fetch.LIST_KUES, "1")
    with pytest.raises(fetch.ApiError, match="not JSON"):
        client(FakeResponse(200, text="<html>maintenance</html>")).get(fetch.LIST_KUES, "1")


def test_request_limit_counts_every_attempt():
    c = client(FakeResponse(), FakeResponse(), FakeResponse(), max_requests=2)
    c.get(fetch.LIST_KUES, "1")
    c.get(fetch.LIST_KUES, "2")
    with pytest.raises(fetch.RequestLimitReached):
        c.get(fetch.LIST_KUES, "3")
    retried = client(requests.Timeout(), FakeResponse(), max_requests=1)
    with pytest.raises(fetch.RequestLimitReached):
        retried.get(fetch.LIST_KUES, "1")


def test_shape_check_stops_on_an_unexpected_response():
    assert fetch.check_shape([]) == []  # an empty list is valid (unknown PLZ, empty region)
    for bad in ({"error": "x"}, "text", [1, 2], [{"adressid": 1}], None):
        with pytest.raises(fetch.ApiError):
            fetch.check_shape(bad)
    with pytest.raises(fetch.ApiError, match="lack the expected keys"):
        client(FakeResponse(200, body=[{"adressid": 1, "name": "x"}])).get(fetch.LIST_KUES, "1")


def test_to_response_reads_the_server_centre_and_max_distance():
    resp = fetch.to_response(fetch.LIST_KUES, "80636", RECORDS)
    assert resp.centre == (48.150002, 11.5833) and not resp.mixed_centres
    assert resp.max_entfernung_km == pytest.approx(49.76, abs=0.01)
    empty = fetch.to_response(fetch.LIST_KUES, "00001", [])
    assert empty.centre is None and empty.max_entfernung_km is None
    mixed = [dict(RECORDS[0]), dict(RECORDS[1], inbreite="50.0")]
    assert fetch.to_response(fetch.LIST_KUES, "1", mixed).mixed_centres
