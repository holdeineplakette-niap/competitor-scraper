# TÜV SÜD — recon notes and build spec

State: 2026-10-05. Recon done, access route decided, adapter not built yet. The short per-source status (field table, tested behaviour, options considered) is in `0_instructions/scraper_status.md`, section "TÜV SÜD". This file is the build spec.

## 1. Source in one paragraph

TÜV SÜD's HU finder (`tuvsud.com/de-de/branchen/mobilitaet-und-automotive/hauptuntersuchung/service-center-und-termin`) loads its results from `POST https://www.tuvsud.com/astscsearch/api/tscsearch/searchTSCLocation` with the JSON body `{"searchTerm":"<PLZ>","hu":1}`. The answer holds the server's search centre (top-level `lat`, `lon`, `locationname`) and `finaltsc[]`: the **50 nearest** Service-Center, sorted by distance (`distance` = great-circle km from that centre, verified on three records). No count field, no paging, no limit parameter in the body. Whether a sparse area also returns exactly 50, or fewer because of a radius limit, is untested. The corporate locator (`ajax.plex?modulename=WOlocationSearch`) is **not** a source (no HU sites).

## 2. Access decision (management, 2026-10-05)

The whole site (API and static pages) answers scripted requests with a Cloudflare challenge (HTTP 403, `cf-mitigated: challenge`). Management approved **option 2**: Nikos runs the queries **in his own, normal browser session** on tuvsud.com, from the DevTools console; everything else runs offline in Python. `robots.txt` was checked and is fine (management confirmed).

Boundaries that are part of the decision and must stay in the code:

- No headless browser, no browser automation (Playwright, Selenium, …), no faked headers or fingerprints, no challenge solvers, no copying cookies into Python. The browser snippet sends the same request the page itself sends, from the page, with the page's own session.
- Slow and bounded: one request at a time, 5 s + random 0–2 s between requests, a hard ceiling per pass, and an immediate stop (keeping what was collected) if an answer is not the expected JSON (e.g. a challenge page).
- If TÜV SÜD starts challenging the in-browser requests too, the run stops and we report; no escalation.
- OpenStreetMap stays the planned cross-check (later, separate step).

## 3. How a monthly run works (Nikos's view)

1. In the notebook `tuev_sued_control.ipynb`: run **plan**. It writes the browser snippet for pass 1 (`tuev_sued_pass01.js`) into the run folder; the snippet contains the PLZ list for this pass.
2. In Chrome: open the HU finder page normally (accept cookies, search one PLZ by hand so the page is fully loaded). Open DevTools → Console, paste the snippet, press Enter. It shows progress, takes a few minutes and downloads `tuev_sued_pass01_<date>_<time>.jsonl`.
3. In the notebook: run **check** with that file. It proves coverage. Result: either **PASS** (go to 4), or a list of uncovered areas plus a new snippet for pass 2 (repeat step 2 with it).
4. Run **build**: exports, run summary, drop check.

Expected: first month 2 passes (~130 + ~20–60 requests); later months usually 1 pass, because the plan reuses the query list of the last passing run.

## 4. Coverage proof (the core idea)

- Each answer proves: *every listed Service-Center closer to the server's centre than the last returned record was returned* (strict distance sort, cap = number returned). Per query: centre `c_q` = top-level `lat`/`lon`; radius `r_q` = `distance` of the **last** record − 0.5 km (tie margin).
  - This rule is conservative for both cases: 50 returned (cap) and fewer than 50 (would mean a radius limit; flagged as a warning in the summary, because it changes our picture of the API).
  - 0 records: `r_q` = 0, the query covers nothing; flagged.
- **Targets** = all regular GeoNames PLZ centroids (the cleaned list in `2_core`, ~8,309). A target `t` is covered if some query has `dist(t, c_q) ≤ r_q − M`, with **M = 15 km**.
- Why that proves completeness: if every TÜV SÜD site lies within M of some regular PLZ centroid, then a site near a covered target is within `r_q` of `c_q` and must have been returned. The assumption is checked on every run: the maximum distance from a collected site to its nearest PLZ centroid is reported; above 10 km → warning (KÜS measured 12.8 km max on 413 sites; M = 15 km has room).
- **PASS** needs every target covered. Reuse `2_core/radius_cover.py` if it can take a per-query radius; otherwise extend it there (shared), not a private copy in the adapter. Say which you did.

