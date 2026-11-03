"""Continuity of sites across months: a site that is still there keeps its master_id (Nikos, 2026-11-03).

The changelog knows only NEW / CHANGED / RETIRED. A source that hands a site a new id, a site that moves within its town, or
a second listing at an address the same ÜO already has must not become RETIRED + NEW. Two steps, both within ONE ÜO (one row
per ÜO stays the rule, decision 2026-11-03):

1. `join_pairs` (before the groups are built): a NEW listing (location_id in no earlier master row) at the street + number + PLZ
   of an active row of the same source joins that row, and listings that shared one master row last month stay together.
   Ronnenberg (Maaß next to Ziemer / Ziegenhorn), Schleswig. KÜS rows with different licensees stay apart (the `veto` field).
2. `match` (after master_ids.assign): every active row of the previous master that no group claims ("missing") is compared
   with every group that got a new id ("new"). A pair is taken 1:1, best first:
     rekeyed   same street + number and the same PLZ or town (or <= REKEY_KM apart with a matching name / phone /
               domain): Fromm, Erdem, Fuchs, Oldenburg
     moved     <= MOVE_KM apart (or the same town without coordinates) and a similar name, the same phone number or a
               rare e-mail / website domain: Abstatt (vogler-ing.de), Mus, DEKRA Neuss
   The new group takes the old master_id, so the export sees CHANGED fields (address, name, ...).

What is left missing goes to master_lifecycle (retired only after RETIRE_AFTER_MISSED_RUNS fresh runs in a row).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import pandas as pd

from core import normalise as nm

REKEY_KM = 0.05
MOVE_KM = 10.0
NAME_MIN = 0.9  # "Büro Zeta" ~ "Büro Eta" is 0.86: a near-name alone is no evidence
RARE_DOMAIN_MAX = 3  # a domain on more rows than this is a network's (dekra.com, gtue.de, ...): it proves nothing
FREEMAIL = {"gmail.com", "googlemail.com", "gmx.de", "gmx.net", "gmx.com", "web.de", "t-online.de", "outlook.com", "outlook.de",
            "hotmail.com", "hotmail.de", "yahoo.com", "yahoo.de", "icloud.com", "aol.com", "freenet.de", "online.de", "live.de", "mail.de"}
# words that say what a site is, not who: removed before names are compared
GENERIC = {"ingenieurbuero", "ingenieurburo", "ing", "buero", "buro", "ingbuero", "ingburo", "kfz", "pruefstelle", "prufstelle",
           "pruefzentrum", "prufzentrum", "pruefstation", "station", "aussenstelle", "ausenstelle", "service", "center", "servicecenter",
           "partner", "gtue", "gtu", "kues", "kus", "dekra", "tuev", "tuv", "sued", "sud", "nord", "rheinland", "hessen", "fsp",
           "gmbh", "gbr", "ug", "kg", "ohg", "mbh", "co", "und", "fuer", "fur", "das", "der", "die", "f", "d", "sachverstaendigenbuero",
           "sachverstandigenburo", "sachverstaendige", "sachverstandige", "sv", "kraftfahrzeugwesen", "kfzwesen", "wesen", "fahrzeugtechnik"}
VERDICT_RANK = {"rekeyed": 0, "moved": 1}


def _km(a, b) -> float | None:
    try:
        la, lo, lb, lob = float(a["lat"]), float(a["lng"]), float(b["lat"]), float(b["lng"])
    except (TypeError, ValueError):
        return None
    dlat, dlng = math.radians(lb - la), math.radians(lob - lo)
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(la)) * math.cos(math.radians(lb)) * math.sin(dlng / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", nm.fold(name)) if t and t not in GENERIC]


def name_similarity(a: str, b: str) -> float:
    """0..1 after the generic words are gone; 0 when one side has nothing left ('Ingenieurbüro' proves nothing).
    All words of the shorter name inside the longer one count as 1 ('Station Neuss' ~ 'Station Neuss-Uedesheim')."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    short, long_ = sorted((ta, tb), key=len)
    if set(short) <= set(long_):
        return 1.0
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


