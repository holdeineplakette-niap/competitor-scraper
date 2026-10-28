"""TÜV SÜD run orchestration: plan the browser passes, import and check them, build the exports.

    from tuev_sued import run as tuev_run
    tuev_run.run(mode="simulated")                            # offline fake finder + fake browser, whole flow, must PASS
    p = tuev_run.plan(mode="sample")                          # writes tuev_sued_pass01.js (3 PLZ); paste it in the browser
    tuev_run.check(p.run_folder, "<downloaded .jsonl>")       # imports the pass; sample: just reports
    tuev_run.build(p.run_folder)                              # exports + run summary (sample: never PASS)
    p = tuev_run.plan(mode="full")                            # pass 1 of a monthly run
    c = tuev_run.check(p.run_folder, "<downloaded .jsonl>")   # PASS, or INCOMPLETE with c.next_plan (next snippet)
    tuev_run.build(p.run_folder)
    tuev_run.run(mode="rebuild", source_run="<run folder>")   # offline, from an earlier run's raw.jsonl

Python makes no request to TÜV SÜD at all: tuvsud.com blocks scripts (Cloudflare), so the queries run in Nikos's own
browser from a generated console snippet (browser.py, 0_instructions/sources/tuev_sued.md §2). A run is one folder,
opened by `plan` and reused by `check` and `build` (several manual passes). Coverage proof: coverage.py and
core/radius_cover.py ("Variable radius").
"""

from __future__ import annotations

import csv
import json
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from core import enrich, export as core_export, paths
from core.geo import ensure_geonames, haversine_km, load_plz_centroids
from tuev_sued import browser, classify, coverage, parse, simulate

SLUG = parse.SLUG
SOURCE_ORG = parse.SOURCE_ORG
MODES = ("simulated", "sample", "rebuild", "full")
LIVE_PASS_MODES = ("sample", "full")  # the modes whose passes are run by hand in the browser
SAMPLE_PLZ = ("80686", "24937", "01067")  # München, Flensburg (sparse area: exactly 50?), Dresden
FLENSBURG = "24937"
EXPORT_KEYS = core_export.EXPORT_KEYS  # everything that can be written
DEFAULT_EXPORTS = ("pruefstellen_csv",)  # what a run writes unless asked otherwise: no `all` file and no Excel (Nikos, 2026-10-06)
MAX_DROP = core_export.MAX_DROP
LOG_COLUMNS = [
    "pass", "seq", "zip", "city", "reason", "status", "records", "first_km", "last_km", "radius_km",
    "centre_lat", "centre_lng", "centre_offset_km", "pass_started", "pass_finished",
]
OPEN_TARGETS_FILE = "open_targets.csv"
_RUN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(full|sample|simulated|rebuild)(?:_\d+)?$")


class RunError(RuntimeError):
    """A step that cannot be done in the current state of the run folder (the message says what to do)."""


@dataclass
class RunResult:
    run_folder: Path
    summary: dict
    frame: pd.DataFrame
    files: dict


@dataclass
class CheckResult:
    """What `check` found after importing a pass. status: PASS · INCOMPLETE (next_plan holds the next pass) · FAIL
    (passes used up or nothing left to plan; build still writes the labelled exports) · BLOCKED (the site challenged the
    browser requests: stop and report) · SAMPLE (sample runs are never judged)."""

    run_folder: Path
    pass_no: int
    status: str
    coverage: dict
    imported: browser.PassImport
    next_plan: browser.PassPlan | None
    message: str


# --------------------------------------------------------------------------
# Run folders and files
# --------------------------------------------------------------------------

def _root(root) -> Path:
    return Path(root) if root is not None else paths.project_root()


def _centroids(centroids, root: Path) -> pd.DataFrame:
    return centroids if centroids is not None else load_plz_centroids(ensure_geonames(paths.geonames_dir(root)))


def _mode_of(folder: Path) -> str:
    m = _RUN_NAME.match(folder.name)
    if not m:
        raise RunError(f"{folder} is not a run folder (expected YYYY-MM-DD_HHMM_<mode>)")
    return m.group(3)


