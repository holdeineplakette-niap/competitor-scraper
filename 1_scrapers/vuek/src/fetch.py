"""Polite HTTP access to vuek-gmbh.de (and, for the coordinate fallback, to OpenStreetMap Nominatim).

A blocked page (Cloudflare challenge, 403/503) is never handed to the parser: it raises `BlockedError`. Transient errors
(timeouts, connection errors, 429, 5xx) are retried at most `MAX_RETRIES` times with backoff. One request at a time,
2-4 s pause between requests (`delay_range`). Nominatim: at most 1 request per second, identifying User-Agent.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import requests

BASE = "https://www.vuek-gmbh.de"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
}
DELAY_RANGE = (5.0, 7.0)  # robots.txt of vuek-gmbh.de says "Crawl-Delay: 5": honoured although robots.txt need not be obeyed
MAX_RETRIES = 3
BACKOFF_SECONDS = (5, 15, 45)
TIMEOUT = 60

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {  # Nominatim usage policy: an identifying User-Agent with a contact
    "User-Agent": "Pruefstellen-Sourcing/0.1 (HolDeinePlakette; contact: nikos.apostolakis@holdeineplakette.de)",
    "Accept": "application/json",
    "Accept-Language": "de",
}
NOMINATIM_DELAY = 1.1  # policy: at most 1 request per second


class BlockedError(RuntimeError):
    """Cloudflare (or similar) answered instead of the site. Stop; never parse it."""


class FetchError(RuntimeError):
    """The request failed even after the allowed retries."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    elapsed_s: float
    attempts: int
    content_type: str
    cf_mitigated: str | None = None


def detect_block(status: int, headers, text: str) -> str | None:
    """Reason string if this answer is a bot-protection page, else None."""
    reasons = []
    if headers.get("cf-mitigated"):
        reasons.append(f"cf-mitigated header = {headers.get('cf-mitigated')}")
    if status in (403, 503):
        reasons.append(f"HTTP {status}")
    head = text[:200_000]
    if "Just a moment" in head:
        reasons.append("'Just a moment' page")
    if "cf-chl" in head:
        reasons.append("cf-chl marker")
    return "; ".join(reasons) or None


class VuekClient:
    """requests.Session with browser-like headers and a polite pace."""

    def __init__(self, sleep=time.sleep, clock=time.monotonic, delay_range=DELAY_RANGE):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._sleep = sleep
        self._clock = clock
        self._delay_range = delay_range
        self._last_request_at: float | None = None
        self.requests_made = 0

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        wait = random.uniform(*self._delay_range) - (self._clock() - self._last_request_at)
        if wait > 0:
            self._sleep(wait)

    def get(self, url: str, params: dict | None = None) -> Response:
        last_error = "unknown"
        for attempt in range(1, MAX_RETRIES + 2):  # first try + MAX_RETRIES retries
            self._pace()
            started = self._clock()
            try:
                r = self.session.get(url, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                self._last_request_at = self._clock()
                self.requests_made += 1
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                self._last_request_at = self._clock()
                self.requests_made += 1
                reason = detect_block(r.status_code, r.headers, r.text)
                if reason:
                    raise BlockedError(f"{r.url} was blocked ({reason})")
                if r.status_code == 429 or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                elif r.status_code != 200:
                    raise FetchError(f"{r.url} returned HTTP {r.status_code}")
                else:
                    return Response(
                        url=r.url,
                        status=r.status_code,
                        text=r.text,
                        elapsed_s=round(self._last_request_at - started, 2),
                        attempts=attempt,
                        content_type=r.headers.get("content-type", ""),
                        cf_mitigated=r.headers.get("cf-mitigated"),
                    )
            if attempt <= MAX_RETRIES:
                self._sleep(BACKOFF_SECONDS[attempt - 1])
        raise FetchError(f"{url} failed after {MAX_RETRIES} retries ({last_error})")


class NominatimClient:
    """OpenStreetMap Nominatim geocoder: 1 request per second at most, identifying User-Agent, results are ODbL data."""

    def __init__(self, sleep=time.sleep, clock=time.monotonic, delay: float = NOMINATIM_DELAY):
        self.session = requests.Session()
        self.session.headers.update(NOMINATIM_HEADERS)
        self._sleep, self._clock, self._delay = sleep, clock, delay
        self._last: float | None = None
        self.requests_made = 0

    def geocode(self, street: str, plz: str, city: str) -> dict:
        """{'lat', 'lng', 'display_name', 'raw'} for the best hit, or {'lat': None, 'lng': None, ..., 'raw': []} if none."""
        params = {k: v for k, v in {"street": street, "postalcode": plz, "city": city}.items() if v}
        params.update(country="Germany", format="jsonv2", limit=1, addressdetails=0)
        last_error = "unknown"
        for attempt in range(1, MAX_RETRIES + 2):
            if self._last is not None:
                wait = self._delay - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            try:
                r = self.session.get(NOMINATIM_URL, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                self._last = self._clock()
                self.requests_made += 1
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                self._last = self._clock()
                self.requests_made += 1
                if r.status_code == 200:
                    try:
                        hits = r.json()
                    except ValueError as exc:
                        raise FetchError(f"Nominatim answered with non-JSON: {exc}") from exc
                    if not isinstance(hits, list):
                        raise FetchError(f"Nominatim answered with an unexpected shape: {str(hits)[:200]}")
                    if not hits:
                        return {"lat": None, "lng": None, "display_name": "", "raw": []}
                    h = hits[0]
                    return {"lat": float(h["lat"]), "lng": float(h["lon"]), "display_name": h.get("display_name", ""),
                            "raw": hits}
                if r.status_code in (403, 429) or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                else:
                    raise FetchError(f"Nominatim returned HTTP {r.status_code}")
            if attempt <= MAX_RETRIES:
                self._sleep(BACKOFF_SECONDS[attempt - 1])
        raise FetchError(f"Nominatim failed after {MAX_RETRIES} retries ({last_error})")
