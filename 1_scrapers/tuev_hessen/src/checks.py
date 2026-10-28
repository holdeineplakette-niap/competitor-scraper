"""Completeness, consistency and data-quality checks for the TÜV Hessen locator. Pure functions, no network.

Severities of a check: `hard` fails the run (no export is written as a success), `review` is listed prominently in the
report but does not fail the run (a finding for a human), `info` is only listed.
"""

from __future__ import annotations

import json
from collections import defaultdict

import numpy as np
from core import geo

from tuev_hessen import fetch, parse

PLZ_FAR_KM = geo.PLZ_FAR_KM  # a site farther than this from the centroid of its own PLZ is flagged (rough check)
DROP_ALERT = 0.10

# flags that set needs_review on a row (tag/title mismatches are common and only reported)
REVIEW_FLAGS = {
    "unclassified", "coords_outside_germany", "coords_missing", "coords_far_from_plz", "coords_shared_with_other_address",
    "address_shared_with_other_uid", "hu_tag_without_hu_evidence", "non_hu_with_hu_evidence", "plz_not_5_digits",
    "plz_not_in_geonames", "coordinates_field_differs", "location_sign_shared",
}


def check(name: str, ok: bool, detail: str, severity: str = "hard") -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": severity}


def decode_payload(body: bytes, content_type: str, status: int) -> tuple[dict | None, str]:
    """(payload, error). Any block, HTTP error, empty or non-JSON answer is an error text, never an empty payload."""
    if not body:
        return None, "empty body"
    reason = fetch.looks_blocked(status, {}, body)
    if reason:
        return None, f"blocked: {reason}"
    if status != 200:
        return None, f"HTTP {status}"
    if "json" not in (content_type or "").lower():
        return None, f"content type {content_type!r} is not JSON"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"not valid JSON ({exc})"
    hits = (payload.get("hits") or {}) if isinstance(payload, dict) else {}
    if not isinstance(hits.get("hits"), list) or not isinstance(hits.get("total"), dict):
        return None, "JSON without hits.hits (list) / hits.total (object): unexpected shape"
    return payload, ""


def strip_volatile(payload: dict) -> dict:
    """The payload without `took` (query time) so two answers can be compared for content."""
    return {k: v for k, v in payload.items() if k != "took"}


def expected_fields(tag_value: str) -> list[list[str]]:
    return ([["language", fetch.EXPECTED_STATIC_FIELDS["language"]]] + [["fixedTags[]", t] for t in fetch.EXPECTED_FIXED_TAGS]
            + [["address", fetch.EXPECTED_STATIC_FIELDS["address"]], ["tags[]", tag_value]])


