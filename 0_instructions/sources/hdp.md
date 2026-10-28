# HDP (HolDeinePlakette own sites)

> **Status (2026-10-08):** adapter built, live full run `2026-10-07_1835_full` **PASS, 19 sites**. Not a competitor source: the list is used by the aggregator to remove HolDeinePlakette's own sites from the competitor data. Live per-source state: `scraper_status.md`, project-wide item "HDP own sites".

Official company name: **HolDeinePlakette, short HDP** (Nikos, 2026-10-07). The old name "Liria" has been removed from code and docs (2026-10-08); the only place it remains is the host name of the endpoint below, which is the server's real address. The column `is_hdp_site` (formerly `is_liria_site`) in the KÜS, VÜK and master exports is filled only from the optional file `4_reference/hdp/hdp_sites.csv`, which does not exist, so it stays empty; own sites are removed by the mechanism in section 4.

## 1. Source

- The public location list behind holdeineplakette.de: `POST https://europe-west10-liria-ai-inspect-backend.cloudfunctions.net/getAllPublicLocations` (a Firebase callable function), body `{"data":{}}`, `Origin` / `Referer` set to the website. **No key, no cookies.**
- One request per run. Timeout 60 s; at most 3 retries with backoff (5, 15, 45 s) for timeouts, connection errors, 429 and 5xx; any other non-200 answer or non-JSON answer stops the run at once.
- Answer: `result.success` and `result.locations`, a list of objects with an id (the site's short name, e.g. "Dortmund"), address and coordinates.
- The User-Agent names the project and HolDeinePlakette; the request is to our own company's public endpoint.

## 2. Adapter

- Code `1_scrapers/hdp/src/` (`fetch.py`, `parse.py`, `checks.py`, `run.py`, `simulate.py`), control notebook `1_scrapers/hdp/notebook/hdp_control.ipynb`, tests `1_scrapers/hdp/tests/`.
- Run: `python -m hdp.run full --confirm` (live; needs an explicit instruction). Output per run in `1_scrapers/hdp/output/<run>/`: `hdp_sites_<date>.csv`, `raw.jsonl`, `query_log.csv`, `run_summary.json` / `.md`.
- Checks (hard): `success` is true; `locations` is a non-empty list; every location is an object with a unique, non-empty id; every location has a postcode and numeric coordinates; every coordinate lies in Germany. Review: 5-digit postcodes; street and house number present (the address match needs both).

## 3. Result so far

19 sites (2026-10-07): Dortmund, Duisburg, Erlangen, Frankfurt-Ost, Frankfurt-West, Fürth, Kitzingen, Kohlscheid, Krefeld, Köln-Porz, Neu-Isenburg, Nuernberg-Nord, Nürnberg, Regensburg, Schwabach, Schwerte, Solingen, Unna, Weiterstadt. All 8 checks ok.

## 4. Use in the aggregator (`2_core/hdp_removal.py`, wired into `core.master_run`)

- All HDP sites run under KÜS, so each must exist as a KÜS row. The aggregator reads the **newest** HDP run (must be PASS and at most 7 days old, `MAX_HDP_AGE_DAYS`; there is no fallback to "remove nothing" and no switch to skip it), matches each HDP site to exactly one KÜS row and removes that row **before merging**.
- Match: normalised PLZ + street + house number; only if that finds nothing, PLZ + coordinates within 150 m (every fallback match is listed). A KÜS row is matched by the HDP site's address, never by name or licence code.
- Checks: every HDP site matches exactly one KÜS row; no KÜS row is claimed by two sites; rows removed (R) equal sites found (N); no removed row is left in the master. A site with no match, several matches or a doubly claimed row is **not** removed; the KÜS rows involved stay in the master flagged `hdp_match_unresolved` and are listed in `hdp_review.csv`.
- Outputs in the master run folder: `removed_hdp_own_sites.csv`, `hdp_review.csv`, the section "HDP own-site removal" of the master summary.

## 5. Open (2026-10-07)

N = 19, R = 17. The count check is a review item, it does not fail the build.

| HDP site | Problem |
|---|---|
| Fürth (Sigmundstr. 113, 90431 Nürnberg) | No KÜS row at its address; the nearest KÜS row is 1.4 km away |
| Dortmund (Wambeler Hellweg 52, 44143 Dortmund) | Matches two KÜS rows by coordinates (KUES-58908, licence I1122; KUES-30052, licence E0982, a PSP since the KÜS export includes the vorort = 0 records) |

Nikos decides how these two are resolved; no tie-break (for example by licence code) and no looser matching has been added. 18 KÜS rows carry licence I1122.
