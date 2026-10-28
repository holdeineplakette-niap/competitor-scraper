## GTÜ

**Status:** investigating. Recon complete. Parser, grid, fetch and enrichment built in Claude Code (`1_scrapers/gtue/src/adapter.py`, `2_core/grid.py`, `2_core/enrich.py`). 33 tests pass; simulated run found 1,200/1,200. Not yet verified against the live API. Key use approved; the full run follows once the sample run (7b) checks out.

### How the locator works

- Public page: gtue.de "Vertragspartnersuche". It embeds a finder hosted by **SmartMaps** at `gtue-finder.smartmaps.app` (Leaflet map).
- Data endpoint: `GET https://gtue-finder-bff.smartmaps.app/addresssearch/ByBranchesAndAddress`
  - Parameters seen: `Top`, `MaxRadius` (metres), `Branches=GTUE`, `City`, `Zip`, `Street`, `IsoCountryCode=DE`.
  - Needs header `X-Api-Key` plus `Origin`/`Referer` = `https://gtue-finder.smartmaps.app`. Without them: 401. Called from the gtue.de frame: blocked by CORS.
  - **The key is not stored in this file or in git.** It goes in `.env` only. Current value: see `api-keys.md` in the project knowledge base (not in the repo).
  - Response reports `IsRequestAuthenticated: false`, `IsAccessApproved: false`. The key looks like a public/anonymous finder key.
- Also seen: `/api/getSearchFilters?Active=true&Route=kfz` (returned 500 during recon). It may list filter and service values. Not investigated.
- The locate button (⊙) does **not** send coordinates to the search: the browser position goes to SmartMaps `geocode` (POST, GeoJSON point, `REVERSE_GEOCODE`), and the resulting address is then passed to `ByBranchesAndAddress`. **The search accepts addresses only (City/Zip/Street).** Street-level queries work (tested: Berlin, 10119, Torstraße).
- `mapaction` requests (on map pan/zoom) carry only `apiKey` and `provider=smartmaps`. Most likely usage tracking, not a data source.
- SmartMaps requests (`settings`, `geocode`, `mapaction`, map tiles) pass the key **in the URL** as `apiKey=`. Strip it before sharing URLs. `settings` uses the key before any other exchange, so the key is most likely embedded client-side (JS bundle). Not yet confirmed.

### Tested behaviour (2026-09-28)

| Test | Result |
|---|---|
| `Top=20`, radius 50 km, München 80686 | 20 returned · `MaxCount 45` |
| `Top=1000`, radius 50 km | 20 returned · `MaxCount 45`. **`Top` is capped at 20 by the server.** |
| `Top=1000`, radius 1,000 km | 20 returned · **`MaxCount 1157`**, i.e. the national total listed in the finder |
| Paging: `Page=2` | Empty result list (`MaxPage 1`). **No paging.** |
| Paging: `Skip=20` | Ignored (same first result) |
| **Sample run 7b** (script, 3 requests) | Works live. National check from 36251 Bad Hersfeld (1,000 km): **`MaxCount 1430`**, vs. 1,157 from München. 2 grid squares: `MaxCount` 35 and 58 → split. 60 unique locations: 47 `pruefstelle`, 12 `partner_office`, 1 `unknown`. |

**Finding: the national `MaxCount` is not Germany-only.** Both 1,000 km circles cover all of Germany but give different totals (1,157 vs. 1,430), so the API also counts locations abroad; `IsoCountryCode=DE` does not filter. Consequence: "unique IDs == national MaxCount" can't be a pass/fail check. **Completeness proof = every grid square complete** (`Count == MaxCount` and the circle covers the square from the actual centre). The national figure becomes informational; rows with `Country != "D"` are kept but out of scope.

**Geocoding:** `center_offset_km` in the sample: 3.01 (36251 Bad Hersfeld), 0.48 (79686 Hasel), **15.26 (88086 Immenstaad am Bodensee)**. The large offset was handled correctly (recomputed from the actual centre → split) but costs requests. Offsets > 5 km get listed in the run summary.

- `Paging.MaxPage` always reports 1, even when `Count < MaxCount`.
- **Built-in completeness check:** every response includes `MaxCount` (the total within the radius). A query is complete only if `Count == MaxCount`.

### Fetch strategy: adaptive grid (decided 2026-09-28)

1. Cover Germany's bounding box (approx. lat 47.2–55.1, lng 5.8–15.1) with ~100 × 100 km squares.
2. Query each square with the circle that fully covers it (radius = half the diagonal).
3. `MaxCount ≤ 20` → square is complete. `MaxCount > 20` → split into 4 and repeat.
4. Minimum square size ~500 m. If still `> 20` → log a coverage warning; never cut off silently.
5. Dedupe by `ProviderForeignKey`. Run check: unique IDs == national `MaxCount`.
6. Polite pacing (a few seconds between requests). Expected: a few hundred requests per run.

