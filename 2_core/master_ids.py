"""Stable master ids (PS-000001) carried over from the previous official master by source_ids overlap."""

from __future__ import annotations

import re

ID_RE = re.compile(r"^PS-(\d{6})$")


def _num(master_id: str) -> int:
    m = ID_RE.match(master_id)
    if not m:
        raise ValueError(f"not a master id: {master_id!r}")
    return int(m.group(1))


def fmt(n: int) -> str:
    return f"PS-{n:06d}"


def max_id(ids) -> int:
    return max([_num(i) for i in ids] + [0])


def assign(groups: list[list[str]], previous: dict[str, list[str]], max_issued: int = 0) -> tuple[list[str], list[dict]]:
    """Master ids for new groups (each a list of location_ids) given the previous master {master_id: [location_ids]}.

    `max_issued` is the highest id ever issued before (kept in each official run's summary), so an id that disappeared
    in an earlier month is never handed out again.

    Deterministic: a previous id goes to the group that keeps most of its rows (tie: the group whose sorted ids come
    first). A group that wins several previous ids (a merge) keeps the lowest and the others are retired. A group that
    wins none (a split-off part, or new) gets a new id after the highest id ever issued.
    Returns (ids aligned with `groups`, events [{event, master_id, ...}]).
    """
    owner = {i: mid for mid, ids in previous.items() for i in ids}
    top = max(max_id(previous), max_issued)
    order = sorted(range(len(groups)), key=lambda k: sorted(groups[k]))
    position = {k: pos for pos, k in enumerate(order)}
    overlap: dict[int, dict[str, int]] = {}
    for k in order:
        counts: dict[str, int] = {}
        for i in groups[k]:
            if i in owner:
                counts[owner[i]] = counts.get(owner[i], 0) + 1
        overlap[k] = counts
    winner: dict[str, int] = {}
    for k in order:
        for mid, c in overlap[k].items():
            best = winner.get(mid)
            if best is None or c > overlap[best][mid]:  # strictly more rows; a tie keeps the earlier group
                winner[mid] = k
    out: list[str | None] = [None] * len(groups)
    events: list[dict] = []
    for k in order:
        owned = sorted((m for m in overlap[k] if winner[m] == k), key=_num)
        if owned:
            out[k] = owned[0]
            events += [{"event": "retired", "master_id": m, "merged_into": owned[0]} for m in owned[1:]]
    for k in order:
        if out[k] is None:
            top += 1
            out[k] = fmt(top)
            if overlap[k]:
                events.append({"event": "split_new_id", "master_id": out[k], "from": ",".join(sorted(overlap[k], key=_num))})
    assert len(set(out)) == len(out), "duplicate master ids assigned"
    return [str(m) for m in out], events
