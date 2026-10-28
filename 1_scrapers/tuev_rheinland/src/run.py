"""TÜV Rheinland run: the finder JSON plus the three landing pages, checked, one folder per run (headless; for core.monthly).

Modes (see 0_instructions/storage_and_naming_convention.md):
  simulated  offline, fake tuv.com with known truth, must PASS (faults can be injected in tests)          -> test_output/
  full       live: 1 GET of the finder JSON (+1 retry on network errors/5xx) and 1 GET per landing page,
             4 requests; needs confirm_full_run=True                                                       -> output/
  rebuild    offline: re-runs everything on the raw files of an earlier run folder (source_runs)          -> output/

Command line (from the project folder, Windows):
  .venv\\Scripts\\python.exe -m tuev_rheinland.run simulated
  .venv\\Scripts\\python.exe -m tuev_rheinland.run full --confirm
  .venv\\Scripts\\python.exe -m tuev_rheinland.run rebuild --source 1_scrapers\\tuev_rheinland\\output\\<run folder>

PASS needs every `hard` check to hold: the adapter's checks (JSON shape, fields, ids, both pools, addresses, 10 % drop) and
the Saarland page (24 rows, text says 24). The landing pages never add or drop rows by themselves: only Nikos's decisions in
ADD_FROM_LANDING / FILL_FROM_LANDING change the CSV. A landing row that matches nothing and is not in KNOWN_UNMATCHED is a
`review` item ("check now"), and so is a decision that no longer applies; neither fails the run. A failed run writes its CSV
with `unverified` in the name. The control notebook keeps the step-by-step view.
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

from tuev_rheinland import adapter
from tuev_rheinland.adapter import FetchError

SLUG = adapter.SLUG
RUN_MODES = ("simulated", "full", "rebuild")
RAW_JSON = "raw.json"
DROP_CHECK = "in-scope count vs last PASS CSV"  # name of the adapter's 10 % drop check

# Nikos's decisions (2026-10-06, from the control notebook), applied on every run
ADD_FROM_LANDING = [("saarland", "66679", "Saarbrücker Straße 221")]  # second Losheim station, not in the finder JSON
FILL_FROM_LANDING = {"45371": ("saarland", "66333", "Saarwiesenstraße 7")}  # finder record with empty address (Völklingen)
# landing rows that match nothing and are known (region, PLZ, street); anything else unmatched is a "check now" item
KNOWN_UNMATCHED = {("saarland", "66687", "Mittelbachweg 1")}  # Wadern-Lockweiler, unresolved since 2026-10-06
KNOWN_GAPS = [
    "Mobile PSPs and non-VIS types are not exported.",
    "Wadern-Lockweiler (Mittelbachweg 1, 66687) is on the Saarland landing page but not in the finder JSON; unresolved.",
]
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
    """tuv.com through the adapter's fetch functions (no bypass: a 403 or a non-JSON answer stops the run)."""

    def __init__(self):
        self.requests_made = 0

    def get_json(self, url: str):
        self.requests_made += 1
        return adapter.fetch(url)

    def get_page(self, url: str) -> bytes:
        self.requests_made += 1
        return adapter.fetch_page(url)


class OfflineClient:
    """Serves the raw files of an earlier run folder (raw/ or the folder itself, as in the 2026-10-06 sample copies)."""

    requests_made = 0

    def __init__(self, source_runs):
        self.dirs = [Path(d) for d in source_runs]

    def _file(self, name: str) -> Path:
        for d in self.dirs:
            for f in (d / "raw" / name, d / name):
                if f.exists():
                    return f
        raise FetchError(f"{name} is not in the source run(s) {[str(d) for d in self.dirs]}")

    def get_json(self, url: str):
        content = self._file(RAW_JSON).read_bytes()
        return json.loads(content), content

    def get_page(self, url: str) -> bytes:
        region = next(r for r, u in adapter.LANDING_PAGES.items() if u == url)
        return self._file(f"landing_{region}.html").read_bytes()


class RunLog:
    def __init__(self, path: Path | None):
        self.path, self.rows = path, []

    def add(self, **row) -> None:
        self.rows.append(row)
        if self.path:
            new = not self.path.exists()
            with open(self.path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, delimiter=";")
                if new:
                    w.writeheader()
                w.writerow({k: row.get(k, "") for k in LOG_COLUMNS})


# ------------------------------------------------------------------------------------------------ landing decisions
def apply_decisions(df: pd.DataFrame, matches: pd.DataFrame) -> tuple[pd.DataFrame, list[dict], dict]:
    """Apply FILL_FROM_LANDING and ADD_FROM_LANDING one by one; a decision that no longer fits is skipped and reported."""
    checks, applied = [], {"filled": [], "added": []}
    for source_id, target in FILL_FROM_LANDING.items():
        try:
            df = adapter.fill_from_landing(df, matches, {source_id: target})
            applied["filled"].append(source_id)
        except ValueError as exc:
            checks.append(check(f"decision 'fill {source_id} from {target}' still applies", False, exc, "review"))
    for pick in ADD_FROM_LANDING:
        try:
            df = adapter.add_from_landing(df, matches, [pick])
            applied["added"].append(list(pick))
        except ValueError as exc:
            checks.append(check(f"decision 'add {pick}' still applies", False, exc, "review"))
    return df, checks, applied


