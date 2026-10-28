"""One-off DEKRA recon: probe POST /api/locations/fromBoundaries with 13 request variants (R1..R13).

Not an adapter. Run from the project root:

    .venv\\Scripts\\python.exe 1_scrapers/dekra/src/recon.py fetch      # LIVE, 13 requests, ~2 min
    .venv\\Scripts\\python.exe 1_scrapers/dekra/src/recon.py analyze <run_folder>   # offline -> summary.md

Output goes to 1_scrapers/dekra/test_output/<YYYY-MM-DD_HHMM>_recon/ (git-ignored, contains contact data).
A run never overwrites: the folder gets a _2/_3 suffix on a same-minute clash, files are opened with "x".
"""

from __future__ import annotations

import copy
import csv
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from core import paths

SLUG = "dekra"
URL = "https://www.dekra.de/api/locations/fromBoundaries"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.dekra.de",
    "Referer": "https://www.dekra.de/de/standorte/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}
PAUSE_S = 5.0
JITTER_S = 2.0
TIMEOUT_S = 60

AUTO = "2fbbb592-6bd8-4b57-860d-52b34c6cbbb1"
ALL_TYPES = [
    AUTO,
    "68a4d9fa-9295-4a62-8f2b-db5dcfc11756",
    "84d03801-02a5-4a5c-b0c1-4eb993b164a9",
    "051adab5-009c-4a62-8b86-e2c05ea167eb",
    "b6fa213c-6633-404b-94d7-7bebba82c25c",
    "c1fa7185-f287-4803-8131-cd70d4cf160b",
    "079443a2-f91b-40fc-bbd0-8a4151dc4b95",
]
# order of the site's filter list (ASSUMPTION, verified in `analyze` against the records)
ASSUMED_TYPE_NAMES = dict(zip(ALL_TYPES, [
    "Automobil Prüfwesen und Gutachten",
    "Industrie, Bau und Immobilien",
    "Fahrerlaubniswesen",
    "Begutachtungsstellen für Fahreignung",
    "Arbeitsmedizin",
    "Bildung",
    "Zeitarbeit",
]))

BERLIN = {"lat": 52.530741, "lng": 13.4058114}
MUENCHEN = {"lat": 48.137, "lng": 11.575}
CAPTURED_BBOX = {
    "latFrom": 46.698823048624014, "latTo": 55.43868434143115,
    "lngFrom": 1.819349454986634, "lngTo": 15.969740079986634,
}
WORLD = {"latFrom": -60, "latTo": 75, "lngFrom": -180, "lngTo": 180}
EUROPE = {"latFrom": 30, "latTo": 72, "lngFrom": -25, "lngTo": 45}
GERMANY = {"latFrom": 46.9, "latTo": 55.4, "lngFrom": 5.5, "lngTo": 15.4}
_MID_LAT = (GERMANY["latFrom"] + GERMANY["latTo"]) / 2
_MID_LNG = (GERMANY["lngFrom"] + GERMANY["lngTo"]) / 2
QUARTERS = {  # Q1 north-west, Q2 north-east, Q3 south-west, Q4 south-east
    "Q1": {"latFrom": _MID_LAT, "latTo": GERMANY["latTo"], "lngFrom": GERMANY["lngFrom"], "lngTo": _MID_LNG},
    "Q2": {"latFrom": _MID_LAT, "latTo": GERMANY["latTo"], "lngFrom": _MID_LNG, "lngTo": GERMANY["lngTo"]},
    "Q3": {"latFrom": GERMANY["latFrom"], "latTo": _MID_LAT, "lngFrom": GERMANY["lngFrom"], "lngTo": _MID_LNG},
    "Q4": {"latFrom": GERMANY["latFrom"], "latTo": _MID_LAT, "lngFrom": _MID_LNG, "lngTo": GERMANY["lngTo"]},
}

P_AUTO = {
    "locale": "de_DE",
    "searchedLocation": BERLIN,
    "tagFilters": [],
    "initialSearch": False,
    "boundaries": CAPTURED_BBOX,
    "types": [AUTO],
    "commercialVehicles": False,
    "locationsWithoutType": False,
}
P_ALL = {**P_AUTO, "types": list(ALL_TYPES)}


def variant(base: dict, **changes) -> dict:
    body = copy.deepcopy(base)
    body.update(copy.deepcopy(changes))
    return body


def build_requests(world: dict) -> list[tuple[str, str, dict]]:
    """(id, description, body) for R1..R13. `world` is WORLD, or EUROPE after a fallback."""
    reqs = [
        ("R01", "P_AUTO exactly as captured", P_AUTO),
        ("R02", "P_AUTO, searchedLocation Muenchen", variant(P_AUTO, searchedLocation=MUENCHEN)),
        ("R03", "P_ALL, bbox GERMANY", variant(P_ALL, boundaries=GERMANY)),
    ]
    for i, (q, bbox) in enumerate(QUARTERS.items(), start=4):
        reqs.append((f"R{i:02d}", f"P_ALL, bbox {q}", variant(P_ALL, boundaries=bbox)))
    reqs += [
        ("R08", "P_AUTO, bbox WORLD", variant(P_AUTO, boundaries=world)),
        ("R09", "P_ALL types=[], bbox WORLD", variant(P_ALL, types=[], boundaries=world)),
        ("R10", "types=[], locationsWithoutType=true, bbox WORLD",
         variant(P_ALL, types=[], locationsWithoutType=True, boundaries=world)),
        ("R11", "P_AUTO, bbox WORLD, locationsWithoutType=true",
         variant(P_AUTO, boundaries=world, locationsWithoutType=True)),
        ("R12", "P_AUTO, bbox WORLD, commercialVehicles=true",
         variant(P_AUTO, boundaries=world, commercialVehicles=True)),
        ("R13", "P_ALL, bbox WORLD", variant(P_ALL, boundaries=world)),
    ]
    return reqs


