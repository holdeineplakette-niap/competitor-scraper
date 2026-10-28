# GTÜ grid: what went wrong on 2026-09-29 and what changed

Date: 2026-09-29 (updated for the Niederdorla reference query, rule C4 and early stop E). Affects `2_core/grid.py`, `1_scrapers/gtue/src/adapter.py`, `1_scrapers/gtue/notebook/gtue_control.ipynb`, the tests in `1_scrapers/gtue/tests/` and `2_core/tests/`.
This document does not replace `scraper_status.md`, which still needs its own update (section 8, item 1).

## 1. What happened

The first GTÜ full run (notebook 7c) stopped at the 2,000-request safety limit.

| | |
|---|---|
| Squares | complete 1,058 · split 941 · warning 0 · error 1 · skipped 85 |
| Collected | 1,429 unique IDs, all `Address.Country` = D (national MaxCount 1,429) |
| Result | 855 Prüfstellen in scope, 483 partner offices, 91 `unknown` (needs review) |
| Files | raw responses in `raw.jsonl` and query log `query_log.csv` of run folder `1_scrapers/gtue/output/2026-09-29_1019_full/` |

Findings from the query log and raw files (no live requests):

- **The "error" was the limit itself.** Query 2001 was never sent; `RequestLimitReached` is logged as an error row. All 2,000 real requests returned HTTP 200.
- **"Not queried 0" was wrong.** The abort was raised inside the query call, before the counter was set. 1,743 squares were still queued (1,728 to be queried, 15 to be skipped).
- **All IDs were found by request 952.** The other 1,048 requests added nothing: 16,652 entries were returned for 1,429 unique IDs, and 92% of queries added no new ID.
- **Cause: the geocoded centre.** The server geocodes each PLZ to its own centre, not the GeoNames centroid. 928 of 2,000 queries had a centre more than 3 km (the radius margin) from the centroid, 736 more than 5 km, up to 176 km (PLZ 03202). The same PLZ always gets the same centre.
- **Coverage-only splits.** 612 of the 941 splits happened although Count == MaxCount (median MaxCount 1, 259 with 0): the circle just didn't reach the square from the actual centre. Splitting doesn't help: 502 of the 810 logged children reused the parent's PLZ and hit the same offset.
- **Continuing unchanged would have needed about 4,450 requests** (replay, extrapolated beyond request 2000) and would still have ended with roughly 2,000 coverage warnings.
- **The server's search area is roughly a square window, not a circle.** Returned entries lie up to 1.416 × the requested radius from the centre, but never more than about 1.09 × along either axis. Not used by the changes below; see section 8.

## 2. The old national MaxCount was not a reliable total (radius-cap check)

The old national checks were 1,000 km queries from München and from Bad Hersfeld. Offline test on the collected coordinates:

| | München 80686 (2026-09-28) | 36251 Bad Hersfeld (2026-09-28 / 09-29) |
|---|---|---|
| MaxCount reported by the server | **1,157** | 1,430 / 1,429 |
| Collected locations within 1,000 km of the actual centre | **1,429 of 1,429** | 1,429 of 1,429 |
| Farthest collected location | 752 km (Flensburg) | 437 km |
| Circle radius that holds exactly the reported count | **≈ 512 km** | not applicable (all inside) |

The München count is not the total: the server applies a smaller radius than requested (cap confirmed at 500 km on 2026-09-29, per Nikos). The earlier explanation "the API also counts locations abroad" is not supported: none of the 1,429 collected locations is abroad. That is why the reference query below uses exactly the capped radius from a centre whose 500 km circle is known to cover Germany.

## 3. Reference query and rule C4

