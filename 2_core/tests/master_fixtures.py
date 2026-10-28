"""Synthetic inputs for the master tests: a temporary project root with source exports. No real rows, no personal data."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from core import master_inputs as mi
from core import paths

# PLZ -> (city, state, lat, lng): invented-but-plausible centroids so tests need no GeoNames file
CENTROIDS = pd.DataFrame(
    [
        ("01067", "Dresden", "Sachsen", 51.0600, 13.7400),
        ("10115", "Berlin", "Berlin", 52.5320, 13.3850),
        ("53340", "Meckenheim", "Nordrhein-Westfalen", 50.6300, 7.0300),
        ("61440", "Oberursel", "Hessen", 50.2000, 8.5800),
        ("61348", "Bad Homburg", "Hessen", 50.2270, 8.6180),
        ("65520", "Bad Camberg", "Hessen", 50.2960, 8.2650),
        ("35037", "Marburg", "Hessen", 50.8100, 8.7700),
        ("80331", "München", "Bayern", 48.1370, 11.5750),
        ("20095", "Hamburg", "Hamburg", 53.5500, 10.0000),
        ("17367", "Eggesin", "Mecklenburg-Vorpommern", 53.6900, 14.0700),
        ("17235", "Neustrelitz", "Mecklenburg-Vorpommern", 53.3600, 13.0700),
        ("30453", "Hannover", "Niedersachsen", 52.3500, 9.7000),
        ("32791", "Lage", "Nordrhein-Westfalen", 51.9900, 8.7900),
        ("01279", "Dresden", "Sachsen", 51.0400, 13.8000),
        ("01309", "Dresden", "Sachsen", 51.0500, 13.8000),
    ],
    columns=["zip", "city", "state", "lat", "lng"],
)

# defaults that satisfy the mapping tables for each source
DEFAULTS: dict[str, dict] = {
    "gtue": {"site_type": "pruefstelle", "in_scope": "True", "country": "D"},
    "kues": {"site_type": "pruefstelle", "in_scope": "True", "country": "DE", "kues_licensee": "K1", "psp": "No"},
    "dekra": {"site_type": "pruefstelle", "in_scope": "True", "country": "DE"},
    "fsp": {"needs_review": "False"},
    "tuev_sued": {"site_type": "pruefstelle", "operator_group": "own", "in_scope": "True", "country": "DE"},
    "tuev_nord": {"site_type": "own_station_probable", "in_scope": "True"},
    "tuev_rheinland": {"site_type": "own_station", "operator_group": "tuev_rheinland", "in_scope": "True"},
    "tuev_hessen": {"site_type": "own_station", "operator": "TÜV Hessen", "in_scope": "True"},
    "tuev_thueringen": {"site_type": "own_station", "in_scope": "True"},
    "vuek": {"site_type": "pruefstelle", "in_scope": "True", "country": "DE"},
}


def header(slug: str) -> list[str]:
    cfg = mi.SOURCES[slug]
    cols = ["location_id"] + [c for c in cfg.columns if c != "location_id"] + list(cfg.ignored) + list(mi.KNOWN_EXTRAS.get(slug, ()))
    return list(dict.fromkeys(cols))


def source_row(slug: str, location_id: str, **kw) -> dict:
    """A row for `slug`. Keys: master-style names (name, street, zip, city, lat, lng, phone, ...) or raw source columns."""
    cfg = mi.SOURCES[slug]
    tgt = {}
    for col, t in cfg.columns.items():
        tgt.setdefault(t, col)
    row = {c: "" for c in header(slug)}
    row.update(DEFAULTS.get(slug, {}))
    row["location_id"] = location_id
    row["scraped_at"] = "2026-10-06T10:00:00"
    for k, v in kw.items():
        if k in tgt:
            row[tgt[k]] = v
        elif k in row:
            row[k] = v
        else:
            raise KeyError(f"{slug}: no column for {k!r}")
    return row


def write_source(root: Path, slug: str, rows: list[dict], run: str = "2026-10-06_1000_full", status: str | None = "PASS",
                 summary_extra: dict | None = None, file_tag: str = "", drop_columns: tuple[str, ...] = ()) -> Path:
    cfg = mi.SOURCES[slug]
    run_dir = paths.output_dir(slug, "full", root=root) / run
    run_dir.mkdir(parents=True, exist_ok=True)
    pattern = cfg.in_scope_pattern.replace("*", "2026-10-06" + file_tag)
    cols = [c for c in header(slug) if c not in drop_columns]
    pd.DataFrame(rows, columns=header(slug))[cols].to_csv(run_dir / pattern, sep=";", index=False, encoding="utf-8-sig")
    if cfg.proof == "summary" and status is not None:
        key = "outcome" if slug == "fsp" else "status"
        mode = "rebuild" if run.endswith("_rebuild") else "full"
        s = {"mode": mode, key: status, "started": f"{run[:10]}T{run[11:13]}:{run[13:15]}:00", **(summary_extra or {})}
        (run_dir / "run_summary.json").write_text(json.dumps(s), encoding="utf-8")
    return run_dir / pattern


def all_sources(root: Path, extra: dict[str, list[dict]] | None = None, **kwargs) -> dict[str, Path]:
    """One valid, mostly empty export per source; `extra` replaces the rows of the named sources."""
    base = {
        "gtue": [source_row("gtue", "GTUE-1", name="GTÜ Prüfstelle A", street="Hauptstr. 5", zip="01067", city="Dresden", lat="51.06", lng="13.74", phone="0351 1")],
        "kues": [source_row("kues", "KUES-1", name="KÜS Prüfstelle", street="Gewerbering 2", zip="80331", city="München", lat="48.137", lng="11.575")],
        "dekra": [source_row("dekra", "DEKRA-1", name="DEKRA Niederlassung", street="Industriestr. 9", zip="20095", city="Hamburg", lat="53.55", lng="10.0")],
        "fsp": [],
        "tuev_sued": [],
        "tuev_nord": [source_row("tuev_nord", "TUEV_NORD-1", name="TÜV NORD Station", street="Hafenstr. 1", zip="10115", city="Berlin", latitude="52.532", longitude="13.385")],
        "tuev_rheinland": [],
        "tuev_hessen": [],
        "tuev_thueringen": [source_row("tuev_thueringen", "TUEV_THUERINGEN-1", name="TÜV Thüringen Station", street="Lindenweg 3", zip="01067", city="Dresden", lat="51.061", lng="13.741")],
        "vuek": [source_row("vuek", "VUEK-lage", name="VÜK Prüfstelle Lage", street="Sylbacher Str. 1", zip="32791", city="Lage", lat="51.99", lng="8.79")],
    }
    base.update(extra or {})
    return {slug: write_source(root, slug, rows, **kwargs) for slug, rows in base.items()}
