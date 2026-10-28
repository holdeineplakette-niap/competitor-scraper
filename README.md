# Prüfstellen sourcing

Collects vehicle inspection locations (Prüfstellen) per inspection organisation
(ÜO) into CSV/XLSX files for HubSpot import. Project plan, scraper status and the
storage and naming convention live in `0_instructions/`.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m playwright install chromium   # one-time, only the TÜV Hessen adapter needs it
.venv\Scripts\python.exe -m ipykernel install --user --name pruefstellen --display-name "Python (pruefstellen)"
copy .env.example .env   # then fill in GTUE_API_KEY
```

`pip install -e .` makes the numbered code folders importable by short names
(`gtue` = `1_scrapers/gtue/src`, `core` = `2_core`), from any folder and from the
notebook. In VS Code, open the notebook and pick the kernel **Python (pruefstellen)**.
The GeoNames file is downloaded into `4_reference/geonames/` on first use if missing.

Run the tests (offline, no network) with `.venv\Scripts\python.exe -m pytest`.

## Layout

See `0_instructions/storage_and_naming_convention.md` (binding). In short:

| Path | Content |
|---|---|
| `1_scrapers/<slug>/src/` | One adapter per ÜO. GTÜ: `gtue/adapter.py` (parser, site type, validation, export, fetch, `run(mode=...)`) |
| `1_scrapers/<slug>/tests/` | Tests and fixtures (saved API responses) |
| `1_scrapers/<slug>/notebook/` | `<slug>_control.ipynb`, the manual control panel |
| `1_scrapers/<slug>/output/` | Official runs (`full`, `rebuild`), one folder per run |
| `1_scrapers/<slug>/test_output/` | `sample`, `simulated` and `recon` runs, one folder per run |
| `2_core/` | Shared code: `grid.py` (adaptive grid, offline simulator), `enrich.py`, `geo.py`, `paths.py` |
| `3_master_output/` | Combined dataset across all ÜOs, one folder per month |
| `4_reference/` | Inputs we didn't scrape (GeoNames, API samples, ground truth, HubSpot mapping) |
| `9_archive/`, `_scratch/` | Pre-cleanup leftovers, throwaway scripts |

## One run = one folder

Every run writes a new folder `YYYY-MM-DD_HHMM_<mode>` (a same-minute clash gets `_2`, `_3`, ...; an
existing folder is never written to):

```
raw.jsonl            all API responses of the run, one {"file", "response"} object per line
query_log.csv        one line per request
run_summary.json     machine-readable, source of truth (the drop check reads it)
run_summary.md       PASS/FAIL, counts, warnings
open_squares.csv     only if grid squares stayed open
gtue_all_<date>.csv/.xlsx, gtue_pruefstellen_<date>.csv/.xlsx    the exports
```

Exports are CSV (`;` separator, `.` decimal, UTF-8 with BOM, IDs and PLZ as text) plus an XLSX copy.
Don't save the CSV from Excel: it drops leading zeros.

## GTÜ run modes

`gtue.adapter.run(mode=...)` (`from gtue import adapter as gtue`):

- `simulated`: no network; 1,200 fake locations spread like PLZ centroids; must find all and PASS.
  `sim_clusters=grid.BIG_CITIES` adds dense city clusters as a stress test (finds all, but FAILs on
  purpose with coverage warnings). Output goes to `1_scrapers/gtue/test_output/`.
- `sample`: real API, 5 requests: the reference query (99986 Niederdorla, 500 km), a cap probe (2) and
  2 grid squares. Output goes to `test_output/`. Always FAIL (most squares not queried).
- `full`: real API, whole grid. Only with `confirm_full_run=True` (key use approved by management on
  2026-09-28). Output goes to `1_scrapers/gtue/output/`. A same-day cache reuses responses from the
  `raw.jsonl` of earlier runs of the same day in `output/`; the summary says how many and from which run folders.
- `rebuild` (`rebuild_from_raw(<run folder>, centroids)`): no network; rebuilds the exports from a run's
  `raw.jsonl` into a run folder of its own next to it in `output/` (for a run that stopped early).
- `recon` (`write_recon_run`): no network; exports the saved sample response into `test_output/`.

Run status: **PASS** only if every grid square is complete and the collected count matches the reference
MaxCount (rule C4, see `0_instructions/sources/gtue_grid_changes.md`). **FAIL** otherwise, with the reasons
listed. The drop check (alert if German locations fall more than 10 %) compares with the most recent
`full` run in `output/` whose `run_summary.json` says PASS ("no baseline" if there is none); it never
changes the status.

## VÜK run modes

`vuek.run` (`from vuek import run as vuek`; command line `.venv\Scripts\python.exe -m vuek.run <mode>`):

- `simulated`: no network; a fake vuek-gmbh.de with 5 stations and a fake geocoder; must PASS. Faults can be injected
  (`SimulatedVuek("menu_six")` and others, see `vuek/simulate.py`); each must FAIL. Output: `1_scrapers/vuek/test_output/`.
- `sample`: live, `/ueber-uns/` + the first station page + 1 Nominatim request; reports `SAMPLE`, never PASS.
- `full`: live, 11 requests to vuek-gmbh.de (5-7 s apart) + 5 to Nominatim, about 2 minutes; only with `--confirm`
  (`confirm_full_run=True`). Output: `1_scrapers/vuek/output/`. PASS needs the station sets of menu, footer, body text and
  both sitemaps to agree, the stated number of stations to match, the set to equal the last passing full run, and every
  station page to yield street + PLZ + city. A real change of the station set fails until it is accepted
  (`--accept-station-change`).
- `rebuild`: no network; re-parses the raw files and the saved Nominatim answers of an earlier run folder
  (`--source <run folder>`) into a run folder of its own next to it in `output/`.

A run writes `vuek_pruefstellen_<date>.csv` (in-scope rows only), `run_summary.json`/`.md`, `query_log.csv` and `raw/`.

## Data sources and licences

- **GeoNames postal codes (Germany)**: `4_reference/geonames/DE.zip` / `DE.txt`, downloaded from
  <https://download.geonames.org/export/zip/DE.zip>. Used for PLZ centroids (query points) and
  state/PLZ checks. © GeoNames (<https://www.geonames.org>), licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **OpenStreetMap Nominatim (VÜK coordinates)**: the VÜK pages carry no coordinates, so `vuek` geocodes
  the five station addresses with the public Nominatim service (<https://nominatim.openstreetmap.org>):
  at most 1 request per second, an identifying User-Agent with a contact address, results kept in the
  run's `raw/nominatim/`. The column `coord_source` says `nominatim` (or `page`). Coordinates ©
  OpenStreetMap contributors, licensed under the
  [Open Database Licence (ODbL) 1.0](https://www.openstreetmap.org/copyright).
- **Cleaned PLZ list:** `DE.txt` is kept as downloaded. Cleaning happens when it is loaded
  (`core/geo.py`, `load_plz_centroids`): only regular PLZ rows are used, Großkunden-PLZ (numeric
  state codes, company names as place) are dropped, leaving 8,309 PLZ centroids.
