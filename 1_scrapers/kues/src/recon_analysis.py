"""Offline analysis of a KÜS recon run folder (see recon.py): writes summary.md next to the responses.

No network. Contains counts, distances and adressids only, never names, emails or phone numbers.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np

from core import paths
from core.geo import haversine_km, load_plz_centroids

SAVED_80636 = "partnersuche_80636_2026-10-05.json"
FIELDS = [
    "adressid", "lizenznehmer", "name", "zusatz", "plz", "ort", "strasse", "tel1", "fax", "email", "http", "lat", "lon",
    "inbreite", "inlaenge", "pruefungvorortmoeglich", "keinestrasse", "entfernung", "plzorder",
]
# a "regular" German PLZ lies roughly inside this box (Germany's bounding box with a small margin)
DE_BOX = (47.0, 55.2, 5.5, 15.3)


def _load(run: Path, step: str):
    path = run / "requests" / f"{step}_response.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _meta(run: Path, step: str) -> dict:
    return json.loads((run / "requests" / f"{step}_meta.json").read_text(encoding="utf-8"))


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def response_stats(data: list[dict], centroids) -> dict:
    """Count, distance range, server centre and distance of the centre from the GeoNames centroid."""
    ent = [_f(r["entfernung"]) for r in data]
    ent = [e for e in ent if e is not None]
    return {
        "records": len(data),
        "unique_ids": len({r["adressid"] for r in data}),
        "ent_min": round(min(ent), 2) if ent else None,
        "ent_max": round(max(ent), 2) if ent else None,
        "centre": (float(data[0]["inbreite"]), float(data[0]["inlaenge"])) if data else None,
        "sorted_by_entfernung": ent == sorted(ent),
        "vorort": dict(Counter(r["pruefungvorortmoeglich"] for r in data)),
        "keinestrasse": dict(Counter(r["keinestrasse"] for r in data)),
        "plzorder": dict(Counter(r["plzorder"] for r in data)),
    }


def site_vs_plz_centroid_km(data: list[dict], centroids) -> list[float]:
    """Distance of each located site from the GeoNames centroid of its own PLZ (sites with coordinates and a known PLZ)."""
    lookup = centroids.set_index("zip")
    out = []
    for r in data:
        lat, lon, plz = _f(r.get("lat")), _f(r.get("lon")), str(r.get("plz") or "").strip().zfill(5)
        if lat is None or lon is None or plz not in lookup.index:
            continue
        out.append(float(haversine_km(lat, lon, lookup.loc[plz, "lat"], lookup.loc[plz, "lng"])))
    return out


def analyze(run: Path) -> Path:
    run = Path(run)
    centroids = load_plz_centroids(paths.geonames_dir() / "DE.txt")
    lookup = centroids.set_index("zip")
    log = (run / paths.QUERY_LOG_FILE).read_text(encoding="utf-8").splitlines()
    lines = [f"# KÜS recon summary: {run.name}", "", f"Live requests in this folder: {len(log) - 1} (see query_log.csv).", ""]

    steps = sorted(p.name.split("_")[0] for p in (run / "requests").glob("*_response.json"))
    api_steps = [s for s in steps if s[0] in "abcdehx"]

    # ---- per response
    lines += ["## 1. Per response", "", "| step | request | records | max entfernung km | centre (lat, lon) | centre vs GeoNames km | plzorder | vorort 1/0 |", "|---|---|---|---|---|---|---|---|"]
    all_sites: dict[str, dict] = {}
    for step in api_steps:
        data = _load(run, step)
        meta = _meta(run, step)
        query = meta["url"].split("/api/kues/")[1]
        if not isinstance(data, list) or not data or "inbreite" not in data[0]:
            lines.append(f"| {step} | `{query}` | {len(data) if isinstance(data, list) else '-'} | | | | | |")
            continue
        st = response_stats(data, centroids)
        plz = next((p.split("=")[1] for p in query.split("?")[1].split("&") if p.startswith("plz=")), None)
        off = ""
        if plz in lookup.index:
            off = f"{float(haversine_km(*st['centre'], lookup.loc[plz, 'lat'], lookup.loc[plz, 'lng'])):.2f}"
        v = st["vorort"]
        lines.append(f"| {step} | `{query}` | {st['records']} | {st['ent_max']} | {st['centre'][0]}, {st['centre'][1]} | {off} | {st['plzorder']} | {v.get('1', 0)}/{v.get('0', 0)} |")
        if step[0] in "aeb" or step == "x01":
            for r in data:
                all_sites.setdefault(r["adressid"], r)
    lines.append("")

    # ---- repeatability
    saved = paths.reference_dir() / "api_samples" / "kues" / SAVED_80636
    a01 = _load(run, "a01")
    if a01 is not None and saved.exists():
        old = json.loads(saved.read_text(encoding="utf-8"))
        same_ids = [r["adressid"] for r in old] == [r["adressid"] for r in a01]
        diffs = {}
        old_by = {r["adressid"]: r for r in old}
        for r in a01:
            o = old_by.get(r["adressid"])
            if o:
                for k in FIELDS:
                    if o.get(k) != r.get(k):
                        diffs[k] = diffs.get(k, 0) + 1
        lines += ["## 2. Re-query of 80636 vs the saved file (same day, browser capture vs our bare request)", "",
                  f"- same adressids in the same order: **{same_ids}** ({len(old)} saved, {len(a01)} now)",
                  f"- fields that differ (count of records): {diffs or 'none'}", ""]

    # ---- parameters
    b01, b02, x01, b03 = _load(run, "b01"), _load(run, "b02"), _load(run, "x01"), _load(run, "b03")
    lines += ["## 3. Parameters", ""]
    if b01 is not None:
        lines.append(f"- coordinates without plz (`latitude`/`longitude`): {len(b01)} records (empty list)")
    if b02 is not None and a01 is not None:
        lines.append(f"- plz plus other coordinates: identical to plz alone: **{[r['adressid'] for r in b02] == [r['adressid'] for r in a01]}**, centre {b02[0]['inbreite']}, {b02[0]['inlaenge']} (the coordinates are ignored)")
    if x01 is not None:
        st = response_stats(x01, centroids)
        lines.append(f"- candidate radius names with different values (radius 60, umkreis 70, entfernung 80, **km 90**, distance 55, maxdistance 65, range 75): max entfernung {st['ent_max']} km -> the parameter that counts is **`km`**; {st['records']} records")
    if b03 is not None:
        st = response_stats(b03, centroids)
        lines.append(f"- all candidates at 200 (km 200, limit/max/top 500): max entfernung {st['ent_max']} km, {st['records']} records -> `km` is **capped at 100 km**")
    e01 = _load(run, "e01")
    if e01 is not None:
        st = response_stats(e01, centroids)
        lines.append(f"- 44135 with km=100: {st['records']} records, max {st['ent_max']} km (no count cap at 100 or 200 records)")
    c01 = _load(run, "c01")
    if c01 is not None:
        lines.append(f"- `plzdaten?term=80636`: {c01} (PLZ autocomplete of the page: plz and ort only, no coordinates)")
    lines.append("")

    # ---- radius, cap, centres
    lines += ["## 4. Radius, count and server centre", ""]
    centre_off = []
    for step in api_steps:
        data = _load(run, step)
        if not isinstance(data, list) or not data or "inbreite" not in data[0]:
            continue
        meta = _meta(run, step)
        q = meta["url"].split("?")[1]
        plz = next((p.split("=")[1] for p in q.split("&") if p.startswith("plz=")), None)
        if plz in lookup.index and step.startswith("a"):
            c = (float(data[0]["inbreite"]), float(data[0]["inlaenge"]))
            centre_off.append((plz, round(float(haversine_km(*c, lookup.loc[plz, "lat"], lookup.loc[plz, "lng"])), 2)))
    lines.append(f"- server centre vs GeoNames centroid per queried PLZ (km): {centre_off}")
    # does entfernung agree with the great-circle distance from the centre?
    gaps = []
    for step in api_steps:
        data = _load(run, step)
        if isinstance(data, list) and data and "inbreite" in data[0] and step in ("a01", "a03", "a04", "a05", "a07", "d01"):
            for r in data:
                lat, lon = _f(r.get("lat")), _f(r.get("lon"))
                if lat is not None and lon is not None:
                    gaps.append(abs(float(haversine_km(float(r["inbreite"]), float(r["inlaenge"]), lat, lon)) - float(r["entfernung"])))
    if gaps:
        lines.append(f"- |haversine(centre, site) - entfernung|: max {max(gaps):.3f} km, median {float(np.median(gaps)):.3f} km over {len(gaps)} located sites")
    lines.append("")

    # ---- plzorder
    lines += ["## 5. plzorder", ""]
    for step in ("a04", "a07"):
        data = _load(run, step)
        if data:
            plz = _meta(run, step)["url"].split("plz=")[1][:5]
            inside = [r for r in data if r["plz"] == plz]
            order_inside = Counter(r["plzorder"] for r in inside)
            order_outside = Counter(r["plzorder"] for r in data if r["plz"] != plz)
            lines.append(f"- {step} (PLZ {plz}): records with that PLZ: {len(inside)}, their plzorder {dict(order_inside)}; other records' plzorder {dict(order_outside)}")
    lines.append("")

    # ---- technik vs kues
    lines += ["## 6. partnersuche-kues-technik vs partnersuche (same PLZ)", ""]
    for kues_step, tech_step in (("a01", "a02"), ("a04", "a06")):
        k, t = _load(run, kues_step), _load(run, tech_step)
        if k is None or t is None:
            continue
        ki, ti = {r["adressid"]: r for r in k}, {r["adressid"]: r for r in t}
        both = set(ki) & set(ti)
        same = sum(1 for i in both if all(ki[i].get(f) == ti[i].get(f) for f in FIELDS if f != "entfernung"))
        keys_t = sorted(set().union(*(set(r) for r in t)) - set(FIELDS))
        lines.append(
            f"- PLZ {_meta(run, kues_step)['url'].split('plz=')[1][:5]}: kues {len(ki)} records, technik {len(ti)}; in both lists: {len(both)} ({same} with identical content); technik only: {len(set(ti) - set(ki))}; "
            f"technik vorort {dict(Counter(r['pruefungvorortmoeglich'] for r in t))}; extra fields in technik: {keys_t or 'none'}; same field set: {all(set(r) == set(FIELDS) for r in t)}"
        )
        # name patterns of the technik-only records (no personal data: licensee codes and counts)
        only = [ti[i] for i in set(ti) - set(ki)]
        lines.append(f"  - technik-only licensees: {dict(Counter(r['lizenznehmer'] for r in only).most_common(8))}")
    lines.append("")

    # ---- abroad
    lines += ["## 7. Records outside Germany", ""]
    outside = []
    for step in api_steps:
        data = _load(run, step)
        if not isinstance(data, list):
            continue
        for r in data:
            lat, lon = _f(r.get("lat")), _f(r.get("lon"))
            if lat is not None and lon is not None and not (DE_BOX[0] <= lat <= DE_BOX[1] and DE_BOX[2] <= lon <= DE_BOX[3]):
                outside.append((step, r["adressid"], r["plz"], r["ort"]))
    unknown_plz = {(s, r["adressid"]): r["plz"] for s in api_steps for r in (_load(run, s) or []) if isinstance(r, dict) and str(r.get("plz")).zfill(5) not in lookup.index}
    lines.append(f"- records with coordinates outside Germany's box {DE_BOX}: {len(outside)} {outside[:10]}")
    lines.append(f"- records whose PLZ is not a regular German PLZ in GeoNames: {len(unknown_plz)} {sorted(set(unknown_plz.values()))[:15]}")
    lines.append("")

    # ---- site vs own PLZ centroid
    lines += ["## 8. Distance of a site from the GeoNames centroid of its own PLZ (justifies COVER_KM)", ""]
    dist = site_vs_plz_centroid_km(list(all_sites.values()), centroids)
    if dist:
        arr = np.array(dist)
        lines.append(f"- {len(arr)} located sites (unique adressid over the kues responses): median {np.median(arr):.1f} km, p95 {np.percentile(arr, 95):.1f} km, p99 {np.percentile(arr, 99):.1f} km, **max {arr.max():.1f} km**")
    lines.append("")

    out = run / "summary.md"
    if out.exists():
        n = 2
        while (run / f"summary_{n}.md").exists():
            n += 1
        out = run / f"summary_{n}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
