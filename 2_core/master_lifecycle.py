"""Lifecycle of master rows across months: active -> retired -> (reactivated).

Every master_id ever issued stays in the master. A row of the previous official master that gets no group this month:
  * is RETIRED at once (no grace period) when every source behind it has a fresh input run this month;
  * otherwise stays active, unchanged, flagged `not_refreshed:<sources>` (a stale or replaced input is not a closure).
A retired row keeps its last known values, `status = retired`, `retired_on` = the first run that missed it. If its source
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
    out["status"] = out["status"].replace("", "active")
    return out


def _flag(row: dict, token: str) -> None:
    flags = [f for f in str(row.get("doubt_flags", "")).split("; ") if f]
    if token not in flags:
        flags.append(token)
    row["doubt_flags"] = "; ".join(flags)


def _addr(row) -> str:
    return nm.address_key(row["street"], row["zip"]) if nm.has_street(row["street"]) and row["zip"] else ""


def apply(cur: pd.DataFrame, prev: pd.DataFrame | None, run_date: str, fresh: dict[str, bool]) -> Lifecycle:
    """`cur` = this run's rows (status active, needs_review False, from master_resolve.finish)."""
    cur = cur.copy()
    empty = {"new": 0, "retired": [], "reactivated": [], "carried_retired": 0, "not_refreshed": [], "rekey": [],
             "retired_by_source": {}, "active_total": len(cur), "retired_total": 0, "rows_total": len(cur)}
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
    carried_retired = 0
    by_source: dict[str, int] = {}
    for row in prev.to_dict("records"):
        mid = row["master_id"]
        if mid in cur_ids:
            continue
        row = {c: row.get(c, "") for c in ms.MASTER_COLUMNS}
        row["needs_review"], row["needs_review_reason"] = "False", ""
        if prev_status[mid] == "retired":  # stays retired, unchanged; the flag of its retirement month is gone
            carried.append(row)
            carried_retired += 1
            continue
        sources = [s for s in row["listed_by"].split("|") if s]
        stale = [s for s in sources if not fresh.get(s, False)]
        if stale:
            _flag(row, "not_refreshed:" + "|".join(stale))
            carried.append(row)
            not_refreshed.append({"master_id": mid, "sources": stale})
            events.append({"master_id": mid, "location_ids": row["source_ids"], "action": "lifecycle", "rule": "not_refreshed", "field": "",
                           "chosen_source": "", "chosen_value": "", "dropped": "",
                           "note": f"not in this run, but the input of {', '.join(stale)} is stale or was replaced: kept active, not retired"})
            continue
        row.update(status="retired", retired_on=run_date, needs_review="True", needs_review_reason="retired")
        carried.append(row)
        retired_now.append(mid)
        for s in sources:
            by_source[s] = by_source.get(s, 0) + 1
        events.append({"master_id": mid, "location_ids": row["source_ids"], "action": "lifecycle", "rule": "retired", "field": "",
                       "chosen_source": "", "chosen_value": run_date, "dropped": "", "note": "no source lists it any more (fresh inputs)"})

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
            "not_refreshed": not_refreshed, "rekey": rekey, "retired_by_source": dict(sorted(by_source.items())),
            "active_total": int((full["status"] == "active").sum()), "retired_total": int((full["status"] == "retired").sum()),
            "rows_total": len(full)}
    return Lifecycle(full, events, info)
