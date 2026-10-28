"""Geo helpers: great-circle distance and GeoNames PLZ centroids.

GeoNames postal code data (https://download.geonames.org/export/zip/),
licensed CC BY 4.0. See README.md for attribution.
"""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

EARTH_RADIUS_KM = 6371.0088

GEONAMES_URL = "https://download.geonames.org/export/zip/DE.zip"
GEONAMES_COLUMNS = [
    "country",
    "postal_code",
    "place_name",
    "admin_name1",
    "admin_code1",
    "admin_name2",
    "admin_code2",
    "admin_name3",
    "admin_code3",
    "lat",
    "lng",
    "accuracy",
]


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km. Works on scalars and numpy/pandas arrays (NaN in -> NaN out)."""
    lat1, lng1, lat2, lng2 = (np.radians(np.asarray(v, dtype=float)) for v in (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


GERMANY_BOX = (47.2, 55.1, 5.8, 15.1)  # lat_min, lat_max, lng_min, lng_max
PLZ_FAR_KM = 15.0


def in_germany(lat, lng, box=GERMANY_BOX) -> bool:
    """True if the point lies inside the Germany bounding box (a plausibility check, not a border test)."""
    return box[0] <= lat <= box[1] and box[2] <= lng <= box[3]


def centroid_lookup(centroids: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """{PLZ: (lat, lng)} from the `load_plz_centroids` frame."""
    return {str(z): (la, lo) for z, la, lo in zip(centroids["zip"], centroids["lat"], centroids["lng"])}


def plz_distance_km(lat, lng, zip_, cent: dict[str, tuple[float, float]]) -> float | None:
    """Distance from the point to the centroid of its PLZ in km; None if the PLZ is not in GeoNames."""
    c = cent.get(str(zip_))
    if c is None:
        return None
    return float(haversine_km(lat, lng, *c))


def shared_coordinate_groups(items, coord_of, address_of, ndigits: int = 5) -> list[list]:
    """Groups of items that sit on the same coordinates (rounded) but carry more than one distinct address.

    `coord_of(item)` returns (lat, lng) or None; `address_of(item)` a comparable address key.
    Groups come in first-seen order, items inside a group in input order.
    """
    by_coord: dict[tuple, list] = {}
    for it in items:
        c = coord_of(it)
        if c is not None and c[0] is not None and c[1] is not None:
            by_coord.setdefault((round(c[0], ndigits), round(c[1], ndigits)), []).append(it)
    return [g for g in by_coord.values() if len(g) > 1 and len({address_of(i) for i in g}) > 1]


def ensure_geonames(reference_dir: str | Path) -> Path:
    """Return the path to DE.txt, downloading and unzipping DE.zip into reference_dir if missing."""
    reference_dir = Path(reference_dir)
    txt_path = reference_dir / "DE.txt"
    if txt_path.exists():
        return txt_path
    reference_dir.mkdir(parents=True, exist_ok=True)
    zip_path = reference_dir / "DE.zip"
    if not zip_path.exists():
        response = requests.get(GEONAMES_URL, timeout=60)
        response.raise_for_status()
        zip_path.write_bytes(response.content)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extract("DE.txt", reference_dir)
    return txt_path


def load_plz_centroids(path: str | Path) -> pd.DataFrame:
    """One row per PLZ: zip, city (first place_name), state (most frequent admin_name1), mean lat/lng.

    GeoNames DE mixes two datasets. Regular PLZ rows have letter state codes
    (BY, NW, ...) and German state names. About 2,500 Großkunden-PLZ (codes
    assigned to one company or authority, e.g. 10875 for Daimler) have numeric
    state codes (01-16), English state names ("Bavaria", "Land Berlin") and
    company names as place_name, with rows scattered across several states.
    Those rows are dropped: they are useless as query points and would mix
    German and English state names.
    """
    raw = pd.read_csv(
        path,
        sep="\t",
        header=None,
        names=GEONAMES_COLUMNS,
        dtype=str,
        keep_default_na=False,
        quoting=csv.QUOTE_NONE,
    )
    raw = raw[~raw["admin_code1"].str.isdigit()].copy()
    raw["lat"] = pd.to_numeric(raw["lat"])
    raw["lng"] = pd.to_numeric(raw["lng"])
    raw["admin_name1"] = raw["admin_name1"].replace("", None)
    grouped = raw.groupby("postal_code", sort=True)
    centroids = pd.DataFrame(
        {
            "city": grouped["place_name"].first(),
            "state": grouped["admin_name1"].agg(lambda s: s.mode().iloc[0] if s.notna().any() else None),
            "lat": grouped["lat"].mean(),
            "lng": grouped["lng"].mean(),
        }
    )
    return centroids.rename_axis("zip").reset_index()