def _started_of(folder: Path) -> datetime:
    m = _RUN_NAME.match(folder.name)
    return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H%M")


def find_run(run: str | Path, root=None) -> Path:
    """A run folder given as a path or as its name (looked up in output/ and test_output/)."""
    p = Path(run)
    if p.is_dir():
        return p
    for mode in ("full", "sample", "simulated"):
        candidate = paths.output_dir(SLUG, mode, root) / str(run)
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"run folder not found: {run}")


def _read_lines(path: Path) -> list[tuple[str, object]]:
    """(file, response) of every line of a pass file or raw.jsonl. Iterates the file: splitlines() would also break at U+2028."""
    out = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            if line.strip():
                item = json.loads(line)
                out.append((item["file"], item["response"]))
    return out


def _split(lines: list[tuple[str, object]], reasons: dict | None = None) -> tuple[list[parse.Answer], dict]:
    """Answers (in file order) and the browser summaries by pass number."""
    answers, summaries = [], {}
    for name, response in lines:
        m = parse.SUMMARY_FILE.match(name)
        if m:
            summaries[int(response.get("pass", m.group(1) or 0))] = response
            continue
        ans = parse.to_answer(name, response)
        ans.reason = (reasons or {}).get((ans.pass_no, ans.seq), ans.reason)
        answers.append(ans)
    return answers, summaries


def _plan_reasons(folder: Path) -> dict:
    out = {}
    for n in browser.passes_planned(folder):
        plan = browser.read_plan(folder / browser.pass_js_name(n))
        for seq, (_, reason) in enumerate(plan.queries, 1):
            out[(n, seq)] = reason
    return out


def load_answers(folder: Path) -> tuple[list[parse.Answer], dict]:
    """Answers and summaries of all imported passes of a run folder (raw_passNN.jsonl), pass by pass."""
    reasons = _plan_reasons(folder)
    lines = []
    for n in browser.passes_imported(folder):
        lines += _read_lines(folder / browser.raw_pass_name(n))
    return _split(lines, reasons)


def previous_query_zips(root: Path, exclude: Path | None = None) -> list[str]:
    """Every PLZ of the last passing full run (its query_log.csv), in query order; [] when there is none."""
    base = core_export.baseline_run(paths.output_dir(SLUG, "full", root), exclude)
    if not base:
        return []
    log = paths.output_dir(SLUG, "full", root) / base["run_folder"] / paths.QUERY_LOG_FILE
    if not log.exists():
        return []
    return pd.read_csv(log, dtype={"zip": str})["zip"].dropna().tolist()


# --------------------------------------------------------------------------
# plan / check
# --------------------------------------------------------------------------

def plan(mode: str = "full", *, root=None, started_at: datetime | None = None, centroids=None) -> browser.PassPlan:
    """Open a run folder and write the snippet of pass 1. Makes no request.

    full: the PLZ of the last passing full run, else a hex lattice (spacing 60 km). sample: SAMPLE_PLZ (3 PLZ).
    simulated: like a first full run. Returns the plan; `.run_folder` and `.js_path` say where things are."""
    if mode not in ("full", "sample", "simulated"):
        raise ValueError("plan() opens a run of mode 'full', 'sample' or 'simulated'")
    root = _root(root)
    started = started_at or datetime.now()
    if mode == "sample":
        queries = [(z, "sample") for z in SAMPLE_PLZ]
    else:
        previous = previous_query_zips(root) if mode == "full" else []
        queries = coverage.first_pass_queries(_centroids(centroids, root), previous)
    folder = paths.new_run_dir(SLUG, mode, started, root)
    p = browser.PassPlan(pass_no=1, queries=queries, mode=mode)
    browser.write_snippet(folder, p)
    return p


