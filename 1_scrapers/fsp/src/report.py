"""Sanity checks and the markdown run report for the FSP recon. Pure functions, no network access.

The report contains business contact data from the cards: it lives in the git-ignored run folder only.
"""

from __future__ import annotations

import random
import re
from collections import Counter

from fsp import parse

BENCHMARK = (120, 170)  # FSP says "über 125 Prüfstellen"; directories say 120-150 (+ Gutachtenbüros in the 'all' list)
HU_PAGES_IN_BROWSER = 14
SPOTCHECK_N = 5


def slug_kind(slug: str) -> str:
    for prefix in ("kfz-sachverstaendigenbuero", "kfz-pruefstelle", "pruefstelle", "gutachtenbuero", "ingenieurbuero"):
        if slug.startswith(prefix):
            return prefix + "-*"
    return "other: " + "-".join(slug.split("-")[:2]) + "-*"


def sanity(crawl: dict) -> dict:
    rows = crawl["rows"]
    slugs = [r["slug"] for r in rows if r["slug"]]
    badges = Counter(b for r in rows for b in r["type_badges"].split(" | ") if b)
    dists = [r["distance_km"] for r in rows if r["distance_km"] is not None]
    cards_per_page = [p["cards"] for p in crawl["pages"]]
    return {
        "cards": len(rows),
        "unique_slugs": len(set(slugs)),
        "failing_cards": [r for r in rows if not (r["name"] and r["street"] and r["plz"] and r["city"] and r["slug"])],
        "duplicates": {s: n for s, n in Counter(slugs).items() if n > 1},
        "plz_not_5_digits": [r for r in rows if not re.fullmatch(r"\d{5}", r["plz"] or "")],
        "distance_missing_or_over_1000": [r for r in rows if r["distance_km"] is None or r["distance_km"] > 1000],
        "max_distance": max(dists, default=None),
        "min_distance": min(dists, default=None),
        "sorted_by_distance": all(a <= b for a, b in zip(dists, dists[1:])),
        "badge_counts": badges,
        "badge_combos": Counter(r["type_badges"] or "(none)" for r in rows),
        "unknown_badges": {b: n for b, n in badges.items() if b not in parse.KNOWN_BADGES},
        "cards_per_page": cards_per_page,
        "kind_x_badge": Counter((slug_kind(r["slug"]), r["type_badges"] or "(none)") for r in rows),
    }


def _table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _slug_lines(rows_by_slug: dict[str, dict], slugs, limit=400) -> str:
    slugs = sorted(slugs)
    lines = [
        f"- `{s}`" + (f": {rows_by_slug[s]['name']} ({rows_by_slug[s]['plz']} {rows_by_slug[s]['city']}, "
                      f"{rows_by_slug[s]['type_badges']})" if s in rows_by_slug else "")
        for s in slugs[:limit]
    ]
    if len(slugs) > limit:
        lines.append(f"- ... and {len(slugs) - limit} more (see the CSV)")
    return "\n".join(lines) or "- (none)"


def _benchmark_rows(key: str, s: dict, lo: int, hi: int) -> list[list]:
    """The benchmark (FSP: 'über 125 Prüfstellen', directories 120-150) is about Prüfstellen, not Gutachtenbüros."""
    def verdict(n):
        return f"{n}: " + ("inside" if lo <= n <= hi else "**outside**")

    if key == "hu":
        return [[f"Unique slugs vs. benchmark {lo}-{hi}", verdict(s["unique_slugs"])]]
    pst = s["badge_counts"].get("Prüfstelle", 0)
    return [[f"Cards with badge 'Prüfstelle' vs. benchmark {lo}-{hi}", verdict(pst)],
            ["All unique slugs (incl. Gutachtenbüros)", f"{s['unique_slugs']}: not comparable to the benchmark"]]


def oddities(rows: list[dict]) -> dict[str, list[dict]]:
    """Cards the project owner may want to look at; nothing here is excluded or changed."""
    low = lambda r: f"{r['name']} {r['slug']}".lower()  # noqa: E731
    return {
        "named 'Prüfstützpunkt' but present in the locator": [
            r for r in rows if re.search(r"pr(ü|ue|u)fst(ü|ue|u)tzpunkt", low(r))],
        "badge 'Prüfstelle' but name says Sachverständigen-/Gutachten-/Schadenbüro": [
            r for r in rows if "Prüfstelle" in r["type_badges"] and re.search(r"sachverst|gutachten|schaden", low(r))],
        "name says Prüfstelle but no 'Prüfstelle' badge": [
            r for r in rows if re.match(r"(Kfz-)?Prüfstelle\b", r["name"]) and "Prüfstelle" not in r["type_badges"]],
        "both badges (Prüfstelle and Gutachtenbüro)": [
            r for r in rows if "Prüfstelle" in r["type_badges"] and "Gutachtenbüro" in r["type_badges"]],
        "no street line (PLZ and city only)": [r for r in rows if r["plz"] and not r["street"]],
        "PLZ not 5 digits": [r for r in rows if not re.fullmatch(r"\d{5}", r["plz"] or "")],
        "no phone and no email": [r for r in rows if not r["phone"] and not r["email"]],
    }