def unmatched_rows(matches: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """(new, known): landing rows that are not a VIS match and are neither known nor covered by a decision."""
    covered = set(KNOWN_UNMATCHED) | {tuple(p) for p in ADD_FROM_LANDING} | {tuple(v) for v in FILL_FROM_LANDING.values()}
    rows = matches[matches["status"] != "matched_vis"] if len(matches) else matches
    cols = ["region", "standort", "street", "zip", "city", "status", "matched_type", "matched_id", "title_hint"]
    new = [r for r in rows[cols].to_dict("records") if (r["region"], r["zip"], r["street"]) not in covered]
    known = [r for r in rows[cols].to_dict("records") if (r["region"], r["zip"], r["street"]) in KNOWN_UNMATCHED]
    return new, known


# ------------------------------------------------------------------------------------------------ run
def run(mode: str = "simulated", confirm_full_run: bool = False, root=None, now: datetime | None = None, client=None,
        centroids=None, source_runs=None, save_raw: bool = True) -> RunResult:
    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {RUN_MODES}, got {mode!r}")
    if mode == "full" and not confirm_full_run:
        raise RuntimeError("A full run sends 4 live requests to tuv.com (finder JSON + 3 landing pages). "
                           "Pass confirm_full_run=True to start it.")
    if mode == "rebuild" and not source_runs:
        raise ValueError("mode 'rebuild' needs source_runs: the run folder(s) that hold the raw files")
    if client is None:
        if mode == "simulated":
            from tuev_rheinland.simulate import SimulatedTuv
            client = SimulatedTuv()
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
        "landing": {}, "decisions": {}, "check_now": [], "fsp_crosscheck": {}, "known_gaps": KNOWN_GAPS, "error": "",
    }
    say(f"[{mode}] run folder: {run_dir}")
    df = pd.DataFrame(columns=adapter.COLUMNS)
    try:
        # ---- finder JSON (a failure stops the run: there is nothing to export)
        say("Finder JSON")
        try:
            records, content = client.get_json(adapter.SOURCE_URL)
        except FetchError as exc:
            log.add(time=datetime.now().isoformat(timespec="seconds"), label="finder_json", url=adapter.SOURCE_URL, note=f"FAILED: {exc}")
            raise
        log.add(time=datetime.now().isoformat(timespec="seconds"), label="finder_json", url=adapter.SOURCE_URL, status=200, bytes=len(content))
        if raw_dir:
            (raw_dir / RAW_JSON).write_bytes(content)
        if centroids is None:
            centroids = geo.load_plz_centroids(paths.geonames_dir() / "DE.txt")
        df = adapter.add_distance(adapter.classify(records, scraped_at), centroids)

        # ---- landing pages (cross-check; Saarland is a hard check, Berlin/Pfalz are review)
        found, claims, saarland_rows, page_checks = [], {}, None, []
        for region, url in adapter.LANDING_PAGES.items():
            say(f"Landing page: {region}")
            try:
                page = client.get_page(url)
            except FetchError as exc:
                log.add(time=datetime.now().isoformat(timespec="seconds"), label=f"landing_{region}", url=url, note=f"FAILED: {exc}")
                page_checks.append(check(f"landing page {region} could be fetched", False, exc,
                                         "hard" if region == "saarland" else "review"))
                continue
            log.add(time=datetime.now().isoformat(timespec="seconds"), label=f"landing_{region}", url=url, status=200, bytes=len(page))
            if raw_dir:
                (raw_dir / f"landing_{region}.html").write_bytes(page)
            rows, claims[region] = adapter.parse_landing(page, region)
            found.append(adapter.match_landing(rows, records))
            if region == "saarland":
                saarland_rows = rows
        matches = pd.concat(found, ignore_index=True) if found else pd.DataFrame(
            columns=["region", "standort", "street", "zip", "city", "status", "matched_type", "matched_id", "how", "title_hint"])
        if saarland_rows is not None:
            ok, detail = adapter.saarland_check(saarland_rows, claims["saarland"])
            page_checks.append(check("Saarland landing page: 24 rows and the text still says 24", ok, detail))
            df = adapter.set_saarland(df, matches)
        df, decision_checks, applied = apply_decisions(df, matches)
        df = adapter.add_distance(df, centroids)  # fills the state of added rows
        new, known = unmatched_rows(matches)
        page_checks += decision_checks
        page_checks.append(check("every landing row matches a finder VIS, a decision or a known gap", not new,
                                 f"{len(new)} new: " + "; ".join(f"{r['region']} {r['standort']} ({r['street']}, {r['zip']})" for r in new)
                                 if new else "none new", "review"))
        summary["check_now"] = new
        summary["decisions"] = {"applied": applied, "add_from_landing": [list(p) for p in ADD_FROM_LANDING],
                                "fill_from_landing": {k: list(v) for k, v in FILL_FROM_LANDING.items()}}
        summary["landing"] = {
            "claims": claims,
            "status": {r: matches[matches["region"] == r]["status"].value_counts().to_dict() for r in claims},
            "known_unmatched": known,
            "similar_street_matches": matches[matches["how"].str.contains("check|PLZ missing", na=False)][
                ["region", "standort", "street", "zip", "how", "matched_id"]].to_dict("records"),
        }

        # ---- checks
        all_checks = [check(n, ok, d) for n, ok, d in adapter.run_checks(records, df, root)] + page_checks
        if mode == "simulated":  # the baseline is the last real CSV in output/, which a fake source cannot be measured against
            all_checks = [check(c["check"], True, "not applicable in simulated mode") if c["check"] == DROP_CHECK else c for c in all_checks]
        fsp = adapter.fsp_crosscheck(df, root)
        summary["fsp_crosscheck"] = {k: v for k, v in fsp.items() if k not in ("only_here", "only_fsp")} | {
            "only_here": len(fsp.get("only_here", [])), "only_fsp": len(fsp.get("only_fsp", []))}

        failed_hard = [c for c in all_checks if c["severity"] == "hard" and not c["ok"]]
        scope = df[df["in_scope"]]
        summary["checks"] = all_checks
        summary["failed_hard"] = [c["check"] for c in failed_hard]
        status = "FAIL" if failed_hard else "PASS"
        summary.update(status=status, outcome=status if not failed_hard else f"FAIL: {len(failed_hard)} hard check(s) failed",
                       in_scope=len(scope))
        summary["counts"] = {
            "records": len(records) if isinstance(records, list) else None, "vis": int(len(df)), "in_scope": len(scope),
            "site_type": scope["site_type"].value_counts().to_dict(), "operator_group": scope["operator_group"].value_counts().to_dict(),
            "needs_review": int(scope["needs_review"].sum()),
        }
    except Exception as exc:  # a block, a non-JSON answer or a bug: the folder keeps what was fetched, the summary says why
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
    L = [f"# TÜV Rheinland run report: {s['status']}", "", f"- **Result:** {s['outcome']}",
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
        L += ["", f"**In scope: {c.get('in_scope')}** (finder records {c.get('records')}, VIS {c.get('vis')}) · "
              f"site type {c.get('site_type')} · operator group {c.get('operator_group')} · needs review {c.get('needs_review')}"]
    if s.get("check_now"):
        L += ["", "## Check now: landing rows that match nothing", ""]
        L += _table(["region", "Standort", "address", "status", "title hint"],
                    [[r["region"], r["standort"], f"{r['street']}, {r['zip']} {r['city']}", r["status"], r["title_hint"]] for r in s["check_now"]])
    L += ["", "## Checks", ""]
    verdict = {"hard": "**NO**", "review": "**NO** (review)", "info": "no (info only)"}
    L += _table(["severity", "ok", "check", "detail"],
                [[k["severity"], "yes" if k["ok"] else verdict[k["severity"]], k["check"], k["detail"]] for k in s.get("checks", [])])
    if s.get("failed_hard"):
        L += ["", "**Failed hard checks:** " + "; ".join(s["failed_hard"])]
    lp = s.get("landing") or {}
    if lp:
        L += ["", "## Landing pages", "", f"- Page text states: {lp.get('claims')}", f"- Match status: {lp.get('status')}",
              f"- Decisions applied: {(s.get('decisions') or {}).get('applied')}",
              f"- Known unmatched: {', '.join(r['standort'] for r in lp.get('known_unmatched', [])) or 'none'}"]
        if lp.get("similar_street_matches"):
            L += ["- Matched only by a similar street: " + "; ".join(
                f"{r['region']} {r['standort']} ({r['street']}, {r['zip']}) -> {r['matched_id']}" for r in lp["similar_street_matches"])]
    fx = s.get("fsp_crosscheck") or {}
    if fx.get("file"):
        L += ["", "## FSP cross-check (report only)", "",
              f"- FSP run {fx['file']}: fsp_partner here {fx['here']}, rows in FSP CSV {fx['fsp']}, matched exact {fx['matched']}, "
              f"loose {fx['matched_loose']}, only here {fx['only_here']}, only in FSP {fx['only_fsp']}"]
    L += ["", "## Known gaps", ""] + [f"- {g}" for g in s.get("known_gaps", [])]
    if s.get("written"):
        L += ["", "## Files", ""] + [f"- `{v['file']}`: {v['rows']} rows" for v in s["written"].values()]
        L += [f"- `{paths.QUERY_LOG_FILE}`, `{paths.RUN_SUMMARY_JSON}`, `{paths.RUN_SUMMARY_MD}`, `raw/` (finder JSON, landing pages)"]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tuev_rheinland.run", description=__doc__.split("\n\n")[0])
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
            print(f"  [{k['severity']}] {k['check']}: {k['detail']}")
    return 0 if res.outcome == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
