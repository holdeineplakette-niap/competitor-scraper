# Scraper Automation Plan

> **Status:** plan approved in principle (Nikos, 2026-10-21); the headless entry point `core.monthly` is built (2026-10-28, §10); Actions workflow, n8n workflows and the HubSpot write are not built; decisions open (§8).
> **Snapshot:** Oct 21, 2026
> **Companion context:** `project_plan.md` (Phase 4 — Automation), `scraper_status.md` (per-source state, master lifecycle, HubSpot import).

---

## 1. Goal

Run the whole pipeline once a month on the 1st: scrapers → aggregator → HubSpot prep → HubSpot import → Vercel refresh → changelog in Slack. Hands-off on the 1st except the TÜV SÜD browser step (~15 min), which moves to the last working day of the previous month.

**Rules carried over:** free (existing tools only), completeness beats elegance, no silent failure, nothing deleted (in Drive or HubSpot), no bypass of access blocks, no personal data in Slack or git.

---

## 2. Architecture

| Layer | Tool | Role |
|---|---|---|
| Code | Private GitHub repo | Code, tests, docs only; no data (storage convention, git rules). |
| Compute | GitHub Actions | Runs the Python pipeline (incl. Playwright for TÜV Hessen). Free tier 2,000 min/month for private repos; one run ≈ 2–3 h, mostly GTÜ's ~900 polite requests. |
| Control plane | n8n (existing) | The only scheduler: starts the Actions job (`repository_dispatch`), receives its result by webhook, waits for the TÜV SÜD file, Slack approval, HubSpot import, Vercel hook, changelog. |
| Data store | Restricted Google Drive folder | Run folders, last month's master, geocode cache, last passing run per source. Synced in at job start and out at job end. |
| Secrets | GitHub Actions secrets + n8n credentials | `GTUE_API_KEY`, HubSpot private-app token, Slack credential, Vercel deploy hook. Never in code, logs or chat. |

Why split: Actions cannot wait cheaply for a person; n8n cannot run our Python/Playwright. Each does what it is good at.

**Fallback compute:** a self-hosted GitHub runner on an office machine, if datacenter IPs get challenged (TÜV Hessen / Cloudflare) or a locator geo-restricts.

---

## 3. Monthly timeline

```
Last working day  n8n → Slack: "TÜV SÜD due" + generated snippet
  (Nikos, ~15 min) Runs snippet in own browser → JSON into Drive inbox
                  → Actions: coverage check → PASS, or next-pass snippet to Slack
1st 02:00   A  Scrapers: 9 ÜO adapters + HDP, one parallel job per ÜO
1st ~05:00  B  Aggregator (official)
            C  HubSpot prep (changes files)
            D  Gate: checks + Slack approval
            E  HubSpot import (API) → poll until done
            F  Vercel deploy hook
            G  Changelog to Slack
```

---

## 4. Steps

| Step | What | Pass rule | On failure |
|---|---|---|---|
| **A. Scrapers** | Existing adapters in `full` mode: GTÜ, KÜS, DEKRA, FSP, TÜV NORD, TÜV Rheinland (+ Saarland), TÜV Hessen, TÜV Thüringen, VÜK, plus HDP (same night, satisfies the 7-day HDP freshness rule). Parallel across ÜOs (one host each, so per-host politeness is unchanged). | Adapter's own completeness check PASS; drop vs. last passing run ≤ 10 %. | Source uses its last passing run; the lifecycle does not retire its sites (no false closures in HubSpot). Slack alert, one retry after 1 h. |
| **A2. TÜV SÜD (manual)** | Stays manual (management decision 2026-10-05: own browser, no automation tools). Pipeline generates the snippet, checks coverage offline, posts the next pass if needed. | Coverage proof PASS. | Not blocking: last month's file is still within the 45-day freshness limit, so nothing is retired; no new TÜV SÜD sites that month, stated in the changelog. |
| **B. Aggregator** | `core.master_run official --confirm`: merge, HDP removal, lifecycle, diff. | Hard checks PASS; mass-retirement guard (> 10 % of active rows) not triggered. | Stop; nothing reaches HubSpot. Guard → approval needed (`--accept-retirements`). |
| **C. HubSpot prep** | `core.hubspot_export`: `hubspot_changes_YYYY-MM.csv` + overview + excluded. Only new / changed / retired / reactivated rows; changed rows carry only changed fields; owner, lifecycle stage and notes are never touched. | Row counts match the diff. | Stop. |
| **D. Gate** | Slack message: counts per category, failed/stale sources, review items, **Approve / Hold**. | Months 1–3: always manual approval. Then auto-approve if all checks are green and changes are small (proposal: < 5 % of rows changed, < 2 % retired). | Hold and wait. |
| **E. HubSpot import** | HubSpot Imports API (same mechanism as the tested native CSV import), "create and update", match key `prufstellenid_datascraper`. Token scoped to companies + imports only. | Created/updated counts equal the overview file. | Fetch the error rows, alert. Re-running is safe (unique match key). |
| **F. Vercel** | After the import is DONE, n8n calls the project's Deploy Hook; the app rebuilds and pulls fresh data. | Deploy succeeds. | Alert; HubSpot data is already correct. |
| **G. Changelog** | One Slack message: status per ÜO, totals, new / retired / reactivated / changed per ÜO and Bundesland, retirements highlighted for M&A (succession signals), links to filtered HubSpot views and the run folder. Optional short Claude summary on top; the deterministic numbers stay the source of truth. | — | — |