def check_dataset(key: str, cap, ui: dict | None, ui_err: str, replay: dict | None, replay_err: str) -> list[dict]:
    """Completeness checks for one dataset. `ui` / `replay` are the decoded payloads (None when decoding failed)."""
    label = f"{key}"
    out = []
    out.append(check(f"{label}: UI answer is HTTP 200 JSON with hits", ui is not None, ui_err or f"{len(cap.ui_body)} bytes"))
    if cap.replay_body or replay_err:
        out.append(check(f"{label}: replay from the page is HTTP 200 JSON with hits", replay is not None,
                         replay_err or f"{len(cap.replay_body)} bytes", "review"))
    body = replay if replay is not None else ui
    if body is None:
        return out
    hits = body["hits"]["hits"]
    total = body["hits"]["total"]
    out.append(check(f"{label}: hits.total.relation == 'eq'", total.get("relation") == "eq", f"relation={total.get('relation')!r}"))
    out.append(check(f"{label}: hits.total.value == len(hits.hits)", total.get("value") == len(hits),
                     f"total.value={total.get('value')}, len(hits.hits)={len(hits)}"))
    out.append(check(f"{label}: at least one hit (an empty list is an error, not '0 sites')", len(hits) > 0, f"{len(hits)} hits"))
    counter = (cap.ui_counter or "").strip()
    out.append(check(f"{label}: UI counter on the button == len(hits.hits)", counter.isdigit() and int(counter) == len(hits),
                     f"counter={counter!r}, len(hits.hits)={len(hits)}"))
    shards = body.get("_shards") or {}
    out.append(check(f"{label}: not timed out, all shards answered", body.get("timed_out") is False
                     and shards.get("failed", 1) == 0 and shards.get("successful") == shards.get("total"),
                     f"timed_out={body.get('timed_out')}, _shards={shards}"))
    uids = [h.get("_source", {}).get("uid") for h in hits]
    ids = [h.get("_id") for h in hits]
    out.append(check(f"{label}: no duplicate uid / _id, every hit has a uid", len(set(uids)) == len(uids) and None not in uids
                     and len(set(ids)) == len(ids), f"{len(uids)} hits, {len(set(uids))} distinct uids, {len(set(ids))} distinct _id"))
    if ui is not None and replay is not None:
        same = strip_volatile(ui) == strip_volatile(replay)
        out.append(check(f"{label}: replay answer == UI answer (content, ignoring 'took')", same,
                         "identical" if same else f"differ: UI {len(ui['hits']['hits'])} hits, replay {len(replay['hits']['hits'])} hits"))
    fields_ok = [list(f) for f in cap.request_fields] == expected_fields(cap.tag_value)
    out.append(check(f"{label}: captured request fields are the expected ones", fields_ok,
                     "as expected" if fields_ok else f"captured {cap.request_fields}, expected {expected_fields(cap.tag_value)}"))
    out.append(check(f"{label}: request has no size/limit field (so the server decides; total == len proves no truncation)",
                     not any(name.lower() in {"size", "limit", "from", "rows", "max", "count"} for name, _ in cap.request_fields),
                     "no size/limit field" if not any(n.lower() in {"size", "limit", "from", "rows", "max", "count"} for n, _ in cap.request_fields)
                     else "a size-like field is present: check it", "review"))
    return out


def diff_datasets(all_hits: list[dict], hu_hits: list[dict]) -> dict:
    """uids only in 'all' (the non-HU sites), only in 'hu' (must be empty) and common records whose _source differs."""
    a = {h["_source"]["uid"]: h for h in all_hits}
    u = {h["_source"]["uid"]: h for h in hu_hits}
    only_all = sorted(set(a) - set(u))
    only_hu = sorted(set(u) - set(a))
    changed = sorted(uid for uid in set(a) & set(u) if a[uid]["_source"] != u[uid]["_source"])
    return {"only_in_all": only_all, "only_in_hu": only_hu, "common": len(set(a) & set(u)), "common_but_different": changed}


def check_cross(all_hits: list[dict], hu_hits: list[dict], rows_by_uid: dict) -> tuple[list[dict], dict]:
    d = diff_datasets(all_hits, hu_hits)
    out = [
        check("HU list is a subset of 'Alle Standorte'", not d["only_in_hu"], f"{len(d['only_in_hu'])} uid(s) only in the HU list: {d['only_in_hu']}"),
        check("records in both lists are identical", not d["common_but_different"],
              f"{len(d['common_but_different'])} of {d['common']} differ: {d['common_but_different'][:10]}"),
    ]
    tag_hu = {uid for uid, r in rows_by_uid.items() if r["is_hu_site"]}
    hu_uids = {h["_source"]["uid"] for h in hu_hits}
    out.append(check("tag 'Kfz Prüfstelle' on a record <=> record is in the HU list", tag_hu == hu_uids,
                     f"{len(tag_hu)} tagged in 'all', {len(hu_uids)} in the HU list, symmetric difference {sorted(tag_hu ^ hu_uids)[:10]}"))
    non_hu = [rows_by_uid[u] for u in d["only_in_all"] if u in rows_by_uid]
    with_hu = [r["uid"] for r in non_hu if r["hu_evidence"]]
    out.append(check("none of the non-HU sites (only in 'Alle Standorte') mentions Hauptuntersuchung / HU or has an HU booking link",
                     not with_hu, f"{len(non_hu)} non-HU sites, {len(with_hu)} with HU evidence: {with_hu}", "review"))
    return out, d


