# n8n ↔ GitHub Actions contract (competitor scraper)

## n8n → Actions: `repository_dispatch`

`POST https://api.github.com/repos/<owner>/<repo>/dispatches` (fine-grained token, Contents: write)

```json
{ "event_type": "<see table>", "client_payload": { "run_month": "2026-11", "n8n_execution_id": "123" } }
```

| event_type | Actions runs | client_payload extra |
|---|---|---|
| `tuev-sued-plan` | `python -m core.monthly tuev-sued plan --confirm` | – |
| `tuev-sued-check` | `python -m core.monthly tuev-sued check --file <file> --confirm` | `file`: `3_master_output/_monthly/<run_month>/tuev_sued_upload/<name>.jsonl` |
| `fsp-check` | `python -m core.monthly fsp check --file <file> --confirm` | `file`: `3_master_output/_monthly/<run_month>/fsp_upload/fsp_YYYY-MM-DD_HHMM.jsonl` |
| `scrape` | `python -m core.monthly scrape --confirm` | – |
| `build-without-tuev-sued` | `python -m core.monthly build --without-tuev-sued --confirm` | – |
| `status` | `python -m core.monthly status --confirm` | – |

Actions YAML: `on: repository_dispatch: types: [tuev-sued-plan, tuev-sued-check, fsp-check, scrape, build-without-tuev-sued, status]`, plus `workflow_dispatch` for manual runs, and one `concurrency:` group (plan §10 "Build once").

## Actions → n8n: status callback

After every run, also when the command fails (`if: always()`), send:

`POST https://holdeineplakette.app.n8n.cloud/webhook/competitor-scraper/status`
Header `X-Scraper-Token: ${{ secrets.N8N_CALLBACK_TOKEN }}`

```json
{
  "command": "tuev-sued-plan",
  "run_url": "https://github.com/<owner>/<repo>/actions/runs/<id>",
  "status": { ...contents of 3_master_output/_monthly/YYYY-MM/monthly_status.json... },
  "snippet_js": "<text of status.tuev_sued.snippet if phase is tuev_sued_waiting_for_upload or tuev_sued_next_pass, else null>",
  "fsp_snippet_js": "<text of status.fsp.snippet for command tuev-sued-plan while status.fsp.status is WAITING_FOR_UPLOAD, else null>"
}
```

- If the status file is missing (crash before it was written), send `status: { "exit_code": 2, "phase": "stopped", "needs_human": ["<short error, no secrets>"] }`.
- No personal data and no secrets in the payload (status.json is already clean; the snippets contain only TÜV SÜD / FSP query code).
- n8n routes: snippet → .js file in Slack (`fsp_snippet.js` into the same thread when `fsp_snippet_js` is set); exit 0 → OK; exit 1 → needs human; exit 2 / anything else → stopped.
- `build-without-tuev-sued` on a month whose master is already built (TÜV SÜD came in time; nothing written): the callback sends `status: { "month", "simulated", "exit_code": 0, "phase": "already_built", "needs_human": [], "next_action": "nothing to do: ..." }` instead of the stored status, so n8n posts one OK line and starts no HubSpot approval (approval only for phase `built`).
- `fsp-check`: OK when `status.fsp.status` is `PASS`, else needs human. It never changes `status.phase` (may still be `built`), so n8n must never start the HubSpot approval for `fsp-check`.

## Details (checked against `core.monthly`, 2026-10-28)

- **`run_month` is the cycle month, not the calendar month.** From the reminder day (last Mon–Fri before the 1st) on, the TÜV SÜD steps belong to the next month: the plan on Fri 2026-10-30 writes `3_master_output/_monthly/2026-11/monthly_status.json`. n8n sends `run_month` = the month whose master is being built; Actions reads the status file of that month and falls back to the newest `_monthly/*/monthly_status.json` if `run_month` is missing.
- **`status.tuev_sued.snippet` is a file path** (relative to the data root, e.g. `1_scrapers/tuev_sued/output/<run>/tuev_sued_pass01.js`), not the code. Actions reads that file and sends its text as `snippet_js`.
- **Data root:** every job works on the synced data copy (`PRUEFSTELLEN_DATA_ROOT`); without it the TÜV SÜD run folder opened by `plan` is lost before `check`. The `file` path of `tuev-sued-check` is defined together with the Drive sync.
- **`status.fsp`** (2026-11-03): `{status: WAITING_FOR_UPLOAD | PASS | FAIL | BLOCKED | IMPORT_ERROR, run_folder, in_scope, message, snippet, upload}`. `snippet` is a file path (`3_master_output/_monthly/<month>/fsp_snippet.js`), sent as `fsp_snippet_js`. fsp.de refuses the GitHub runner's IP range (HTTP 403), so FSP is not in `scrape`; it comes from the browser snippet posted with the TÜV SÜD pass 1.