**Personal data:** Slack references rows only as `master_id` + ÜO + city (site names often contain owner names). Details stay in HubSpot and Drive.

**Known HubSpot limit (unchanged):** a reactivated site gets `business_status = aktiv`, but its old `compscraper_closedondate` is not cleared by an import (empty cells never overwrite). The changelog lists reactivations; clear by hand, or later by a single API update.

---

## 5. Reliability and security

- **Fail loud:** every stage posts its status. Dead-man check: no changelog by 12:00 on the 1st → alert.
- **Idempotent and resumable:** each stage reads/writes the month's run folder; rerun from the failed stage.
- **Least privilege:** one token per system with minimal scopes; secrets masked in logs; GitHub logs contain counts only; no data uploaded as Actions artifacts.
- **Change control:** code changes via pull request; CI runs the offline tests and `simulated` mode; green CI is the precondition for the monthly run.
- **Access limits:** no bypass of Cloudflare or other blocks. A blocked source stops, is marked stale and alerted (see A).
- **GTÜ key rotation:** a 401 alerts; the key is updated in the secrets only.

---

## 6. Prerequisites before go-live

1. **First full HubSpot import** from the official 2026-10 master (baseline of the November run). Open.
2. **HDP decisions (Nikos, 2026-10-21):** KUES-30052 is removed; Fürth is a confirmed HDP site that KÜS does not list (known exception). Both still have to be implemented in `core/hdp_removal.py`; KUES-58908 is not yet covered by a decision. Otherwise a review item every month.
3. **Git:** baseline commit, `git check-ignore` on data and `.env`, add `*.egg-info/`, anonymise the München fixture.
4. **One headless entry point** `python -m core.monthly`: runs all adapters' `full` mode, the aggregator and the export; writes one status JSON. **Done 2026-10-28** (§10).
5. **Configurable data root** in `core/paths.py` (env var) so the runner works on a synced Drive copy. **Done 2026-10-28:** `PRUEFSTELLEN_DATA_ROOT` (default: the project folder).
6. **Decimal format:** verify that the Imports API accepts the German decimal comma in `geo_lat` / `geo_lng`; otherwise use a dot for API imports.
7. **GDPR documentation** (purpose, retention), more urgent once personal data is processed automatically every month.
8. **Accounts and credentials:** GitHub private repo, HubSpot private app, Slack channel + n8n credential, Vercel deploy hook, Drive folder access for the runner.

---

## 7. Rollout

**Decided (Nikos, 2026-10-21):** everything is built in the week of 2026-10-21, as streamlined as possible; Nikos's setup tasks run in parallel with the build, in any order.

| When | What |
|---|---|
| Week of 2026-10-21 | Build (Claude) and account setup (Nikos) in parallel; end-to-end test in `simulated` mode, then one live test run on explicit instruction. |
| 1 November 2026 | First automated run, approval gate on. |
| After 3 clean months | Auto-approve within the thresholds. |

---

## 8. Open decisions