## 5. Planning query lists

- **Pass 1, first ever run:** hex lattice over Germany with spacing **60 km**, each point snapped to the nearest regular PLZ (reuse the KÜS lattice code); ~120–140 PLZ. Reasoning: around München the 50th site is at 64 km → effective radius ~48 km after M; denser regions may be smaller, the fill-in pass catches them.
- **Later months, pass 1:** the merged query list of the last PASS run (same PLZ → same server centres → nearly the same coverage).
- **Fill-in passes:** from the uncovered targets, greedy: estimate the expected radius at each uncovered target from nearby answered queries, repeatedly pick the uncovered target that would cover the most uncovered targets, mark them provisionally covered, repeat. Cap 60 PLZ per fill-in pass; at most 4 passes per run, then the run ends as FAIL with the uncovered targets listed.
- The server geocodes each PLZ to its own centre (as at GTÜ/KÜS). Always use the returned centre, never the GeoNames centroid, for coverage. A PLZ whose own target stays uncovered after it was queried is never picked again; its area must be covered from elsewhere.

## 6. Browser snippet (generated JS, one file per pass)

Generated by Python from a template; the PLZ list and the pass number are embedded. Behaviour:

- Abort with a clear message unless `location.hostname === "www.tuvsud.com"`.
- For each PLZ in order: `fetch(ENDPOINT, {method: "POST", credentials: "same-origin", headers: <the same content headers the page sends; check the captured request, add nothing else>, body: JSON.stringify({searchTerm: plz, hu: 1})})`.
- Wait 5 s + random 0–2 s between requests. Timeout 60 s.
- Valid answer = HTTP 200, JSON, has `finaltsc` (array) and top-level `lat`/`lon`. Otherwise wait 30 s, retry once; if still invalid → **stop the loop**, record `stopped_reason` (status code, content type, first 200 characters).
- Each answer becomes one line: `{"file": "pass01_q007_80686.json", "response": <answer as received>}` (the convention's `raw.jsonl` line format).
- Last line: `{"file": "browser_summary.json", "response": {"pass": 1, "planned": n, "done": k, "started": iso, "finished": iso, "stopped_reason": null | {...}, "snippet_version": "..."}}`.
- Console progress per request: `7/132 80686 → 50 sites, last 64.2 km`.
- `window.tuevSuedStop = true` stops gracefully after the current request; the download still happens.
- At the end (or after a stop): download all lines as `tuev_sued_pass01_<YYYY-MM-DD>_<HHMM>.jsonl` (Blob + temporary link). No other network traffic, no storage, no external scripts.
- Hard ceiling: never more requests than PLZ in the embedded list (max 200).

## 7. Python side

Folders per the convention (`1_scrapers/tuev_sued/{src,tests/fixtures,notebook,output,test_output}`), package mapping in `pyproject.toml`, row already in `scraper_status.md`. Modules (role names as in KÜS where they fit):

| Module | Job |
|---|---|
| `browser.py` | Render the snippet from the template; import a downloaded `.jsonl` (validate lines, copy into the run folder as `raw_pass01.jsonl`, …). |
| `parse.py` | Answer → records (one per `finaltsc` item) and per-query facts (PLZ, centre, count, last distance, sorted?). |
| `coverage.py` (or shared core) | Section 4 and 5: coverage, uncovered targets, fill-in plan. |
| `classify.py` | Section 8. |
| `run.py` | Modes and run folder handling; `plan`, `check`, `build`. |
| `simulate.py` | Fake API (Section 9). |

Run folder: one folder per monthly run, `output/YYYY-MM-DD_HHMM_full/` (opened by `plan`, reused by `check`/`build` of the same run; the convention's "never write into an existing run folder" applies to new runs). Holds `tuev_sued_passNN.js`, `raw_passNN.jsonl`, at the end the merged `raw.jsonl`, `query_log.csv` (rebuilt from the answers: pass, PLZ, centre, count, last distance, radius, timestamps from the summary lines), `open_targets.csv` if any, `run_summary.json/.md`, and the exports. **Convention gap to note in the status file:** a run spread over several manual passes, and the extra files `*.js` / `raw_passNN.jsonl`.

Modes: `simulated` (offline fake API, whole multi-pass flow, must PASS) · `sample` (a short pass of 3 PLZ: 80686 München, 24937 Flensburg, 01067 Dresden; never PASS; first live test of the snippet, and tells us whether a sparse area returns exactly 50) · `full` (plan/check/build as in Section 3) · `rebuild` (offline from `raw.jsonl`).

## 8. Records, scope, flags

- Dedupe by `tscId` (across all answers and passes); `location_id = TUEV_SUED-<tscId>`, `source_id = tscId`. Check that the same `tscId` always carries the same address in one run; differences → warning.
- `in_scope` = `huau == 1` and `tscStatus == 1` and country Germany (`countryId` 1206022065302211750256). `site_type = pruefstelle` for in-scope rows; anything else `unknown` + `needs_review` (keep the row).
- Flags (`needs_review`): `uo_notice` when a current notice contains "Prüfstelle der Überwachungsorganisation" (seen at Wolnzach; meaning not yet decided, so the row stays in scope); `company_not_500` (any other `companyId`, e.g. TÜV Hessen sites: report, never drop); `coordinate_far_from_plz` (> 25 km, shared enrich logic).
- Fields to export (candidate schema plus source extras): name built as "TÜV SÜD Service-Center " + `townPart` (or `town`), `street` + `streetnumber`, `zipcode` (text), `town`, `state` from GeoNames, `lat`, `lon`, `phone`, `email` (= `emailtsc`, personal data possible), `website` empty, `source_profile_url` from `tscUrl`, `booking_url` from `locationSign`, `opening_hours` flattened from `times[]`, `services` from the service flags (`…Service`, `huau`, `sga`, `dkgk`, `oldtimer`, …), extra columns `location_sign` (text, leading zeros!), `aspro_id`, `market_area_id`, `federal_state_id`, `heavy_vehicles` (`truckService` or `trailer_biggest`), `current_notices` (notices whose start ≤ run date ≤ end), `currently_closed`.
- Report all keys seen in `location` with fill rates in the run summary (schema not final).

## 9. Simulation and tests

- Fake API: ~350 synthetic sites (dense in Bayern/BW, some in Hessen, Sachsen, a few in the north), the same nearest-50 behaviour, server centre = PLZ centroid + random offset 0–5 km, answers in the real JSON shape. Variants for negative tests: a cap of 40 (must still PASS, the rule is generic), a radius limit of 80 km (must PASS with warnings or FAIL cleanly, never claim a false PASS), a dropped answer (→ uncovered targets, not PASS), a site 20 km from any PLZ centroid (→ warning).
- The simulated flow must find 100 % of the fake sites, report the number of passes and requests, and list nothing as uncovered.
- Unit tests: parsing (anonymised fixture made from the 80636 sample: fake emails/phones, jittered coordinates), radius rule, coverage, greedy plan, snippet rendering (the PLZ list and pass number end up in the JS; the hostname guard is present), import validation (bad line, missing summary, stopped run).

## 10. Open points

- Sample pass: does Flensburg return exactly 50? If fewer, there is a radius limit; the rule stays valid but the summary must say so.
- Wolnzach notice ("Prüfstelle der Überwachungsorganisation … Leistungen und Entgelte weichen ab"): meaning to be decided by Nikos; flagged until then.
- TÜV Hessen: do its HU stations appear in this finder (other `companyId`)? Decides whether TÜV Hessen needs its own adapter.
- Published count of own Service-Center to compare against (2015 interview: "über 300"; find a current figure).
- OpenStreetMap cross-check: separate step after the first PASS.
