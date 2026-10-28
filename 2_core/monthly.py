"""Headless monthly entry point of the Prüfstellen pipeline: scrape every source, take in TÜV SÜD, build the official master.

    .venv\\Scripts\\python.exe -m core.monthly scrape    [--confirm] [--force SLUG ...]
    .venv\\Scripts\\python.exe -m core.monthly tuev-sued plan|check|build [--file X.jsonl] [--confirm]
    .venv\\Scripts\\python.exe -m core.monthly build     [--confirm] [--without-tuev-sued] [--accept-retirements]
    .venv\\Scripts\\python.exe -m core.monthly status    [--confirm]

The month has up to three trigger moments (n8n starts each one as a GitHub Actions job; 0_instructions/scraper_automation_plan.md):
  * last working day before the 1st: `tuev-sued plan` writes the pass-1 snippet; Nikos runs it in his browser and uploads the
    .jsonl; `tuev-sued check --file` imports it (PASS -> the TÜV SÜD run is built; gaps -> the next snippet).
  * the 1st: `scrape` runs HDP and every scraped source (not TÜV SÜD), one after the other, each in its own process with a
    wall-clock timeout; a failed source is retried once at the end and never stops the others.
  * the master is built by whichever finishes last: `scrape` and `tuev-sued` both call the build when the other half is ready.
    After the timeout n8n calls `build --without-tuev-sued` (TÜV SÜD rows stay active with last month's data).

Without --confirm a command only prints what it would do (sources, expected requests) and sends nothing. The automation always
passes --confirm; the flag only makes an accidental run by a person or a Claude session harmless.

The build runs at most once per month (a lock file, and nothing happens when the master folder exists). It never loosens a
master guard: a source whose newest run failed is pinned to its newest qualifying run (the pin mechanism of master_run; the
lifecycle still treats it as not fresh, so none of its rows retire) only if that run is at most MAX_INPUT_AGE_DAYS old.
`--accept-retirements` stays a human decision.

Fresh TÜV SÜD: a qualifying TÜV SÜD run whose data started on or after the cycle's reminder day (the last Mon-Fri before the 1st;
`--cycle-start YYYY-MM-DD` overrides it, e.g. after a holiday).

Status: 3_master_output/_monthly/YYYY-MM/monthly_status.json (+ .md), rewritten after every step; the only thing n8n reads.
`status` rebuilds it from the run folders on disk (no requests), e.g. after a crash.

Exit codes (for GitHub Actions):
  0  everything passed, or a normal wait (TÜV SÜD not uploaded yet, next TÜV SÜD pass, master already built or being built)
  1  done, but something needs a human: a source failed or was not refreshed, TÜV SÜD was not refreshed or its pass failed,
     a review item of the master
  2  stopped, no master built: HDP failed twice, an input too old or missing, a hard master check failed, an unexpected error

Secrets: GTUE_API_KEY is only checked for presence (environment or .env), never read into the status or printed.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd
from dotenv import dotenv_values

from core import hdp_removal, master_run, paths
from core import master_inputs as mi
from core.export import relative

HDP = "hdp"
TUEV_SUED = "tuev_sued"
SCRAPE_ORDER = ("hdp", "tuev_rheinland", "tuev_thueringen", "tuev_nord", "vuek", "fsp", "dekra", "tuev_hessen", "kues", "gtue")
GTUE_KEY = "GTUE_API_KEY"
MAX_INPUT_AGE_DAYS = master_run.MAX_INPUT_AGE_DAYS
BUILD_LOCK = "build.lock"
STATUS_LOCK = "status.lock"
BUILD_LOCK_STALE_S = 6 * 3600  # a build lock older than this is left over from a crash
STATUS_LOCK_WAIT_S = 60

# expected live requests per source (shown in the plan; each source enforces its own caps and delays)
REQUESTS = {
    "hdp": "1", "gtue": "~900 (cap 2,000)", "kues": "~182 (cap 900)", "dekra": "6", "fsp": "~45 (robots.txt disregarded, Nikos)",
    "tuev_nord": "5 (up to 13)", "tuev_rheinland": "4", "tuev_hessen": "3 page loads + 4 searches + 2 detail pages (Playwright)",
    "tuev_thueringen": "2", "vuek": "~16 (11 site + 5 Nominatim)",
}
TIMEOUTS = {"gtue": 3 * 3600, "kues": 3600}  # seconds; every other source DEFAULT_TIMEOUT
DEFAULT_TIMEOUT = 20 * 60


def say(msg: str) -> None:
    print(msg, flush=True)


def label(slug: str) -> str:
    return "HDP" if slug == HDP else mi.SOURCES[slug].label


# ---- the source runs (top level, so a child process can import them) ----------------------------------------------
def _run_hdp(mode, root, started, centroids):
    from hdp import run as m
    m.run(mode, confirm_full_run=mode == "full", root=root, now=started)


def _run_gtue(mode, root, started, centroids):
    from gtue import adapter as m
    m.run(mode=mode, confirm_full_run=mode == "full", root=root, started_at=started, centroids=centroids, early_stop=mode == "full")


def _run_kues(mode, root, started, centroids):
    from kues import run as m
    m.run(mode, confirm_full_run=mode == "full", root=root, started_at=started, centroids=centroids, exports=("pruefstellen_csv",))


def _run_dekra(mode, root, started, centroids):
    from dekra import adapter as m
    m.run(mode, confirm_full_run=mode == "full", root=root, started_at=started, centroids=centroids, exports=("pruefstellen_csv",))


def _run_fsp(mode, root, started, centroids):
    from fsp import run as m
    m.run(mode, confirm_full_run=mode == "full", root=root, now=started, allow_robots_disallowed=True)


def _run_tuev_nord(mode, root, started, centroids):
    from tuev_nord import run as m
    m.run(mode, confirm_full_run=mode == "full", root=root, now=started, centroids=centroids)


def _run_tuev_rheinland(mode, root, started, centroids):
    from tuev_rheinland import run as m
    m.run(mode, confirm_full_run=mode == "full", root=root, now=started, centroids=centroids)


def _run_tuev_thueringen(mode, root, started, centroids):
    from tuev_thueringen import run as m
    m.run(mode, confirm_full_run=mode == "full", root=root, now=started, centroids=centroids)


def _run_tuev_hessen(mode, root, started, centroids):
    from tuev_hessen import run as m
    # unattended: no visible browser to fall back to, so a block is a clean failure of this source
    m.run(mode, confirm_full_run=mode == "full", root=root, now=started, centroids=centroids, headless=True, fallback_headed=False)


def _run_vuek(mode, root, started, centroids):
    from vuek import run as m
    m.run(mode, confirm_full_run=mode == "full", root=root, now=started, centroids=centroids)


RUNNERS: dict[str, Callable] = {
    "hdp": _run_hdp, "gtue": _run_gtue, "kues": _run_kues, "dekra": _run_dekra, "fsp": _run_fsp, "tuev_nord": _run_tuev_nord,
    "tuev_rheinland": _run_tuev_rheinland, "tuev_thueringen": _run_tuev_thueringen, "tuev_hessen": _run_tuev_hessen, "vuek": _run_vuek,
}


# ---- context ------------------------------------------------------------------------------------------------------
@dataclass
class Context:
    """Everything a command needs besides its own arguments. Tests and the notebook replace the clock, the runners, etc.

    simulated: every source runs in its simulated mode, each run folder is copied into output/ as `<stamp>_full_simulated`
    (summary marked "simulated": true) so the master can select it, and the master is a dev run (3_master_output/_dev/).
    Only allowed in a sandbox root, never in the code folder or the data root."""

    root: Path | str | None = None
    simulated: bool = False
    clock: Callable[[], datetime] = datetime.now
    centroids: pd.DataFrame | None = None
    isolation: str = "process"  # "process": each source in a child process with a timeout; "inline": in this process (tests)
    runners: dict[str, Callable] = field(default_factory=dict)  # slug -> fn(mode, root, started, centroids), replaces RUNNERS
    timeouts: dict[str, float] = field(default_factory=dict)
    master_kwargs: dict = field(default_factory=dict)  # extra arguments of master_run.run (tests: centroids, geocoder, ...)
    cycle_start: date | None = None
    say: Callable[[str], None] = say

    def __post_init__(self):
        self.root = Path(self.root) if self.root is not None else paths.project_root()
        if self.isolation not in ("process", "inline"):
            raise ValueError("isolation must be 'process' or 'inline'")
        if self.simulated and self.root.resolve() in (paths.code_root().resolve(), paths.project_root().resolve()):
            raise ValueError("simulated runs write fake runs into output/: use a sandbox root, never the project or data root")

    def now(self) -> datetime:
        return self.clock()


@dataclass
class Outcome:
    code: int
    status: dict | None
    message: str = ""


# ---- month and cycle ----------------------------------------------------------------------------------------------
def month_of(d: date | datetime) -> str:
    return f"{d:%Y-%m}"


def month_start(month: str) -> datetime:
    return datetime.strptime(month + "-01", "%Y-%m-%d")


def reminder_day(month: str) -> date:
    """The last Mon-Fri before the 1st of `month` (public holidays are not known here; n8n passes --cycle-start then)."""
    d = month_start(month).date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def cycle_month(today: date) -> str:
    """The month whose master the TÜV SÜD step feeds: the next month from its reminder day on, else this month."""
    first_next = (date(today.year, today.month, 1) + timedelta(days=32)).replace(day=1)
    nxt = month_of(first_next)
    return nxt if today >= reminder_day(nxt) else month_of(today)


def cycle_start(ctx: Context, month: str) -> datetime:
    d = ctx.cycle_start or reminder_day(month)
    return datetime(d.year, d.month, d.day)


def _folder_started(folder: Path) -> datetime | None:
    try:
        return datetime.strptime(folder.name[:15], "%Y-%m-%d_%H%M")
    except ValueError:
        return None


# ---- inputs on disk -----------------------------------------------------------------------------------------------
def fresh_tuev_sued(root: Path, since: datetime) -> mi.Candidate | None:
    """The newest qualifying TÜV SÜD run whose data started on or after `since` (the cycle start), or None."""
    for c in mi.candidates(TUEV_SUED, root):
        if c.qualifies and (c.root_started or c.started) >= since:
            return c
    return None


def open_tuev_sued_run(root: Path, since: datetime) -> Path | None:
    """The newest TÜV SÜD full run of this cycle that is not built yet (plan written, passes pending or imported)."""
    out = paths.output_dir(TUEV_SUED, "full", root)
    if not out.is_dir():
        return None
    for d in sorted((p for p in out.iterdir() if p.is_dir()), reverse=True):
        started = _folder_started(d)
        if started is None or started < since or "_full" not in d.name:
            continue
        return None if (d / paths.RUN_SUMMARY_JSON).exists() else d
    return None


def _hdp_folders(root: Path) -> list[Path]:
    out = paths.output_dir(HDP, "full", root)
    return sorted((d for d in out.iterdir() if d.is_dir()), reverse=True) if out.is_dir() else []


def _read_summary(folder: Path) -> dict:
    try:
        return json.loads((folder / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def passed_since(slug: str, root: Path, since: datetime) -> Path | None:
    """The run folder that already passed this month (resume: such a source is skipped), or None."""
    if slug == HDP:
        folders = _hdp_folders(root)
        if folders and _read_summary(folders[0]).get("status") == "PASS" and (_folder_started(folders[0]) or datetime.min) >= since:
            return folders[0]
        return None
    for c in mi.candidates(slug, root):
        if c.qualifies and (c.root_started or c.started) >= since:  # data of this month: a rebuild of old data does not count
            return c.run_dir
    return None


def _requests(summary: dict):
    r = summary.get("requests_made")
    if isinstance(r, dict):
        return sum(v for v in r.values() if isinstance(v, int))
    return r if isinstance(r, int) else None


def _previous_rows(root: Path, month: str) -> dict[str, int]:
    """Rows per source in the input manifest of the previous official master (the delta's baseline)."""
    prev = paths.latest_official_master(root, before_month=month)
    if prev is None or not (prev.parent / "input_manifest.csv").exists():
        return {}
    df = mi.read_source_csv(prev.parent / "input_manifest.csv")
    return {r["slug"]: int(r["rows"]) for r in df.to_dict("records") if str(r.get("rows", "")).isdigit()}


def folder_record(slug: str, folder: Path, root: Path, month: str) -> dict:
    """PASS/FAIL, in-scope count, delta, requests of one run folder (PASS = the master would accept it as input)."""
    s = _read_summary(folder)
    rec = {"slug": slug, "label": label(slug), "run_folder": relative(folder, root), "started_at": None, "status": "FAIL", "why": "",
           "in_scope": None, "delta": None, "requests": _requests(s), "duration_s": s.get("runtime_s"), "error": ""}
    started = _folder_started(folder)
    rec["started_at"] = started.isoformat(timespec="minutes") if started else None
    if slug == HDP:
        ok = s.get("status") == "PASS"
        rec.update(status="PASS" if ok else "FAIL", in_scope=s.get("n_sites"),
                   why="PASS" if ok else (s.get("error") or "; ".join(s.get("failed_hard") or []) or "no run_summary.json"))
        return rec
    c = next((x for x in mi.candidates(slug, root) if x.run_dir.name == folder.name), None)
    if c is None:
        rec["why"] = "not a full/rebuild run folder in output/"
        return rec
    rec["why"] = c.why
    if c.qualifies:
        rec["status"] = "PASS"
        rec["in_scope"] = len(mi.read_source_csv(c.file))
        prev = _previous_rows(root, month).get(slug)
        rec["delta"] = None if prev is None else rec["in_scope"] - prev
    else:
        rec["in_scope"] = mi.summary_in_scope(c.summary)
    return rec


# ---- running one source -------------------------------------------------------------------------------------------
def gtue_key_present(root: Path) -> bool:
    """Presence only: the value is never kept, printed or written."""
    env = os.environ.get(GTUE_KEY) or ""
    if not env.strip() and (root / ".env").exists():
        env = dotenv_values(root / ".env").get(GTUE_KEY) or ""
    return bool(env.strip())


def _child(fn, mode, root, started, centroids, conn):
    try:
        fn(mode, root, started, centroids)
        conn.send("")
    except BaseException as exc:  # the parent records it; nothing may escape silently
        conn.send(f"{type(exc).__name__}: {exc}"[:500])
    finally:
        conn.close()


def _execute(ctx: Context, slug: str, fn, mode: str, started: datetime) -> str:
    """Run one source; returns the error text ('' if it returned normally)."""
    if ctx.isolation == "inline":
        try:
            fn(mode, ctx.root, started, ctx.centroids)
            return ""
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"[:500]
    timeout = ctx.timeouts.get(slug, TIMEOUTS.get(slug, DEFAULT_TIMEOUT))
    spawn = mp.get_context("spawn")
    recv, send = spawn.Pipe(duplex=False)
    p = spawn.Process(target=_child, args=(fn, mode, ctx.root, started, ctx.centroids, send), name=f"monthly-{slug}")
    p.start()
    send.close()
    p.join(timeout)
    if p.is_alive():
        p.terminate()
        p.join(30)
        return f"timeout: stopped after {timeout:.0f} s"
    try:
        return recv.recv() if recv.poll() else f"the source process ended without a result (exit code {p.exitcode})"
    except EOFError:
        return f"the source process ended without a result (exit code {p.exitcode})"
    finally:
        recv.close()


def stage_simulated(slug: str, folder: Path, root: Path) -> Path:
    """Copy a simulated run into output/ as `<stamp>_full_simulated` so the master selects it (sandbox roots only)."""
    dest = paths.output_dir(slug, "full", root) / folder.name.replace("_simulated", "_full_simulated", 1)
    shutil.copytree(folder, dest)
    f = dest / paths.RUN_SUMMARY_JSON
    if f.exists():
        s = json.loads(f.read_text(encoding="utf-8"))
        s.update(mode="full", simulated=True, staged_from=relative(folder, root))
        f.write_text(json.dumps(s, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return dest


def run_source(ctx: Context, slug: str, month: str) -> dict:
    """One source, isolated: own run folder, own error, own timeout. Never raises for a source failure."""
    mode = "simulated" if ctx.simulated else "full"
    out = paths.output_dir(slug, mode, ctx.root)
    if slug == "gtue" and not ctx.simulated and not gtue_key_present(ctx.root):
        rec = {"slug": slug, "label": label(slug), "run_folder": None, "started_at": None, "status": "FAIL", "why": "",
               "in_scope": None, "delta": None, "requests": 0, "duration_s": 0,
               "error": f"{GTUE_KEY} is not set (locally: tools\\with-secrets.ps1 {GTUE_KEY} ...; in Actions: the repository secret)",
               "retryable": False}
        ctx.say(f"{slug}: FAIL {rec['error']}")
        return rec
    before = {d.name for d in out.iterdir()} if out.is_dir() else set()
    started = ctx.now()
    ctx.say(f"{slug}: {mode} run started")
    t0 = time.monotonic()
    error = _execute(ctx, slug, ctx.runners.get(slug, RUNNERS[slug]), mode, started)
    duration = round(time.monotonic() - t0, 1)
    new = sorted(n for n in ({d.name for d in out.iterdir()} if out.is_dir() else set()) - before)
    if not new:
        rec = {"slug": slug, "label": label(slug), "run_folder": None, "started_at": started.isoformat(timespec="minutes"), "status": "FAIL",
               "why": "no run folder written", "in_scope": None, "delta": None, "requests": None, "duration_s": duration,
               "error": error or "no run folder written"}
    else:
        folder = out / new[-1]
        if ctx.simulated:
            folder = stage_simulated(slug, folder, ctx.root)
        rec = folder_record(slug, folder, ctx.root, month)
        rec.update(duration_s=duration, error=error)
    rec.setdefault("retryable", True)
    delta = "" if rec["delta"] is None else f" ({rec['delta']:+d})"
    ctx.say(f"{slug}: {rec['status']} {rec['in_scope'] if rec['in_scope'] is not None else '-'} in scope{delta}, "
            f"{rec['requests'] if rec['requests'] is not None else '-'} requests, {duration:.0f} s"
            + (f"; {rec['error'] or rec['why']}" if rec["status"] != "PASS" else ""))
    return rec


# ---- status file --------------------------------------------------------------------------------------------------
def new_status(month: str, ctx: Context) -> dict:
    return {"month": month, "cycle_start": cycle_start(ctx, month).date().isoformat(), "simulated": ctx.simulated, "phase": "not_started",
            "exit_code": None, "next_action": "", "needs_human": [], "updated_at": None,
            "scrape": {"started_at": None, "finished_at": None, "sources": {}}, "tuev_sued": {}, "build": {}}


def load_status(month: str, root: Path) -> dict | None:
    f = paths.monthly_dir(month, root) / paths.MONTHLY_STATUS_JSON
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@contextmanager
def _lock(path: Path, wait_s: float, stale_s: float):
    """An exclusive lock file (O_EXCL). Yields True if taken, False if someone else holds it after `wait_s`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    fd = None
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_s:
                    path.unlink(missing_ok=True)  # left over from a crash
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.05)
    try:
        os.write(fd, f"pid {os.getpid()} since {datetime.now().isoformat(timespec='seconds')}\n".encode())
        os.close(fd)
        yield True
    finally:
        path.unlink(missing_ok=True)


def needs_human(s: dict) -> list[str]:
    out = []
    for r in s["scrape"]["sources"].values():
        if r["status"] != "PASS":
            out.append(f"{r['label']}: run failed, not refreshed ({r['error'] or r['why']})")
    t = s.get("tuev_sued") or {}
    if t.get("status") in ("FAIL", "BLOCKED", "IMPORT_ERROR"):
        out.append(f"TÜV SÜD: {t['status']} ({t.get('message', '')})")
    b = s.get("build") or {}
    if b.get("without_tuev_sued"):
        out.append("TÜV SÜD not refreshed: built without it, its rows stay active with last month's data")
    for x in b.get("not_refreshed") or []:
        if x["slug"] == TUEV_SUED and b.get("without_tuev_sued"):
            continue
        out.append(f"{label(x['slug'])}: not refreshed, the master used {x['run_folder']} ({x['age_days']} days old)")
    for c in b.get("failed_checks") or []:
        out.append(f"master check failed: {c}")
    for c in b.get("review_checks") or []:
        out.append(f"master review item: {c}")
    if b.get("status") == "STOPPED":
        out.append(f"build stopped: {b.get('reason', '')}")
    return list(dict.fromkeys(out))


def exit_code(s: dict) -> int:
    if (s.get("build") or {}).get("status") in ("FAIL", "STOPPED"):
        return 2
    return 1 if s["needs_human"] else 0


def render_md(s: dict) -> str:
    L = [f"# Monthly run {s['month']}{' (simulated)' if s.get('simulated') else ''}: {s['phase']} (exit {s['exit_code']})", "",
         f"Updated {s['updated_at']}. Cycle start (TÜV SÜD reminder day): {s['cycle_start']}.", ""]
    if s.get("next_action"):
        L += [f"**Next:** {s['next_action']}", ""]
    if s["needs_human"]:
        L += ["**Needs a human**", *[f"- {x}" for x in s["needs_human"]], ""]
    sc = s["scrape"]
    L += ["## Sources", f"Scrape started {sc['started_at'] or '-'}, finished {sc['finished_at'] or '-'}.", "",
          "| source | status | in scope | delta | requests | duration | run folder | note |", "|---|---|---|---|---|---|---|---|"]
    for r in sc["sources"].values():
        note = (r["error"] or r["why"]) if r["status"] != "PASS" else ("skipped: passed earlier this month" if r.get("skipped") else "")
        if r.get("attempts", 1) > 1:
            note = f"attempt {r['attempts']}; " + note
        cells = [r["label"], r["status"], r["in_scope"], None if r["delta"] is None else f"{r['delta']:+d}", r["requests"],
                 None if r["duration_s"] is None else f"{r['duration_s']:.0f} s", r["run_folder"], note]
        L.append("| " + " | ".join("" if c is None else str(c) for c in cells) + " |")
    t = s.get("tuev_sued") or {}
    L += ["", "## TÜV SÜD", f"- status: {t.get('status', 'nothing yet')}; run folder: {t.get('run_folder') or '-'}"]
    if t.get("snippet"):
        L.append(f"- snippet to run in the browser: {t['snippet']} (pass {t.get('pass_no')})")
    if t.get("message"):
        L.append(f"- {t['message']}")
    b = s.get("build") or {}
    L += ["", "## Master", f"- status: {b.get('status', 'not built')}" + (f" ({b['reason']})" if b.get("reason") else "")]
    for k in ("master_dir", "hubspot_changes", "hubspot_changes_overview", "hubspot_import", "hubspot_excluded", "master_summary"):
        if b.get(k):
            L.append(f"- {k}: {b[k]}")
    if b.get("changes"):
        L.append(f"- changes: {b['changes']}")
    if b.get("pins"):
        L.append(f"- pinned to the last passing run: {b['pins']}")
    return "\n".join(L) + "\n"


def update_status(ctx: Context, month: str, fn: Callable[[dict], None], *, write: bool = True) -> dict:
    """Read, change, recompute needs_human / exit_code, write atomically (one writer at a time). write=False touches nothing."""

    def change() -> dict:
        s = load_status(month, ctx.root) or new_status(month, ctx)
        fn(s)
        s["needs_human"] = needs_human(s)
        s["exit_code"] = exit_code(s)
        s["updated_at"] = ctx.now().isoformat(timespec="seconds")
        return s

    if not write:
        return change()
    d = paths.monthly_dir(month, ctx.root)
    with _lock(d / STATUS_LOCK, STATUS_LOCK_WAIT_S, STATUS_LOCK_WAIT_S) as ok:
        if not ok:
            raise RuntimeError(f"the status file of {month} stayed locked for {STATUS_LOCK_WAIT_S} s")
        s = change()
        for name, text in ((paths.MONTHLY_STATUS_JSON, json.dumps(s, ensure_ascii=False, indent=2, default=str)),
                           (paths.MONTHLY_STATUS_MD, render_md(s))):
            tmp = d / (name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, d / name)
    return s


def _set(ctx: Context, month: str, **sections) -> dict:
    def fn(s):
        for k, v in sections.items():
            if k == "source":
                s["scrape"]["sources"][v["slug"]] = v
            elif k == "scrape":
                s["scrape"].update(v)
            else:
                s[k] = v
    return update_status(ctx, month, fn)


# ---- build --------------------------------------------------------------------------------------------------------
def _master_dir_of(s: dict | None, ctx: Context, month: str) -> Path | None:
    if not ctx.simulated:
        d = paths.master_output_dir(month, ctx.root)
        return d if d.exists() else None
    b = (s or {}).get("build") or {}
    return ctx.root / b["master_dir"] if b.get("master_dir") and (ctx.root / b["master_dir"]).exists() else None


def build_record(out_dir: Path, summary: dict, root: Path) -> dict:
    """The master's result as the status file shows it."""
    files = {}
    month = summary.get("month", "")
    for key in ("hubspot_changes", "hubspot_changes_overview", "hubspot_import", "hubspot_excluded"):
        for tag in ("", "_unverified"):  # the names hubspot_export.files_for_run gives them
            if (out_dir / f"{key}{tag}_{month}.csv").exists():
                files[key] = relative(out_dir / f"{key}{tag}_{month}.csv", root)
    checks = summary.get("checks") or []
    lc = summary.get("lifecycle") or {}
    return {"status": summary.get("status", "FAIL"), "master_dir": relative(out_dir, root),
            "master_summary": relative(out_dir / "master_summary.md", root), **files,
            "failed_checks": [f"{c['check']}: {c['detail']}" for c in checks if not c["ok"] and c["severity"] == "hard"],
            "review_checks": [f"{c['check']}: {c['detail']}" for c in checks if not c["ok"] and c["severity"] == "review"],
            "changes": summary.get("diff"), "master_rows": summary.get("master_rows_total"),
            "lifecycle": {"new": lc.get("new"), "retired": len(lc.get("retired") or []), "reactivated": len(lc.get("reactivated") or []),
                          "kept_active_not_refreshed": len(lc.get("not_refreshed") or [])},
            "error": summary.get("error", "")}


def plan_inputs(root: Path, month: str, now: datetime, since_tuev: datetime) -> tuple[dict, list[dict], list[str]]:
    """(pins, not_refreshed, problems) for the master. A source whose newest run was rejected is pinned to its newest qualifying
    run, mirroring the master's own `newer_not_qualifying` rule, but only if that run is at most MAX_INPUT_AGE_DAYS old."""
    pins, stale, problems = {}, [], []
    start = month_start(month)
    for slug in mi.SOURCE_ORDER:
        cands = mi.candidates(slug, root)
        ok = [c for c in cands if c.qualifies]
        if not ok:
            problems.append(f"{label(slug)}: no qualifying run in output/")
            continue
        best = ok[0]
        data = best.root_started or best.started
        age = (now - data).days
        if any(c.started > data and not c.qualifies for c in cands):
            if age > MAX_INPUT_AGE_DAYS:
                problems.append(f"{label(slug)}: newest run failed and the last passing run {best.run_dir.name} is {age} days old "
                                f"(limit {MAX_INPUT_AGE_DAYS})")
                continue
            pins[slug] = best.run_dir.name
        if data < (since_tuev if slug == TUEV_SUED else start):
            stale.append({"slug": slug, "run_folder": best.run_dir.name, "age_days": age})
    return pins, stale, problems


def ensure_hdp(ctx: Context, month: str, now: datetime) -> str:
    """'' if the HDP input is usable; otherwise HDP is rerun once (1 request) and the reason is returned if it still fails."""
    try:
        hdp_removal.load_hdp_input(ctx.root, now)
        return ""
    except hdp_removal.HdpInputError as exc:
        ctx.say(f"HDP input not usable ({exc}); rerunning HDP once")
    rec = run_source(ctx, HDP, month)
    rec["attempts"] = (((load_status(month, ctx.root) or {}).get("scrape", {}).get("sources", {}).get(HDP) or {}).get("attempts", 1) + 1)
    _set(ctx, month, source=rec)
    try:
        hdp_removal.load_hdp_input(ctx.root, ctx.now())
        return ""
    except hdp_removal.HdpInputError as exc:
        return f"HDP own-site input: {exc}"


def _build(ctx: Context, month: str, without_tuev_sued: bool = False, accept_retirements: bool = False) -> Outcome:
    """Build the master if it is not built yet and its inputs are ready. Called by every command."""
    root = ctx.root
    s = load_status(month, root)
    done = _master_dir_of(s, ctx, month)
    if done is not None:
        b = (s or {}).get("build") or {}
        if b.get("status") == "FAIL" or (not ctx.simulated and (done / f"pruefstellen_master_unverified_{month}.csv").exists()):
            msg = f"{relative(done, root)} exists and FAILED: a human decides; to rebuild, move it to 9_archive/ and run build again"
            ctx.say(msg)
            return Outcome(2, s, msg)
        ctx.say(f"master of {month} already built ({relative(done, root)}): nothing to do")
        return Outcome(0, s, "already built")
    with _lock(paths.monthly_dir(month, root) / BUILD_LOCK, 0, BUILD_LOCK_STALE_S) as ok:
        if not ok:
            ctx.say("another build of this month is running: nothing to do")
            return Outcome(0, load_status(month, root), "build running elsewhere")
        if _master_dir_of(load_status(month, root), ctx, month) is not None:  # finished while we waited for the lock
            ctx.say("master was built in the meantime: nothing to do")
            return Outcome(0, load_status(month, root), "already built")
        now = ctx.now()
        if month_of(now) != month:
            s = _set(ctx, month, phase="stopped", build={"status": "STOPPED", "reason": f"an official master is built in its own month, not on {now:%Y-%m-%d}"})
            return Outcome(2, s)
        since_tuev = cycle_start(ctx, month)
        fresh = fresh_tuev_sued(root, since_tuev)
        if fresh is None and not without_tuev_sued:
            s = _set(ctx, month, phase="waiting_for_tuev_sued",
                     next_action="waiting for the TÜV SÜD upload; after the timeout n8n runs `build --without-tuev-sued`")
            ctx.say("no fresh TÜV SÜD run: the master waits for it")
            return Outcome(s["exit_code"], s, "waiting for TÜV SÜD")
        reason = ensure_hdp(ctx, month, now)
        pins, stale, problems = plan_inputs(root, month, now, since_tuev)
        if reason or problems:
            why = "; ".join([reason] * bool(reason) + problems)
            s = _set(ctx, month, phase="stopped", next_action="fix the input named in build.reason, then run build again",
                     build={"status": "STOPPED", "reason": why, "pins": pins, "not_refreshed": stale, "without_tuev_sued": without_tuev_sued})
            ctx.say(f"build stopped: {why}")
            return Outcome(2, s, why)
        _set(ctx, month, phase="build_running", next_action="")
        mode = "dev" if ctx.simulated else "official"
        kwargs = dict(ctx.master_kwargs)
        if ctx.simulated:
            kwargs.setdefault("live_geocoding", False)
            kwargs.setdefault("check_volume", False)
        ctx.say(f"master ({mode}) for {month}; pinned: {pins or 'none'}")
        try:
            res = master_run.run(mode, root=root, started_at=now, confirm=True, pins=pins, accept_retirements=accept_retirements, **kwargs)
        except Exception as exc:  # MasterRunError and anything unexpected: the status must say it
            s = _set(ctx, month, phase="stopped", build={"status": "STOPPED", "reason": f"{type(exc).__name__}: {exc}", "pins": pins,
                                                          "not_refreshed": stale, "without_tuev_sued": without_tuev_sued})
            return Outcome(2, s, str(exc))
        if res.out_dir is None:
            rec = {"status": "STOPPED", "reason": res.summary.get("error", "the master could not be built")}
        else:
            rec = build_record(res.out_dir, res.summary, root)
        rec.update(pins=pins, not_refreshed=stale, without_tuev_sued=without_tuev_sued, accept_retirements=accept_retirements,
                   tuev_sued_run=fresh.run_dir.name if fresh is not None else None)
        guard = any("mass retirement" in c for c in rec.get("failed_checks", []))
        nxt = ("Slack approval of hubspot_changes, then the HubSpot import" if rec["status"] == "PASS" else
               "mass-retirement guard tripped: a human decides; to accept, move the month folder to 9_archive/ and run "
               "build --accept-retirements" if guard else "a hard master check failed: see master_summary.md")
        s = _set(ctx, month, phase="built" if rec["status"] == "PASS" else "build_failed", next_action=nxt, build=rec)
        ctx.say(f"master {rec['status']}: {rec.get('master_dir', '')}")
        return Outcome(s["exit_code"], s)


# ---- commands -----------------------------------------------------------------------------------------------------
def _scrape_done(s: dict | None) -> bool:
    return bool(s and s["scrape"].get("finished_at"))


def describe_scrape(ctx: Context, force=()) -> str:
    now = ctx.now()
    month = month_of(now)
    since = month_start(month)
    L = [f"scrape {month} ({'simulated' if ctx.simulated else 'live'}): would run, one after the other:"]
    for slug in SCRAPE_ORDER:
        hit = None if slug in force else passed_since(slug, ctx.root, since)
        what = f"skip, passed this month ({hit.name})" if hit else f"run, {REQUESTS[slug]} requests, timeout {ctx.timeouts.get(slug, TIMEOUTS.get(slug, DEFAULT_TIMEOUT)) / 60:.0f} min"
        if slug == "gtue" and not hit and not ctx.simulated:
            what += f"; {GTUE_KEY} {'set' if gtue_key_present(ctx.root) else 'NOT SET (GTÜ would fail)'}"
        L.append(f"  {slug:16} {what}")
    fresh = fresh_tuev_sued(ctx.root, cycle_start(ctx, month))
    L.append(f"then: {'build the master (fresh TÜV SÜD run ' + fresh.run_dir.name + ')' if fresh else 'wait for TÜV SÜD (no fresh run since ' + cycle_start(ctx, month).date().isoformat() + ')'}")
    L.append("Nothing was sent. Add --confirm to run.")
    return "\n".join(L)


def scrape(ctx: Context, *, confirm: bool = False, force=()) -> Outcome:
    """Run HDP and every scraped source; then build if TÜV SÜD is fresh, else wait for it."""
    unknown = set(force) - set(SCRAPE_ORDER)
    if unknown:
        raise ValueError(f"--force: unknown source(s) {sorted(unknown)}; choose from {SCRAPE_ORDER}")
    if not confirm:
        ctx.say(describe_scrape(ctx, force))
        return Outcome(0, None, "plan only")
    month = month_of(ctx.now())
    since = month_start(month)
    _set(ctx, month, phase="scrape_running", next_action="", scrape={"started_at": ctx.now().isoformat(timespec="seconds"), "finished_at": None})
    failed = []
    for slug in SCRAPE_ORDER:
        hit = None if slug in force else passed_since(slug, ctx.root, since)
        if hit is not None:
            old = ((load_status(month, ctx.root) or {}).get("scrape", {}).get("sources", {}).get(slug)) or {}
            rec = old if old.get("run_folder") == relative(hit, ctx.root) else folder_record(slug, hit, ctx.root, month)
            rec["skipped"] = True
            ctx.say(f"{slug}: skipped, passed this month ({hit.name})")
        else:
            rec = run_source(ctx, slug, month)
            rec["attempts"] = 1
            if rec["status"] != "PASS" and rec.get("retryable", True):
                failed.append(slug)
        _set(ctx, month, source=rec)
    for slug in failed:  # one retry at the end (the plan's "retry after 1 h", without paying for an idle runner)
        ctx.say(f"{slug}: retry")
        rec = run_source(ctx, slug, month)
        rec["attempts"] = 2
        _set(ctx, month, source=rec)
    s = _set(ctx, month, scrape={"finished_at": ctx.now().isoformat(timespec="seconds")})
    if fresh_tuev_sued(ctx.root, cycle_start(ctx, month)) is not None:
        return _build(ctx, month)
    s = _set(ctx, month, phase="waiting_for_tuev_sued",
             next_action="waiting for the TÜV SÜD upload; after the timeout n8n runs `build --without-tuev-sued`")
    ctx.say("scrape done; waiting for TÜV SÜD")
    return Outcome(s["exit_code"], s, "waiting for TÜV SÜD")


def _tuev_record(folder: Path, root: Path, status: str, message: str, plan=None) -> dict:
    rec = {"run_folder": relative(folder, root), "status": status, "message": message, "snippet": None, "pass_no": None}
    if plan is not None:
        rec.update(snippet=relative(plan.js_path, root), pass_no=plan.pass_no, queries=len(plan.queries))
    return rec


def _after_tuev_build(ctx: Context, month: str, folder: Path) -> Outcome:
    """Record the built TÜV SÜD run; build the master if it passed and this month's scrape is done."""
    summary = _read_summary(folder)
    ok = summary.get("status") == "PASS"
    rec = _tuev_record(folder, ctx.root, "PASS" if ok else "FAIL", summary.get("outcome", ""))
    rec["in_scope"] = summary.get("in_scope")
    _set(ctx, month, tuev_sued=rec)
    if not ok:
        s = _set(ctx, month, next_action="the TÜV SÜD run did not pass: plan a new run or wait for the timeout")
        return Outcome(s["exit_code"], s)
    if _scrape_done(load_status(month, ctx.root)):
        return _build(ctx, month)
    s = _set(ctx, month, phase="waiting_for_scrape", next_action="TÜV SÜD is ready; the scrape on the 1st builds the master")
    return Outcome(s["exit_code"], s)


def describe_tuev_sued(ctx: Context, action: str, month: str) -> str:
    since = cycle_start(ctx, month)
    open_run = open_tuev_sued_run(ctx.root, since)
    fresh = fresh_tuev_sued(ctx.root, since)
    L = [f"tuev-sued {action} for the {month} master (cycle start {since.date().isoformat()}); Python sends no request to TÜV SÜD.",
         f"  fresh TÜV SÜD run: {fresh.run_dir.name if fresh else 'none'}; open run: {open_run.name if open_run else 'none'}"]
    L.append({"plan": "  would " + ("show the pending snippet of " + open_run.name if open_run else "open a new full run and write the pass-1 snippet"),
              "check": "  would import the file, prove coverage, then build the run (PASS) or write the next snippet",
              "build": "  would build the open run as it is (labelled exports if coverage has gaps)"}[action])
    L.append("Nothing was written. Add --confirm to run.")
    return "\n".join(L)


def tuev_sued(ctx: Context, action: str, *, confirm: bool = False, file: str | Path | None = None, month: str | None = None) -> Outcome:
    """The headless TÜV SÜD steps around tuev_sued.run: plan -> snippet, check an upload, build the run; then the master."""
    from tuev_sued import browser
    from tuev_sued import run as tuev_run

    if action not in ("plan", "check", "build"):
        raise ValueError("action must be plan, check or build")
    month = month or cycle_month(ctx.now().date())
    if not confirm:
        ctx.say(describe_tuev_sued(ctx, action, month))
        return Outcome(0, None, "plan only")
    since = cycle_start(ctx, month)
    folder = open_tuev_sued_run(ctx.root, since)
    if action == "plan":
        if folder is None:
            p = tuev_run.plan("full", root=ctx.root, started_at=ctx.now(), centroids=ctx.centroids)
            folder = p.run_folder
        else:
            pending = [n for n in browser.passes_planned(folder) if n not in browser.passes_imported(folder)]
            p = browser.read_plan(folder / browser.pass_js_name(pending[0])) if pending else None
        if p is None:
            s = _set(ctx, month, tuev_sued=_tuev_record(folder, ctx.root, "READY_TO_BUILD", "every planned pass is imported: run `tuev-sued build`"))
            return Outcome(s["exit_code"], s)
        s = _set(ctx, month, phase="tuev_sued_waiting_for_upload", next_action="run the snippet in the browser and upload the .jsonl",
                 tuev_sued=_tuev_record(folder, ctx.root, "WAITING_FOR_UPLOAD", f"pass {p.pass_no}: {len(p.queries)} PLZ", p))
        ctx.say(f"TÜV SÜD snippet: {p.js_path}")
        return Outcome(s["exit_code"], s)
    if folder is None:
        s = _set(ctx, month, tuev_sued={"status": "IMPORT_ERROR", "run_folder": None, "message": "no open TÜV SÜD run of this cycle: run `tuev-sued plan` first",
                                       "snippet": None, "pass_no": None})
        return Outcome(s["exit_code"], s)
    if action == "build":
        tuev_run.build(folder, centroids=ctx.centroids, root=ctx.root, say=ctx.say)
        return _after_tuev_build(ctx, month, folder)
    if file is None:
        raise ValueError("tuev-sued check needs --file (the downloaded .jsonl)")
    try:
        c = tuev_run.check(folder, file, centroids=ctx.centroids, root=ctx.root, say=ctx.say)
    except (browser.ImportProblem, tuev_run.RunError, OSError) as exc:
        s = _set(ctx, month, tuev_sued=_tuev_record(folder, ctx.root, "IMPORT_ERROR", f"{type(exc).__name__}: {exc}"))
        return Outcome(s["exit_code"], s)
    ctx.say(c.message)
    if c.status == "PASS":
        tuev_run.build(folder, centroids=ctx.centroids, root=ctx.root, say=ctx.say)
        return _after_tuev_build(ctx, month, folder)
    if c.status == "INCOMPLETE" and c.next_plan is not None:
        s = _set(ctx, month, phase="tuev_sued_next_pass", next_action="post the next snippet in the thread; Nikos runs it and uploads the .jsonl",
                 tuev_sued=_tuev_record(folder, ctx.root, "NEXT_PASS", c.message, c.next_plan))
        return Outcome(s["exit_code"], s)
    if c.status == "FAIL":  # passes used up: the run is built with labelled exports and does not feed the master
        tuev_run.build(folder, centroids=ctx.centroids, root=ctx.root, say=ctx.say)
        return _after_tuev_build(ctx, month, folder)
    s = _set(ctx, month, tuev_sued=_tuev_record(folder, ctx.root, c.status, c.message),
             next_action="TÜV SÜD refused the browser requests: stop, nothing is retried automatically")
    return Outcome(s["exit_code"], s)


def tuev_sued_simulated(ctx: Context) -> Outcome:
    """Simulated only (notebook, tests): the whole TÜV SÜD flow offline, staged into output/, then the master if the scrape is done."""
    from tuev_sued import run as tuev_run

    if not ctx.simulated:
        raise ValueError("tuev_sued_simulated needs a simulated context")
    month = cycle_month(ctx.now().date())
    built = tuev_run.run("simulated", root=ctx.root, started_at=ctx.now(), centroids=ctx.centroids, verbose=False)
    return _after_tuev_build(ctx, month, stage_simulated(TUEV_SUED, Path(built.run_folder), ctx.root))


def describe_build(ctx: Context, without_tuev_sued: bool) -> str:
    now = ctx.now()
    month = month_of(now)
    L = [f"build {month} ({'dev, simulated' if ctx.simulated else 'official'}):"]
    done = _master_dir_of(load_status(month, ctx.root), ctx, month)
    if done is not None:
        L.append(f"  master already exists: {relative(done, ctx.root)}; nothing would happen")
    try:
        h = hdp_removal.load_hdp_input(ctx.root, now)
        L.append(f"  HDP: {h.run_folder} usable ({h.age_days} days old)")
    except hdp_removal.HdpInputError as exc:
        L.append(f"  HDP: not usable ({exc}); would rerun HDP once ({'simulated' if ctx.simulated else '1 request'})")
    since = cycle_start(ctx, month)
    fresh = fresh_tuev_sued(ctx.root, since)
    L.append(f"  TÜV SÜD: {'fresh run ' + fresh.run_dir.name if fresh else 'no fresh run since ' + since.date().isoformat()}"
             + ("" if fresh or without_tuev_sued else "; the build would wait"))
    pins, stale, problems = plan_inputs(ctx.root, month, now, since)
    L.append(f"  pinned to the last passing run: {pins or 'none'}")
    L.append(f"  not refreshed this month: {[x['slug'] for x in stale] or 'none'}")
    if problems:
        L.append(f"  would stop: {'; '.join(problems)}")
    L.append(f"  master geocoding: Nominatim, up to {master_run.geocode.REQUEST_CAP} live requests" if not ctx.simulated else "  master geocoding: offline")
    L.append("Nothing was sent. Add --confirm to run.")
    return "\n".join(L)


def build(ctx: Context, *, confirm: bool = False, without_tuev_sued: bool = False, accept_retirements: bool = False) -> Outcome:
    """The official master of this month (dev in a simulated context)."""
    if not confirm:
        ctx.say(describe_build(ctx, without_tuev_sued))
        return Outcome(0, None, "plan only")
    return _build(ctx, month_of(ctx.now()), without_tuev_sued, accept_retirements)


def status_from_disk(ctx: Context, month: str) -> dict:
    """The status rebuilt from the run folders (no requests). Keeps what only the file knows (errors without a folder, durations)."""
    old = load_status(month, ctx.root) or new_status(month, ctx)
    s = json.loads(json.dumps(old))
    since = month_start(month)
    for slug in SCRAPE_ORDER:
        out = paths.output_dir(slug, "full", ctx.root)  # simulated runs are staged there too
        folders = [d for d in (sorted(out.iterdir(), reverse=True) if out.is_dir() else []) if d.is_dir() and (_folder_started(d) or datetime.min) >= since]
        prev = s["scrape"]["sources"].get(slug) or {}
        if folders:
            rec = folder_record(slug, folders[0], ctx.root, month)
            if prev.get("run_folder") == rec["run_folder"]:
                rec.update({k: prev[k] for k in ("duration_s", "error", "attempts", "skipped") if k in prev})
            s["scrape"]["sources"][slug] = rec
    if not s["scrape"].get("finished_at") and all(slug in s["scrape"]["sources"] for slug in SCRAPE_ORDER):
        s["scrape"]["finished_at"] = "recovered from disk"
    csince = cycle_start(ctx, month)
    fresh = fresh_tuev_sued(ctx.root, csince)
    open_run = open_tuev_sued_run(ctx.root, csince)
    if fresh is not None:
        s["tuev_sued"] = {**_tuev_record(fresh.run_dir, ctx.root, "PASS", fresh.why), "in_scope": mi.summary_in_scope(fresh.summary)}
    elif open_run is not None and not s["tuev_sued"].get("run_folder"):
        s["tuev_sued"] = _tuev_record(open_run, ctx.root, "WAITING_FOR_UPLOAD", "open run found on disk")
    done = _master_dir_of(s, ctx, month)
    if done is not None and (done / "master_summary.json").exists():
        b = s.get("build") or {}
        s["build"] = {**b, **build_record(done, json.loads((done / "master_summary.json").read_text(encoding="utf-8")), ctx.root)}
        s["phase"] = "built" if s["build"]["status"] == "PASS" else "build_failed"
    elif (paths.monthly_dir(month, ctx.root) / BUILD_LOCK).exists():
        s["phase"] = "build_running"
    elif s["scrape"].get("finished_at"):
        s["phase"] = "waiting_for_tuev_sued" if fresh is None else "ready_to_build"
    elif s["scrape"]["sources"]:
        s["phase"] = "scrape_incomplete"
    return s


def status(ctx: Context, *, confirm: bool = False, month: str | None = None) -> Outcome:
    """Rebuild the status from disk; --confirm writes it, without it is only printed."""
    month = month or cycle_month(ctx.now().date())
    disk = status_from_disk(ctx, month)

    def fn(s):
        s.clear()
        s.update(disk)

    s = update_status(ctx, month, fn, write=confirm)
    ctx.say(render_md(s))
    if not confirm:
        ctx.say("Not written. Add --confirm to rewrite the status file.")
    return Outcome(s["exit_code"], s)


def folders(ctx: Context, month: str | None = None) -> list[Path]:
    """The status folder, every run folder and the master folder of a month (for the notebook's "open" cell)."""
    month = month or cycle_month(ctx.now().date())
    s = load_status(month, ctx.root) or new_status(month, ctx)
    out = [paths.monthly_dir(month, ctx.root)]
    out += [ctx.root / r["run_folder"] for r in s["scrape"]["sources"].values() if r.get("run_folder")]
    out += [ctx.root / x[k] for x, k in ((s.get("tuev_sued") or {}, "run_folder"), (s.get("build") or {}, "master_dir")) if x.get(k)]
    return [f for f in out if f.exists()]


# ---- command line -------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.monthly", description="Monthly run of the Prüfstellen pipeline (see the module docstring).")
    sub = ap.add_subparsers(dest="command", required=True)
    p_scrape = sub.add_parser("scrape", help="run HDP and every scraped source, then build if TÜV SÜD is fresh")
    p_scrape.add_argument("--force", nargs="+", default=[], metavar="SLUG", help="rerun these sources even if they passed this month")
    p_tuev = sub.add_parser("tuev-sued", help="TÜV SÜD: plan (snippet), check an upload, build the run")
    p_tuev.add_argument("action", choices=["plan", "check", "build"])
    p_tuev.add_argument("--file", help="check: the downloaded .jsonl")
    p_build = sub.add_parser("build", help="the official master of this month")
    p_build.add_argument("--without-tuev-sued", action="store_true", help="timeout: build with last month's TÜV SÜD data")
    p_build.add_argument("--accept-retirements", action="store_true", help="a human decided: the mass-retirement check becomes a review item")
    p_status = sub.add_parser("status", help="rebuild the status file from disk (no requests)")
    p_status.add_argument("--month", help="YYYY-MM (default: the current cycle)")
    for p in (p_scrape, p_tuev, p_build, p_status):
        p.add_argument("--confirm", action="store_true", help="really run (without it: print the plan only)")
        p.add_argument("--cycle-start", type=date.fromisoformat, help="the TÜV SÜD reminder day of this cycle, YYYY-MM-DD")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    ctx = Context(cycle_start=a.cycle_start)
    if a.command == "scrape":
        res = scrape(ctx, confirm=a.confirm, force=a.force)
    elif a.command == "tuev-sued":
        res = tuev_sued(ctx, a.action, confirm=a.confirm, file=a.file)
    elif a.command == "build":
        res = build(ctx, confirm=a.confirm, without_tuev_sued=a.without_tuev_sued, accept_retirements=a.accept_retirements)
    else:
        res = status(ctx, confirm=a.confirm, month=a.month)
    if res.status is not None and a.confirm:
        say(f"status: {paths.monthly_dir(res.status['month'], ctx.root) / paths.MONTHLY_STATUS_JSON}  phase {res.status['phase']}, exit {res.code}")
    return res.code


if __name__ == "__main__":
    raise SystemExit(main())
