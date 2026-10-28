"""Polite plain-HTTP access to tuev-nord.de (TYPO3 behind a Citrix NetScaler, no Cloudflare).

A fresh `requests.Session` per run that **accepts no cookies at all** (the NetScaler's NSC_ cookie is noted by name only),
browser-like headers, one request at a time, 2-4 s apart. A blocked or error page is never handed to the parser as "0 stations":
it raises `BlockedError` / `FetchError`. Playwright is not used: plain HTTP works (see the run report).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from http.cookiejar import DefaultCookiePolicy

import requests

from tuev_nord.parse import BASE, LIST_PATH, STATIONS_PATH

STATIONS_URL = BASE + STATIONS_PATH
LIST_URL = BASE + LIST_PATH
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
}
DELAY_RANGE = (2.0, 4.0)
MAX_RETRIES = 3
BACKOFF_SECONDS = (5, 15, 45)
TIMEOUT = 90
MAX_EXTRA_PAGES = 8  # detail pages fetched only to explain stations that are in one source and not the other
BLOCK_MARKERS = ("Just a moment", "cf-chl", "Access Denied", "Request Rejected", "Zugriff verweigert")


class FetchError(RuntimeError):
    """The request failed even after the allowed retries (or answered with an unexpected status)."""

    def __init__(self, message: str, outcome=None):
        super().__init__(message)
        self.outcome = outcome


class BlockedError(FetchError):
    """A bot-protection or WAF page answered instead of the site. Stop; never parse as '0 results'."""


def detect_block(status: int, headers, text: str) -> str | None:
    reasons = []
    if headers.get("cf-mitigated"):
        reasons.append(f"cf-mitigated header = {headers.get('cf-mitigated')}")
    if status in (403, 503):
        reasons.append(f"HTTP {status}")
    head = text[:200_000]
    reasons += [f"{m!r} in the page" for m in BLOCK_MARKERS if m in head and len(text) < 100_000]
    return "; ".join(reasons) or None


@dataclass
class Response:
    url: str
    status: int
    text: str
    content: bytes
    elapsed_s: float
    attempts: int
    content_type: str
    set_cookie_names: list = field(default_factory=list)
    server: str = ""


class NordClient:
    def __init__(self, sleep=time.sleep, clock=time.monotonic, delay_range=DELAY_RANGE):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))  # fresh session, no cookies kept or sent
        self._sleep, self._clock, self._delay_range = sleep, clock, delay_range
        self._last_request_at: float | None = None
        self.requests_made = 0
        self.log: list[dict] = []

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        wait = random.uniform(*self._delay_range) - (self._clock() - self._last_request_at)
        if wait > 0:
            self._sleep(wait)

    def get(self, url: str, what: str, ok_statuses: tuple = (200,)) -> Response:
        """GET with retries. `ok_statuses` are returned to the caller (e.g. 404 for a detail page); anything else raises."""
        last_error = "unknown"
        for attempt in range(1, MAX_RETRIES + 2):
            self._pace()
            started = self._clock()
            try:
                r = self.session.get(url, timeout=TIMEOUT)
            except requests.RequestException as exc:
                self._last_request_at = self._clock()
                self.requests_made += 1
                last_error = f"{type(exc).__name__}: {exc}"
                self.log.append({"time": time.strftime("%H:%M:%S"), "what": what, "url": url, "status": "", "seconds": "", "note": last_error})
            else:
                self._last_request_at = self._clock()
                self.requests_made += 1
                seconds = round(self._last_request_at - started, 2)
                self.log.append({"time": time.strftime("%H:%M:%S"), "what": what, "url": url, "status": r.status_code, "seconds": seconds,
                                 "note": f"{len(r.content)} bytes, attempt {attempt}"})
                reason = detect_block(r.status_code, r.headers, r.text)
                if reason:
                    raise BlockedError(f"{r.url} was blocked ({reason})")
                if r.status_code in ok_statuses:
                    raw_headers = getattr(r.raw, "headers", None)
                    names = sorted({c.split("=", 1)[0].strip() for c in raw_headers.getlist("Set-Cookie")}) if hasattr(raw_headers, "getlist") else []
                    return Response(url=r.url, status=r.status_code, text=r.text, content=r.content, elapsed_s=seconds, attempts=attempt,
                                    content_type=r.headers.get("content-type", ""), set_cookie_names=names, server=r.headers.get("server", ""))
                if r.status_code == 429 or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                else:
                    raise FetchError(f"{r.url} returned HTTP {r.status_code}")
            if attempt <= MAX_RETRIES:
                self._sleep(BACKOFF_SECONDS[attempt - 1])
        raise FetchError(f"{url} failed after {MAX_RETRIES} retries ({last_error})")


@dataclass
class FetchOutcome:
    stations: Response | None = None
    list_page: Response | None = None
    details: list = field(default_factory=list)  # {"role", "url", "status", "html", "elapsed_s"}
    extra: list = field(default_factory=list)  # same shape: pages fetched to explain one-source-only stations
    log: list = field(default_factory=list)
    access: dict = field(default_factory=dict)
    requests_made: int = 0


def _page(client: NordClient, role: str, url: str) -> dict:
    r = client.get(url, f"detail page ({role})", ok_statuses=(200, 404))
    return {"role": role, "url": url, "status": r.status, "html": r.text, "elapsed_s": r.elapsed_s}


def fetch_all(client: NordClient | None = None, pick_details=None, pick_extra=None) -> FetchOutcome:
    """Stations page, list page, then the detail pages chosen by `pick_details(outcome)` and `pick_extra(outcome)`.

    Both pickers return [(role, absolute_url), ...]; they see the pages already fetched. On any failure the partly filled
    outcome is attached to the raised `FetchError` (so the run can still write what it has and say what went wrong).
    """
    client = client or NordClient()
    out = FetchOutcome()
    out.access = {"method": "requests.Session (plain HTTP)", "playwright": False, "cookies_accepted": False,
                  "user_agent": HEADERS["User-Agent"], "delay_s": list(DELAY_RANGE), "set_cookie_names": [], "server_header": ""}
    try:
        out.stations = client.get(STATIONS_URL, "stations page")
        out.list_page = client.get(LIST_URL, "list page")
        for r in (out.stations, out.list_page):
            out.access["set_cookie_names"] = sorted(set(out.access["set_cookie_names"]) | set(r.set_cookie_names))
            out.access["server_header"] = out.access["server_header"] or r.server
        for role, url in (pick_details(out) if pick_details else []):
            out.details.append(_page(client, role, url))
        for role, url in (pick_extra(out) if pick_extra else [])[:MAX_EXTRA_PAGES]:
            out.extra.append(_page(client, role, url))
    except FetchError as exc:
        exc.outcome = out
        raise
    finally:
        out.log, out.requests_made = client.log, client.requests_made
    return out