def data_quality(rows: list[dict], centroids=None, plz_far_km: float = PLZ_FAR_KM) -> dict:
    """Adds `plz_distance_km` and `dq_flags` / `needs_review` to every row (in place) and returns the findings."""
    flags: dict[int, list[str]] = defaultdict(list)
    by_uid = {r["uid"]: r for r in rows}
    findings: dict = {"plz_check": "skipped (no PLZ centroids)" if centroids is None else "done"}

    uids = [r["uid"] for r in rows]
    findings["duplicate_uids"] = sorted({u for u in uids if uids.count(u) > 1})

    cent = None
    if centroids is not None:
        cent = geo.centroid_lookup(centroids)

    outside, missing, far, no_plz, bad_plz, field_diff, phone_diff = [], [], [], [], [], [], []
    dists = []
    for r in rows:
        lat, lng = r["latitude"], r["longitude"]
        r["plz_distance_km"] = None
        if lat is None or lng is None:
            missing.append(r["uid"]); flags[r["uid"]].append("coords_missing")
        else:
            if not geo.in_germany(lat, lng):
                outside.append(r["uid"]); flags[r["uid"]].append("coords_outside_germany")
            c = r["_coordinates_dict"]
            if c and (_f(c.get("lat")) != lat or _f(c.get("lon")) != lng):
                field_diff.append(r["uid"]); flags[r["uid"]].append("coordinates_field_differs")
        if not (len(r["zip"]) == 5 and r["zip"].isdigit()):
            bad_plz.append(r["uid"]); flags[r["uid"]].append("plz_not_5_digits")
        elif cent is not None:
            if r["zip"] not in cent:
                no_plz.append(r["uid"]); flags[r["uid"]].append("plz_not_in_geonames")
            elif lat is not None and lng is not None:
                km = geo.plz_distance_km(lat, lng, r["zip"], cent)
                r["plz_distance_km"] = round(km, 1)
                dists.append(km)
                if km > plz_far_km:
                    far.append((r["uid"], round(km, 1))); flags[r["uid"]].append("coords_far_from_plz")
        if not r["phone"]:
            flags[r["uid"]].append("no_phone")
        pp = "".join(ch for ch in r["_phone_preview"] if ch.isdigit())
        if r["phone"] and pp and pp != "".join(ch for ch in r["phone"] if ch.isdigit()):
            phone_diff.append(r["uid"]); flags[r["uid"]].append("phone_differs_from_preview")
        if r["site_type"] == "unclassified":
            flags[r["uid"]].append("unclassified")
        if r["tag_title_mismatch"]:
            flags[r["uid"]].append("tag_title_mismatch")
        if r["is_lkw_site"]:
            flags[r["uid"]].append("lkw_in_title")
        if r["is_hu_site"] and not r["hu_evidence"]:
            flags[r["uid"]].append("hu_tag_without_hu_evidence")
        if not r["is_hu_site"] and r["hu_evidence"]:
            flags[r["uid"]].append("non_hu_with_hu_evidence")
        if r["unknown_source_fields"]:
            flags[r["uid"]].append("unknown_source_fields")

    # coordinates shared by records with different addresses
    by_addr = defaultdict(list)
    for r in rows:
        by_addr[parse.normalise_address(r["street"], r["zip"])].append(r)
    shared_coords = []
    for group in geo.shared_coordinate_groups(
            rows, lambda r: (r["latitude"], r["longitude"]), lambda r: parse.normalise_address(r["street"], r["zip"])):
        group = sorted(group, key=lambda g: g["uid"])  # uids, addresses and flags stay aligned by position
        shared_coords.append({"lat": round(group[0]["latitude"], 5), "lng": round(group[0]["longitude"], 5),
                              "uids": [g["uid"] for g in group],
                              "addresses": [f"{g['street']}, {g['zip']} {g['city']}" for g in group],
                              "hu": [g["is_hu_site"] for g in group]})
        for g in group:
            flags[g["uid"]].append("coords_shared_with_other_address")
    shared_addr = []
    for addr, group in by_addr.items():
        if len(group) > 1:
            group = sorted(group, key=lambda g: g["uid"])
            shared_addr.append({"uids": [g["uid"] for g in group], "address": f"{group[0]['street']}, {group[0]['zip']} {group[0]['city']}",
                                "site_types": [g["site_type"] for g in group], "hu": [g["is_hu_site"] for g in group]})
            for g in group:
                flags[g["uid"]].append("address_shared_with_other_uid")
    urls = defaultdict(list)
    for r in rows:
        if r["detail_url"]:
            urls[r["detail_url"]].append(r["uid"])
    signs = defaultdict(list)  # the HU booking link carries a locationSign: one per site, so a shared one is a copy-paste error
    for r in rows:
        if r["location_sign"]:
            signs[r["location_sign"]].append(r)
    shared_signs = {k: sorted(g["uid"] for g in v) for k, v in signs.items() if len(v) > 1}
    for uids_ in shared_signs.values():
        for u in uids_:
            flags[u].append("location_sign_shared")

    for r in rows:
        f = flags.get(r["uid"], [])
        r["dq_flags"] = "; ".join(dict.fromkeys(f))
        r["needs_review"] = bool(REVIEW_FLAGS & set(f))

    findings.update(
        coords_outside_germany=outside, coords_missing=missing, coords_far_from_plz=far, plz_not_in_geonames=no_plz,
        plz_not_5_digits=bad_plz, coordinates_field_differs=field_diff, phone_differs_from_preview=phone_diff,
        coords_shared_with_other_address=shared_coords, address_shared=shared_addr,
        duplicate_detail_urls={u: v for u, v in urls.items() if len(v) > 1}, shared_location_signs=shared_signs,
        missing_phone=sorted(r["uid"] for r in rows if not r["phone"]),
        missing_detail_url=sorted(r["uid"] for r in rows if not r["detail_url"]),
        unknown_source_fields=sorted({r["unknown_source_fields"] for r in rows if r["unknown_source_fields"]}),
        plz_distance={"max_km": round(float(np.max(dists)), 1), "p95_km": round(float(np.percentile(dists, 95)), 1),
                      "median_km": round(float(np.median(dists)), 1), "threshold_km": plz_far_km} if dists else {},
        hu_bbox=_bbox([r for r in rows if r["is_hu_site"] and r["latitude"] is not None]),
    )
    return findings


