import json

import pytest

from core import geocode


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = [] if payload is None else payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        r = self.responses.pop(0) if self.responses else FakeResponse(200, [])
        if isinstance(r, Exception):
            raise r
        return r


def hit(lat, lon, name="x"):
    return FakeResponse(200, [{"lat": str(lat), "lon": str(lon), "display_name": name}])


def make(tmp_path, responses, **kw):
    clock = {"t": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock["t"] += s

    g = geocode.Geocoder(tmp_path / "cache" / "n.jsonl", session=FakeSession(responses), sleep=sleep, clock=lambda: clock["t"], **kw)
    return g, sleeps


ok = lambda h: True  # noqa: E731


def test_user_agent_is_just_hdp_and_contact_is_optional(tmp_path):
    g, _ = make(tmp_path, [hit(51, 13)])
    g.lookup("Hauptstr. 5", "01067", "Dresden", ok)
    assert g.session.calls[0]["headers"]["User-Agent"] == "HDP"
    g2, _ = make(tmp_path / "b", [hit(51, 13)], contact="a@b.c")
    g2.lookup("Hauptstr. 5", "01067", "Dresden", ok)
    assert g2.session.calls[0]["headers"]["User-Agent"] == "HDP (a@b.c)"


def test_structured_query_and_delay_between_live_requests(tmp_path):
    g, sleeps = make(tmp_path, [hit(51, 13), hit(52, 14)])
    g.lookup("Hauptstr. 5", "01067", "Dresden", ok)
    g.lookup("Nebenstr. 1", "10115", "Berlin", ok)
    p = g.session.calls[0]["params"]
    assert p["street"] == "Hauptstr. 5" and p["postalcode"] == "01067" and p["city"] == "Dresden" and p["country"] == "Germany"
    assert g.requests_made == 2 and any(s >= 1.0 for s in sleeps)


def test_cache_makes_reruns_free_and_negative_results_are_cached(tmp_path):
    g, _ = make(tmp_path, [hit(51, 13), FakeResponse(200, [])])
    assert g.lookup("Hauptstr. 5", "01067", "Dresden", ok)[0]["lat"] == 51.0
    assert g.lookup("Gibtesnicht 1", "10115", "Berlin", ok)[0] is None
    assert g.save() >= 2
    g2, _ = make(tmp_path, [])
    assert g2.lookup("Hauptstraße 5", "01067", "Dresden", ok)[0]["lng"] == 13.0  # spelled differently, same normalised key
    assert g2.lookup("Gibtesnicht 1", "10115", "Berlin", ok)[0] is None
    assert g2.requests_made == 0 and g2.cache_hits >= 2
    lines = (tmp_path / "cache" / "n.jsonl").read_text(encoding="utf-8").splitlines()
    assert all(json.loads(l)["key"] for l in lines)


def test_hard_cap_of_live_requests(tmp_path):
    g, _ = make(tmp_path, [hit(51, 13)] * 5, cap=2)
    for i in range(4):
        g.lookup(f"Str. {i}", "01067", "Dresden", ok)
    assert g.requests_made == 2 and g.capped
    assert g.lookup("Str. 9", "01067", "Dresden", ok)[0] is None


def test_fallbacks_without_district_and_without_city(tmp_path):
    g, _ = make(tmp_path, [FakeResponse(200, []), FakeResponse(200, []), hit(52, 8)])
    h, note = g.lookup("Driburger Str. 1", "33647", "Bielefeld-Brackwede", ok)
    assert h["lat"] == 52.0
    cities = [c["params"].get("city", "") for c in g.session.calls]
    assert cities == ["Bielefeld-Brackwede", "Bielefeld", ""]


def test_a_result_the_caller_rejects_tries_the_next_variant_and_may_end_unresolved(tmp_path):
    g, _ = make(tmp_path, [hit(10, 10), hit(10, 10), hit(10, 10)])
    h, note = g.lookup("Driburger Str. 1", "33647", "Bielefeld-Brackwede", lambda r: False)
    assert h is None and "rejected" in note


def test_no_street_or_plz_means_no_request(tmp_path):
    g, _ = make(tmp_path, [])
    assert g.lookup("", "01067", "Dresden", ok)[0] is None and g.lookup("A 1", "", "Dresden", ok)[0] is None
    assert g.requests_made == 0


def test_offline_mode_never_requests(tmp_path):
    g, _ = make(tmp_path, [hit(51, 13)], live=False)
    assert g.lookup("Hauptstr. 5", "01067", "Dresden", ok)[0] is None
    assert g.requests_made == 0 and g.errors


def test_repeated_429_blocks_the_rest_of_the_run(tmp_path):
    g, _ = make(tmp_path, [FakeResponse(429), FakeResponse(429), FakeResponse(429)])
    assert g.lookup("Hauptstr. 5", "01067", "Dresden", ok)[0] is None
    assert g.blocked
    assert g.lookup("Nebenstr. 1", "10115", "Berlin", ok)[0] is None
    assert g.requests_made <= 3


def test_non_json_answer_is_an_error_not_a_result(tmp_path):
    g, _ = make(tmp_path, [FakeResponse(200, ValueError("html"))])
    assert g.lookup("Hauptstr. 5", "01067", "Dresden", ok)[0] is None and g.errors


def test_attribution_names_osm_and_odbl():
    assert "OpenStreetMap" in geocode.ATTRIBUTION and "ODbL" in geocode.ATTRIBUTION


def test_an_old_negative_answer_is_asked_again_a_fresh_one_and_any_hit_are_not(tmp_path):
    from datetime import datetime, timedelta
    p = tmp_path / "cache" / "n.jsonl"
    p.parent.mkdir()
    key_old, key_new, key_hit = (geocode.cache_key(s, "01067", "Dresden") for s in ("Alt 1", "Neu 1", "Treffer 1"))
    old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    new = datetime.now().isoformat(timespec="seconds")
    p.write_text("\n".join(json.dumps(r) for r in [
        {"key": key_old, "query": {}, "result": None, "fetched_at": old},
        {"key": key_new, "query": {}, "result": None, "fetched_at": new},
        {"key": key_hit, "query": {}, "result": {"lat": 51.0, "lng": 13.0, "display_name": "x"}, "fetched_at": old}]) + "\n", encoding="utf-8")
    g = geocode.Geocoder(p, session=FakeSession([hit(51.1, 13.1)]), sleep=lambda s: None)
    assert g.query("Alt 1", "01067", "Dresden")["lat"] == 51.1 and g.requests_made == 1
    assert g.query("Neu 1", "01067", "Dresden") is None and g.query("Treffer 1", "01067", "Dresden")["lat"] == 51.0
    assert g.requests_made == 1


def test_last_rejected_hits_are_available_to_the_caller(tmp_path):
    g, _ = make(tmp_path, [hit(10, 10)])
    g.lookup("Hauptstr. 5", "01067", "Dresden", lambda r: False)
    assert [h["lat"] for h in g.last_rejected] == [10.0]
