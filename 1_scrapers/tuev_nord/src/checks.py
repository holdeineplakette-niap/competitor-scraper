"""Completeness, consistency and data-quality checks for the TÜV NORD station finder. Pure functions, no network.

Severities: `hard` fails the run (exports are then written as `unverified`), `review` is listed prominently in the report but
does not fail the run (a finding for a human), `info` is only listed.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np
from core.geo import haversine_km

from tuev_nord import parse

PLZ_FAR_KM = 15.0  # a site farther than this from the centroid of its own PLZ is flagged (rough check)
TOTAL_CHANGE_ALERT = 0.10  # review when the record count moves more than this against the previous run
LIST_MISMATCH_HARD = 0.05  # hard when more than this share of the stations is in only one of map and list
COORD_TOL = 1e-5

# flags that set needs_review on a row
REVIEW_FLAGS = {
    "unclassified", "coords_missing", "coords_outside_germany", "coords_far_from_plz", "plz_not_5_digits", "plz_not_in_geonames",
    "duplicate_source_key", "id_reused_other_address", "address_shared_with_other_id", "coords_shared_across_plz",
    "office_with_hu", "hu_site_without_station_page", "station_without_page_link", "not_in_list_page", "unknown_source_fields",
    "missing_or_odd_fields", "street_missing",
}


def check(name: str, ok: bool, detail: str, severity: str = "hard") -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": severity}


# ------------------------------------------------------------------------------------------------ the stations page
def check_stations_page(status: int, page: parse.StationsPage, rows: list[dict]) -> list[dict]:
    n = len(page.records)
    types = Counter(r["service_type"] for r in rows)
    out = [
        check("stations page answered HTTP 200", status == 200, f"HTTP {status}"),
        check("exactly one map element with data-locations, valid JSON list, at least one record", page.n_map_elements == 1 and n > 0,
              f"{page.n_map_elements} map element(s), {n} records, {page.locations_chars} characters of JSON"),
        check("the page shows its counter '<N> Stationen gefunden'", bool(page.counters), f"counter text found {len(page.counters)} time(s): {page.counters}"),
        check("counter == number of parsed records", bool(page.counters) and set(page.counters) == {n},
              f"counter {page.counters}, parsed records {n}"),
        check("page is unfiltered: all three serviceTypes present", all(types.get(t, 0) > 0 for t in parse.SERVICE_TYPES),
              ", ".join(f"{t} {types.get(t, 0)}" for t in parse.SERVICE_TYPES)),
        check("no serviceType value other than HU / HU-UNG / UNG", set(types) <= set(parse.SERVICE_TYPES),
              f"values seen: {dict(types)}"),
    ]
    missing = [(r["source_id"], r["_missing_fields"]) for r in rows if r["_missing_fields"]]
    odd = [r["source_id"] for r in rows if not r["_raw_types_ok"] or not r["source_id"]]
    out.append(check("every record has all ten expected fields", not missing, f"{len(missing)} record(s) with missing fields: {missing[:5]}"))
    out.append(check("field types as expected (id int, isOffice bool, name/plz text)", not odd, f"{len(odd)} record(s): {odd[:10]}", "review"))
    unknown = sorted({r["unknown_source_fields"] for r in rows if r["unknown_source_fields"]})
    out.append(check("no unknown fields in the records (schema unchanged)", not unknown, f"{unknown}", "review"))
    return out


# ------------------------------------------------------------------------------------------------ data quality
def data_quality(rows: list[dict], centroids=None, plz_far_km: float = PLZ_FAR_KM) -> dict:
    """Adds `plz_distance_km`, `same_id_count`, `same_address_count`, `dq_flags`, `needs_review` to every row (in place); returns the findings."""
    flags: dict[str, list[str]] = defaultdict(list)  # location_id -> flags
    findings: dict = {"plz_check": "skipped (no PLZ centroids)" if centroids is None else "done"}
    cent = None
    if centroids is not None:
        cent = {str(z): (la, lo) for z, la, lo in zip(centroids["zip"], centroids["lat"], centroids["lng"])}
    box = parse.GERMANY_BOX
    outside, missing, far, no_plz, bad_plz, no_street, dists = [], [], [], [], [], [], []
    for r in rows:
        lid = r["location_id"]
        r["plz_distance_km"] = None
        lat, lng = r["latitude"], r["longitude"]
        if lat is None or lng is None:
            missing.append(lid); flags[lid].append("coords_missing")
        elif not (box[0] <= lat <= box[1] and box[2] <= lng <= box[3]):
            outside.append(lid); flags[lid].append("coords_outside_germany")
        if not (len(r["zip"]) == 5 and r["zip"].isdigit()):
            bad_plz.append(lid); flags[lid].append("plz_not_5_digits")
        elif cent is not None:
            if r["zip"] not in cent:
                no_plz.append(lid); flags[lid].append("plz_not_in_geonames")
            elif lat is not None and lng is not None:
                km = float(haversine_km(lat, lng, *cent[r["zip"]]))
                r["plz_distance_km"] = round(km, 1)
                dists.append(km)
                if km > plz_far_km:
                    far.append((lid, round(km, 1))); flags[lid].append("coords_far_from_plz")
        if not r["street"]:
            no_street.append(lid); flags[lid].append("street_missing")
        if r["site_type"] == "unclassified":
            flags[lid].append("unclassified")
        if r["psp_suspect"]:
            flags[lid].append("psp_suspect")
        if r["unknown_source_fields"]:
            flags[lid].append("unknown_source_fields")
        if r["_missing_fields"] or not r["_raw_types_ok"]:
            flags[lid].append("missing_or_odd_fields")
        if r["is_office"] and r["is_hu_site"]:
            flags[lid].append("office_with_hu")
        if not r["is_office"] and not r["station_slug"]:
            flags[lid].append("station_without_page_link")
            if r["is_hu_site"]:
                flags[lid].append("hu_site_without_station_page")
        if r["link_variant"] == "stationsseiten":
            flags[lid].append("link_variant_stationsseiten")
        if r["name"] != r["name_clean"]:
            flags[lid].append("name_whitespace_cleaned")
        if r["site_type_reason"].startswith("bracket repeats"):
            flags[lid].append("bracket_is_place_name")

    by_key, by_id, by_addr = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in rows:
        by_key[r["source_key"]].append(r)
        by_id[r["source_id"]].append(r)
        by_addr[r["address_norm"]].append(r)
    dup_keys = {k: [r["location_id"] for r in g] for k, g in by_key.items() if len(g) > 1}
    for g in by_key.values():
        if len(g) > 1:
            for r in g:
                flags[r["location_id"]].append("duplicate_source_key")
    reused = []
    for sid, g in by_id.items():
        if len({r["address_norm"] for r in g}) > 1:
            reused.append({"id": sid, "locations": [{"location_id": r["location_id"], "name": r["name_clean"], "address": _addr(r),
                                                     "service_type": r["service_type"], "is_office": r["is_office"]} for r in g]})
            for r in g:
                flags[r["location_id"]].append("id_reused_other_address")
    shared_addr = []
    for a, g in by_addr.items():
        if len({r["source_id"] for r in g}) > 1:
            shared_addr.append({"address": _addr(g[0]), "locations": [{"location_id": r["location_id"], "source_id": r["source_id"], "name": r["name_clean"],
                                                                        "service_type": r["service_type"], "is_office": r["is_office"],
                                                                        "site_type": r["site_type"]} for r in g]})
            for r in g:
                flags[r["location_id"]].append("address_shared_with_other_id")
    by_coord = defaultdict(list)
    for r in rows:
        if r["latitude"] is not None and r["longitude"] is not None:
            by_coord[(round(r["latitude"], 6), round(r["longitude"], 6))].append(r)
    shared_across_plz, shared_same_plz = [], 0
    for (la, lo), g in by_coord.items():
        if len(g) < 2:
            continue
        if len({r["zip"] for r in g}) > 1:
            shared_across_plz.append({"lat": la, "lng": lo, "locations": [{"location_id": r["location_id"], "name": r["name_clean"], "zip": r["zip"],
                                                                           "service_type": r["service_type"], "is_office": r["is_office"]} for r in g]})
            for r in g:
                flags[r["location_id"]].append("coords_shared_across_plz")
        elif len({r["address_norm"] for r in g}) > 1:
            shared_same_plz += 1
    slugs = Counter(r["station_slug"] for r in rows if r["station_slug"])
    dup_slugs = {s: n for s, n in slugs.items() if n > 1}
    for r in rows:
        r["same_id_count"] = len(by_id[r["source_id"]])
        r["same_address_count"] = len(by_addr[r["address_norm"]])
        f = flags.get(r["location_id"], [])
        r["dq_flags"] = "; ".join(dict.fromkeys(f))
        r["needs_review"] = bool(REVIEW_FLAGS & set(f))
    hu = [r for r in rows if r["is_hu_site"] and r["latitude"] is not None]
    findings.update(
        coords_outside_germany=outside, coords_missing=missing, coords_far_from_plz=far, plz_not_in_geonames=no_plz, plz_not_5_digits=bad_plz,
        street_missing=[{"location_id": r["location_id"], "name": r["name_clean"], "service_type": r["service_type"], "is_office": r["is_office"],
                         "zip": r["zip"], "city": r["city"]} for r in rows if not r["street"]],
        duplicate_source_keys=dup_keys, ids_reused_with_other_address=reused, addresses_shared_by_several_ids=shared_addr,
        coords_shared_across_plz=shared_across_plz, coords_shared_same_plz_other_address=shared_same_plz, duplicate_station_slugs=dup_slugs,
        unclassified=[{"location_id": r["location_id"], "name": r["name_clean"], "service_type": r["service_type"], "reason": r["site_type_reason"]}
                      for r in rows if r["site_type"] == "unclassified"],
        plz_distance=({"max_km": round(float(np.max(dists)), 1), "p95_km": round(float(np.percentile(dists, 95)), 1),
                       "median_km": round(float(np.median(dists)), 1), "threshold_km": plz_far_km} if dists else {}),
        hu_bbox=({"lat_min": min(r["latitude"] for r in hu), "lat_max": max(r["latitude"] for r in hu),
                  "lng_min": min(r["longitude"] for r in hu), "lng_max": max(r["longitude"] for r in hu)} if hu else {}),
        flag_counts=dict(Counter(f for fl in flags.values() for f in set(fl))),
    )
    return findings


def _addr(r: dict) -> str:
    return f"{r['street']}, {r['zip']} {r['city']}"


def quality_checks(f: dict) -> list[dict]:
    return [
        check("coordinates present and inside the Germany box", not f["coords_outside_germany"] and not f["coords_missing"],
              f"outside: {f['coords_outside_germany']}, missing: {f['coords_missing']}", "review"),
        check("every PLZ is 5 digits and known to GeoNames", not f["plz_not_5_digits"] and not f["plz_not_in_geonames"],
              f"not 5 digits: {f['plz_not_5_digits']}, not in GeoNames: {f['plz_not_in_geonames']}", "review"),
        check("every record has a street", not f["street_missing"],
              f"{len(f['street_missing'])} without street ({sum(1 for x in f['street_missing'] if x['is_office'])} offices, "
              f"{sum(1 for x in f['street_missing'] if x['service_type'] != 'UNG')} with HU)", "review"),
        check(f"coordinates within {f.get('plz_distance', {}).get('threshold_km', PLZ_FAR_KM):g} km of the centroid of the PLZ",
              not f["coords_far_from_plz"] and f["plz_check"] == "done", f"{f['plz_check']}; far: {f['coords_far_from_plz']}", "review"),
        check("no duplicate key (id + normalised address)", not f["duplicate_source_keys"], f"{f['duplicate_source_keys']}", "review"),
        check("no id used for several different addresses", not f["ids_reused_with_other_address"],
              f"{len(f['ids_reused_with_other_address'])} id(s): {[x['id'] for x in f['ids_reused_with_other_address']]} (known quirk, kept, flagged)", "review"),
        check("no address listed under several ids", not f["addresses_shared_by_several_ids"],
              f"{len(f['addresses_shared_by_several_ids'])} address(es) (kept, flagged)", "review"),
        check("no coordinates shared by records with different PLZ", not f["coords_shared_across_plz"],
              f"{len(f['coords_shared_across_plz'])} group(s) (kept, flagged)", "review"),
        check("no two stations share one page slug", not f["duplicate_station_slugs"], f"{f['duplicate_station_slugs']}", "review"),
        check("every station name is classified (own / partner / office)", not f["unclassified"],
              f"{len(f['unclassified'])} unclassified (listed in the report)", "review"),
    ]


# ------------------------------------------------------------------------------------------------ cross-check with the A-Z list
def cross_check(rows: list[dict], listing: parse.ListPage | None) -> tuple[list[dict], dict]:
    """Stations of the map page (offices have no page) against the A-Z list, by page slug. Every one-source-only record is listed."""
    if listing is None:
        return [check("A-Z list page was fetched and parsed", False, "no list page")], {}
    stations = [r for r in rows if r["station_slug"]]
    map_by_slug: dict[str, list] = defaultdict(list)
    for r in stations:
        map_by_slug[r["station_slug"]].append(r)
    list_by_slug: dict[str, list] = defaultdict(list)
    for it in listing.items:
        list_by_slug[it["slug"]].append(it)
    both = sorted(set(map_by_slug) & set(list_by_slug))
    only_map = sorted(set(map_by_slug) - set(list_by_slug))
    only_list = sorted(set(list_by_slug) - set(map_by_slug))
    map_names = {parse.normalise_text(r["name_clean"]): r for r in stations}
    list_names = {parse.normalise_text(it["name"]): it for it in listing.items}

    def twin_in_list(r):
        it = list_names.get(parse.normalise_text(r["name_clean"]))
        return it["slug"] if it else ""

    def twin_in_map(it):
        r = map_names.get(parse.normalise_text(it["name"]))
        return r["station_slug"] if r else ""

    name_diff = [{"slug": s, "map": map_by_slug[s][0]["name_clean"], "list": list_by_slug[s][0]["name"]} for s in both
                 if parse.normalise_text(map_by_slug[s][0]["name_clean"]) != parse.normalise_text(list_by_slug[s][0]["name"])]
    path_diff = [{"slug": s, "map": map_by_slug[s][0]["link_variant"], "list": list_by_slug[s][0]["variant"] or "standard"} for s in both
                 if (map_by_slug[s][0]["link_variant"] == "stationsseiten") != (list_by_slug[s][0]["variant"] == "stationsseiten")]
    d = {
        "map_stations": len(stations), "map_unique_slugs": len(map_by_slug), "offices_without_page": sum(1 for r in rows if r["is_office"]),
        "list_items": len(listing.items), "list_unique_slugs": len(list_by_slug), "list_other_links": listing.other_links, "list_letters": listing.letters,
        "both": len(both),
        "only_in_map": [{"slug": s, "name": map_by_slug[s][0]["name_clean"], "service_type": map_by_slug[s][0]["service_type"],
                         "site_type": map_by_slug[s][0]["site_type"], "address": _addr(map_by_slug[s][0]), "location_id": map_by_slug[s][0]["location_id"],
                         "same_name_in_list_as": twin_in_list(map_by_slug[s][0])} for s in only_map],
        "only_in_list": [{"slug": s, "name": list_by_slug[s][0]["name"], "variant": list_by_slug[s][0]["variant"] or "standard",
                          "letter": list_by_slug[s][0]["letter"], "same_name_in_map_as": twin_in_map(list_by_slug[s][0])} for s in only_list],
        "name_differs": name_diff, "path_variant_differs": path_diff,
        "duplicate_slugs_in_list": {s: len(v) for s, v in list_by_slug.items() if len(v) > 1},
        "service_type_of_both": dict(Counter(map_by_slug[s][0]["service_type"] for s in both)),
    }
    for r in stations:
        r["in_list_page"] = r["station_slug"] in list_by_slug
        if not r["in_list_page"]:
            r["dq_flags"] = "; ".join(filter(None, [r.get("dq_flags", ""), "not_in_list_page"]))
            r["needs_review"] = True
    share = (len(only_map) + len(only_list)) / max(len(map_by_slug), len(list_by_slug), 1)
    out = [
        check("A-Z list page parsed (at least one station link)", bool(listing.items), f"{len(listing.items)} station links, letters {''.join(listing.letters)}"),
        check("list page has no duplicate slugs", not d["duplicate_slugs_in_list"], f"{d['duplicate_slugs_in_list']}", "review"),
        check(f"map stations and list agree (hard above {LIST_MISMATCH_HARD:.0%} in one source only)", share <= LIST_MISMATCH_HARD,
              f"{len(both)} in both, {len(only_map)} only in the map, {len(only_list)} only in the list ({share:.1%})"),
        check("every station is in both the map data and the list", not only_map and not only_list,
              f"only in map: {[x['slug'] for x in d['only_in_map']]}; only in list: {[x['slug'] for x in d['only_in_list']]}", "review"),
    ]
    return out, d


# ------------------------------------------------------------------------------------------------ previous run
def snapshot(page: parse.StationsPage) -> dict:
    rows = parse.parse_records(page.records)
    return {"records": len(page.records), "counters": page.counters,
            "fields": sorted({k for r in page.records for k in r}),
            "service_types": dict(Counter(r["service_type"] for r in rows)),
            "hu": sum(1 for r in rows if r["is_hu_site"]), "offices": sum(1 for r in rows if r["is_office"]),
            "keys": sorted(r["source_key"] for r in rows), "map_attributes": sorted(page.map_attrs)}


def compare_previous(prev: dict | None, now: dict, prev_name: str = "") -> tuple[list[dict], dict]:
    """Structure-only comparison with the raw stations page of the previous run (counts, field set, serviceType set, keys)."""
    if prev is None:
        return [check("comparison with the previous run's raw page", True, "no previous run with a saved stations page (first run)", "info")], {}
    pk, nk = set(prev["keys"]), set(now["keys"])
    change = (now["records"] - prev["records"]) / prev["records"] if prev["records"] else 0.0
    d = {"previous_run": prev_name, "records": [prev["records"], now["records"]], "change": round(change, 4),
         "fields_added": sorted(set(now["fields"]) - set(prev["fields"])), "fields_removed": sorted(set(prev["fields"]) - set(now["fields"])),
         "service_types": [prev["service_types"], now["service_types"]], "hu": [prev["hu"], now["hu"]],
         "keys_added": len(nk - pk), "keys_removed": len(pk - nk), "map_attributes_changed": sorted(set(prev["map_attributes"]) ^ set(now["map_attributes"]))}
    return [
        check(f"record count within ±{TOTAL_CHANGE_ALERT:.0%} of the previous run", abs(change) <= TOTAL_CHANGE_ALERT,
              f"{prev['records']} -> {now['records']} ({change:+.1%}) vs {prev_name}", "review"),
        check("same record fields and serviceType values as the previous run", not d["fields_added"] and not d["fields_removed"]
              and set(prev["service_types"]) == set(now["service_types"]),
              f"fields added {d['fields_added']}, removed {d['fields_removed']}; serviceTypes {sorted(prev['service_types'])} -> {sorted(now['service_types'])}", "review"),
        check("same map-element attributes as the previous run", not d["map_attributes_changed"], f"{d['map_attributes_changed']}", "review"),
    ], d


# ------------------------------------------------------------------------------------------------ detail pages
def check_detail(role: str, url: str, status: int, desc: dict, row: dict | None, expect_hu: bool | None) -> list[dict]:
    """Sanity of one detail page against the record it belongs to."""
    out = [check(f"detail page ({role}) answered HTTP 200 with a heading", status == 200 and bool(desc.get("h1")), f"HTTP {status}, h1={desc.get('h1')!r}", "review")]
    if status != 200:
        return out
    out.append(check(f"detail page ({role}) is the station's own page (not the finder served instead)", not desc.get("is_finder_page"),
                     f"canonical {desc.get('canonical')!r}", "review"))
    if row is not None and desc.get("booking_station_ids"):
        out.append(check(f"detail page ({role}): booking stationId == id in the list data", desc["booking_station_ids"] == [row["source_id"]],
                         f"page {desc['booking_station_ids']}, list id {row['source_id']}", "review"))
    if row is not None:
        coords = (desc.get("map_coords") or [{}])[0]
        ok = (coords.get("lat") is not None and abs(float(coords["lat"]) - row["latitude"]) < COORD_TOL
              and abs(float(coords["lng"]) - row["longitude"]) < COORD_TOL)
        out.append(check(f"detail page ({role}): map coordinates == coordinates in the list data", ok, f"page {coords}, list {row['latitude']}, {row['longitude']}", "review"))
    if expect_hu is not None:
        out.append(check(f"detail page ({role}): HU service listed <=> serviceType says HU", desc.get("lists_hu_service") == expect_hu,
                         f"service headings list HU: {desc.get('lists_hu_service')}, expected {expect_hu}", "review"))
    return out