def quality_checks(findings: dict) -> list[dict]:
    f = findings
    return [
        check("no duplicate uids in 'Alle Standorte'", not f["duplicate_uids"], f"{f['duplicate_uids']}"),
        check("every site has plausible coordinates (inside the Germany box)", not f["coords_outside_germany"] and not f["coords_missing"],
              f"outside: {f['coords_outside_germany']}, missing: {f['coords_missing']}", "review"),
        check(f"coordinates within {f.get('plz_distance', {}).get('threshold_km', PLZ_FAR_KM):g} km of the centroid of the site's PLZ",
              not f["coords_far_from_plz"] and f["plz_check"] == "done",
              f"{f['plz_check']}; far: {f['coords_far_from_plz']}", "review"),
        check("no coordinates shared by sites with different addresses", not f["coords_shared_with_other_address"],
              f"{len(f['coords_shared_with_other_address'])} group(s): {[g['uids'] for g in f['coords_shared_with_other_address']]}", "review"),
        check("no address shared by several uids", not f["address_shared"],
              f"{len(f['address_shared'])} group(s): {[g['uids'] for g in f['address_shared']]}", "review"),
        check("no HU booking `locationSign` shared by several sites", not f["shared_location_signs"],
              f"{len(f['shared_location_signs'])} shared: {f['shared_location_signs']}", "review"),
        check("every PLZ is 5 digits and known to GeoNames", not f["plz_not_5_digits"] and not f["plz_not_in_geonames"],
              f"not 5 digits: {f['plz_not_5_digits']}, not in GeoNames: {f['plz_not_in_geonames']}", "review"),
        check("`coordinates` object equals latitude/longitude", not f["coordinates_field_differs"], f"{f['coordinates_field_differs']}", "review"),
        check("no unknown fields in the records (schema unchanged)", not f["unknown_source_fields"], f"{f['unknown_source_fields']}", "review"),
        check("phone present", not f["missing_phone"], f"{len(f['missing_phone'])} site(s) without phone: {f['missing_phone']}", "info"),
    ]


def _f(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bbox(rows: list[dict]) -> dict:
    if not rows:
        return {}
    lats = [r["latitude"] for r in rows]
    lngs = [r["longitude"] for r in rows]
    return {"lat_min": min(lats), "lat_max": max(lats), "lng_min": min(lngs), "lng_max": max(lngs)}
