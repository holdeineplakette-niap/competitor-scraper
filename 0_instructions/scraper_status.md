# Scraper Status

> **Last updated:** 2026-10-21 (**decisions logged: `psp = Yes` rows stay in, no fresh scrape or rebuild before the first import (it comes from the official 2026-10 master), no contacts file for now; retired-on date added to the HubSpot export as `compscraper_closedondate`**; earlier the same day: **master lifecycle, `needs_review` rule and HubSpot export built; HubSpot test import done**: `needs_review` is now True only for rows retired or reactivated in the run (Nikos), the old reasons are doubt flags; new master columns `status` / `retired_on`, every `master_id` stays in the master (retired after one missed month, only with fresh inputs); `core/hubspot_export.py` writes the HubSpot import files; the 49-row test import into HubSpot was reported successful by Nikos; full import not done; see the project-wide items "Master aggregator" and "HubSpot import") · 2026-10-08 (**HDP own-site mechanism recorded; the open `is_hdp_site` (formerly `is_liria_site`) / `hdp_sites.csv` item is closed**: the company is HolDeinePlakette (HDP), the HDP scraper exists (own row and `sources/hdp.md`) and the aggregator removes HDP's own sites; open: Fürth and Dortmund, see the project-wide item "HDP own sites"; **KÜS count corrected to 1,099 in scope** (all records, `psp` column) to match the 2026-10-07 rebuild and the aggregator; master aggregator item added, official run 2026-10 PASS; "HDP" replaced by HolDeinePlakette in `project_plan.md`, `sources/kues.md` and the storage convention) · 2026-10-07 (**GTÜ and KÜS scope rules changed to "include unless there is a certain cause to exclude"** (Nikos): GTÜ 855 → **1,394 in scope** (G offices that list HU and rows with conflicting signals now count; only the 35 offices with no HU service stay out), KÜS 582 → **587** (5 `vorort = 0` records whose name says Prüfstelle come in flagged; 3 hand-checked ones stay out); both rebuilt offline from their passing full runs, 907 tests pass; duplicate E/G listings of one GTÜ site are left to the aggregator; see the GTÜ and KÜS sections and the project-wide item "Include unless certain cause") · 2026-10-06 (**TÜV Rheinland adapter completed, live full run PASS: 341 HU sites in scope** from one JSON file; TÜV Saarland covered through `operator_group` and a 24-row landing-page cross-check, see the TÜV Rheinland section) · 2026-10-06 (**TÜV Thüringen adapter completed, live full run PASS: 12 own stations in scope**; one server-rendered page, three views merged per store id, cross-check URL agrees; 3 stores flagged, see the TÜV Thüringen section) · 2026-10-06 (**TÜV Hessen scraper marked completed**; overlap with the TÜV SÜD finder measured offline: 102 of 113 HU sites are in both lists, 11 only at TÜV Hessen; dedupe is left to the aggregator, see the project-wide item "Cross-source overlap") · 2026-10-06 (**VÜK adapter completed, live full run PASS: 5 own stations, all in scope**; VÜK GmbH (OWL) was not in the brief's ÜO list; completeness = five agreeing station sets (menu, footer, body text, XML sitemap, HTML sitemap) + the number the site states; coordinates from OpenStreetMap Nominatim, the pages carry none) · 2026-10-06 (**TÜV NORD adapter built, live full run PASS: 497 HU sites of 612 records**; page counter = parsed records, A-Z list cross-check done: 7 slugs differ between map data and list, see the TÜV NORD section) · 2026-10-06 (**TÜV Hessen adapter built, live run PASS: 113 HU sites of 131 listed**; completeness proven by `hits.total` = hits = UI counter, Playwright headless, Cloudflare did not block) · 2026-10-06 (FSP: adapter, notebook and **first live full run PASS: 169 sites in scope** = 168 in the HU-filtered list + 1 sitemap-only site whose page names HU, identical to the recon; HU filter used on Nikos's instruction although robots.txt disallows `d=`) · 2026-10-05 (**TÜV SÜD adapter completed 2026-10-06**: full run PASS, 718 in scope, route = Nikos's own browser session ·  KÜS adapter finished: 50 km and 100 km full runs PASS and agree, production setting 100 km · DEKRA adapter built, full run PASSED · GTÜ full run passed, verification pending) · Live state for the Prüfstellen sourcing project. Replace the old copy in the project knowledge base and in the repo (`0_instructions/scraper_status.md`) after every update.

**Adapter status values:** not started · investigating · working · broken

---

## Overview