- [ ] **Vercel data source:** does the app read HubSpot or a file? A file needs a sanitized export (no personal data) in a private location. **To be confirmed with Nikos's boss.** Step F stays a placeholder until then.
- [ ] **Slack channel and audience** (M&A / Expansion / Sales), level of detail: to be decided by Nikos later. The approver group is defined by the channel (see below), so the channel has to exist before the gate can be wired.
- [ ] **Auto-approve thresholds** after 3 months (proposal in §4, step D): not decided yet.

**Decided**

- [x] Architecture, monthly timeline and step design as above (Nikos, 2026-10-21: "overall everything looks fine").
- [x] **Data store and compute:** Google Drive (restricted folder) and GitHub are fine (Nikos, 2026-10-21). The GDPR documentation still has to name both as processors.
- [x] **Approval:** Nikos approves, but in practice anyone in the assigned Slack channel may approve; the channel is created and its members decided later (Nikos, 2026-10-21).
- [x] **TÜV SÜD timing:** the last working day of the month before the run (Nikos, 2026-10-21).

---

## 9. HubSpot import strategy: revised 2026-10-21 (ABORTED first import)

**Status:** Nikos aborted the first full import on 2026-10-21 before it started. **Nothing from the master has been imported into HubSpot** except the 49-row test sample. `hubspot_import_2026-10.csv` (4,821 rows) exists in `3_master_output/2026-10/` and **must not be imported as it is**.

**Why:** the plan assumed "no matching against existing records" (project plan §6, boss decision of the same day). That is not safe. HubSpot already holds **~53k companies** and the **M&A Deals**, which are sacred: they must never be deleted, overwritten or lose data. A deal may or may not belong to a company that is also a Prüfstelle in our master. A plain address match is not enough: a Prüfstelle and a Werkstatt can sit in the same building and are different companies.

### The three workstreams of the master import

| # | Workstream | What | Safety rule |
|---|---|---|---|
| 1 | **M&A Deals → connected companies** | Run a similarity analysis for ALL M&A Deals against the master and find the company each deal is connected to. Import these connected companies safely. | A deal is never deleted, merged away or changed. No data loss. Matches are proposals checked by a human, never auto-merged. |
| 2 | **Duplicates in the companies directory** | Work out how to handle duplicate companies among the ~53k existing ones. | Analysis first; nothing is deleted or merged without a decision. |
| 3 | **Everything else** | Import all other master companies as new companies. | Only after 1 and 2 define which master rows are already covered. |

Open for 1 and 2: which signals count as a match (name + address + PLZ + phone + website; a shared address alone must not match), how matches are scored and reviewed, and whether HubSpot's own duplicate management or our own code does the matching. To be designed; nothing built.

### New monthly mechanism (Nikos's idea): delta in code, upload only the changelog

Compare last month's master with the new one **locally in GitHub Actions** with code, and upload **only the changelog of NEW and RETIRED companies** to HubSpot. That saves about 95 % of the rows each month and keeps most of the matching risk out of the monthly run. It replaces the earlier plan (steps C and E in §4) of sending changes for all four categories.

- The baseline is the previous month's master, so the master import (workstreams 1–3) has to exist first.
- New companies still need the match check from workstreams 1–2 before they are created, so a new site never duplicates an existing company or touches a deal.
- Open: what happens to **changed** fields of existing companies now that only NEW and RETIRED are uploaded (dropped, or a rare separate step), and reactivations.
- Retired sites: `business_status = scraper-geschlossen` plus `compscraper_closedondate`, as before; never deleted.

### Superseded by this section

- Project plan §6 "Existing companies: no matching" and the same decision in `scraper_status.md` (item "HubSpot import").
- Prerequisite 1 in §6 above (first full import from the 2026-10 master as it is).
- Steps C and E in §4 (they stay as the technical shape, but the content becomes NEW and RETIRED only).

### Next steps (for tomorrow)

1. Decide how to read the M&A Deals and the ~53k companies for the analysis (HubSpot export file, or read-only API access with a token scoped to read).
2. Design the matching (signals, score, review list) and run it for all M&A Deals; Nikos reviews the proposals.
3. Duplicate analysis of the companies directory; decide how to handle it.
4. Build the import files for workstreams 1–3; test on a small sample.
5. Only then the baseline import; then the monthly delta automation.

