"""KÜS fetch layer: the HTTP client, the response model and the response-shape check. No parsing of fields here.

The finder behind kues.de/standortsuche/ is a plain JSON API on webservice.kues.de (recon 2026-10-05, see
0_instructions/sources/kues.md):

    GET https://webservice.kues.de/api/kues/partnersuche?plz=<PLZ>               every Partner within 50 km
    GET https://webservice.kues.de/api/kues/partnersuche-kues-technik?plz=<PLZ>  the subset that does EBE appraisals

No key, no cookies, no special headers. The PLZ is the only query input (coordinates are ignored). An optional `km`
parameter sets the radius (capped at 100 km); the web page never sends it, so the adapter does not either unless asked.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

import requests

from core.grid import StopRun

BASE_URL = "https://webservice.kues.de/api/kues"
LIST_KUES = "partnersuche"
LIST_TECHNIK = "kues_technik"
ENDPOINTS = {LIST_KUES: "partnersuche", LIST_TECHNIK: "partnersuche-kues-technik"}
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}  # recon: the API also answers with no headers of our own at all

DEFAULT_RADIUS_KM = 50  # what the web page gets
RADIUS_CAP_KM = 100  # the server's own cap on the `km` parameter (recon: km=200 returned 99.8 km)

PACE_S = 3.0  # between requests, plus 0-JITTER_S random
JITTER_S = 1.0
TIMEOUT_S = 30
MAX_ATTEMPTS = 3  # timeouts, connection errors, 5xx
BACKOFF_S = 5.0  # 5 s, then 10 s
RATE_LIMIT_WAIT_S = 60  # on the first 429; a second 429 in a row stops the run
FULL_MAX_REQUESTS = 900  # hard ceiling of live requests per full run: ~341 planned + <= 100 fill-in queries, 2 lists each = <= 882

# Keys every record must carry; a response without them is an unexpected shape and stops the run.
REQUIRED_KEYS = (
    "adressid", "lizenznehmer", "name", "plz", "ort", "strasse", "lat", "lon", "inbreite", "inlaenge",
    "pruefungvorortmoeglich", "keinestrasse", "entfernung", "plzorder",
)


class ApiError(StopRun):
    """HTTP error, timeout, non-JSON answer or unexpected response shape."""


class RequestLimitReached(StopRun):
    """The hard request limit of the run was reached."""


class RateLimited(StopRun):
    """HTTP 429 twice in a row, or 401/403: the server does not want this traffic."""


@dataclass
class Response:
    """One answered query: the records of one list for one PLZ."""

    list_name: str  # LIST_KUES or LIST_TECHNIK
    zip: str
    records: list[dict]
    http_status: int | None = 200
    bytes: int | None = None
    elapsed_s: float | None = None
    raw_file: str | None = None
    centre: tuple[float, float] | None = None  # the point the server searched around (inbreite/inlaenge)
    mixed_centres: bool = False  # records disagree about the centre (should never happen)
    max_entfernung_km: float | None = None
    problems: list[str] = field(default_factory=list)


def check_shape(data) -> list[dict]:
    """The record list of a response. Stops the run on anything but a JSON array of records with the expected keys.
    An empty array is valid: it is what the server answers for a PLZ it does not know or an empty region."""
    if not isinstance(data, list):
        raise ApiError(f"unexpected response shape (not a list): {str(data)[:200]!r}")
    for rec in data:
        if not isinstance(rec, dict):
            raise ApiError(f"unexpected record shape: {str(rec)[:200]!r}")
        missing = [k for k in REQUIRED_KEYS if k not in rec]
        if missing:
            raise ApiError(f"records lack the expected keys {missing}; keys seen: {sorted(rec)}")
    return data


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_response(list_name: str, plz: str, data: list[dict], info: dict | None = None, raw_file: str | None = None) -> Response:
    """Wrap a checked record list: centre, largest distance, and whether all records agree on the centre."""
    info = info or {}
    centres = {(_float(r.get("inbreite")), _float(r.get("inlaenge"))) for r in data}
    centres.discard((None, None))
    ent = [e for e in (_float(r.get("entfernung")) for r in data) if e is not None]
    return Response(
        list_name=list_name,
        zip=plz,
        records=data,
        http_status=info.get("http_status", 200),
        bytes=info.get("bytes"),
        elapsed_s=info.get("elapsed_s"),
        raw_file=raw_file,
        centre=next(iter(centres)) if len(centres) == 1 else (sorted(centres)[0] if centres else None),
        mixed_centres=len(centres) > 1,
        max_entfernung_km=max(ent) if ent else None,
    )


class KuesClient:
    """Sequential GET client: PACE_S + jitter between requests, retries with backoff, hard request limit.

    Every HTTP attempt counts against max_requests. Stops (raises a StopRun) on 401/403, on a second 429 in a row,
    on a non-JSON answer, on any other 4xx, when 5xx/timeouts persist after MAX_ATTEMPTS, and at the request limit.
    """

    def __init__(
        self,
        pace_s: float = PACE_S,
        jitter_s: float = JITTER_S,
        timeout_s: float = TIMEOUT_S,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_s: float = BACKOFF_S,
        rate_limit_wait_s: float = RATE_LIMIT_WAIT_S,
        max_requests: int | None = FULL_MAX_REQUESTS,
        sleep=time.sleep,
        session=None,
    ):
        self.pace_s, self.jitter_s, self.timeout_s = pace_s, jitter_s, timeout_s
        self.max_attempts, self.backoff_s, self.rate_limit_wait_s = max_attempts, backoff_s, rate_limit_wait_s
        self.max_requests = max_requests
        self._sleep = sleep
        self._session = session or requests
        self.requests_made = 0
        self._first = True

    def _pause(self) -> None:
        self._sleep(self.pace_s + random.uniform(0, self.jitter_s))

    def get(self, list_name: str, plz: str, radius_km: int | None = None) -> tuple[list[dict], dict]:
        """One query. radius_km None = no `km` parameter (the server's default, 50 km)."""
        url = f"{BASE_URL}/{ENDPOINTS[list_name]}"
        params = {"plz": plz}
        if radius_km is not None and radius_km != DEFAULT_RADIUS_KM:
            params["km"] = int(radius_km)
        if not self._first:
            self._pause()
        self._first = False
        rate_limited = 0
        for attempt in range(1, self.max_attempts + 1):
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise RequestLimitReached(f"request limit of {self.max_requests} reached")
            self.requests_made += 1
            t0 = time.time()
            try:
                resp = self._session.get(url, params=params, headers=HEADERS, timeout=self.timeout_s)
            except requests.RequestException as exc:  # timeouts, connection resets, broken chunked bodies, ...
                if attempt == self.max_attempts:
                    raise ApiError(f"KÜS API unreachable after {attempt} attempts: {type(exc).__name__}") from exc
                self._sleep(self.backoff_s * attempt)
                continue
            if resp.status_code in (401, 403):
                raise RateLimited(f"HTTP {resp.status_code}: the API refuses the request ({resp.text[:200]!r})")
            if resp.status_code == 429:
                rate_limited += 1
                if rate_limited >= 2:
                    raise RateLimited("HTTP 429 twice in a row")
                self._sleep(self.rate_limit_wait_s)
                continue
            if resp.status_code >= 500:
                if attempt == self.max_attempts:
                    raise ApiError(f"HTTP {resp.status_code} after {attempt} attempts: {resp.text[:200]!r}")
                self._sleep(self.backoff_s * attempt)
                continue
            if resp.status_code >= 400:
                raise ApiError(f"HTTP {resp.status_code} for plz={plz}: {resp.text[:200]!r}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise ApiError(f"HTTP {resp.status_code} but the body is not JSON: {resp.text[:200]!r}") from exc
            info = {
                "http_status": resp.status_code,
                "bytes": len(resp.content),
                "elapsed_s": round(time.time() - t0, 2),
                "attempts": attempt,
            }
            return check_shape(data), info
        raise ApiError("no answer")  # unreachable: every loop path returns or raises