**Query points (decided after recon):** no coordinate query exists, so PLZ centroids from **GeoNames** (`DE.zip`, CC BY 4.0) are used as query points.
- Per square: query the PLZ nearest to its centre, with radius = distance to the farthest corner + 3 km.
- The response's `SearchData.Location` gives the **actual** centre the server used. A square counts as complete only if `Count == MaxCount` **and** the radius covers the square from that actual centre. Otherwise it is split.
- Minimum square size 1 km. If still incomplete → coverage warning.
- Grid logic lives in `2_core/grid.py`, independent of GTÜ, so other ÜOs with radius search can reuse it.

**Implementation notes (from the build, 2026-09-28):**
- Squares are laid out in the lat-51 projection, but **coverage distances use great-circle distance**. The projection understates east–west distances by up to ~8% in southern Germany, which exceeded the 3 km margin on large squares.
- **GeoNames cleaned:** ~2,500 company PLZs (Großkunden, e.g. 10875) dropped because they have company names as place and English state names. 8,309 regular PLZs remain. Noted in the README next to the CC BY 4.0 credit.
- Sea/abroad squares are skipped at every split level.
- The smallest square is 1.56 km (halving from 100 km).
- Safety limit: a full run stops after 2,000 requests.
- An unexpected response shape (API errors, missing `Paging`/`Location`) stops the run. Connection errors are retried like timeouts.
- Simulated/sample runs write to their own folders (`1_scrapers/gtue/test_output/<run folder>/`).
- **Expected full run:** 459–498 requests (simulation with PLZ-like spread, no warnings), ~30 min at 3.5 s/request.

**Known limitations:**
- Very dense spots: small squares still use circles of ≥ ~5 km radius (3 km margin + PLZ offset). More than 20 locations inside → coverage warnings and wasted splits. None expected at GTÜ's density (the realistic simulation had none; the artificial-cluster simulation had 165, all locations still found). If they appear in the real run → use street-level query points there.
- No resume: if a PLZ can't be geocoded or the run stops, it restarts from the beginning (raw files are kept).
- The national check (unique IDs == national `MaxCount`) can fail a complete run if the 1,000 km radius includes entries abroad or data changes mid-run. The country list in the summary helps diagnose it.

**Run modes:** `simulated` (no network, 1,200 fake locations, must find 100%) · `sample` (max. 3 live requests) · `full` (only with `confirm_full_run=True`; key use approved, run it after the sample run checks out). The notebook's 7c note still says "only once approved"; that note is now outdated.
**Politeness:** 3 s + jitter between requests, sequential, retries with backoff, stop on 401/repeated 429. Raw responses in `raw.jsonl` and the query log in `query_log.csv`, both in the run folder (`1_scrapers/gtue/output/<run folder>/`).

### Record structure (key fields)

| Field (path under `AddressItems[].BasicData`) | Meaning / note |
|---|---|
| `Identifiers.ProviderForeignKey` | **Stable GTÜ location ID.** Matches the `id` on the finder's list element and the gtue.de profile URL. → `location_id = GTUE-<id>` |
| `Address.CompanyName` / `CompanyNameAddon1` | Name. The addon often holds the owner ("Inh. …") but can also be a company or site name. **Personal data can appear in name, addon and email** (surnames in office names, personal email addresses). |
| `Address.CompanyNameAddon2` | Site label (e.g. "Prüfstelle Aubing"). The only field that tells an operator's sites apart when `CompanyName` is identical. → `name_addon2` |
| `Address.Street/Zip/City` | Street includes the house number. `HouseNo` is null. |
| `Geo.XCoord` / `Geo.YCoord` | **X = longitude, Y = latitude** |
| `Contact.Phone/Mobile/Email/Url/Url3` | `Url3` = gtue.de profile page |
| `MemoItems` (Type `OPENING_HOURS`) | Free-text opening hours with `<br/>` (12/20 in sample) |
| `MemoItems` (Type `CUSTOM`, Title `TerminUrl`) | Online booking link (3/20) |
| `MemoItems` (Type `JSON_INFO`) | Hidden `HiddenPoiInfos.Herkunft` (`E`/`G`) and `Stand` (data date) |
| `BranchListElements[]` | Code **`GTUP`** = "GTÜ-Ingenieurbüro mit amtlicher Fahrzeugprüfung vor Ort" (the "Prüfung vor Ort" icon in the UI). `GTUE` = "GTÜ-Ingenieurbüro" (all records). **The code is the primary signal**, the text a cross-check. |
| `ObjectListItems[].Headline` / `.Description` | Services offered, tagged `G`/`B`/`T` (guess: official inspection / appraisal / Technischer Dienst) |
| `BusinessData.DateLastChange` | **Batch timestamp** (identical on all 20 sample records, 2026-09-21; `Stand` 2026-09-20). **Not usable for change detection.** Our own snapshot diff is required. |

