# KÜS: recon notes

Recon date: 2026-10-05. Adapter: `1_scrapers/kues/src/` (`fetch.py`, `parse.py`, `classify.py`, `run.py`, plus `simulate.py`, `report.py`, `recon.py`, `recon_analysis.py`), shared coverage code `2_core/radius_cover.py`, shared export code `2_core/export.py`, control notebook `1_scrapers/kues/notebook/kues_control.ipynb`. Live state per ÜO: `scraper_status.md`. Raw recon files (contain personal data, git-ignored): `1_scrapers/kues/test_output/2026-10-05_1607_recon/` (22 live requests: 4 page/script fetches, 18 API calls); saved München response: `4_reference/api_samples/kues/partnersuche_80636_2026-10-05.json`.

## 1. How the locator works

- Public page: `https://kues.de/standortsuche/` (WordPress, Leaflet map). It loads three theme scripts (`.../kues-theme-2023/kuesmap/js/kuesMap.js`, `kuesMapList.js`, `partnersuche.js`); `partnersuche.js` builds the requests.
- Data endpoints (JSON, host `webservice.kues.de`, no key, no login, no cookie needed):
  - `GET https://webservice.kues.de/api/kues/partnersuche?plz=<PLZ>`: every Partner within a fixed radius of the PLZ.
  - `GET https://webservice.kues.de/api/kues/partnersuche-kues-technik?plz=<PLZ>`: the same shape, layer "KÜS Technik (EBE Begutachtungen)" (see §3).
  - `GET https://webservice.kues.de/api/kues/plzdaten?term=<2+ chars>`: PLZ autocomplete, answers `[{"plz","ort"}]` only (no coordinates). Not used.
- The page merges both lists and tags each record `source = "kues"` or `"technik"` (two map layers, each with "Prüfung vor Ort möglich ja/nein"). It also sends `latitude`/`longitude` (browser geolocation); see the table below: they are ignored.
- Minimum request: **a plain GET with no headers of our own and no cookies works** (HTTP 200). The server sets a load-balancer cookie (`cookiesession1`) that is not needed. The adapter sends `Accept: application/json` and a browser User-Agent anyway.
- The response is a **plain JSON array**, one object per Partner, sorted by `entfernung`. No total, no count field, no paging, no `isNextLocation`. An unknown or empty PLZ answers `[]` with HTTP 200.
- What the page does with `keinestrasse = 1`: it treats such an entry as "address disabled" (no marker unless deactivated markers are shown, no street in the list) and shows **"Prüfung vor Ort nicht möglich" even if `pruefungvorortmoeglich` is 1** (`kuesMapList.js`: `pruefungvorortmoeglich > 0 && !keineAdresse`). So the page itself does not offer an on-site inspection for such entries.

## 2. Tested behaviour (recon 2026-10-05)