# ---------------------------------------------------------------- response shape

def find_record_list(data):
    """Return (path, list) of the first list of dicts in a response, or (None, None).

    Path is () for a top-level list, else the tuple of dict keys leading to it. An empty list
    is accepted only at a key/top level (we cannot tell what it holds), so it returns its path too.
    """
    if isinstance(data, list):
        return (), data
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, list) and (not value or isinstance(value[0], dict)):
                return (key,), value
    return None, None


class Stop(Exception):
    """Unexpected status or shape: stop the run and report."""


def post_once(body: dict) -> requests.Response:
    return requests.post(URL, headers=HEADERS, data=json.dumps(body), timeout=TIMEOUT_S)


def post_with_retry(body: dict, log) -> tuple[requests.Response, int]:
    for attempt in (1, 2):
        try:
            return post_once(body), attempt
        except (requests.Timeout, requests.ConnectionError) as exc:
            log(f"    {type(exc).__name__} on attempt {attempt}: {exc}")
            if attempt == 2:
                raise Stop(f"timeout/connection error twice: {exc}") from exc
            time.sleep(PAUSE_S + random.uniform(0, JITTER_S))
    raise AssertionError("unreachable")


def write_new(path: Path, data: bytes | str) -> None:
    mode = "xb" if isinstance(data, bytes) else "x"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as fh:
        fh.write(data)


def run_fetch() -> Path:
    started = datetime.now()
    run = paths.new_run_dir(SLUG, "recon", started)
    (run / "requests").mkdir()
    print(f"run folder: {run}")

    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    world, world_name = WORLD, "WORLD"
    shape: dict | None = None  # record-list path + top-level keys, fixed by R1
    r1_count = None
    rows: list[dict] = []
    stop_reason = None
    pending = build_requests(world)
    idx = 0
    first = True
    while idx < len(pending):
        rid, desc, body = pending[idx]
        if not first:
            time.sleep(PAUSE_S + random.uniform(0, JITTER_S))
        first = False
        log(f"{rid}: {desc}")
        t0 = time.time()
        try:
            resp, attempts = post_with_retry(body, log)
        except Stop as exc:
            stop_reason = f"{rid}: {exc}"
            break
        elapsed = time.time() - t0
        write_new(run / "requests" / f"{rid.lower()}_body.json", json.dumps(body, indent=2))
        write_new(run / "requests" / f"{rid.lower()}_response.json", resp.content)

        n = None
        note = ""
        is_world = body["boundaries"] == world and rid >= "R08"
        if resp.status_code >= 400:
            if is_world and world_name == "WORLD" and rid == "R08" and resp.status_code < 500:
                # sanctioned exception to "stop on 4xx": the server may reject the WORLD box
                note = f"WORLD rejected with HTTP {resp.status_code}: {resp.text[:300]!r} -> falling back to EUROPE"
                log(f"    {note}")
                world, world_name = EUROPE, "EUROPE"
                pending = pending[: idx] + build_requests(world)[idx:]
                rows.append(_row(rid, desc, resp, attempts, elapsed, n, note))
                # rename the rejected exchange so the retry with EUROPE can use the same id
                for suffix in ("body", "response"):
                    src = run / "requests" / f"{rid.lower()}_{suffix}.json"
                    src.rename(run / "requests" / f"{rid.lower()}_{suffix}_rejected_world.json")
                continue
            stop_reason = f"{rid}: HTTP {resp.status_code}: {resp.text[:500]!r}"
            rows.append(_row(rid, desc, resp, attempts, elapsed, n, "STOP"))
            break
        try:
            data = resp.json()
        except ValueError:
            stop_reason = f"{rid}: HTTP {resp.status_code} but body is not JSON: {resp.text[:500]!r}"
            rows.append(_row(rid, desc, resp, attempts, elapsed, n, "STOP"))
            break
        path, records = find_record_list(data)
        if path is None:
            stop_reason = f"{rid}: no list of records found in response: {resp.text[:500]!r}"
            rows.append(_row(rid, desc, resp, attempts, elapsed, n, "STOP"))
            break
        n = len(records)
        keys = sorted(data) if isinstance(data, dict) else []
        if shape is None:
            shape = {"path": path, "keys": keys}
            log(f"    shape fixed by {rid}: record list at {list(path) or 'top level'}, top-level keys {keys}")
        elif shape["path"] != path or shape["keys"] != keys:
            stop_reason = (f"{rid}: response shape differs from R1 "
                           f"(path {list(path)} keys {keys} vs {list(shape['path'])} {shape['keys']})")
            rows.append(_row(rid, desc, resp, attempts, elapsed, n, "STOP"))
            break
        if rid == "R01":
            r1_count = n
        if rid == "R08" and world_name == "WORLD" and n == 0 and r1_count:
            # a silent rejection: WORLD box returns nothing while Germany has records
            note = "WORLD returned 0 records while R1 has some -> falling back to EUROPE"
            log(f"    {note}")
            world, world_name = EUROPE, "EUROPE"
            pending = pending[: idx] + build_requests(world)[idx:]
            rows.append(_row(rid, desc, resp, attempts, elapsed, n, note))
            for suffix in ("body", "response"):
                src = run / "requests" / f"{rid.lower()}_{suffix}.json"
                src.rename(run / "requests" / f"{rid.lower()}_{suffix}_rejected_world.json")
            continue
        log(f"    HTTP {resp.status_code}, {len(resp.content)} bytes, {n} records, {elapsed:.1f}s")
        rows.append(_row(rid, desc, resp, attempts, elapsed, n, note))
        idx += 1

    with open(run / "query_log.csv", "x", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["request"])
        w.writeheader()
        w.writerows(rows)
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "url": URL,
        "world_bbox_used": world_name,
        "record_list_path": list(shape["path"]) if shape else None,
        "top_level_keys": shape["keys"] if shape else None,
        "completed_requests": [r["request"] for r in rows if r["records"] is not None and r["note"] == ""],
        "stopped": stop_reason,
    }
    write_new(run / "run_meta.json", json.dumps(meta, indent=2, ensure_ascii=False))
    if stop_reason:
        log(f"STOPPED: {stop_reason}")
    else:
        log("all 13 requests done")
    return run


