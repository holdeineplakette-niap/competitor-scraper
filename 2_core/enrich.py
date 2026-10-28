"""Source-independent enrichment based on GeoNames PLZ centroids.

Expects a parsed source frame with at least zip, lat, lng, state and
needs_review columns (needs_review_reason is created if missing).
"""

from __future__ import annotations

import pandas as pd

from core.geo import haversine_km

PLZ_DISTANCE_LIMIT_KM = 25
REASON_FAR_FROM_PLZ = "coordinate far from PLZ"
REASON_UNKNOWN_PLZ = "unknown PLZ"
REASON_SEPARATOR = "; "


def split_reasons(value) -> list[str]:
    if not isinstance(value, str):
        return []
    return [r for r in (p.strip() for p in value.split(REASON_SEPARATOR.strip())) if r]


def add_reason(existing, reason: str) -> str:
    """Append a reason once, keeping any reasons already set."""
    reasons = split_reasons(existing)
    if reason not in reasons:
        reasons.append(reason)
    return REASON_SEPARATOR.join(reasons)


def enrich(
    df: pd.DataFrame,
    centroids: pd.DataFrame,
    max_distance_km: float = PLZ_DISTANCE_LIMIT_KM,
    check: pd.Series | None = None,
) -> pd.DataFrame:
    """Fill state from the PLZ, add plz_distance_km and flag suspicious rows.

    - state: filled from GeoNames where the source left it empty
    - plz_distance_km: distance from the row's lat/lng to its PLZ centroid
    - needs_review + needs_review_reason: "coordinate far from PLZ" (> max_distance_km)
      or "unknown PLZ" (zip missing or not in GeoNames)
    check: boolean mask of rows to enrich (e.g. German rows only); other rows are
    left untouched, because a foreign postcode can look like a German PLZ.
    Idempotent: running it twice does not duplicate reasons.
    """
    out = df.copy()
    lookup = centroids.set_index("zip")
    mask = pd.Series(True, index=out.index) if check is None else check.reindex(out.index).fillna(False).astype(bool)
    zips = out["zip"].astype("string").str.strip().where(mask)
    known = zips.isin(lookup.index).fillna(False).astype(bool) | ~mask

    out["state"] = out["state"].where(out["state"].notna(), zips.map(lookup["state"]))

    distance = pd.Series(
        haversine_km(out["lat"], out["lng"], zips.map(lookup["lat"]), zips.map(lookup["lng"])),
        index=out.index,
    ).round(2)
    if "plz_distance_km" in out.columns:
        out["plz_distance_km"] = distance
    else:
        position = out.columns.get_loc("lng") + 1 if "lng" in out.columns else len(out.columns)
        out.insert(position, "plz_distance_km", distance)

    if "needs_review_reason" not in out.columns:
        out["needs_review_reason"] = None
    reasons = out["needs_review_reason"].astype(object).where(out["needs_review_reason"].notna(), None)

    far = (distance > max_distance_km).fillna(False).astype(bool)
    for idx in out.index[far]:
        reasons[idx] = add_reason(reasons[idx], REASON_FAR_FROM_PLZ)
    for idx in out.index[~known]:
        reasons[idx] = add_reason(reasons[idx], REASON_UNKNOWN_PLZ)

    out["needs_review_reason"] = reasons
    out["needs_review"] = out["needs_review"].astype(bool) | far | ~known
    return out


def reason_counts(df: pd.DataFrame) -> dict[str, int]:
    """needs_review count per reason (a row with two reasons counts for both)."""
    counts: dict[str, int] = {}
    if "needs_review_reason" not in df.columns:
        return counts
    for value in df["needs_review_reason"]:
        for reason in split_reasons(value):
            counts[reason] = counts.get(reason, 0) + 1
    return counts
