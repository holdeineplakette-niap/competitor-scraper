"""One-off live recon of fsp.de: full location list via r=1000 + pagination, r=2000 check, sitemap cross-check.

Run (live requests, only when you mean it):
    .venv\\Scripts\\python.exe -m fsp.recon                       # all-services list, r=2000 check, sitemap
    .venv\\Scripts\\python.exe -m fsp.recon --stages all,sitemap  # choose stages
    .venv\\Scripts\\python.exe -m fsp.recon --stages all,hu,r2000,sitemap --allow-robots-disallowed

The HU-filtered list (`hu`, parameter d=hauptuntersuchung) is disallowed by fsp.de's robots.txt, so it only runs
with --allow-robots-disallowed (a deliberate decision of the project owner, never a default).

Output goes to a recon run folder: 1_scrapers/fsp/test_output/<date>_<time>_recon/ (see the storage convention).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from core import paths

from fsp import parse, robots
from fsp.fetch import BASE, BlockedError, FetchError, FspClient, Response
from fsp.report import render_report

SLUG = "fsp"
LIST_URL = f"{BASE}/standorte"
SITEMAP_URL = f"{BASE}/sitemap.xml"
CENTER = {"lat": 51.1634, "lng": 10.4477}  # geographic centre of Germany
MAX_PAGES = 50
STAGES = ("all", "hu", "r2000", "sitemap")
MAX_DETAIL_SAMPLES = 5

COLUMNS = [
    "source", "scraped_at", "query_filter", "query_url", "page", "position_on_page", "slug", "detail_url",
    "name", "type_badges", "offers_hu", "distance_km", "distance_raw",
    "street", "plz", "city", "address_raw", "maps_url",
    "email", "phone", "phone_raw", "website_url", "booking_url",
    "hours_mo", "hours_di", "hours_mi", "hours_do", "hours_fr", "hours_sa", "hours_so", "hours_other_days",
    "opening_hours_raw", "has_photo", "image_url", "other_icon_labels", "other_links",
    "detail_href_raw", "email_raw_href", "parse_warnings", "duplicate_slug_in_run", "card_text_raw",
]


class PaginationError(RuntimeError):
    """`&page=N` did not return a different page. Stop and report; do not try anything clever."""


class Guard:
    """Checks every URL against robots.txt before it is fetched."""

    def __init__(self, disallow: list[str], allow: list[str], allowed: bool):
        self.disallow, self.allow, self.allowed = disallow, allow, allowed

    def check(self, url: str, params: dict | None) -> None:
        full = requests.Request("GET", url, params=params).prepare().url
        p = urlparse(full)
        rule = robots.disallowing_rule(p.path + (f"?{p.query}" if p.query else ""), self.disallow, self.allow)
        if rule and not self.allowed:
            raise robots.RobotsDisallowedError(
                f"{full} is disallowed by robots.txt (rule '{rule}'). Not fetched. "
                "Re-run with --allow-robots-disallowed only if the project owner decided so."
            )


class RunLog:
    """query_log.csv: one line per request."""

    FIELDS = ["time", "label", "url", "status", "bytes", "elapsed_s", "attempts", "cards", "note"]

    def __init__(self, path: Path | None):
        self.path = path  # None: keep the rows in memory only (offline rebuild has nothing to log)
        self.rows: list[dict] = []

    def add(self, **kw) -> None:
        self.rows.append({k: kw.get(k, "") for k in self.FIELDS})
        if self.path is None:
            return
        with self.path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS, delimiter=";")
            w.writeheader()
            w.writerows(self.rows)


def say(msg: str) -> None:
    print(msg, flush=True)


def fetch_logged(client: FspClient, guard: Guard, log: RunLog, label: str, url: str, params=None, note=""):
    guard.check(url, params)
    resp = client.get(url, params=params)
    log.add(
        time=datetime.now().isoformat(timespec="seconds"), label=label, url=resp.url, status=resp.status,
        bytes=len(resp.text.encode("utf-8")), elapsed_s=resp.elapsed_s, attempts=resp.attempts, note=note,
    )
    return resp


class OfflineClient:
    """Serves the raw files of earlier recon runs with the FspClient interface: re-parse without new requests."""

    requests_made = 0

    def __init__(self, source_runs):
        self.dirs = [Path(d) for d in source_runs]

    def _read(self, rels: list[str]) -> str:
        for d in self.dirs:
            for rel in rels:
                if (d / rel).exists():
                    return (d / rel).read_text(encoding="utf-8")
                if (d / (rel + ".gz")).exists():  # production runs store the 2.8 MB list pages gzipped
                    with gzip.open(d / (rel + ".gz"), "rt", encoding="utf-8") as f:
                        return f.read()
        raise FetchError(f"{rels[0]} is not in the source run folder(s) {[str(d) for d in self.dirs]}")

    def get(self, url: str, params: dict | None = None) -> Response:
        # two layouts: recon runs (raw/, sitemap/, detail/) and fsp.run runs (raw/pages/, raw/sitemap/, raw/detail/)
        params = params or {}
        if url.endswith("robots.txt"):
            rel = ["raw/robots.txt"]
        elif url.endswith("sitemap.xml"):
            rel = ["sitemap/sitemap.xml", "raw/sitemap/sitemap.xml"]
        elif "/standorte/" in url:
            name = f"{url.rsplit('/', 1)[1]}.html"
            rel = [f"detail/{name}", f"raw/detail/{name}"]
        else:
            label = "hu" if "d" in params else ("all_r2000" if params.get("r") == 2000 else "all")
            name = f"{label}_page_{params['page']:02d}.html"
            rel = [f"raw/{name}", f"raw/pages/{name}"]
        full = requests.Request("GET", url, params=params).prepare().url
        return Response(url=full, status=200, text=self._read(rel), elapsed_s=0.0, attempts=1,
                        content_type="text/html", cf_mitigated=None)


def crawl_list(client, guard, log, label, params, raw_dir: Path | None, scraped_at: str, query_filter: str,
               max_pages: int = MAX_PAGES, gzip_raw: bool = False) -> dict:
    """Loop page=1,2,... until an empty page, a page with only known slugs, or the safety cap.

    raw_dir=None (offline rebuild): nothing is written, the raw files already exist in the source run.
    gzip_raw: store each page as .html.gz (production runs; a page is 2.8 MB of which 2.3 MB is one SVG).
    """
    rows: list[dict] = []
    pages: list[dict] = []
    seen: set[str] = set()
    stop_reason = f"safety cap of {max_pages} pages reached"
    for n in range(1, max_pages + 1):
        resp = fetch_logged(client, guard, log, f"{label}_page_{n:02d}", LIST_URL, {**params, "page": n})
        if raw_dir:  # raw before parsing
            name = f"{label}_page_{n:02d}.html"
            if gzip_raw:
                with gzip.open(raw_dir / (name + ".gz"), "wt", encoding="utf-8") as f:
                    f.write(resp.text)
            else:
                (raw_dir / name).write_text(resp.text, encoding="utf-8")
        lp = parse.parse_list_page(resp.text, n)
        if not lp.cards and not lp.has_results_heading:
            raise BlockedError(f"{resp.url}: no result cards and no results heading, so this is not a real result page")
        slugs = [c["slug"] for c in lp.cards if c["slug"]]
        new = [s for s in slugs if s not in seen]
        pages.append({
            "page": n, "cards": len(lp.cards), "new_slugs": len(new), "declared_pages": lp.declared_pages,
            "active_page": lp.active_page, "total_text": lp.total_text, "url": resp.url,
        })
        log.rows[-1]["cards"] = len(lp.cards)
        say(f"  [{label}] page {n}: {len(lp.cards)} cards, {len(new)} new slugs, "
            f"pagination bar says {lp.declared_pages} pages")
        if n == 2 and (not new or len(new) < len(slugs)):
            raise PaginationError(
                f"page 2 of '{label}' has {len(slugs)} cards of which only {len(new)} are new "
                f"(page 1 and page 2 should not share slugs). Pagination via GET is NOT proven. Stopping."
            )
        if not lp.cards:
            stop_reason = f"page {n} has zero cards (a real, empty result page)"
            break
        for c in lp.cards:
            c.update(source="FSP", scraped_at=scraped_at, query_filter=query_filter, query_url=resp.url)
            rows.append(c)
        if not new:
            stop_reason = f"page {n} returned only slugs already seen"
            break
        seen.update(new)
    counts = {}
    for r in rows:
        counts[r["slug"]] = counts.get(r["slug"], 0) + 1
    for r in rows:
        r["duplicate_slug_in_run"] = counts.get(r["slug"], 0) > 1 if r["slug"] else False
    return {
        "label": label, "params": params, "query_filter": query_filter, "rows": rows, "pages": pages,
        "stop_reason": stop_reason,
        "declared_pages": pages[0]["declared_pages"] if pages else None,
        "pages_with_cards": sum(1 for p in pages if p["cards"]),
    }


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    extra = sorted({k for r in rows for k in r} - set(columns))
    cols = columns + extra
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel shows umlauts
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def check_r2000(client, guard, log, raw_dir, r1000_crawl) -> dict:
    params = {**CENTER, "r": 2000, "page": 1}
    resp = fetch_logged(client, guard, log, "all_r2000_page_01", LIST_URL, params)
    if raw_dir:
        (raw_dir / "all_r2000_page_01.html").write_text(resp.text, encoding="utf-8")
    lp = parse.parse_list_page(resp.text, 1)
    if not lp.cards and not lp.has_results_heading:
        raise BlockedError(f"{resp.url}: not a real result page")
    log.rows[-1]["cards"] = len(lp.cards)
    p1 = r1000_crawl["pages"][0]
    slugs_2000 = [c["slug"] for c in lp.cards]
    slugs_1000 = [c["slug"] for c in r1000_crawl["rows"] if c["page"] == 1]
    dists = [c["distance_km"] for c in lp.cards if c["distance_km"] is not None]
    return {
        "cards": len(lp.cards), "declared_pages": lp.declared_pages, "declared_pages_r1000": p1["declared_pages"],
        "total_text": lp.total_text, "same_page1_slugs": slugs_2000 == slugs_1000,
        "same_page1_slug_set": set(slugs_2000) == set(slugs_1000), "max_distance_km_page1": max(dists, default=None),
        "differs": lp.declared_pages != p1["declared_pages"] or lp.total_text != p1["total_text"],
    }


def run_sitemap(client, guard, log, raw_dir: Path | None, searches: dict[str, set[str]]) -> dict:
    sm_dir = raw_dir.parent / "sitemap" if raw_dir else None
    resp = fetch_logged(client, guard, log, "sitemap", SITEMAP_URL)
    if sm_dir:
        sm_dir.mkdir(exist_ok=True)
        (sm_dir / "sitemap.xml").write_text(resp.text, encoding="utf-8")
    kind, entries = parse.parse_sitemap(resp.text)
    files = ["sitemap.xml"]
    all_entries: list[dict] = []
    if kind == "index":
        for i, e in enumerate(entries, start=1):
            child = fetch_logged(client, guard, log, f"sitemap_child_{i:02d}", e["loc"])
            name = f"sitemap_child_{i:02d}.xml"
            if sm_dir:
                (sm_dir / name).write_text(child.text, encoding="utf-8")
            files.append(name)
            ckind, centries = parse.parse_sitemap(child.text)
            if ckind != "urlset":
                raise RuntimeError(f"nested sitemap index in {e['loc']}: not handled, stop and look")
            all_entries += [{**c, "from": e["loc"]} for c in centries]
    else:
        all_entries = [{**e, "from": SITEMAP_URL} for e in entries]
    slugs: dict[str, dict] = {}
    other_urls = 0
    for e in all_entries:
        s = parse.standorte_slug(e["loc"])
        if s:
            slugs.setdefault(s, e)
        else:
            other_urls += 1
    return {"kind": kind, "files": files, "total_urls": len(all_entries), "standorte_slugs": slugs,
            "non_standorte_urls": other_urls, "sm_dir": sm_dir}


def inspect_detail(client, guard, log, raw_dir: Path | None, slug: str) -> dict:
    from bs4 import BeautifulSoup

    det_dir = raw_dir.parent / "detail" if raw_dir else None
    if det_dir:
        det_dir.mkdir(exist_ok=True)
    url = f"{BASE}/standorte/{slug}"
    guard.check(url, None)
    try:
        resp = client.get(url)
    except FetchError as exc:
        log.add(time=datetime.now().isoformat(timespec="seconds"), label=f"detail_{slug}", url=url, note=str(exc))
        return {"slug": slug, "error": str(exc)}
    log.add(time=datetime.now().isoformat(timespec="seconds"), label=f"detail_{slug}", url=resp.url,
            status=resp.status, bytes=len(resp.text.encode("utf-8")), elapsed_s=resp.elapsed_s, attempts=resp.attempts)
    if det_dir:
        (det_dir / f"{slug}.html").write_text(resp.text, encoding="utf-8")
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "svg", "nav", "footer", "header"]):
        tag.decompose()
    main = soup.find("main") or soup
    badges = [parse.clean(b.get_text(" ")) for b in main.select("span.badge")]
    h1 = main.find("h1")
    return {
        "row": parse.parse_detail(resp.text, slug),
        "slug": slug, "status": resp.status, "title": parse.clean(soup.title.get_text()) if soup.title else "",
        "h1": parse.clean(h1.get_text(" ")) if h1 else "", "badges": badges,
        "text_excerpt": parse.clean(main.get_text(" "))[:500],
    }


def run(stages=("all", "r2000", "sitemap"), allow_robots_disallowed=False, client=None, root=None, now=None,
        max_pages=MAX_PAGES, mode="recon", source_runs=None) -> Path:
    """mode='recon': live requests, raw HTML saved to test_output/<run>/raw.
    mode='rebuild': offline, re-parses the raw files of `source_runs`; writes only derived files to output/."""
    unknown = set(stages) - set(STAGES)
    if unknown:
        raise ValueError(f"unknown stages {sorted(unknown)}; choose from {STAGES}")
    if mode not in ("recon", "rebuild"):
        raise ValueError("mode must be 'recon' (live) or 'rebuild' (offline)")
    if mode == "rebuild":
        if not source_runs:
            raise ValueError("mode 'rebuild' needs source_runs (the recon run folder(s) with the raw files)")
        client = client or OfflineClient(source_runs)
    started = now or datetime.now()
    run_dir = paths.new_run_dir(SLUG, mode, started, root)
    raw_dir = None
    if mode == "recon":
        raw_dir = run_dir / "raw"
        raw_dir.mkdir()
    date = f"{started:%Y-%m-%d}"
    scraped_at = started.isoformat(timespec="seconds")
    client = client or FspClient()
    log = RunLog(run_dir / paths.QUERY_LOG_FILE if mode == "recon" else None)
    result: dict = {"started": scraped_at, "mode": mode, "stages": list(stages), "run_dir": str(run_dir),
                    "status": "FAIL", "error": "", "crawls": {}, "date": date,
                    "source_runs": [str(s) for s in (source_runs or [])]}
    say(f"Run folder: {run_dir}")
    try:
        # robots.txt first: it decides what we may fetch
        robots_resp = client.get(f"{BASE}/robots.txt")
        log.add(time=datetime.now().isoformat(timespec="seconds"), label="robots", url=robots_resp.url,
                status=robots_resp.status, bytes=len(robots_resp.text), elapsed_s=robots_resp.elapsed_s)
        if raw_dir:
            (raw_dir / "robots.txt").write_text(robots_resp.text, encoding="utf-8")
        disallow, allow, sitemaps = robots.parse_rules(robots_resp.text)
        guard = Guard(disallow, allow, allow_robots_disallowed)
        result["robots"] = {"text": robots_resp.text, "disallow": disallow, "allow": allow, "sitemaps": sitemaps,
                            "allowed_override": allow_robots_disallowed}
        result["method"] = {"name": "requests (plain HTTP session)", "playwright_needed": False}
        if mode == "rebuild":  # the live facts belong to the source run: carry them over, do not invent them
            for src in source_runs:
                f = Path(src) / paths.RUN_SUMMARY_JSON
                if f.exists():
                    old = json.loads(f.read_text(encoding="utf-8"))
                    result["method"] = old.get("method", result["method"])
                    result["source_requests_made"] = result.get("source_requests_made", 0) + old.get("requests_made", 0)

        if "all" in stages:
            say("All-services list (r=1000):")
            result["crawls"]["all"] = crawl_list(client, guard, log, "all", {**CENTER, "r": 1000}, raw_dir,
                                                 scraped_at, "none", max_pages)
        if "hu" in stages:
            say("HU-filtered list (r=1000, d=hauptuntersuchung):")
            result["crawls"]["hu"] = crawl_list(client, guard, log, "hu",
                                                {**CENTER, "r": 1000, "d": "hauptuntersuchung"}, raw_dir,
                                                scraped_at, "hauptuntersuchung", max_pages)
        if "r2000" in stages:
            if "all" not in result["crawls"]:
                raise ValueError("stage r2000 compares against the 'all' list: run it with stage 'all'")
            say("Robustness check r=2000, page 1:")
            result["r2000"] = check_r2000(client, guard, log, raw_dir, result["crawls"]["all"])
        # scope rule (Nikos, 2026-10-06): a site that offers HU counts, whatever its badge. "Offers HU" = it is in the
        # HU-filtered list (d=hauptuntersuchung). Without that list the column says "not run", never a guess.
        hu_slugs = {r["slug"] for r in result["crawls"]["hu"]["rows"]} if "hu" in result["crawls"] else None
        for crawl in result["crawls"].values():
            for r in crawl["rows"]:
                r["offers_hu"] = "not run" if hu_slugs is None else r["slug"] in hu_slugs
        for key, crawl in result["crawls"].items():
            write_csv(run_dir / f"fsp_{key}_{date}.csv", crawl["rows"], COLUMNS)

        if "sitemap" in stages:
            say("Sitemap:")
            searches = {k: {r["slug"] for r in c["rows"]} for k, c in result["crawls"].items()}
            sm = run_sitemap(client, guard, log, raw_dir, searches)
            result["sitemap"] = {k: v for k, v in sm.items() if k not in ("standorte_slugs", "sm_dir")}
            sm_slugs = set(sm["standorte_slugs"])
            union_search = set().union(*searches.values()) if searches else set()
            only_sitemap = sorted(sm_slugs - union_search) if searches else []
            result["sitemap"]["slugs"] = sm["standorte_slugs"]
            result["sitemap"]["only_sitemap"] = only_sitemap
            rows = []
            names = {r["slug"]: r for c in result["crawls"].values() for r in c["rows"]}
            for s in sorted(sm_slugs | union_search):
                rows.append({
                    "slug": s, "in_sitemap": s in sm_slugs,
                    "in_all_search": (s in searches["all"]) if "all" in searches else "not run",
                    "in_hu_search": (s in searches["hu"]) if "hu" in searches else "not run",
                    "sitemap_lastmod": sm["standorte_slugs"].get(s, {}).get("lastmod", ""),
                    "sitemap_url": sm["standorte_slugs"].get(s, {}).get("loc", ""),
                    "name_in_search": names.get(s, {}).get("name", ""),
                })
            write_csv(run_dir / f"fsp_sitemap_slugs_{date}.csv", rows,
                      ["slug", "in_sitemap", "in_all_search", "in_hu_search", "sitemap_lastmod", "sitemap_url",
                       "name_in_search"])
            sample = random.Random(42).sample(only_sitemap, min(MAX_DETAIL_SAMPLES, len(only_sitemap)))
            result["sitemap"]["detail_samples"] = []
            for s in sample:
                say(f"  detail page of sitemap-only slug: {s}")
                result["sitemap"]["detail_samples"].append(inspect_detail(client, guard, log, raw_dir, s))
            # Sites the locator does not list but that exist in the sitemap: one extra row each in the 'all' CSV,
            # clearly marked, with HU decided from the site's own page (a site that offers HU counts).
            extra = []
            for d in result["sitemap"]["detail_samples"]:
                if "row" not in d:
                    continue
                r = d["row"]
                hu = r["hu_on_detail_page"]
                r.update(source="FSP", scraped_at=scraped_at, query_filter="sitemap_only_detail_page",
                         query_url=r["detail_url"], offers_hu=f"{hu} (detail page)", duplicate_slug_in_run=False)
                extra.append(r)
            if "all" in result["crawls"]:
                write_csv(run_dir / f"fsp_all_{date}.csv", result["crawls"]["all"]["rows"] + extra, COLUMNS)
        result["status"] = "DONE"
    except Exception as exc:  # the run folder keeps everything fetched so far; the summary names the failure
        result["error"] = f"{type(exc).__name__}: {exc}"
        say(f"STOPPED: {result['error']}")
        raise
    finally:
        result["requests_made"] = client.requests_made
        slim = {k: v for k, v in result.items() if k not in ("crawls", "robots", "sitemap", "r2000")}
        (run_dir / paths.RUN_SUMMARY_JSON).write_text(
            json.dumps({**slim, "crawls": {k: {kk: vv for kk, vv in c.items() if kk != "rows"}
                                           for k, c in result["crawls"].items()}},
                       indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        if result["crawls"]:
            try:
                (run_dir / f"fsp_run_report_{date}.md").write_text(render_report(result), encoding="utf-8")
            except Exception:
                if result["status"] == "DONE":
                    raise  # a finished run must produce its report; a failed run keeps its original error
    say(f"Done. Report: {run_dir / f'fsp_run_report_{date}.md'}")
    return run_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Recon of fsp.de: live requests, or an offline rebuild of saved raw HTML.")
    ap.add_argument("--stages", default="all,r2000,sitemap", help=f"comma list from {STAGES}")
    ap.add_argument("--allow-robots-disallowed", action="store_true",
                    help="allow URLs that robots.txt disallows (needed for the HU filter d=...)")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    ap.add_argument("--rebuild-from", nargs="+", metavar="RUN_FOLDER",
                    help="NO live requests: re-parse the raw files of these recon run folders into a new rebuild run")
    args = ap.parse_args(argv)
    run(tuple(s.strip() for s in args.stages.split(",") if s.strip()),
        allow_robots_disallowed=args.allow_robots_disallowed, max_pages=args.max_pages,
        mode="rebuild" if args.rebuild_from else "recon", source_runs=args.rebuild_from)
    return 0


if __name__ == "__main__":
    sys.exit(main())