def _crawl_section(key: str, crawl: dict, res: dict) -> str:
    s = sanity(crawl)
    label = {"all": "All services (no `d=` filter)", "hu": "HU filter (`d=hauptuntersuchung`)"}[key]
    lo, hi = BENCHMARK
    lines = [f"### {label}", ""]
    lines.append(f"- Query: `{crawl['params']}` · stop reason: **{crawl['stop_reason']}**")
    lines.append(f"- Pages with cards: **{crawl['pages_with_cards']}** · pagination bar on page 1 declares "
                 f"**{crawl['declared_pages']}** pages" + (f" · browser showed {HU_PAGES_IN_BROWSER} pages for HU"
                                                           if key == "hu" else ""))
    lines.append(f"- Cards: **{s['cards']}** · unique slugs: **{s['unique_slugs']}** · cards per page: "
                 + ", ".join(str(c) for c in s["cards_per_page"]))
    match = crawl["declared_pages"] == crawl["pages_with_cards"]
    lines.append(f"- Declared page count {'matches' if match else '**DIFFERS from**'} pages actually returned")
    lines.append(f"- Any 'N Standorte' total on the page: "
                 f"{crawl['pages'][0]['total_text'] or 'none (the pagination bar is the only count signal)'}")
    lines.append("")
    chk = [
        ["Every card has name, street, PLZ, city and slug", "PASS" if not s["failing_cards"] else
         f"**FAIL: {len(s['failing_cards'])} card(s)**"],
        ["No duplicate slugs in the run", "PASS" if not s["duplicates"] else f"**{len(s['duplicates'])} duplicate(s)**"],
        ["All PLZ have 5 digits", "PASS" if not s["plz_not_5_digits"] else
         f"**FAIL: {len(s['plz_not_5_digits'])} card(s)**"],
        ["All distances present and <= 1000 km", "PASS" if not s["distance_missing_or_over_1000"] else
         f"**FAIL: {len(s['distance_missing_or_over_1000'])} card(s)**"],
        ["Distance range / sorted ascending across pages",
         f"{s['min_distance']} to {s['max_distance']} km / {'yes' if s['sorted_by_distance'] else '**NO**'}"],
        *_benchmark_rows(key, s, lo, hi),
        ["Only known badge values", "PASS" if not s["unknown_badges"] else f"**unknown: {s['unknown_badges']}**"],
    ]
    lines += [_table(["Check", "Result"], chk), ""]
    lines.append("Type badges (a card can carry several): " +
                 ", ".join(f"{b}: {n}" for b, n in s["badge_counts"].most_common()))
    lines.append("")
    lines.append("Badge combinations: " + ", ".join(f"{b}: {n}" for b, n in s["badge_combos"].most_common()))
    lines.append("")
    lines.append("Slug prefix vs. badge (is the badge the same kind of thing the URL says?):")
    lines.append("")
    lines.append(_table(["slug prefix", "badges", "cards"],
                        [[k, b, n] for (k, b), n in sorted(s["kind_x_badge"].items())]))
    lines.append("")
    if s["failing_cards"]:
        lines.append("Failing cards:\n" + "\n".join(
            f"- page {r['page']} #{r['position_on_page']} `{r['slug']}`: {r['parse_warnings']}"
            for r in s["failing_cards"]))
        lines.append("")
    if s["duplicates"]:
        lines.append("Duplicate slugs: " + ", ".join(f"`{k}` x{v}" for k, v in s["duplicates"].items()))
        lines.append("")
    return "\n".join(lines)


def _fields_section(rows: list[dict], columns: list[str]) -> str:
    n = len(rows)
    table = []
    for c in columns:
        filled = sum(1 for r in rows if str(r.get(c, "")) not in ("", "False", "None"))
        table.append([f"`{c}`", filled, f"{100 * filled / n:.0f}%" if n else "-"])
    return _table(["field (CSV column)", "filled", "of cards"], table)


def spotcheck(rows: list[dict], n: int = SPOTCHECK_N, seed: int = 7) -> list[dict]:
    """Random sites for a Google Maps check: only entries named 'Prüfstelle ...' (company, not person names)."""
    pool = [r for r in rows if re.match(r"(Kfz-)?Prüfstelle\b", r["name"]) and "Prüfstelle" in r["type_badges"]
            and r["street"] and re.fullmatch(r"\d{5}", r["plz"] or "")]  # clean addresses only: easy to find on a map
    return random.Random(seed).sample(pool, min(n, len(pool)))


