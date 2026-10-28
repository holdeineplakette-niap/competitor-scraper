"""One master row per merged group: fixed field precedence, every decision logged.

Precedence (ties break by the fixed source order SÜD, Hessen, Rheinland, FSP, then slug):
  coordinates  first source whose coordinates pass the Germany box and the 15 km PLZ check, then Nominatim
  contact      the source with the most filled of phone/e-mail/website/booking_url; gaps filled from the others
  name         a listed name beats a constructed one (TÜV SÜD builds names from the town)
  address      complete street + valid PLZ wins; a missing PLZ is filled from a matched row
  services     union
  operator     the source that names the operator comes first (Hessen before SÜD)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core import master_schema as ms
from core import normalise as nm
from core.geo import PLZ_FAR_KM, haversine_km, in_germany, plz_distance_km

COORD_DISAGREE_KM = 1.0  # merged sources whose (individually valid) coordinates lie farther apart get an info flag


@dataclass
class Group:
    gid: int
    recs: list[dict]
    row: dict
    logs: list[dict] = field(default_factory=list)
    need_geo: bool = False
    reasons: list[str] = field(default_factory=list)  # closed-list review reasons
    coord_ok_from: str = ""
    agg_flags: list[str] = field(default_factory=list)
    rejected_coords: list[str] = field(default_factory=list)
    family: str = ""
    slugs: set[str] = field(default_factory=set)
    akeys: set[str] = field(default_factory=set)


def _log(g: Group, field_: str, chosen_slug: str, chosen_value, dropped: list[str], note: str = "", rule: str = "precedence") -> None:
    g.logs.append({"gid": g.gid, "location_ids": "|".join(r["location_id"] for r in g.recs), "action": "field",
                   "rule": rule, "field": field_, "chosen_source": chosen_slug,
                   "chosen_value": "" if chosen_value is None else str(chosen_value), "dropped": "; ".join(dropped), "note": note})


def _differs(a: str, b: str) -> bool:
    return nm.fold(a).strip() != nm.fold(b).strip()


def _dropped(ordered: list[dict], field_: str, chosen: str) -> list[str]:
    return [f"{r['slug']}={r[field_]}" for r in ordered if r[field_] and _differs(r[field_], chosen)]


def check_coords(lat, lng, zip_: str, cent: dict, known: set[str]) -> tuple[bool, float | None, str]:
    """(passes, plz_distance_km, why_not). Needs a valid PLZ to check against."""
    if lat is None or lng is None:
        return False, None, "missing"
    if not in_germany(lat, lng):
        return False, None, "outside_germany"
    if not nm.is_valid_plz(zip_, known) or zip_ not in cent:
        return False, None, "plz_unchecked"
    d = plz_distance_km(lat, lng, zip_, cent)
    if d > PLZ_FAR_KM:
        return False, d, f"far_{d:.1f}km"
    return True, d, ""


def resolve_group(gid: int, recs: list[dict], agg_flags: list[str], cent: dict, states: dict, known: set[str]) -> Group:
    ordered = sorted(recs, key=lambda r: (r["rank"], r["location_id"]))
    oordered = sorted(recs, key=lambda r: (ms.operator_rank(r["slug"]), r["location_id"]))
    g = Group(gid, ordered, {}, family=ordered[0]["family"], slugs={r["slug"] for r in ordered},
              akeys={r["akey"] for r in ordered if r["akey"]})
    row: dict = {c: "" for c in ms.MASTER_COLUMNS}
    flags: list[str] = []

    # --- address: complete street + valid PLZ wins; a missing PLZ is filled from a matched row
    def complete(r):
        return nm.has_street(r["street"]) and nm.is_valid_plz(r["zip"], known)

    winner = next((r for r in ordered if complete(r)), None) \
        or next((r for r in ordered if nm.has_street(r["street"]) and r["zip"]), None) \
        or next((r for r in ordered if nm.has_street(r["street"])), None) or ordered[0]
    street = winner["street"]
    zip_ = winner["zip"] if nm.is_valid_plz(winner["zip"], known) else \
        next((r["zip"] for r in ordered if nm.is_valid_plz(r["zip"], known)), winner["zip"] or next((r["zip"] for r in ordered if r["zip"]), ""))
    valid_zips = sorted({r["zip"] for r in ordered if nm.is_valid_plz(r["zip"], known) and r["zip"] in cent})
    if len(valid_zips) > 1:  # sources disagree on the PLZ: the one whose centroid is nearest a coordinate inside Germany wins
        pin = next(((r["lat"], r["lng"]) for r in ordered if r["lat"] is not None and in_germany(r["lat"], r["lng"])), None)
        if pin is not None:
            nearest = min(valid_zips, key=lambda z: (plz_distance_km(pin[0], pin[1], z, cent), z))
            if nearest != zip_:
                _log(g, "zip", "coordinates", nearest, [f"{zip_}"], "sources disagree on the PLZ: nearest centroid to the coordinate chosen")
                zip_ = nearest
    city = winner["city"] or next((r["city"] for r in ordered if r["city"]), "")
    row.update(street=street, zip=zip_, city=city)
    for f_, val in (("street", street), ("zip", zip_), ("city", city)):
        src = next((r["slug"] for r in ordered if r[f_] == val), winner["slug"])
        dropped = _dropped(ordered, f_, val)
        filled = src != winner["slug"]
        if dropped or filled:
            _log(g, f_, src, val, dropped, "filled from another source" if filled else "")
    if not nm.has_street(street) or not zip_:
        g.reasons.append("address_incomplete")
    if zip_ and not nm.is_valid_plz(zip_, known):
        g.reasons.append("plz_invalid")
    zips = sorted({r["zip"] for r in ordered if r["zip"]})
    if len(zips) > 1:  # the sources disagree on the PLZ: the first source's is kept, the conflict stays visible
        flags.append("zip_conflict:" + "/".join(zips))
    row["state"] = states.get(zip_, "") if nm.is_valid_plz(zip_, known) else ""
    row["country"] = "DE"

    # --- name
    real = [r for r in ordered if r["name"] and r["slug"] not in ms.CONSTRUCTED_NAME_SOURCES]
    named = real[0] if real else next((r for r in ordered if r["name"]), ordered[0])
    row["name"] = named["name"]
    dropped = _dropped(ordered, "name", named["name"])
    if dropped:
        _log(g, "name", named["slug"], named["name"], dropped, "listed name preferred over a constructed one")

    # --- contact: the source with the most filled fields wins, gaps are filled from the others
    top = max(ordered, key=lambda r: sum(1 for f_ in ms.CONTACT_FIELDS if r[f_]))  # first max = fixed order on ties
    contact_win = top
    for f_ in ms.CONTACT_FIELDS:
        val = top[f_] or next((r[f_] for r in ordered if r[f_]), "")
        row[f_] = val
        src = next((r["slug"] for r in [top] + ordered if r[f_] == val), "") if val else ""
        dropped = [d for d in _dropped(ordered, f_, val)] if val else []
        if dropped or (val and not top[f_]):
            _log(g, f_, src, val, dropped, "filled from another source" if not top[f_] else "")
    for f_ in ("opening_hours", "profile_url"):
        val = next((r[f_] for r in [contact_win] + ordered if r[f_]), "")
        row[f_] = val
        dropped = _dropped(ordered, f_, val) if val else []
        if dropped:
            _log(g, f_, next(r["slug"] for r in [contact_win] + ordered if r[f_] == val), val, dropped)
    seen: dict[str, None] = {}
    for r in ordered:
        for t in ms.split_services(r["services"]):
            seen.setdefault(t, None)
    row["services"] = "; ".join(seen)

    # --- operator, uo, site type: the source that names the operator comes first
    st_rec = next((r for r in oordered if r["site_type"] != "unknown"), oordered[0])
    row["site_type"] = st_rec["site_type"]
    if len({r["site_type"] for r in ordered}) > 1:
        _log(g, "site_type", st_rec["slug"], st_rec["site_type"], [f"{r['slug']}={r['site_type']}" for r in oordered if r["site_type"] != st_rec["site_type"]],
             "operator-naming source first, a known type beats unknown")
    row["site_type_evidence"] = " | ".join(r["site_type_evidence"] for r in ordered)
    row["uo"] = oordered[0]["uo"]
    uos = sorted({r["uo"] for r in ordered})
    if len(uos) > 1:
        _log(g, "uo", oordered[0]["slug"], row["uo"], [f"{r['slug']}={r['uo']}" for r in oordered if r["uo"] != row["uo"]])
        flags.append("uo_conflict:" + "/".join(uos))
    row["operator_group"] = next((r["operator_group"] for r in oordered if r["operator_group"]), "")
    row["operator_name"] = next((r["operator_name"] for r in oordered if r["operator_name"]), "")
    # psp: Yes only if every merged record says Yes; a record that inspects on site (No) makes the merged site one
    row["psp"] = "Yes" if all(r["psp"] == "Yes" for r in ordered) else "No"
    if len({r["psp"] for r in ordered}) > 1:
        _log(g, "psp", "", row["psp"], [f"{r['slug']}={r['psp']}" for r in ordered], "merged records disagree: No wins")
    row["listed_by"] = "|".join(sorted(g.slugs))
    row["source_ids"] = "|".join(r["location_id"] for s in sorted(g.slugs) for r in ordered if r["slug"] == s)

    # --- scope and meta
    row["in_scope"] = str(any(r["in_scope"] for r in ordered))
    row["scope_reason"] = next((r["scope_reason"] for r in ordered if r["scope_reason"] and (r["in_scope"] or not any(x["in_scope"] for x in ordered))), "")
    row["scraped_at"] = max((r["scraped_at"] for r in ordered), default="")

    # --- coordinates: first source that passes the PLZ check
    for r in ordered:
        ok, dist, why = check_coords(r["lat"], r["lng"], zip_, cent, known)
        if why == "missing":
            continue
        if ok:
            row["lat"], row["lng"] = f"{r['lat']:.6f}", f"{r['lng']:.6f}"
            row["coord_source"] = "nominatim" if r["coord_hint"] == "nominatim" else r["slug"]
            row["plz_distance_km"] = f"{dist:.2f}"
            g.coord_ok_from = r["slug"]
            for x in ordered:  # another source's coordinates that pass the check on their own but lie far away: say so
                if x is r or x["lat"] is None or not check_coords(x["lat"], x["lng"], zip_, cent, known)[0]:
                    continue
                km = float(haversine_km(r["lat"], r["lng"], x["lat"], x["lng"]))
                if km > COORD_DISAGREE_KM:
                    flags.append(f"coords_differ_between_sources:{x['slug']}:{km:.1f}km")
            if any(x is not r and x["lat"] is not None and (x["lat"], x["lng"]) != (r["lat"], r["lng"]) for x in ordered):
                others = [f"{x['slug']}=({x['lat']:.5f},{x['lng']:.5f})" for x in ordered if x is not r and x["lat"] is not None
                          and (x["lat"], x["lng"]) != (r["lat"], r["lng"])]
                _log(g, "lat/lng", r["slug"], row["lat"] + "," + row["lng"], others, "first source passing the PLZ check")
            break
        g.rejected_coords.append(f"{r['slug']}:{why}")
        _log(g, "lat/lng", "", "", [f"{r['slug']}=({r['lat']:.5f},{r['lng']:.5f}) {why}"], "rejected by the PLZ check", rule="coords_rejected")
    if not row["lat"]:
        g.need_geo = nm.has_street(street) and nm.is_valid_plz(zip_, known)

    g.agg_flags = list(agg_flags) + flags
    g.row = row
    return g


def build_flags(g: Group, known: set[str]) -> str:
    """Source flags (prefixed with their source) minus the issues the merge resolved, then aggregator flags."""
    row = g.row
    plz_ok = nm.is_valid_plz(row["zip"], known)
    out: list[str] = []
    for r in g.recs:
        for t in r["flags"]:
            if t.split(":")[0] in ("uo_assumed", "country_not_de"):
                out.append(t)
            elif t in ms.RESOLVED_BY_PLZ and plz_ok:
                continue
            elif t in ms.RESOLVED_BY_COORDS and row["lat"] and g.coord_ok_from != r["slug"]:
                continue
            else:
                out.append(f"{r['slug']}:{t}")
    if not row["lat"]:  # coordinates that failed the check are not lost: the row says so
        out += [f"coords_rejected:{c}" for c in g.rejected_coords]
    return "; ".join(dict.fromkeys(out + g.agg_flags))


def finish(g: Group, extra_reasons: list[str], known: set[str], hdp: bool = False) -> None:
    """Set doubt_flags (the old review reasons are plain tokens at its end, closed list, fixed order), is_hdp_site and the
    lifecycle defaults. needs_review stays False here: only the lifecycle step (master_lifecycle) sets it, for retired and
    reactivated rows."""
    flags = build_flags(g, known)
    reasons = set(g.reasons) | set(extra_reasons)
    if not g.row["lat"] and not reasons & {"address_incomplete", "plz_invalid"}:
        reasons.add("coords_unresolved")  # with an incomplete address the address is the one root cause
    ordered = [r for r in ms.DOUBT_REASONS if r in reasons]
    assert set(ordered) == reasons, f"doubt reason outside the closed list: {reasons - set(ordered)}"
    g.row["doubt_flags"] = "; ".join(dict.fromkeys([x for x in flags.split("; ") if x] + ordered))
    g.row["needs_review"] = "False"
    g.row["needs_review_reason"] = ""
    g.row["is_hdp_site"] = "True" if hdp else ""
    g.row["status"] = "active"
    g.row["retired_on"] = ""
