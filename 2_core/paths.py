"""Every project path is built here (see 0_instructions/storage_and_naming_convention.md).

Nothing else in the project hard-codes relative paths like "../output". All functions take an
optional `root` so tests can work in a temporary project folder.

The data root (run folders, master output, 4_reference) is the code folder unless the environment variable
PRUEFSTELLEN_DATA_ROOT names another folder: the monthly runner works on a synced Drive copy that way. Code-side
folders (test fixtures, notebooks) always stay in the code folder.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

MODES = ("full", "rebuild", "sample", "simulated", "recon")
OFFICIAL_MODES = ("full", "rebuild")  # go to output/; every other mode goes to test_output/

RAW_FILE = "raw.jsonl"
QUERY_LOG_FILE = "query_log.csv"
OPEN_SQUARES_FILE = "open_squares.csv"
RUN_SUMMARY_JSON = "run_summary.json"
RUN_SUMMARY_MD = "run_summary.md"


DATA_ROOT_ENV = "PRUEFSTELLEN_DATA_ROOT"


def code_root() -> Path:
    """The folder that holds 0_instructions/, 1_scrapers/, 2_core/, ..."""
    return Path(__file__).resolve().parents[1]


def project_root() -> Path:
    """The data root: $PRUEFSTELLEN_DATA_ROOT if set, else the code folder."""
    env = os.environ.get(DATA_ROOT_ENV, "").strip()
    return Path(env) if env else code_root()


def _root(root) -> Path:
    return Path(root) if root is not None else project_root()


def scraper_dir(slug: str, root=None) -> Path:
    return _root(root) / "1_scrapers" / slug


def output_dir(slug: str, mode: str, root=None) -> Path:
    """output/ for official runs (full, rebuild), test_output/ for sample, simulated and recon."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return scraper_dir(slug, root) / ("output" if mode in OFFICIAL_MODES else "test_output")


def run_name(mode: str, started_at: datetime) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return f"{started_at:%Y-%m-%d_%H%M}_{mode}"


def run_dir(slug: str, mode: str, started_at: datetime, root=None) -> Path:
    """The run folder for this start time (YYYY-MM-DD_HHMM_<mode>), without creating it."""
    return output_dir(slug, mode, root) / run_name(mode, started_at)


def new_run_dir(slug: str, mode: str, started_at: datetime, root=None) -> Path:
    """Create and return a run folder that did not exist before.

    On a same-minute clash the next free suffix (_2, _3, ...) is used. mkdir(exist_ok=False) makes the
    no-overwrite rule hold even if two runs race for the same name: a run never writes into an existing folder.
    """
    base = run_dir(slug, mode, started_at, root)
    base.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    path = base
    while True:
        try:
            path.mkdir(exist_ok=False)
            return path
        except FileExistsError:
            n += 1
            path = base.with_name(f"{base.name}_{n}")


def reference_dir(root=None) -> Path:
    return _root(root) / "4_reference"


def geonames_dir(root=None) -> Path:
    return reference_dir(root) / "geonames"


def fixtures_dir(slug: str, root=None) -> Path:
    return (Path(root) if root is not None else code_root()) / "1_scrapers" / slug / "tests" / "fixtures"


def master_output_dir(month: str, root=None) -> Path:
    """3_master_output/YYYY-MM/"""
    return _root(root) / "3_master_output" / month


MASTER_DEV_DIR = "_dev"


def new_master_dev_dir(started_at: datetime, root=None) -> Path:
    """Create 3_master_output/_dev/YYYY-MM-DD_HHMM/ (next free suffix on a same-minute clash)."""
    base = _root(root) / "3_master_output" / MASTER_DEV_DIR / f"{started_at:%Y-%m-%d_%H%M}"
    base.parent.mkdir(parents=True, exist_ok=True)
    n, path = 1, base
    while True:
        try:
            path.mkdir(exist_ok=False)
            return path
        except FileExistsError:
            n += 1
            path = base.with_name(f"{base.name}_{n}")


def latest_official_master(root=None, before_month: str | None = None) -> Path | None:
    """The newest official master CSV (3_master_output/YYYY-MM/pruefstellen_master_YYYY-MM.csv), or None.

    `_dev/` and failed (`_unverified`) masters are never returned. `before_month` limits the search to
    months strictly before it.
    """
    base = _root(root) / "3_master_output"
    if not base.is_dir():
        return None
    for d in sorted((p for p in base.iterdir() if p.is_dir() and len(p.name) == 7 and p.name[4] == "-"), reverse=True):
        if before_month is not None and d.name >= before_month:
            continue
        f = d / f"pruefstellen_master_{d.name}.csv"
        if f.exists():
            return f
    return None


def official_masters(root=None, before_month: str | None = None) -> list[Path]:
    """Every official master CSV (3_master_output/YYYY-MM/), oldest first; `_dev/` and `_unverified` files are never listed."""
    base = _root(root) / "3_master_output"
    if not base.is_dir():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir() and len(p.name) == 7 and p.name[4] == "-"):
        if before_month is not None and d.name >= before_month:
            continue
        f = d / f"pruefstellen_master_{d.name}.csv"
        if f.exists():
            out.append(f)
    return out


def geocode_cache_path(root=None) -> Path:
    return reference_dir(root) / "geocode_cache" / "nominatim.jsonl"


def overrides_path(root=None) -> Path:
    return reference_dir(root) / "ground_truth" / "master_match_overrides.csv"


def hdp_sites_path(root=None) -> Path:
    return reference_dir(root) / "hdp" / "hdp_sites.csv"


def master_notebook_dir(root=None) -> Path:
    return (Path(root) if root is not None else code_root()) / "2_core" / "notebook"


MONTHLY_DIR = "_monthly"
MONTHLY_STATUS_JSON = "monthly_status.json"
MONTHLY_STATUS_MD = "monthly_status.md"


def monthly_dir(month: str, root=None) -> Path:
    """3_master_output/_monthly/YYYY-MM/: status file and build lock of core.monthly (not the master folder itself:
    an official build refuses to start when 3_master_output/YYYY-MM/ exists)."""
    return _root(root) / "3_master_output" / MONTHLY_DIR / month