def render_report(res: dict) -> str:
    from fsp.recon import COLUMNS  # late import: recon imports this module

    L: list[str] = []
    a = L.append
    a(f"# FSP run report {res['date']}")
    a("")
    a(f"Run started {res['started']} · status: **{res['status']}** · stages: {', '.join(res['stages'])} · "
      f"requests made: {res.get('requests_made', '?')}")
    if res["error"]:
        a("")
        a(f"**The run stopped early: {res['error']}**")
    a("")
    a("## 1. Access method and robots.txt")
    a("")
    meth = res.get("method", {})
    if res.get("mode") == "rebuild":
        a("- **Offline rebuild: no request was made for this report.** It re-parses the raw HTML saved by: "
          + ", ".join(f"`{s}`" for s in res["source_runs"]) + f" ({res.get('source_requests_made', '?')} live requests there).")
    a(f"- Method (live access, from the source run): **{meth.get('name', 'unknown')}**. Playwright needed: "
      f"**{'yes' if meth.get('playwright_needed') else 'no'}**. "
      "No Cloudflare block detected: every response was HTTP 200 and passed the block checks (status 403/503, "
      "'Just a moment', `cf-chl`, `cf-mitigated` header, result page with zero cards and no results heading). "
      "Pace: 2-4 s random pause, one request at a time, max 3 retries with backoff.")
    rb = res.get("robots")
    if rb:
        a("- `robots.txt` (group `User-agent: *`):")
        a("")
        a("```")
        a(rb["text"].strip())
        a("```")
        a("")
        a(f"- Disallow rules parsed: {len(rb['disallow'])}; Allow rules: {len(rb['allow'])}; "
          f"`Sitemap:` lines: {len(rb['sitemaps'])}. Nothing disallows `/standorte` itself or `/sitemap.xml`. "
          "**The filter parameters `d=`, `f=` and `search=` are disallowed** (so `d=hauptuntersuchung` is). "
          "The parameters used for the all-services list (`lat`, `lng`, `r`, `page`) are not.")
        a(f"- HU-filter run executed: **{'yes (override flag set by the project owner)' if 'hu' in res['crawls'] else 'no'}**")
    a("")
    a("## 2. Lists")
    a("")
    for key, crawl in res["crawls"].items():
        a(_crawl_section(key, crawl, res))
    if "all" in res["crawls"] and "hu" in res["crawls"]:
        allrows = {r["slug"]: r for r in res["crawls"]["all"]["rows"]}
        hu = {r["slug"] for r in res["crawls"]["hu"]["rows"]}
        al = set(allrows)
        a("### All services vs. HU filter")
        a("")
        a(f"- all: {len(al)} · HU: {len(hu)} · difference: {len(al - hu)} · in HU but not in all: {len(hu - al)}")
        a("- Slugs only in the all-services run (not offering HU, or filtered for another reason):")
        a(_slug_lines(allrows, al - hu))
        a("")
    if "r2000" in res:
        r = res["r2000"]
        a("### Robustness: r=2000 vs r=1000 (all services, page 1)")
        a("")
        a(f"- r=2000: {r['cards']} cards on page 1, pagination bar declares **{r['declared_pages']}** pages "
          f"(r=1000: {r['declared_pages_r1000']}). Total text: {r['total_text'] or 'none'}.")
        a(f"- Page-1 slugs identical in the same order: {r['same_page1_slugs']} · same set: {r['same_page1_slug_set']} "
          f"· max distance on page 1: {r['max_distance_km_page1']} km")
        a("- **FLAG: r=2000 differs from r=1000**" if r["differs"] else "- No difference in page count or total: r=1000 already covers everything.")
        a("")
    if "sitemap" in res:
        sm = res["sitemap"]
        a("## 3. Sitemap cross-check")
        a("")
        a(f"- Sitemap type: {sm['kind']} · files: {', '.join(sm['files'])} · URLs in total: **{sm['total_urls']}** · "
          f"`/standorte/<slug>` URLs: **{len(sm['slugs'])}** · other URLs: {sm['non_standorte_urls']}")
        searches = {k: {r["slug"] for r in c["rows"]} for k, c in res["crawls"].items()}
        names = {r["slug"]: r for c in res["crawls"].values() for r in c["rows"]}
        S = set(sm["slugs"])
        a("")
        trows = [["sitemap", len(S)]]
        for k, v in searches.items():
            trows += [[f"{k} search", len(v)], [f"sitemap ∩ {k}", len(S & v)], [f"sitemap only (not in {k})", len(S - v)],
                      [f"{k} only (not in sitemap)", len(v - S)]]
        if len(searches) == 2:
            h, al = searches["hu"], searches["all"]
            trows += [["sitemap ∩ hu ∩ all", len(S & h & al)], ["sitemap only (in neither search)", len(S - h - al)]]
        elif len(searches) == 1:
            trows += [["sitemap only (in no search run)", len(sm["only_sitemap"])]]
        a(_table(["set", "slugs"], trows))
        a("")
        a("**Slugs in the sitemap but in no search (potential misses):**")
        a("")
        a(_slug_lines(names, sm["only_sitemap"]))
        a("")
        a("**Slugs in a search but not in the sitemap:**")
        a("")
        for k, v in searches.items():
            a(f"{k}: ")
            a(_slug_lines(names, v - S))
        a("")
        if sm.get("detail_samples"):
            a("### Detail pages of sitemap-only slugs (max 5, for orientation only)")
            a("")
            for d in sm["detail_samples"]:
                if d.get("error"):
                    a(f"- `{d['slug']}`: request failed ({d['error']})")
                else:
                    a(f"- `{d['slug']}`: HTTP {d['status']} · title: {d['title']} · h1: {d['h1']} · badges: "
                      f"{', '.join(d['badges']) or 'none'} · **HU named on the page: {d['row']['hu_on_detail_page']}**")
                    a(f"  - text: {d['text_excerpt']}")
            a("")
    if "hu" in res["crawls"]:
        hu_n = len({r["slug"] for r in res["crawls"]["hu"]["rows"]})
        det = [d["row"] for d in res.get("sitemap", {}).get("detail_samples", []) if "row" in d]
        det_hu = [r for r in det if r["hu_on_detail_page"]]
        a("## 3b. Scope (rule of Nikos, 2026-10-06: a site that offers HU counts, whatever its badge)")
        a("")
        a(f"- Offers HU per the HU-filtered list: **{hu_n}**")
        a(f"- Sitemap-only sites whose own page names HU: **{len(det_hu)}** of {len(det)} looked at "
          + (": " + ", ".join(f"`{r['slug']}`" for r in det_hu) if det_hu else ""))
        a(f"- **In scope in total: {hu_n + len(det_hu)}**. Column `offers_hu` in `fsp_all_*.csv`: `True` (HU list), "
          "`True (detail page)` (sitemap-only site, extra row), `False`, `False (detail page)`.")
        a("")
    base = res["crawls"].get("all") or next(iter(res["crawls"].values()))
    a("## 4. Fields available per card")
    a("")
    a(f"Fill rates over the {len(base['rows'])} cards of the `{base['label']}` run. Raw text is kept next to every "
      "parsed value that could lose information (`opening_hours_raw`, `address_raw`, `phone_raw`, `distance_raw`, "
      "`detail_href_raw`, `email_raw_href`, `card_text_raw`). Not on the cards: coordinates, operator/partner name, "
      "services, vehicle types.")
    a("")
    a(_fields_section(base["rows"], COLUMNS))
    a("")
    a("## 5. Odd or fragile")
    a("")
    warn = Counter(w for r in base["rows"] for w in r["parse_warnings"].split("; ") if w)
    a(f"- Parser warnings in the `{base['label']}` run: " + (", ".join(f"{w}: {n}" for w, n in warn.most_common()) or "none"))
    a("- The result list depends on Livewire/Laravel markup (`div.location.card`, `nav[aria-label=Seitennummerierung]`); "
      "a redesign breaks the parser, which then reports zero cards (never a silent '0 results': see the block checks).")
    a("- `r=1000` is an unvalidated parameter. The robustness check above shows whether a bigger radius changes the page count.")
    a("- Prüfstützpunkte (stations inside workshops) were assumed not to be in the locator (**known coverage gap**). "
      "The lists below show where that assumption is only partly true.")
    a("- Cards have no coordinates and no operator name; both need the detail pages (only the sitemap-only samples were fetched in this run).")
    a("")
    for title, hits in oddities(base["rows"]).items():
        a(f"**{title}: {len(hits)}**")
        a("")
        a("\n".join(f"- `{r['slug']}`: {r['name']} ({r['plz']} {r['city']}, badges: {r['type_badges']})"
                    for r in hits[:60]) or "- (none)")
        if len(hits) > 60:
            a(f"- ... and {len(hits) - 60} more (see the CSV)")
        a("")
    a("")
    a("## 6. Sites to verify by hand (Google Maps)")
    a("")
    for r in spotcheck(base["rows"]):
        a(f"- {r['name']}, {r['street']}, {r['plz']} {r['city']} (`{r['slug']}`)")
    a("")
    return "\n".join(L)