| ÜO | Locator type | Approach | Adapter status | Last verified count | Known coverage gaps | Open issues |
|---|---|---|---|---|---|---|
| GTÜ | JSON API behind a location search (SmartMaps), max. 20 results per query, no paging | Adaptive grid (quadtree) checked via `MaxCount`; completeness proved by a reference query (Niederdorla, 500 km) plus early stop; collect all entries, filter to Prüfstellen on export | investigating: full run `2026-09-30_1100_full` **PASS (early stop, count match)**, 899 requests; post-run verification pending | 1,429 German entries collected = reference MaxCount (2026-09-30); **1,394 in scope** after the 2026-10-07 rule (rebuild `2026-10-07_1726_rebuild`, PASS inherited): 855 `pruefstelle`, 448 `office_hu_listed`, 91 `unknown_conflict`; 35 `office_no_hu` out; about 70 sites are listed twice (E and G) and left to the aggregator to merge; 5 + 5 G offices spot-checked by hand | None known for in-scope sites; 192 grid squares were not queried because of the early stop (listed) | aggregator: merge the E/G twins, new columns and `status_count` (see the GTÜ section); 1,394 is far above GTÜ's published "700+ / ~800" Prüfstellen because about 450 are offices (filter on `scope_tier`); Nürnberg spot-check; baseline selector picked a FAIL run; `sources/gtue.md` outdated; key source (rotation risk) |
| KÜS | JSON API on `webservice.kues.de` behind a PLZ search: every Partner within a **fixed 50 km** of the PLZ, no count field, no paging, no key, no cookies | PLZ query points from a hex lattice at **`km=100`** (84 planned + 7 fill-in = 91 PLZ, two lists = 182 requests, ~13 min; the page's 50 km design needs ~740 requests and stays available); completeness **proved** by four rules on the actual server centres (A coverage, B fringe, C appearance consistency, D cap detector; `2_core/radius_cover.py`); collect all entries, filter on `in_scope` | **working, verified (2026-10-05)**: adapter, notebook and 105 tests; two live full runs both **PASS**, 50 km (`2026-10-05_1704_full`, 736 requests, 55 min) and 100 km (`2026-10-05_1812_full`, 182 requests, 12.5 min), and they agree completely (1,102 records, 0 ids in one run only, 0 content differences). **Production setting: `km=100`** | **1,102 records, 1,099 in scope** (latest rebuild `2026-10-08_1121_rebuild`, PASS, same numbers as `2026-10-07_1830_rebuild`, rebuilt only to carry the renamed column `is_hdp_site`; **changed from 587 on 2026-10-07**: every record is now in scope, with `psp = Yes` for the 517 with `pruefungvorortmoeglich = 0` and `psp = No` for the 582 with `= 1`; site types 582 `pruefstelle`, 515 `partner_office`, 5 `unknown`; only the 3 hand-checked non-Prüfstellen stay out; the earlier rebuild `_1734_rebuild` still had 587), 13 needs review, 46 PLZ-only; 350 also in the technik list, 0 technik-only | None known. No count cap (largest answer 135 records at 50 km, 258 at 100 km), radius held (max `entfernung` 50.0 and 99.9 km); sparse east (Sachsen 8, Thüringen 8, Mecklenburg-Vorpommern 4 in scope) is confirmed by both radii | Nine questions about the flag and the pattern analysis (`sources/kues.md` §5b); 19-row spot check to label; HolDeinePlakette's own sites are handled by the HDP scraper and the aggregator's own-site removal (see the project-wide item "HDP own sites"); the `is_hdp_site` column (renamed from `is_liria_site` on 2026-10-08) stays empty and is not used; 1,099 in scope (582 of them `psp = No`) vs the unverified "400 eigene Prüfstellen" |
| DEKRA | Map-bounds JSON API: `POST www.dekra.de/api/locations/fromBoundaries`, no key/cookies, no cap seen | Whole map in one request (`types: []`, world box) as the data source, plus Germany box and 4 quarters as completeness proof; scope rule on our side, **all in-scope records included, doubts only flagged** | **working**: adapter, notebook and 44 tests built; full runs `2026-10-05_1140_full` and `_1146_full` both **PASS** (6 requests, 39 s) | **826 in scope** (796 Automobil-typed `pruefstelle` + 30 untyped/AT-typed `unknown`) of 1,723 DE records, 3,017 in the universe (2026-10-05); identical in both runs | None known. PSP-like and possibly stale sites are included and flagged, not excluded (decision 2026-10-05) | Nürnberg spot-check and comparison with "über 600" not done; validation of flagged rows happens later in the competitor map (out of scope here) |
| FSP | Server-rendered HTML list (Laravel/Livewire) at `www.fsp.de/standorte`; no JSON API. GET parameters `lat`, `lng`, `r`, `page` (and `d`, `f`, `search`, which robots.txt disallows). 12 cards per page, the pagination bar states the page count; no coordinates or operator on the cards | One query from the centre of Germany with `r=1000` returns the whole country (nearest 28.2 km, farthest 413.5 km), paged with `&page=N`; `sitemap.xml` (428 URLs, 315 `/standorte/<slug>`) as cross-check. Plain HTTP works, **no Cloudflare block** (33 requests, all 200). The HU-filtered list (`d=hauptuntersuchung`, which robots.txt disallows) was collected on Nikos's instruction (2026-10-06) | **working as a recon (2026-10-06)**: all-services list, HU-filtered list and sitemap cross-check done; **working (2026-10-06)**: run modes (`simulated` PASS 31/31, `sample`, `full`, `rebuild`) and control notebook built; **live `full` run `2026-10-06_1047_full` PASS** (47 requests, 15 checks ok, no warnings); no detail-page crawl of the in-scope sites yet | **In scope: 169** = 168 in the HU-filtered list (14 pages, as in the browser) + 1 sitemap-only site whose own page names HU. All-services list 313 (27 pages, 0 duplicates), sitemap 315, overlap 313 (2026-10-06) | Prüfstützpunkte are **not all absent**: 3 "Prüfstützpunkt …" entries are in the list. 1 in-scope site is not in the locator at all (Kfz-Prüfstützpunkt Bad Salzungen, found via the sitemap). Sites in neither sitemap nor list are undetectable | Detail pages for coordinates and operator; hand-check of 5 sites + Bad Salzungen; production run mode; FSP is a TÜV Rheinland partner (103 booking links go to tuv.com): overlap with a later TÜV Rheinland adapter |
| TÜV SÜD | JSON API behind the HU Service-Center finder: `POST www.tuvsud.com/astscsearch/api/tscsearch/searchTSCLocation`, body `{"searchTerm":"<PLZ>","hu":1}`; the **50 nearest** Service-Center sorted by distance, no count field, server returns its search centre. **Cloudflare challenge (HTTP 403) on every scripted request**, with or without browser headers | **Decided (management, 2026-10-05):** queries run in Nikos's own browser session from the DevTools console (generated snippet per pass, slow, bounded, stops on any challenge); coverage proof, classification and export offline in Python. Coverage: each answer covers a disk of radius = distance of the last record − 0.5 km around the server's centre; all regular PLZ centroids must lie ≥ 15 km inside some disk. First run needed 3 passes (128 + 1 + 1 PLZ). OpenStreetMap later as cross-check. Spec: `sources/tuev_sued.md` | **completed** (2026-10-06): adapter, notebook and 123 tests; sample pass and full run `2026-10-06_0913_full` **PASS**; offline rebuild `2026-10-06_1003_rebuild` with the final classification | **718 in scope**, all `pruefstelle` (2026-10-06): own 377, TÜV SÜD partner 246, TÜV Hessen 52, TÜV Hessen partner 35, TÜV Hanse 8 | None known. Answers below 50 only in the far north (the finder seems to cap at ~300 km; the proof stays valid) | 8 rows carry the Wolnzach-type `uo_notice` (meaning undecided, kept in scope); TÜV Hessen's 87 sites are in this finder too: dedupe at the master step; no current published count to compare with; OSM cross-check later |
| TÜV NORD | Server-rendered TYPO3 page `www.tuev-nord.de/de/stationen/`: all locations as HTML-escaped JSON in the `data-locations` attribute of the map div, plus the text "<N> Stationen gefunden"; no API, no key. Plain HTTP works (Citrix NetScaler, no Cloudflare), fresh session, no cookies | One GET of the page is the data source (612 records). Completeness evidence: counter == parsed records, all three serviceTypes present, cross-check with the A-Z list `/liste-aller-stationen/` by page slug, 3 detail pages. HU = serviceType `HU` or `HU-UNG`; `site_type` by name (office / partner / own probable / unclassified). Details: section TÜV NORD | **completed (2026-10-06)**: adapter, control notebook and 64 tests; live full run `2026-10-06_1113_full` **PASS** (12 requests) and offline rebuild `2026-10-06_1116_rebuild` PASS | **497 HU sites** (216 `HU` + 281 `HU-UNG`) of 612 records (2026-10-06): partner 296, own probable 195, unclassified 6. Not HU: 115 `UNG` (112 offices + 3 stations) | Page counter and parsed records agree (612). The A-Z list differs from the map data by 7 slugs: 2 map stations have no page, 5 list pages are not in the map data (3 real locations + 2 online product pages); HU at workshop Prüfstützpunkte probably not listed (not verified) | 5 list-only pages and 2 pageless stations to decide; 7 unclassified names; scope of partner stations in workshops (25 HU sites `psp_suspect`); hand-check of 5 sites; detail pages not crawled |
| TÜV Rheinland | One static JSON file, `www.tuv.com/germany/de/locationfinder/locationfinder-config.json`: the complete worldwide location dataset (1,198 records, 10 types) that the finder filters client-side; no key, no cookies needed. HU = type `VIS` | One GET is the data source (dev copy saved, everything else offline). German = `vic_id` country `de` (foreign postcode/coordinates override), plus 4 VIS without `vic_id` that look German (flagged); `site_type` own_station / fsp_partner / unknown, `operator_group` tuev_rheinland / fsp / tuev_saarland. Cross-check against three tuv.com landing pages (Saarland, Berlin, Pfalz). Section TÜV Rheinland | **completed (2026-10-06)**: adapter + control notebook (no pytest suite); live full run `2026-10-06_1536_full` **PASS** (1 request to the finder + 3 to the landing pages; an identical earlier run `_1528_full`) | **341 in scope** (live run 2026-10-06): own_station 176, fsp_partner 164, unknown 1; 12 need review; landing pages: Saarland 21 of 24 matched by address (+2 decided by Nikos), Berlin 6 of 6, Pfalz 21 of 21 | Mobile PSPs and other types (GUT, OFF, LAB, …) not exported; 1 station on the Saarland page (Losheim, Saarbrücker Str. 221) is not in the finder JSON (added from the page, no coordinates) | 9 rows without PLZ (4 FSP) or coordinates (5), left to the aggregator (geocoding, PLZ from the FSP list); Wadern-Lockweiler vs finder record 45291 unverified; Wörth `unknown`; overlap with FSP (dedupe in the aggregator); `robots.txt` not checked (Nikos's decision) |
| TÜV Hessen | Elasticsearch JSON behind the TYPO3 locator page: `POST www.tuev-hessen.de/?eID=location_search` (multipart form, 669 bytes), all hits in one answer, no paging and no size field. Cloudflare in front (passive JS check, `cf_clearance`) | Playwright (headless Chromium): load the page, decline cookies, run "Alle Standorte" and "Kfz Prüfstelle" through the UI, replay each request from the page; completeness = `hits.total` (`eq`) = `len(hits.hits)` = UI counter, UI and replay answers identical. HU = tag "Kfz Prüfstelle". Spec/notes: section below | **completed (2026-10-06)**: adapter, control notebook, 61 tests; live run `2026-10-06_1059_full` **PASS** (32 s); final offline rebuild `2026-10-06_1102_rebuild` | **113 HU sites** of 131 listed (2026-10-06): own_station 52, partner 44, tuev_sued_auto_partner 16, unclassified 1 (LKW Service-Center Friedberg Süd); the other 18 listed sites are MPU / Managementsysteme (no HU wording in any of them) | HU stations inside workshops are probably not listed (not verified). "Alle Standorte" = three fixed tags only; completeness is proven against what the locator offers, not against the full index | 1 unclassified title; 21 own stations without the "TÜH Service-Center" tag; coordinate copy-paste errors (433 Bad Camberg); `robots.txt` not checked; **102 of the 113 HU sites are also in the TÜV SÜD finder: the aggregator must dedupe (see project-wide item "Cross-source overlap")** |
| TÜV Thüringen | Server-rendered Shopware + Acris Store Locator, store group "Prüfanlagen" (`/store-locator/08a232c58b2f4816939b7d6327804b88/store-group`); every store appears 3 times on the page (map overlay, desktop table, mobile table); plain HTTP, no key, no API | One GET of the page, the three views merged per `data-store-id`; every store of the group = `own_station`, in scope; doubts only flagged. Cross-check: second URL `/store-locator/Pruefanlagen`. Section TÜV Thüringen | **completed (2026-10-06)**: adapter + control notebook (no pytest suite); live full run `2026-10-06_1509_full` **PASS** | **12 own stations, all in scope**, 3 flagged for review (2026-10-06) | HU at workshop PSPs and outside Thüringen not listed | Mühlhausen (own site mhl-hu.de, no e-mail) and Schmalkalden (no e-mail) possible partners; Altenburg sits in an Autohaus (`psp_suspect`); `robots.txt` not checked (Nikos's decision) |
| TÜV Saarland | None of its own; stations listed in the TÜV Rheinland finder and on tuv.com's Saarland landing page | Covered by the TÜV Rheinland adapter (`operator_group` tuev_saarland), 24-row landing-page cross-check | **working via TÜV Rheinland** (live full run `2026-10-06_1536_full` PASS) | **23 labelled tuev_saarland** of the page's 24 (live, 2026-10-06): 21 matched by address + Völklingen (address filled) + Losheim Saarbrücker Str. 221 (added from the page); 22 of them are finder VIS | ~300 mobile Prüfstützpunkte (PSPs, out of scope) | Wadern-Lockweiler (Mittelbachweg 1) is probably finder record 45291 (Wadern, Franz-Haas-Straße 1 a), not labelled |
| VÜK (added; not in the brief's list) | **Static HTML, no locator** (Jimdo CMS, plain HTTP, no key, no Cloudflare block): five station contact pages `/kontakt/pruefstelle-<ort>/`, linked from the Kontakt menu, the footer list "VÜK Standorte", the body text of `/ueber-uns/`, `sitemap.xml` and the HTML sitemap; the site states the number ("fünf eigenen Standorten", meta description and body text) | Discover the stations from five sources, fetch each station page (name, address, phone, e-mail, hours, services), geocode with OpenStreetMap Nominatim (the pages carry no coordinates). **Completeness (fails loudly):** menu = footer = body text = XML sitemap = HTML sitemap, count = stated number, set = last passing full run (first run: the five of the brief); every page must yield street + PLZ + city | **completed (2026-10-06)**: adapter, notebook, 105 tests; live `full` run `2026-10-06_1146_full` **PASS** (11 requests to vuek-gmbh.de + 5 to Nominatim, 16 of 16 hard checks); offline rebuild `2026-10-06_1148_rebuild` identical; offline rebuild `2026-10-08_1121_rebuild` (from `2026-10-06_1152_full`, PASS, 5 stations) carries the renamed column `is_hdp_site` | **5 own stations, all in scope** (2026-10-06): Lage, Bielefeld-Brackwede, Bad Oeynhausen-Rehme, Minden, Kirchlengern; the site states five | **Partner workshops (PSPs) are not listed** (site: "zahlreichen Partnerwerkstätten in OWL"); out of scope | Hand-check of the five addresses on Google Maps; Lage page carries a past temporary notice; one phone number and one e-mail address shared by all five; change detection has no baseline until the second full run |
| HDP (own sites, not a competitor) | Firebase callable function behind holdeineplakette.de: `POST …cloudfunctions.net/getAllPublicLocations`, body `{"data":{}}`, no key, no cookies | One request returns all sites; 8 checks (hard: success, non-empty list, unique ids, postcode, coordinates, coordinates in Germany; review: 5-digit PLZ, street + house number). Used by the aggregator to remove HDP's own KÜS rows before merging. Spec: `sources/hdp.md` | **completed (2026-10-07)**: adapter, notebook and tests; live full run `2026-10-07_1835_full` **PASS** (1 request) | **19 sites** (2026-10-07) | None known (the list is HDP's own) | Fürth has no KÜS row, Dortmund matches two: N = 19, R = 17 in the aggregator (see "HDP own sites") |

---

## Project-wide open items

- [x] **Master aggregator (2026-10-07):** `core.master_run` builds `pruefstellen_master` from the 10 competitor exports (HDP is applied as a removal, not as a source). The official run for 2026-10 (`3_master_output/2026-10/`) is **PASS**: 5,174 rows in, 4,823 in the master (334 merged away, 17 HDP own sites removed), 71 rows to review (old meaning, see the next item); two review checks fail (HDP N ≠ R, above). Its row-count anchors (`status_count` in `2_core/master_inputs.py`) are GTÜ 1,394 and KÜS 1,099, i.e. they follow the numbers in the overview table.
- [x] **Master lifecycle and `needs_review` rule (Nikos, decided and built 2026-10-21; 992 tests pass):**
  - **`needs_review`** is True **only** for a row whose status changed in this run: reason `retired` or `reactivated`; it is cleared the next month. The former reasons (`match_candidate_unresolved`, `coords_unresolved`, `address_incomplete`, `plz_invalid`, `hdp_match_unresolved`) are plain tokens in `doubt_flags` and are counted in the summary under "Doubt flags"; the scrapers' own `needs_review` columns are unchanged and no longer set the master flag. Dev run on the current inputs: 0 needs_review rows, doubt flags 17 / 5 / 46 / 2 / 2 (in the order above).
  - **New master columns** `status` (`active` / `retired`) and `retired_on` (37 columns in total). Every `master_id` ever issued stays in the master (new ids = highest id ever issued + 1, never reused). A row that no source lists any more is **retired after one missed month** (no grace period), keeping its last known values; it is retired only if every source behind it has a fresh input run (at most 45 days old, no newer run rejected), otherwise it stays active with the doubt flag `not_refreshed:<source>`. If a retired site returns it keeps its id, becomes active again and is flagged `reactivated` for one run. A retired row and a new row at the same address are both flagged `possibly_rekeyed:<id>` and never merged. The diff reports `retired` / `reactivated` instead of `disappeared`.
  - **Mass-retirement guard:** the existing 10 % check now counts active rows; `--accept-retirements` turns it into a review item. Code: `2_core/master_lifecycle.py`; the official 2026-10 master is **still the old schema** (no `status` columns, old `needs_review` meaning, header still `is_liria_site`); it is read as "all rows active". **Decision (Nikos, 2026-10-21): no rebuild and no fresh HDP scrape** (the newest one was 14 days old, limit 7); the first full HubSpot import is made from this official 2026-10 master as it is, and it is the baseline of the November run.
- [ ] **HubSpot import (2026-10-21) — REVISED THE SAME DAY: the first import was aborted by Nikos; the "no matching against existing companies" decision below is superseded (HubSpot holds ~53k companies and the M&A Deals, which must never be touched). New approach (match analysis of all M&A Deals, duplicate handling, delta import of NEW and RETIRED only): `scraper_automation_plan.md` §9.** Earlier text of this item: decided with the boss: one initial import of our own companies, then monthly updates; most of these companies are not in HubSpot yet, so **no matching against existing companies** (a Prüfstelle that already exists there would be created a second time; accepted). Nothing in HubSpot is ever deleted by us.
  - **Match key:** unique company property `prufstellenid_datascraper` = our `master_id` (unique values switched on, per Nikos). `company_datasource` (dropdown, internal value `competitor_scraper`, no default) marks every record we create.
  - **Properties used** (internal names, created by Nikos): `name`, `address`, `zip`, `city`, `state`, `country` (value `Germany`), `phone`, `unternehmens_e_mail`, `website` (standard Website URL, never the company domain), `stutzpunkt_pruforganisation` (checkbox; TÜV SÜD is written as the internal value `TÜV`, TÜV NORD as `TÜV Nord`; options for TÜV Saarland, TÜV Thüringen and VÜK were added), `stutzpunkt` (`Ja` / `Nein` from `psp`), `geo_lat` / `geo_lng` (numbers, German decimal comma), `compscraper_adresse_unvollstandig` (`Ja` / `Nein`), `business_status` (`aktiv` / `scraper-geschlossen`), and the retired-on date `compscraper_closedondate` (written as `YYYY-MM-DD` together with `scraper-geschlossen`; empty for active sites; property created by Nikos in HubSpot on 2026-10-21; the 49-row test import was done before this column existed). **Not created, so not exported:** opening hours, services, booking and profile URL, site type. Known limit: a reactivated site gets `business_status = aktiv`, but its old `compscraper_closedondate` stays in HubSpot (an empty cell never overwrites), so it is cleared by hand; the changes overview lists every reactivation.
  - **Files** (`core/hubspot_export.py`, written next to every master; `python -m core.hubspot_export <master.csv> --out <dir> [--sample 50]` for an existing master): `hubspot_import_YYYY-MM.csv` (first master: every active row; comma-separated UTF-8, text only), `hubspot_changes_YYYY-MM.csv` + `hubspot_changes_overview_YYYY-MM.csv` (later months: new / changed / retired / reactivated rows; changed rows carry only the changed fields, retired rows only `business_status`), `hubspot_excluded_YYYY-MM.csv` (rows kept out: the two possible HDP own sites KUES-30052 / KUES-58908 until Nikos decides), `hubspot_test_sample_YYYY-MM.csv` (~50 rows covering shared domains, street-less rows, every ÜO, Stützpunkt Ja). A dev master built as of 2026-10-08 gives 4,821 import rows (all active rows incl. `psp = Yes`; 3,772 with phone, 3,514 with email) and a 49-row test sample.
  - **Test import (2026-10-21):** the 49-row sample was imported in HubSpot (Erweiterte Importe, Unternehmen, "Nur neue Unternehmen erstellen", number format Deutschland, no enrichment) and **reported successful by Nikos**. **Confirmed by Nikos (2026-10-21): the 12 rows sharing a website domain stayed 12 separate companies.** Not reported back, so not confirmed here: the created / skipped counts, that `geo_lat` / `geo_lng` arrived as numbers, and what the workflows (17) and segments (10) that use the imported properties did with the new companies.
  - **Decisions (Nikos, 2026-10-21):** the 515 `psp = Yes` rows **stay in** the import (Stützpunkt = Ja); **no fresh scrape and no rebuild**, the first import comes from the official 2026-10 master; a **contacts file for the emails is not needed for now** (emails stay on the company property; HubSpot sequences would need contacts); the retired-on date is exported as `compscraper_closedondate`.
  - **Open:** the full first import itself (import file from the official 2026-10 master not generated yet); the two possible HDP own sites (KUES-30052 / KUES-58908) stay out until decided; the later monthly import needs "Unternehmen erstellen und aktualisieren"; the GDPR documentation (emails and operator names are in the file) is still open; which HubSpot pipeline gets which records (project plan §10).

- [ ] **ÜO list:** verify the complete current list of recognized ÜOs performing HU against an authoritative source. The list above is from the project brief. VÜK GmbH (OWL, 5 own stations) was missing from the brief's list. The list is demonstrably incomplete; verify it against an authoritative source before the master CSV.
- [ ] **Schema:** not fixed yet (Nikos to define). Parsers output a candidate field set until then.
- [x] **API key use approved (management, 2026-09-28), for all ÜOs:** automated collection via the locators' own client-side API keys is approved. Still to do: GDPR documentation (purpose, retention) and private storage of snapshots (see project plan §7).
- [x] **Scope decision (management, 2026-09-28):** only **actual Prüfstellen** are in scope. PSPs (Prüfstützpunkte in workshops/dealerships) are **out of scope** for all ÜOs.
- [x] **DEKRA exception (Nikos / management, 2026-10-05):** "better 200 records too many than 3 missing". For DEKRA, **all records that pass the scope rule are included, with no validation beforehand**; PSP-like, thin (possibly stale), untyped and "no vehicle testing" records are only flagged (`doubt_flags`, `needs_review`). Validation happens later, after inclusion in the competitor map, which is not part of this project for now. (GTÜ and KÜS followed on 2026-10-07: see the next items.)
- [ ] **HDP own sites (2026-10-07; replaces the earlier `is_hdp_site` / `hdp_sites.csv` reference-file idea, which was still called `is_liria_site` / `liria_sites.csv` then):** the company is **HolDeinePlakette (HDP)**. The HDP scraper (`1_scrapers/hdp/`, one POST to the public location list, no key) collects HDP's own sites; live full run `2026-10-07_1835_full` **PASS, 19 sites**. The aggregator (`2_core/hdp_removal.py`, wired into `core.master_run`) removes the matching KÜS rows before merging, so own sites don't appear as competitors: it needs a PASS HDP run no older than 7 days, matches on PLZ + street + number (fallback PLZ + coordinates within 150 m, listed), and checks that rows removed (R) equal sites found (N). **Decisions (Nikos, 2026-10-21; not yet applied in code or in the master):** (1) **KUES-30052 (PS-002750) is removed** from the master: same address and coordinates as HDP Dortmund (Wambeler Hellweg 50-52), licence E0982 (one site, `pruefung_vor_ort = 0`, Ingenieurbüro). (2) **Fürth (Sigmundstr. 113, 90431 Nürnberg) is a confirmed HDP site** that KÜS does not list: there is nothing to remove, and it must count as a known exception so that N (19) = R + exceptions. Evidence from the 2026-10 master: no KÜS row within 150 m (nearest 1.3-1.7 km, other licences), and no row of any ÜO at Sigmundstraße. KUES-58908 (PS-003342, licence I1122, the licence of all 17 removed HDP sites) was not mentioned in the decision; it is still flagged `hdp_match_unresolved`. **Open:** N = 19, R = 17 until the aggregator implements the two decisions (the check is a review item and doesn't fail the build). No `sources/hdp.md` yet.
- [x] **Include unless certain cause (Nikos, 2026-10-07): "if we do not have certain cause for exclusion, we include them".** A row is excluded only with a certain cause: the source's own explicit statement that nothing is done on site or no HU is offered, a hand check, or (aggregator) a certain duplicate. Conflicting signals, operator siblings and rows with public-facing signs stay in and are flagged. Applied to GTÜ and KÜS (below); the scrapers still collect everything and filter on `in_scope`. Duplicates of one physical site are merged by the aggregator (`core.dedupe`), not dropped in the scrapers.
- [x] **TÜV SÜD access (management, 2026-10-05):** tuvsud.com blocks scripts (Cloudflare). Queries run in Nikos's own browser session via a generated console snippet (slow, bounded, stops on any challenge); no automation tools, no bypass techniques. `robots.txt` checked, fine. Monthly run needs ~10–15 min of manual work and cannot be unattended. Details: `sources/tuev_sued.md`.
- [x] **KÜS rule (management via Nikos, 2026-10-05):** `pruefungvorortmoeglich = "0"` means the entry inspects at PSPs, not at its own address → **out of scope** (`partner_office`, same category as GTÜ's offices without on-site inspection); `"1"` → `pruefstelle`, in scope. Everything is collected and filtered only by `in_scope`; conflicts with the address are flagged for review. **Extended 2026-10-07:** a `"0"` record whose own name says Prüfstelle / Prüfzentrum / Prüfingenieur is kept (`unknown`, in scope, needs review) unless it was hand-checked as not a Prüfstelle (3 records).
- [x] **Assumption accepted (management, 2026-09-28):** for GTÜ, "Prüfung vor Ort" (= "GTÜ-Ingenieurbüro mit amtlicher Fahrzeugprüfung vor Ort") means the location is a Prüfstelle. **Refined 2026-10-07 after hand checks:** this holds for the E rows, but G offices (no on-site code) that list Hauptuntersuchung do HU too (5 of 5 checked), so they are in scope as well; only offices without any HU service are out.
- [ ] **Site-type categories:** working set is `pruefstelle` · `partner_office` · `unknown`. Scrapers collect all entries a source lists (needed for completeness checks); the export is filtered to `in_scope` rows. DEKRA: Automobil-typed rows are `pruefstelle`, untyped/AT-typed rows `unknown`, and `in_scope` is true for every row that passes the scope rule (see the DEKRA section). The five `site_kind` values of the DEKRA labelling sample (`pruefstelle` · `gutachten_office` · `partner_engineer` · `admin_or_other_division` · `other`) were never filled in and are not used.
- [x] **Project restructure (2026-09-30):** numbered folders (`0_instructions/`, `1_scrapers/<org>/`, `2_core/`, `3_master_output/`, `4_reference/`, `9_archive/`), one folder per run (`output/` or `test_output/YYYY-MM-DD_HHMM_<mode>/`). Rules in `0_instructions/storage_and_naming_convention.md`. **All tests pass** (2026-10-05, offline: 63 core, 86 GTÜ, 44 DEKRA, 105 KÜS, plus the TÜV SÜD tests; the whole suite was 388 at the last KÜS change).
- [ ] **Convention gap:** no slot yet for (a) one-off live recon runs (DEKRA used `test_output/<run>_recon/` and `src/recon.py`; the convention's `recon` mode means an offline parse of a saved sample) and (b) scoring output against hand labels (proposal: in the run folder it scores, `<slug>_scoring_<date>.xlsx`; labels stay in `4_reference/ground_truth/`), and (c) runs that skip files: the DEKRA notebook defaults to `KEEP = ("pruefstellen_csv",)` and `SAVE_RAW = False`, so a run folder then holds one CSV plus the small summary files and no `raw.jsonl` (the convention expects the raw responses in every run), and (e) TÜV SÜD (planned): one run spread over several manual browser passes in the same run folder, with extra files `tuev_sued_passNN.js` and `raw_passNN.jsonl` merged into `raw.jsonl` at the end, and (d) KÜS used the same live-recon slot as DEKRA (`test_output/2026-10-05_1607_recon/`, `src/recon.py` with `fetch` and `analyze`, every live request in its `query_log.csv`) and wrote the pattern analysis (`pattern_analysis.md`) into the sample run folder it analysed (`test_output/2026-10-05_1635_sample/`); the spot-check list for hand labels went to `4_reference/ground_truth/kues_spotcheck_2026-10-05.csv`. Shared export code now exists once in `2_core/export.py` (used by KÜS; GTÜ and DEKRA keep their own copies).
- [ ] **Docs to update:** `0_instructions/sources/gtue.md` still describes the state of 2026-09-28 (33 tests, "not yet verified against the live API", national check, PASS rule without the reference query, "MaxCount includes entries abroad"). `sources/gtue_grid_changes.md` and this file are current. `0_instructions/sources/dekra.md` (recon notes, step 3 of "How to add a new ÜO") does not exist yet; the DEKRA details live in the DEKRA section of this file.
- [ ] **Cross-source overlap (TÜV SÜD / TÜV Hessen), to be cleaned in the aggregator:** TÜV SÜD is the majority owner of TÜV Hessen and both finders list the same sites. Measured offline on 2026-10-06 (TÜV SÜD `2026-10-06_1003_rebuild` 718 rows vs TÜV Hessen `2026-10-06_1102_rebuild` HU file 113 rows; my matching, not hand-checked): **102 of the 113 TÜV Hessen HU sites are also in the TÜV SÜD list**, so about 729 unique sites. Both sources stay in the master input unchanged; **the aggregator removes the duplicates** (one row per physical site, keep the source of each row in a separate column).
  - **What TÜV SÜD calls them:** TÜV Hessen own stations → `tuev_hessen` (51 of 52 found), TÜV Hessen partners → `tuev_hessen_partner` (35 of 44), "TÜV SÜD Auto Partner GmbH" → `tuev_sued_partner` (15 of 16), LKW Friedberg Süd (uid 409) → `tuev_hessen` (1 of 1). All 87 `tuev_hessen` / `tuev_hessen_partner` rows of the TÜV SÜD file are in the TÜV Hessen list.
  - **Only at TÜV Hessen (11):** uids 150 (Birstein, own), 362, 360, 288, 311, 315, 330, 399, 272, 286 (partners), 440 (Mainz-Hechtsheim, SÜD Auto Partner; text offers only "Schadengutachten", HU doubtful). uid 272 may be the same business as the SÜD site "An der Automeile 18B" (300 m away). Not checked whether these are open and do HU.
  - **Match keys, best first:** (1) booking `locationSign` (TÜV Hessen `location_sign` = TÜV SÜD `location_sign`): 90 of 91 signs match; the exception is Biedenkopf (uid 71) with Worms's sign 712, the error is on the TÜV Hessen side. (2) Normalised street + PLZ (`tuev_hessen.parse.normalise_address`): 96 matches; it misses parentheses ("(bei Fa. …)"), typos ("Willhelm-Dichmann") and "Str."/"Straße" variants. (3) Coordinates only as a candidate proposal: the same address differs by a median of 64 m and up to 2.6 km (27 pairs > 150 m); a 150 m rule gave one false match (Oberursel Zeilweg 6 vs a Bad Homburg site, 24 m).
  - **Coordinates:** for uid 433 (Bad Camberg) TÜV SÜD has the correct location; TÜV Hessen's coordinates are 22.3 km off (identical to uid 428). Prefer the TÜV SÜD coordinates for matched sites; keep `listed_by` and `operator` apart.
- [ ] **Core must validate coordinates against the PLZ:** the TÜV Hessen data contains coordinate copy-paste errors (uid 433, Bad Camberg, carries the coordinates of uid 428, Oberursel: 23.6 km from its own PLZ centroid). The checks exist in the TÜV Hessen adapter (`tuev_hessen/checks.py`: Germany box, distance to the GeoNames PLZ centroid with a 15 km threshold, coordinates shared by different addresses); move them to `2_core` for all ÜOs.
- [ ] **Git / GitHub and automation:** deferred until all scrapers are done (Nikos, 2026-10-05). Before the first commit: baseline commit, `git check-ignore` on data and `.env`, add `*.egg-info/`, anonymise the München fixture.
- [ ] **Unique keys per source may need id + address (TÜV NORD reuses ids for multi-site offices):** TÜV NORD id 55292878 is used by two offices at different addresses. The TÜV NORD adapter keys on `source_key` = id + normalised address and numbers the `location_id` of a reused id (`TUEV_NORD-55292878-1`, `-2`). Check the other sources and the master step for the same pattern; the KÜS rule (dedupe by `adressid` only) is unchanged.
- [ ] **Backup zip** in the parent folder contains the old `api-keys.md` and raw data with personal data: keep private, delete once a full run on the new structure has succeeded (the GTÜ `2026-09-30_1100_full` folder was migrated from the old layout, so it does not count; the DEKRA full runs of 2026-10-05 were made on the new structure but the zip holds GTÜ data: Nikos to decide).

---

## TÜV SÜD

**Status:** **completed (2026-10-06).** Cloudflare blocks every scripted request; **management approved option 2** (queries in Nikos's own browser session, see "Blocked by Cloudflare"). Build spec: `0_instructions/sources/tuev_sued.md`. Adapter `1_scrapers/tuev_sued/src/`, notebook `tuev_sued_control.ipynb`, 123 tests. Full run `2026-10-06_0913_full` PASS (3 passes: 128 + 1 + 1 PLZ; 718 Service-Center, all in scope); `2026-10-06_1003_rebuild` is the same data with the final rules. Decisions: partner-operated sites and TÜV Hessen / TÜV Hanse stations in the finder are Prüfstellen and count (no flag; column `operator_group`); `booking_url` and `source_profile_url` stay empty; a run writes only `tuev_sued_pruefstellen_<date>.csv` (no `all` file, no Excel). The open-issues list at the end of this section is from the build phase and is superseded by this status. Throwaway test scripts in `_scratch/` (`tuev_sued_cf_test.py`, `tuev_sued_page_test.py`). `robots.txt` checked manually by Nikos: fine, confirmed by management (2026-10-05).

### How the locator works (as far as known)

- **Wrong source, ruled out:** `GET https://www.tuvsud.com/ajax.plex?modulename=WOlocationSearch&function=getLocations&countryId=…` (the corporate "Standorte" finder; empty filter returns everything in one ~9 s request). It returned **233 records of all TÜV SÜD group companies** (Industrie Service 50, Life Service / MPU 42, Pluspunkt 34, TÜV Hessen 25 mostly MPU, Akademie, Advimo, Rail …), only 6 Auto Service records (offices, one "keine HU Termine möglich"), `HUAU` = 0 wherever present, and none of the known HU Service-Center (Hammelburg, Murnau, Schongau, Farchant, Altenau). Not used. The saved response (`tuevsued_APIresponse.json`, 3.75 MB) can go to `4_reference/api_samples/tuev_sued/` as a negative reference.
- **HU source:** the HU finder on `tuvsud.com/de-de/branchen/mobilitaet-und-automotive/hauptuntersuchung/service-center-und-termin` (accepts `?zip=`; per-state pages such as `…/bayern`, `…/sachsen` say they list all Service-Center of the state).
- **Data endpoint:** `POST https://www.tuvsud.com/astscsearch/api/tscsearch/searchTSCLocation`, JSON body `{"searchTerm":"80686","hu":1}` (PLZ as text, `hu` = HU filter). No limit, radius or paging field in the body. Behind Cloudflare.
- Response (search "80636", captured 2026-10-05): `{status, errorId, errormsg, locationname, zipcode, zipcodes, lat, lon, finaltsc: [...], geodata}`. `lat`/`lon` at the top = **the centre the server used** (48.1518 N / 11.5431 E for 80636). No total or count field.
- Each `finaltsc[]` item: `tscId`, `distance` (string) / `sortdistance` (number), `times[]` (opening hours per `dayId` 1–7, morning + afternoon ranges, `isSpecialTime`), `location {…}`, `images`, `currentlyclosed`.

### Tested behaviour

| Test | Result |
|---|---|
| PLZ 80636 München (browser, 2026-10-05) | **Exactly 50 results**, strictly sorted by distance, 2.7 km (Eichstätter Str. 7) to 64.2 km (Manching). Known Service-Center further out (Farchant, Altenau, ~70–80 km) not included → **looks like a top-50 cap, not a radius**. Not yet proven (see open issues). |
| `distance` check | Equals the great-circle distance (km) from the returned top-level centre to the record's `lat`/`lon` (Eichstätter Str. 2.741 vs 2.743 reported; Manching 64.10 vs 64.15; Wasserburg 47.99 vs 48.03). So each response proves "every listed Service-Center closer than the 50th result's distance was returned", **if** the server returns the true 50 nearest. |
| All 50 records | `companyId` 500, `huau` 1, `tscStatus` 1, `federalStateId` 111 (Bayern), `countryId` = Germany |
| Scripted POST (Python `requests`, no cookies), 2026-10-05, 3 requests: A minimal headers 80686, B browser-like headers 80686, C browser-like headers 24937 Flensburg | **All three: HTTP 403, `text/html`, `cf-mitigated: challenge`, Cloudflare "Just a moment…" page.** Headers make no difference. The endpoint is protected against scripts. Cap behaviour in sparse areas therefore still untested. |
| Scripted GET of the Bayern state page (browser-like headers), 2026-10-05, 1 request | **HTTP 403, `cf-mitigated: challenge`.** Static pages are blocked for scripts too: the whole site is closed to scripted access. |

### Blocked by Cloudflare: options and decision needed (2026-10-05)

The management approval of 2026-09-28 covers automated use of locators and their client-side keys; it does **not** cover getting past bot protection that the operator has switched on. **No workaround is built** (no faked browser fingerprints, no challenge solvers, no copying of clearance cookies into scripts); besides the compliance question, these break without warning.

| Option | What | Status |
|---|---|---|
| 1. Per-state pages | One GET per state page; usable only if served without the challenge **and** the list is in the HTML | **ruled out** (2026-10-05): the page is challenged too |
| 2. Run in Nikos's browser | Nikos opens the HU finder in his normal browser; a console script runs the PLZ queries slowly from that page and downloads one JSON file per pass; the adapter does coverage proof, classification and export offline. ~10–15 min manual work per month, not unattended. Still scripted querying of a protected endpoint | **approved by management (2026-10-05)**; spec in `sources/tuev_sued.md` |
| 3. OpenStreetMap (vehicle-inspection stations), Google Maps | Free (OSM: ODbL licence); completeness cannot be proven | cross-check or fallback only; a fallback would be recorded as a coverage gap |

Decision: option 2, with option 3 as a later cross-check (management, 2026-10-05). Boundaries that are part of the decision: no headless browser or automation tools, no faked headers or fingerprints, no challenge solvers, no cookies copied into Python; the snippet sends the page's own request from the page; 5 s + jitter, hard ceiling per pass, immediate stop on any non-JSON answer; if the in-browser requests get challenged too, stop and report. Other TÜV SÜD hosts (e.g. the booking system) are deliberately **not** probed for an unprotected route. Meanwhile, recon continues with the other ÜOs (next: TÜV NORD).

### Proposed fetch strategy (not built; to confirm after the request URL is known)

1. **Nearest-50 coverage:** query points over Germany; each answer covers a disk around the server's centre with radius = distance of the 50th result (minus a small margin); an answer with fewer than 50 results covers all of Germany for that query (cap not reached). Germany is covered when the union of disks covers it, with overlap. Same proof idea as KÜS (`2_core/radius_cover.py`), but with a **per-query radius** instead of a fixed 50 km; check whether the module can take that or needs a small extension.
2. Dense south (Bayern, Baden-Württemberg) needs many points (50 sites within ~64 km around München); the north, where TÜV SÜD has few sites, needs few (the 50 nearest reach far). Expected size: tens to low hundreds of queries; to be simulated before any full run.
3. Before that, check for a cheaper route: a count/limit parameter in the request, or the per-state pages (if they embed the full list, they are a one-request-per-state source or at least a cross-check per state).
4. Dedupe by `tscId`. Politeness as in the project plan §7.

### Record structure (from the 80636 sample, key fields)

| Field (`finaltsc[].location`) | Meaning / note |
|---|---|
| `tscId` | **Stable ID candidate** (also at item level). → `location_id = TUEV_SUED-<tscId>`. Stability between runs not yet checked. |
| `locationSign` | Short site number (e.g. `214`, `12107`), used in the booking link (`…anmelden-zum-tuev-sued-service-center_1?locationSign=…`). Leading zeros matter (`008`, `001`): store as text. |
| `asproId` | Internal number; equals `locationSign` on newer sites (Karlsfeld, Gröbenzell, Grafing, Wolnzach …). |
| `tscStatus` | 1 on all 50. Meaning of other values unknown (inactive?). |
| `companyId` / `marketAreaId` / `federalStateId` / `countryId` | 500 on all / region code / 111 = Bayern / Germany |
| `street`, `streetnumber`, `zipcode`, `town`, `townPart` | Street and number separate. No site name (`name` empty): display name has to be built (e.g. "TÜV SÜD Service-Center " + `townPart`). |
| `lat`, `lon` | Coordinates. |
| `tscUrl` | Profile slug, e.g. `europe/germany/muenchen/eichstaetter-str-7`. |
| `huau` | 1 = HU/AU offered. **Proposed scope signal** (with `tscStatus` = 1 and country Germany). |
| `carService`, `truckService`, `bikeService`, `trailer*`, `camper*`, `tractor*`, `e_carService`, `gasService`, `oldtimer`, `sga` (Schadengutachten), `dkgk` (Kontrollgerätekarten) … | Service flags. Useful as `services`; `truckService` / `trailer_biggest` show heavy-vehicle capacity. |
| `phone`, `fax`, `directcall`, `email` (central `ksc@`), `emailtsc` (site address; **one is a person's name**), `sgamail` | Contact. Personal data possible in `emailtsc`. |
| `notificationText`, `…2`, `…3` (+ start/end Unix timestamps), `currentlyClosed`, `currentlyClosedMsg` | Notices and closures. `currentlyClosedMsg` often holds stale texts from 2020–2023 even with `currentlyClosed` 0. |
| `usenewtscmanagertime` | Unix date; on new sites it looks like an opening/activation date (Manching: 2026-09-17). Unconfirmed. |
| `times[]` (item level) | Opening hours per weekday → `opening_hours`. |

Fill rate and full field list: to be measured on the first complete set.

### Classification (proposal, not decided)

- All entries in this finder appear to be TÜV SÜD's own Service-Center → `pruefstelle`, `in_scope` if `huau` = 1, `tscStatus` = 1 and German. Anything else → `unknown` + `needs_review`.
- **Question for Nikos:** Wolnzach (`locationSign` 10810) carries the notice "Prüfstelle der Überwachungsorganisation – Leistungen und Entgelte … weichen ab": possibly a site operated differently (partner or other entity). Keep in scope and flag, or treat differently?

### Known coverage gaps

- Not known yet. To check: whether partner-operated or workshop (PSP-type) TÜV SÜD sites appear in this finder at all; whether TÜV Hessen's HU stations are in the same finder (TÜV Hessen belongs to the TÜV SÜD group; its records appear in the corporate locator).

### Validation plan (proposal)

- Per query: results strictly sorted by distance; count = 50 (capped) or < 50 (complete); centre returned.
- Per run: coverage disks cover Germany; drop check against the last passing full run (alert below −10 %).
- Cross-check: per-state pages (count per state), the published count of own Service-Center (an interview from 2015 said over 300; find a current figure), spot-checks: Altenau (opened August 2026, Laubenecker Str. 4), Manching (new, September 2026), Hammelburg.

### Compliance

- No key seen. Cloudflare blocks scripted requests (2026-10-05): the approval of 2026-09-28 does not cover getting past bot protection. **Management approved the browser-session route on 2026-10-05** within the boundaries listed under "Blocked by Cloudflare"; nothing beyond them.
- `emailtsc` can contain personal names: raw files and outputs stay private and git-ignored. Do not save cookie values in fixtures or the repo.

### Open issues / next steps

Done: endpoint and request body captured · cookieless test (blocked by Cloudflare, 2026-10-05).

Done: state-page test (blocked, 2026-10-05) · `robots.txt` (fine, confirmed by management).

Done: management approval of option 2 (2026-10-05) · build spec `sources/tuev_sued.md`.

1. **Build the adapter in Claude Code** per `sources/tuev_sued.md` (folders, pyproject mapping, snippet template, plan/check/build, simulation, tests).
2. **Simulated run** must PASS (100 % of fake sites, negative tests as specified).
3. **Sample pass** in the browser (80686, 24937, 01067): the snippet works end to end; does Flensburg return exactly 50?
4. First full run (2 passes expected), then spot-checks (Altenau, Manching, Hammelburg) and the comparison with a current published count.
3. **Cap check** (still open): in a browser, search a PLZ in a sparse area (e.g. 24937 Flensburg) and note whether exactly 50 come back and how far the last one is.
4. Ask whether TÜV Hessen's HU stations appear in the same finder.
5. Save the 80636 response as `4_reference/api_samples/tuev_sued/response_muenchen_80636_2026-10-05.json` (contains a personal email, so it is a recon sample, not a test fixture until anonymised).
6. Then: create the five folders (`1_scrapers/tuev_sued/…`), `0_instructions/sources/tuev_sued.md`, the pyproject mapping, and build the adapter (simulated → sample → full), following the KÜS layout.

---

## GTÜ

**Status:** investigating. Adapter built (`1_scrapers/gtue/src/adapter.py`, shared `2_core/grid.py`, `2_core/enrich.py`). **Full run `2026-09-30_1100_full` passed** (early stop, count match). **Scope rule changed 2026-10-07** (see "Site-type mapping"): 1,394 in scope, rebuilt offline as `2026-10-07_1726_rebuild`. Not yet signed off: the post-run checks below are open. Grid changes since the sample run, the reference query, rule C4 and the early stop: `0_instructions/sources/gtue_grid_changes.md`.

### How the locator works

- Public page: gtue.de "Vertragspartnersuche". It embeds a finder hosted by **SmartMaps** at `gtue-finder.smartmaps.app` (Leaflet map).
- Data endpoint: `GET https://gtue-finder-bff.smartmaps.app/addresssearch/ByBranchesAndAddress`
  - Parameters seen: `Top`, `MaxRadius` (metres), `Branches=GTUE`, `City`, `Zip`, `Street`, `IsoCountryCode=DE`.
  - Needs header `X-Api-Key` plus `Origin`/`Referer` = `https://gtue-finder.smartmaps.app`. Without them: 401. Called from the gtue.de frame: blocked by CORS.
  - **The key is not stored in this file or in git.** It goes in `.env` only. Current value: see `api-keys.md` in the project knowledge base (outside the repo, `../private/api-keys.md`).
  - Response reports `IsRequestAuthenticated: false`, `IsAccessApproved: false`. The key looks like a public/anonymous finder key.
- Also seen: `/api/getSearchFilters?Active=true&Route=kfz` (returned 500 during recon). It may list filter and service values. Not investigated.
- The locate button (⊙) does **not** send coordinates to the search: the browser position goes to SmartMaps `geocode` (POST, GeoJSON point, `REVERSE_GEOCODE`), and the resulting address is then passed to `ByBranchesAndAddress`. **The search accepts addresses only (City/Zip/Street).** Street-level queries work (tested: Berlin, 10119, Torstraße).
- `mapaction` requests (on map pan/zoom) carry only `apiKey` and `provider=smartmaps`. Most likely usage tracking, not a data source.
- SmartMaps requests (`settings`, `geocode`, `mapaction`, map tiles) pass the key **in the URL** as `apiKey=`. Strip it before sharing URLs. `settings` uses the key before any other exchange, so the key is most likely embedded client-side (JS bundle). Not yet confirmed.

### Tested behaviour

| Test | Result |
|---|---|
| `Top=20`, radius 50 km, München 80686 (2026-09-28) | 20 returned · `MaxCount 45` |
| `Top=1000`, radius 50 km | 20 returned · `MaxCount 45`. **`Top` is capped at 20 by the server.** |
| `Top=1000`, radius 1,000 km, München | 20 returned · `MaxCount 1157` (**not** the national total, see the finding below) |
| Paging: `Page=2` | Empty result list (`MaxPage 1`). **No paging.** |
| Paging: `Skip=20` | Ignored (same first result) |
| Sample run 7b (2026-09-28, 3 requests) | Works live. 1,000 km from 36251 Bad Hersfeld: `MaxCount 1430`. 2 grid squares: `MaxCount` 35 and 58 → split. 60 unique locations: 47 `pruefstelle`, 12 `partner_office`, 1 `unknown`. |
| Reference query, zip 99986 Niederdorla, 500 km (from 2026-09-29) | `MaxCount` **1,429** at start and end of the 2026-09-30 run; all 20 returned items have `Country = D` |
| Cap probe, München 80686 (2026-09-30) | `MaxCount` 1,045 at 455 km vs. 1,056 at 460 km → different → the server's cap still covers Germany from Niederdorla, so the early stop is valid |

**Finding (corrects the 2026-09-28 note): the differing national `MaxCount` values come from a server radius cap, not from entries abroad.** The server applies a smaller radius than requested (cap 500 km, confirmed by Nikos on 2026-09-29). From München the reported 1,157 equals a circle of about 512 km; from Bad Hersfeld the whole of Germany fits inside the cap (1,429–1,430). All 1,429 collected locations have `Address.Country = "D"`; none is abroad. The reference query therefore uses exactly the capped radius from a centre whose 500 km circle covers Germany (Niederdorla, 51.15844 N / 10.44595 E; the German border is at most 454 km away).

**Geocoding:** the server geocodes each PLZ to its **own** centre, not the GeoNames centroid, and always to the same centre for the same PLZ. In the first full run 928 of 2,000 queries were more than 3 km off (736 more than 5 km, up to 176 km, PLZ 03202); in the passing run 102 queries had an offset above 5 km. Examples: 88086 Immenstaad 15.26 km, 15837 Baruth/Mark 37.29 km. This is the main reason for the high request count.

- `Paging.MaxPage` always reports 1, even when `Count < MaxCount`.
- **Built-in completeness check per query:** every response includes `MaxCount` (the total within the radius). A query is complete only if `Count == MaxCount`.
- The server's search area seems to be a square window rather than a circle (returned entries lie up to 1.416 × the radius from the centre but at most about 1.09 × along either axis). Not used by the code; unconfirmed.

### Runs so far (all `full`, folder `1_scrapers/gtue/output/`)

| Run | Result | Requests | Note |
|---|---|---|---|
| `2026-09-29_1019_full` | **FAIL**, aborted | 2,000 (hard limit) | Old grid. All 1,429 IDs were found by request 952; the rest added nothing. Rebuilt offline as `2026-09-29_1619_rebuild` (exports labelled `grid-incomplete`, not a verified complete crawl). |
| `2026-09-29_1824_full` | **FAIL**, 214 coverage warnings | 262 live + 1,629 from the same-day cache | 1,429 collected, reference query MATCH, but squares open at minimum size. |
| `2026-09-29_1909_full` | **FAIL**, aborted | 590 | `ConnectionError` on PLZ 21483; 310 squares not queried; 1,421 collected (gap 8). |
| `2026-09-30_1100_full` | **PASS (early stop, count match)** | 899 of max 2,000 | 3,342 s. 1,429 collected, all in Germany; `N_circle` 1,429 = `MaxCount` 1,429; 576 squares complete, 59 re-queried, 259 split, 0 warnings, 0 errors, 60 skipped, 192 not queried (early stop). 855 `pruefstelle` · 483 `partner_office` · 91 `unknown`. Drop check ok (+0.0%). |
| `2026-10-07_1726_rebuild` | **PASS (inherited)**, offline, no network | 0 | Re-classification of `2026-09-30_1100_full` under the 2026-10-07 scope rule. Same 1,429 / 1,429 German locations, so the rebuild inherits the PASS (outcome `PASS (rebuilt from 2026-09-30_1100_full, …)`, no tag in the file names; the aggregator accepts it). **1,394 in scope**: 855 `pruefstelle` · 448 `office_hu_listed` · 91 `unknown_conflict`; out 35 `office_no_hu`. 219 in-scope rows carry `same_site_ids`. |

### Fetch strategy: adaptive grid (decided 2026-09-28, revised 2026-09-29)

1. Cover Germany's bounding box (approx. lat 47.2–55.1, lng 5.8–15.1) with ~100 × 100 km squares.
2. Query each square with the circle that fully covers it (radius = farthest corner from the **actual** server centre + 3 km margin).
3. `MaxCount ≤ 20` and the circle covers the square → complete. `MaxCount > 20` → split into 4 and repeat.
4. **Saver A:** if `Count == MaxCount` but the circle does not reach the square from the actual centre, re-query the same point once with a larger radius instead of splitting (status `requery`).
5. **Saver C:** same-day response cache (key = PLZ, city, radius); cache hits are not requests and do not count against the 2,000 limit. On for `full`; reference and probe queries bypass it.
6. Minimum square size ~1.5 km. If still incomplete → coverage warning; never cut off silently.
7. Dedupe by `ProviderForeignKey`.
8. **Reference query** (Niederdorla, 500 km) at start and end of a full run. **Rule C4:** PASS needs (a) every square complete **or** the early stop, and (b) `N_circle == reference MaxCount`, where `N_circle` = collected unique IDs within 500 km of the reference's actual centre. Rows with `Country != D` are never in scope.
9. **Early stop (rule E)**, on in the passing run: the grid stops once `N_circle` reaches the reference `MaxCount`; the reference is re-queried first and must still match. Open squares stay listed in `open_squares.csv`. Disabled automatically if the cap probe shows the server cap no longer covers Germany.
10. Polite pacing (3 s + jitter), sequential.

**Query points:** no coordinate query exists, so PLZ centroids from **GeoNames** (`DE.zip`, CC BY 4.0) are the query points; the response's `SearchData.Location` gives the centre the server really used. Grid logic lives in `2_core/grid.py`, independent of GTÜ, so other ÜOs with radius search can reuse it.

**Implementation notes:**
- Squares are laid out in the lat-51 projection, but coverage distances use great-circle distance.
- GeoNames cleaned: ~2,500 company PLZs (Großkunden) dropped; 8,309 regular PLZs remain (noted in the README with the CC BY 4.0 credit).
- Sea/abroad squares are skipped at every split level. Safety limit: a full run stops after 2,000 requests.
- If the reference centre is more than 5 km from the expected one, the run stops (`ReferenceCentreError`).
- An unexpected response shape stops the run. Connection errors are retried like timeouts.
- Expected request count in the simulation was 459–498; the real runs needed 899 (early stop) because of the geocoding offsets above.

**Known limitations:**
- Very dense spots: small squares still use circles of ≥ ~5 km radius. More than 20 locations in reach → coverage warnings. Without the early stop, 175–245 squares would stay open at minimum size (about 165 of them around 12 PLZs with far-off geocodes, chiefly 15837 Baruth/Mark and 15838 Kummersdorf-Gut; about 49 in Berlin). That is why the early stop is needed for a PASS.
- No resume: a stopped run restarts from the beginning (raw data is kept; the same-day cache helps within a date).
- Ideas not built: avoid PLZs whose geocode is more than 5 km off; street-level query points in dense spots; a square-window radius test (would need a few live requests to confirm).

**Run modes:** `simulated` (no network, 1,200 fake locations, must find 100%) · `sample` (5 live requests: reference query, cap probe ×2, 2 grid squares) · `full` (only with `confirm_full_run=True`) · `rebuild` (offline, from a run's `raw.jsonl`).
**Storage:** one folder per run, `1_scrapers/gtue/output/` (full, rebuild) or `test_output/` (sample, simulated), named `YYYY-MM-DD_HHMM_<mode>`; each holds `raw.jsonl`, `query_log.csv`, `run_summary.json`/`.md`, the exports and `open_squares.csv` if squares stayed open. Never written into an existing folder. A run that is not a PASS labels its exports `grid-incomplete` or `count-mismatch`.
**Baseline:** the most recent `full` run whose `run_summary.json` says PASS (and has no drop alert). See open issue 5.
**Politeness:** 3 s + jitter between requests, sequential, retries with backoff, stop on 401/repeated 429.

### Record structure (key fields)

| Field (path under `AddressItems[].BasicData`) | Meaning / note |
|---|---|
| `Identifiers.ProviderForeignKey` | **Stable GTÜ location ID.** Matches the `id` on the finder's list element and the gtue.de profile URL. → `location_id = GTUE-<id>` |
| `Address.CompanyName` / `CompanyNameAddon1` | Name. The addon often holds the owner ("Inh. …") but can also be a company or site name. **Personal data can appear in name, addon and email.** |
| `Address.CompanyNameAddon2` | Site label (e.g. "Prüfstelle Aubing"). The only field that tells an operator's sites apart when `CompanyName` is identical. → `name_addon2` |
| `Address.Street/Zip/City` | Street includes the house number. `HouseNo` is null. |
| `Geo.XCoord` / `Geo.YCoord` | **X = longitude, Y = latitude** |
| `Contact.Phone/Mobile/Email/Url/Url3` | `Url3` = gtue.de profile page |
| `MemoItems` (Type `OPENING_HOURS`) | Free-text opening hours with `<br/>` (12/20 in sample) |
| `MemoItems` (Type `CUSTOM`, Title `TerminUrl`) | Online booking link (3/20) |
| `MemoItems` (Type `JSON_INFO`) | Hidden `HiddenPoiInfos.Herkunft` (`E`/`G`) and `Stand` (data date) |
| `BranchListElements[]` | Code **`GTUP`** = "GTÜ-Ingenieurbüro mit amtlicher Fahrzeugprüfung vor Ort". `GTUE` = "GTÜ-Ingenieurbüro" (all records). **The code is the primary signal**, the text a cross-check. |
| `ObjectListItems[].Headline` / `.Description` | Services offered, tagged `G`/`B`/`T` (guess: official inspection / appraisal / Technischer Dienst) |
| `BusinessData.DateLastChange` | **Batch timestamp**, not usable for change detection. Our own snapshot diff is required. |

Sample fill rates (n=20): HU offered 19 · email 17 · phone 15 · "vor Ort" 13 · opening hours 12 · website 9 · booking link 3.

### Site-type mapping (assumption accepted by management, 2026-09-28)

| Herkunft | "Fahrzeugprüfung vor Ort" | Sample | Proposed `site_type` |
|---|---|---|---|
| `E` | yes (13/13) | 13 | `pruefstelle`: HU performed at this address |
| `G` | no (0/7) | 7 | `partner_office`: engineering office; inspections likely at workshops (PSPs) |

- The two signals agreed in 20/20 sample records. If they disagree → `unknown` + flag the row.
- Third signal (report only): ID prefix. All 13 `E` IDs start with `1`, all 7 `G` IDs with `00`.
- What `Herkunft` E/G stands for is **not documented by GTÜ** (probably the data source of the record); we only observe that E rows carry `GTUP` and G rows do not, and that one physical site is often listed twice, once as E and once as G.

**Scope rule (Nikos, 2026-10-07, replaces `in_scope = inspects_on_site`):** `site_type` keeps recording the three signals; the new column **`scope_tier`** decides `in_scope` (always `and Country == "D"`):

| `scope_tier` | Rows (2026-09-30 data) | `in_scope` | Why |
|---|---|---|---|
| `pruefstelle` | 855 | yes | E + `GTUP` + text: own on-site inspection |
| `unknown_conflict` | 91 | yes, `needs_review` | the three signals disagree; no certain cause to exclude (81 are G with code and text, 10 are E without) |
| `office_hu_listed` | 448 | yes | G office, no on-site code, but its service list has "Hauptuntersuchung Pkw". Hand check: 5 of 5 do HU |
| `office_no_hu` | 35 | **no**, `scope_reason` "no HU offered (finder service list)" | no HU service at all. Hand check: 5 of 5 do no HU (one is a lift and wind-turbine inspector) |

- Why the service list and not the E/G flag: the three official services (HU Pkw, Änderungsabnahme, BOKraft) are bundled identically on E, G and `unknown` rows (847 / 448 / 88), and the 5 + 5 hand checks (`4_reference/ground_truth/gtue_spotcheck_2026-10-07.csv`) showed the proxies "no opening hours / no `GTUP` / no E listing" do not separate real HU providers from non-providers.
- **Duplicates are not removed in the scraper.** 101 non-E rows share a site (same normalised street + PLZ, or within 150 m) with an E row; 70 of them also share phone, e-mail or website with that row (certain duplicates, my own matching, not hand-checked). The scraper adds **`same_site_ids`** (other German rows at the same site; information only) and keeps both rows; the aggregator merges them (`core.dedupe`, strong evidence), or flags the pair for review.
- The count 1,394 is far above GTÜ's published "700+ / ~800 own Prüfstellen" because about 450 rows are offices. Filter on `scope_tier = pruefstelle` for the stricter set.
- Analysis behind the rule (offline, 3 live requests of gtue.de detail pages with Nikos's approval): the detail pages carry no signal beyond `GTUP` and the "vor Ort" text; the raw JSON has no further discriminating field.

### Known coverage gaps

- None known for in-scope sites (Prüfstellen).
- Out of scope, for reference: GTÜ's 10,400–11,000+ Prüfstützpunkte are not listed in the finder.

### Data-model notes

- Exports: CSV (`;` separator, `.` decimal, IDs as text) is the machine-readable master. An `.xlsx` copy (PLZ/IDs as text, coordinates as numbers) is for Excel. **Don't save the CSV from Excel.**
- `Address.Country` is `"D"`, not `"DE"`.
- Columns added 2026-10-07: `scope_tier`, `scope_reason` (only for out-of-scope rows: "no HU offered (finder service list)", "outside Germany", "country missing"), `same_site_ids` (other German rows at the same site, joined with "; ", empty if none). `offers_hu` = "Hauptuntersuchung Pkw" is in the service list.
- One operator can run several sites (e.g. SCHORER + WOLF). Keep one row per site; group by operator later for M&A.

### Validation plan

- Per query: `Count < MaxCount` → incomplete → split (or re-query, saver A).
- Per run: **PASS (rule C4)** = no errors/abort **and** `N_circle == reference MaxCount` **and** either every square complete or the early stop triggered. Open squares are always listed.
- Per run: Prüfstellen count plausible vs. "700+ / ~800 own Prüfstellen" (855 in the first passing run: see above).
- Alert if German locations drop more than 10% vs. the baseline run.

### Compliance

- The API key is GTÜ's (via SmartMaps). **Use approved by management on 2026-09-28** (all ÜOs).
- Operator names and emails are personal data: outputs stay private and git-ignored.

### Open issues / next steps

Done: recon · parser · grid · enrichment · key-use approval · sample run 7b · restructure into run folders · reference query, rule C4, savers A and C, early stop · full run `2026-09-30_1100_full` (PASS) · 124 tests pass.

1. ~~Review the 91 `unknown` rows~~ **Decided 2026-10-07:** they are in scope (`unknown_conflict`, `needs_review`); 25 of them share a site with an E row.
2. ~~855 Prüfstellen vs. ~750 expected~~ **Superseded 2026-10-07:** 855 stay `pruefstelle`; 1,394 are in scope including 448 G offices that list HU and 91 conflicting rows. Report the tiers, not one number.
2a. **Aggregator (Nikos):** set `status_count` 855 → 1394 (and KÜS 582 → 587) in `2_core/master_inputs.py`; map `scope_reason` for GTÜ; add `scope_tier` and `same_site_ids` to `KNOWN_EXTRAS["gtue"]`; run `master_run dev` and check that both new rebuild folders qualify and how many E/G pairs merge (expected about 70; unmerged same-address pairs go to review); decide whether the master keeps `scope_tier` (otherwise E rows and offices look the same in HubSpot, every GTÜ site type maps to `unknown`).
2b. **Hand checks:** `4_reference/ground_truth/gtue_spotcheck_2026-10-07.csv` (5 + 5 G offices): Nikos's results (5 of 5 without HU listed do none, 5 of 5 with HU listed do HU) are recorded here; the label columns of the CSV itself are still empty. The question "is a Prüfer physically on site" is still open for the G offices (the checks showed they do HU, not where).
3. **Spot-check** Nürnberg (and one border region) against Google Maps.
4. **Request count:** 899 vs. 459–498 simulated. Cause identified (server geocodes PLZ to its own centre); no further saver built. Decide whether the far-off PLZs or street-level points are worth building.
5. **Baseline selector:** the passing run names the FAIL run `2026-09-29_1824_full` as its baseline ("Last full run: ok"), although the rule says the most recent PASS. Check the selector (the counts are equal, so no harm yet).
6. Minor code issues (`gtue_grid_changes.md` §8): the request-limit hit is logged as `error`; if the reference query itself fails, `squares_not_queried` stays 0.
7. Update `0_instructions/sources/gtue.md` (see project-wide items).
8. Only then: status **working** and record the verified count.
9. Later: find where the key comes from (JS bundle vs. issued by a request). If it's in a bundle, the scraper can read it from the page each run, so a rotated key doesn't break the monthly run. Until then a 401 stops the run with a clear message.

---

## DEKRA

**Status:** working (Nikos, 2026-10-05). Adapter `1_scrapers/dekra/src/adapter.py` (single module like GTÜ), control notebook `1_scrapers/dekra/notebook/dekra_control.ipynb`, tests `1_scrapers/dekra/tests/test_dekra_adapter.py` (44). Full runs `2026-10-05_1140_full` and `2026-10-05_1146_full` (folders in `1_scrapers/dekra/output/`) both **PASS (6 responses consistent)**. Recon (2026-09-30): `src/recon.py` (`fetch` live, `analyze` offline), run folder `1_scrapers/dekra/test_output/2026-09-30_1831_recon/` (contains contact data, git-ignored).

### How the locator works

- Public pages: `dekra.de/de/pruefstandorte/` and `dekra.de/de/standorte/` (Nuxt front end, FirstSpirit CMS). The finder is JavaScript-rendered; the HTML contains no location data.
- Data endpoint: `POST https://www.dekra.de/api/locations/fromBoundaries` (same origin as the site).
  - **No API key, no cookies.** Standard headers only (`Content-Type`/`Accept: application/json`, `Origin`/`Referer` dekra.de).
  - Request body (JSON): `locale`, `searchedLocation {lat, lng}`, `tagFilters []`, `initialSearch`, `boundaries {latFrom, latTo, lngFrom, lngTo}`, `types [<UUID>…]`, `commercialVehicles`, `locationsWithoutType`.
  - Response: `{"isNextLocation": bool, "locations": [...]}`. No total/count field.
- It is a **map-bounds API**: the server returns every location inside the rectangle. The finder sends the visible map area, so UI counts change when the map moves.
- Filter panel ("Standorttyp") maps to `types` (one UUID per radio button). The checkbox "Für Nutzfahrzeuge (Nfz) geeignet" maps to `commercialVehicles`. "No filter" in the UI sends all 7 type UUIDs explicitly, still with `locationsWithoutType: false`, so **untyped locations never appear in the finder**.
- Each location also has a server-rendered page on dekra.de (`linkWebsite`, e.g. `/de/moabit/`) with address, coordinates, opening hours and a booking link.
- Booking system: `booking.dekra.com` (separate single-page app, keyed by booking UUIDs). Not investigated.

**Type UUIDs (confirmed against the records):**

| UUID | Type | Key |
|---|---|---|
| `2fbbb592-6bd8-4b57-860d-52b34c6cbbb1` | Automobil Prüfwesen und Gutachten | `automotive_testing` |
| `68a4d9fa-9295-4a62-8f2b-db5dcfc11756` | Industrie, Bau und Immobilien | `location_type_DE_Industry_construction_real estate` |
| `84d03801-02a5-4a5c-b0c1-4eb993b164a9` | Fahrerlaubniswesen | `location_type_DE_Driving license` |
| `051adab5-009c-4a62-8b86-e2c05ea167eb` | Begutachtungsstellen für Fahreignung | `driving_suitability` |
| `b6fa213c-6633-404b-94d7-7bebba82c25c` | Arbeitsmedizin | `occupational_medicine` |
| `c1fa7185-f287-4803-8131-cd70d4cf160b` | Bildung | `location_type_education` |
| `079443a2-f91b-40fc-bbd0-8a4151dc4b95` | Zeitarbeit | `location_type_work` |
| `9de4a8cc-f470-445b-a69e-06d233562a95` | Automobil Prüfwesen (**AT type**, not in the finder) | `location_type_AT_Automotive_Testing` |
| `4021f9dc-8f2a-4ade-a0a4-ce74379c1089` | WHG Standort (water-law sites, out of scope) | `whg_location` |

About 50 further types exist for other countries (FR, PL, MA, US, BE, SK …); see the recon `summary.md` §3.

### Tested behaviour (recon, 2026-09-30)

| Request | Result |
|---|---|
| R1 Automobil filter, browser box (lat 46.7–55.4, lng 1.8–16.0) | 797 |
| R2 same, `searchedLocation` München | Identical IDs; only `distance` changes |
| R3 all 7 types, Germany box (lat 46.9–55.4, lng 5.5–15.4) | 1,071 (= all DE records of R13) |
| R4–R7 the four quarters of R3 | Union = 1,071, identical to R3 → **no cap visible** |
| R8 Automobil filter, world box | 822 = R1 + 25 abroad (SK, CL, BR, MA, PT). World box accepted. |
| R9 `types: []`, world box | **3,017 = full universe.** Empty `types` means "no filter", not "nothing". |
| R10 `types: []` + `locationsWithoutType: true` | Same 3,017 IDs (flag has no effect when `types` is empty) |
| R11 Automobil + `locationsWithoutType: true` | 1,652 = R8 + 830 untyped records |
| R12 Automobil + `commercialVehicles: true` | 432 = exactly the R8 records with `suitableForCommercialVehicles = true`. Unticked = no restriction. |
| R13 all 7 types, world box | 1,099 |

Per request ~1–12 MB (universe 12.3 MB). `isNextLocation` was `false` in all 13 responses.

**Finding: DEKRA's own type filter misses German locations.** Universe minus R13 = 1,918 records; 652 of them in Germany: 545 WHG sites (out of scope), **105 with no type** and **2 with the AT type** ("Standort Breitscheid", "Station Bietigheim-Bissingen"). Some untyped ones look like real sites (Station Niestetal, Standort Versmold, Standort Karlsruhe, Standort Nürnberg, Standort Bad Arolsen, Standort Duisburg). Full list: recon `summary.md` §2. Consequence: **collect the full universe and filter on our side**, never by DEKRA's type filter.

### Fetch strategy (built)

`dekra.run(mode=...)` sends 6 requests, all with `types: []` (no type filter, so untyped records come back), sequential, 5 s + jitter apart, 60 s timeout, one retry on timeout/connection error, stop on any 4xx/5xx, non-JSON answer, unexpected shape or `isNextLocation == true`:

1. `world`: the whole map (lat −60..75, lng −180..180). **This is the data source.** The earlier plan (Germany box + quarters) would miss DE-coded records whose coordinates lie outside Germany's box.
2. `de_box`: Germany's box (lat 46.9–55.4, lng 5.5–15.4).
3. `q1`–`q4`: the four quarters of that box.

**PASS** needs: no error or abort, no duplicate ids, and by record id: quarters == Germany box, Germany box inside world, and world records inside the box == Germany box. A failed run writes its exports with `unverified` in the names. Modes: `simulated` (offline fake API, 100 synthetic records, must PASS), `sample` (1 live request, quarter `q4` only, reports `SAMPLE`, never PASS), `full` (needs `confirm_full_run=True`). Options `exports=` and `save_raw=` decide what is written; the checks always use the full responses in memory. The drop check compares the in-scope count with the newest earlier full run that passed (alert below −10%).

### Full-run results (2026-10-05)

| | |
|---|---|
| Requests (records) | `world` 3,017 · `de_box` 1,801 · `q1` 643 · `q2` 275 · `q3` 592 · `q4` 291; all HTTP 200, 38.7 s, universe 12.4 MB |
| Completeness | all four checks ok, in both runs |
| Collected | 3,017 unique records, **1,723 with country DE** (same as the recon five days earlier) |
| **In scope** | **826** = 796 `pruefstelle` (Automobil-typed) + 30 `unknown` (28 untyped, 2 AT-typed, with a vehicle-site name) |
| Out of scope | outside Germany 1,292 · WHG site only 545 · other, not vehicle-related type 275 · untyped/AT-typed without vehicle-site name 77 · country missing 2 |
| Flags (in scope, a row can carry several) | `thin` 32 · `untyped` 28 · `non_dekra_operator` 28 · `psp_suspect` 10 · `no_vehicle_disclaimer` 6 · `at_type` 2 |
| Needs review | 58 in-scope rows (flags only; nothing excluded) |
| Drop check | second run: 826 → 826 (+0.0%), baseline = first run |
| Files | first run: all four exports + `raw.jsonl` (32 MB, the only raw copy of 2026-10-05); second run: `dekra_pruefstellen_<date>.csv` only, no raw |

**IDs are stable.** Comparing the recon (2026-09-30) with the first full run (2026-10-05): 3,016 of 3,017 ids are in both, one id was replaced by one new id. 752 records changed content, almost all in `openingHoursAreas` (749); the rest are a handful of notices, phone numbers and names. `distance` is ignored.

### Scope rule and flags (decided 2026-10-05)

**In scope** = `countryResolved.code == "DE"`, not a pure *WHG Standort*, and either typed *Automobil Prüfwesen und Gutachten*, or untyped / AT-typed (*Automobil Prüfwesen*) with a vehicle-site word in the name (Station, Standort, Niederlassung, Außenstelle, Prüfstelle, Gutachten, GA-Büro, Automobil). **No further check or exclusion** (see the DEKRA exception in the project-wide items). Doubts are only marked in `doubt_flags`; `needs_review` is set by every flag except `non_dekra_operator`:

| Flag | Meaning |
|---|---|
| `untyped` / `at_type` | no Automobil type; in scope only because of the name |
| `thin` | no profile page, no booking link, no opening hours, no free text |
| `psp_suspect` | parenthesis or dealer / premises words in the name (possible workshop site) |
| `no_vehicle_disclaimer` | free text says the site is not a Kfz-Prüfstelle / does no vehicle testing |
| `non_dekra_operator` | operator line (`name2`) is not DEKRA, i.e. a partner office; informational |

**Why include everything (evidence from the 86-row labelling sample, labelled by Nikos, file received 2026-10-05).** `hu_on_site` was labelled for 81 rows: 70 of the 74 Automobil-typed rows are "yes" (person-named partner offices 12/12, Niederlassungen and Außenstellen 13/13); the other four are one "no" (Kfz-Prüfstelle Klötze), two "still open?" and one PSP. All 48 rows with a booking link or an HU mention are "yes", but 25 of the 30 rows with neither are "yes" too, so the text signals only prove "yes". The untyped / AT-typed rows are mixed (3 yes, 4 no, 5 not labelled). The free-text disclaimers ("keine Kfz-Prüfstelle", "nur Industrie-Dienstleistungen") were the only working negative signal. A check of the 19 "thin" records (possibly closed) by Nikos showed no pattern. `site_kind` was never filled in. **PSPs do appear in the finder** (e.g. a site at a KIA dealership), contrary to the earlier note; they are flagged `psp_suspect`, not excluded. Files in `4_reference/ground_truth/` (all contain personal data, git-ignored): `dekra_labelling_sample_FINISHED.csv`, `dekra_labelling_s2_matches_2026-09-30.csv`, `dekra_psp_review_list_2026-10-05.csv`, `dekra_thin_records_2026-10-05.csv`. The Guide sheet of the sample still lacks the labelling rules (they were cut off in the request).

The count (826) is above DEKRA's published "über 600" HU locations. That is accepted: the sample suggests the surplus is mostly partner offices and Niederlassungen that do perform HU, plus a few PSP-like, closed or non-HU records that the later validation has to catch.

### Record structure (key fields)

| Field | Meaning / note |
|---|---|
| `id` | **Stable location ID** (UUID, unique in all responses, stable between 2026-09-30 and 2026-10-05). → `location_id = DEKRA-<id>`, `source_id = id`. |
| `uniqueImportId` | Import ID (SAP), 87% filled, 1 duplicate value. Not exported. |
| `name`, `name2`, `additionalAddress` | → `name`, `name_addon` (operator), `name_addon2`. **Personal data can appear in `name`** (person-named partner offices). |
| `street`, `houseNumber`, `zipCode`, `city` | → `street` (street + house number, not repeated if already in the street), `zip` (`.strip()`, German 4-digit codes padded to 5), `city`. DEKRA's own `state` field is 4% filled and not used; `state` comes from the PLZ (GeoNames). |
| `latitude`, `longitude` | → `lat`, `lng`. Always filled. |
| `countryResolved.code` | → `country` (ISO, DE, SK …). 2 records have `null` (out of scope, "country missing"). |
| `locationTypesResolved` | → `dekra_types`. Empty on 830 universe records; a location can carry several types. |
| `suitableForCommercialVehicles` | → `nfz` (bool). |
| `specialServicesList` | → `services`. LapID Führerscheinkennzeichnung/-kontrolle, Ausgabe/Auslesen Fahrerkarte, DEKRA Siegel Gebrauchtfahrzeuge. **Not an HU signal.** |
| `freeText1`, `freeText2`, `services`, `currentInformation`, `openingHoursAreas`, `name`, `name2` | searched for "Hauptuntersuchung" / "HU" → `hu_mention` (field names). `freeText1` carries most mentions. |
| `openingHoursAreas` | → `opening_hours`, flattened to e.g. "Fahrzeugprüfungen: Mo–Do 08:00–18:00, Fr 08:00–16:00". The times are local although stored as `…Z`. `displayed` inside is a CMS flag (always false). |
| `linkOnlineAppointment.to` | → `booking_url` (601 records; 563 contain `/location/<uuid>`, 3 UUIDs shared by two records; the booking UUID is **not** the record `id`). |
| `linkWebsite` | → `dekra_profile_url` (`website` stays empty: DEKRA has no own site per location). |
| `phone1`, `phoneMobile`, `email` | → `phone`, `mobile`, `email`. Personal data. `fax` and `department*` are not exported. |
| `tags`, `deleted`, `services` (RichText), `legalEntityId`, `image` | not exported (`deleted` is always false; `services` is empty in all but one record). |

Extra columns from the adapter: `same_address_count` (rows in the universe, all countries, with the same normalised street + PLZ, itself included; 1 if the address is missing), `doubt_flags`, `site_type`, `site_type_evidence`, `needs_review`, `needs_review_reason`, `in_scope`, `scope_reason`, `plz_distance_km`. No record has a modification date, so staleness cannot be seen in the data.

### Data-quality notes

- **Duplicates at one address:** e.g. Stuttgart HQ Handwerkstr. 15 (~20 records), Hamburg ("Niederlassung Hamburg" / "Automobil Niederlassung Hamburg", same booking UUID), Oberursel (Station / Standort, same booking UUID), Koblenz (2). **Not merged**: every record keeps its row; `same_address_count` shows them. Merging into one row per physical site belongs to the master step.
- **Country/coordinate errors:** a Saint-Étienne record is labelled DE; INFATEC Warburg has coordinates outside Germany. `country` is `countryResolved.code`; `plz_distance_km` and `needs_review_reason` catch far-off coordinates. Records outside Germany's box are only returned by the `world` request, which is why it is the data source.
- **PLZ:** 14 DE records are not in GeoNames; 13 are fixed by `.strip().zfill(5)`. Some foreign 5-digit postcodes collide with German PLZ (65 non-DE records): never infer the country from the PLZ alone.

### Known coverage gaps

- None known. Possible surplus rather than gaps: PSP-like, possibly closed and non-HU records are included on purpose (flagged).
- Out of scope, for reference: DEKRA's own count of Prüfstützpunkte in workshops is not known; some PSP-like sites do appear in the finder.

### Validation plan

- Per run: PASS needs the four completeness checks and 0 errors (implemented). `isNextLocation == false` is enforced (a `true` stops the run).
- Per run: drop check on the in-scope count against the last passing full run, alert below −10% (implemented).
- Done: `id` stability between runs (3,016 of 3,017 on 2026-09-30 vs 2026-10-05).
- Not done: Nürnberg spot-check against Google Maps; comparison of the 826 with "über 600".
- Later, outside this project: validation of flagged rows in the competitor map.

### Compliance

- No key involved. Automated collection covered by the management approval of 2026-09-28 (all ÜOs).
- Contact fields and person-named entries are personal data: raw files, run folders, the labelling files and outputs stay private and git-ignored.

### Open issues / next steps

Done: recon · parser · scope rule and flags · export · notebook · tests · full runs (PASS) · labelling sample and checks of doubtful records.

1. Optional: Nürnberg spot-check and the comparison with "über 600".
2. Merge rows of the same physical site (same address, same booking UUID) in the master step, together with cross-ÜO dedupe.
3. Validate flagged rows (PSP-like, thin / possibly closed, untyped, disclaimer) after inclusion in the competitor map (not part of this project for now).
4. Create `0_instructions/sources/dekra.md` from this section if wanted; decide what to do with the labelling files and the Guide sheet (rules still missing).
5. Optional later: `booking.dekra.com` location list as a second cross-check.
6. GitHub and automation (monthly run) at the end, together with the other scrapers.

---

## KÜS

**Status:** **finished and verified (2026-10-05).** Two full runs PASS and agree completely; the production setting is `RADIUS_KM = 100`, `COVER_KM = 50` (now the default of `kues_run.run`). **Scope extended 2026-10-07:** 587 in scope (see "Classification"), rebuild `2026-10-07_1734_rebuild` PASS. Adapter `1_scrapers/kues/src/` (`fetch.py`, `parse.py`, `classify.py`, `run.py`, plus `simulate.py`, `report.py`, `recon.py`, `recon_analysis.py`), shared `2_core/radius_cover.py` and `2_core/export.py`, control notebook `1_scrapers/kues/notebook/kues_control.ipynb`, tests `1_scrapers/kues/tests/` (105) and `2_core/tests/test_radius_cover.py`, `test_export.py` (19). Full recon notes, field table, coverage design and the pattern analysis: `0_instructions/sources/kues.md`. Live requests of the build session: 24 (recon 22, sample 2) of a budget of 25.

### How the locator works

- Public page `kues.de/standortsuche/`; the theme script `partnersuche.js` calls `GET https://webservice.kues.de/api/kues/partnersuche?plz=<PLZ>` and `.../partnersuche-kues-technik?plz=<PLZ>`. **No key, no cookies, no special headers** (a bare GET works). Response: a plain JSON array of Partner records, no total, no paging.
- **Fixed radius of 50 km**, count varies (3 to 98 at 50 km, 231 at 100 km), no count cap seen. The PLZ is the only query input (`latitude`/`longitude` are ignored). An undocumented `km` parameter sets the radius on both endpoints (capped at 100 km: `km=1000` from Niederdorla returned 30 records, max 96 km); the page never sends it, **the adapter sends `km=100` in production** (the 100 km crawl finds exactly the records of the 50 km crawl, see "Runs so far"). No parameter covers all of Germany.
- The server geocodes each PLZ to its own centre (0.6–5.2 km from the GeoNames centroid in the recon, always the same for one PLZ); an unknown PLZ answers `[]`.
- The technik list ("KÜS Technik (EBE Begutachtungen)") is a **subset** of the main list in both regions tested: it adds the label `both`, no locations.

### Fetch strategy (built)

Hex lattice over Germany (production: radius 100 km, `COVER_KM` 50, spacing 73.6 km, 84 planned queries; at 50 km: `COVER_KM` 25, spacing 36.8 km, 341 planned), each point snapped to a regular GeoNames PLZ, then a fill-in loop (rule A: PLZ still more than `COVER_KM` from every actual centre; rule B: sites more than 0.9 x radius from every centre) of at most 100 queries. Sequential, 3 s + jitter, 30 s timeout, 3 attempts with backoff, stop on 401/403, a second 429, non-JSON, an unexpected shape and at 900 requests. **PASS** (full) = no error/abort, every planned query made, rules A–D hold (also C and D on the technik list), drop check ok (alert below −10 % against the last passing full run). A run that is not a PASS labels its exports `grid-incomplete` or `unverified`. Modes: `simulated` (fake API, ~1,500 sites, must PASS; negative tests: a capped API and a dropped site must FAIL), `sample` (live, 80636, 2 requests, never PASS), `recon` (offline, saved response), `rebuild` (offline, from `raw.jsonl`, re-checks the rules), `full` (needs `confirm_full_run=True`).

| | |
|---|---|
| Size of a full run (production, `km=100`) | 84 planned + 7 fill-in = **91 PLZ queries, 182 requests** with both lists, **12.5 min** measured (91 requests, ~6 min with `FETCH_TECHNIK = False`). The 50 km design: 341 planned + 27 fill-in = 368 queries, 736 requests, 55 min measured. Request ceiling 900 |
| `COVER_KM` | 50 at 100 km, 25 at 50 km (half the radius). At 50 km: measured maximum site-to-own-PLZ-centroid distance 12.8 km (413 sites, p99 8.4 km), 25 + 12.8 < 50 with room for rural outliers; 30 km would save ~100 queries but leaves no margin for a 20 km site |

### Runs so far

| Run | Result |
|---|---|
| `2026-10-05_1635_simulated` (`test_output/`) | **PASS**: 1,500 of 1,500 sites found, 341 planned + 11 fill-in queries, 704 requests, rules A–D ok |
| `2026-10-05_1635_sample` (live, 80636) | 2 requests; 83 records (33 flag 1, 50 flag 0), 23 also in the technik list; **same ids, same order, 0 changed fields** as the saved browser capture |
| recon runs on the saved 80636 response and the Dortmund 100 km response (offline) | export and flags as expected |
| `2026-10-05_1704_full` (`output/`, live, **50 km**) | **PASS**, 55 min, 736 requests (341 planned + 27 fill-in A), 340 centres; rules A–D ok on both lists; 20 uncovered PLZ answered empty (note); **1,102 records, 582 in scope**, 520 partner offices, 13 needs review, 46 PLZ-only; 350 in both lists, 0 technik-only; largest answer 135 records, max `entfernung` 50.0 km |
| `2026-10-05_1812_full` (`output/`, live, **100 km**) | **PASS**, 12.5 min, 182 requests (84 planned + 7 fill-in A), 91 centres; rules A–D ok; largest answer 258 records, max `entfernung` 99.9 km; **identical to the 50 km run**: 1,102 records, 582 in scope, 0 ids in one run only, 0 content differences (`report.compare_runs`); warning: 2 server centres 14 and 21 km from the GeoNames centroid (PLZ 14778, 12559) |
| `2026-10-07_1734_rebuild` (`output/`, offline, from `2026-10-05_1812_full`) | **PASS**, rules A–D re-checked and ok; no requests. Same 1,102 records; **587 in scope** (582 `pruefstelle` + 5 `unknown`), 515 partner offices out; drop check ok (582 → 587, +0.9 %); 13 needs review |

### Classification (decided 2026-10-05)

> **Update 2026-10-07 (rebuild `2026-10-07_1830_rebuild`):** the rules below still describe how `site_type` is set, but `in_scope` is now true for all records except the 3 hand-checked non-Prüfstellen: **1,099 in scope**, with the new column `psp` (`Yes` = `pruefungvorortmoeglich = 0`, inspects at PSPs, 517 records; `No` = `= 1`, 582). Where this section says "out of scope" or "587", read "`psp = Yes`" and "1,099". The aggregator carries `psp` into the master. The reasoning for the change is not recorded in this doc yet.

`pruefungvorortmoeglich` = 1 → `pruefstelle`, `in_scope`, evidence `vorort:1`; 0 → `partner_office`, out of scope, `vorort:0` (same category as GTÜ's offices without on-site inspection); anything else → `unknown`, needs_review. **Exception (Nikos, 2026-10-07, include unless certain cause):** a `0` record whose own name says Prüfstelle / Prüfzentrum / Prüfingenieur (flag `name_pruefstelle_vorort0`) contradicts the flag, so it is kept: `site_type = unknown`, `in_scope`, needs_review (5 records: adressids 19735, 24525, 55830, 56021, 57750). Three records of that kind were hand-checked as not Prüfstellen (a Gutachter, a workshop that is a PSP, a non-Prüfstelle; adressids 56492, 28704, 51603) and stay out with `scope_reason` "checked by hand: not a Prüfstelle" (`classify.VERIFIED_NOT_PRUEFSTELLE`). The "strong evidence" flag (two of station word, website, business e-mail) was **not** used: all three hand-checked records carried it and none was a Prüfstelle. The other 512 `0` records stay out (the ÜO's own field, and 121 of 520 belong to a licensee with another `1` site, so the field does separate the addresses; 13 share an address with an in-scope row). Review flags (set `needs_review`): `name_pruefstelle_vorort0`, `vorort1_no_address`, `vorort_invalid`, `name_psp_like`. Info flags (no review, no scope effect): `vorort1_weak_evidence`, `vorort0_strong_evidence`, `contact_missing`, `same_address_as_inscope_site`, `shared_address_in_scope`. Columns `name_class`, `licensee_has_inscope_site`, `licensee_site_count`, `same_address_count`, `kues_list`, `location_precision`, `display_name`, `is_hdp_site` (renamed from `is_liria_site` on 2026-10-08; always empty, superseded by the HDP own-site removal in the aggregator). Dedupe by `adressid` only, never by address or licence code.

**Pattern analysis (independent subagent, 449 distinct sites from 6 regions; not ground truth).** The flag is internally consistent and plausible (52 of 55 Prüfstelle-named records have flag 1); all 22 `keinestrasse = 1` records have flag 0; 59 of 222 flag-1 records have no station word, no website and no business email; no PSP-like names at all (the finder lists licensees, not the ~8,000 PSPs); the technik list has no scope information; SV Team AG, Has4, SVZ, PWE, KPN are separate addresses. **Seven Has4 Technologies records (licence I1122, NRW, flag 1) use the HolDeinePlakette email domain: HolDeinePlakette's own sites (the HDP removal matched 17 of the 19 HDP sites to KÜS rows, see "HDP own sites").** Nine open questions for Nikos / management are in `sources/kues.md` §5b; the 19-row spot-check list is `4_reference/ground_truth/kues_spotcheck_2026-10-05.csv` (`hu_on_site` and `notes` empty).

### Known coverage gaps

- None known. Two independent crawls (different centres, different radii) found the same 1,102 records, no count cap appeared (largest answer 135) and rules C and D watch for one in later runs. The east is thin (Sachsen 24 records, Thüringen 12, Mecklenburg-Vorpommern 9, Bremen 7); both radii agree. One data inconsistency: adressid 4125 has `lizenznehmer` "K0495,K0229" in the main list and "K0229" in the technik list; the parser keeps the first value, so that one row carries two comma-joined licence codes (affects `licensee_site_count` for it only).
- Out of scope, for reference: KÜS says it supports ~8,000 Prüfstützpunkte; they are not in the finder as far as the 449 records show.

### Validation plan

- Per run: rules A–D (implemented), no error or abort, drop check against the last passing full run (implemented).
- Done after the full runs: the technik list is a subset (0 technik-only records nationally); flag-1 share 582 of 1,102 (53 %); weak-evidence share 157 of 582 (27 %), as in the 449-site pattern analysis. Still open: compare 587 (582 before 2026-10-07) with the unverified "rund 400 eigene Prüfstellen" (probably KÜS-owned only; ours includes partner-run sites).
- Hand labels: the 19-row spot check decides whether the vorort rule is right.

### Compliance

- No key involved. Automated collection covered by the management approval of 2026-09-28 (all ÜOs); politeness rules of the project plan §7 are built in.
- Names, emails and phones of operators are personal data: raw files, run folders, the recon folder, `pattern_analysis.md`, the spot-check file and exports stay private and git-ignored. The test fixture `response_muenchen_80636_2026-10-05.json` is anonymised (fake names, emails, phones, streets; coordinates jittered).

### Open issues / next steps

Done: recon · coverage design and shared module · adapter · simulated and sample runs · notebook · tests · pattern analysis and the safe-to-automate flags · **full runs at 50 km and 100 km, both PASS and identical (2026-10-05) · 100 km is the production setting.** Monthly runs use `RADIUS_KM = 100`, `COVER_KM = 50` only (notebook section 3e, `confirm_full_run = True`, about 13 min).

1. Answer the nine questions in `sources/kues.md` §5b; label the spot-check list.
2. ~~Create `4_reference/hdp/hdp_sites.csv`~~ **No longer needed:** HolDeinePlakette (HDP) is the company, and its own sites come from the HDP scraper (`1_scrapers/hdp/`) and are removed by the aggregator (`2_core/hdp_removal.py`); `is_hdp_site` stays empty. Open there: Fürth and Dortmund (see "HDP own sites").
3. Technik query: confirmed as a subset, it only adds the label `kues_list = both`. Keeping it costs ~6 min per run; `FETCH_TECHNIK = False` halves the requests. Decision to Nikos.
4. Optional: find the page that publishes "rund 400 eigene Prüfstellen"; split comma-joined `lizenznehmer` values (adressid 4125) if licence-level analysis needs it.
5. GitHub and automation (monthly run) at the end, together with the other scrapers.

---

## FSP

**Status:** **working (2026-10-06); first live full run PASS, 169 sites in scope.** Adapter `1_scrapers/fsp/src/` (`fetch.py`, `parse.py`, `robots.py`, `recon.py`, `report.py`, **`run.py`** = production modes `simulated` / `sample` / `full` / `rebuild`, one in-scope CSV `fsp_pruefstellen_<date>.csv` + `run_summary` per run, list pages stored gzipped; `simulate.py` = fake fsp.de with known truth), control notebook **`1_scrapers/fsp/notebook/fsp_control.ipynb`** (offline rebuild of the recon, inspect, simulated, sample, full with `confirm_full_run`, run comparison), tests `1_scrapers/fsp/tests/test_fsp.py` + `test_run.py` (45, offline; simulated run must find 31 of 31, negative tests: ignored `&page=N`, Cloudflare page, silently dropped HU page, unreadable sitemap-only page, `d=` without override, `full` without confirmation). The one-off `python -m fsp.recon` stays for exploration (writes the extra `fsp_all` / `fsp_hu` / sitemap CSVs and report). Offline run on the saved real data: `output/2026-10-06_1035_rebuild/` **PASS, 169 in scope**, 15 checks ok. Two live runs: `test_output/2026-10-06_0954_recon/` (all-services list, r=2000 check, sitemap, 2 detail pages; 33 requests) and `test_output/2026-10-06_1022_recon/` (HU-filtered list, 15 requests); their offline rebuild `output/2026-10-06_1025_rebuild/` (CSVs and report; no requests). **Not done:** a full detail-page crawl (coordinates, operator), a production run mode with only the in-scope CSV. FSP is the TÜV Rheinland partner network ("über 125 Prüfstellen" claimed; 750+ partners); 103 of 122 booking links on the cards point to `tuv.com`.

### How the locator works

- `GET https://www.fsp.de/standorte?lat=&lng=&r=&page=`: Laravel + Livewire, **server-rendered HTML**, no JSON API (the Livewire endpoint is ignored). Result cards are `div.location.card`, 12 per page; the pagination bar (`nav[aria-label=Seitennummerierung]`) lists the pages and states the highest one. No "N Standorte" total anywhere on the page.
- Card fields: distance badge, type badges (`Prüfstelle`, `Gutachtenbüro`; a card can carry both), name, address (street, PLZ, city, Google Maps link), e-mail (Cloudflare-obfuscated, decoded by XOR with the first byte, then HTML-unescaped), phone, optional website, opening hours per weekday (free text; second slots with "&", "geschlossen", notes such as "Hinweis: …"), optional booking link, detail link (`/standorte/<slug>`, query string stripped; **the slug is the ID**, FSP's own spellings and typos included). **Not on the cards:** coordinates, operator or partner name, services, vehicle types. The detail pages show services (e.g. "Hauptuntersuchung (HU) nach §29 StVZO"), a "Hinweis" with the partner name, and more (only 2 detail pages looked at).
- Each list page is ~2.8 MB (one decorative SVG); parsed with BeautifulSoup (`html.parser`), never with regex over the page.

### Tested behaviour (2026-10-06)

| Test | Result |
|---|---|
| Plain `requests` session with browser-like headers (`robots.txt`, then list pages, sitemap, 2 detail pages) | **33 requests, all HTTP 200, no Cloudflare block** (server header `cloudflare`; no challenge, no `cf-mitigated`, no "Just a moment"). Playwright not needed and not used. The block checks (403/503, "Just a moment", `cf-chl`, `cf-mitigated`, result page without cards and without the results heading) are built in and covered by tests |
| Pagination via direct GET `&page=N` | **Confirmed.** Page 1 and page 2 hold 12 different slugs each; the whole run has 313 unique slugs over 27 pages, 0 duplicates |
| `r=1000` from 51.1634 N / 10.4477 E, all services | 27 pages declared and 27 returned (26 × 12 + 1 = 313); page 28 is a real empty page (heading present, zero cards), which stops the loop. Distances sorted ascending across all pages, 28.2 to 413.5 km |
| `r=2000`, page 1 | Same 27 pages declared, same 12 slugs in the same order: **no difference** from `r=1000` (page 1 only; later pages not compared) |
| Sitemap `https://www.fsp.de/sitemap.xml` | Plain urlset (no index), 428 URLs: 315 `/standorte/<slug>`, 113 others; `lastmod` on all 315 slug URLs (2022-08-01 to 2026-10-01; the 2 sitemap-only pages: 2024-04-19 and 2024-04-23); robots.txt has no `Sitemap:` line |
| Cross-check | sitemap 315 · all-services list 313 · overlap 313 · **in the sitemap only 2** · in the list only 0 |
| The 2 sitemap-only slugs (detail pages fetched, 2 requests) | `kfz-pruefstuetzpunkt-bad-salzungen`: HTTP 200, badge Prüfstelle, services include HU nach §29 StVZO (a possible miss); `kfz-sachverstaendigenbuero-schmidt-und-partner-schwarzmeier`: Gutachtenbüro, "Partnerbüro Untergruppenbach", PLZ and city only. **Why they are not in the list is not known** |
| HU-filtered list, `r=1000&d=hauptuntersuchung` (live, 15 requests) | **14 pages declared and returned, 168 cards, 168 unique slugs, 0 duplicates**; page 15 empty (stop). Matches the 14 pages Nikos saw in the browser. All 168 are in the all-services list and in the sitemap |
| HU list vs badges | The 145 sites not in the HU list: 143 Gutachtenbüro-only and 2 badged Prüfstelle (`kfz-sachverstaendigenbuero-semmelroth`, `-matull-nordgutachter`, both named Sachverständigenbüro). The HU list holds 154 Prüfstelle-only, 13 + 1 both-badge, **0 Gutachtenbüro-only** |

### Scope (decided by Nikos, 2026-10-06)

**A site that offers HU counts, whatever its badge; Gutachtenbüros without HU do not count.** "Offers HU" = in the HU-filtered list, or (for sites the locator does not list) its own detail page names Hauptuntersuchung. Result: **168 + 1 = 169**. The one extra: `kfz-pruefstuetzpunkt-bad-salzungen` (sitemap only, page lists "Hauptuntersuchung (HU) nach §29 StVZO"); the other sitemap-only page (`…schmidt-und-partner-schwarzmeier`, Gutachtenbüro) names no HU, so it is out. Meckenheim (PLZ "553340" as published) is in the HU list, so it is in scope with the PLZ flagged. Column `offers_hu` in `fsp_all_*.csv`: `True` / `False` (HU list), `True (detail page)` / `False (detail page)` (the two extra sitemap-only rows).

### Result (all-services list, 2026-10-06)

313 sites: 156 only Prüfstelle, 143 only Gutachtenbüro, 14 both badges (Prüfstelle badge in total 170; the HU-filtered list, 168, is the benchmark-relevant count and is inside 120–170). Parsed into 40 columns; fill rates: street 99 %, PLZ and city 100 %, e-mail 79 %, phone 98 %, website 44 %, booking link 39 %, opening hours 71 % (Gutachtenbüros often have none), photo 54 %. Raw text is kept next to every lossy parse (`opening_hours_raw`, `address_raw`, `phone_raw`, `distance_raw`, `detail_href_raw`, `email_raw_href`, `card_text_raw`).

Odd cases (listed in the run report, nothing excluded): 3 cards named "Prüfstützpunkt …"; 4 cards badged Prüfstelle whose name says Sachverständigenbüro or Schaden-/Prüfzentrum; 2 cards with PLZ and city but no street (Schmidt & Partner); 1 card with a 6-digit PLZ as published by FSP (Meckenheim, "553340"); 3 cards without phone and e-mail; slug typos (`prufstelle-berlin-…`, `puerfstelle-gross-…`), so a slug prefix is not a type signal.

### Known coverage gaps

- Prüfstützpunkte: the earlier assumption "not in the locator" holds only partly: 3 "Prüfstützpunkt" entries are in the list, and 1 Kfz-Prüfstützpunkt page exists that is in the sitemap but not in the list.
- 2 sitemap-only pages (above); one of them (Bad Salzungen) offers HU and is in scope although the locator does not list it. A site that is in neither the sitemap nor the list cannot be seen; coverage is therefore **not proven**, only consistent between two sources (every listed site is in the sitemap).
- Not verified: whether the HU filter itself is complete (a site that does HU but is missing from the filtered list while being listed unfiltered): the 2 Prüfstelle-badged sites outside the HU list (Semmelroth, Matull) are named Sachverständigenbüro and were not checked on their detail pages.

### Compliance

- **robots.txt (`User-agent: *`) disallows `/*?d=`, `/*&d=`, `/*?f=`, `/*&f=`, `/*?search=`, `/*&search=`** (and `rCH`, `trk`, `ref`, `cookiehint`, `widget`). `/standorte`, `page`, `lat`, `lng`, `r` and `/sitemap.xml` are not disallowed. **Nikos instructed (2026-10-06) to disregard robots.txt for this** and the HU-filtered list (`d=hauptuntersuchung`) was fetched: 15 requests, same polite pace. It is Nikos's decision, not a management approval recorded here. The code still refuses disallowed URLs unless `--allow-robots-disallowed` is passed, so it stays a deliberate switch per run. Detail pages without a query string are not disallowed.
- Headers: a browser-like `User-Agent`, `Accept`, `Accept-Language: de-DE` and a cookie session were used as specified; no challenge appeared, so no bot protection was bypassed. 2–4 s random pause, one request at a time, at most 3 retries.
- E-mail addresses and phone numbers on the cards, and person-named Gutachtenbüros, are personal data: run folders and exports stay private and git-ignored (like all `output/` and `test_output/`).

### Open issues / next steps

Done: robots.txt check · access test (plain HTTP works) · pagination test · all-services and HU-filtered lists, parser · r=2000 check · sitemap cross-check · 2 detail pages · scope rule · tests · offline rebuild.

1. Hand-check the 5 sites of the report (section 6) and Bad Salzungen on Google Maps.
2. Detail pages of the 169 in-scope sites (coordinates, operator note, services); the markup is only known from 2 pages. Optionally check the 2 Prüfstelle-badged sites outside the HU list (Semmelroth, Matull).
3. Done: production modes writing only the in-scope CSV and run summary, and the **first live `full` run** from the notebook (`output/2026-10-06_1047_full/`, 47 requests, PASS, 169 in scope). It matches the recon exactly: same 169 `source_id`s, 0 changed fields (name, address, PLZ, city, e-mail, phone, hours, booking link, website, badges) between the live fetches of 10:22/09:54 and 10:47. Drop check 0.0 % against the earlier rebuild. A PASS shows consistency and reproducibility, **not completeness** (a site in neither list nor sitemap stays invisible) and not real-world correctness.
4. Validation and a fallback grid (several smaller `r` queries around PLZ points) to test whether `r=1000` hides sites; not built.
5. Convention: the live recons went to `test_output/<run>_recon/` with raw HTML in `raw/` (no `raw.jsonl`), the offline rebuild to `output/<run>_rebuild/`; `0_instructions/sources/fsp.md` was not created (docs only on request). `fsp.run` runs keep the raw pages under `raw/pages/` (gzipped), `raw/sitemap/`, `raw/detail/`; the offline reader accepts both layouts.
6. Overlap with a later TÜV Rheinland adapter: FSP booking links go to `tuv.com`; dedupe in the master step.

---

## TÜV Hessen

**Status:** **completed (2026-10-06).** Adapter `1_scrapers/tuev_hessen/src/` (`fetch.py` Playwright access, `parse.py`, `checks.py`, `simulate.py`, `run.py` with modes `simulated` / `full` / `rebuild`), control notebook `1_scrapers/tuev_hessen/notebook/tuev_hessen_control.ipynb`, tests `1_scrapers/tuev_hessen/tests/test_tuev_hessen.py` (61, offline, all pass). The scraper is kept although 102 of its 113 HU sites are also in the TÜV SÜD finder (decision 2026-10-06: it adds 11 sites TÜV SÜD does not list); duplicates are removed in the aggregator, not here (project-wide item "Cross-source overlap"). Live run `output/2026-10-06_1059_full/` **PASS** (32 s: 3 page loads, 4 searches); `output/2026-10-06_1102_rebuild/` is the same raw data parsed with the final rules (wider HU-wording pattern, `betreiber_text` / `websites` columns, shared-booking-sign check) and is the folder to use. No detail-page crawl. Run it: `.venv\Scripts\python.exe -m tuev_hessen.run full --confirm` (simulated: `... run simulated`, offline rebuild: `... run rebuild --source <run folder>`).

### How the locator works

- **Locator page:** `https://www.tuev-hessen.de/38/alle-standorte-von-tuev-hessen-seinen-partnern-und-den-tuev-sued-auto-partnern/` (TYPO3). The form `form.location-search-form` has a type list (options: "Alle Standorte", "Kfz Prüfstelle", "MPU Begutachtung"), an address field and the button "Standorte finden" with a counter.
- **Data endpoint:** `POST https://www.tuev-hessen.de/?eID=location_search`, `multipart/form-data`, 669 bytes (682 with the filter). Answer: Elasticsearch JSON `{took, timed_out, _shards, hits: {total: {value, relation}, max_score, hits: [...]}}`, index `german_website_locations`, ~660 KB for all sites, **all results in one answer, no paging**.
- **Captured payload** (identical for both datasets except the last field):

| field | "Alle Standorte" | "Kfz Prüfstelle" |
|---|---|---|
| `language` | `0` | `0` |
| `fixedTags[]` (hidden inputs, sent every time) | `Kfz Prüfstelle`, `Managementsysteme`, `MPU Begutachtung` | same |
| `address` | empty | empty |
| `tags[]` | empty | `Kfz Prüfstelle` |

  **There is no size, limit or paging field.** "Alle Standorte" therefore means "tagged with one of the three fixed tags", not every record of the index. The page script does `fetch('/?eID=location_search', {method: 'POST', body: FormData(form)})` and writes `hits.hits.length` into the button counter, so the counter is not an independent count.
- **Record** (`hits.hits[]`): hit level `_index`, `_type`, `_id`, `_score`, `sort` (`[city, score factor]`), `_ignored` (16 hits: `infotext_contents.bodytext.keyword`, an indexing note); `_source`: `uid` (stable ID), `pid`, `title`, `street`, `zip`, `city`, `latitude`, `longitude` (strings), `coordinates {lat, lon}`, `phone` (118 of 131), `tags[]` (objects with `uid`, `pid`, `title`), `infotext` (services text with entities; 129), `infotext_contents` (notices; 24), `is_service_center` (`"1"` on the 113 HU sites, **absent on the 18 others**), `searchable_score_factor`, `searchable_meta {preview, mapPreview, renderedLink, linkTitle}`. The preview HTML holds fax (73), mobile (17), opening hours (129), the HU booking link with `locationSign` (91), a contact-form link (18, MPU sites) and the Google route link. No e-mail address is in the data.

### Access method

Playwright, Chromium 153, **headless**. **Cloudflare did not block anything**: the page and all 4 searches answered HTTP 200; the page sets only `cf_clearance` (passive JavaScript check, no interactive challenge). The headed retry is built (`fallback_headed`) but was not needed. The cookie banner must be declined first ("Nur erforderliche Cookies erlauben", `a.cc-deny`): its overlay blocks the search button until then. Each dataset is run through the UI (type chosen, button clicked, real request, answer and counter recorded), then the same fields are replayed from inside the page (`fetch` + FormData); the replay answer is the data and **was identical to the UI answer in both datasets**. No stealth plugin, no faked fingerprint, no challenge solver. Playwright is used on Nikos's instruction (2026-10-06); the TÜV SÜD decision against automation tools was made for tuvsud.com and was not asked again for this site.

### Verified counts (2026-10-06)

| | Alle Standorte | Kfz Prüfstelle (HU) |
|---|---|---|
| `hits.total` | 131 (`eq`) | 113 (`eq`) |
| `len(hits.hits)` | 131 | 113 |
| UI counter on the button | 131 | 113 |

Completeness checks all hold (`relation eq`, total = hits = counter, not timed out, 2 of 2 shards, no duplicate `uid` / `_id`, HU list ⊂ all list, the 113 records are identical in both lists, tag "Kfz Prüfstelle" ⇔ in the HU list, replay = UI). **HU sites: 113.** Non-HU: 18 (16 "MPU Begutachtung", 2 "Managementsysteme"; none has HU wording or an HU booking link).

| `site_type` (by title) | operator | all | HU |
|---|---|---|---|
| `own_station` ("TÜV Hessen Service-Center …") | TÜV Hessen | 52 | 52 |
| `partner` ("TÜV Hessen Partner …") | partner | 44 | 44 |
| `tuev_sued_auto_partner` ("TÜV SÜD Auto Partner GmbH …") | TÜV SÜD Auto Partner | 16 | 16 |
| `unclassified` | — | 19 | 1 |

`listed_by` = "TÜV Hessen" on every row. Unclassified: the 18 non-HU titles ("TÜV Hessen - MPU <Ort> - Begutachtungsstelle für Fahreignung", "TÜV Hessen Darmstadt", "TÜV Hessen - Hamburg") and uid 409 **"TÜV Hessen LKW Service-Center Friedberg Süd"** (HU-tagged, LKW in the title; evidently an own station but the rule is literal, so it is listed for a decision). Tags in the data: Kfz Prüfstelle 113, Auto-Partner 44, TÜH Service-Center 31, TÜV SÜD Auto Partner 16, MPU Begutachtung 16, ÜO Preisliste 11, TP Preisliste 9, HU Aktion After Work Plakette 7, Managementsysteme 2, MPU Begutachtung Eisenbahn 2. Outputs: `tuev_hessen_hu_<date>.csv` (113) and `tuev_hessen_all_<date>.csv` (131), `;`-separated, UTF-8 with BOM, plus `raw.jsonl`, `query_log.csv`, `run_summary.json/.md` (the full report). Both CSVs were requested explicitly for this run (the default for other ÜOs is the in-scope CSV only).

### Footprint

By PLZ (GeoNames state), the 113 HU sites: **Hessen 104, Rheinland-Pfalz 6** (Mainz area, Bodenheim, Nieder-Olm, Worms), **Nordrhein-Westfalen 3** (Siegen ×2, Burbach); bounding box lat 49.55–51.65, lng 7.98–10.03. The non-HU MPU sites are further out (by title: Bielefeld, Hamm, Minden, Osnabrück, Hannover, Hameln, Hildesheim, Koblenz; Managementsysteme in Hamburg).

### Data quality issues found

- **Coordinate copy-paste:** uid 433 (Bad Camberg, 65520) has the coordinates of uid 428 (Oberursel, 61440); 433 lies 23.6 km from its own PLZ centroid (all other sites: median 1.2 km, 95th percentile 3.3 km). Not corrected, flagged `coords_shared_with_other_address` and `coords_far_from_plz`.
- Coordinates shared by uids 58 and 122 (Darmstadt, MPU and Managementsysteme, same building written "Robert-Bosch-Str. 16A" / "Robert-Bosch-Straße 16") and an address shared by uids 56 (MPU Wiesbaden) and 84 (own station): both look legitimate (same building), flagged only.
- uids 71 (Biedenkopf-Eckelshausen) and 463 (Worms) carry the same booking `locationSign` 712 (another copy-paste candidate; 90 distinct signs among the 91 HU booking links).
- 21 own stations lack the tag "TÜH Service-Center" (`tag_title_mismatch`; reported, not fixed). 13 HU sites have no phone (uids 52, 360, 370, 391, 412, 414, 423, 425, 427, 440, 441, 451, 463). uid 288 (Rüsselsheim partner) is HU-tagged but its record has no HU wording (the only such case; the other candidate, uid 356, says "Haupt- und Abgasuntersuchen nach §29 StVZO").
- No duplicate uids, no coordinates outside the Germany box, all PLZ 5 digits and known to GeoNames, `coordinates` equals `latitude`/`longitude` everywhere, no unknown fields. 25 rows carry `needs_review` in the final rebuild: the 18 non-HU rows (unclassified title) and 7 HU rows (uids 71, 84, 288, 409, 428, 433, 463).

### Detail pages (2 opened: Kassel own station, Butzbach partner; not crawled)

Both HTTP 200 and no block. They repeat the list data (address, opening hours, services text, phone numbers, booking link) and add only: images (3–4), a Google rating line ("x,x / 5 Durchschnittliche Bewertung bei Google"), a "Preise & Gebühren" teaser, and on the partner page "Betreiber: …" with the partner's website (also in the list's `infotext`). No JSON-LD, no e-mail address. Only two pages looked at.

### Known gaps

- HU stations inside workshops (Prüfstützpunkte) are probably not listed (not verified; they cannot be seen through a locator).
- "Alle Standorte" is limited to the three fixed tags: a site without any of them would be invisible. Not tested whether the index holds such sites.
- Completeness is proven for what the locator offers (`total` = hits = counter), not against an independent source (no published count found yet).

### Fragile points

Cloudflare could add an interactive challenge (headless would stop; the headed retry would run once, then the run fails); the run depends on the form markup (`form.location-search-form`, `select[name='tags[]']`, `.location-search-results-count`, `a.cc-deny`); the three `fixedTags[]` define "all" and a change there is caught by the request-field check but would change the counts; classification is by title prefix only (a reworded title becomes `unclassified`).

### Compliance

No key. **`robots.txt` was not checked in this run.** Operator and partner names, phone numbers and the `betreiber_text` column can be personal data: run folders and exports stay private and git-ignored.

### Open issues / next steps

Done: recon of the page and the request · adapter · simulated run · tests · live run (PASS) · offline rebuild.

1. Decide uid 409 (LKW Service-Center Friedberg Süd): own station? LKW-only sites in or out of the HU file (now in, flagged `is_lkw_site`).
2. Hand-check on Google Maps: the 5 sites in the chat summary, uid 433 against 428, uid 288, uids 71 / 463.
3. Check `robots.txt` of tuev-hessen.de.
4. Optional probe (1 request): the search without `fixedTags[]` or with other tag values, to see whether the index holds sites outside the three tags.
5. Dedupe with the TÜV SÜD finder: **done for the aggregator to apply**, findings in the project-wide item "Cross-source overlap" (102 of 113 in both lists; `location_sign` matches 90 of 91; 11 only here).
6. Move the coordinate / PLZ checks to `2_core` (project-wide item).
7. `0_instructions/sources/tuev_hessen.md` not created (docs only on request).

---

## TÜV NORD

**Status:** **completed (2026-10-06, marked done by Nikos); first live full run PASS: 497 HU sites in 612 records.** The open issues at the end of this section are follow-up decisions, not build work. Adapter `1_scrapers/tuev_nord/src/` (`fetch.py`, `parse.py`, `checks.py`, `simulate.py`, `run.py` with modes `simulated` / `full` / `rebuild`), tests `1_scrapers/tuev_nord/tests/test_tuev_nord.py` (64, offline; the whole suite passed with 588 before the last test was added; the simulated run must PASS with known truth, negative tests: counter mismatch, filtered page, unknown serviceType, missing field, A-Z list missing half, changed or blocked page, HU drop against the last full run, previous-run structure change). Runs in `1_scrapers/tuev_nord/output/`: `2026-10-06_1113_full` (live, 12 requests, **holds the raw HTML** in `raw/`) and `2026-10-06_1116_rebuild` (the same raw pages parsed with the final code, no requests): both PASS, and **the rebuild folder is the one to use** (its report knows about pages that do not exist). Files per run folder: `tuev_nord_all_<date>.csv` (612), `tuev_nord_hu_<date>.csv` (497), `tuev_nord_run_report_<date>.md` (the full report), `run_summary.json`, `query_log.csv`; CSVs `;`-separated, UTF-8 with BOM. Folder layout follows the storage convention (run folder), not a flat `data/` folder. Control notebook `1_scrapers/tuev_nord/notebook/tuev_nord_control.ipynb` (inspect a run from disk, offline rebuild, simulated run, full run behind `confirm_full_run`, run comparison; the offline cells were executed, the live cell was not). No detail-page crawl, no `0_instructions/sources/tuev_nord.md` (docs only on request). Run it: `.venv\Scripts\python.exe -m tuev_nord.run full --confirm` (simulated: `... run simulated`; offline rebuild: `... run rebuild --source <run folder>`).

### How the locator works

- **Page:** `GET https://www.tuev-nord.de/de/stationen/` (TYPO3, 4.8 MB). **Data location:** the attribute `data-locations` of the map div `[data-station-map="station-results"]` holds all locations as HTML-escaped JSON (a list of 612 objects, 183 KB). Fields on every record: `id`, `name`, `street`, `plz`, `city`, `latitude`, `longitude`, `serviceType`, `link`, `isOffice`; no other field exists. The page prints "612 Stationen gefunden" once (the counter text sits in a div that is hidden on very wide screens, but it is in the HTML).
- **`serviceType`:** `HU` 216, `HU-UNG` 281, `UNG` 115. `isOffice = true` on 112 records, **all `UNG`**: appraisal offices without a station page; their `link` is `/de/stationen/kontakt-aufnehmen/?token=<JWT + cHash>` and changes on every load, so it is never used as an id (the CSV keeps only the path). Station links are `/de/stationen/<slug>/` (495) or `/de/stationen/stationsseiten/<slug>/` (5).
- **`id`** equals the `stationId` in the online-booking link of the station page (checked on 3 pages), so it is also the booking system's id.
- **Also exists:** `/de/stationen/liste-aller-stationen/` (A-Z list, name and link only, see the cross-check).

### Access method

Plain HTTP with `requests`: a **fresh session that keeps no cookies** (the NetScaler sets an `NSC_…` cookie on the first answer; it is never stored or sent back), browser-like headers, one request at a time, 2–4 s apart. All 12 requests of the full run answered HTTP 200; no challenge, no Cloudflare; **Playwright was not needed and is not built**. The cookie consent manager on the page is irrelevant (the data is in the HTML). `robots.txt` checked once by hand on 2026-10-06 (not part of the adapter): `User-agent: *` `Allow: /`; it disallows forms, login, search, certificate files, and `/de/stationen/kontakt-aufnehmen/?token=` (the office contact link, which the adapter never requests). `/de/stationen/`, the list, station pages and `/stationsseiten/` are allowed. Live requests of the build session: 18 (recon 5, full run 12, `robots.txt` 1).

### Verified counts (2026-10-06)

| | all | HU (in scope) |
|---|---|---|
| records on the page | **612** (counter 612) | **497** |
| `serviceType` `HU` / `HU-UNG` / `UNG` | 216 / 281 / 115 | 216 / 281 / – |
| `isOffice` true / false | 112 / 500 | 0 / 497 |
| `site_type` `partner_station` | 297 | 296 |
| `site_type` `own_station_probable` | 196 | 195 |
| `site_type` `office` | 112 | 0 |
| `site_type` `unclassified` | 7 | 6 |

`is_hu_site` = `serviceType` in (`HU`, `HU-UNG`); `tuev_nord_hu_<date>.csv` = those 497. The 115 `UNG` records are 112 offices and 3 stations (1 own probable, 1 partner, 1 unclassified). By `serviceType` within `site_type`: own probable 116 `HU-UNG` / 79 `HU` / 1 `UNG`; partner 163 / 133 / 1; unclassified 2 / 4 / 1. All 497 HU records carry a station link; two of those links (below) lead to no page. `site_type` rules: `office` = `isOffice`; `partner_station` = "City (Operator)" or "City, Operator" when the operator part has a company or office word; `own_station_probable` = plain name (or a bracket that repeats the record's own city field, e.g. "Halle (Saale)"); `unclassified` = everything else (listed in the report, never guessed). `operator_name` is the bracket or comma part (302 rows, of which 22 distinct names occur on more than one HU site; not merged). `listed_by` = "TÜV NORD" on every row.

### Footprint

Nationwide. HU sites by first PLZ digit: 0: 6 · 1: 28 · 2: 89 · 3: 83 · 4: 108 · 5: 78 · 6: 35 · 7: 33 · 8: 19 · 9: 18, so **all ten PLZ regions have HU sites**, but 358 of 497 (72 %) are in regions 2–5 (north and west). **Partner stations (296) are in regions 1–9, none in region 0; own stations probable (195) are in regions 0–5 plus one in region 8, none in 6, 7 or 9** (corrects the assumption "partners in all PLZ regions"). HU bounding box lat 47.75–54.91, lng 6.11–14.04.

### Completeness check used

1. **Counter:** "612 Stationen gefunden" = 612 parsed records (hard check).
2. **Unfiltered page:** all three `serviceType` values present and no other value (hard).
3. **A-Z list cross-check** by page slug (hard when more than 5 % of the stations are in only one source).
4. **Detail pages (3):** map coordinates, booking `stationId` and HU service heading agree with the list data (review).
5. **HU count** against the last passing full run, alert below −10 % (hard); structure of the raw page against the previous run (record count, fields, `serviceType` values, keys; none exists yet).

Limits: the counter is the page's own number (it proves our parse equals what the page says, not that the page lists every station), and a station that is in neither the map data nor the A-Z list is invisible. Coverage is therefore **consistent between two sources, not proven**.

### A-Z list cross-check (2026-10-06)

The list has 503 station links (503 unique slugs, letters A–Z without Q, X, Y plus Ö; 5 `/stationsseiten/` links; no serviceType, no address). The map data has 500 stations with a page (the 112 offices have none). **In both: 498** (280 `HU-UNG`, 215 `HU`, 3 `UNG`), names identical for all 498, path variants identical. **7 differ:**

- **Only in the map data (2):** Marl (`marl`, `HU-UNG`, own probable) and Saarlouis (`saarlouis-kfz-sachverstaendigenbuero-kobsev`, `HU`, partner). Their links answer HTTP 200 with the **finder page itself** (canonical `/de/stationen/`, byte-identical to the stations page): the station page does not exist. They stay in the CSVs (`in_list_page` = False, `needs_review`).
- **Only in the list (5):** `pruefstuetzpunkt-bilster-berg` (Bad Driburg), `tnsg-sfh` (Schmelz), `ulm-ingenieurbuero-daeubler` (Ulm, partner note on the page): real pages with address, coordinates and "Hauptuntersuchung (HU)" among the service headings; and `tuev-nord-autokaufcheck-online` / `tuev-nord-autoankaufcheck-online`: online product pages with the same address and coordinates as `tnsg-sfh` (booking `stationId` 432 and 436, not in the map data). **None of the five is in the CSVs or the HU count** (no `serviceType`, no id in the map data). Whether the three real ones do HU on site is not verified.

### Data quality issues found

- **Id reused:** 55292878 is used by two offices at different addresses (Bad Kissingen, Bad Neustadt an der Saale; both `UNG`). Kept; `location_id` `TUEV_NORD-55292878-1` and `-2`; the unique key is `source_key` = id + normalised address. **0 duplicate keys.**
- **4 addresses (8 records) are used by an HU partner station and an office** under different ids, among them the known pair id 55636771 (office, `UNG`) and id 630 (station, `HU-UNG`) at Dieselstr. 3, 90592. Kept, flagged `address_shared_with_other_id`; the address normalisation matters here (3 of the 4 pairs spell the street differently).
- **7 unclassified names** (6 of them HU): 5 with brackets whose text has no company word (a district, a place or a surname) and 2 plain names with company words (one `UNG` GmbH, one "Kfz-…" site); listed in the report. Two bracket names repeat the city field and are treated as place names (Halle (Saale), Kreuztal (Hilchenbach-Dahlbruch)).
- **12 offices have no street** (none with HU). 7 names carry stray or double spaces (cleaned in `name_clean`, raw kept in `name`).
- **26 rows `psp_suspect`** (25 HU): the operator text mentions Autohaus / Werkstatt / Stützpunkt / Gelände / bei / Fa. / ÜO (informational, not a scope decision).
- Clean: no duplicate slugs, no coordinates outside the Germany box, all PLZ 5 digits and in GeoNames, largest distance to the PLZ centroid 10.0 km (threshold 15), no coordinates shared by different PLZ (one pair shares coordinates with another address inside the same PLZ, info).
- **Dated notes on the pages** (closures, holiday hours) are in the opening-hours text, so that text goes stale.

### Detail pages (3 opened by the run, 7 more to explain the differences; not crawled)

Own (Flensburg), partner (Versmold) and HU-only (Schleswig): all HTTP 200, ~400 KB each (the site navigation is part of the page). Content: address, opening hours per weekday with dated notes ("Hinweise"), service headings (HU/AU, Umweltplakette, Fahrerlaubnisprüfungen, Schadengutachten, AutoKaufCheck, BatterieCheck …), a "TÜV NORD-Partner" block naming the operator on the partner page, an online-booking link with `stationId`, map coordinates (identical to the list data on all 3), breadcrumbs, JSON-LD `WebPage` only. **No station-specific phone or e-mail**: the pages show the central service number (0800 80 70 600) and one central address. A crawl of the ~497 pages would be ~200 MB (not done).

### Known gaps

- HU at workshop Prüfstützpunkte is probably not listed (not verified); one "Prüfstützpunkt" page (Bilster Berg) exists only in the A-Z list.
- Offices are appraisal only: verified, all 112 `UNG`.
- `site_type` is a name heuristic; "own" is only probable. Nothing in the data says who operates a station except the name.
- Completeness is consistent between two sources, not proven (see above).

### Fragile points

The result depends on one HTML attribute (`[data-station-map="station-results"][data-locations]`) and on the counter text; a redesign fails the run with a clear message (never "0 stations"). The A-Z list markup (`a[data-list-teaser]`) is the second dependency. A missing station page is answered with HTTP 200 and the finder page, so a status code alone proves nothing; the run checks the canonical URL. Office links carry a time-stamped token. Classification by name.

### Compliance

No key. `robots.txt` checked (see Access method). Operator and partner names (some are person names) and office names are personal data: run folders and exports stay private and git-ignored. Office contact links (tokenised, disallowed in `robots.txt`) are not requested.

### Open issues / next steps

Done: recon (page, A-Z list, 10 detail pages) · `robots.txt` · adapter · simulated, live and rebuild runs · tests.

1. Decide the 5 list-only pages (3 real locations: add as extra rows from their detail pages, or leave out?) and the 2 map stations without a page (Marl, Saarlouis).
2. Hand-check the 5 sites of the report (section 9) and the 7 unclassified names; decide whether the 6 unclassified HU sites are own or partner.
3. Scope: partner stations are in the HU file (as for TÜV SÜD); 25 HU sites are `psp_suspect`. Confirm or exclude.
4. Optional: detail-page crawl of the 497 HU pages (opening hours, partner note); look for a sitemap as a third source; `0_instructions/sources/tuev_nord.md`.
5. Overlap with other ÜOs (partner operators may be listed elsewhere): dedupe in the master step by address and coordinates; keep `listed_by` and `operator_name` apart.
6. Monthly run: `full --confirm`; the structure check compares with the previous run's raw page automatically.

---

## VÜK

**Status:** **completed (2026-10-06)** (build finished, live full run PASS; the open items at the end are follow-ups, not build work). VÜK GmbH (Verkehrssicherheit, Ostwestfalen-Lippe) is a recognized ÜO that was **not in the brief's list** (see "Project-wide open items"). Adapter `1_scrapers/vuek/src/` (`fetch.py` polite HTTP client + Nominatim client, `parse.py`, `checks.py`, `simulate.py` = fake site, `run.py` = modes `simulated` / `sample` / `full` / `rebuild`), control notebook `1_scrapers/vuek/notebook/vuek_control.ipynb` (offline cells executed; the live cells were not run from the notebook), tests `1_scrapers/vuek/tests/test_vuek_parse.py` / `test_vuek_fetch.py` / `test_vuek_run.py` (105, offline; the whole suite passed, 696, in the same session). **Live `full` run `output/2026-10-06_1146_full/` PASS** (11 requests to vuek-gmbh.de + 5 to Nominatim; 16 of 16 hard checks ok, 0 warnings, no review finding failed). Offline rebuild `output/2026-10-06_1148_rebuild/` (0 requests): PASS, CSV identical to the live run. `sample` was tested offline only (not run live). Commands: `.venv\Scripts\python.exe -m vuek.run simulated | sample | full --confirm | rebuild --source <run folder>` (`--accept-station-change` after a real change of the station set). pyproject mapping `vuek = "1_scrapers/vuek/src"` and a README section / OSM attribution added; `0_instructions/sources/vuek.md` not created (docs only on request).

### Site structure (verified 2026-10-06)

- Jimdo CMS, static HTML, no locator, no JSON, no key, no cookies needed. Plain HTTP works; no Cloudflare block (HTTP 200 on all 11 pages; Cloudflare only obfuscates the e-mail addresses, decoded with the FSP decoder; the obfuscation key changes on every request).
- **Five station pages**, one contact page each, all with the same template: `/kontakt/pruefstelle-lage/`, `-bielefeld/`, `-bad-oeynhausen/`, `-minden/`, `-kirchlengern/`. Page: h1 "VÜK-Prüfstelle <Ort>", one sentence (HU, Änderungsabnahme, weitere Fahrzeugprüfungen), street, "PLZ City" (the city carries the district: "Bielefeld-Brackwede", "Bad Oeynhausen-Rehme"), phone, e-mail, heading "Öffnungszeiten (ohne Termin)" with free-text hours, and a Google Maps embed whose only content is an address query: **no coordinates on any page**.
- The five are linked from **five places**: the Kontakt menu (desktop and mobile navigation), the footer list "VÜK Standorte", the body text of `/ueber-uns/`, `sitemap.xml` (22 URLs) and the HTML sitemap `/sitemap/`; the hub `/kontakt/` links them too (checked as review only). The meta description and the body text both say "fünf eigenen Standorten". An XML sitemap exists (listed in robots.txt); it holds `/about/`, which the HTML sitemap does not (info only, no station).
- `robots.txt`: `Crawl-Delay: 5`, `Disallow: /app/` and `/j/`. No URL of ours is disallowed. Management decided that robots.txt need not be obeyed (project plan §7); the crawl delay is honoured anyway (5–7 s between requests instead of the 2–4 s of the brief), sequential, 3 retries with backoff, any 403/503/Cloudflare page stops the run.

### Approach

1. `/ueber-uns/`: station links from the menu (each navigation container separately), the footer block, the body text; the stated number from the meta description and the body text (number word or digit). Then robots.txt, HTML sitemap, `sitemap.xml`, `/kontakt/`, `/hauptuntersuchung/`, then the station pages (menu order, sorted), raw HTML of every page kept in `raw/`.
2. Station page → name, street, PLZ, city, phone, fax, mobile, e-mail, opening hours (raw text kept; also normalized, e.g. `Mo-Fr 09:00-12:00, 12:30-17:00; Sa 08:00-12:00`), services (from the intro sentence), temporary notices (red text before the hours), red text inside the hours, map address, content hash. Anything the parser does not understand stays in `unparsed_lines` and is flagged.
3. Classification: all five own stations → `site_type = pruefstelle`, `in_scope = true`, `source_org = VÜK`, `location_id = VUEK-<slug>` (e.g. `VUEK-lage`). HU is checked on every page (the page text names Hauptuntersuchung) and on the HU page; a page without it would be flagged `hu_not_on_page` and stay in scope (all five name it).
4. Export: only `vuek_pruefstellen_<date>.csv` (the in-scope rows, candidate schema of the project plan §3 plus `coord_source`, `fax`, `notice`, `hu_evidence`, `content_hash`) with `run_summary.json`/`.md`, `query_log.csv` and `raw/`; no `all` file, no Excel. A run that does not PASS writes `vuek_pruefstellen_unverified_<date>.csv`.

### Completeness check (fails loudly, 16 hard checks)

The run FAILS, with a message that lists the differences, if any of these does not hold: `/ueber-uns/` has its content area · the Kontakt menu lists station links and the desktop and mobile navigation list the same ones · exactly one footer block "VÜK Standorte" · **menu = footer = body text = `sitemap.xml` = HTML sitemap** (as station URL sets: any link under `/kontakt/` except the hub, or with `pruefstelle` in the path, so a station with another URL pattern is not missed) · the HTML sitemap page has content · the stated number is readable (meta description) and equals the number of stations · the numbers in the body text are readable and equal it · at least one station · **the station set equals the last passing full run** (the first run: the five stations of the brief; a real change fails until `accept_station_change=True`, and the accepted run becomes the baseline) · every station page could be read · every page yields street + 5-digit PLZ + city. A Cloudflare/403 page or an unreadable `/ueber-uns/` stops the run with a FAIL summary and no export. Review findings (listed, never failing): the `/kontakt/` hub agrees, the body text states the number, the HU page mentions HU, every page names HU, every page has an h1, coordinates exist / lie in Germany / within 25 km of the PLZ centroid / the geocoder answer names the PLZ.

Tested offline with the simulated site (negative tests, each FAILS with the named check): menu shows 6 stations · footer lacks one · body text lacks one · the site states "sechs" · the stated number is unreadable · body text says "sechs" · footer block gone · XML sitemap with an extra / a missing station · HTML sitemap with an extra station · broken XML sitemap · a station page without address, without street, answering 404 · a Cloudflare block · a consistent sixth station (fails against the baseline until accepted).

### Verified count (2026-10-06, run `2026-10-06_1146_full`)

**5 own stations, all in scope; the site states five; all five station sets are identical.** Addresses as published:

| Station | Street | PLZ, city | Coordinates (Nominatim) | PLZ distance |
|---|---|---|---|---|
| Lage | Sylbacher Straße 228 | 32791 Lage | 52.02593, 8.76539 | 4.2 km |
| Bielefeld | Driburger Straße 1 | 33647 Bielefeld-Brackwede | 51.98079, 8.51929 | 1.16 km |
| Bad Oeynhausen | Adam-Opel-Straße 8 | 32547 Bad Oeynhausen-Rehme | 52.21698, 8.83081 | 1.84 km |
| Minden | Ringstraße 11 | 32427 Minden | 52.28221, 8.90146 | 1.4 km |
| Kirchlengern | Alfred-Krupp-Str. 2a | 32278 Kirchlengern | 52.18202, 8.65739 | 2.59 km |

State by PLZ (GeoNames): Nordrhein-Westfalen for all five.

### Coordinates

None of the pages carries coordinates (the parser also looks for `@lat,lng`, `ll=` / `center=` / `q=lat,lng`, `data-lat` and JSON `latitude`/`longitude` and would use them as `coord_source = page`; none found). All five addresses were geocoded with **OpenStreetMap Nominatim** (structured query street + PLZ + city; fallbacks: city without district suffix, then without city; **all five found on the first attempt**): `coord_source = nominatim`, the answer names the station's PLZ in all five, all inside Germany, `plz_distance_km` 1.16–4.2 (limit 25). Pace: 1.1 s between requests, User-Agent with application name and contact. The answers are stored in `raw/nominatim/<slug>.json`; a rebuild replays them. Attribution (© OpenStreetMap contributors, ODbL) is in the README and in `run_summary.md`. Not verified: the building-level accuracy of the coordinates (only the PLZ distance and the PLZ in the answer were checked).

### Change detection

Every page gets a content hash (sha256 of the normalized page text; e-mail addresses decoded first because the obfuscation key changes per request; scripts, cookie and map-placeholder texts removed). The summary lists per station whether it is new, removed, changed (address / phone / fax / e-mail / hours / services / notice / name, or "other page content") or unchanged against the last passing full run. Verified offline (simulated site: a phone change and an hours change are found, the e-mail re-keying is not a change). **Not yet exercised live: the first full run has no baseline** ("no earlier passing full run"); the offline rebuild against it reports 0 changes.

### Known gaps

- **Partner workshops (PSPs) are not listed** anywhere on the site and are out of scope; they cannot be found with this adapter. The site's own sentence, recorded in every run summary: "Zudem sind wir in zahlreichen Partnerwerkstätten in OWL vertreten."
- Completeness is proven against what the site says about itself (five sources and the stated number agree). A station that the site omits everywhere and does not count would be invisible.
- HU at a station rests on the page text (the sentence "führen wir Hauptuntersuchungen (HU) … durch" on all five pages and the HU page), not on a flag.

### Odd findings (2026-10-06)

- The Lage page carries a red temporary notice about "Donnerstag 24.09. sowie Freitag 25.09. NUR VON 10-16.00 Uhr" (the Prüfstelle is open only then): dates in September 2026, already past on 2026-10-06, so probably stale. Kept in column `notice`, flag `notice_on_page` (info).
- **One phone number and one e-mail address are shared by all five stations** (a central contact; flag `phone_shared_with_other_stations`, info). The e-mail address is the same after decoding; only the Cloudflare hex differs.
- Kirchlengern: the Thursday morning hours are partly in red ("09.00 bis 12.00 Uhr" instead of "12.30 Uhr" on the other weekdays; flag `hours_highlighted`, info; the hours are normalized as `Mo-Fr 09:00-12:30 (außer Do), 13:00-17:00; Do 09:00-12:00, 13:00-17:00; Sa 10:00-12:00`).
- The map query on the Bielefeld and Bad Oeynhausen pages omits the district ("Bielefeld", "Bad Oeynhausen") that the address line carries; no mismatch flag (the street matches).
- The hours heading says "(ohne Termin)": the hours are for walk-ins. The heading is **not exported** (only in the raw HTML); the CSV column `opening_hours_raw` holds the day and time lines.

### Compliance

No key or login. robots.txt handled as above (not disallowed, crawl delay honoured). Phone numbers and the e-mail address are business contact data but still contact data: run folders and exports stay private and git-ignored. The saved raw HTML contains the website's own embedded third-party map keys (not ours), in git-ignored run folders only.

### Open issues / next steps

Done: recon of 5 pages and the 5 station pages · adapter, simulated run and negative tests, 105 tests · notebook (offline cells executed) · live `full` run (PASS) · offline rebuild · README / pyproject.

1. Hand-check the five addresses and coordinates on Google Maps (or on site).
2. Second full run to exercise change detection live (the first has no baseline); decide the monthly rhythm.
3. Decide what to do with the stale notice text on the Lage page (nothing is dropped; `notice` is in the CSV).
4. Dedupe at the master step by address and coordinates (not checked whether VÜK sites appear in another ÜO's finder).
5. `0_instructions/sources/vuek.md` (recon notes, step 3 of "How to add a new ÜO") not created (docs only on request).
6. The ÜO list of the brief is incomplete (project-wide item): look for further ÜOs missing from it.

---

## TÜV Thüringen

**Status:** **completed (2026-10-06)**. Adapter `1_scrapers/tuev_thueringen/src/adapter.py` (fetch, parse, classify, check, export in one module), control notebook `notebook/tuev_thueringen_control.ipynb` (`CONFIRM_LIVE`); no pytest suite, no run_summary. Live full run `output/2026-10-06_1509_full/` **PASS**: one CSV `tuev_thueringen_pruefstellen_2026-10-06.csv` (12 rows). Offline dev copy of the page: `test_output/2026-10-06_1446_sample/page.html`.
- **Locator:** Shopware + Acris Store Locator, group "Prüfanlagen" (`/store-locator/08a232c58b2f4816939b7d6327804b88/store-group`, same group at `/store-locator/Pruefanlagen`). Server-rendered; each store 3 times (map overlay with lat/lng, desktop table, mobile table). Plain HTTP, browser User-Agent, 2–4 s pause, max 5 requests per session. 403, a challenge page or a page without the store table stops the run.
- **Approach:** BeautifulSoup, the three views merged per `data-store-id`; name from the table (overlay name kept as `name_overlay`), coordinates from the overlay, e-mail "(at)" → "@", opening hours as text plus raw HTML. Every store of the group = `own_station`, `in_scope` true.
- **Hard checks:** ids identical in the three views · cross-check URL gives the same ids · ids unique · street, 5-digit PLZ, city, coordinates in the Germany box · count not more than 10 % below the last verified CSV (first run: 12 expected). Any failure → file named `..._unverified`.
- **Flags** (`needs_review`, never exclusion): `psp_suspect` (Autohaus/Werkstatt in name or street), `possible_partner` (external website or no tuev-thueringen.de e-mail), `missing_email`, `name_mismatch` (overlay vs table), `plz_far` (> 15 km from the PLZ centroid; none).
- **Result (2026-10-06):** 12 stations; flagged: Mühlhausen (`possible_partner`, `missing_email`; own site mhl-hu.de), Altenburg (`psp_suspect`, `name_mismatch`; "im Autohaus Hußner"), Schmalkalden (`possible_partner`, `missing_email`; hours in a different format).
- **Service-Center / Kunden-Center:** the notebook's last cell scans both groups for HU / Prüfanlage / Prüfstelle (report only, nothing written to the CSV); its result is not recorded in this file.
- **Known gaps:** HU at workshop PSPs and outside Thüringen not listed. Leinefelde and Immelborn (Bad Salzungen) are in Thüringen by PLZ.
- **Open issues:** decide the three flagged stations (partner or own); address split depends on the layout "Street PLZ City Germany"; `opening_hours_raw` carries stray `<br/>`; `robots.txt` not checked (Nikos's decision); hand-check of 5 sites.

## TÜV Rheinland

**Status:** **completed (2026-10-06)**; live full run `output/2026-10-06_1536_full/` **PASS** (an identical earlier run `_1528_full` is kept). Adapter `1_scrapers/tuev_rheinland/src/adapter.py` (fetch, parse, classify, check, export in one module), control notebook `notebook/tuev_rheinland_control.ipynb` (`CONFIRM_LIVE = False` by default); no pytest suite, no run_summary. A live run writes only one CSV: `output/<run>_full/tuev_rheinland_pruefstellen_<date>.csv` (`..._unverified` if a hard check fails). Dev copies (finder JSON + 3 landing pages): `test_output/2026-10-06_1441_sample/` (recon) and `test_output/2026-10-06_1536_sample/` (saved by the live run).
- **Locator:** one GET of `https://www.tuv.com/germany/de/locationfinder/locationfinder-config.json` = the complete worldwide dataset (1,198 records; types VIS, GUT, OFF, DRI, TRC, HEC, LAB, DLO, ZUL, TEC); the finder filters client-side. Browser User-Agent, 60 s timeout, 1 retry only on network errors/5xx; 403, HTML or non-JSON stops the run. `robots.txt` not checked (Nikos's decision).
- **Approach:** type `VIS` only. German = `vic_id` country `de`; a foreign postcode ("L-5326", Luxembourg) or coordinates outside Germany override it (excluded). VIS without `vic_id` but German PLZ/coordinates and `external_db` tuev/fsp are included and flagged (Meisenheim, Mühlheim, Oberursel, Offenburg). `site_type`: own_station = `external_db` "" or "tuev" with pool `_de-DE` (the main pool is "tuev", not the empty value), fsp_partner = "fsp" with `_de-FSP`, else unknown. `operator_group`: tuev_rheinland / fsp / tuev_saarland. Key on `id` only; the vic number exists in both pools.
- **Hard checks:** JSON list · 19 known fields · ids unique · German VIS in both pools · every in-scope row has street and 5-digit PLZ (FSP partners may lack the PLZ) · count not more than 10 % below the last verified CSV · Saarland landing page: 24 rows parsed and the text still says 24. **Flag only:** missing PLZ or coordinates (must all be `needs_review`), `plz_distance_km` > 15, odd `vic_id`.
- **Landing-page cross-check** (Saarland, Berlin, Pfalz; report only, per-row status matched_vis / matched_other_type / not_in_json): normalised street + PLZ, fallback same PLZ + similar street. Saarland 21 of 24 matched by address, Berlin 6 of 6, Pfalz 21 of 21 (all VIS). Decided by Nikos, set in the notebook: Völklingen record 45371 (empty address) filled from the page; Losheim Saarbrücker Str. 221 added from the page (no coordinates, flagged).
- **Result (live run 2026-10-06, same as the offline recon):** 341 in scope: own_station 176, fsp_partner 164, unknown 1 (Wörth); operator_group tuev_saarland 23; 12 need review; all hard checks PASS. FSP cross-check (report only): 143 exact + 8 loose of the 161 fsp_partner rows then in scope, against the 169 of the FSP CSV.
- **Known gaps:** 9 in-scope rows lack PLZ (4 FSP: Neubrandenburg, Hannover, Bremerhaven, Bad Doberan) or coordinates (5: Völklingen, Mühlheim, Oberursel, Offenburg, Losheim); the aggregator geocodes them and fills the PLZ from the FSP list. Mobile PSPs and the other types (GUT, OFF, LAB, …) are not exported.
- **Open issues:** Wadern-Lockweiler (Mittelbachweg 1) vs finder record 45291 (Wadern, Franz-Haas-Straße 1 a) unverified; Wörth (`fsp` + `_de-DE`) unknown; Köln-Wahn `vic_id` "de-fsp"; the Berlin and Pfalz pages state 10 and 22 stations, their tables hold 6 and 21; dedupe against FSP in the aggregator.

## Aggregator (pruefstellen_master)

Status: built 2026-10-07; dev run PASS; official run pending (Nikos). Code: `2_core/master_*.py`, `dedupe.py`, `normalise.py`, `geocode.py`, `diff.py`; notebook `2_core/notebook/master_control.ipynb`; ~250 tests.

Result (dev, 2026-10-07): 4,118 input rows -> 3,850 master rows (102 SÜD/Hessen, 154 FSP/Rheinland, 12 within-source merged away). 8 rows to review. 4 addresses shared across families (flagged, not merged).

Rules: merge families {SÜD, Hessen}, {Rheinland, FSP}, rest alone; order: location_sign (1:1 only) -> street+PLZ -> overrides -> similar street. Coordinates must pass the 15 km PLZ check; Nominatim only for the rest (cap 60, cached).

Open: Bad Salzungen PLZ 36569 unknown; 2 DEKRA/GTÜ same-spot pairs; PS-000866 PLZ choice; TÜV Rheinland and TÜV Thüringen have no run summary.