### Reference query (replaces the national check everywhere)
Zip 99986, City Niederdorla, MaxRadius 500,000 m, Top 20, Branches GTUE, IsoCountryCode DE. All values are constants (`REFERENCE_*`, `SERVER_RADIUS_CAP_KM`) in one block in `1_scrapers/gtue/src/adapter.py` with the reasoning: server cap 500 km; expected actual centre 51.15844 N, 10.44595 E; the German border is at most 454 km from that centre (List/Sylt), so the circle covers Germany.
- **Centre sanity check.** If `SearchData.Location` is more than 5 km from the expected centre, the run stops with `ReferenceCentreError` ("the coverage proof depends on this centre"). The end query and every early-stop recheck are checked too.
- **When it is queried.** Full run: at the start and at the end (both MaxCounts logged with timestamps: new log column `time`, log statuses `reference_check` / `reference_end`). Sample run: at the start only; a sample makes 5 requests in all (`SAMPLE_MAX_REQUESTS`): the reference query, the cap probe (2) and 2 grid squares. The probe runs in every sample so its result is on record before a full run; the early stop itself stays off unless `early_stop=True`, and equal probe counts are only reported. Reference and probe queries are always live, never served from the cache.

### Status rule C4
PASS needs both:
- **(a)** every grid square complete: 0 coverage warnings, 0 squares not queried, 0 errored queries;
- **(b)** N_circle == the reference MaxCount, N_circle = collected unique IDs whose coordinates lie within 500 km (great circle) of the reference's actual centre. If the reference MaxCount differed between start, rechecks and end, N_circle may be anywhere in that range.

