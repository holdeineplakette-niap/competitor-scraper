"""Parse the getAllPublicLocations answer into one row per location `id`. Pure functions, no network.

`slugs` are SEO aliases of one site, never separate sites. The display name (`publicName`) does not always match the real
address ("Fürth" is in Nürnberg, "Schwabach" in Büchenbach), so nothing downstream may use the name: street, number, postcode
and coordinates are the identity of a site. `publicName` can be null; the row is kept.
"""

from __future__ import annotations

import json

import pandas as pd

SLUG = "hdp"
SOURCE_ORG = "HDP"
ID_PREFIX = "HDP-"

COLUMNS = [
    "location_id", "source_org", "source_id", "name", "street", "street_name", "house_number", "zip", "city", "state",
    "country", "lat", "lng", "address_raw", "opening_hours", "disabled_service_types", "maps_url", "slugs", "in_scope",
    "doubt_flags", "scraped_at",
]
DAYS = (("monday", "Mo"), ("tuesday", "Di"), ("wednesday", "Mi"), ("thursday", "Do"), ("friday", "Fr"), ("saturday", "Sa"),
        ("sunday", "So"))


class ParseError(ValueError):
    """The answer is not the expected callable envelope."""


def parse_payload(text: str) -> dict:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ParseError(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParseError(f"JSON top level is {type(payload).__name__}, expected an object")
    return payload


def envelope(payload: dict) -> tuple[object, object]:
    """(result.success, result.locations) as delivered; None for whatever is missing. No validation here (see checks.py)."""
    result = payload.get("result")
    if not isinstance(result, dict):
        return None, None
    return result.get("success"), result.get("locations")


def _text(value) -> str:
    return "" if value is None else " ".join(str(value).split())


def number(value) -> float | None:
    """A coordinate as float; None for a missing, non-numeric, NaN or boolean value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def hours_text(hours) -> str:
    """'Mo 09:00-17:00; ...; Sa closed': compact and stable (no slot data)."""
    if not isinstance(hours, dict):
        return ""
    parts = []
    for key, label in DAYS:
        d = hours.get(key)
        if not isinstance(d, dict):
            continue
        parts.append(f"{label} {_text(d.get('open'))}-{_text(d.get('close'))}" if d.get("isOpen") else f"{label} closed")
    return "; ".join(parts)


def to_row(loc: dict, scraped_at: str) -> dict:
    street_name, house_number = _text(loc.get("street")), _text(loc.get("number"))
    flags = []
    if not _text(loc.get("publicName")):
        flags.append("no_public_name")
    if not house_number:
        flags.append("no_house_number")
    disabled = loc.get("disabledServiceTypes")
    return {
        "location_id": f"{ID_PREFIX}{_text(loc.get('id'))}", "source_org": SOURCE_ORG, "source_id": _text(loc.get("id")),
        "name": _text(loc.get("publicName")), "street": f"{street_name} {house_number}".strip(), "street_name": street_name,
        "house_number": house_number, "zip": _text(loc.get("postcode")), "city": _text(loc.get("city")),
        "state": _text(loc.get("bundesland")), "country": "DE", "lat": number(loc.get("latitude")),
        "lng": number(loc.get("longitude")), "address_raw": _text(loc.get("address")),
        "opening_hours": hours_text(loc.get("openingHours")),
        "disabled_service_types": "|".join(map(str, disabled)) if isinstance(disabled, list) else _text(disabled),
        "maps_url": _text(loc.get("mapsUrl")),
        "slugs": "|".join(map(str, loc.get("slugs") or [])) if isinstance(loc.get("slugs"), list) else "",
        "in_scope": True, "doubt_flags": "; ".join(flags), "scraped_at": scraped_at,
    }


def to_frame(locations: list[dict], scraped_at: str) -> pd.DataFrame:
    rows = [to_row(loc, scraped_at) for loc in locations if isinstance(loc, dict)]
    return pd.DataFrame(rows, columns=COLUMNS).sort_values("location_id", kind="stable").reset_index(drop=True)