def phone_key(phone: str) -> str:
    """Digits without the country code or the leading 0; '' when too short to identify a line (`07062`)."""
    d = re.sub(r"\D", "", str(phone))
    d = d[2:] if d.startswith("49") else d
    d = d.lstrip("0")
    return d if len(d) >= 7 else ""


def domain(value: str) -> str:
    v = str(value).strip().lower()
    if not v:
        return ""
    v = v.split("@", 1)[1] if "@" in v else re.sub(r"^[a-z]+://", "", v)
    v = v.split("/", 1)[0].removeprefix("www.")
    return "" if v in FREEMAIL else v


def _domains(row) -> set[str]:
    return {d for d in (domain(row.get("email", "")), domain(row.get("website", ""))) if d}


def _same_street(a, b) -> bool:
    if not (nm.has_street(a["street"]) and nm.has_number(a["street"]) and nm.street_key(a["street"]) == nm.street_key(b["street"])):
        return False
    d = _km(a, b)
    return bool((a["zip"] and a["zip"] == b["zip"]) or (nm.city_keys(a["city"]) & nm.city_keys(b["city"])) or (d is not None and d <= 1.0))


def features(old, new, rare: set[str]) -> dict:
    d = _km(old, new)
    shared = sorted((_domains(old) & _domains(new)) & rare)
    pk = phone_key(old.get("phone", ""))
    return {"same_street": _same_street(old, new), "km": None if d is None else round(d, 3),
            "same_town": bool(nm.city_keys(old["city"]) & nm.city_keys(new["city"])),
            "name_sim": round(name_similarity(old["name"], new["name"]), 2),
            "same_phone": bool(pk and pk == phone_key(new.get("phone", ""))), "shared_domain": "|".join(shared)}


def verdict(f: dict) -> str:
    """rekeyed: the same street + number, or <= REKEY_KM apart with a matching name / phone / domain (coordinates alone
    never: two sites can share one pair of coordinates). moved: near and a matching name / phone / domain."""
    evidence = f["name_sim"] >= NAME_MIN or f["same_phone"] or bool(f["shared_domain"])
    if f["same_street"] or (f["km"] is not None and f["km"] <= REKEY_KM and evidence):
        return "rekeyed"
    near = f["km"] <= MOVE_KM if f["km"] is not None else f["same_town"]
    return "moved" if near and evidence else ""


def _rare_domains(*frames: pd.DataFrame) -> set[str]:
    c: Counter = Counter()
    for f in frames:
        for r in f.to_dict("records"):
            c.update(_domains(r))
    return {d for d, n in c.items() if n <= RARE_DOMAIN_MAX}


def candidates(prev: pd.DataFrame, cur: pd.DataFrame, exclude: set[str] | None = None) -> pd.DataFrame:
    """Every (missing old row, new row) pair of one ÜO within MOVE_KM or the same town, with its features and verdict.
    `prev` = the previous master, `cur` = this run's rows with their master_id (the classifier notebook shows this frame).
    `exclude` = old ids that are no candidates (merged into another row by master_ids: duplicates, not moves)."""
    prev = prev.fillna("").astype(str)
    cur = cur.fillna("").astype(str)
    status = prev["status"] if "status" in prev.columns else pd.Series("active", index=prev.index)
    cur_ids = set(cur["master_id"]) | set(exclude or ())
    missing = prev[(status.replace("", "active") == "active") & ~prev["master_id"].isin(cur_ids)]
    new = cur[~cur["master_id"].isin(set(prev["master_id"]))]
    rare = _rare_domains(prev, cur)
    rows = []
    by_uo = defaultdict(list)
    for n in new.to_dict("records"):
        by_uo[n["uo"]].append(n)
    for o in missing.to_dict("records"):
        for n in by_uo.get(o["uo"], []):
            f = features(o, n, rare)
            if not (f["same_street"] or f["same_town"] or (f["km"] is not None and f["km"] <= MOVE_KM)):
                continue
            rows.append({"old": o["master_id"], "new": n["master_id"], "uo": o["uo"], "old_name": o["name"], "new_name": n["name"],
                         "old_address": f"{o['street']}, {o['zip']} {o['city']}", "new_address": f"{n['street']}, {n['zip']} {n['city']}",
                         **f, "verdict": verdict(f)})
    cols = ["old", "new", "uo", "old_name", "new_name", "old_address", "new_address", "same_street", "km", "same_town", "name_sim",
            "same_phone", "shared_domain", "verdict"]
    return pd.DataFrame(rows, columns=cols)


