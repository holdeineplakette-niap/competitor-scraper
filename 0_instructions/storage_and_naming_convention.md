# Storage and naming convention

Binding for every new file in this project. Established 2026-09-30.

## Folder structure

```
pruefstellen-sourcing/
├── CLAUDE.md                  # stays at root (Claude Code reads it here)
├── README.md                  # setup, how to run, data licences (GeoNames CC BY 4.0)
├── pyproject.toml             # dependencies, pytest settings, makes the numbered code folders importable
├── conftest.py                # shared test fixtures for every scraper and core (the only conftest)
├── .env / .env.example        # API keys (never in git)
├── .gitignore
├── .venv/                     # (ignored)
│
├── 0_instructions/            # every .md about the project, nothing else
│   ├── storage_and_naming_convention.md
│   ├── project_plan.md
│   ├── scraper_status.md      # overview table + short status block per ÜO
│   └── sources/
│       ├── gtue.md            # full recon notes: endpoints, fields, quirks, tests
│       └── gtue_grid_changes.md
│
├── 1_scrapers/                # one folder per ÜO, all built identically
│   ├── gtue/
│   │   ├── src/               # adapter code (importable as package `gtue`)
│   │   ├── tests/
│   │   │   └── fixtures/      # saved API responses the tests use
│   │   ├── notebook/          # gtue_control.ipynb (manual control panel)
│   │   ├── output/            # official runs only (full, rebuild), one folder per run
│   │   └── test_output/       # sample, simulated and recon runs, one folder per run
│   └── dekra/                 # same five folders, empty for now (.gitkeep)
│
├── 2_core/                    # shared code (importable as package `core`)
│   ├── grid.py, enrich.py, geo.py, …   # later: http.py, dedupe.py, diff.py, export.py
│   ├── notebook/
│   │   └── master_control.ipynb        # control notebook of the aggregator
│   └── tests/
│
├── 3_master_output/           # combined dataset across all ÜOs
│   ├── YYYY-MM/               # official master (never overwritten); a failed official run is moved to 9_archive/ before a rerun;
│   │                          # holds the HubSpot files too: hubspot_import_YYYY-MM.csv (first master) or hubspot_changes_YYYY-MM.csv (+ _overview), hubspot_excluded_YYYY-MM.csv
│   ├── _dev/YYYY-MM-DD_HHMM/  # dev runs of the aggregator (try-outs; clean out by hand)
│   └── _monthly/YYYY-MM/      # core.monthly: monthly_status.json + monthly_status.md (the month's status, what n8n reads),
│                              # build.lock only while a build runs; kept forever like the master folders
├── 4_reference/               # inputs we didn't scrape
│   ├── geonames/              # DE.zip + DE.txt
│   ├── geocode_cache/
│   │   └── nominatim.jsonl    # Nominatim answers, so reruns make no requests
│   ├── api_samples/gtue/      # recon samples NOT used by tests
│   ├── ground_truth/          # hand-labeled locations (later)
│   │   └── master_match_overrides.csv   # manual same/different decisions (location_id_a;location_id_b;decision;decided_by;date;note)
│   ├── hdp/
│   │   └── hdp_sites.csv    # optional, not needed: HDP's own sites come from the HDP scraper (1_scrapers/hdp/); is_hdp_site stays empty without this file
│   └── hubspot/               # HubSpot ID mapping (later)
├── 9_archive/                 # pre-cleanup leftovers
└── _scratch/                  # throwaway debug scripts; never referenced, emptied regularly
```

## One run = one folder

```
1_scrapers/gtue/output/2026-10-01_0900_full/
├── gtue_all_2026-10-01.csv            # every entry the source lists
├── gtue_pruefstellen_2026-10-01.csv   # in_scope rows only
├── gtue_pruefstellen_2026-10-01.xlsx
├── raw.jsonl                          # ALL API responses of the run, one JSON object per line
├── query_log.csv                      # one line per request
├── open_squares.csv                   # only if grid squares stayed open
├── run_summary.json                   # machine-readable, the source of truth
└── run_summary.md                     # PASS/FAIL, counts, warnings (rendered from the JSON)
```

`raw.jsonl` lines are `{"file": "<name>", "response": <original JSON>}`, in request order.

An aggregator run (official or dev) is also one folder:

```
3_master_output/YYYY-MM/                       (dev: 3_master_output/_dev/YYYY-MM-DD_HHMM/)
├── pruefstellen_master_YYYY-MM.csv            # or pruefstellen_master_unverified_YYYY-MM.csv if a hard check failed
├── diff_YYYY-MM.csv
├── merge_log.csv
├── input_manifest.csv
├── master_summary.json / master_summary.md
└── pruefstellen_extra_<slug>_YYYY-MM.csv      # source columns with no master home, keyed by master_id + location_id
```

