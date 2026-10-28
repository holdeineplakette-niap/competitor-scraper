"""Merge duplicates into physical sites. Strict order, every merge is an edge with a rule name.

Merge families: {tuev_sued, tuev_hessen}, {tuev_rheinland, fsp}, every other source on its own. Nothing merges across
families automatically (MERGE_ACROSS_UO = False); a shared address across families only gets an info flag.

Order inside a family:
  1. booking location_sign, only where the sign is used by exactly one row on each side
  2. normalised street + PLZ (a row without PLZ: street + city, only if the match is unique)
  3. manual decisions (overrides): `same` adds an edge, `different` removes an automatic edge
  4. similar street in the same PLZ (typos, house-number letter): only if mutual and unique; coordinates can veto
Coordinates alone never merge. They only propose candidates (see `proximity_candidates`).
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import numpy as np

from core import master_schema as ms
from core import normalise as nm
from core.geo import haversine_km

SIMILAR_MIN = 0.9  # street similarity needed to merge two leftover rows of one family in the same PLZ
SIMILAR_MAX_KM = 0.5  # if both rows have coordinates, they must lie this close for a similarity merge
CANDIDATE_KM = 0.15  # coordinate proposal radius
OVERRIDE_COLUMNS = ["location_id_a", "location_id_b", "decision", "decided_by", "date", "note"]


class MergeConflict(Exception):
    """Two rows an override says are different ended up in one merged group."""


@dataclass
class Edge:
    a: int
    b: int
    rule: str
    note: str = ""


@dataclass
class Dedupe:
    recs: list[dict]
    edges: list[Edge]
    comps: list[list[int]]
    comp_of: list[int]
    flags: dict[int, list[str]]
    info: dict = field(default_factory=dict)


def load_overrides(path: Path) -> list[dict]:
    """Read the persisted manual decisions; a missing file means none yet."""
    if not Path(path).exists():
        return []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for i, r in enumerate(csv.DictReader(fh, delimiter=";"), start=2):
            if not any((v or "").strip() for v in r.values()):
                continue
            if None in r:
                raise ValueError(f"{path}: line {i}: more fields than columns (a ';' inside a note?)")
            missing = [c for c in OVERRIDE_COLUMNS if c not in r]
            if missing:
                raise ValueError(f"{path}: line {i}: missing column(s) {missing}")
            r = {k: (v or "").strip() for k, v in r.items()}
            if r["decision"] not in ("same", "different"):
                raise ValueError(f"{path}: line {i}: decision must be 'same' or 'different', got {r['decision']!r}")
            if not r["location_id_a"] or not r["location_id_b"] or r["location_id_a"] == r["location_id_b"]:
                raise ValueError(f"{path}: line {i}: two different location ids are required")
            rows.append(r)
    return rows


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def strong_evidence(a: dict, b: dict) -> str | None:
    """Why two rows of one source at one address are the same business, or None (then they stay separate).

    A differing licensee (veto column, e.g. KÜS) always keeps them apart. Otherwise one match of booking URL,
    phone, e-mail, operator or name is enough.
    """
    if a["veto"] and b["veto"] and a["veto"] != b["veto"]:
        return None
    if a["booking_url"] and a["booking_url"] == b["booking_url"]:
        return "same booking_url"
    if _digits(a["phone"]) and _digits(a["phone"]) == _digits(b["phone"]):
        return "same phone"
    if a["email"] and a["email"].casefold() == b["email"].casefold():
        return "same email"
    if a["operator_name"] and nm.fold(a["operator_name"]) == nm.fold(b["operator_name"]):
        return "same operator"
    if (a["slug"] not in ms.NAME_EVIDENCE_EXCLUDED and a["name"] and nm.fold(a["name"]) == nm.fold(b["name"])
            and not (a["operator_name"] and b["operator_name"] and nm.fold(a["operator_name"]) != nm.fold(b["operator_name"]))):
        return "same name"
    return None


def _coord(rec: dict):
    return (rec["lat"], rec["lng"]) if rec["lat"] is not None and rec["lng"] is not None else None


def _dist_km(r1: dict, r2: dict) -> float | None:
    c1, c2 = _coord(r1), _coord(r2)
    return float(haversine_km(c1[0], c1[1], c2[0], c2[1])) if c1 and c2 else None


OVERRIDE_MAX_KM = 25.0  # a manual 'same' between rows farther apart than this is refused as a typo
SIGN_NEAR_KM = 5.0  # a shared sign with different addresses merges only if PLZ or city agree, or the coordinates are this close
PLZ_NEAR_KM = 15.0


def _near_plz(rec: dict, zip_: str, cent: dict | None) -> bool:
    """False only if the row has coordinates and they lie more than 15 km from the centroid of the candidate's PLZ."""
    if not cent or rec["lat"] is None or rec["lng"] is None or zip_ not in cent:
        return True
    return float(haversine_km(rec["lat"], rec["lng"], *cent[zip_])) <= PLZ_NEAR_KM


