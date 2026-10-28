"""Offline tests for vuek.fetch: pacing, retries with backoff, block detection, Nominatim policy. No network, no real sleeping."""

from __future__ import annotations

import pytest
import requests

from vuek import fetch


class FakeResp:
    def __init__(self, status=200, text="<html>ok</html>", headers=None, url="https://www.vuek-gmbh.de/x/", payload=None):
        self.status_code, self.text, self.headers, self.url, self._payload = status, text, headers or {}, url, payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Clock:
    """A clock that only advances when the code 'sleeps'."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.now += s


def client(responses, delay_range=(5.0, 5.0)):
    clock = Clock()
    c = fetch.VuekClient(sleep=clock.sleep, clock=clock.time, delay_range=delay_range)
    queue = list(responses)

    def get(url, params=None, timeout=None):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    c.session.get = get
    return c, clock


def test_default_pace_honours_the_site_crawl_delay():
    assert fetch.DELAY_RANGE[0] >= 5.0  # robots.txt: Crawl-Delay 5


def test_requests_are_paced_one_at_a_time():
    c, clock = client([FakeResp(), FakeResp(), FakeResp()])
    for _ in range(3):
        c.get("https://www.vuek-gmbh.de/x/")
    assert clock.slept == [5.0, 5.0] and c.requests_made == 3  # no wait before the first request


def test_transient_errors_are_retried_with_backoff_then_succeed():
    c, clock = client([FakeResp(500), requests.ConnectionError("boom"), FakeResp(429), FakeResp(200, "fine")], delay_range=(0.0, 0.0))
    r = c.get("https://www.vuek-gmbh.de/x/")
    assert r.text == "fine" and r.attempts == 4 and c.requests_made == 4
    assert clock.slept == [5, 15, 45]


def test_gives_up_after_the_allowed_retries():
    c, clock = client([FakeResp(500)] * 4, delay_range=(0.0, 0.0))
    with pytest.raises(fetch.FetchError, match="failed after 3 retries"):
        c.get("https://www.vuek-gmbh.de/x/")
    assert c.requests_made == 4


@pytest.mark.parametrize("resp", [
    FakeResp(403, "Forbidden"),
    FakeResp(200, "<title>Just a moment...</title>"),
    FakeResp(200, "<html>cf-chl-bypass</html>"),
    FakeResp(200, "ok", headers={"cf-mitigated": "challenge"}),
])
def test_a_bot_protection_page_stops_at_once_and_is_never_returned(resp):
    c, clock = client([resp, FakeResp()], delay_range=(0.0, 0.0))
    with pytest.raises(fetch.BlockedError):
        c.get("https://www.vuek-gmbh.de/x/")
    assert c.requests_made == 1 and clock.slept == []  # no retry on a block


def test_a_missing_page_is_an_error_not_an_empty_result():
    c, _ = client([FakeResp(404, "Not found")])
    with pytest.raises(fetch.FetchError, match="HTTP 404"):
        c.get("https://www.vuek-gmbh.de/x/")
    assert c.requests_made == 1


# ------------------------------------------------------------------------------------------------ Nominatim
def nominatim(responses):
    clock = Clock()
    n = fetch.NominatimClient(sleep=clock.sleep, clock=clock.time)
    queue, seen = list(responses), []

    def get(url, params=None, timeout=None):
        seen.append((url, dict(params)))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    n.session.get = get
    return n, clock, seen


def hit(lat="51.99", lon="8.79", name="Musterweg 1, Lage, 32791, Deutschland"):
    return FakeResp(200, payload=[{"lat": lat, "lon": lon, "display_name": name}])


def test_nominatim_policy_user_agent_and_rate():
    n, clock, seen = nominatim([hit(), hit(), hit()])
    for _ in range(3):
        n.geocode("Musterweg 1", "32791", "Lage")
    assert all(s >= 1.0 for s in clock.slept) and len(clock.slept) == 2  # at most one request per second
    ua = n.session.headers["User-Agent"]
    assert "Pruefstellen-Sourcing" in ua and "@" in ua  # identifies the application and gives a contact
    assert seen[0][0] == fetch.NOMINATIM_URL
    assert seen[0][1] == {"street": "Musterweg 1", "postalcode": "32791", "city": "Lage", "country": "Germany",
                          "format": "jsonv2", "limit": 1, "addressdetails": 0}


def test_nominatim_result_empty_result_and_empty_parameters():
    n, _, seen = nominatim([hit("52.1", "8.6"), FakeResp(200, payload=[])])
    r = n.geocode("Musterweg 1", "32791", "Lage")
    assert (r["lat"], r["lng"]) == (52.1, 8.6) and r["display_name"].startswith("Musterweg") and len(r["raw"]) == 1
    r = n.geocode("Musterweg 1", "32791", "")
    assert r["lat"] is None and r["raw"] == [] and "city" not in seen[1][1]


def test_nominatim_retries_on_429_and_rejects_odd_answers():
    n, clock, _ = nominatim([FakeResp(429), hit()])
    assert n.geocode("a", "32791", "Lage")["lat"] == 51.99 and 5 in clock.slept
    with pytest.raises(fetch.FetchError, match="unexpected shape"):
        nominatim([FakeResp(200, payload={"error": "x"})])[0].geocode("a", "1", "b")
    with pytest.raises(fetch.FetchError, match="non-JSON"):
        nominatim([FakeResp(200, payload=ValueError("bad"))])[0].geocode("a", "1", "b")
    with pytest.raises(fetch.FetchError, match="HTTP 400"):
        nominatim([FakeResp(400)])[0].geocode("a", "1", "b")
