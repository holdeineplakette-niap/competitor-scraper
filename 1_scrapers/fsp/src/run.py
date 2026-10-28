"""FSP run: HU-filtered list + all-services list + sitemap cross-check, one in-scope CSV per run.

Scope rule (Nikos, 2026-10-06): a site that offers HU counts, whatever its badge; Gutachtenbüros without HU do not.
"Offers HU" = the site is in the HU-filtered list (`d=hauptuntersuchung`), or (for a site the locator does not list but
the sitemap does) its own detail page names Hauptuntersuchung.

Modes (see 0_instructions/storage_and_naming_convention.md):
  simulated  offline, fake fsp.de with known truth, must PASS                        -> test_output/
  sample     live, 2 pages of the HU list, reports SAMPLE (never PASS)               -> test_output/
  full       live, ~45 requests, ~3 min; needs confirm_full_run=True                 -> output/
  rebuild    offline, re-parses the raw files of earlier run folders (source_runs)   -> output/

The HU filter is disallowed by fsp.de's robots.txt. Nikos decided to disregard that (2026-10-06); the code still
needs `allow_robots_disallowed=True` per run so it is never done by accident.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from core import paths

from fsp import parse
from fsp.fetch import FspClient
from fsp.recon import (CENTER, MAX_PAGES, Guard, OfflineClient, RunLog, crawl_list, inspect_detail, run_sitemap,
                       say, write_csv)
from fsp.robots import parse_rules
from fsp.simulate import EXPECTED_IN_SCOPE, SimulatedFsp

SLUG = "fsp"
RUN_MODES = ("simulated", "sample", "full", "rebuild")
DROP_ALERT = 0.10  # in-scope count more than 10 % below the last passing run
MAX_SITEMAP_ONLY = 30  # more than this many sitemap-only pages means something is off: stop and look

OUT_COLUMNS = [
    "location_id", "source", "source_id", "scraped_at", "name", "type_badges", "street", "plz", "city",
    "email", "phone", "website_url", "booking_url",
    "hours_mo", "hours_di", "hours_mi", "hours_do", "hours_fr", "hours_sa", "hours_so", "hours_other_days",
    "opening_hours_raw", "detail_url", "scope_reason", "doubt_flags", "needs_review", "in_locator",
]


@dataclass
class RunResult:
    run_dir: Path
    mode: str
    outcome: str
    frame: pd.DataFrame  # the in-scope rows
    summary: dict
    written: dict


def doubt_flags(row: dict) -> list[str]:
    """Only marks; nothing is excluded."""
    flags = []
    if not re.fullmatch(r"\d{5}", row.get("plz") or ""):
        flags.append("plz_not_5_digits")
    if not row.get("street"):
        flags.append("no_street")
    low = f"{row.get('name', '')} {row.get('slug', '')}".lower()
    if re.search(r"pr(ü|ue|u)fst(ü|ue|u)tzpunkt", low):
        flags.append("named_pruefstuetzpunkt")
    if re.search(r"sachverst|gutachten|schaden", low):
        flags.append("named_gutachten_but_does_hu")
    if not row.get("phone") and not row.get("email"):
        flags.append("no_contact")
    return flags


def _out_row(row: dict, reason: str, in_locator: bool) -> dict:
    flags = doubt_flags(row)
    if not in_locator:
        flags.append("not_in_locator")
    out = {c: row.get(c, "") for c in OUT_COLUMNS}
    out.update(location_id=f"FSP-{row['slug']}", source_id=row["slug"], scope_reason=reason,
               doubt_flags="; ".join(flags), needs_review=bool(flags), in_locator=in_locator)
    return out


def _baseline(root, current_dir: Path) -> tuple[Path, int] | None:
    """Newest earlier full/rebuild run whose summary says PASS, with its in-scope count."""
    base = paths.output_dir(SLUG, "full", root)
    found = []
    for f in base.glob("*/run_summary.json"):
        if f.parent == current_dir:
            continue
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if s.get("outcome") == "PASS" and s.get("mode") in ("full", "rebuild") and "in_scope" in s.get("counts", {}):
            found.append((f.parent.name, f.parent, s["counts"]["in_scope"]))
    if not found:
        return None
    _, folder, n = sorted(found)[-1]
    return folder, n


def _check(checks: list, name: str, ok: bool, detail: str) -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})


def _crawl_checks(checks: list, key: str, crawl: dict) -> None:
    pages = crawl["pages"]
    rows = crawl["rows"]
    dup = len(rows) - len({r["slug"] for r in rows})
    _check(checks, f"{key}: pagination bar count equals pages returned",
           crawl["declared_pages"] == crawl["pages_with_cards"],
           f"declared {crawl['declared_pages']}, returned {crawl['pages_with_cards']}")
    _check(checks, f"{key}: loop ended on a real empty page", crawl["stop_reason"].endswith("(a real, empty result page)"),
           crawl["stop_reason"])
    _check(checks, f"{key}: no duplicate slugs", dup == 0, f"{len(rows)} cards, {dup} duplicate(s)")
    _check(checks, f"{key}: every card has name and slug", all(r["name"] and r["slug"] for r in rows),
           f"{sum(1 for r in rows if not (r['name'] and r['slug']))} card(s) without")
    _check(checks, f"{key}: all pages full except the last",
           all(p["cards"] == pages[0]["cards"] for p in pages[:-2]) if len(pages) > 2 else True,
           f"cards per page: {[p['cards'] for p in pages]}")


def run(mode: str = "simulated", confirm_full_run: bool = False, root=None, now: datetime | None = None,
        client=None, allow_robots_disallowed: bool = False, save_raw: bool = True, source_runs=None,
        max_pages: int = MAX_PAGES) -> RunResult:
    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {RUN_MODES}, got {mode!r}")
    if mode == "full" and not confirm_full_run:
        raise RuntimeError("A full run sends about 45 live requests. Pass confirm_full_run=True to start it.")
    if mode == "rebuild" and not source_runs:
        raise ValueError("mode 'rebuild' needs source_runs: the run folder(s) that hold the raw files")
    if client is None:
        client = {"simulated": SimulatedFsp, "rebuild": lambda: OfflineClient(source_runs)}.get(mode, FspClient)()
    started = now or datetime.now()
    run_dir = paths.new_run_dir(SLUG, mode, started, root)
    live = mode in ("full", "sample")
    raw_dir = None
    if save_raw and mode != "rebuild":
        raw_dir = run_dir / "raw" / "pages"  # the list pages; sitemap/ and detail/ go next to it under raw/
        raw_dir.mkdir(parents=True)
    date = f"{started:%Y-%m-%d}"
    scraped_at = started.isoformat(timespec="seconds")
    log = RunLog(run_dir / paths.QUERY_LOG_FILE if mode != "rebuild" else None)
    summary: dict = {"mode": mode, "outcome": "FAIL", "started": scraped_at, "run_dir": str(run_dir),
                     "source_runs": [str(s) for s in (source_runs or [])], "counts": {}, "checks": [], "warnings": [],
                     "robots_override": allow_robots_disallowed, "error": ""}
    checks = summary["checks"]
    rows_out: list[dict] = []
    say(f"[{mode}] run folder: {run_dir}")
    try:
        robots_resp = client.get("https://www.fsp.de/robots.txt")
        log.add(time=datetime.now().isoformat(timespec="seconds"), label="robots", url=robots_resp.url,
                status=robots_resp.status)
        if raw_dir:
            (raw_dir.parent / "robots.txt").write_text(robots_resp.text, encoding="utf-8")
        disallow, allow, _ = parse_rules(robots_resp.text)
        guard = Guard(disallow, allow, allow_robots_disallowed)

        say("HU-filtered list:")
        hu = crawl_list(client, guard, log, "hu", {**CENTER, "r": 1000, "d": "hauptuntersuchung"}, raw_dir,
                        scraped_at, "hauptuntersuchung", 2 if mode == "sample" else max_pages, gzip_raw=True)
        summary["counts"]["hu_cards"] = len(hu["rows"])
        for r in hu["rows"]:
            rows_out.append(_out_row(r, "hu_list", True))

        if mode != "sample":
            _crawl_checks(checks, "hu", hu)
            say("All-services list:")
            al = crawl_list(client, guard, log, "all", {**CENTER, "r": 1000}, raw_dir, scraped_at, "none",
                            max_pages, gzip_raw=True)
            _crawl_checks(checks, "all", al)
            hu_slugs, all_slugs = {r["slug"] for r in hu["rows"]}, {r["slug"] for r in al["rows"]}
            summary["counts"].update(all_cards=len(al["rows"]), not_hu=len(all_slugs - hu_slugs))
            _check(checks, "HU list is a subset of the all-services list", hu_slugs <= all_slugs,
                   f"{len(hu_slugs - all_slugs)} HU slug(s) not in the all-services list")

            say("Sitemap:")
            sm = run_sitemap(client, guard, log, raw_dir, {"all": all_slugs, "hu": hu_slugs})
            sm_slugs = set(sm["standorte_slugs"])
            only_sm = sorted(sm_slugs - all_slugs)
            summary["counts"].update(sitemap_slugs=len(sm_slugs), sitemap_only=len(only_sm))
            _check(checks, "sitemap is a plain urlset with /standorte/ pages", sm["kind"] == "urlset" and sm_slugs,
                   f"{sm['kind']}, {len(sm_slugs)} slugs")
            _check(checks, "every listed site is in the sitemap", all_slugs <= sm_slugs,
                   f"{len(all_slugs - sm_slugs)} listed site(s) not in the sitemap")
            _check(checks, f"at most {MAX_SITEMAP_ONLY} sitemap-only pages", len(only_sm) <= MAX_SITEMAP_ONLY,
                   f"{len(only_sm)} sitemap-only")
            say(f"Sitemap-only pages: {len(only_sm)}")
            details = [inspect_detail(client, guard, log, raw_dir, s) for s in only_sm[:MAX_SITEMAP_ONLY]]
            bad = [d["slug"] for d in details if "row" not in d]
            _check(checks, "every sitemap-only page could be read", not bad, f"unreadable: {bad}" if bad else
                   f"{len(details)} read")
            summary["sitemap_only"] = []
            for d in details:
                if "row" not in d:
                    continue
                r = d["row"]
                r.update(source="FSP", scraped_at=scraped_at)
                summary["sitemap_only"].append({"slug": d["slug"], "hu_on_detail_page": r["hu_on_detail_page"]})
                if r["hu_on_detail_page"]:
                    rows_out.append(_out_row(r, "detail_page_hu", False))
            summary["counts"]["sitemap_only_with_hu"] = sum(1 for s in summary["sitemap_only"] if s["hu_on_detail_page"])
            baseline = _baseline(root, run_dir)
            if baseline:
                folder, n_old = baseline
                drop = (n_old - len(rows_out)) / n_old if n_old else 0
                summary["drop_check"] = {"baseline": folder.name, "baseline_in_scope": n_old, "drop": round(drop, 4)}
                if drop > DROP_ALERT:
                    summary["warnings"].append(f"DROP ALERT: {len(rows_out)} in scope vs {n_old} in {folder.name} "
                                               f"({-drop:+.1%})")
            else:
                summary["drop_check"] = "no earlier passing full run"
            if mode == "simulated":
                _check(checks, "simulation: all known in-scope sites found", len(rows_out) == EXPECTED_IN_SCOPE,
                       f"{len(rows_out)} of {EXPECTED_IN_SCOPE}")
        summary["counts"]["in_scope"] = len(rows_out)
        summary["outcome"] = "SAMPLE" if mode == "sample" else ("PASS" if all(c["ok"] for c in checks) else "FAIL")
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["outcome"] = "FAIL"
        say(f"STOPPED: {summary['error']}")
        _write_summary(run_dir, summary, client)
        raise

    frame = pd.DataFrame(rows_out, columns=OUT_COLUMNS)
    tag = "" if summary["outcome"] in ("PASS", "SAMPLE") else "unverified_"
    csv_path = run_dir / f"fsp_pruefstellen_{tag}{date}.csv"
    write_csv(csv_path, rows_out, OUT_COLUMNS)
    summary["flags"] = {"needs_review": int(frame["needs_review"].sum()) if len(frame) else 0}
    written = {"pruefstellen_csv": (csv_path, len(frame))}
    _write_summary(run_dir, summary, client)
    say(f"[{mode}] {summary['outcome']}: {len(frame)} sites in scope -> {csv_path.name}")
    return RunResult(run_dir, mode, summary["outcome"], frame, summary, written)


def _write_summary(run_dir: Path, summary: dict, client) -> None:
    summary["requests_made"] = getattr(client, "requests_made", 0)
    (run_dir / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str),
                                                  encoding="utf-8")
    c = summary["counts"]
    lines = [f"# FSP run: {summary['outcome']}", "",
             f"- Mode: {summary['mode']} · started {summary['started']} · requests made: {summary['requests_made']}",
             f"- Robots.txt override (HU filter `d=`): {summary['robots_override']}"]
    if summary["source_runs"]:
        lines.append("- Rebuilt offline from: " + ", ".join(f"`{s}`" for s in summary["source_runs"]))
    if summary["error"]:
        lines += ["", f"**Stopped: {summary['error']}**"]
    for w in summary["warnings"]:
        lines += ["", f"**{w}**"]
    lines += ["", f"- In scope: **{c.get('in_scope', '-')}** (HU list {c.get('hu_cards', '-')} + sitemap-only sites with HU "
              f"{c.get('sitemap_only_with_hu', 0)}) · all-services list {c.get('all_cards', '-')} · sitemap "
              f"{c.get('sitemap_slugs', '-')} · sitemap-only {c.get('sitemap_only', '-')}",
              f"- Drop check: {summary.get('drop_check', '-')}", "", "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {k['check']} | {'ok' if k['ok'] else '**FAIL**'} | {k['detail']} |" for k in summary["checks"]]
    (run_dir / paths.RUN_SUMMARY_MD).write_text("\n".join(lines) + "\n", encoding="utf-8")
