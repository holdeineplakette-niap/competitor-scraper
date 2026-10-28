"""Completeness and data-quality checks for the VÜK site. Pure functions, no network.

Severities: `hard` fails the run (no export is written as a success), `review` is listed prominently but does not fail
the run (a finding for a human), `info` is only listed.

The completeness rule (Nikos, 2026-10-06): the station sets of the menu, the footer, the body text of /ueber-uns/, the XML
sitemap and the HTML sitemap are identical, and the number the site states ("fünf eigenen Standorten") equals the size of
that set. Anything else FAILS with a message that lists the differences. A run never passes with a different count.
"""

from __future__ import annotations

import re
from collections import defaultdict

from vuek import parse

# flags (see station_flags) that set needs_review on a row; the others are only listed
REVIEW_FLAGS = {
    "hu_not_on_page", "title_street_mismatch", "map_address_mismatch", "unparsed_lines", "no_hours", "no_phone",
    "coords_missing", "coords_postcode_mismatch", "duplicate_address", "page_warnings",
}
PLZ_FAR_KM = 25.0


def check(name: str, ok: bool, detail: str, severity: str = "hard") -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": severity}


def diff_text(a_name: str, a, b_name: str, b) -> str:
    only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    return f"only in {a_name}: {only_a or '-'}; only in {b_name}: {only_b or '-'}"


def _same_as_menu(menu, other, other_name: str, severity: str = "hard") -> dict:
    ok = set(menu) == set(other)
    return check(f"menu == {other_name}", ok, f"{len(set(other))} station(s)" if ok else diff_text("menu", menu, other_name, other),
                 severity)


def discovery_checks(about: dict, sitemap_xml: list[str] | None, sitemap_page: dict | None, hub: dict | None,
                     baseline_paths: list[str] | None = None, baseline_label: str = "the last passing full run",
                     accept_change: bool = False) -> tuple[list[dict], list[str]]:
    """All completeness checks. Sources that were not fetched (None) are skipped, not assumed fine.

    baseline_paths: the station set a new or vanished station is measured against (hard check). `accept_change=True` is the
    caller's explicit acknowledgement that the set changed on purpose: the check then passes and says so.

    Returns (checks, the station paths to crawl = the menu set)."""
    out = [check("/ueber-uns/ has a content area", about["content_found"], "div#content_area found" if about["content_found"]
                 else "the page has no div#content_area: the markup changed or this is not the page")]
    menu = about["menu_set"]
    out.append(check("menu: the Kontakt menu lists station links", bool(about["menus"]) and bool(menu),
                     f"{len(about['menus'])} navigation container(s) with a Kontakt menu, {len(menu)} station link(s)"))
    variants = {tuple(m["stations"]) for m in about["menus"]}
    out.append(check("menu: desktop and mobile navigation list the same stations", len(variants) <= 1,
                     "identical" if len(variants) <= 1 else f"variants differ: {[list(v) for v in variants]}"))
    out.append(check("footer: exactly one block 'VÜK Standorte'", about["footer_blocks"] == 1,
                     f"{about['footer_blocks']} block(s) found"))
    out.append(_same_as_menu(menu, about["footer_set"], "footer"))
    out.append(_same_as_menu(menu, about["body_set"], "body text of /ueber-uns/"))
    if sitemap_xml is not None:
        out.append(_same_as_menu(menu, parse.station_paths_of_urls(sitemap_xml), "sitemap.xml"))
    if sitemap_page is not None:
        out.append(check("HTML sitemap page has a content area with links", sitemap_page["content_found"] and bool(sitemap_page["paths"]),
                         f"{len(sitemap_page['paths'])} internal link(s)"))
        out.append(_same_as_menu(menu, sitemap_page["station_set"], "HTML sitemap page"))
    if hub is not None:
        out.append(_same_as_menu(menu, hub["station_set"], "/kontakt/ page", "review"))
    if sitemap_xml is not None and sitemap_page is not None:
        xml_paths = {p for u in sitemap_xml if (p := parse.norm_path(u))}
        page_paths = set(sitemap_page["paths"])
        out.append(check("sitemap.xml and HTML sitemap list the same pages", xml_paths == page_paths,
                         "identical" if xml_paths == page_paths else diff_text("sitemap.xml", xml_paths, "HTML sitemap", page_paths),
                         "info"))

    n = len(menu)
    meta = about["stated_meta"]
    meta_ok = bool(meta) and all(s["value"] is not None for s in meta) and len({s["value"] for s in meta}) == 1
    out.append(check("stated number of stations is readable (meta description)", meta_ok,
                     f"{[s['raw'] for s in meta]}" if meta else f"no 'N eigenen Standorten' statement in the meta description: {about['meta_description']!r}"))
    if meta_ok:
        out.append(check("stated number (meta description) == number of stations", meta[0]["value"] == n,
                         f"the site says {meta[0]['value']} ({meta[0]['raw']!r}), the links give {n}"))
    body = about["stated_body"]
    body_ok = all(s["value"] is not None and s["value"] == n for s in body)
    out.append(check("stated number in the body text is readable and equals the number of stations", body_ok,
                     f"{[(s['raw'], s['value']) for s in body]} against {n} station(s)" if body else "no statement in the body text"))
    out.append(check("the body text states the number of stations", bool(body), f"{len(body)} statement(s)", "review"))
    out.append(check("at least one station found", n > 0, f"{n} station(s)"))
    if baseline_paths is not None:
        same = set(baseline_paths) == set(menu)
        detail = "identical" if same else diff_text(baseline_label, baseline_paths, "this run", menu)
        if not same and accept_change:
            out.append(check(f"station set equals {baseline_label}", True, f"CHANGE ACCEPTED by the caller: {detail}"))
        else:
            out.append(check(f"station set equals {baseline_label}", same, detail))
    return out, list(menu)