def check(run_folder: str | Path, download: str | Path, *, centroids=None, root=None, say=print) -> CheckResult:
    """Import the file a browser pass downloaded and prove coverage over all passes so far.

    The file is validated first (bad line, missing summary, wrong pass or plan: ImportProblem, nothing is copied), then
    copied into the run folder as raw_passNN.jsonl. If coverage is not complete and a pass is left, the next snippet
    (fill-in queries, at most 60 PLZ) is written and returned in `next_plan`."""
    root = _root(root)
    folder = find_run(run_folder, root)
    mode = _mode_of(folder)
    pending = [n for n in browser.passes_planned(folder) if n not in browser.passes_imported(folder)]
    if not pending:
        raise RunError("no pass is waiting for a file: run build() to finish the run")
    plan_ = browser.read_plan(folder / browser.pass_js_name(pending[0]))
    imported = browser.import_pass(Path(download), folder, plan_)
    targets = _centroids(centroids, root)
    answers, _ = load_answers(folder)
    cov = coverage.evaluate(targets, answers)
    n_done = len(browser.passes_imported(folder))
    next_plan, message = None, ""
    if mode == "sample":
        status = "SAMPLE"
        message = f"Sample pass imported: {len(imported.answers)} answers. Run build() for the report."
    elif imported.blocked:
        status = "BLOCKED"
        message = (
            "TÜV SÜD challenged or refused the in-browser requests "
            f"({json.dumps(imported.stopped_reason, ensure_ascii=False)[:300]}). Stop here and report it; there is no workaround. "
            "build() still writes what was collected, labelled."
        )
    elif cov["ok"]:
        status = "PASS"
        message = f"Coverage complete after {n_done} pass(es): all {cov['targets']} targets covered. Run build()."
    elif n_done >= coverage.MAX_PASSES:
        status = "FAIL"
        message = f"{len(cov['uncovered'])} targets still uncovered after the maximum of {coverage.MAX_PASSES} passes. Run build() (labelled exports, open_targets.csv)."
    else:
        queries = coverage.fill_in_queries(targets, answers)
        if not queries:
            status = "FAIL"
            message = (
                f"{len(cov['uncovered'])} targets uncovered and nothing left to plan"
                + ("" if cov["premise"]["ok"] else " (the premise check failed: the answers are not the true nearest sites)")
                + ". Run build()."
            )
        else:
            status = "INCOMPLETE"
            next_plan = browser.PassPlan(pass_no=n_done + 1, queries=queries, mode=mode)
            browser.write_snippet(folder, next_plan)
            message = f"{len(cov['uncovered'])} targets uncovered: pass {next_plan.pass_no} ({len(queries)} PLZ) written to {next_plan.js_path.name}."
    if imported.stopped_reason and status != "BLOCKED":
        message += f" Note: the pass stopped early ({imported.stopped_reason.get('reason')})."
    say(f"pass {imported.pass_no}: {len(imported.answers)} answers · {status} · {message}")
    return CheckResult(folder, imported.pass_no, status, cov, imported, next_plan, message)


# --------------------------------------------------------------------------
# Processing (shared by build, rebuild and the simulated flow)
# --------------------------------------------------------------------------

def query_log_rows(answers: list[parse.Answer], summaries: dict, centroids: pd.DataFrame) -> list[dict]:
    where = centroids.set_index("zip")
    rows = []
    for a in answers:
        offset = None
        if a.zip in where.index:
            offset = round(float(haversine_km(a.centre[0], a.centre[1], where.at[a.zip, "lat"], where.at[a.zip, "lng"])), 2)
        summary = summaries.get(a.pass_no, {})
        rows.append({
            "pass": a.pass_no, "seq": a.seq, "zip": a.zip, "city": where.at[a.zip, "city"] if a.zip in where.index else None,
            "reason": a.reason, "status": "ok" if a.count else "empty", "records": a.count,
            "first_km": None if a.first_km is None else round(a.first_km, 2), "last_km": None if a.last_km is None else round(a.last_km, 2),
            "radius_km": round(a.radius_km, 2), "centre_lat": a.centre[0], "centre_lng": a.centre[1], "centre_offset_km": offset,
            "pass_started": summary.get("started"), "pass_finished": summary.get("finished"),
        })
    return rows