def _row(rid, desc, resp, attempts, elapsed, n, note) -> dict:
    return {
        "request": rid, "description": desc, "http_status": resp.status_code,
        "bytes": len(resp.content), "records": n, "attempts": attempts,
        "elapsed_s": round(elapsed, 1), "note": note,
    }


# ---------------------------------------------------------------- analysis (offline)

MOABIT_BOOKING_UUID = "d77c878f-7cfb-44a5-ac95-14cfe24e6177"
CONTACT_FIELDS = {"phone1", "phone2", "phoneMobile", "fax", "email",
                  "department1Email", "department1Phone", "department2Email", "department2Phone"}
TEXT_FIELDS = ["freeText1", "freeText2", "services", "currentInformation", "openingHoursAreas"]
NAME_PREFIXES = ["Kfz-Prüfstelle", "Station", "Standort", "Niederlassung"]


NULLABLE_DICTS = ("countryResolved", "country")
NULLABLE_LISTS = ("locationTypesResolved", "locationTypes", "specialServicesList", "tags")
NULL_FIELDS: dict[str, set] = {}


def load_responses(run: Path) -> dict[str, list[dict]]:
    out = {}
    for f in sorted((run / "requests").glob("r??_response.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        recs = data["locations"]
        for r in recs:  # null -> empty container so the analysis can index safely; NULL_FIELDS counts the originals
            for k in NULLABLE_DICTS:
                if r.get(k) is None:
                    NULL_FIELDS.setdefault(k, set()).add(r["id"])
                    r[k] = {}
            for k in NULLABLE_LISTS:
                if r.get(k) is None:
                    NULL_FIELDS.setdefault(k, set()).add(r["id"])
                    r[k] = []
        out[f.name[:3].upper()] = recs
    return out


def is_empty(v) -> bool:
    if v is None or v == "" or v == [] or v == {}:
        return True
    if isinstance(v, dict):
        t = v.get("type")
        if t == "RichText":
            return not v.get("elements")
        if t == "Date":
            return not v.get("date")
        if t == "Link":
            return not v.get("to")
    return False


def strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings(x)


def walk(v, path=""):
    """Yield (path, dict) for every dict inside v; list indexes are dropped from the path."""
    if isinstance(v, dict):
        yield path, v
        for k, x in v.items():
            yield from walk(x, f"{path}.{k}" if path else k)
    elif isinstance(v, list):
        for x in v:
            yield from walk(x, path)


def md_table(header, rows) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |" for r in rows]
    return "\n".join(lines)


def label(rec: dict) -> str:
    return f"{rec.get('name', '?')} ({rec.get('zipCode', '')} {rec.get('city', '')})".replace("\n", " ")


def type_names(rec: dict) -> list[str]:
    return [t["name"] for t in rec.get("locationTypesResolved") or []]


def next_free(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.stem}_{n}{path.suffix}").exists():
        n += 1
    return path.with_name(f"{path.stem}_{n}{path.suffix}")


def run_analyze(run: Path) -> Path:
    from collections import Counter

    resp = load_responses(run)
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    qlog = list(csv.DictReader(open(run / "query_log.csv", encoding="utf-8")))
    out: list[str] = []
    w = out.append

    ids = {rid: [r["id"] for r in recs] for rid, recs in resp.items()}
    sets = {rid: set(v) for rid, v in ids.items()}
    by_id: dict[str, dict] = {}
    conflicting = set()
    for rid in sorted(resp):
        for r in resp[rid]:
            prev = by_id.get(r["id"])
            if prev is None:
                by_id[r["id"]] = r
            elif {k: v for k, v in prev.items() if k != "distance"} != {k: v for k, v in r.items() if k != "distance"}:
                conflicting.add(r["id"])
    diffs_rows: list[dict] = []

    def name_of(i):
        return label(by_id[i])

    def diff(a: str, b: str, title: str):
        added, removed = sorted(sets[b] - sets[a]), sorted(sets[a] - sets[b])
        w(f"**{title}**: {a}={len(sets[a])}, {b}={len(sets[b])}; added in {b}: {len(added)}, removed (only in {a}): {len(removed)}\n")
        for direction, lst in (("added", added), ("removed", removed)):
            for i in lst:
                diffs_rows.append({"pair": f"{a}_vs_{b}", "direction": direction, "id": i,
                                   "name": by_id[i].get("name"), "zip": by_id[i].get("zipCode"),
                                   "city": by_id[i].get("city"), "country": by_id[i]["countryResolved"].get("code"),
                                   "types": "; ".join(type_names(by_id[i]))})
            if lst:
                shown = lst[:40]
                w(f"- {direction} ({len(lst)}):" + (" (first 40, all in `diffs.csv`)" if len(lst) > 40 else ""))
                for i in shown:
                    w(f"  - {name_of(i)} [{by_id[i]['countryResolved'].get('code')}]")
                w("")

    w(f"# DEKRA recon summary\n\nRun folder: `{run.name}`; started {meta['started_at']}; "
      f"endpoint `POST {URL}`; WORLD box accepted: **{'yes' if meta['world_bbox_used'] == 'WORLD' else 'NO, fell back to ' + meta['world_bbox_used']}**"
      f"; stopped early: {meta['stopped'] or 'no'}.\n")
    w("Quarters: Q1 north-west, Q2 north-east, Q3 south-west, Q4 south-east (split at lat "
      f"{_MID_LAT:.2f}, lng {_MID_LNG:.2f}). Record identity below is the `id` field.\n")

    # ---------------- 1
    w("## 1. Response shape and IDs\n")
    w(f"- Every response is a JSON object with the top-level keys `{meta['top_level_keys']}`; the record list is `locations` "
      f"(`isNextLocation` is a boolean, no total/count field). `isNextLocation` values seen: "
      f"{Counter(json.loads((run / 'requests' / f'{rid.lower()}_response.json').read_text(encoding='utf-8'))['isNextLocation'] for rid in sorted(resp))}.")
    dup = {rid: len(v) - len(set(v)) for rid, v in ids.items()}
    w(f"- `id` (UUID) is the record id: duplicates inside a single response: {dict((k, v) for k, v in dup.items() if v) or 'none'}; "
      f"records with the same `id` but different content across responses (ignoring `distance`): {len(conflicting)}.")
    uniq = Counter(r.get("uniqueImportId") for r in by_id.values() if r.get("uniqueImportId"))
    w(f"- `uniqueImportId` exists on {sum(uniq.values())} of {len(by_id)} universe records, duplicated values: {sum(1 for c in uniq.values() if c > 1)}.")
    moabit = [r for r in by_id.values() if MOABIT_BOOKING_UUID in json.dumps(r)]
    for r in moabit:
        where = [k for k, v in r.items() if MOABIT_BOOKING_UUID in json.dumps(v)]
        w(f"- Moabit booking UUID `{MOABIT_BOOKING_UUID}` appears in the record `{r['name']}` (`id` = `{r['id']}`) **only inside field(s) {where}**; it is NOT the record `id`.")
    if not moabit:
        w("- Moabit booking UUID not found in any record.")
    w(f"- Moabit record in: {[rid for rid in sorted(resp) if any(r['id'] == moabit[0]['id'] for r in resp[rid])] if moabit else 'n/a'}")
    uuid_re = re.compile(r"/location/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
    with_link = [r for r in by_id.values() if not is_empty(r.get("linkOnlineAppointment"))]
    booking_by_rec = {r["id"]: (uuid_re.search(r["linkOnlineAppointment"]["to"]) or [None, None])[1] for r in with_link}
    booking_ids = [b.lower() for b in booking_by_rec.values() if b]
    shared = Counter(booking_ids)
    shared_recs = {b: [label(by_id[i]) for i, x in booking_by_rec.items() if x and x.lower() == b] for b, c in shared.items() if c > 1}
    w(f"- Booking links (`linkOnlineAppointment.to`): {len(with_link)} records; {len(booking_ids)} contain a `/location/<uuid>` (the rest are other URL forms: "
      f"{len(with_link) - len(booking_ids)}); {len(set(booking_ids))} distinct UUIDs; UUIDs shared by several records: {len(shared_recs)} "
      f"({'; '.join(f'{b}: ' + ' / '.join(v) for b, v in list(shared_recs.items())[:5])}); "
      f"booking UUID equal to the record `id`: {sum(1 for i, b in booking_by_rec.items() if b and b.lower() == i)}. "
      f"Link hosts: {dict(Counter(re.sub(r'^https?://([^/]+).*', r'\1', r['linkOnlineAppointment']['to']) for r in with_link).most_common())}.\n")

    # ---------------- 2
    w("## 2. Counts and set differences\n")
    w(md_table(["Request", "Description", "HTTP", "MB", "Records", "Unique ids"],
               [[q["request"], q["description"], q["http_status"], f"{int(q['bytes']) / 1e6:.1f}", q["records"],
                 len(sets.get(q["request"], ()))] for q in qlog if q["records"]]))
    w("")
    diff("R01", "R02", "R1 vs R2 (searchedLocation Berlin vs München)")
    same_content = all({k: v for k, v in a.items() if k != "distance"} == {k: v for k, v in b.items() if k != "distance"}
                       for a in resp["R01"] for b in resp["R02"] if a["id"] == b["id"])
    w(f"Record content identical except `distance`: {same_content}. `distance` is relative to searchedLocation.\n")
    uq = set().union(*(sets[f"R{i:02d}"] for i in range(4, 8)))
    qsum = sum(len(sets[f"R{i:02d}"]) for i in range(4, 8))
    w(f"**R3 vs union(R4..R7)**: R3={len(sets['R03'])}, union={len(uq)} (sum of quarters {qsum}, i.e. {qsum - len(uq)} on a quarter border returned twice); "
      f"only in R3: {len(sets['R03'] - uq)}, only in the union: {len(uq - sets['R03'])} -> "
      f"**{'identical, no cap visible' if sets['R03'] == uq else 'NOT identical, possible cap'}**.\n")
    for a, b, t in [("R01", "R08", "R1 vs R8 (P_AUTO: captured box vs WORLD)"),
                    ("R01", "R11", "R1 vs R11 (P_AUTO + locationsWithoutType=true, WORLD)"),
                    ("R01", "R12", "R1 vs R12 (P_AUTO + commercialVehicles=true, WORLD)")]:
        diff(a, b, t)
    w(f"**R8 vs R11**: R11 minus R8 = {len(sets['R11'] - sets['R08'])}, R8 minus R11 = {len(sets['R08'] - sets['R11'])}. "
      f"**R8 vs R12**: R12 minus R8 = {len(sets['R12'] - sets['R08'])}, R8 minus R12 = {len(sets['R08'] - sets['R12'])}.\n")
    universe = sets["R10"] | sets["R13"]
    no_type = universe - sets["R13"]
    w(f"**R9 (types=[], locationsWithoutType=false)**: {len(sets['R09'])} records; identical id set to R10: {sets['R09'] == sets['R10']}; "
      f"R9 ⊇ R13: {sets['R13'] <= sets['R09']}; R9 minus R13: {len(sets['R09'] - sets['R13'])}. "
      f"-> empty `types` does **not** mean 'nothing'; it returns "
      f"{'the whole universe' if sets['R09'] == universe else 'a subset of the universe'} "
      f"({len(sets['R09'])} of {len(universe)}), independent of locationsWithoutType.\n")
    w(f"**Universe = R10 ∪ R13**: {len(universe)}. R13 ⊆ R10: {sets['R13'] <= sets['R10']}. "
      f"Universe minus R13 = **{len(no_type)}** records that R13 (the 7 known types) does not return.\n")
    typed = Counter()
    for i in no_type:
        typed[tuple(type_names(by_id[i]))] += 1
    w("Type sets of these " + str(len(no_type)) + " records (`locationTypesResolved` names):\n")
    w(md_table(["Types", "Count"], [[" + ".join(k) if k else "(none)", v] for k, v in typed.most_common()]))
    w("")
    w("Country of these records: " + str(dict(Counter(by_id[i]["countryResolved"].get("code") for i in no_type).most_common())) + "\n")
    w(f"R11 (P_AUTO + locationsWithoutType) added {len(sets['R11'] - sets['R08'])} records to R8; of those with no type at all: "
      f"{sum(1 for i in sets['R11'] - sets['R08'] if not by_id[i]['locationTypes'])}. "
      f"Records with an empty `locationTypes` in the universe: {sum(1 for r in by_id.values() if not r['locationTypes'])}.\n")
    w("Full list of universe-minus-R13 (name, address, type names, specialServices, services text): `no_type_locations.csv` in this folder "
      f"({len(no_type)} rows). Below: every such record in Germany that is NOT a 'WHG Standort' (water-law certification sites), i.e. the ones that could matter; "
      "`services` (RichText) is empty everywhere, so the services column shows `specialServicesList`.\n")
    de_rest = [by_id[i] for i in sorted(no_type, key=lambda x: (by_id[x].get("zipCode") or ""))
               if by_id[i]["countryResolved"].get("code") == "DE" and "WHG Standort" not in type_names(by_id[i])]
    w(f"{len(de_rest)} records (DE, not WHG):\n")
    w(md_table(["Name", "Address", "Types", "specialServicesList", "Booking link"],
               [[r["name"], f"{r.get('street', '')} {r.get('houseNumber', '')}, {r.get('zipCode')} {r.get('city')}".strip(),
                 " + ".join(type_names(r)) or "(none)", "; ".join(o["value"] for o in r["specialServicesList"]),
                 bool(not is_empty(r.get("linkOnlineAppointment")))] for r in de_rest]))
    w("")
    with open(next_free(run / "no_type_locations.csv"), "x", newline="", encoding="utf-8") as fh:
        cw = csv.writer(fh)
        cw.writerow(["id", "name", "name2", "street", "houseNumber", "zipCode", "city", "country", "types", "specialServicesList", "services_text", "tags", "uniqueImportId"])
        for i in sorted(no_type, key=lambda x: (by_id[x]["countryResolved"].get("code") or "", by_id[x].get("zipCode") or "")):
            r = by_id[i]
            cw.writerow([i, r.get("name"), r.get("name2"), r.get("street"), r.get("houseNumber"), r.get("zipCode"), r.get("city"),
                         r["countryResolved"].get("code"), "; ".join(type_names(r)),
                         "; ".join(o["value"] for o in r["specialServicesList"]), " ".join(strings(r["services"])),
                         "; ".join(t["name"] for t in r["tags"]), r.get("uniqueImportId")])

    # ---------------- 3
    w("## 3. Type UUID -> name\n")
    tmap: dict[str, set] = {}
    for r in by_id.values():
        for t in r["locationTypesResolved"]:
            tmap.setdefault(t["id"], set()).add((t["key"], t["name"]))
    rows = []
    for tid in list(ALL_TYPES) + [t for t in tmap if t not in ALL_TYPES]:
        found = sorted(tmap.get(tid, []))
        names = "; ".join(f"{n} (key `{k}`)" for k, n in found) or "(not in any record)"
        ok = ("OK" if found and all(n == ASSUMED_TYPE_NAMES.get(tid) for _, n in found) else
              "not in the 7 requested" if tid not in ALL_TYPES else "**MISMATCH**")
        rows.append([f"`{tid}`", ASSUMED_TYPE_NAMES.get(tid, "-"), names, ok])
    w(md_table(["Type UUID", "Assumed name", "Name in records (locationTypesResolved)", "Check"], rows))
    r13 = resp["R13"]
    w("\n**R13 count per type** (a location can carry several):\n")
    cnt = Counter(t for r in r13 for t in type_names(r))
    w(md_table(["Type", "Locations"], [[k, v] for k, v in cnt.most_common()]))
    w("\n**R13 count per type combination**:\n")
    combo = Counter(tuple(sorted(type_names(r))) for r in r13)
    w(md_table(["Types", "Locations"], [[" + ".join(k) or "(none)", v] for k, v in combo.most_common()]))
    w("\nSame per single type over the whole universe (R10 ∪ R13):\n")
    cnt_u = Counter(t for r in by_id.values() for t in type_names(r))
    w(md_table(["Type", "Locations"], [[k, v] for k, v in cnt_u.most_common()]))
    w("")

    # ---------------- 4
    w("## 4. Field profile over the universe\n")
    n_u = len(by_id)
    fill = Counter()
    keys_seen = Counter()
    for r in by_id.values():
        for k, v in r.items():
            keys_seen[k] += 1
            if not is_empty(v):
                fill[k] += 1
    rows = [[k, keys_seen[k], f"{100 * keys_seen[k] / n_u:.0f}%", fill[k], f"{100 * fill[k] / n_u:.0f}%",
             type(next(r[k] for r in by_id.values() if k in r)).__name__] for k in sorted(keys_seen, key=lambda k: -fill[k])]
    w(f"`deleted` values over the universe: {dict(Counter(r['deleted'] for r in by_id.values()))}.\n")
    w(f"{n_u} records. *present* = key exists, *filled* = not null/''/[]/empty RichText/empty Date/Link without target.\n")
    w(md_table(["Field", "present", "%", "filled", "%", "JSON type"], rows))
    w("\n**Option-type values** (dicts with `type: Option`; key = UUID), counted over the universe:\n")
    opt = Counter()
    for r in by_id.values():
        for k, v in r.items():
            if k in ("countryResolved", "country"):
                continue
            for path, d in walk(v, k):
                if d.get("type") == "Option":
                    opt[(path, d.get("key"), d.get("value"))] += 1
    w(md_table(["Field path", "Key (UUID)", "Value", "Records"], [[p, f"`{k}`", v, c] for (p, k, v), c in sorted(opt.items())]))
    w("\n**Other type-tagged nested objects** (which `type`/`sectionType` values occur per field):\n")
    kinds = Counter()
    for r in by_id.values():
        for k in ("openingHoursAreas", "whgCertificatesList", "freeText1", "freeText2", "services", "currentInformation", "imageResolved", "image", "linkOnlineAppointment", "linkWebsite", "link", "linkOpeningHours"):
            for path, d in walk(r.get(k), k):
                if d.get("type") in ("Section", "Link", "RichText", "Date", "Media"):
                    kinds[(path, d.get("type"), d.get("sectionType", d.get("subType", "")))] += 1
    w(md_table(["Path", "type", "sectionType/subType", "Occurrences"], [[*k, v] for k, v in sorted(kinds.items())]))
    w("\n**Nfz suitability**: `suitableForCommercialVehicles` (boolean). Counts over the universe: "
      f"{dict(Counter(r['suitableForCommercialVehicles'] for r in by_id.values()))}; in R8 (P_AUTO): "
      f"{dict(Counter(r['suitableForCommercialVehicles'] for r in resp['R08']))}; "
      f"R12 (commercialVehicles=true) records with the flag true: {sum(1 for r in resp['R12'] if r['suitableForCommercialVehicles'])} of {len(resp['R12'])}; "
      f"R8 records with flag true: {sum(1 for r in resp['R08'] if r['suitableForCommercialVehicles'])}; "
      f"R12 == {{R8 records with flag true}}: {sets['R12'] == {r['id'] for r in resp['R08'] if r['suitableForCommercialVehicles']}}.\n")
    disp = Counter()
    for r in by_id.values():
        for k, v in r.items():
            for path, d in walk(v, k):
                if "displayed" in d:
                    disp[(path, d.get("sectionType", d.get("type", "")), d["displayed"])] += 1
    w("**`displayed`** belongs to CMS section objects (`type: Section`), not to the record itself. Occurrences by field path:\n")
    w(md_table(["Path", "sectionType", "displayed", "Count"], [[*k, v] for k, v in sorted(disp.items(), key=lambda x: str(x))]) if disp else "(none)")
    w("")

    # ---------------- 5
    w("## 5. Country\n")
    w("`countryResolved` (object with `code`, `key`, `name`) and `country` (a DatasetRef UUID). Records where the source had `null` instead of an object/list "
      f"(treated as empty in this analysis): { {k: len(v) for k, v in NULL_FIELDS.items()} or 'none' }. Counts over the universe:\n")
    cc = Counter((r["countryResolved"].get("code"), r["countryResolved"].get("name"), r["country"].get("referenceId")) for r in by_id.values())
    w(md_table(["Code", "Name", "country referenceId", "Records"], [[c, n, f"`{u}`", v] for (c, n, u), v in cc.most_common()]))
    cc13 = Counter(r["countryResolved"].get("code") for r in r13)
    w(f"\nIn R13 (7 types): {dict(cc13.most_common())}. In R3 (GERMANY box): {dict(Counter(r['countryResolved'].get('code') for r in resp['R03']).most_common())}. "
      f"In R8 (P_AUTO, WORLD): {dict(Counter(r['countryResolved'].get('code') for r in resp['R08']).most_common())}.\n")
    plz = set()
    root = paths.project_root()
    for line in open(paths.geonames_dir(root) / "DE.txt", encoding="utf-8"):
        parts = line.split("\t")
        if len(parts) > 1:
            plz.add(parts[1])
    de = [r for r in by_id.values() if r["countryResolved"].get("code") == "DE"]
    bad = [r for r in de if r.get("zipCode") not in plz]
    w(f"Cross-check with GeoNames DE PLZ ({len(plz)} codes): of {len(de)} records with code DE, {len(bad)} have a `zipCode` not in the GeoNames list"
      + (":\n" if bad else ".\n"))
    for r in bad[:40]:
        w(f"- {label(r)} (zip `{r.get('zipCode')}`)")
    non_de = [r for r in by_id.values() if r["countryResolved"].get("code") != "DE"]
    fixable = [r for r in bad if (r.get("zipCode") or "").strip().zfill(5) in plz]
    w(f"\nOf those {len(bad)}, {len(fixable)} become valid after `.strip().zfill(5)` (trailing space / lost leading zero); "
      f"zipCode values with whitespace in the universe: {sum(1 for r in by_id.values() if (r.get('zipCode') or '') != (r.get('zipCode') or '').strip())}; "
      f"4-digit zipCodes among DE: {sum(1 for r in de if len((r.get('zipCode') or '').strip()) == 4)}.")
    nc = [r for r in by_id.values() if not r["countryResolved"]]
    w(f"Records with `country`/`countryResolved` = null: {len(nc)}: " + "; ".join(f"{label(r)} [{', '.join(type_names(r)) or 'no type'}]" for r in nc) + ".")
    w(f"Non-DE records whose zipCode IS a valid German PLZ (possible mislabelled country; note 5-digit codes of FR/ES/US etc. collide with DE PLZ): "
      f"{sum(1 for r in non_de if r.get('zipCode') in plz)} of {len(non_de)}.")
    lat_ok = [r for r in de if not (GERMANY['latFrom'] <= r['latitude'] <= GERMANY['latTo'] and GERMANY['lngFrom'] <= r['longitude'] <= GERMANY['lngTo'])]
    w(f"DE records with coordinates outside the GERMANY box: {len(lat_ok)}. Records with code DE = {len(de)}; R3 = {len(sets['R03'])}; "
      f"R3 == DE records of R13: {sets['R03'] == {r['id'] for r in r13 if r['countryResolved'].get('code') == 'DE'}}; "
      f"DE records outside the box (name): {[label(r) for r in lat_ok]}.\n")

    # ---------------- 6
    w("## 6. Classification hints for the Automobil set (R8)\n")
    auto = resp["R08"]
    hu_re = re.compile(r"\bHU\b")
    hu_re_ci = re.compile(r"hauptuntersuchung", re.I)

    def prefix(r):
        n = (r.get("name") or "").strip()
        for p in NAME_PREFIXES:
            if n.lower().startswith(p.lower()):
                return p
        return "other"

    def has_booking(r):
        return not is_empty(r.get("linkOnlineAppointment"))

    def hu_fields(r):
        hit = []
        for f in TEXT_FIELDS + ["name", "name2"]:
            txt = " ".join(strings(r.get(f)))
            if hu_re_ci.search(txt) or hu_re.search(txt):
                hit.append(f)
        return hit

    w(f"R8 has {len(auto)} records. HU mention = text in `freeText1`, `freeText2`, `services`, `currentInformation`, `openingHoursAreas`, `name`, `name2` matching `Hauptuntersuchung` (any case) or the word `HU`.\n")
    pc = Counter(prefix(r) for r in auto)
    w(md_table(["Name prefix", "Records"], [[k, pc.get(k, 0)] for k in NAME_PREFIXES + ["other"]]))
    oth = Counter(" ".join((r["name"] or "").split()[:2]) for r in auto if prefix(r) == "other")
    w("\nMost frequent first two words of the 'other' names: " + ", ".join(f"{k} ({v})" for k, v in oth.most_common(15)) + "\n")
    hu_by_field = Counter(f for r in auto for f in hu_fields(r))
    hu_n = sum(1 for r in auto if hu_fields(r))
    book_n = sum(1 for r in auto if has_booking(r))
    w(f"- Records with a booking link (`linkOnlineAppointment.to`): {book_n}; with an HU mention: {hu_n} (by field: {dict(hu_by_field)}); "
      f"with a `linkWebsite`: {sum(1 for r in auto if not is_empty(r.get('linkWebsite')))}.")
    ct = Counter((has_booking(r), bool(hu_fields(r))) for r in auto)
    w("\nBooking link x HU mention:\n")
    w(md_table(["Booking link", "HU mention", "Records"], [[b, h, ct.get((b, h), 0)] for b in (True, False) for h in (True, False)]))
    svc_values = sorted({o["value"] for r in auto for o in r["specialServicesList"]})
    w("\nCross-tab against `specialServicesList` values (`services` is a RichText that is empty in "
      f"{sum(1 for r in auto if is_empty(r['services']))} of {len(auto)} records; so the service values are the Option list):\n")
    rows = []
    groups = {p: [r for r in auto if prefix(r) == p] for p in NAME_PREFIXES + ["other"]}
    for p, rs in groups.items():
        rows.append([p, len(rs), sum(has_booking(r) for r in rs), sum(bool(hu_fields(r)) for r in rs), sum(bool(r["specialServicesList"]) for r in rs)]
                    + [sum(any(o["value"] == v for o in r["specialServicesList"]) for r in rs) for v in svc_values])
    rows.append(["**all**", len(auto), book_n, hu_n, sum(bool(r["specialServicesList"]) for r in auto)]
                + [sum(any(o["value"] == v for o in r["specialServicesList"]) for r in auto) for v in svc_values])
    w(md_table(["Prefix", "n", "booking", "HU text", "any service"] + svc_values, rows))
    w("\nSame, split by booking link / HU mention / commercial flag (n, per service value):\n")
    rows = []
    for title, pred in [("booking link", has_booking), ("no booking link", lambda r: not has_booking(r)),
                        ("HU mention", lambda r: bool(hu_fields(r))), ("no HU mention", lambda r: not hu_fields(r)),
                        ("Nfz suitable", lambda r: r["suitableForCommercialVehicles"]), ("not Nfz suitable", lambda r: not r["suitableForCommercialVehicles"]),
                        ("no booking AND no HU mention", lambda r: not has_booking(r) and not hu_fields(r))]:
        rs = [r for r in auto if pred(r)]
        rows.append([title, len(rs), sum(bool(r["specialServicesList"]) for r in rs)]
                    + [sum(any(o["value"] == v for o in r["specialServicesList"]) for r in rs) for v in svc_values])
    w(md_table(["Group", "n", "any service"] + svc_values, rows))
    w("\nRecords of R8 with neither a booking link nor an HU mention, by prefix: "
      f"{dict(Counter(prefix(r) for r in auto if not has_booking(r) and not hu_fields(r)))}.\n")
    w("Auto records also carrying another type: "
      f"{dict(Counter(tuple(sorted(set(type_names(r)) - {'Automobil Prüfwesen und Gutachten'})) for r in auto if len(r['locationTypes']) > 1).most_common(8))}\n")

    # ---------------- 7
    w("## 7. One complete record (Moabit), as returned in R13\n")
    if moabit:
        w("```json\n" + json.dumps(next(r for r in resp["R13"] if r["id"] == moabit[0]["id"]), ensure_ascii=False, indent=2) + "\n```")
    else:
        w("Moabit record not found.")

    with open(next_free(run / "diffs.csv"), "x", newline="", encoding="utf-8") as fh:
        cw = csv.DictWriter(fh, fieldnames=["pair", "direction", "id", "name", "zip", "city", "country", "types"])
        cw.writeheader()
        cw.writerows(diffs_rows)
    target = next_free(run / "summary.md")
    with open(target, "x", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print(f"wrote {target}")
    return target


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        run_fetch()
    elif cmd == "analyze" and len(sys.argv) > 2:
        run_analyze(Path(sys.argv[2]))
    else:
        sys.exit(__doc__)