| Test | Result |
|---|---|
| Bare headers, PLZ 80636 | 200, 83 records, identical to the browser capture saved earlier (same ids, same order, 0 changed fields). Re-checked by the live sample run (same result). |
| Radius | Maximum `entfernung` per response: 49.76 (80636), 46.24 (66111), 49.25 (44135), 49.90 (85221), 49.85 (54290), 36.47 (10115 Berlin), 35.20 (17213, 3 records). **Fixed radius of 50 km.** `entfernung` equals the great-circle distance from the server centre to the site within 0.06 km (294 located sites). |
| Count | 3 (17213 rural Mecklenburg), 27 (66111 Saarbrücken), 38 (10115 Berlin), 76 (85221), 83 (80636), 98 (44135 Dortmund). **Varies, no fixed N.** |
| Count cap | None seen: 44135 with `km=100` returned **231** records, 80636 with `km=90` 127, with `km=200` 135. Not excluded for other places; rule D of the coverage check watches for it. |
| Hidden radius parameter | **`km`** sets the radius. Candidates sent with different values (radius 60, umkreis 70, entfernung 80, **km 90**, distance 55, maxdistance 65, range 75): max `entfernung` 89.85 → `km`. `km=200` gives 99.8 km → **capped at 100 km**. The page never sends `km`; the adapter does not either unless `RADIUS_KM` is changed. `limit`/`max`/`top` had no effect. |
| Coordinates | `latitude`/`longitude` without `plz` → `[]`. With `plz` → ignored (80636 + Berlin coordinates = same 83 records, same centre). **The PLZ is the only query input.** |
| Single request for all of Germany | **No.** Largest radius is 100 km (cap), no count cap seen, but no parameter covers the country. |
| Server centre (`inbreite`/`inlaenge`) | Always the same for the same PLZ (80636: 6 responses, same centre). It is the server's own PLZ table (rounded coordinates), not the GeoNames centroid: offsets 3.21 (80636), 5.22 (17213), 1.43 (66111), 0.72 (44135), 0.75 (85221), 0.59 (54290), 2.02 (10115) km. |
| Unknown PLZ (00001) | `[]`, HTTP 200. An empty answer carries no centre. |
| `plzorder` | **1 if the record's own PLZ equals the queried PLZ, else 2** (66111: 1 record with 1; 85221: 2 records with 1; every other record 2). Not used. |
| Outside Germany | None: 0 of 413 located sites outside Germany's box, 0 PLZ unknown to GeoNames (incl. 66111 Saarbrücken and 54290 Trier near the border). The adapter still checks every row (country DE only if the PLZ is a GeoNames PLZ and the coordinates fit). |
| Site vs its own PLZ centroid | Median 1.6 km, p95 6.3, p99 8.4, **max 12.8 km** (413 sites). |
| Technik vs main list (80636, 66111) | The technik list is a **subset** of the main list: 23 of 83 and 3 of 27 records, all with identical content, none technik-only. |
| `km` on the technik endpoint (80636, `km=100`) | **Honoured**: 42 records, max `entfernung` 94.5 km; all 42 are in the main list's 100 km answer for the same PLZ with identical content, none technik-only. The subset property holds at 100 km too. |
| `km=1000` from Niederdorla 99986 (Germany's centre) | **Capped at 100 km, not national**: 30 records, max 96.0 km (central Germany is sparse). Server centre 3.21 km from the GeoNames centroid. Together with `km=200` at 80636 (99.8 km) the cap is 100 km. |
| Other regions' technik lists | Not tested (80636 and 66111 only). |

Live requests so far: 22 (recon folder `2026-10-05_1607_recon`: 4 page/script fetches, 18 API calls) + 2 (sample run `2026-10-05_1635_sample`) = 24 of a session budget of 25.

## 3. Fields

| Field | Meaning / note | Column |
|---|---|---|
| `adressid` | Stable integer ID, unique per record. | `source_id` (text), `location_id = KUES-<adressid>` |
| `lizenznehmer` | **Licence code** (e.g. I0500), not an owner. One company holds several: "SV TEAM AG" runs ~10 sites under I0119 and I0500. | `kues_licensee`, `licensee_site_count` |
| `name`, `zusatz` | Name and addon. About 20 of 83 have `name` = "Herrn" / "Herrn Dipl.-Ing. (FH)" with the person in `zusatz`. **Personal data.** | `name`, `name_addon`, `display_name` |
| `strasse`, `plz`, `ort` | Address. `strasse` null when `keinestrasse = 1`. | `street`, `zip` (`strip().zfill(5)`), `city` |
| `lat`, `lon` | Strings; null when there is no street. | `lat`, `lng` (floats) |
| `tel1`, `fax`, `email`, `http` | Contact. `fax` is not exported. `email` can hold two addresses ("a; b") or a leading space. **Personal data.** | `phone`, `email`, `website` |
| `pruefungvorortmoeglich` | "0"/"1": the engineer inspects at this address (see §5). | `pruefung_vor_ort`, `site_type`, `in_scope` |
| `keinestrasse` | "0"/"1": no street, no coordinates (7 of 83). Still has an `entfernung`. | `location_precision` = `plz_only` |
| `entfernung`, `inbreite`, `inlaenge`, `plzorder` | Query-dependent (distance from the server centre, the centre, see above). Ignored when the same `adressid` comes back twice and the content is compared. | not exported |
| `partneralias` | Not seen in any recon response; the page would link `https://partner.kues.de/<alias>` when `http` is empty. The parser handles it. | `source_profile_url` |

Extra columns of the adapter: `display_name` (name, or name + zusatz when `name` is only a salutation; the salutation word is dropped), `location_precision`, `kues_list` (`partnersuche` / `kues_technik` / `both`), `name_class`, `licensee_site_count`, `licensee_has_inscope_site`, `same_address_count` (same normalised street + PLZ, never merged), `plz_distance_km`, `is_hdp_site` (renamed from `is_liria_site` on 2026-10-08; always empty), `doubt_flags`.

**Dedupe:** by `adressid` only, first seen wins. Never by address (Karl-Benz-Straße 12 in 85221 Dachau holds a Prüfstelle and an Ingenieurbüro). The same `adressid` with different content in two responses (query-dependent fields aside) is counted and listed in the run summary (`content_conflicts`).

**Legacy hook (not used since 2026-10-07):** HolDeinePlakette's own sites now come from the HDP scraper and are removed by the aggregator (`2_core/hdp_removal.py`), so this file is not needed. The code still supports the optional file `4_reference/hdp/hdp_sites.csv` (not created by the code; the column stays empty without it). Columns `kues_licensee`, `adressid`, `address`; any may be empty, a row with all three empty is ignored. A KÜS row would be an own site when **all non-empty columns of at least one row match**: `adressid` equals `source_id`; `kues_licensee` equals the licence code; `address` (e.g. "Teststraße 4 81671") is contained in street + PLZ + city (case, spaces and "straße/str." ignored).

## 4. Coverage design (fixed radius, no count field)

**Production setting (2026-10-05): `km=100`, `cover_km` 50** (`run.default_cfg()`; the proportions below scale with the radius). Two full crawls, 50 km (the page's own request) and 100 km, found **exactly the same 1,102 records** with identical content (see "Full-run results" below), so the 100 km crawl is used from now on: 91 queries / 182 requests / ~13 min instead of 368 / 736 / ~55 min. The 50 km design (cover 25) remains selectable with `RADIUS_KM = 50` and is tested.

The finder answers "everything within 50 km of this PLZ" (100 km with the hidden `km` parameter) and says nothing about a total, so completeness cannot be read from the response. The adapter proves it with four rules on the **actual server centres** (never the GeoNames centroids). Generic code: `2_core/radius_cover.py` (reusable for any fixed-radius locator).

| Rule | Check | Numbers (config block `CoverConfig`) |
|---|---|---|
| **A coverage** | every regular PLZ centroid (GeoNames, 8,309, same cleaning as GTÜ) lies within `cover_km` of a server centre | `cover_km` 25 |
| **B fringe** | every collected located site lies within `fringe_km` of a centre, else its own PLZ is queried | 45 km |
| **C consistency** | every centre within `consistency_km` of a collected located site must have returned it; a miss = cap, flaky server or changed radius → FAIL. Sites without coordinates are skipped (counted separately); sites 48–50 km away are in the boundary zone | 48 km |
| **D cap detector** | a response with ≥ 500 records, or the run's largest count (≥ 40) repeated in ≥ 3 responses = cap signature → FAIL | 500 / 40 / 3 |

Plan: a hexagonal lattice over Germany's bounding box (spacing `0.85 · √3 · cover_km` = 36.8 km), each point snapped to the nearest regular PLZ (points with no PLZ within 15 km, i.e. sea or abroad, skipped), then a **fill-in loop**: PLZ centroids that still break rule A (query the uncovered PLZ that would cover most of them) and sites that break rule B (query the site's own PLZ), at most 100 fill-in queries. An empty answer carries no centre and does not count as coverage; an uncovered PLZ that was itself queried and answered empty is reported as `assumed_empty` (warning, e.g. islands such as Helgoland), not as a rule A failure.

**Why `cover_km` = 25.** A site found by a centre at most 25 km from its PLZ centroid and at most `d` km from that centroid is at most 25 + d km from the centre; the radius is 50. Measured `d` over 413 sites: p99 8.4 km, max 12.8 km (rural PLZ can reach ~20 km, 25 + 20 = 45 < 50, so there is a margin). 30 km would work for the measured maximum (25 % fewer queries: 246 instead of 352) but leaves no margin for a 20 km site; kept at 25, reconsider after the full run when rule B/C results are known.

| Variant | Planned | Expected incl. fill-in (simulated, offsets like the real ones) | Requests (both lists) | Runtime at 3 s + jitter + ~0.8 s answer |
|---|---|---|---|---|
| 50 km, cover 25, both lists (measured: 341 + 27 fill-in = 368 queries, 736 requests, 55 min) | 341 | 352 | 704 | ~50 min |
| 50 km, cover 25, main list only (`FETCH_TECHNIK = False`) | 341 | 352 | 352 | ~25 min |
| 50 km, cover 30, both lists | 238 | 246 | 492 | ~35 min |
| **Production: `km=100` (hidden, capped), cover 50, both lists** (measured: 84 + 7 fill-in = 91 queries, 182 requests, 12.5 min) | 84 | 91 | 182 | ~13 min |

The request ceiling of a full run is 900 (`fetch.FULL_MAX_REQUESTS`: 341 + at most 100 fill-in, two lists each). The `km=100` variant is built in (`RADIUS_KM = 100` in the notebook; the proportions scale) and works for **both lists** (`km` is honoured by the technik endpoint, §2), so it can keep `FETCH_TECHNIK = True` (~182 requests, ~13 min). It is **the default since 2026-10-05**: the cross-check passed (below). The page itself never sends `km`; if KÜS ever removes the parameter, rules A–D fail loudly (rule C: a changed radius) and `RADIUS_KM = 50` is the fallback.

**Full-run results (2026-10-05).** `1_scrapers/kues/output/2026-10-05_1704_full` (50 km) and `..._1812_full` (100 km), both **PASS** with rules A–D ok on both lists. 50 km: 736 requests, 340 server centres, 20 PLZ in the east answered empty (note), largest answer 135 records. 100 km: 182 requests, 91 centres, largest answer 258 records, max `entfernung` 99.94 km. **Result of `report.compare_runs`: 1,102 records in both, 0 ids in one run only, 0 content differences.** In scope 582 (flag 1), 520 partner offices, 0 unknown, 13 needs review, 46 PLZ-only; 350 records in both lists, 0 technik-only. Weak-evidence share 157 of 582 (27 %), as in the 449-site pattern analysis. Two server centres lie far from the GeoNames centroid (PLZ 14778: 14.3 km, 12559: 21.1 km); the planner handles that through rule A fill-in.

**The technik list.** It is a subset of the main list in both regions tested (and at 100 km in 80636), so it adds no locations, only the label `both` (partner also does EBE appraisals, `kues_list`). The adapter queries it at every point by default (doubles the requests); a technik-only record would be kept and labelled `kues_technik`, and rules C and D run on that list too. Set `FETCH_TECHNIK = False` to skip it.

**Run status.** PASS (full) = no error or abort, no planned query missing, rules A–D hold (technik list: C and D), drop check ok. Anything else is FAIL and the exports carry `grid-incomplete` (aborted, planned queries missing, rule A or B) or `unverified` (rule C/D or drop alert) in their names. Sample, recon and simulated runs are never PASS (simulated runs report PASS for the fake API only). The drop check compares the in-scope count with the newest earlier **passing** full run (alert below −10 %).

**Warnings in the run summary (not failures):** empty responses (and whether sites are known nearby), server centres more than 8 km from the GeoNames centroid, responses with ≥ 20 records whose farthest site is nearer than 30 km (radius shrank?), any `entfernung` above 51 km (radius grew), responses whose records disagree about the centre, content conflicts, technik-only records.

## 5. Classification

**Decision (management via Nikos, 2026-10-05):** only actual Prüfstellen are in scope; PSPs are out. `pruefungvorortmoeglich = "0"` means the entry inspects at PSPs, not at its own address → out of scope.

| Flag | `site_type` | `in_scope` | Evidence |
|---|---|---|---|
| "1" | `pruefstelle` | true | `vorort:1` |
| "0" | `partner_office` (same category as GTÜ's offices without on-site inspection) | false | `vorort:0` |
| missing / other | `unknown` | false | `vorort:invalid`, needs_review |

Everything is collected; filtering is only the `in_scope` column (`kues_all_<date>.csv` vs `kues_pruefstellen_<date>.csv`). Conflicts are flagged (`doubt_flags`, `needs_review`), never used to overrule the flag:

| Flag | Meaning |
|---|---|
| `name_pruefstelle_vorort0` | name/addon says Prüfstelle / Prüfzentrum / KÜS-Team / Kfz-Prüf… but the flag is 0 |
| `vorort1_no_address` | flag is 1 but there is no street or no coordinates (the page itself shows "Prüfung vor Ort nicht möglich" for `keinestrasse = 1`) |
| `vorort_invalid` | flag missing or not 0/1 |
| `name_psp_like` | in scope, but the name has a workshop / dealer / tyre / brand word (never seen so far) |

Info flags (also in `doubt_flags`, **no `needs_review`, no scope effect**), added after the pattern analysis below: `vorort1_weak_evidence` (in scope, but no station word in the name, no website, no business email), `vorort0_strong_evidence` (out of scope, but two of those three), `contact_missing` (in scope, no phone, email or website), `same_address_as_inscope_site` (out of scope, same normalised street + PLZ as an in-scope row), `shared_address_in_scope` (several in-scope rows at one address). Derived columns: `name_class` (herrn / pruefstelle / team / kues_other / ingenieurbuero / sv_buero / person_title / company / other, information only) and `licensee_has_inscope_site` (another row of the same licence code is in scope). "Business email" = an address that is not at a consumer provider (`classify.PRIVATE_EMAIL_DOMAINS`); kues domains count as business.

## 5b. Pattern analysis (2026-10-05)

Done by an independent subagent on the München sample (83 records) and the union of the recon responses (**449 distinct sites**: 222 with flag 1, 227 with flag 0; Bayern, NRW, Berlin/Brandenburg, Saarland, Trier). Report with names (git-ignored): `1_scrapers/kues/test_output/2026-10-05_1635_sample/pattern_analysis.md`. Spot-check list for Nikos (19 rows, columns `hu_on_site` and `notes` empty): `4_reference/ground_truth/kues_spotcheck_2026-10-05.csv`. **Nothing here is ground truth** and the pool is not a random sample of Germany (union of radius queries); all counts need the full run.

| Finding | Pool of 449 | Handling |
|---|---|---|
| The flag is internally consistent and plausible, not proven. Name says Prüfstelle/Prüfzentrum: 52 of 55 have flag 1. Private email: 52 % of flag 0 vs 19 % of flag 1 | | rule unchanged; the spot check decides |
| `keinestrasse = 1`: 22 records, **all flag 0**, all without street and coordinates; flag 1 always has street and coordinates | 0 exceptions | cut, no review (the page does the same); `vorort1_no_address` never fired |
| Weak side is flag 1: 158 of 222 (71 %) have no station word in the name (82 are "Ingenieurbüro"); 59 have no station word, no website, no business email | 59 | info flag `vorort1_weak_evidence` (not a cut) |
| Name conflicts on the flag-0 side are rare: 4 of 227 (e.g. adressid 55830, zusatz "Kfz-Prüfstelle", no flag-1 sibling) | 4 | `name_pruefstelle_vorort0` + needs_review (existing) |
| 42 flag-0 records share a licence code with a flag-1 site; 6 are near-identical twins less than 2 km from a flag-1 record | 42 / 6 | column `licensee_has_inscope_site`; twins are a decision (Q2) |
| Shared addresses: 11 groups, 23 records (4 groups flag 1 only, 5 mixed incl. Dachau, 2 flag 0 only) | 11 | info flags; **never dedupe by address** |
| Multi-site licensees (SV Team AG 10 sites, Has4 Technologies 7 NRW sites, SVZ, PWE, KPN) are genuinely separate addresses | | never merge by licence code |
| **No PSP-like names** (workshop, dealer, tyre, brand) among 449 records: the finder lists licensees (engineers' offices and stations), not the PSPs | 0 | no name-based cut; watch flag `name_psp_like` (needs_review) |
| Technik list carries no scope information (same flag semantics, subset in both regions) | 26 | attribute `kues_list` only |
| 16 flag-1 records have no phone, email or website | 16 | info flag `contact_missing` |
| **7 records (licence I1122, Has4 Technologies Nürnberg GmbH, flag 1, NRW) use the email domain of HolDeinePlakette** | 7 | HolDeinePlakette's own sites; handled by the HDP scraper and the aggregator's own-site removal, `hdp_sites.csv` (§3) is not needed |

The flag-1 share varies by region (40 % around 80636, 47 % Dortmund, 52 % Saarbrücken, 66 % Trier), so the national ratio cannot be extrapolated.

**Needs a decision (Nikos / management), nothing of this is implemented:**
1. Is a flag-1 "Ingenieurbüro / SV-Büro / Herrn" with no further station evidence a Prüfstelle for us, or do we require evidence (59 of 222 have none)?
2. Flag-0 records whose licence code has a flag-1 site (42): drop, or keep as extra contact?
3. Company granularity for HubSpot: per `adressid`, per address (collapses the Dortmund duplicate and Kairies-type groups), or per licence / operator?
4. Flag-0 records with two evidence signals and no flag-1 sibling (24, e.g. 22878, 17079): trust the flag, or sample first?
5. Name says Prüfstelle but flag 0 (4, clearest 55830): accept the loss or review case by case?
6. KÜS-owned units (Losheim GmbHs: flag 0; KÜS Mobility GmbH stations 47232, 28224: flag 1): exclude from a competitor list or treat as ordinary stations?
7. Flag-1 records without any contact data (16): import anyway?
8. Technik list: keep the second query (doubles the requests, gives `both`) or skip it once the full run confirms the subset?
9. Chains (Has4, SVZ, PWE, KPN): separate Prüfstellen (yes by address) or grouped for a competitor map? Has4 (licence I1122) is probably HolDeinePlakette's own; the HDP removal matched 17 of the 19 HDP sites to KÜS rows (see the table).

What the full runs confirmed (2026-10-05): technik is a subset nationally; no other flag values (0 unknown); 4 records "country unverified". Still to read from the full export: flag-1 ratio by region; `keinestrasse = 1` stays inside flag 0 and flag 1 always has an address (1 record flag 1 without address, flagged); no PSP-like names; licensee and address counts (lower bounds here because of radius truncation); small name-class cells (KÜS-Team 4, Dipl.-Ing. 4); the email-provider list is a heuristic.

## 6. Published figures

- "Mit **über 1.800 Prüfingenieurinnen und Prüfingenieuren** deutschlandweit verfügt die KÜS über ein starkes Netzwerk" (https://kues.de/geschaeftskunden/fahrzeugueberwachung/, checked 2026-10-05). People, not sites: no direct check.
- "Rund 1.600 Prüfingenieure an rund **400 eigenen Prüfstellen**", "8.000 Prüfstützpunkte": **seen only in a search-result snippet of kues.de; the page was not found** (checked press release of 2016, `/ueber-kues/verein/`, `/ueber-kues/firmenverbund/`, `/ueber-kues/historie/`, the HU pages: none contains it). Unverified. If it holds, our `in_scope` count (flag 1) should be near 400 or above (own Prüfstellen plus partner Prüfstellen listed by the finder). The finder lists licensees (engineers' offices and stations); no PSP-like names were found among 449 records (§5b), so the 8,000 PSPs are probably not in it.
- The 80636 sample: 33 of 83 entries within 50 km have flag 1.

## 7. Known coverage gaps and open issues

- Resolved by the full runs: real fill-in need (27 queries at 50 km, 7 at 100 km), far-off server centres (up to 21 km from the GeoNames centroid, handled by fill-in), no count cap (largest answers 135 at 50 km, 258 at 100 km), technik list a subset nationally (0 technik-only records). Rules C and D keep watching in later runs.
- Thin east (Sachsen 24 records, Thüringen 12, Mecklenburg-Vorpommern 9, Bremen 7): both radii agree, so it is the real network, not a gap.
- Data inconsistency: adressid 4125 has `lizenznehmer` "K0495,K0229" in the main list and "K0229" in the technik list. The parser keeps the first value, so a record can carry two comma-joined licence codes (affects `licensee_site_count` of that row only).
- The "400 own Prüfstellen" figure is unverified (§6).
- `km` radius parameter is undocumented (found by testing) but now the production setting; a changed radius is caught by rule C and the radius checks (`RADIUS_SHRUNK_FRACTION`, `RADIUS_TOLERANCE_KM`), fallback `RADIUS_KM = 50`.
- Own sites: no longer a KÜS task; see the HDP scraper and the aggregator's own-site removal (`scraper_status.md`, "HDP own sites"). `4_reference/hdp/hdp_sites.csv` (§3) is not needed.
- Convention: this recon used `test_output/<run>_recon/` with `src/recon.py` and wrote `pattern_analysis.md` into a sample run folder (both noted in `scraper_status.md`).