| Outcome | Result |
|---|---|
| N_circle == MaxCount, 0 rows with Country != D inside the circle | `PASS (grid complete + count match)`; the MaxCount is the German total |
| N_circle == MaxCount, some rows with Country != D inside the circle | PASS with a note in the label; German total = rows with Country == D |
| N_circle < MaxCount | FAIL, the gap is reported |
| N_circle > MaxCount | FAIL, investigate (data change, or the server's count or cap changed) |

Every row with Country != D has `in_scope = false` whatever its site type (missing country counts as abroad). Not evaluated (b): after an abort (already FAIL) and in sample mode (a sample never collects everything).

The run summary prints: both reference MaxCounts with timestamps, N_circle, abroad rows inside and outside the circle, the German total, the grid completeness figures (squares, open squares), and the centre offsets above 5 km. A full run that is not a PASS writes its files with `grid-incomplete` (squares open) or `count-mismatch` (no square open) in the names; the open squares are listed in `open_squares.csv` in the run folder.

### Early stop (rule E), `run(early_stop=False)` by default
Definition (Nikos): the grid stops once N_circle reaches the reference MaxCount; the run then reports PASS even with open squares or coverage warnings. Errors and aborts still fail. It replaces criterion (a); criterion (b) still applies. Safeguards:
- Only N_circle counts, never all collected IDs (rows abroad beyond 500 km must not trigger the stop).
- When N_circle reaches the start MaxCount, the reference is queried once more (live). The run stops only if N_circle still equals it; if the reference changed, the run continues against the new value. N_circle above the MaxCount stops the run and it FAILs (b).
- Cap probe at run start (2 extra requests): MaxCount for Zip 80686 / München at 455 and at 460 km. Equal counts mean the server cap has dropped below what covers Germany from Niederdorla: early stop is disabled, the full grid runs, and the summary says so.
- Labels: `PASS (grid complete + count match)` versus `PASS (early stop, count match)`. With an early stop the squares not queried and every coverage warning are still listed (summary and CSV).
- A failed recheck or a moved reference centre switches the early stop off for the rest of the run.
- Off by default; Nikos turns it on after the first full run (`EARLY_STOP` in notebook 7c).

## 4. Request savers

### A. Re-query from the actual centre (`grid.run_grid`, `requery_uncovered=True`)
If Count == MaxCount but the radius doesn't cover the square from the server's centre, the same point is queried once more with radius = far corner from the actual centre + 3 km margin, instead of splitting into 4. The first query is logged as status `requery`.

### C. Same-day response cache (`gtue.ResponseCache`, `run(use_cache=...)`, on for `full`)
A query already made today is answered from disk: key = (PLZ, city, radius in m), filled from the query logs of earlier runs of the same date and from the run's own answers. Cache hits are not requests and don't count against the 2,000 limit; they are logged with `cached = True`. Other dates are never used. The reference and probe queries bypass the cache.

## 5. Fixes and tools from earlier the same day
- **Pending counter** (`grid._record_pending`): `squares_not_queried` is now set when a run aborts, and counts only squares that would still be queried; their bounds are kept (`GridResult.pending_squares`). It is a lower bound: children of unqueried squares don't exist yet.
- **Rebuild from raw** (`gtue.rebuild_from_raw`, notebook 7d): parses the `raw.jsonl` of a run folder, dedupes, classifies, exports into a run folder of its own (`<date>_<time>_rebuild`, next to its source run in `1_scrapers/gtue/output/`) as `gtue_*_<date>_grid-incomplete.csv/.xlsx`. No network, never overwrites. If the first raw file is a reference query it also prints N_circle against its MaxCount; runs from before the reference query (like 2026-09-29) get no count check. The 2026-09-29 files were produced this way and are not a verified complete crawl.

## 6. Expected effect (offline replay of the 2026-09-29 log)

The replay reproduces the logged run exactly (1,058 complete, 941 split); queries it hasn't seen are answered by a model of the server, so these are estimates. Ranges cover square-window models at 0.95–1.05 and a circle model. The reference queries (2 in a full run, plus 2 for the cap probe with early stop) are extra.

| Scenario | Live requests | Squares still open at minimum size |
|---|---|---|
| Unchanged grid, whole grid | ~4,450 | ~2,000 |
| A only, whole grid, from scratch | 1,750–1,950 (fits the 2,000 limit, tight) | 175–245 |
| A + C, re-run on the same date after the 2026-09-29 run | ~240–270 new | 175–245 |
| A + early stop, from scratch | ~860–900 | not needed for PASS (listed) |
| A + avoiding PLZs whose geocode is more than 5 km off (idea, not built) | 1,300–1,440 | 26–70 |

Without the early stop, PASS under rule C4 needs every square complete, which A + C alone does not reach: 175–245 squares stay open at minimum size (more than 20 locations within reach of a 1.5 km square). About 165 of the 214 (middle model) come from 12 PLZs whose geocode is 5–37 km away, chiefly 15837 Baruth/Mark (52 squares) and 15838 Kummersdorf-Gut (49 squares); about 49 are in Berlin. That is why the early stop matters for a PASS.

## 7. Tests
98 tests pass (`pytest`), no network: `test_grid.py`, `test_gtue_parse.py`, `test_enrich.py` (existing, adapted where they pinned the old national check), `test_gtue_rebuild.py`, `test_gtue_savers.py` (A, C, open squares), `test_gtue_reference.py` (reference query, centre check, N_circle, the four C4 outcomes and start/end tolerance, early stop, cap probe). `tests/maxcount_sweep.py` (live requests) moved to `diagnostics/` (now in `9_archive/`).

## 8. Open items
1. **`scraper_status.md` needs updating** (not edited here). It still says the 1,157 vs 1,430 difference is locations abroad, and the "Confirmed (2026-09-29)" / C1–C4 sections referred to in the task were not in the file when this work was done (dated 2026-09-28 17:23).
2. **Square search window** (section 1): if confirmed by a few live requests, a radius and coverage test for a square window would cut requests further. If the window were a circle, that test would be wrong, so nothing was built on it.
3. **Avoiding far-off PLZs and street-level query points in dense spots** would shrink the open squares (section 6); not built.
4. **Limit row logged as `error`.** `run_status` reports "1 queries errored" for a plain limit hit. Consider a separate status.
5. **Aborted reference query.** If the reference query itself fails, `squares_not_queried` stays 0 although no square was queried.
6. **In-scope count.** 855 Prüfstellen (60% of entries) is above the plan's rough extrapolation of about 750; worth a look before the number is reported.
