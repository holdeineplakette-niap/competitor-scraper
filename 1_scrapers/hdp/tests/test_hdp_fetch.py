"""Offline tests for hdp.fetch: the callable body, retries with backoff, and that a bad answer never reaches the parser."""

from __future__ import annotations

import pytest
import requests

from hdp import fetch


class FakeResp:
    def __init__(self, status=200, text='{"result":{"success":true,"locations":[]}}', headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {"content-type": "application/json"}


def client(responses):
    sleeps = []
    c = fetch.HdpClient(sleep=sleeps.append)
    queue, sent = list(responses), []

    def post(url, data=None, timeout=None):
        sent.append((url, data, timeout))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    c.session.post = post
    return c, sleeps, sent


def test_the_body_is_the_11_byte_callable_envelope_and_the_headers_match_the_website():
    c, _, sent = client([FakeResp()])
    c.post_locations()
    url, data, _ = sent[0]
    assert url == fetch.URL and data == '{"data":{}}' and len(data.encode()) == 11
    h = c.session.headers
    assert h["Content-Type"] == "application/json" and h["Origin"] == "https://www.holdeineplakette.de"
    assert h["Referer"] == "https://www.holdeineplakette.de/"


def test_a_transient_error_is_retried_with_backoff_and_then_succeeds():
    c, sleeps, _ = client([FakeResp(503), requests.ConnectionError("down"), FakeResp(429), FakeResp()])
    r = c.post_locations()
    assert r.status == 200 and r.attempts == 4 and sleeps == [5, 15, 45] and c.requests_made == 4


def test_it_fails_after_the_allowed_retries():
    c, sleeps, _ = client([FakeResp(500)] * 4)
    with pytest.raises(fetch.FetchError, match="after 3 retries"):
        c.post_locations()
    assert c.requests_made == 4 and sleeps == [5, 15, 45]


def test_a_client_error_is_not_retried():
    c, sleeps, _ = client([FakeResp(403)])
    with pytest.raises(fetch.FetchError, match="HTTP 403"):
        c.post_locations()
    assert c.requests_made == 1 and sleeps == []


def test_an_answer_that_is_not_json_raises_instead_of_being_parsed():
    c, _, _ = client([FakeResp(text="<html>Just a moment</html>")])
    with pytest.raises(fetch.FetchError, match="non-JSON"):
        c.post_locations()
