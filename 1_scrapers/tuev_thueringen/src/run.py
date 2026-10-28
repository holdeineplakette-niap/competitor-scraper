"""TÜV Thüringen run: the store group "Prüfanlagen" plus its cross-check URL, checked, one folder per run (headless; for core.monthly).

Modes (see 0_instructions/storage_and_naming_convention.md):
  simulated  offline, fake tuev-thueringen.de with known truth, must PASS (faults can be injected in tests)  -> test_output/
  full       live: main page + cross-check URL, 2 requests 2-4 s apart; needs confirm_full_run=True           -> output/
  rebuild    offline: re-runs everything on the raw files of an earlier run folder (source_runs)             -> output/

Command line (from the project folder, Windows):
  .venv\\Scripts\\python.exe -m tuev_thueringen.run simulated
  .venv\\Scripts\\python.exe -m tuev_thueringen.run full --confirm
  .venv\\Scripts\\python.exe -m tuev_thueringen.run rebuild --source 1_scrapers\\tuev_thueringen\\output\\<run folder>

PASS needs every check of adapter.check to hold: the three page views list the same stores, the cross-check URL lists
them too, ids are unique, every row has street, PLZ, city and German coordinates, and the count has not dropped more than
10 % against the last passing CSV. A blocked page or a page without the store table stops the run; a cross-check page
that cannot be fetched is a failed check. A failed run writes its CSV with `unverified` in the name. The Service-Center /
Kunden-Center scan (report only) stays in the control notebook and is not part of this run.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from core import export as core_export
from core import geo, paths
from core.export import relative

from tuev_thueringen import adapter
from tuev_thueringen.adapter import FetchError

SLUG = adapter.SLUG
RUN_MODES = ("simulated", "full", "rebuild")
RAW_FILES = {"main": ("main.html", "page.html"), "cross": ("cross.html",)}  # page.html: the 2026-10-06 sample copy
KNOWN_GAPS = ["HU at workshop PSPs and outside Thüringen is not listed; the Prüfanlagen group holds TÜV Thüringen's own stations only."]
LOG_COLUMNS = ["time", "label", "url", "status", "bytes", "note"]


@dataclass
class RunResult:
    run_dir: Path
    mode: str
    outcome: str
    frame: pd.DataFrame
    summary: dict
    written: dict


def say(msg: str) -> None:
    print(msg, flush=True)


def check(name: str, ok: bool, detail: str = "", severity: str = "hard") -> dict:
    return {"check": name, "ok": bool(ok), "detail": str(detail), "severity": severity}


# ------------------------------------------------------------------------------------------------ clients
class LiveClient:
    """tuev-thueringen.de through adapter.fetch (2-4 s pause, no retry around a block)."""

    def __init__(self):
        self.requests_made = 0

    def get(self, url: str) -> str:
        self.requests_made += 1
        return adapter.fetch(url)


class OfflineClient:
    """Serves the raw files of an earlier run folder (raw/ or the folder itself, as in the 2026-10-06 sample copy)."""

    requests_made = 0

    def __init__(self, source_runs):
        self.dirs = [Path(d) for d in source_runs]

    def get(self, url: str) -> str:
        key = "cross" if url == adapter.CROSSCHECK_URL else "main"
        for d in self.dirs:
            for name in RAW_FILES[key]:
                for f in (d / "raw" / name, d / name):
                    if f.exists():
                        return f.read_text(encoding="utf-8")
        raise FetchError(f"{' / '.join(RAW_FILES[key])} not in the source run(s) {[str(d) for d in self.dirs]}")


class RunLog:
    def __init__(self, path: Path | None):
        self.path = path

    def add(self, **row) -> None:
        if self.path:
            new = not self.path.exists()
            with open(self.path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, delimiter=";")
                if new:
                    w.writeheader()
                w.writerow({k: row.get(k, "") for k in LOG_COLUMNS})


# ------------------------------------------------------------------------------------------------ run
def run(mode: str = "simulated", confirm_full_run: bool = False, root=None, now: datetime | None = None, client=None,
        centroids=None, source_runs=None, save_raw: bool = True) -> RunResult:
    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {RUN_MODES}, got {mode!r}")
    if mode == "full" and not confirm_full_run:
        raise RuntimeError("A full run sends 2 live requests to tuev-thueringen.de (store group + cross-check URL). "
                           "Pass confirm_full_run=True to start it.")
    if mode == "rebuild" and not source_runs:
        raise ValueError("mode 'rebuild' needs source_runs: the run folder(s) that hold the raw files")
    if client is None:
        if mode == "simulated":
            from tuev_thueringen.simulate import SimulatedThueringen
            client = SimulatedThueringen()
        else:
            client = OfflineClient(source_runs) if mode == "rebuild" else LiveClient()
    started = now or datetime.now()
    run_dir = paths.new_run_dir(SLUG, mode, started, root)
    raw_dir = None
    if save_raw and mode != "rebuild":
        raw_dir = run_dir / "raw"
        raw_dir.mkdir()
    date = f"{started:%Y-%m-%d}"
    scraped_at = started.isoformat(timespec="seconds")
    log = RunLog(run_dir / paths.QUERY_LOG_FILE if mode != "rebuild" else None)
    summary: dict = {
        "mode": mode, "status": "FAIL", "outcome": "FAIL", "run_date": date, "started": scraped_at,
        "run_dir": relative(run_dir, paths.project_root() if root is None else Path(root)),
        "source_runs": [str(s) for s in (source_runs or [])], "in_scope": None, "checks": [], "warnings": [], "counts": {},
        "stores": [], "known_gaps": KNOWN_GAPS, "error": "",
    }
    say(f"[{mode}] run folder: {run_dir}")
    df = pd.DataFrame()

    def fetch(label: str, url: str) -> str:
        try:
            html = client.get(url)
        except FetchError as exc:
            log.add(time=datetime.now().isoformat(timespec="seconds"), label=label, url=url, note=f"FAILED: {exc}")
            raise
        log.add(time=datetime.now().isoformat(timespec="seconds"), label=label, url=url, status=200, bytes=len(html.encode("utf-8")))
        if raw_dir:
            (raw_dir / RAW_FILES[label][0]).write_text(html, encoding="utf-8")
        return html

    try:
        # ---- store group page (a failure stops the run: there is nothing to export)
        say("Store group Prüfanlagen")
        main_html = fetch("main", adapter.MAIN_URL)
        # ---- cross-check URL (a failure is a failed check, not a stop)
        say("Cross-check URL")
        try:
            cross_ids = adapter.parse(fetch("cross", adapter.CROSSCHECK_URL))[1]["table"]
        except FetchError as exc:
            cross_ids = None
            summary["warnings"].append(f"cross-check URL not fetched: {exc}")
        if centroids is None:
            centroids = geo.load_plz_centroids(paths.geonames_dir() / "DE.txt")
        df, ids = adapter.build(main_html, scraped_at=scraped_at, centroids=centroids)

        # the baseline is the last real CSV in output/, which a fake source cannot be measured against
        baseline = None if mode == "simulated" else adapter.last_pass_count(paths.output_dir(SLUG, "full", root))
        all_checks = [check(n, ok, d) for n, ok, d in adapter.check(df, ids, cross_ids, baseline)]
        failed_hard = [c for c in all_checks if not c["ok"]]
        summary["checks"] = all_checks
        summary["failed_hard"] = [c["check"] for c in failed_hard]
        status = "FAIL" if failed_hard else "PASS"
        summary.update(status=status, outcome=status if not failed_hard else f"FAIL: {len(failed_hard)} hard check(s) failed",
                       in_scope=int(df["in_scope"].sum()) if len(df) else 0)
        flags = df["doubt_flags"].str.split(";").explode().replace("", pd.NA).dropna() if len(df) else pd.Series(dtype=str)
        summary["counts"] = {"stores": len(df), "ids_per_view": {k: len(v) for k, v in ids.items()},
                             "cross_check_ids": None if cross_ids is None else len(set(cross_ids)),
                             "in_scope": summary["in_scope"], "needs_review": int(df["needs_review"].sum()) if len(df) else 0,
                             "doubt_flags": flags.value_counts().to_dict()}
        summary["stores"] = [{"source_id": r.source_id, "name": r.name, "address": f"{r.street}, {r.zip} {r.city}",
                              "doubt_flags": r.doubt_flags} for r in df.itertuples()]
    except Exception as exc:  # a block, a page without the store table or a bug: the folder keeps what was fetched
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["outcome"] = f"FAIL: {summary['error']}"
        say(f"STOPPED: {summary['error']}")
        _finish(run_dir, summary, client)
        raise

    written = {}
    if len(df):
        out = core_export.export(df, run_dir, SLUG, run_date=date, tag=None if summary["status"] == "PASS" else "unverified",
                                 only=("pruefstellen_csv",))
        written = {k: (Path(p), n) for k, (p, n) in out.items()}
    summary["written"] = {k: {"file": v[0].name, "rows": v[1]} for k, v in written.items()}
    _finish(run_dir, summary, client)
    say(f"[{mode}] {summary['outcome']}: {summary['in_scope']} in scope -> {', '.join(v[0].name for v in written.values()) or 'no CSV'}")
    return RunResult(run_dir, mode, summary["status"], df, summary, written)


# ------------------------------------------------------------------------------------------------ summary files
def _finish(run_dir: Path, summary: dict, client) -> None:
    summary["requests_made"] = getattr(client, "requests_made", 0)
    (run_dir / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (run_dir / paths.RUN_SUMMARY_MD).write_text(render_markdown(summary), encoding="utf-8")


def _table(header: list[str], rows: list[list]) -> list[str]:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]


def render_markdown(s: dict) -> str:
    L = [f"# TÜV Thüringen run report: {s['status']}", "", f"- **Result:** {s['outcome']}",
         f"- **Mode / date:** {s['mode']} / {s['run_date']} (started {s['started']}) · requests: {s.get('requests_made', '-')}",
         f"- **Run folder:** `{s['run_dir']}`"]
    if s.get("source_runs"):
        L.append("- **Rebuilt offline from:** " + ", ".join(f"`{x}`" for x in s["source_runs"]))
    if s.get("error"):
        L += ["", f"**STOPPED: {s['error']}**", "", "Nothing here counts as a result."]
    for w in s.get("warnings", []):
        L += ["", f"**{w}**"]
    c = s.get("counts", {})
    if c:
        L += ["", f"**In scope: {c.get('in_scope')}** (stores per view {c.get('ids_per_view')}, cross-check {c.get('cross_check_ids')}) · "
              f"needs review {c.get('needs_review')} · doubt flags {c.get('doubt_flags')}"]
    L += ["", "## Checks", ""]
    L += _table(["ok", "check", "detail"], [["yes" if k["ok"] else "**NO**", k["check"], k["detail"]] for k in s.get("checks", [])])
    if s.get("failed_hard"):
        L += ["", "**Failed checks:** " + "; ".join(s["failed_hard"])]
    if s.get("stores"):
        L += ["", "## Stations", ""]
        L += _table(["name", "address", "doubt flags"], [[r["name"], r["address"], r["doubt_flags"]] for r in s["stores"]])
    L += ["", "## Known gaps", ""] + [f"- {g}" for g in s.get("known_gaps", [])]
    if s.get("written"):
        L += ["", "## Files", ""] + [f"- `{v['file']}`: {v['rows']} rows" for v in s["written"].values()]
        L += [f"- `{paths.QUERY_LOG_FILE}`, `{paths.RUN_SUMMARY_JSON}`, `{paths.RUN_SUMMARY_MD}`, `raw/` (store group and cross-check page)"]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tuev_thueringen.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=RUN_MODES)
    ap.add_argument("--confirm", action="store_true", help="needed for 'full': sends live requests")
    ap.add_argument("--source", nargs="+", help="rebuild: the run folder(s) that hold the raw files")
    args = ap.parse_args(argv)
    try:
        res = run(args.mode, confirm_full_run=args.confirm, source_runs=args.source)
    except FetchError as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        return 1
    except (RuntimeError, ValueError) as exc:  # e.g. 'full' without --confirm: nothing was sent
        print(f"\nNOT STARTED: {exc}")
        return 2
    print(f"\n{res.outcome}")
    print(f"run folder: {res.run_dir}")
    print(f"counts: {res.summary['counts']}")
    for k in res.summary["checks"]:
        if not k["ok"]:
            print(f"  [hard] {k['check']}: {k['detail']}")
    return 0 if res.outcome == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
