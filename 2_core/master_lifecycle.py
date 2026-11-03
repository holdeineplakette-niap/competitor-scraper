"""Lifecycle of master rows across months: active -> retired -> (reactivated).

Every master_id ever issued stays in the master. A row of the previous official master that gets no group this month
(master_continuity has already given its id to a re-keyed or moved site where there is one):
  * when every source behind it has a fresh input run, its `missed_runs` goes up by one; it is RETIRED when that reaches
    ms.retire_after(): 1 by default (retired at the first fresh run without it), 2 with ms.TWO_RUN_RETIREMENT on (two fresh
    runs in a row; until then it stays active with its last values, so a one-off gap in a finder never reaches HubSpot);
  * otherwise stays active, unchanged, flagged `not_refreshed:<sources>`, `missed_runs` unchanged (a stale or rejected input
    is no evidence, so it neither counts nor resets);
  * when master_ids merged it into another row (its source rows now belong to that row) it is retired at once and flagged
    `duplicate_of:<id>`; a row retired by the count is flagged the same when an active row of its ÜO has its address. The
    export lists those as CHANGED (a duplicate closed), never as RETIRED.
Every row of this run has `missed_runs = 0`.
A retired row keeps its last known values, `status = retired`, `retired_on` = the run that retired it. If its source
rows come back (master_ids hands the same id to the group), it is ACTIVE again with `retired_on` cleared.

`needs_review` is True ONLY for rows whose status changed in this run: reason `retired` or `reactivated`. It is cleared in
the next month's run. A retired row and a new row at the same address are both flagged `possibly_rekeyed:<other ids>`; they
are never merged here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core import master_schema as ms
from core import normalise as nm


@dataclass
class Lifecycle:
    master: pd.DataFrame  # every row: this run's groups plus the carried-forward rows
    events: list[dict] = field(default_factory=list)  # lines for merge_log.csv
    info: dict = field(default_factory=dict)


def fresh_sources(input_notes: dict, max_age_days: int) -> dict[str, bool]:
    """A source is fresh when its input run is at most `max_age_days` old and no newer run of it was rejected."""
    return {s: n["age_days"] <= max_age_days and not n["newer_not_qualifying"] for s, n in input_notes.items()}


def with_lifecycle_columns(prev: pd.DataFrame) -> pd.DataFrame:
    """A master written before the lifecycle columns existed: every row is active."""
    out = prev.copy()
    if "status" not in out.columns:
        out["status"] = "active"
    if "retired_on" not in out.columns:
        out["retired_on"] = ""
    if "missed_runs" not in out.columns:
        out["missed_runs"] = "0"
    out["status"] = out["status"].replace("", "active")
    out["missed_runs"] = out["missed_runs"].fillna("0").replace("", "0")
    return out


def _flag(row: dict, token: str) -> None:
    flags = [f for f in str(row.get("doubt_flags", "")).split("; ") if f]
    if token not in flags:
        flags.append(token)
    row["doubt_flags"] = "; ".join(flags)


def _addr(row) -> str:
    return nm.address_key(row["street"], row["zip"]) if nm.has_street(row["street"]) and row["zip"] else ""


def apply(cur: pd.DataFrame, prev: pd.DataFrame | None, run_date: str, fresh: dict[str, bool],
          merged_into: dict[str, str] | None = None) -> Lifecycle:
    """`cur` = this run's rows (status active, needs_review False, from master_resolve.finish).
    `merged_into` = {master_id: the id it was merged into} from master_ids.assign."""
    cur = cur.copy()
    cur["missed_runs"] = "0"
    merged_into = merged_into or {}
    empty = {"new": 0, "retired": [], "reactivated": [], "carried_retired": 0, "not_refreshed": [], "missing": [], "duplicates": [],
             "rekey": [], "retired_by_source": {}, "active_total": len(cur), "retired_total": 0, "rows_total": len(cur)}
    if prev is None:
        return Lifecycle(cur.reset_index(drop=True), [], empty)
    prev = with_lifecycle_columns(prev)
    cur_ids = set(cur["master_id"])
    prev_status = dict(zip(prev["master_id"], prev["status"]))
    events: list[dict] = []

    reactivated = sorted(i for i in cur_ids if prev_status.get(i) == "retired")
    mask = cur["master_id"].isin(reactivated)
    cur.loc[mask, ["needs_review", "needs_review_reason"]] = ["True", "reactivated"]
    for r in cur[mask].itertuples():
        events.append({"master_id": r.master_id, "location_ids": r.source_ids, "action": "lifecycle", "rule": "reactivated", "field": "",
                       "chosen_source": "", "chosen_value": "", "dropped": "", "note": "back in the sources; same master_id, status active again"})

    carried: list[dict] = []
    retired_now: list[str] = []
    not_refreshed: list[dict] = []
    missing: list[dict] = []
    duplicates: list[dict] = []
    active_by_addr: dict[tuple[str, str], str] = {}
    for r in cur.sort_values("master_id").to_dict("records"):
        if _addr(r):
            active_by_addr.setdefault((r["uo"], _addr(r)), r["master_id"])
    carried_retired = 0
    by_source: dict[str, int] = {}
    for row in prev.to_dict("records"):
        mid = row["master_id"]
        if mid in cur_ids:
            continue
        row = {c: row.get(c, "") for c in ms.MASTER_COLUMNS}
        row["needs_review"], row["needs_review_reason"] = "False", ""
        missed = int(row.get("missed_runs") or 0)
        if prev_status[mid] == "retired":  # stays retired, unchanged; the flag of its retirement month is gone
            carried.append(row)
            carried_retired += 1
            continue
        sources = [s for s in row["listed_by"].split("|") if s]
        stale = [s for s in sources if not fresh.get(s, False)]
        if mid in merged_into:  # its source rows belong to another row now: a duplicate, closed at once
            _flag(row, f"duplicate_of:{merged_into[mid]}")
            row.update(status="retired", retired_on=run_date, needs_review="True", needs_review_reason="retired")
            carried.append(row)
            retired_now.append(mid)
            duplicates.append({"master_id": mid, "of": merged_into[mid]})
            events.append({"master_id": mid, "location_ids": row["source_ids"], "action": "lifecycle", "rule": "duplicate_closed", "field": "",
                           "chosen_source": "", "chosen_value": merged_into[mid], "dropped": "",
                           "note": f"merged into {merged_into[mid]}: the duplicate is closed (exported as CHANGED)"})
            continue
        if stale:
            _flag(row, "not_refreshed:" + "|".join(stale))
            carried.append(row)
            not_refreshed.append({"master_id": mid, "sources": stale})
            events.append({"master_id": mid, "location_ids": row["source_ids"], "action": "lifecycle", "rule": "not_refreshed", "field": "",
                           "chosen_source": "", "chosen_value": "", "dropped": "",
                           "note": f"not in this run, but the input of {', '.join(stale)} is stale or was replaced: kept active, not retired"})
            continue
        missed += 1
        row["missed_runs"] = str(missed)
        if missed < ms.retire_after():  # not found in a fresh run, but not yet often enough (two-run safety on): stays active
            carried.append(row)
            missing.append({"master_id": mid, "missed_runs": missed, "last_seen": row["last_seen"]})
            events.append({"master_id": mid, "location_ids": row["source_ids"], "action": "lifecycle", "rule": "missing", "field": "",
                           "chosen_source": "", "chosen_value": str(missed), "dropped": "",
                           "note": f"not found in a fresh run ({missed} of {ms.retire_after()}): kept active"})
            continue
        row.update(status="retired", retired_on=run_date, needs_review="True", needs_review_reason="retired")
        dup = active_by_addr.get((row["uo"], _addr(row))) if _addr(row) else None
        if dup:  # another active row of its ÜO at its address: it was a duplicate, not a closure
            _flag(row, f"duplicate_of:{dup}")
            duplicates.append({"master_id": mid, "of": dup})
        carried.append(row)
        retired_now.append(mid)
        for s in sources:
            by_source[s] = by_source.get(s, 0) + 1
        events.append({"master_id": mid, "location_ids": row["source_ids"], "action": "lifecycle", "rule": "duplicate_closed" if dup else "retired",
                       "field": "", "chosen_source": "", "chosen_value": dup or run_date, "dropped": "",
                       "note": f"not found in {missed} fresh run{'s in a row' if missed > 1 else ''}" + (f"; same address as {dup}: closed as its duplicate" if dup else "")})

    # re-keying: a site retired now and a brand-new id at the same address. Both are flagged, nothing is merged.
    prev_ids = set(prev["master_id"])
    new_rows = [r for r in cur.to_dict("records") if r["master_id"] not in prev_ids]
    retired_set = set(retired_now)
    retired_rows = [r for r in carried if r["master_id"] in retired_set]
    old_by_addr: dict[str, list[dict]] = {}
    for r in retired_rows:
        if _addr(r):
            old_by_addr.setdefault(_addr(r), []).append(r)
    new_by_addr: dict[str, list[dict]] = {}
    for r in new_rows:
        if _addr(r):
            new_by_addr.setdefault(_addr(r), []).append(r)
    rekey: list[dict] = []
    flag_new: dict[str, str] = {}
    for key, olds in old_by_addr.items():
        news = new_by_addr.get(key)
        if not news:
            continue
        for o in olds:
            _flag(o, "possibly_rekeyed:" + "|".join(sorted(n["master_id"] for n in news)))
        for n in news:
            flag_new[n["master_id"]] = "possibly_rekeyed:" + "|".join(sorted(o["master_id"] for o in olds))
        rekey.append({"address": key, "retired": sorted(o["master_id"] for o in olds), "new": sorted(n["master_id"] for n in news)})
        for o in olds:
            events.append({"master_id": o["master_id"], "location_ids": o["source_ids"], "action": "lifecycle", "rule": "possibly_rekeyed", "field": "",
                           "chosen_source": "", "chosen_value": "|".join(sorted(n["master_id"] for n in news)), "dropped": "",
                           "note": "same address as a new master_id: re-keyed by a source? not merged, a human decides"})
    for i, r in cur.iterrows():
        if r["master_id"] in flag_new:
            row = r.to_dict()
            _flag(row, flag_new[r["master_id"]])
            cur.at[i, "doubt_flags"] = row["doubt_flags"]

    full = pd.concat([cur, pd.DataFrame(carried, columns=ms.MASTER_COLUMNS)], ignore_index=True) if carried else cur
    full = full[ms.MASTER_COLUMNS].sort_values("master_id", kind="stable").reset_index(drop=True)
    info = {"new": len(new_rows), "retired": sorted(retired_now), "reactivated": reactivated, "carried_retired": carried_retired,
            "not_refreshed": not_refreshed, "missing": missing, "duplicates": duplicates, "rekey": rekey, "retired_by_source": dict(sorted(by_source.items())),
            "active_total": int((full["status"] == "active").sum()), "retired_total": int((full["status"] == "retired").sum()),
            "rows_total": len(full)}
    return Lifecycle(full, events, info)