def _address_ok(r: dict) -> bool:
    return bool(r["address_ok"])


def station_checks(rows: list[dict], expected_paths: list[str], failed_fetches: dict[str, str]) -> list[dict]:
    """Per-station checks. `failed_fetches` maps a path to the reason its page could not be read."""
    out = [check("every station page could be read", not failed_fetches and len(rows) == len(expected_paths),
                 f"{len(rows)} of {len(expected_paths)} read" + (f"; failed: {failed_fetches}" if failed_fetches else ""))]
    bad = [(r["slug"], r["warnings"] or "street, 5-digit PLZ or city missing") for r in rows if not _address_ok(r)]
    out.append(check("every station page yields street + PLZ + city", not bad,
                     f"{len(rows) - len(bad)} of {len(rows)} complete" + (f"; incomplete: {bad}" if bad else "")))
    names = [r["name"] for r in rows if not r["name"]]
    out.append(check("every station page has a name (h1)", not names, f"{len(names)} without", "review"))
    hu_missing = [r["slug"] for r in rows if not r["hu_on_page"]]
    out.append(check("every station page says it offers the Hauptuntersuchung", not hu_missing,
                     "all pages name it" if not hu_missing else f"not named on: {hu_missing}", "review"))
    return out


def station_flags(row: dict, phone_counts: dict[str, int], address_counts: dict[str, int]) -> list[str]:
    flags: list[str] = []
    if row["warnings"]:
        flags.append("page_warnings")
    if not row["hu_on_page"]:
        flags.append("hu_not_on_page")
    if row["street"] and row["title"] and parse.normalize_street(row["street"]) not in parse.normalize_street(row["title"]):
        flags.append("title_street_mismatch")
    if row["street"] and row["map_query"] and parse.normalize_street(row["street"]) not in parse.normalize_street(row["map_query"]):
        flags.append("map_address_mismatch")
    if row["unparsed_lines"]:
        flags.append("unparsed_lines")
    if not row["opening_hours_raw"]:
        flags.append("no_hours")
    elif row.get("unparsed_hours_lines"):
        flags.append("hours_not_normalized")
    if row["hours_highlight"]:
        flags.append("hours_highlighted")
    if row["notice"]:
        flags.append("notice_on_page")
    if not row["phone"]:
        flags.append("no_phone")
    elif phone_counts.get(re.sub(r"\D", "", row["phone"]), 0) > 1:
        flags.append("phone_shared_with_other_stations")
    if not row["email"]:
        flags.append("no_email")
    key = parse.normalize_street(f"{row['street']}{row['zip']}")
    if key and address_counts.get(key, 0) > 1:
        flags.append("duplicate_address")
    return flags


def counts_of(rows: list[dict], key) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        out[key(r)] += 1
    return dict(out)


def coordinate_checks(rows: list[dict], threshold_km: float = PLZ_FAR_KM) -> list[dict]:
    """After geocoding and enrichment: every row has coordinates inside Germany, near its PLZ centroid, and (for Nominatim)
    the answer names the PLZ. These are review checks: a geocoder problem is reported loudly but is not a completeness failure."""
    box = parse.GERMANY_BOX
    missing = [r["slug"] for r in rows if r.get("lat") is None or r.get("lng") is None]
    outside = [r["slug"] for r in rows if r.get("lat") is not None
               and not (box[0] <= r["lat"] <= box[1] and box[2] <= r["lng"] <= box[3])]
    far = [(r["slug"], r["plz_distance_km"]) for r in rows if r.get("plz_distance_km") is not None and r["plz_distance_km"] > threshold_km]
    no_plz = [r["slug"] for r in rows if r.get("plz_distance_km") is None and r.get("lat") is not None]
    mismatch = [r["slug"] for r in rows if "coords_postcode_mismatch" in r.get("flags", [])]
    return [
        check("every station has coordinates", not missing, f"missing: {missing}" if missing else f"{len(rows)} of {len(rows)} have coordinates", "review"),
        check("coordinates lie inside Germany", not outside, f"outside the Germany box: {outside}" if outside else "all inside", "review"),
        check(f"coordinates within {threshold_km:g} km of the PLZ centroid (plz_distance_km)", not far and not no_plz,
              f"far: {far}; no PLZ distance: {no_plz}" if far or no_plz else "all within the limit", "review"),
        check("geocoder answer names the station's PLZ", not mismatch, f"PLZ not in the answer for: {mismatch}" if mismatch else "ok", "review"),
    ]