---

## 10. Monthly entry point `core.monthly` (built 2026-10-28)

What GitHub Actions runs. One module (`2_core/monthly.py`), control notebook `2_core/notebook/monthly_control.ipynb`, offline tests `2_core/tests/test_monthly.py`.

### Commands

| Command | Trigger (n8n) | What it does |
|---|---|---|
| `python -m core.monthly tuev-sued plan --confirm` | (A) last working day before the 1st | Opens the TÜV SÜD run, writes the pass-1 snippet; its path is in the status file. |
| `python -m core.monthly tuev-sued check --file X.jsonl --confirm` | (B) upload in the Slack thread | Imports the pass. PASS: builds the TÜV SÜD run, then the master if this month's scrape is done. Gaps: next snippet. |
| `python -m core.monthly scrape --confirm` | (C) the 1st | HDP + every source except TÜV SÜD, one after the other, each in its own process with a time limit (GTÜ 3 h, KÜS 1 h, others 20 min). Sources that passed this month are skipped (`--force SLUG` reruns one). A failed source is retried once at the end. Then the master if TÜV SÜD is fresh, else it waits. |
| `python -m core.monthly build --without-tuev-sued --confirm` | timeout, 3 working days after the 1st | Builds the master with last month's TÜV SÜD data. |
| `python -m core.monthly status --confirm` | recovery | Rewrites the status file from the run folders on disk; no requests. |

Without `--confirm` every command only prints its plan (sources, expected requests) and sends and writes nothing. The automation always passes it.

### Rules

- **Fresh TÜV SÜD:** a qualifying TÜV SÜD run whose data started on or after the reminder day (last Mon–Fri before the 1st; `--cycle-start YYYY-MM-DD` overrides it after a holiday).
- **Build once:** whichever of scrape / TÜV SÜD finishes last builds; a lock file and "master folder exists" prevent a second build. Across two runners the Actions workflow needs a `concurrency:` group (the lock only protects one machine).
- **Failed source:** after its retry, the master uses its last passing run (pinned on purpose; the lifecycle treats the source as not fresh, so none of its rows retire) and the status names it "not refreshed". If that run is older than 45 days, the build stops.
- **HDP:** if the HDP input is unusable (failed or older than 7 days), the build reruns HDP once (1 request); if it still fails, the build stops.
- **Guards** are never loosened. A hard master check failure stops; `--accept-retirements` stays a human decision (move the failed month folder to `9_archive/` first).
- **TÜV Hessen** runs headless without the headed fallback (no screen on the runner): a block is a clean source failure. **FSP** runs with `allow_robots_disallowed=True`. **GTÜ** runs with early stop. `GTUE_API_KEY` is checked for presence only.

### Status file

`3_master_output/_monthly/YYYY-MM/monthly_status.json` (+ `monthly_status.md` for people), rewritten after every step; the only thing n8n reads. Contents:

- `phase`: `tuev_sued_waiting_for_upload`, `tuev_sued_next_pass`, `scrape_running`, `waiting_for_tuev_sued`, `waiting_for_scrape`, `build_running`, `built`, `build_failed`, `stopped`
- `exit_code`, `next_action`, `needs_human` (one line per item a person must look at)
- `scrape.sources.<slug>`: run folder, status, in-scope count, delta vs last month's master input, requests, duration, error, attempts
- `tuev_sued`: run folder, status, snippet path and pass number, message
- `build`: status, master folder, paths of `hubspot_changes` / overview / excluded (or `hubspot_import` for a first master), failed and review checks, pins, not refreshed sources, change counts

No personal data and no secrets: rows appear only as counts; names stay in the run folders.

### Exit codes

| Code | Meaning | Actions job |
|---|---|---|
| 0 | Everything passed, or a normal wait (TÜV SÜD not uploaded yet, next TÜV SÜD pass, master already built or being built) | green |
| 1 | Done, but something needs a human: a failed or not refreshed source, TÜV SÜD not refreshed or its pass failed/blocked, a review item of the master | red; n8n posts `needs_human` |
| 2 | Stopped, no master built: HDP failed twice, an input too old or missing, a hard master check failed, an unexpected error | red; n8n alerts |