### Run modes

| Mode | Meaning | Goes to |
|---|---|---|
| `full` | live, whole grid | `output/` |
| `rebuild` | offline rebuild of the exports from the `raw.jsonl` of an earlier run; its `run_summary` names the source run folder; no copy of `raw.jsonl` | `output/`, next to its source run |
| `sample` | live, a few requests (testing only) | `test_output/` |
| `simulated` | offline, fake API | `test_output/` |
| `recon` | offline parse/export of a saved sample response | `test_output/` |

A run never writes into an existing run folder. If a folder of the same name exists (same minute), the new run takes the next free suffix (`_2`, `_3`, …).

Simulated monthly runs (`core.monthly` with a simulated context, tests and the notebook only) copy each simulated run into `output/` as `YYYY-MM-DD_HHMM_full_simulated` (run_summary marked `"simulated": true`) so the master can select it. This happens only in a throwaway sandbox root; `core.monthly` refuses it in the project folder or the data root.

## Naming rules

- Everything is lowercase, ASCII and snake_case. No spaces, no umlauts, no dots except before the extension.
- Dates use ISO format and come first in folder names so they sort chronologically.
- ÜO slugs (fixed list): `gtue`, `kues`, `dekra`, `fsp`, `tuev_sued`, `tuev_nord`, `tuev_rheinland`, `tuev_hessen`, `tuev_thueringen`, `tuev_saarland`. The `location_id` prefix is the slug in uppercase (`GTUE-166591000`, `TUEV_SUED-…`).

| What | Pattern | Example |
|---|---|---|
| Run folder | `YYYY-MM-DD_HHMM_<mode>` (+ `_2`, `_3` on a same-minute clash) | `2026-09-28_1412_sample` |
| Export files | `<slug>_<content>_YYYY-MM-DD.<ext>` | `gtue_pruefstellen_2026-10-01.csv` |
| Internal run files | fixed names | `raw.jsonl`, `query_log.csv`, `open_squares.csv`, `run_summary.json`, `run_summary.md` |
| Master | `pruefstellen_master_YYYY-MM.csv` (+ `diff_YYYY-MM.csv`) | `pruefstellen_master_2026-10.csv` |
| Adapter modules | same role names in every ÜO (target; GTÜ is still one module, `adapter.py`) | `fetch.py`, `parse.py`, `classify.py`, `run.py` |
| Notebook | `<slug>_control.ipynb` | `gtue_control.ipynb` |
| Tests / fixtures | `test_<module>.py` / `<what>_YYYY-MM-DD.json` | `response_muenchen_50km_2026-09-28.json` |
| Docs | fixed name, never dated or versioned | no `_v2`, `_final`, `_new`, `_old` |

## Housekeeping rules

- Nothing new goes at the root beyond the files listed above. Generated tool folders are tolerated: `.pytest_cache/` and the `*.egg-info/` that `pip install -e .` creates.
- Ad-hoc/debug scripts go only in `_scratch/`, and no code or doc references them.
- All paths in code come from `core/paths.py`; no hard-coded relative paths like `../output`.
- Data root: the project folder, unless the environment variable `PRUEFSTELLEN_DATA_ROOT` names another folder (the monthly runner on a synced Drive copy). Run folders, `3_master_output/` and `4_reference/` then live there; code, test fixtures and notebooks stay in the project folder.
- `test_output/` runs older than 30 days may be deleted. `output/` and `3_master_output/` (including `_monthly/`) are kept forever (the change report needs the history).
- Git tracks code, tests, docs, `pyproject.toml`, `.env.example`, and fixtures only if they contain no personal data. Git ignores `.env`, `.venv/`, `**/output/`, `**/test_output/`, `3_master_output/`, `4_reference/` (except a README), `9_archive/`, `_scratch/`, `.ipynb_checkpoints/`, `.pytest_cache/`, `*.zip`, `api-keys.md` and the personal-data fixture.
- Data folders are not in git, so they need a private backup outside the repo.
- API keys live in `.env` only. The key reference file (`api-keys.md`) lives outside the project (`../private/`).
- Docs in `0_instructions/` are maintained in the Claude project. Edit them only when explicitly asked.

## How to add a new ÜO

1. Create the five folders `1_scrapers/<slug>/{src,tests/fixtures,notebook,output,test_output}` (`.gitkeep` in empty ones).
2. Add the package mapping to `pyproject.toml` (`<slug> = "1_scrapers/<slug>/src"`) and to `packages`.
3. Create `0_instructions/sources/<slug>.md` (recon notes).
4. Add the row to `scraper_status.md`.
