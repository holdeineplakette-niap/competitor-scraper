"""Pass/fail checks of the HDP adapter. Pure functions, no network.

A `hard` check fails the run (the CSV is then written as `unverified`, or not at all when the answer holds no locations).
`review` is listed prominently but does not fail the run. The site count is deliberately NOT a pass condition: a count that
differs from the previous run is only a warning.
"""

from __future__ import annotations

from collections import Counter

from core import geo
from hdp import parse


def check(name: str, ok: bool, detail: str, severity: str = "hard") -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": severity}


def response_checks(payload: dict | None) -> list[dict]:
    """The envelope: result.success is true and result.locations is a non-empty list."""
    success, locations = parse.envelope(payload) if isinstance(payload, dict) else (None, None)
    return [
        check("result.success is true", success is True, f"success = {success!r}"),
        check("result.locations is a non-empty list", isinstance(locations, list) and len(locations) > 0,
              f"{len(locations)} location(s)" if isinstance(locations, list) else f"locations is {type(locations).__name__}"),
    ]


def _ids(rows: list[str]) -> str:
    return ", ".join(rows[:10]) + (f" (+{len(rows) - 10} more)" if len(rows) > 10 else "") if rows else "none"


def location_checks(locations: list) -> list[dict]:
    """Every location needs an id, a postcode and coordinates; coordinates must lie in Germany."""
    dicts = [loc for loc in locations if isinstance(loc, dict)]
    label = lambda loc: str(loc.get("id") or "<no id>")  # noqa: E731
    no_id = [i for i, loc in enumerate(dicts) if not str(loc.get("id") or "").strip()]
    dup = sorted(k for k, n in Counter(str(loc.get("id")) for loc in dicts if loc.get("id")).items() if n > 1)
    no_zip = [label(loc) for loc in dicts if not str(loc.get("postcode") or "").strip()]
    coords = [(label(loc), parse.number(loc.get("latitude")), parse.number(loc.get("longitude"))) for loc in dicts]
    no_coord = [i for i, lat, lng in coords if lat is None or lng is None]
    outside = [i for i, lat, lng in coords if lat is not None and lng is not None and not geo.in_germany(lat, lng)]
    not_dict = len(locations) - len(dicts)
    out = [
        check("every location is an object with a unique, non-empty id", not no_id and not dup and not not_dict,
              f"{len(no_id)} without id, {not_dict} not an object, duplicate ids: {dup or 'none'}"),
        check("every location has a postcode", not no_zip, f"missing: {_ids(no_zip)}"),
        check("every location has numeric coordinates", not no_coord, f"missing: {_ids(no_coord)}"),
        check("every coordinate lies in Germany", not outside, f"outside: {_ids(outside)}"),
    ]
    odd_zip = [label(loc) for loc in dicts if str(loc.get("postcode") or "").strip() and
               not (str(loc.get("postcode")).strip().isdigit() and len(str(loc.get("postcode")).strip()) == 5)]
    out.append(check("every postcode is 5 digits", not odd_zip, f"not 5 digits: {_ids(odd_zip)}", "review"))
    no_number = [label(loc) for loc in dicts if not str(loc.get("number") or "").strip() or not str(loc.get("street") or "").strip()]
    out.append(check("every location has a street and a house number (the address match needs both)", not no_number,
                     f"missing: {_ids(no_number)}", "review"))
    return out


def count_warning(n: int, baseline: dict | None) -> str | None:
    """A warning text (never a failure) when the count differs from the last passing full run."""
    if not baseline or baseline.get("in_scope") is None or baseline["in_scope"] == n:
        return None
    return f"site count changed: {baseline['in_scope']} in {baseline.get('run_folder')} -> {n} now"