def build(recs: list[dict], overrides: list[dict], merge_across_uo: bool = ms.MERGE_ACROSS_UO,
          extra_same: list[tuple[str, str, str]] | None = None, cent: dict | None = None) -> Dedupe:
    """`extra_same` = [(location_id_a, location_id_b, note)]: pairs the coordinate stage found to be the same site.
    `cent` = {PLZ: (lat, lng)}: lets the street-only rule check a row's own coordinates against the candidate's PLZ."""
    n = len(recs)
    idx = {r["location_id"]: i for i, r in enumerate(recs)}
    if len(idx) != n:
        raise ValueError("location_id is not unique across the inputs")
    scope = (lambda r: "all") if merge_across_uo else (lambda r: r["family"])
    edges: list[Edge] = []
    by_pair: dict[tuple[int, int], Edge] = {}
    flags: dict[int, list[str]] = defaultdict(list)
    info: dict = {"suppressed": [], "sign_not_1to1": [], "weak_pairs": [], "overrides_unused": [], "overrides_applied": 0,
                  "similar_merges": [], "no_plz_ambiguous": [], "forced_review": {}, "sign_rejected": []}

    def force_review(i: int, reason: str) -> None:
        """A match the rules cannot decide safely: both rows stay separate and become review items."""
        info["forced_review"].setdefault(recs[i]["location_id"], reason)
        f = f"match_ambiguous:{reason}"
        if f not in flags[i]:
            flags[i].append(f)

    different = set()
    same_over = []
    for o in overrides:
        a, b = o["location_id_a"], o["location_id_b"]
        if a not in idx or b not in idx:
            info["overrides_unused"].append(f"{a} / {b} ({o['decision']}): id not in the current inputs")
            continue
        if o["decision"] == "different":
            different.add(frozenset((a, b)))
        else:
            same_over.append((idx[a], idx[b], o))

    contradictions = [f"{sorted(p)[0]} and {sorted(p)[1]}" for p in sorted(different, key=sorted)
                      if any(frozenset((recs[a]["location_id"], recs[b]["location_id"])) == p for a, b, _ in same_over)]
    if contradictions:
        raise MergeConflict("the overrides file contradicts itself ('same' and 'different') for: " + "; ".join(contradictions))

    live = _UF(n)  # the groups as they grow while edges are added
    live_members: dict[int, set[int]] = {i: {i} for i in range(n)}

    def can_join(a: int, b: int) -> bool:
        """Two rows of one source may share a group only if the source's own evidence ties them (strong_evidence)."""
        ra, rb = live.find(a), live.find(b)
        if ra == rb:
            return True
        return all(recs[x]["slug"] != recs[y]["slug"] or strong_evidence(recs[x], recs[y])
                   for x in live_members[ra] for y in live_members[rb])

    def add(a: int, b: int, rule: str, note: str = "") -> bool:
        if frozenset((recs[a]["location_id"], recs[b]["location_id"])) in different:
            info["suppressed"].append(f"{recs[a]['location_id']} / {recs[b]['location_id']} ({rule})")
            return False
        pair = (min(a, b), max(a, b))
        if rule not in ("override", "within_source_address") and pair not in by_pair and not can_join(a, b):
            force_review(a, "would_join_unproven_rows_of_one_source")
            force_review(b, "would_join_unproven_rows_of_one_source")
            return False
        if pair in by_pair:  # a later rule agrees with an earlier one: keep one edge (the stronger rule), note the corroboration
            first = by_pair[pair]
            first.note = (first.note + "; " if first.note else "") + f"also {rule}"
            return True
        by_pair[pair] = Edge(pair[0], pair[1], rule, note)
        edges.append(by_pair[pair])
        ra, rb = live.find(a), live.find(b)
        if ra != rb:
            live.union(a, b)
            root = live.find(a)
            live_members[root] = live_members[ra] | live_members[rb]
        return True

    # 1. booking location_sign, 1:1 on both sides
    by_sign: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(recs):
        if r["sign"]:
            by_sign[r["sign"]].append(i)
    for sign, members in sorted(by_sign.items()):
        slugs = {recs[i]["slug"] for i in members}
        per_slug = {s: [i for i in members if recs[i]["slug"] == s] for s in slugs}
        if len(slugs) < 2:
            continue
        if any(len(v) != 1 for v in per_slug.values()):
            info["sign_not_1to1"].append({"sign": sign, "rows": [recs[i]["location_id"] for i in members]})
            continue
        a, b = members[0], members[1]
        note = ""
        if not (recs[a]["akey"] and recs[a]["akey"] == recs[b]["akey"]):
            ra, rb = recs[a], recs[b]
            d = _dist_km(ra, rb)
            agree = bool((ra["zip"] and ra["zip"] == rb["zip"]) or (nm.city_keys(ra["city"]) & nm.city_keys(rb["city"]))
                         or (d is not None and d <= SIGN_NEAR_KM))
            if not agree:
                # the same booking sign at two different places: a copy-paste error on one side, so do not merge
                info["sign_rejected"].append((ra["location_id"], rb["location_id"], sign))
                force_review(a, "sign_shared_by_different_places")
                force_review(b, "sign_shared_by_different_places")
                continue
            note = "address_differs"
            flags[a].append("sign_address_mismatch")
            flags[b].append("sign_address_mismatch")
        add(a, b, "location_sign", note)

    # 2a. normalised street + PLZ
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(recs):
        if r["akey"]:
            groups[(scope(r), r["akey"])].append(i)
    for key in sorted(groups):
        members = groups[key]
        # within one source, rows are the same business only on strong evidence; a group where one source has
        # several unproven rows AND another source is present is ambiguous (the other source's row could belong to either)
        cluster = _UF(n)
        proven: dict[tuple[int, int], str] = {}
        for a, b in combinations(members, 2):
            if recs[a]["slug"] == recs[b]["slug"]:
                why = strong_evidence(recs[a], recs[b])
                if why:
                    proven[(a, b)] = why
                    cluster.union(a, b)
        slugs_here = {recs[i]["slug"] for i in members}
        ambiguous = len(slugs_here) > 1 and any(
            len({cluster.find(i) for i in members if recs[i]["slug"] == s}) > 1 for s in slugs_here)
        numberless = not nm.has_number(recs[members[0]]["street"])
        for a, b in combinations(members, 2):
            ra, rb = recs[a], recs[b]
            if ra["slug"] == rb["slug"]:
                if (a, b) in proven:
                    add(a, b, "within_source_address", proven[(a, b)])
                else:
                    info["weak_pairs"].append((a, b))
                continue
            if ambiguous:
                force_review(a, "same_address_several_rows_in_one_source")
                force_review(b, "same_address_several_rows_in_one_source")
                continue
            if numberless:  # "Hauptstr." without a number says little: coordinates must agree
                d = _dist_km(ra, rb)
                if d is None or d > 0.15:
                    force_review(a, "address_without_house_number")
                    force_review(b, "address_without_house_number")
                    continue
            add(a, b, "address")

    # 2b. no PLZ: street + city (street alone if the row has no city either), accepted only if the match is unique.
    # A candidate must still be unmatched by the row's own source, so an FSP site already merged with a Rheinland row
    # that has a PLZ is never taken twice.
    uf0 = _UF(n)
    for e in edges:
        uf0.union(e.a, e.b)
    for i, r in enumerate(recs):
        if r["akey"] or r["zip"] or not nm.has_street(r["street"]):
            continue
        sk, ck = nm.street_key(r["street"]), nm.city_keys(r["city"])
        if not ck and r["lat"] is None:  # nothing but a street: too little to match on
            force_review(i, "street_only_without_coordinates")
            continue
        cands = [j for j, o in enumerate(recs)
                 if j != i and o["zip"] and o["slug"] != r["slug"] and scope(o) == scope(r)
                 and nm.has_street(o["street"]) and nm.street_key(o["street"]) == sk
                 and (not ck or (nm.city_keys(o["city"]) & ck))
                 and _near_plz(r, o["zip"], cent)
                 and not any(recs[k]["slug"] == r["slug"] and k != i for k in range(n) if uf0.find(k) == uf0.find(j))]
        if len(cands) == 1:
            add(i, cands[0], "address_city", "street + city" if ck else "street only (row has neither PLZ nor city), unique")
        elif len(cands) > 1:
            info["no_plz_ambiguous"].append(r["location_id"])

    # 3. overrides: `same`
    for a, b, o in same_over:
        ra, rb = recs[a], recs[b]
        d = _dist_km(ra, rb)
        far_plz = bool(ra["zip"] and rb["zip"] and ra["zip"][:2] != rb["zip"][:2])
        if far_plz or (d is not None and d > OVERRIDE_MAX_KM):
            raise MergeConflict(f"override 'same' {ra['location_id']} = {rb['location_id']} looks like a typo: PLZ {ra['zip']} vs {rb['zip']}"
                                + (f", {d:.0f} km apart" if d is not None else "") + ". Fix the id in master_match_overrides.csv.")
        if add(a, b, "override", o.get("note", "")):
            info["overrides_applied"] += 1

    for a_id, b_id, note in extra_same or []:
        add(idx[a_id], idx[b_id], "address_proximity", note)

    def components() -> tuple[list[list[int]], list[int]]:
        uf = _UF(n)
        for e in edges:
            uf.union(e.a, e.b)
        roots: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            roots[uf.find(i)].append(i)
        comps = [sorted(v) for _, v in sorted(roots.items())]
        comp_of = [0] * n
        for c, members in enumerate(comps):
            for i in members:
                comp_of[i] = c
        return comps, comp_of

    comps, comp_of = components()

    # 4. similar street, same PLZ, leftovers of two sources in one family; mutual and unique only
    if not merge_across_uo:
        by_fam: dict[str, list[int]] = defaultdict(list)
        for c, members in enumerate(comps):
            slugs = {recs[i]["slug"] for i in members}
            fam = recs[members[0]]["family"]
            if len(slugs) == 1 and any(ms.family_of(s) == fam for s in slugs) and sum(1 for s in ms.FAMILIES if ms.FAMILIES[s] == fam) > 1:
                by_fam[fam].append(c)
        for fam, cl in sorted(by_fam.items()):
            by_zip: dict[str, list[int]] = defaultdict(list)
            for c in cl:
                r = recs[comps[c][0]]
                if r["zip"] and nm.has_street(r["street"]):
                    by_zip[r["zip"]].append(c)
            for z, zl in sorted(by_zip.items()):
                cand: dict[int, list[tuple[int, float]]] = defaultdict(list)
                for c1, c2 in combinations(zl, 2):
                    r1, r2 = recs[comps[c1][0]], recs[comps[c2][0]]
                    if r1["slug"] == r2["slug"]:
                        continue
                    if frozenset((r1["location_id"], r2["location_id"])) in different:
                        continue
                    sim = nm.street_similarity(r1["street"], r2["street"])
                    d = _dist_km(r1, r2)
                    if sim >= SIMILAR_MIN and (d is None or d <= SIMILAR_MAX_KM):
                        cand[c1].append((c2, sim))
                        cand[c2].append((c1, sim))
                for c1, lst in sorted(cand.items()):
                    if len(lst) == 1 and len(cand[lst[0][0]]) == 1 and c1 < lst[0][0]:
                        c2, sim = lst[0]
                        a, b = comps[c1][0], comps[c2][0]
                        if add(a, b, "address_similar", f"street similarity {sim:.2f}"):
                            info["similar_merges"].append((recs[a]["location_id"], recs[b]["location_id"], round(sim, 2)))
        comps, comp_of = components()

    # shared addresses: across families (info flag) and within a source without proof (info flag)
    shared_other: dict[tuple[str, str], int] = defaultdict(int)
    if not merge_across_uo:
        by_addr: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(recs):
            if r["akey"]:
                by_addr[r["akey"]].append(i)
        for k in sorted(by_addr):
            fams = sorted({recs[i]["family"] for i in by_addr[k]})
            if len(fams) > 1:
                for i in by_addr[k]:
                    for j in by_addr[k]:
                        if recs[j]["family"] != recs[i]["family"]:
                            f = f"shared_address_other_uo:{recs[j]['slug']}"
                            if f not in flags[i]:
                                flags[i].append(f)
                for fa, fb in combinations(fams, 2):
                    shared_other[(fa, fb)] += 1
    info["shared_other_uo"] = {f"{a}|{b}": v for (a, b), v in sorted(shared_other.items())}
    weak = [(a, b) for a, b in info["weak_pairs"] if comp_of[a] != comp_of[b]]
    info["weak_pairs"] = [(recs[a]["location_id"], recs[b]["location_id"]) for a, b in weak]
    for a, b in weak:
        flags[a].append("shared_address_same_source")
        flags[b].append("shared_address_same_source")

    # transitivity: a `different` decision must not end up inside one group
    clashes = []
    for pair in sorted(tuple(sorted(p)) for p in different):
        a, b = idx[pair[0]], idx[pair[1]]
        if comp_of[a] == comp_of[b]:
            chain = [f"{recs[e.a]['location_id']} = {recs[e.b]['location_id']} ({e.rule})" for e in edges
                     if comp_of[e.a] == comp_of[a]]
            clashes.append(f"{pair[0]} and {pair[1]} are decided 'different' but are merged through: {'; '.join(chain)}")
    if clashes:
        raise MergeConflict("; ".join(clashes))

    # same-source rows that share a group without a direct within-source proof
    direct = {(e.a, e.b) for e in edges if recs[e.a]["slug"] == recs[e.b]["slug"]}
    for members in comps:
        by_slug: dict[str, list[int]] = defaultdict(list)
        for i in members:
            by_slug[recs[i]["slug"]].append(i)
        for lst in by_slug.values():
            for a, b in combinations(lst, 2):
                if (a, b) not in direct:
                    for i in (a, b):
                        if "merged_within_source_unproven" not in flags[i]:
                            flags[i].append("merged_within_source_unproven")

    rules: dict[str, int] = defaultdict(int)
    comp_rules: dict[tuple[str, str], int] = defaultdict(int)
    for e in edges:
        rules[e.rule] += 1
    for members in comps:
        if len(members) > 1:
            comp_rules[(recs[members[0]]["family"], "merged_rows_away")] += len(members) - 1
    info["edges_by_rule"] = dict(sorted(rules.items()))
    info["merged_away_by_family"] = {k[0]: v for k, v in sorted(comp_rules.items())}
    return Dedupe(recs, edges, comps, comp_of, dict(flags), info)