Sample fill rates (n=20): HU offered 19 · email 17 · phone 15 · "vor Ort" 13 · opening hours 12 · website 9 · booking link 3.

### Site-type mapping (assumption accepted by management, 2026-09-28)

| Herkunft | "Fahrzeugprüfung vor Ort" | Sample | Proposed `site_type` |
|---|---|---|---|
| `E` | yes (13/13) | 13 | `pruefstelle`: HU performed at this address |
| `G` | no (0/7) | 7 | `partner_office`: engineering office; inspections likely at workshops (PSPs) |

- The two signals agreed in 20/20 records. If they disagree → `unknown` + flag the row.
- Third signal (report only, not used for classification): ID prefix. All 13 `E` IDs start with `1`, all 7 `G` IDs with `00`. Checked in every validation report.
- Rough national extrapolation: ~65% E → ~750 Prüfstellen. That is consistent with GTÜ's published "700+ / ~800 own Prüfstellen".
- `in_scope = inspects_on_site`. `G` rows are kept in the full file but excluded from the Prüfstellen export.
- Optional sanity check: 6 of 7 `G` records still list "Hauptuntersuchung Pkw". A few could inspect on site without the flag; spot-check 2–3 websites (e.g. si-m.de) during the first full run.

### Known coverage gaps

- None known for in-scope sites (Prüfstellen).
- Out of scope, for reference: GTÜ's 10,400–11,000+ Prüfstützpunkte are not listed in the finder.

### Data-model notes

- Exports: CSV (`;` separator, `.` decimal, IDs as text) is the machine-readable master for HubSpot and the pipeline. An `.xlsx` copy (PLZ/IDs as text, coordinates as numbers) is for opening in Excel. **Don't save the CSV from Excel**: it drops leading zeros (IDs `00…`, Saxony PLZ `01…`).
- `Address.Country` is `"D"`, not `"DE"`.

- One operator can run several sites (e.g. SCHORER + WOLF: München-Aubing, Herrsching, Moorenweis). Keep one row per site; group by operator later for M&A.

### Validation plan

- Per query: `Count < MaxCount` → the query is incomplete → split it (grid) or page further.
- Per run: **PASS only if every grid square is complete** (0 coverage warnings, 0 squares not queried, 0 errored queries). The national `MaxCount` is reported for information only (it includes entries abroad).
- Per run: the Prüfstellen count should be plausible vs. GTÜ's published "700+ / ~800 own Prüfstellen".
- Alert if the number of German locations drops more than 10% vs. the last full run.

### Compliance

- The API key is GTÜ's (via SmartMaps), not ours. **Use approved by management on 2026-09-28** (applies to all ÜOs).
- Politeness is built in: sequential requests, 3 s + jitter, backoff, stop on 401 / repeated 429, ~500 requests per monthly run.
- Operator names and emails are personal data: keep outputs private (`.gitignore` covers `**/output/`, `**/test_output/`, `3_master_output/`, `4_reference/` and the personal-data fixture).

### Open issues / next steps

Done: paging test (not supported → adaptive grid) · coordinate query (none exists → GeoNames PLZ points) · map pan/zoom (`mapaction` = tracking, no data) · E/G meaning (accepted by management) · parser · fetch + grid + enrichment (33 tests, simulation 1,200/1,200) · key-use approval (management, 2026-09-28).

1. ~~No-overwrite fix, national check in sample, `center_offset_km`~~ → done (the no-overwrite protection worked in the sample run).
2. ~~Sample run 7b~~ → done, see "Tested behaviour".
2a. **Fix pending in Claude Code:** new run-status logic (PASS = all squares complete; national check informational; `Country != "D"` → out of scope); list offsets > 5 km; make notebook 7b/7c self-contained; update the 7c note.
2b. Inspect the 1 `unknown` row from the sample run: single case or pattern?
3. **Full run 7c** (set `confirm_full_run=True`), after 2a/2b. Then check: all squares complete; counts per country; ~700–800 German Prüfstellen; no or few coverage warnings; needs-review list; spot-check one region (e.g. Nürnberg) against Google Maps.
4. Only then: set the GTÜ adapter status to **working** and record the verified count.
5. Later: find where the key comes from (JS bundle vs issued by a request). If it's in a bundle, the scraper reads it from the page each run, so a rotated key doesn't break the monthly run. Until then a 401 stops the run with a clear message.
