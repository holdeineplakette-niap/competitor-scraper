"""Shared test fixtures for every scraper and for core (the one conftest of the project)."""

import pytest

from core import paths
from core.geo import load_plz_centroids

GEONAMES = paths.geonames_dir() / "DE.txt"
SAMPLE = paths.fixtures_dir("gtue") / "response_muenchen_50km_2026-09-28.json"


@pytest.fixture(scope="session")
def centroids():
    if not GEONAMES.exists():
        pytest.skip(f"GeoNames file missing: {GEONAMES} (run core.geo.ensure_geonames(paths.geonames_dir()))")
    return load_plz_centroids(GEONAMES)
