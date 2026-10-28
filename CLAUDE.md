# Prüfstellen sourcing (HolDeinePlakette)

A Python pipeline that scrapes every HU inspection location (Prüfstelle) in Germany from each Überwachungsorganisation (ÜO) and exports CSV/XLSX for HubSpot import. One adapter per ÜO in `1_scrapers/<slug>/` (gtue, kues, dekra, tuev_sued, tuev_hessen, tuev_rheinland, fsp, vuek, hdp); shared code and the master aggregator are in `2_core/` (`core`). Current state and decisions: `projects/competitor-scraping.md` in the vault root. Goals, rules and architecture: `0_instructions/project_plan.md`; live per-ÜO state: `0_instructions/scraper_status.md`.

## Rules

- **Storage and naming:** `0_instructions/storage_and_naming_convention.md` is binding for all new files (folder structure, one folder per run, naming, housekeeping). Build every path with `core/paths.py`, never with hard-coded relative paths.
- **Debug scripts only in `_scratch/`.** Nothing there is referenced by code or docs.
- **No live requests without explicit instruction.** Never run `sample` or `full` runs, the notebook's sample/full cells, or anything else that calls a finder API unless asked in that message. Offline tests and `mode="simulated"` are fine.
- **Docs in `0_instructions/` are maintained in the Claude project. Edit them only when explicitly asked.**
- **Secrets:** never print, copy or write an API key. `GTUE_API_KEY` lives DPAPI-encrypted outside the project; run anything that needs it as `powershell -ExecutionPolicy Bypass -File <vault>\tools\with-secrets.ps1 GTUE_API_KEY <cmd>` (there is no `.env` any more; `.env.example` is only the template). It is a public key from GTÜ's open locator (found via DevTools > Network > `ByBranchesAndAddress` > request header `x-api-key`; management approved automated use 2026-09-28; the provider may rotate it, a 401 means fetch the current one and re-store it with `store-secret.ps1`). If you find a key anywhere else, report file and line; don't fix it silently.
- **Scope:** exclude a site only with a *certain* cause; otherwise include and flag. Nikos's hand checks override statistical guesses. DEKRA: include all, flags only.
- **Adapter deliverable** = src + offline tests + simulated mode with negative tests + `<slug>_control.ipynb`. Live requests come last, not first, and only the one run that was asked for.
- **Output files:** default to the minimum (in-scope CSV + `run_summary.md`); no `all` file, no Excel. Ask before adding a new kind of file.
- **Never write "Liria"**: it is HDP. Exception: the endpoint host `liria-ai-inspect-backend` in `1_scrapers/hdp/src/fetch.py`.
- **Aggregator changes are Nikos's to ask for.** Don't loosen guards (can_join, forced review, input checks, HDP match rules) and never run `core.master_run official` unless asked.
- **HubSpot:** never import `hubspot_import_2026-10.csv` as is; M&A Deals are never deleted or overwritten; reading HubSpot data needs Nikos's go.
- Raw data (`raw.jsonl`) is never edited. Nothing is deleted: leftovers go to `9_archive/`.
- Personal data (operator names, emails, phone numbers) is in raw responses, exports and the München test fixture: keep those out of git. Showing them in chat is fine when Nikos asks (2026-10-26).

## Working in the project

- Setup once: `python -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -e ".[dev]"` (makes `gtue` and `core` importable everywhere, including the notebook kernel).
- Tests (offline, mocked network): `.venv\Scripts\python.exe -m pytest`.
- Monthly run: `python -m core.monthly scrape|tuev-sued|build|status` (see `0_instructions/scraper_automation_plan.md` §10). Without `--confirm` it only prints its plan; with it, it sends live requests and builds the official master, so the no-live-requests rule applies.
- The GTÜ adapter is one module for now (`gtue/adapter.py`; import `from gtue import adapter as gtue`). Splitting it into `fetch.py` / `parse.py` / `classify.py` / `run.py` is a later task.