def match(prev: pd.DataFrame | None, cur: pd.DataFrame, exclude: set[str] | None = None) -> list[dict]:
    """1:1 pairs {old, new, verdict, note}, best first (rekeyed before moved, then the nearer, then the more similar name)."""
    if prev is None or cur.empty:
        return []
    c = candidates(prev, cur, exclude)
    c = c[c["verdict"] != ""]
    if c.empty:
        return []
    c = c.assign(rank=c["verdict"].map(VERDICT_RANK), dist=c["km"].fillna(MOVE_KM)).sort_values(
        ["rank", "dist", "name_sim", "old", "new"], ascending=[True, True, False, True, True], kind="stable")
    used_old, used_new, out = set(), set(), []
    for r in c.to_dict("records"):
        if r["old"] in used_old or r["new"] in used_new:
            continue
        used_old.add(r["old"])
        used_new.add(r["new"])
        why = [x for x, ok in (("same street", r["same_street"]), ("same phone", r["same_phone"]),
                               (f"domain {r['shared_domain']}", r["shared_domain"]), (f"name {r['name_sim']}", r["name_sim"] >= NAME_MIN)) if ok]
        km = "" if pd.isna(r["km"]) else f"{r['km'] * 1000:.0f} m apart" if r["km"] < 1 else f"{r['km']:.1f} km apart"
        out.append({"old": r["old"], "new": r["new"], "verdict": r["verdict"], "note": ", ".join([*why, km] if km else why)})
    return out


def join_pairs(recs: list[dict], prev: pd.DataFrame | None) -> list[tuple[str, str, str]]:
    """(location_id_a, location_id_b, note): listings of one source at one street + number + PLZ that belong to one row.
    A new listing joins the existing active row (the lowest master_id when several); listings of one earlier row stay joined."""
    if prev is None:
        return []
    p = prev.fillna("").astype(str)
    status = p["status"].replace("", "active") if "status" in p.columns else pd.Series("active", index=p.index)
    owner = {}
    for mid, ids, st in zip(p["master_id"], p["source_ids"], status):
        for i in ids.split("|"):
            if i:
                owner[i] = (mid, st)
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in recs:
        if r.get("akey") and nm.has_number(r["street"]):
            by_key[(r["slug"], r["akey"])].append(r)
    out = []
    for (slug, _key), rs in sorted(by_key.items()):
        if len(rs) < 2:
            continue

        def vetoed(a, b) -> bool:  # a different licensee (KÜS) is a different business at the same address
            va, vb = str(a.get("veto", "") or "").strip(), str(b.get("veto", "") or "").strip()
            return bool(va and vb and va != vb)

        by_mid: dict[str, list[dict]] = defaultdict(list)
        for r in rs:
            if r["location_id"] in owner:
                by_mid[owner[r["location_id"]][0]].append(r)
        for mid, members in sorted(by_mid.items()):
            for r in members[1:]:
                if not vetoed(members[0], r):
                    out.append((members[0]["location_id"], r["location_id"], f"one row last month ({mid})"))
        active = sorted(((owner[r["location_id"]][0], r) for r in rs
                         if r["location_id"] in owner and owner[r["location_id"]][1] == "active"), key=lambda x: x[0])
        if not active:
            continue
        mid, anchor = active[0][0], active[0][1]
        for r in rs:
            if r["location_id"] not in owner and not vetoed(anchor, r):
                out.append((anchor["location_id"], r["location_id"], f"new listing at the address of {mid} (same source)"))
    return out
