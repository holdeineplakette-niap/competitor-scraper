"""OpenStreetMap Nominatim geocoding for the master: only for rows that still have no usable coordinates.

Rules: structured query (street, PLZ, city) with the VÜK fallbacks, at least 1.1 s between live requests, an
identifying User-Agent, a hard cap of live requests per run, and a persistent cache so reruns make no requests.
Results are © OpenStreetMap contributors (ODbL). The caller decides whether a result is acceptable.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

from core import normalise as nm

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "HDP"  # Nikos, 2026-10-06: send only this; NOMINATIM_CONTACT in .env is appended if set
DELAY_SECONDS = 1.1
REQUEST_CAP = 60
TIMEOUT = 60
NEGATIVE_TTL_DAYS = 30
BACKOFF_SECONDS = (5, 15, 45)
ATTRIBUTION = "Geocoding: © OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright), via Nominatim."


class GeocodeError(Exception):
    pass


def contact_from_env(root=None) -> str:
    """Optional NOMINATIM_CONTACT from .env (or the environment); empty if not set."""
    from dotenv import dotenv_values
    from core import paths

    value = dotenv_values(paths.project_root() / ".env" if root is None else Path(root) / ".env").get("NOMINATIM_CONTACT")
    return (value or os.environ.get("NOMINATIM_CONTACT") or "").strip()


def attempts(street: str, plz: str, city: str) -> list[tuple[str, str, str]]:
    """Query variants in order: full, city without district suffix, no city."""
    base = city.split("-")[0].strip() if city else ""
    out = [(street, plz, city)]
    if base and base != city:
        out.append((street, plz, base))
    out.append((street, plz, ""))
    return list(dict.fromkeys(out))


def cache_key(street: str, plz: str, city: str) -> str:
    return f"{nm.street_key(street)}@{plz}@{nm.city_key(city)}"


class Geocoder:
    """Cached, rate-limited, capped Nominatim client. `lookup` makes live requests only on a cache miss."""

    def __init__(self, cache_path: Path, session=None, contact: str = "", cap: int = REQUEST_CAP,
                 sleep=time.sleep, clock=time.monotonic, backoff=BACKOFF_SECONDS, live: bool = True):
        self.cache_path = Path(cache_path)
        self.session = session or requests.Session()
        ua = f"{USER_AGENT} ({contact})" if contact else USER_AGENT
        self.headers = {"User-Agent": ua, "Accept": "application/json", "Accept-Language": "de"}
        self.cap = cap
        self.sleep, self.clock, self.backoff, self.live = sleep, clock, backoff, live
        self.requests_made = 0
        self.cache_hits = 0
        self.capped = False
        self.blocked = False
        self.errors: list[str] = []
        self._last = None
        self._cache: dict[str, dict] = {}
        self._new: list[dict] = []
        self.last_rejected: list[dict] = []  # hits of the last lookup that the caller's accept() turned down
        if self.cache_path.exists():
            with open(self.cache_path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, start=1):
                    if line.strip():
                        try:
                            rec = json.loads(line)
                            self._cache[rec["key"]] = rec
                        except (ValueError, KeyError):  # a killed run can leave a truncated last line: skip it, say so
                            self.errors.append(f"cache line {n} is unreadable and was skipped")

    # -- one structured query (cached) -------------------------------------------------------------------------
    def query(self, street: str, plz: str, city: str) -> dict | None:
        """The first hit {lat, lng, display_name} or None for 'no result'. Raises GeocodeError if it cannot be asked."""
        key = cache_key(street, plz, city)
        cached = self._cache.get(key)
        if cached is not None and not (cached["result"] is None and self._stale(cached)):
            self.cache_hits += 1
            return cached["result"]
        if not self.live or self.blocked:
            raise GeocodeError("no live request allowed (offline or blocked) and no cache entry")
        if self.requests_made >= self.cap:
            self.capped = True
            raise GeocodeError(f"request cap of {self.cap} reached")
        params = {"street": street, "postalcode": plz, "city": city, "country": "Germany", "format": "jsonv2",
                  "limit": 1, "addressdetails": 0}
        params = {k: v for k, v in params.items() if v != ""}
        data = self._get(params)
        result = None
        if data:
            h = data[0]
            result = {"lat": float(h["lat"]), "lng": float(h["lon"]), "display_name": h.get("display_name", "")}
        rec = {"key": key, "query": {"street": street, "postalcode": plz, "city": city}, "result": result,
               "fetched_at": datetime.now().isoformat(timespec="seconds")}
        self._cache[key] = rec
        self._new.append(rec)
        return result

    @staticmethod
    def _stale(rec: dict) -> bool:
        """A 'no result' answer is asked again after NEGATIVE_TTL_DAYS (OSM data improves); a hit stays."""
        try:
            age = datetime.now() - datetime.fromisoformat(rec["fetched_at"])
        except (KeyError, ValueError):
            return True
        return age.days >= NEGATIVE_TTL_DAYS

    def _get(self, params: dict) -> list:
        last = ""
        for attempt in range(len(self.backoff) + 1):
            if self.requests_made >= self.cap:  # the cap is hard, retries included
                self.capped = True
                raise GeocodeError(f"request cap of {self.cap} reached")
            if self._last is not None:
                wait = DELAY_SECONDS - (self.clock() - self._last)
                if wait > 0:
                    self.sleep(wait)
            self._last = self.clock()
            self.requests_made += 1
            try:
                resp = self.session.get(NOMINATIM_URL, params=params, headers=self.headers, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last = f"request error: {exc}"
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise GeocodeError(f"non-JSON answer: {exc}") from exc
                    if not isinstance(data, list):
                        raise GeocodeError("unexpected answer shape (not a list)")
                    return data
                last = f"HTTP {resp.status_code}"
                if resp.status_code not in (403, 429, 500, 502, 503, 504):
                    raise GeocodeError(last)
                if resp.status_code in (403, 429) and attempt >= 1:
                    self.blocked = True
                    raise GeocodeError(f"{last}: stopping all geocoding for this run")
            if attempt < len(self.backoff):
                self.sleep(self.backoff[attempt])
            if self.requests_made >= self.cap:
                self.capped = True
                break
        raise GeocodeError(last or "failed")

    # -- the master's call: try the variants until one is accepted -----------------------------------------------------
    def lookup(self, street: str, plz: str, city: str, accept) -> tuple[dict | None, str]:
        """(result, note). `accept(result)` says whether a hit is usable; the first accepted variant wins."""
        self.last_rejected = []
        if not street or not plz:
            return None, "no street or PLZ to ask for"
        notes = []
        for s, p, c in attempts(street, plz, city):
            try:
                hit = self.query(s, p, c)
            except GeocodeError as exc:
                self.errors.append(f"{s}, {p} {c}: {exc}")
                notes.append(str(exc))
                if self.capped or self.blocked:
                    break
                continue
            if hit is None:
                notes.append(f"no result for '{c}'")
            elif accept(hit):
                return hit, "ok"
            else:
                self.last_rejected.append(hit)
                notes.append(f"result rejected ({hit['lat']:.4f},{hit['lng']:.4f})")
        return None, "; ".join(notes) or "no result"

    def save(self) -> int:
        """Append the new cache entries; returns how many were written."""
        if not self._new:
            return 0
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path, "a", encoding="utf-8", newline="\n") as fh:
            for rec in self._new:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n = len(self._new)
        self._new = []
        return n