def _csv_write(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "x", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def status_of(mode: str, cov: dict, blocked: bool, drop_alert: bool, n_answers: int) -> tuple[str, list[str]]:
    reasons = []
    if blocked:
        reasons.append("a pass was blocked by the site (challenge / refusal)")
    if not n_answers:
        reasons.append("no answers")
    if cov["uncovered"]:
        reasons.append(f"{len(cov['uncovered'])} targets are not covered (disk radius - {cov['cover_margin_km']:g} km)")
    if not cov["premise"]["ok"]:
        reasons.append("premise check failed: answers not sorted by distance or a site inside a disk was not listed")
    if drop_alert:
        reasons.append("drop check alert")
    if mode == "sample":
        return "SAMPLE", reasons
    return ("FAIL" if reasons else "PASS"), reasons


def export_tag(status: str, cov: dict) -> str | None:
    """Exports of a run that is not a PASS carry a label so nobody mistakes them for a verified crawl."""
    if status in ("PASS", "SAMPLE"):
        return None
    return "grid-incomplete" if cov["uncovered"] or not cov["queries"] else "unverified"


def sample_findings(answers: list[parse.Answer]) -> dict:
    """Sample run: what each PLZ returned, and the question the sample pass exists for (does Flensburg return exactly 50?)."""
    per = [{"zip": a.zip, "count": a.count, "first_km": a.first_km, "last_km": a.last_km, "locationname": a.locationname} for a in answers]
    flensburg = next((a for a in answers if a.zip == FLENSBURG), None)
    return {
        "answers": per,
        "flensburg_count": None if flensburg is None else flensburg.count,
        "flensburg_exactly_50": None if flensburg is None else flensburg.count == coverage.EXPECTED_COUNT,
        "all_exactly_50": all(a.count == coverage.EXPECTED_COUNT for a in answers) if answers else None,
    }


def process(
    *,
    mode: str,
    run_folder: Path,
    root: Path,
    t0: float,
    answers: list[parse.Answer],
    summaries: dict,
    centroids: pd.DataFrame,
    exports=DEFAULT_EXPORTS,
    extra: dict | None = None,
    unimported_passes: list[int] | None = None,
    say=print,
) -> RunResult:
    started = _started_of(run_folder) if _RUN_NAME.match(run_folder.name) else datetime.now()
    scraped_at, run_date = started.isoformat(timespec="seconds"), started.date()
    blocked = any(browser.is_block(s.get("stopped_reason")) for s in summaries.values())
    _csv_write_if(run_folder / paths.QUERY_LOG_FILE, query_log_rows(answers, summaries, centroids))

    items, conflicts = parse.merge_items(answers)
    df, stats = parse.build_frame(list(items.values()), scraped_at, run_date)
    if len(df):
        df = enrich.enrich(df, centroids, check=df["country"] == parse.HOME_COUNTRY)
        df = classify.classify_frame(df)
        for col in ("huau", "tsc_status"):
            df[col] = df[col].astype("Int64")
    cov = coverage.evaluate(centroids, answers)
    in_scope = df[df["in_scope"]] if len(df) else df

    baseline = core_export.baseline_run(paths.output_dir(SLUG, "full", root), run_folder) if mode in ("full", "rebuild") else None
    drop_text, drop_alert = "not run", False
    if mode in ("full", "simulated", "rebuild"):
        drop_text, drop_alert = core_export.drop_check(len(in_scope), baseline)
        if mode == "simulated":
            drop_text, drop_alert = "not run (simulated)", False
    status, reasons = status_of(mode, cov, blocked, drop_alert, len(answers))
    outcome = {"PASS": "PASS (every target covered, premise holds)", "FAIL": "FAIL", "SAMPLE": "SAMPLE (no coverage proof)"}[status]
    if cov["uncovered"] and mode != "sample":
        where = centroids.set_index("zip").loc[cov["uncovered"]].reset_index()[["zip", "city", "state", "lat", "lng"]]
        _csv_write_if(run_folder / OPEN_TARGETS_FILE, where.to_dict("records"), ["zip", "city", "state", "lat", "lng"])

    renamed: list = []
    tag = export_tag(status, cov)
    written = core_export.export(df, run_folder, SLUG, run_date.isoformat(), renamed, tag, only=exports) if len(df) else {}

    flags: dict = {}
    for value in df["doubt_flags"].dropna() if len(df) else []:
        for f in value.split("; "):
            flags[f] = flags.get(f, 0) + 1
    pass_rows = [
        {"pass": n, "planned": s.get("planned"), "done": s.get("done"), "started": s.get("started"), "finished": s.get("finished"),
         "stopped_reason": s.get("stopped_reason"), "snippet_version": s.get("snippet_version")}
        for n, s in sorted(summaries.items())
    ]
    browser_s = 0.0
    for s in summaries.values():
        try:
            browser_s += (datetime.fromisoformat(s["finished"].replace("Z", "+00:00")) - datetime.fromisoformat(s["started"].replace("Z", "+00:00"))).total_seconds()
        except (KeyError, ValueError, AttributeError):
            pass
    warnings = {
        "address_conflicts": conflicts,
        "opening_hours_unparsed": stats["opening_hours_unparsed"],
        "notice_window_fields_missing": stats["notice_window_missing"],
        "sites_without_coordinates": int(df["lat"].isna().sum()) if len(df) else 0,
        "passes_never_imported": unimported_passes or [],
        **{k: v for k, v in coverage.compact(cov)["warnings"].items()},
    }
    summary = {
        "source": SOURCE_ORG,
        "mode": mode,
        "status": status,
        "outcome": outcome,
        "status_reasons": reasons,
        "run_date": run_date.isoformat(),
        "started_at": scraped_at,
        "runtime_s": round(time.time() - t0, 1),
        "browser_s": round(browser_s, 1),
        "requests_made": len(answers),
        "passes": pass_rows,
        "coverage": coverage.compact(cov),
        "rows_total": int(len(df)),
        "in_scope": int(len(in_scope)),
        "site_types": core_export.counts(df["site_type"]) if len(df) else {},
        "flags": flags,
        "needs_review": int(df["needs_review"].sum()) if len(df) else 0,
        "review_reasons": enrich.reason_counts(df) if len(df) else {},
        "in_scope_by_state": core_export.counts(in_scope["state"].fillna("unknown")) if len(df) else {},
        "in_scope_by_operator": core_export.counts(in_scope["operator_group"].fillna("unknown")) if len(df) else {},
        "warnings": {k: v for k, v in warnings.items() if v},
        "drop_check": drop_text,
        "drop_alert": drop_alert,
        "baseline": baseline,
        "location_key_fill": parse.key_fill(items),
        "exports": list(exports),
        "files": {"run_folder": core_export.relative(run_folder, root)}
        | {key: core_export.relative(path, root) for key, (path, _rows) in written.items()},
    }
    if mode == "sample":
        summary["sample_findings"] = sample_findings(answers)
    summary.update(extra or {})
    text = format_run_summary(summary)
    (run_folder / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run_folder / paths.RUN_SUMMARY_MD).write_text(f"# Run summary: {run_folder.name}\n\n```text\n{text}\n```\n", encoding="utf-8")
    say(text)
    return RunResult(run_folder=run_folder, summary=summary, frame=df, files=written)


def _csv_write_if(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    if rows:
        _csv_write(path, rows, columns or LOG_COLUMNS)


def format_run_summary(s: dict) -> str:
    cov = s["coverage"]
    lines = [f"TÜV SÜD run summary ({s['mode']}): {s['outcome']}", "=" * 50,
             f"Run date:           {s['run_date']}  (browser time {s['browser_s']} s, processing {s['runtime_s']} s)",
             f"Passes / answers:   {len(s['passes'])} pass(es), {s['requests_made']} answers"]
    for p in s["passes"]:
        stop = f", stopped: {p['stopped_reason'].get('reason')}" if p.get("stopped_reason") else ""
        lines.append(f"  pass {p['pass']}: {p['done']}/{p['planned']} PLZ answered{stop}")
    if s["status_reasons"]:
        lines += ["FAIL because:" if s["status"] == "FAIL" else "Notes:"] + [f"  ! {r}" for r in s["status_reasons"]]
    unc = cov["uncovered"]
    lines += ["",
              f"Coverage:           {'ok  ' if not unc['total'] else 'FAIL'} {cov['targets'] - unc['total']}/{cov['targets']} targets covered "
              f"(disk radius - {cov['cover_margin_km']:g} km)",
              f"Premise:            {'ok  ' if cov['premise']['ok'] else 'FAIL'} sorted by distance, no listed-site missing from a disk"
              + ("" if cov["premise"]["ok"] else f" ({cov['premise']['misses']['total']} misses, {cov['premise']['unsorted_answers']['total']} unsorted)")]
    if cov.get("radius_km"):
        r = cov["radius_km"]
        lines.append(f"Proven radius:      min {r['min']} / median {r['median']} / max {r['max']} km; max site-to-PLZ-centroid {cov.get('max_site_to_plz_centroid_km')} km")
    lines += ["",
              f"Collected:          {s['rows_total']} unique Service-Center",
              f"In scope:           {s['in_scope']} (huau = 1, tscStatus = 1, Germany)",
              f"  by operator:      {s.get('in_scope_by_operator', {})}",
              f"Site types:         {s['site_types']}",
              f"Needs review:       {s['needs_review']}  {s['review_reasons']}",
              f"Flags:              {s['flags']}"]
    sf = s.get("sample_findings")
    if sf:
        lines.append("Sample answers:     " + ", ".join(f"{a['zip']}: {a['count']} sites, last {a['last_km']} km" for a in sf["answers"]))
        lines.append(f"Flensburg {FLENSBURG}: {sf['flensburg_count']} sites -> exactly 50: {sf['flensburg_exactly_50']}")
    for key, value in (s.get("warnings") or {}).items():
        if isinstance(value, dict) and "total" in value:
            lines.append(f"Warning {key}: {value['total']}  {value['items'][:3]}")
        else:
            lines.append(f"Warning {key}: {value if not isinstance(value, (list, dict)) else len(value)}")
    lines.append(f"Drop check:         {s['drop_check']}")
    if s.get("baseline"):
        lines.append(f"Baseline:           {s['baseline']['run_folder']} ({s['baseline']['in_scope']} in scope)")
    lines += ["", "Files:"] + [f"  {k:<18}{v}" for k, v in s["files"].items()]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# build / rebuild / simulated
# --------------------------------------------------------------------------

def merge_raw(folder: Path, passes: list[int]) -> Path:
    """raw.jsonl = the pass files one after the other. Answer lines are copied as they are; each browser summary line
    is renamed browser_summary_passNN.json so that a rebuild can tell the passes apart."""
    target = folder / paths.RAW_FILE
    with open(target, "x", encoding="utf-8", newline="\n") as out:
        for n in passes:
            with open(folder / browser.raw_pass_name(n), encoding="utf-8-sig") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if parse.SUMMARY_FILE.match(item["file"]):
                        out.write(json.dumps({"file": f"browser_summary_pass{n:02d}.json", "response": item["response"]}, ensure_ascii=False) + "\n")
                    else:
                        out.write(line.rstrip("\r\n") + "\n")
    return target


def build(run_folder: str | Path, *, centroids=None, root=None, exports=DEFAULT_EXPORTS, say=print) -> RunResult:
    """Finish a run: merge the pass files into raw.jsonl, classify, export, write the run summary (and open_targets.csv).

    Judges the coverage of everything imported so far: PASS only if every target is covered; otherwise FAIL with labelled
    exports (grid-incomplete / unverified). A sample run is never judged. A folder is built once."""
    t0 = time.time()
    root = _root(root)
    folder = find_run(run_folder, root)
    mode = _mode_of(folder)
    if (folder / paths.RUN_SUMMARY_JSON).exists():
        raise RunError(f"{folder.name} is built already; use run(mode='rebuild', source_run=...) for a new look at its raw data")
    imported = browser.passes_imported(folder)
    if not imported:
        raise RunError("no pass was imported yet: run check() with the downloaded file first")
    answers, summaries = load_answers(folder)
    merge_raw(folder, imported)
    never = [n for n in browser.passes_planned(folder) if n not in imported]
    return process(mode=mode, run_folder=folder, root=root, t0=t0, answers=answers, summaries=summaries,
                   centroids=_centroids(centroids, root), exports=exports, unimported_passes=never, say=say)


def run(
    mode: str = "simulated",
    *,
    root=None,
    started_at: datetime | None = None,
    centroids=None,
    sites: pd.DataFrame | None = None,
    api=None,
    browser_kwargs=None,
    source_run: str | Path | None = None,
    exports=DEFAULT_EXPORTS,
    verbose: bool = True,
) -> RunResult:
    """simulated: the whole flow offline (fake finder, fake browser, same import code). rebuild: offline from the
    raw.jsonl of an earlier run into a new folder in output/ (no copy of raw.jsonl). sample and full are run step by
    step: plan() -> browser -> check() -> ... -> build().

    browser_kwargs: simulated only; a dict, or a function pass_no -> dict, of simulate.browser_download options
    (drop_answers, stop_after, stopped_reason) for the negative tests."""
    if mode in LIVE_PASS_MODES:
        raise ValueError(f"mode={mode!r} is run step by step with plan(), the browser snippet, check() and build()")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    root = _root(root)
    started = started_at or datetime.now()
    t0 = time.time()
    centroids = _centroids(centroids, root)

    def say(msg):
        if verbose:
            print(msg, flush=True)

    if mode == "rebuild":
        if source_run is None:
            raise ValueError("mode='rebuild' needs source_run (a run folder or its name)")
        src = find_run(source_run, root)
        answers, summaries = _split(_read_lines(src / paths.RAW_FILE))
        for a in answers:
            a.reason = "rebuild"
        folder = paths.new_run_dir(SLUG, mode, started, root)
        return process(mode=mode, run_folder=folder, root=root, t0=t0, answers=answers, summaries=summaries, centroids=centroids,
                       exports=exports, extra={"source_run": core_export.relative(src, root)}, say=say)

    api = api or simulate.FakeTuevApi(sites if sites is not None else simulate.simulated_sites(centroids), centroids)
    p = plan("simulated", root=root, started_at=started, centroids=centroids)
    folder, clock = p.run_folder, started
    with tempfile.TemporaryDirectory() as tmp:
        while p is not None:
            options = browser_kwargs(p.pass_no) if callable(browser_kwargs) else (browser_kwargs or {})
            download = Path(tmp) / f"tuev_sued_pass{p.pass_no:02d}_simulated.jsonl"
            download.write_text(simulate.browser_download(p, api, clock, **options), encoding="utf-8")
            checked = check(folder, download, centroids=centroids, root=root, say=say)
            p, clock = checked.next_plan, clock + timedelta(minutes=20)
    return build(folder, centroids=centroids, root=root, exports=exports, say=say)


def estimate(centroids: pd.DataFrame, seed: int = 0, sites: pd.DataFrame | None = None) -> dict:
    """Before a real run: how many passes and queries the simulated world needed (no folder, no files). The real number
    depends on where TÜV SÜD's sites really are, so treat it as an order of magnitude."""
    api = simulate.FakeTuevApi(sites if sites is not None else simulate.simulated_sites(centroids, seed=seed), centroids, seed=seed)
    queries = coverage.first_pass_queries(centroids)
    first = list(queries)
    answers: list[parse.Answer] = []
    per_pass: list[int] = []
    ok = False
    for pass_no in range(1, coverage.MAX_PASSES + 1):
        for seq, (plz, reason) in enumerate(queries, 1):
            answers.append(parse.to_answer(parse.answer_name(pass_no, seq, plz), api.query(plz), reason))
        per_pass.append(len(queries))
        ok = coverage.evaluate(centroids, answers)["ok"]
        queries = [] if ok else coverage.fill_in_queries(centroids, answers)
        if not queries:
            break
    total = sum(per_pass)
    per_request_s = browser.PACE_S + browser.JITTER_S / 2 + 1.0
    return {
        "planned_queries": len(first),
        "planned_points": centroids[centroids["zip"].isin([z for z, _ in first])].reset_index(drop=True),
        "queries_per_pass": per_pass,
        "expected_passes": len(per_pass),
        "expected_queries": total,
        "expected_minutes_per_pass": [round(n * per_request_s / 60, 1) for n in per_pass],
        "simulated_ok": ok,
    }
