"""HTTP access to the HDP location API (a Firebase callable function: POST {"data":{}}).

One request per run. Transient errors (timeouts, connection errors, 429, 5xx) are retried at most `MAX_RETRIES` times with
backoff; any other non-200 answer, or an answer that is not JSON, raises `FetchError` at once. Nothing is ever parsed from a
failed answer.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

URL = "https://europe-west10-liria-ai-inspect-backend.cloudfunctions.net/getAllPublicLocations"
BODY = '{"data":{}}'  # the 11 bytes the website sends (Firebase callable format)
HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.holdeineplakette.de",
    "Referer": "https://www.holdeineplakette.de/",
    "User-Agent": "Pruefstellen-Sourcing/0.1 (HolDeinePlakette, own site list)",
    "Accept": "application/json",
}
MAX_RETRIES = 3
BACKOFF_SECONDS = (5, 15, 45)
TIMEOUT = 60


class FetchError(RuntimeError):
    """The request failed even after the allowed retries, or the answer was not usable JSON."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    elapsed_s: float
    attempts: int
    content_type: str


class HdpClient:
    """requests.Session that POSTs the callable body; `sleep` and `clock` are injectable so tests never wait."""

    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._sleep, self._clock = sleep, clock
        self.requests_made = 0

    def post_locations(self) -> Response:
        last_error = "unknown"
        for attempt in range(1, MAX_RETRIES + 2):  # first try + MAX_RETRIES retries
            started = self._clock()
            try:
                r = self.session.post(URL, data=BODY, timeout=TIMEOUT)
            except requests.RequestException as exc:
                self.requests_made += 1
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                self.requests_made += 1
                if r.status_code == 429 or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                elif r.status_code != 200:
                    raise FetchError(f"{URL} returned HTTP {r.status_code}")
                else:
                    try:
                        json.loads(r.text)
                    except ValueError as exc:
                        raise FetchError(f"{URL} answered with non-JSON content: {exc}") from exc
                    return Response(url=URL, status=r.status_code, text=r.text, elapsed_s=round(self._clock() - started, 2),
                                    attempts=attempt, content_type=r.headers.get("content-type", ""))
            if attempt <= MAX_RETRIES:
                self._sleep(BACKOFF_SECONDS[attempt - 1])
        raise FetchError(f"{URL} failed after {MAX_RETRIES} retries ({last_error})")