PAIR_FAMILIES = ("sued_hessen", "rheinland_fsp")  # families whose two sources list the same sites


def proximity_candidates(groups: list[dict], known_plz: set[str] | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Pairs of master groups of one family closer than 150 m with different addresses.

    `groups` hold: gid, family, slugs, lat, lng, street, zip, city, akeys (set), uo.
    Returns (unresolved, rejected, auto_same):
      auto_same   two sources of a pair family give the same street and house number in the same city but different
                  PLZ, and the coordinates agree: the same site (rule address_proximity), merged by the caller
      rejected    a different street, or the same street with a clearly different number: a different address
      unresolved  could be the same house (similar street, related or missing number): a review item
    """
    unresolved, rejected, auto_same = [], [], []
    by_fam: dict[str, list[dict]] = defaultdict(list)
    for g in groups:
        if g["lat"] is not None and g["lng"] is not None:
            by_fam[g["family"]].append(g)
    for fam, gl in sorted(by_fam.items()):
        lat = np.array([g["lat"] for g in gl], dtype=float)
        lng = np.array([g["lng"] for g in gl], dtype=float)
        for i, g in enumerate(gl):
            d = haversine_km(g["lat"], g["lng"], lat[i + 1:], lng[i + 1:])
            for k in np.nonzero(d < CANDIDATE_KM)[0]:
                h = gl[i + 1 + int(k)]
                if g["akeys"] & h["akeys"]:
                    continue
                if len(g["slugs"] | h["slugs"]) < 2 and not (g["slugs"] | h["slugs"]) & set(ms.CANDIDATE_WITHIN_SOURCE):
                    continue
                pair = {"a": g["gid"], "b": h["gid"], "km": round(float(d[k]), 3)}
                # Open only if it could be the same house: similar street name and a related (or missing) house number.
                # A different street, or the same street with a clearly different number, is a different address.
                if (fam in PAIR_FAMILIES and g["slugs"] != h["slugs"] and nm.has_street(g["street"])
                        and nm.street_key(g["street"]) == nm.street_key(h["street"]) and nm.city_keys(g["city"]) & nm.city_keys(h["city"])):
                    auto_same.append(pair)
                elif nm.street_related(g["street"], h["street"]):
                    unresolved.append(pair)
                else:
                    rejected.append(pair)
    return unresolved, rejected, auto_same
