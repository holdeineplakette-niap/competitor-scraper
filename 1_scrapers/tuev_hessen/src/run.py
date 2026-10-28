"""TÜV Hessen run: two searches ('Alle Standorte', 'Kfz Prüfstelle') through the locator page, checks, one folder per run.

Modes (see 0_instructions/storage_and_naming_convention.md):
  simulated  offline, fake locator with known truth, must PASS (faults can be injected in tests)   -> test_output/
  full       live: Playwright/Chromium, 4 searches (2 via the UI, 2 replays) + 2 detail pages      -> output/
  rebuild    offline: re-parses and re-checks the raw.jsonl of an earlier run folder (source_run)   -> output/

Command line (from the project folder, Windows):
  .venv\\Scripts\\python.exe -m tuev_hessen.run simulated
  .venv\\Scripts\\python.exe -m tuev_hessen.run full --confirm
  .venv\\Scripts\\python.exe -m tuev_hessen.run rebuild --source 1_scrapers\\tuev_hessen\\output\\<run folder>

PASS needs every `hard` check to hold (see checks.py). A run that is not a PASS writes its CSVs with `unverified` in the
name, or no CSV at all when there is no usable data. A block, an error page or an empty answer is a FAIL, never "0 sites".
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from core import paths
from core.export import baseline_run, counts, drop_check, relative, safe_path

from tuev_hessen import checks, fetch, parse
from tuev_hessen.simulate import simulated_outcome

SLUG = "tuev_hessen"
RUN_MODES = ("simulated", "full", "rebuild")
DETAIL_PAGES = 2

# export columns in this order; names starting with "_" are internal and never exported
EXPORT_COLUMNS = [
    "location_id", "source", "source_id", "scraped_at", "title", "site_type", "operator", "listed_by", "title_rest",
    "in_scope", "scope_reason", "is_hu_site", "is_lkw_site", "tags", "tag_title_mismatch", "tag_title_mismatch_reason",
    "street", "zip", "city", "latitude", "longitude", "plz_distance_km",
    "phone", "mobile", "fax", "opening_hours", "booking_url", "location_sign", "contact_url", "websites", "betreiber_text", "detail_url",
    "hu_evidence", "notices", "dq_flags", "needs_review",
    "infotext_text", "uid", "pid", "es_id", "es_index", "is_service_center", "score_factor", "sort_values", "es_ignored",
    "tags_json", "unknown_source_fields",
]
LOG_COLUMNS = ["time", "what", "url", "status", "seconds", "note"]


@dataclass
class RunResult:
    run_dir: Path
    mode: str
    status: str
    summary: dict
    frames: dict
    written: dict


# ------------------------------------------------------------------------------------------------ analysis
def _decode(cap: fetch.Capture):
    ui, ui_err = checks.decode_payload(cap.ui_body, cap.ui_content_type, cap.ui_status)
    if cap.replay_body:
        replay, replay_err = checks.decode_payload(cap.replay_body, cap.replay_content_type, cap.replay_status)
    else:
        replay, replay_err = None, cap.replay_error
    return ui, ui_err, replay, replay_err


def analyze(outcome: fetch.FetchOutcome, centroids, scraped_at: str) -> dict:
    """All checks and parsed rows for one fetch outcome. No file or network access."""
    result: dict = {"checks": [], "datasets": {}, "rows_all": [], "rows_hu": [], "findings": {}, "diff": {}}
    payloads = {}
    for key in fetch.DATASETS:
        cap = outcome.captures.get(key)
        if cap is None:
            result["checks"].append(checks.check(f"{key}: dataset was fetched", False, "no capture recorded"))
            continue
        ui, ui_err, replay, replay_err = _decode(cap)
        result["checks"] += checks.check_dataset(key, cap, ui, ui_err, replay, replay_err)
        used, used_from = (replay, "replay") if replay is not None else (ui, "ui")
        payloads[key] = used
        info = {"source_answer": used_from if used is not None else None}
        if used is not None:
            info.update(total_value=used["hits"]["total"].get("value"), total_relation=used["hits"]["total"].get("relation"),
                        hits=len(used["hits"]["hits"]), ui_counter=cap.ui_counter, took_ms=used.get("took"),
                        shards=used.get("_shards"), timed_out=used.get("timed_out"),
                        ignored_hits=sum(1 for h in used["hits"]["hits"] if "_ignored" in h))
        result["datasets"][key] = info
    if payloads.get("all") is not None:
        hits_all = payloads["all"]["hits"]["hits"]
        rows = parse.parse_hits(hits_all, scraped_at)
        for r in rows:
            r["in_scope"] = r["is_hu_site"]
            r["scope_reason"] = f"tag '{parse.HU_TAG}'" if r["is_hu_site"] else ""
        result["findings"] = checks.data_quality(rows, centroids)
        result["rows_all"] = rows
        if payloads.get("hu") is not None:
            hits_hu = payloads["hu"]["hits"]["hits"]
            by_uid = {r["uid"]: r for r in rows}
            cross, result["diff"] = checks.check_cross(hits_all, hits_hu, by_uid)
            result["checks"] += cross
            hu_uids = {h["_source"]["uid"] for h in hits_hu}
            result["rows_hu"] = [r for r in rows if r["uid"] in hu_uids]
        result["checks"] += checks.quality_checks(result["findings"])
    return result


def pick_detail_urls(captures: dict, n: int = DETAIL_PAGES) -> list[str]:
    """Detail pages to open: the lowest-uid own station, then the lowest-uid partner, then anything (n pages in all)."""
    cap = captures.get("all")
    if cap is None:
        return []
    ui, _, replay, _ = _decode(cap)
    payload = replay or ui
    if payload is None:
        return []
    rows = sorted(parse.parse_hits(payload["hits"]["hits"]), key=lambda r: r["uid"])
    urls = []
    for wanted in ("own_station", "partner"):
        urls += [r["detail_url"] for r in rows if r["site_type"] == wanted and r["detail_url"]][:1]
    urls += [r["detail_url"] for r in rows if r["detail_url"] and r["detail_url"] not in urls]
    return urls[:n]


# ------------------------------------------------------------------------------------------------ files
def _raw_name(key: str, how: str) -> str:
    return f"location_search_{key}_{how}"


def write_raw(run_dir: Path, outcome: fetch.FetchOutcome) -> list[str]:
    """raw.jsonl (every location_search answer, `{"file", "response"}` per line) and raw/detail/*.html. Never overwritten."""
    files = []
    with open(run_dir / paths.RAW_FILE, "x", encoding="utf-8") as fh:
        for key, cap in outcome.captures.items():
            for how, body in (("ui", cap.ui_body), ("replay", cap.replay_body)):
                if not body:
                    continue
                try:
                    response = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    response = {"_not_json": body[:4000].decode("utf-8", "replace")}
                fh.write(json.dumps({"file": _raw_name(key, how), "response": response}, ensure_ascii=False) + "\n")
                files.append(_raw_name(key, how))
    if outcome.detail_pages:
        folder = run_dir / "raw" / "detail"
        folder.mkdir(parents=True)
        for i, page in enumerate(outcome.detail_pages, 1):
            slug = [p for p in page["url"].split("/") if p][-1]
            (folder / f"detail_{i}_{slug}.html").write_text(page["html"], encoding="utf-8")
    return files


def load_outcome(source_run: Path) -> fetch.FetchOutcome:
    """Rebuild a fetch outcome from an earlier run folder (raw.jsonl + run_summary.json + raw/detail)."""
    summary = json.loads((source_run / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
    raw = {}
    with open(source_run / paths.RAW_FILE, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            raw[rec["file"]] = json.dumps(rec["response"], ensure_ascii=False).encode("utf-8")
    out = fetch.FetchOutcome(access=summary.get("access", {}), form_state=summary.get("form_state", {}))
    for key, c in summary["captures"].items():
        cap = fetch.Capture(dataset=key, tag_value=c["tags_value"], ui_label=c["ui_label"])
        cap.request_method, cap.request_url = c["request"]["method"], c["request"]["url"]
        cap.request_content_type, cap.request_body_bytes = c["request"]["content_type"], c["request"]["body_bytes"]
        cap.request_fields = [tuple(f) for f in c["request"]["fields"]]
        cap.ui_status, cap.ui_content_type, cap.ui_counter = c["ui"]["status"], c["ui"]["content_type"], c["ui"]["counter"]
        cap.ui_body = raw.get(_raw_name(key, "ui"), b"")
        cap.replay_status, cap.replay_content_type = c["replay"]["status"], c["replay"]["content_type"]
        cap.replay_body, cap.replay_error = raw.get(_raw_name(key, "replay"), b""), c["replay"]["error"]
        out.captures[key] = cap
    for d in summary.get("detail_pages", []):
        out.detail_pages.append({"url": d["url"], "status": d["status"], "html": ""})
    folder = source_run / "raw" / "detail"
    if folder.is_dir():
        for page, f in zip(out.detail_pages, sorted(folder.glob("*.html"))):
            page["html"] = f.read_text(encoding="utf-8")
    return out


def write_query_log(run_dir: Path, log: list[dict]) -> None:
    with open(run_dir / paths.QUERY_LOG_FILE, "x", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(log)


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{c: r.get(c, "") for c in EXPORT_COLUMNS} for r in rows], columns=EXPORT_COLUMNS)


def write_exports(run_dir: Path, frames: dict, run_date: str, tag: str | None) -> dict:
    written, renamed = {}, []
    suffix = f"_{tag}" if tag else ""
    for key in ("hu", "all"):
        frame = frames.get(key)
        if frame is None or not len(frame):
            continue
        path = safe_path(run_dir / f"{SLUG}_{key}_{run_date}{suffix}.csv", renamed)
        frame.to_csv(path, sep=";", encoding="utf-8-sig", index=False)
        written[key] = {"file": path.name, "rows": len(frame)}
    return written


# ------------------------------------------------------------------------------------------------ run
def _fail_summary(summary: dict, run_dir: Path, error: str, outcome: fetch.FetchOutcome | None) -> None:
    summary.update(status="FAIL", outcome=f"FAIL: {error}", error=error)
    if outcome is not None:
        summary["access"] = outcome.access
        write_query_log(run_dir, outcome.log)


def run(mode: str = "simulated", confirm_full_run: bool = False, root=None, now: datetime | None = None,
        source_run=None, outcome: fetch.FetchOutcome | None = None, centroids=None, headless: bool = True,
        fallback_headed: bool = True, fault: str | None = None) -> RunResult:
    """Run one mode. `outcome` (a prepared FetchOutcome) replaces the fetch step, for tests."""
    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {RUN_MODES}, got {mode!r}")
    if mode == "full" and not confirm_full_run and outcome is None:
        raise RuntimeError("A full run opens the live locator (3 page loads and 4 searches). Pass confirm_full_run=True to start it.")
    if mode == "rebuild" and not source_run:
        raise ValueError("mode 'rebuild' needs source_run: the run folder that holds raw.jsonl and run_summary.json")
    started = now or datetime.now()
    t0 = time.time()
    run_dir = paths.new_run_dir(SLUG, mode, started, root)
    run_date = f"{started:%Y-%m-%d}"
    scraped_at = started.isoformat(timespec="seconds")
    summary: dict = {"mode": mode, "status": "FAIL", "outcome": "FAIL", "run_date": run_date, "started": scraped_at,
                     "run_dir": relative(run_dir, paths.project_root() if root is None else Path(root)),
                     "source_run": str(source_run) if source_run else "", "error": "", "in_scope": None, "access": {},
                     "counts": {}, "checks": []}

    # ---- fetch
    try:
        if outcome is None:
            if mode == "simulated":
                outcome = simulated_outcome(fault)
            elif mode == "rebuild":
                outcome = load_outcome(Path(source_run))
            else:
                outcome = fetch.fetch_all(headless=headless, fallback_headed=fallback_headed, detail_urls=pick_detail_urls)
    except fetch.FetchError as exc:
        _fail_summary(summary, run_dir, str(exc), getattr(exc, "outcome", None))
        _finish(summary, run_dir, {}, {}, t0)
        return RunResult(run_dir, mode, "FAIL", summary, {}, {})
    except (OSError, ValueError, KeyError) as exc:
        _fail_summary(summary, run_dir, f"{type(exc).__name__}: {exc}", None)
        _finish(summary, run_dir, {}, {}, t0)
        return RunResult(run_dir, mode, "FAIL", summary, {}, {})

    if mode != "rebuild":
        write_raw(run_dir, outcome)
        write_query_log(run_dir, outcome.log)

    # ---- analyse
    if centroids is None:
        try:
            from core.geo import ensure_geonames, load_plz_centroids
            centroids = load_plz_centroids(ensure_geonames(paths.geonames_dir(root)))
        except Exception as exc:  # no GeoNames file and no network: the PLZ check is skipped and says so
            summary.setdefault("warnings", []).append(f"PLZ centroids unavailable ({type(exc).__name__}): PLZ plausibility check skipped")
    res = analyze(outcome, centroids, scraped_at)
    all_checks = res["checks"]

    base = baseline_run(paths.output_dir(SLUG, "full", root), exclude=run_dir)
    n_hu = len(res["rows_hu"])
    text, alert = drop_check(n_hu, base) if res["rows_hu"] else ("not applicable (no HU rows)", False)
    all_checks.append(checks.check("HU count has not dropped more than 10 % against the last passing full run", not alert, text))

    failed = [c for c in all_checks if c["severity"] == "hard" and not c["ok"]]
    status = "FAIL" if failed else "PASS"
    summary.update(status=status, outcome="PASS" if not failed else f"FAIL: {len(failed)} hard check(s) failed",
                   in_scope=n_hu, baseline=base, drop_check=text)
    frames = {"hu": _frame(res["rows_hu"]) if res["rows_hu"] else None,
              "all": _frame(res["rows_all"]) if res["rows_all"] else None}
    written = write_exports(run_dir, frames, run_date, None if status == "PASS" else "unverified")

    summary.update(
        access=outcome.access, form_state=outcome.form_state, datasets=res["datasets"],
        captures={k: fetch.capture_summary(c) for k, c in outcome.captures.items()},
        counts={"all": len(res["rows_all"]), "hu": n_hu, "non_hu": len(res["diff"].get("only_in_all", []))},
        checks=all_checks, failed_hard=[c["check"] for c in failed],
        review=[c for c in all_checks if c["severity"] == "review" and not c["ok"]],
        diff=res["diff"], findings=_findings_for_summary(res), written=written,
    )
    if res["rows_all"]:
        summary.update(_breakdowns(res["rows_all"]))
    summary["detail_pages"] = [{"url": d["url"], "status": d["status"], "description": parse.describe_detail(d["html"]) if d["html"] else {}}
                               for d in outcome.detail_pages]
    summary["fields"] = _field_report(outcome, frames.get("all"))
    _finish(summary, run_dir, frames, written, t0)
    return RunResult(run_dir, mode, status, summary, {k: v for k, v in frames.items() if v is not None}, written)


def _findings_for_summary(res: dict) -> dict:
    if not res["rows_all"]:
        return {}  # no parsed data (blocked, empty or malformed answer): there is nothing to report on
    f = dict(res["findings"])
    rows = {r["uid"]: r for r in res["rows_all"]}
    f["tag_title_mismatch"] = [{"uid": u, "site_type": r["site_type"], "hu": r["is_hu_site"], "reason": r["tag_title_mismatch_reason"]}
                               for u, r in rows.items() if r["tag_title_mismatch"]]
    f["unclassified"] = [{"uid": u, "title": r["title"], "tags": r["tags"], "hu": r["is_hu_site"], "lkw": r["is_lkw_site"]}
                         for u, r in rows.items() if r["site_type"] == "unclassified"]
    f["lkw_sites"] = [{"uid": u, "title": r["title"], "site_type": r["site_type"], "hu": r["is_hu_site"]} for u, r in rows.items() if r["is_lkw_site"]]
    f["non_hu_sites"] = [{"uid": u, "title": rows[u]["title"], "tags": rows[u]["tags"], "hu_evidence": rows[u]["hu_evidence"]}
                         for u in res["diff"].get("only_in_all", []) if u in rows]
    f["needs_review_uids"] = sorted(u for u, r in rows.items() if r["needs_review"])
    f["hu_tag_without_hu_evidence"] = sorted(u for u, r in rows.items() if "hu_tag_without_hu_evidence" in r["dq_flags"])
    return f


def _breakdowns(rows: list[dict]) -> dict:
    s = pd.DataFrame(rows)
    hu = s[s["is_hu_site"]]
    return {
        "site_types": {"all": counts(s["site_type"]), "hu": counts(hu["site_type"])},
        "operators": {"all": counts(s["operator"].replace("", "(none)")), "hu": counts(hu["operator"].replace("", "(none)"))},
        "tags": counts(pd.Series([t for r in rows for t in r["tags"].split("; ") if t])),
        "lkw": {"all": int(s["is_lkw_site"].sum()), "hu": int(hu["is_lkw_site"].sum())},
    }


def _field_report(outcome: fetch.FetchOutcome, frame) -> dict:
    cap = outcome.captures.get("all")
    ui, _, replay, _ = _decode(cap) if cap else (None, "", None, "")
    payload = replay or ui
    src_keys, meta_keys, hit_keys = {}, {}, {}
    if payload:
        for h in payload["hits"]["hits"]:
            for k in h:
                hit_keys[k] = hit_keys.get(k, 0) + 1
            for k in h.get("_source", {}):
                src_keys[k] = src_keys.get(k, 0) + 1
            for k in (h.get("_source", {}).get("searchable_meta") or {}):
                meta_keys[k] = meta_keys.get(k, 0) + 1
    fill = {}
    if frame is not None and len(frame):
        for c in EXPORT_COLUMNS:
            col = frame[c]
            fill[c] = int(col.notna().sum() - (col.astype(str).str.strip() == "").sum())
    return {"hit_fields": hit_keys, "source_fields": src_keys, "searchable_meta_fields": meta_keys, "export_fill": fill,
            "rows": 0 if frame is None else len(frame)}


def _finish(summary: dict, run_dir: Path, frames: dict, written: dict, t0: float) -> None:
    summary["seconds"] = round(time.time() - t0, 1)
    (run_dir / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run_dir / paths.RUN_SUMMARY_MD).write_text(render_markdown(summary), encoding="utf-8")


# ------------------------------------------------------------------------------------------------ report
def _table(header: list[str], rows: list[list]) -> list[str]:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]


def render_markdown(s: dict) -> str:
    L = [f"# TÜV Hessen run report: {s['status']}", "",
         f"- **Result:** {s['outcome']}", f"- **Mode / date:** {s['mode']} / {s['run_date']} (started {s['started']}, {s.get('seconds', '?')} s)",
         f"- **Run folder:** `{s['run_dir']}`"]
    if s.get("source_run"):
        L.append(f"- **Rebuilt from:** `{s['source_run']}` (no network)")
    if s.get("error"):
        L += ["", f"**ERROR:** {s['error']}", "", "No data was exported. Nothing here counts as a result."]
    acc = s.get("access", {})
    L += ["", "## 1. Method", ""]
    L += [f"- Locator page `{fetch.LOCATOR_URL}`, loaded in Playwright / {acc.get('browser', '?')} (**{'headless' if acc.get('headless', True) else 'headed'}**).",
          f"- Cloudflare: {'blocked headless, then ran headed: ' + acc['headless_blocked'] if acc.get('headless_blocked') else 'did not block (HTTP 200 on the page and on every search)' if not s.get('error') else 'see error above'}. "
          f"Cookies present (names only): {', '.join(acc.get('cookie_names', [])) or '-'}. No stealth plugin, no faked fingerprint, no challenge solver.",
          f"- Cookie banner: {acc.get('consent', '?')}.",
          "- Each dataset: select the type in the UI, click 'Standorte finden', record the real request and answer and the counter on the button; "
          "then replay the same request from inside the page (`fetch` + FormData, page cookies). The replay answer is the data, the UI answer is its cross-check; they must be identical.",
          "- Detail pages: 2 opened (section 9); the run does not crawl them."]
    L += ["", "## 2. Captured request", ""]
    caps = s.get("captures", {})
    if caps:
        any_cap = next(iter(caps.values()))
        r = any_cap["request"]
        L += [f"`{r['method']} {r['url']}`, `{r['content_type']}`, body {r['body_bytes']} bytes.", ""]
        names = [f[0] for f in r["fields"]]
        L += _table(["field", *[f"value ({k})" for k in caps]], [
            [n, *[json.dumps([f[1] for f in c["request"]["fields"] if f[0] == n], ensure_ascii=False) for c in caps.values()]] for n in dict.fromkeys(names)])
        L += ["", "- **No size / limit / paging field exists in the request.** The server returns everything for the filter in one answer. "
              "Completeness is therefore proven by `hits.total` (`eq`) = number of hits = UI counter, not by a limit set high.",
              "- `fixedTags[]` (3 values) are hidden inputs sent on every search: **'Alle Standorte' means 'tagged with one of these three', not 'every record in the index'.** "
              "A site without any of the three tags cannot appear (see section 10).",
              f"- Type list in the UI: {json.dumps(s.get('form_state', {}).get('select_options', []), ensure_ascii=False)}."]
    L += ["", "## 3. Totals against the UI counters", ""]
    rows = []
    for k, d in s.get("datasets", {}).items():
        rows.append([k, caps.get(k, {}).get("ui_label", ""), d.get("total_value", "-"), d.get("total_relation", "-"), d.get("hits", "-"), d.get("ui_counter", "-"),
                     d.get("source_answer", "-"), d.get("ignored_hits", "-")])
    L += _table(["dataset", "UI label", "hits.total.value", "relation", "len(hits.hits)", "UI counter", "data from", "hits with `_ignored`"], rows)
    if s.get("counts"):
        c = s["counts"]
        L += ["", f"**HU sites (tag 'Kfz Prüfstelle'): {c['hu']}** · all listed: {c['all']} · non-HU: {c['non_hu']}."]
    if s.get("site_types"):
        L += ["", "## 4. Site types (by title)", ""]
        keys = list(dict.fromkeys(list(s["site_types"]["all"]) + list(s["site_types"]["hu"])))
        L += _table(["site_type", "operator", "all", "HU"], [[k, {"own_station": "TÜV Hessen", "partner": "Partner", "tuev_sued_auto_partner": "TÜV SÜD Auto Partner"}.get(k, "(none)"),
                                                           s["site_types"]["all"].get(k, 0), s["site_types"]["hu"].get(k, 0)] for k in keys])
        L += ["", f"LKW in the title: {s['lkw']['all']} in all, {s['lkw']['hu']} in HU. Tags: {json.dumps(s['tags'], ensure_ascii=False)}."]
    f = s.get("findings", {})
    L += ["", "## 5. Checks", ""]
    L += _table(["severity", "ok", "check", "detail"], [[c["severity"], "yes" if c["ok"] else "**NO**", c["check"], c["detail"]] for c in s.get("checks", [])])
    if s.get("failed_hard"):
        L += ["", "**Failed hard checks:** " + "; ".join(s["failed_hard"])]
    L += ["", "## 6. Flags and findings", ""]
    if f:
        L += [f"- **Unclassified titles ({len(f['unclassified'])}):** " + (json.dumps(f["unclassified"], ensure_ascii=False) if f["unclassified"] else "none"),
              f"- **Tag/title mismatches ({len(f['tag_title_mismatch'])}):** reported, not fixed.",
              *[f"  - uid {m['uid']} ({m['site_type']}, HU={m['hu']}): {m['reason']}" for m in f["tag_title_mismatch"]],
              f"- **LKW in title ({len(f['lkw_sites'])}):** " + json.dumps(f["lkw_sites"], ensure_ascii=False),
              f"- **Coordinates shared by different addresses ({len(f['coords_shared_with_other_address'])} group(s)):** " + json.dumps(f["coords_shared_with_other_address"], ensure_ascii=False),
              f"- **Coordinates outside the Germany box:** {f['coords_outside_germany']} (missing: {f['coords_missing']})",
              f"- **Coordinates far from the PLZ centroid:** {f['coords_far_from_plz']} (threshold {f.get('plz_distance', {}).get('threshold_km', '-')} km; distances: {f.get('plz_distance', {})}; check {f['plz_check']})",
              f"- **Addresses shared by several uids ({len(f['address_shared'])}):** " + json.dumps(f["address_shared"], ensure_ascii=False),
              f"- **Duplicate uids:** {f['duplicate_uids']} · duplicate detail URLs: {f['duplicate_detail_urls']} · booking `locationSign` shared by several uids: {f['shared_location_signs']}",
              f"- **PLZ not 5 digits / not in GeoNames:** {f['plz_not_5_digits']} / {f['plz_not_in_geonames']}",
              f"- **Missing phone ({len(f['missing_phone'])}):** uids {f['missing_phone']}",
              f"- **HU tag but no HU wording anywhere in the record ({len(f['hu_tag_without_hu_evidence'])}):** uids {f['hu_tag_without_hu_evidence']}",
              f"- **Rows flagged `needs_review` ({len(f['needs_review_uids'])}):** uids {f['needs_review_uids']}",
              f"- **HU bounding box:** {f['hu_bbox']}"]
        L += ["", "## 7. Diff: 'Alle Standorte' against 'Kfz Prüfstelle'", ""]
        d = s.get("diff") or {}
        L += [f"- Only in 'Alle Standorte' (non-HU): {len(d.get('only_in_all', []))} · only in the HU list: {len(d.get('only_in_hu', []))} (must be 0) · "
              f"in both: {d.get('common', '-')}, of which different: {len(d.get('common_but_different', []))}"]
        L += _table(["uid", "title", "tags", "HU evidence in record"], [[x["uid"], x["title"], x["tags"], x["hu_evidence"] or "none"] for x in f["non_hu_sites"]])
    L += ["", "## 8. Available fields", ""]
    fr = s.get("fields", {})
    if fr:
        L += [f"- Hit level: {json.dumps(fr['hit_fields'])}", f"- `_source` (fill, of {s['counts']['all']}): {json.dumps(fr['source_fields'])}",
              f"- `_source.searchable_meta`: {json.dumps(fr['searchable_meta_fields'])}",
              "- Parsed from the preview HTML: fax, mobile, opening hours (`mapPreview`), HU booking link and its `locationSign`, contact URL, route URL. "
              "Notices from `infotext_contents`; services text from `infotext`.",
              "- Non-empty values per CSV column (all rows): " + ", ".join(f"{k} {v}" for k, v in fr["export_fill"].items())]
    L += ["", "## 9. Detail pages (2 opened, not crawled)", ""]
    for p in s.get("detail_pages", []):
        L += [f"- `{p['url']}` HTTP {p['status']}: {json.dumps(p['description'], ensure_ascii=False)}"]
    if not s.get("detail_pages"):
        L += ["- none"]
    L += ["", "## 10. Fragile points", "",
          "- Cloudflare: the page sets `cf_clearance` through a passive JavaScript check; a stricter rule (interactive challenge) would stop headless runs. The run then retries headed once and fails if that is blocked too.",
          "- The run depends on the page markup: `form.location-search-form`, the type `<select name='tags[]'>`, the counter `.location-search-results-count`, the banner button `a.cc-deny`. A redesign fails the run with a clear message.",
          "- The three hidden `fixedTags[]` define 'Alle Standorte'. If TÜV Hessen adds a tag, the counts change without any error; the request-field check fails the run when the fixed tags change.",
          "- The UI counter is `hits.hits.length` computed by the page script: it proves the page and our copy agree, not that the index holds no further records. HU stations inside workshops that TÜV Hessen does not list are invisible to any locator scrape.",
          "- Classification is by title prefix only; a title that changes wording becomes `unclassified` and is listed in section 6.",
          "- Some hits carry `_ignored` (Elasticsearch could not index a field, here `infotext_contents.bodytext.keyword`); harmless for the data we read.",
          "- `is_service_center` is missing on records outside the Kfz list (it is not always '1'); it is kept as delivered and not used."]
    if s.get("written"):
        L += ["", "## 11. Files", ""] + [f"- `{v['file']}`: {v['rows']} rows" for v in s["written"].values()]
        L += [f"- `{paths.RAW_FILE}`, `{paths.QUERY_LOG_FILE}`, `{paths.RUN_SUMMARY_JSON}`, `{paths.RUN_SUMMARY_MD}`" + (", `raw/detail/`" if s.get("detail_pages") else "")]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tuev_hessen.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=RUN_MODES)
    ap.add_argument("--confirm", action="store_true", help="needed for 'full': opens the live locator")
    ap.add_argument("--headed", action="store_true", help="start with a visible browser instead of headless")
    ap.add_argument("--no-fallback", action="store_true", help="do not retry headed when headless is blocked")
    ap.add_argument("--source", help="rebuild: the run folder to rebuild from")
    args = ap.parse_args(argv)
    res = run(args.mode, confirm_full_run=args.confirm, source_run=args.source, headless=not args.headed,
              fallback_headed=not args.no_fallback)
    print(f"\n{res.status}: {res.summary['outcome']}")
    print(f"run folder: {res.run_dir}")
    if res.summary.get("counts"):
        print(f"counts: {res.summary['counts']}")
    for c in res.summary.get("checks", []):
        if not c["ok"] and c["severity"] != "info":
            print(f"  [{c['severity']}] {c['check']}: {c['detail']}")
    if res.summary.get("error"):
        print(f"ERROR: {res.summary['error']}")
    return 0 if res.status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
