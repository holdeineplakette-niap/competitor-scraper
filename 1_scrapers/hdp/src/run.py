"""HDP run: one request to the own-site location API, one row per location id, one folder per run.

Modes (see 0_instructions/storage_and_naming_convention.md):
  simulated  offline, fake API (faults can be injected in tests)                                  -> test_output/
  full       live: ONE POST to getAllPublicLocations; needs confirm_full_run=True                  -> output/
  rebuild    offline: re-parses and re-checks the raw.jsonl of an earlier run folder (source_runs) -> output/

Command line (from the project folder, Windows):
  .venv\\Scripts\\python.exe -m hdp.run simulated
  .venv\\Scripts\\python.exe -m hdp.run full --confirm
  .venv\\Scripts\\python.exe -m hdp.run rebuild --source 1_scrapers\\hdp\\output\\<run folder>

The run FAILS (exit code 1, an ERROR line, the summary says why) if the request fails after its retries, result.success is not
true, the locations list is missing or empty, or any location lacks an id, a postcode or coordinates. The number of sites is
not a pass condition: a count that differs from the last passing full run is only a warning. The aggregator
(core.hdp_removal) refuses to run on a failed HDP run, so a failure here can never turn into "remove nothing".
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from core import paths
from core.export import baseline_run, relative, safe_path

from hdp import checks, parse
from hdp.fetch import URL, FetchError, HdpClient, Response
from hdp.simulate import SimulatedHdp

SLUG = parse.SLUG
RUN_MODES = ("simulated", "full", "rebuild")
LOG_COLUMNS = ["time", "label", "url", "status", "bytes", "elapsed_s", "attempts", "note"]
RAW_LABEL = "getAllPublicLocations"
logger = logging.getLogger("hdp")


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


class OfflineClient:
    """Serves the raw.jsonl of an earlier run folder with the client interface: re-parse without a request."""

    requests_made = 0

    def __init__(self, source_runs):
        self.dirs = [Path(d) for d in source_runs]

    def post_locations(self) -> Response:
        for d in self.dirs:
            f = d / paths.RAW_FILE
            if f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    entry = json.loads(line)
                    if entry.get("file") == RAW_LABEL:
                        return Response(url=URL, status=200, text=json.dumps(entry["response"], ensure_ascii=False), elapsed_s=0.0,
                                        attempts=1, content_type="application/json")
        raise FetchError(f"no {RAW_LABEL} response in {paths.RAW_FILE} of {[str(d) for d in self.dirs]}")


class RunLog:
    """query_log.csv: one line per request (rewritten after every request so a stopped run keeps it)."""

    def __init__(self, path: Path | None):
        self.path, self.rows = path, []

    def add(self, **kw) -> None:
        self.rows.append({k: kw.get(k, "") for k in LOG_COLUMNS})
        if self.path is None:
            return
        with self.path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=LOG_COLUMNS, delimiter=";")
            w.writeheader()
            w.writerows(self.rows)


def _source_data_started(source_runs) -> str | None:
    for d in source_runs or []:
        try:
            s = json.loads((Path(d) / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return s.get("data_started") or s.get("started")
    return None


def run(mode: str = "simulated", confirm_full_run: bool = False, root=None, now: datetime | None = None, client=None,
        source_runs=None) -> RunResult:
    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {RUN_MODES}, got {mode!r}")
    if mode == "full" and not confirm_full_run:
        raise RuntimeError("A full run sends 1 live request to the HDP location API. Pass confirm_full_run=True to start it.")
    if mode == "rebuild" and not source_runs:
        raise ValueError("mode 'rebuild' needs source_runs: the run folder that holds raw.jsonl")
    if client is None:
        client = {"simulated": SimulatedHdp, "rebuild": lambda: OfflineClient(source_runs)}.get(mode, HdpClient)()
    started = now or datetime.now()
    run_dir = paths.new_run_dir(SLUG, mode, started, root)
    date = f"{started:%Y-%m-%d}"
    scraped_at = started.isoformat(timespec="seconds")
    log = RunLog(run_dir / paths.QUERY_LOG_FILE if mode != "rebuild" else None)
    summary: dict = {
        "mode": mode, "status": "FAIL", "outcome": "FAIL", "run_date": date, "started": scraped_at,
        "data_started": (_source_data_started(source_runs) or scraped_at) if mode == "rebuild" else scraped_at,
        "run_dir": relative(run_dir, paths.project_root() if root is None else Path(root)),
        "source_runs": [str(s) for s in (source_runs or [])], "in_scope": None, "n_sites": None, "sites": [], "checks": [],
        "failed_hard": [], "warnings": [], "baseline": None, "written": {}, "error": "",
    }
    say(f"[{mode}] run folder: {run_dir}")
    frame = pd.DataFrame(columns=parse.COLUMNS)

    try:
        resp = client.post_locations()
    except FetchError as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        log.add(time=datetime.now().isoformat(timespec="seconds"), label=RAW_LABEL, url=URL, note=summary["error"])
        return _stop(run_dir, summary, client, mode, frame)
    log.add(time=datetime.now().isoformat(timespec="seconds"), label=RAW_LABEL, url=resp.url, status=resp.status,
            bytes=len(resp.text.encode("utf-8")), elapsed_s=resp.elapsed_s, attempts=resp.attempts)
    try:
        payload = parse.parse_payload(resp.text)
    except parse.ParseError as exc:
        summary["error"] = f"ParseError: {exc}"
        return _stop(run_dir, summary, client, mode, frame)
    if mode != "rebuild":  # raw before anything else is done with it
        with (run_dir / paths.RAW_FILE).open("w", encoding="utf-8") as f:
            f.write(json.dumps({"file": RAW_LABEL, "response": payload}, ensure_ascii=False) + "\n")

    all_checks = checks.response_checks(payload)
    success, locations = parse.envelope(payload)
    if success is True and isinstance(locations, list) and locations:  # locations of an answer that says "not successful" are never used
        all_checks += checks.location_checks(locations)
        frame = parse.to_frame(locations, scraped_at)
    base = baseline_run(paths.output_dir(SLUG, "full", root), exclude=run_dir) if mode in ("full", "rebuild") else None
    summary["baseline"] = base
    warning = checks.count_warning(len(frame), base) if len(frame) else None
    if warning:
        logger.warning(warning)
        summary["warnings"].append(warning)

    failed_hard = [c for c in all_checks if c["severity"] == "hard" and not c["ok"]]
    status = "FAIL" if failed_hard else "PASS"
    summary.update(status=status, outcome=status if not failed_hard else f"FAIL: {len(failed_hard)} hard check(s) failed",
                   checks=all_checks, failed_hard=[c["check"] for c in failed_hard], n_sites=len(frame),
                   in_scope=int(len(frame)) if len(frame) else None,
                   sites=[{k: (None if pd.isna(r[k]) else r[k]) for k in ("location_id", "source_id", "street", "zip", "city", "lat", "lng")}
                          for _, r in frame.iterrows()])
    logger.info("HDP sites found: %d", len(frame))
    say(f"HDP sites found: {len(frame)}")
    written = {}
    if len(frame):
        tag = "" if status == "PASS" else "unverified_"
        path = safe_path(run_dir / f"hdp_sites_{tag}{date}.csv")
        frame.to_csv(path, sep=";", encoding="utf-8-sig", index=False)
        written["sites_csv"] = (path, len(frame))
    summary["written"] = {k: {"file": v[0].name, "rows": v[1]} for k, v in written.items()}
    for c in failed_hard:
        logger.error("%s: %s", c["check"], c["detail"])
        say(f"ERROR: {c['check']}: {c['detail']}")
    _finish(run_dir, summary, client, mode)
    say(f"[{mode}] {summary['outcome']}: {len(frame)} site(s) -> {', '.join(v[0].name for v in written.values()) or 'no CSV'}")
    return RunResult(run_dir, mode, status, frame, summary, written)


def _stop(run_dir: Path, summary: dict, client, mode: str, frame: pd.DataFrame) -> RunResult:
    """The request failed or the answer was not JSON: the run FAILS, no CSV is written, and the summary says why."""
    summary["outcome"] = f"FAIL: {summary['error']}"
    logger.error(summary["error"])
    say(f"ERROR: {summary['error']}")
    _finish(run_dir, summary, client, mode)
    return RunResult(run_dir, mode, "FAIL", frame, summary, {})


def _finish(run_dir: Path, summary: dict, client, mode: str) -> None:
    summary["requests_made"] = getattr(client, "requests_made", 0)
    (run_dir / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (run_dir / paths.RUN_SUMMARY_MD).write_text(render_markdown(summary), encoding="utf-8")


def render_markdown(s: dict) -> str:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    L = [f"# HDP run report: {s['status']}", "", f"- **Result:** {s['outcome']}",
         f"- **Mode / date:** {s['mode']} / {s['run_date']} (started {s['started']}) · requests: {s.get('requests_made', '-')}",
         f"- **Run folder:** `{s['run_dir']}`", f"- **Sites found:** {s['n_sites'] if s['n_sites'] is not None else 'none'}"]
    if s.get("source_runs"):
        L.append("- **Rebuilt offline from:** " + ", ".join(f"`{x}`" for x in s["source_runs"]))
    if s.get("error"):
        L += ["", f"**STOPPED: {s['error']}**", "", "Nothing here counts as a result."]
    for w in s.get("warnings", []):
        L += ["", f"**Warning: {w}**"]
    if s.get("checks"):
        L += ["", "## Checks", "", "| severity | ok | check | detail |", "|---|---|---|---|"]
        verdict = {"hard": "**NO**", "review": "**NO** (review)", "info": "no (info only)"}
        L += [f"| {k['severity']} | {'yes' if k['ok'] else verdict[k['severity']]} | {esc(k['check'])} | {esc(k['detail'])} |" for k in s["checks"]]
    if s.get("sites"):
        L += ["", "## Sites", "", "| id | address | coordinates |", "|---|---|---|"]
        L += [f"| {esc(x['source_id'])} | {esc(x['street'])}, {esc(x['zip'])} {esc(x['city'])} | {x['lat']}, {x['lng']} |" for x in s["sites"]]
    if s.get("written"):
        L += ["", "## Files", ""] + [f"- `{v['file']}`: {v['rows']} rows" for v in s["written"].values()]
        L += [f"- `{paths.QUERY_LOG_FILE}`, `{paths.RAW_FILE}`, `{paths.RUN_SUMMARY_JSON}`, `{paths.RUN_SUMMARY_MD}`"]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="hdp.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=RUN_MODES)
    ap.add_argument("--confirm", action="store_true", help="needed for 'full': sends a live request")
    ap.add_argument("--source", nargs="+", help="rebuild: the run folder that holds raw.jsonl")
    args = ap.parse_args(argv)
    try:
        res = run(args.mode, confirm_full_run=args.confirm, source_runs=args.source)
    except (RuntimeError, ValueError) as exc:  # e.g. 'full' without --confirm: nothing was sent
        print(f"\nNOT STARTED: {exc}")
        return 2
    print(f"\n{res.outcome}\nrun folder: {res.run_dir}")
    return 0 if res.outcome == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
