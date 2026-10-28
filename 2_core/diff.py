"""Change report between two masters, keyed by master_id."""

from __future__ import annotations

import pandas as pd

from core import master_lifecycle
from core import normalise as nm
from core.geo import haversine_km

MOVED_KM = 0.2  # coordinates count as changed only if they moved more than 200 m
DIFF_COLUMNS = ["change", "master_id", "other_master_id", "field", "old", "new", "uo", "name", "street", "zip", "city"]
# status changes get their own `retired` / `reactivated` lines; the review flag only mirrors them
SKIP_FIELDS = {"master_id", "first_seen", "last_seen", "scraped_at", "status", "retired_on", "needs_review", "needs_review_reason"}


def _addr(row) -> str:
    return nm.address_key(row["street"], row["zip"]) if nm.has_street(row["street"]) and row["zip"] else ""


def _ctx(row) -> dict:
    return {"uo": row["uo"], "name": row["name"], "street": row["street"], "zip": row["zip"], "city": row["city"]}


def diff_masters(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """new / retired / reactivated / disappeared / changed / possibly_rekeyed. `old` None = first master: headers only.
    A master written before the lifecycle columns existed counts every row as active."""
    if old is None:
        return pd.DataFrame(columns=DIFF_COLUMNS)
    old = master_lifecycle.with_lifecycle_columns(old)
    new = master_lifecycle.with_lifecycle_columns(new)
    o = old.set_index("master_id")
    n = new.set_index("master_id")
    gone_ids = [i for i in o.index if i not in n.index]
    new_ids = [i for i in n.index if i not in o.index]
    rows: list[dict] = []

    # a vanished id whose address reappears under a brand-new id is one re-keying, not a closure plus an opening
    new_by_addr: dict[str, list[str]] = {}
    for i in new_ids:
        a = _addr(n.loc[i])
        if a:
            new_by_addr.setdefault(a, []).append(i)
    rekeyed_new: set[str] = set()
    rekeyed_old: set[str] = set()
    for i in gone_ids:
        a = _addr(o.loc[i])
        hits = [x for x in new_by_addr.get(a, []) if x not in rekeyed_new] if a else []
        if len(hits) == 1:
            rekeyed_old.add(i)
            rekeyed_new.add(hits[0])
            rows.append({"change": "possibly_rekeyed", "master_id": i, "other_master_id": hits[0], "field": "", "old": i,
                         "new": hits[0], **_ctx(o.loc[i])})
    for i in gone_ids:
        if i not in rekeyed_old:
            rows.append({"change": "disappeared", "master_id": i, "other_master_id": "", "field": "", "old": "", "new": "",
                         **_ctx(o.loc[i])})
    for i in new_ids:
        if i not in rekeyed_new:
            rows.append({"change": "new", "master_id": i, "other_master_id": "", "field": "", "old": "", "new": "",
                         **_ctx(n.loc[i])})
    for i in [x for x in n.index if x in o.index]:
        a, b = o.loc[i], n.loc[i]
        if a["status"] != b["status"]:
            rows.append({"change": "retired" if b["status"] == "retired" else "reactivated", "master_id": i, "other_master_id": "",
                         "field": "status", "old": a["status"], "new": b["status"], **_ctx(b)})
        for col in n.columns:
            if col in SKIP_FIELDS or col not in o.columns:
                continue
            va, vb = a[col], b[col]
            if va == vb:
                continue
            if col in ("lat", "lng"):
                continue
            rows.append({"change": "changed", "master_id": i, "other_master_id": "", "field": col, "old": va, "new": vb,
                         **_ctx(b)})
        try:
            la, lo, lb, lq = float(a["lat"]), float(a["lng"]), float(b["lat"]), float(b["lng"])
        except ValueError:
            if (a["lat"], a["lng"]) != (b["lat"], b["lng"]):
                rows.append({"change": "changed", "master_id": i, "other_master_id": "", "field": "lat/lng",
                             "old": f"{a['lat']},{a['lng']}", "new": f"{b['lat']},{b['lng']}", **_ctx(b)})
        else:
            if float(haversine_km(la, lo, lb, lq)) > MOVED_KM:
                rows.append({"change": "changed", "master_id": i, "other_master_id": "", "field": "lat/lng",
                             "old": f"{a['lat']},{a['lng']}", "new": f"{b['lat']},{b['lng']}", **_ctx(b)})
    order = {"new": 0, "retired": 1, "reactivated": 2, "disappeared": 3, "possibly_rekeyed": 4, "changed": 5}
    out = pd.DataFrame(rows, columns=DIFF_COLUMNS)
    return out.sort_values(["change", "master_id", "field"], key=lambda s: s.map(order) if s.name == "change" else s,
                           kind="stable").reset_index(drop=True)
