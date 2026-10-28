"""Adaptive grid for location finders that cap results per query.

Source-independent: the finder is reached through a query function passed in
(dependency injection), so the same grid runs against a real API or the
simulator below.

Squares are laid out in km on an equirectangular projection around lat 51.
Distances that decide coverage (query radius, completeness check) are
great-circle distances, because that is what the server measures: the
projection is up to ~8 % short east-west in the south of Germany, which would
exceed the 3 km margin on large squares.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import numpy as np
import pandas as pd

from core.geo import haversine_km

REF_LAT = 51.0
KM_PER_DEG_LNG = math.cos(math.radians(REF_LAT)) * 111.32
KM_PER_DEG_LAT = 110.57

GERMANY_BBOX = (47.2, 55.1, 5.8, 15.1)  # lat_min, lat_max, lng_min, lng_max
TOP_SQUARE_KM = 100.0
MIN_SQUARE_KM = 1.0
RADIUS_MARGIN_KM = 3.0
SKIP_BUFFER_KM = 15.0


def to_xy(lat, lng):
    return np.asarray(lng) * KM_PER_DEG_LNG, np.asarray(lat) * KM_PER_DEG_LAT


def to_latlng(x, y):
    return y / KM_PER_DEG_LAT, x / KM_PER_DEG_LNG


class StopRun(Exception):
    """Raised by a query function to abort the whole run (e.g. API key rejected)."""


@dataclass(frozen=True)
class Square:
    x: float  # south-west corner, projected km
    y: float
    size: float  # edge length, projected km

    def center_xy(self) -> tuple[float, float]:
        return self.x + self.size / 2, self.y + self.size / 2

    def corners_latlng(self) -> list[tuple[float, float]]:
        return [to_latlng(self.x + dx, self.y + dy) for dx in (0, self.size) for dy in (0, self.size)]

    def bounds(self) -> dict:
        lat_min, lng_min = to_latlng(self.x, self.y)
        lat_max, lng_max = to_latlng(self.x + self.size, self.y + self.size)
        return {
            "lat_min": round(lat_min, 5),
            "lat_max": round(lat_max, 5),
            "lng_min": round(lng_min, 5),
            "lng_max": round(lng_max, 5),
        }

    def split(self) -> list[Square]:
        h = self.size / 2
        return [Square(self.x + dx, self.y + dy, h) for dy in (0, h) for dx in (0, h)]


@dataclass(frozen=True)
class QueryPoint:
    zip: str
    city: str
    lat: float
    lng: float


@dataclass
class QueryResult:
    count: int
    max_count: int
    center_lat: float  # the centre the server actually used
    center_lng: float
    items: dict  # location id -> raw item
    http_status: int | None = None
    raw_file: str | None = None
    cached: bool = False  # answered from the same-day response cache, no request sent


@dataclass
class GridResult:
    items: dict = field(default_factory=dict)  # deduped by location id, first seen wins
    log: list = field(default_factory=list)
    coverage_warnings: list = field(default_factory=list)
    squares_skipped: int = 0
    squares_not_queried: int = 0  # still pending when max_queries was reached, the run aborted or stopped early
    stopped_early: bool = False  # stop_when returned True (early stop, see gtue.EarlyStop)
    pending_squares: list = field(default_factory=list)  # bounds of squares not queried (see squares_not_queried)

    def count(self, status: str) -> int:
        return sum(1 for row in self.log if row["status"] == status)

    def open_squares(self) -> list[dict]:
        """Every square the grid could not prove complete: coverage warnings (queried, incomplete at
        minimum size) and squares never queried (limit, abort, early stop)."""
        warned = [{"kind": "coverage warning", **w} for w in self.coverage_warnings]
        return warned + [{"kind": "not queried", **s} for s in self.pending_squares]


QueryFn = Callable[[QueryPoint, int], QueryResult]


class PointIndex:
    """Query points (e.g. PLZ centroids) with nearest-point and proximity lookups."""

    def __init__(self, points: pd.DataFrame):
        self.points = points.reset_index(drop=True)
        self.x, self.y = to_xy(self.points["lat"].to_numpy(float), self.points["lng"].to_numpy(float))

    def nearest(self, x: float, y: float) -> QueryPoint:
        i = int(np.argmin((self.x - x) ** 2 + (self.y - y) ** 2))
        row = self.points.iloc[i]
        return QueryPoint(str(row["zip"]), str(row["city"]), float(row["lat"]), float(row["lng"]))

    def any_near(self, square: Square, buffer_km: float) -> bool:
        dx = np.maximum.reduce([square.x - self.x, np.zeros_like(self.x), self.x - (square.x + square.size)])
        dy = np.maximum.reduce([square.y - self.y, np.zeros_like(self.y), self.y - (square.y + square.size)])
        return bool(np.any(dx**2 + dy**2 <= buffer_km**2))


def initial_squares(bbox=GERMANY_BBOX, size_km: float = TOP_SQUARE_KM) -> list[Square]:
    lat_min, lat_max, lng_min, lng_max = bbox
    x0, y0 = to_xy(lat_min, lng_min)
    x1, y1 = to_xy(lat_max, lng_max)
    nx = math.ceil((x1 - x0) / size_km)
    ny = math.ceil((y1 - y0) / size_km)
    return [Square(float(x0 + i * size_km), float(y0 + j * size_km), size_km) for j in range(ny) for i in range(nx)]


def farthest_corner_km(lat: float, lng: float, square: Square) -> float:
    return max(float(haversine_km(lat, lng, c_lat, c_lng)) for c_lat, c_lng in square.corners_latlng())


def query_radius_km(point: QueryPoint, square: Square, margin_km: float = RADIUS_MARGIN_KM) -> int:
    return math.ceil(farthest_corner_km(point.lat, point.lng, square) + margin_km)


LOG_COLUMNS = [
    "seq", "status", "square_km", "lat_min", "lat_max", "lng_min", "lng_max", "zip", "city",
    "radius_km", "radius_m", "center_lat", "center_lng", "center_offset_km", "corner_dist_km", "count", "max_count",
    "http_status", "raw_file", "note", "cached", "time",
]


def _log_row(seq, status, square, point, radius_km, res, corner_dist_km, note=None) -> dict:
    """One query log row. res is None for a query that raised (status "error")."""
    bounds = square.bounds() if square else dict.fromkeys(["lat_min", "lat_max", "lng_min", "lng_max"])
    return {
        "seq": seq,
        "status": status,
        "square_km": round(square.size, 3) if square else None,
        **bounds,
        "zip": point.zip,
        "city": point.city,
        "radius_km": radius_km,
        "radius_m": radius_km * 1000,
        "center_lat": res.center_lat if res else None,
        "center_lng": res.center_lng if res else None,
        # distance between the point we queried (PLZ centroid) and the centre the server used
        "center_offset_km": (
            round(float(haversine_km(point.lat, point.lng, res.center_lat, res.center_lng)), 2) if res else None
        ),
        "corner_dist_km": round(corner_dist_km, 2) if corner_dist_km is not None else None,
        "count": res.count if res else None,
        "max_count": res.max_count if res else None,
        "http_status": res.http_status if res else None,
        "raw_file": res.raw_file if res else None,
        "note": note,
        "cached": bool(res.cached) if res else False,
        "time": datetime.now().isoformat(timespec="seconds"),  # when the row was logged
    }


def _collect(result: GridResult, res: QueryResult) -> None:
    for key, item in res.items.items():
        result.items.setdefault(key, item)


def _query(query_fn: QueryFn, point: QueryPoint, radius_km: int, square, result: GridResult) -> QueryResult:
    """Run one query; if it raises StopRun, log it as an "error" row and re-raise."""
    try:
        return query_fn(point, radius_km)
    except StopRun as exc:
        row = _log_row(len(result.log) + 1, "error", square, point, radius_km, None, None, f"{type(exc).__name__}: {exc}")
        result.log.append(row)
        raise


def reference_check(query_fn: QueryFn, point: QueryPoint, radius_km: int, result: GridResult) -> QueryResult:
    """The reference query at the start of a run: one wide query that learns the total (MaxCount).
    Its items are collected too. A failure is logged as "error" and stops the run."""
    res = _query(query_fn, point, radius_km, None, result)
    _collect(result, res)
    result.log.append(_log_row(len(result.log) + 1, "reference_check", None, point, radius_km, res, None))
    return res


def reference_recheck(
    query_fn: QueryFn, point: QueryPoint, radius_km: int, result: GridResult, status: str = "reference_recheck"
):
    """The reference query again: before an early stop ("reference_recheck") or at the end of a full
    run ("reference_end"). Pass a query function that bypasses any cache. Returns the QueryResult and
    logs it under `status`, or {"error": ...} if it fails: the caller decides what that means, it is
    not an abort here. Its items are not collected."""
    try:
        res = query_fn(point, radius_km)
    except StopRun as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    result.log.append(_log_row(len(result.log) + 1, status, None, point, radius_km, res, None))
    return res


def cap_probe(query_fn: QueryFn, point: QueryPoint, radius_km: int, result: GridResult) -> QueryResult:
    """One diagnostic query whose MaxCount is compared with another probe's. Logged as "cap_probe",
    items not collected. A failure is logged as "error" and stops the run."""
    res = _query(query_fn, point, radius_km, None, result)
    result.log.append(_log_row(len(result.log) + 1, "cap_probe", None, point, radius_km, res, None))
    return res


def geocoding_outliers(log: list[dict], limit_km: float) -> list[dict]:
    """Queries whose server centre is more than limit_km from the point we queried,
    with the square they covered (square fields are None for the national check).
    The first query of a re-queried square is left out: its final query lists the same PLZ.
    Reference rechecks and cap probes are left out too (the reference offset is checked on its own)."""
    keys = ["seq", "status", "zip", "city", "center_offset_km", "square_km", "lat_min", "lat_max", "lng_min", "lng_max"]
    skip = ("requery", "reference_recheck", "reference_end", "cap_probe")
    return [
        {k: r[k] for k in keys}
        for r in log
        if r["status"] not in skip and r["center_offset_km"] is not None and r["center_offset_km"] > limit_km
    ]


def _record_pending(result: GridResult, squares, index: PointIndex, skip_buffer_km: float) -> None:
    """Remember the squares that would still be queried (squares with no query point nearby are
    skipped, not pending): count in squares_not_queried, bounds in pending_squares."""
    result.pending_squares = [
        {**s.bounds(), "square_km": round(s.size, 3)} for s in squares if index.any_near(s, skip_buffer_km)
    ]
    result.squares_not_queried = len(result.pending_squares)


def run_grid(
    query_fn: QueryFn,
    points: pd.DataFrame,
    bbox=GERMANY_BBOX,
    top_size_km: float = TOP_SQUARE_KM,
    min_size_km: float = MIN_SQUARE_KM,
    margin_km: float = RADIUS_MARGIN_KM,
    skip_buffer_km: float = SKIP_BUFFER_KM,
    max_queries: int | None = None,
    result: GridResult | None = None,
    requery_uncovered: bool = True,
    stop_when: Callable[[GridResult], bool] | None = None,
) -> GridResult:
    """Cover bbox with squares, query each, split incomplete squares until min_size_km.

    A square is complete only if Count == MaxCount and the radius used covers
    the square from the centre the server actually used. Squares with no query
    point within skip_buffer_km (sea, abroad) are skipped at every level.
    Pass `result` to keep partial results if query_fn raises StopRun.

    requery_uncovered: if all locations came back (Count == MaxCount) but the radius doesn't
        reach the square's far corner from the server's actual centre, query the same point
        once more with the radius measured from that centre, instead of splitting into 4.
        The first query is logged with status "requery". Splitting still follows if the
        wider query returns fewer than MaxCount.
    stop_when: called with the result before each square is taken; if it returns True the grid
        stops (result.stopped_early = True, the squares still queued are recorded as pending).
        What the stop means is up to the caller (gtue.EarlyStop).
    """
    index = PointIndex(points)
    result = result if result is not None else GridResult()
    queue = deque(initial_squares(bbox, top_size_km))
    queries = 0

    def ask(square, point, radius_km):
        try:
            res = _query(query_fn, point, radius_km, square, result)
        except StopRun:
            # aborted (limit, 401, ...): this square and the queue were never queried
            _record_pending(result, [square, *queue], index, skip_buffer_km)
            raise
        _collect(result, res)
        return res

    while queue:
        if stop_when is not None and stop_when(result):
            result.stopped_early = True
            _record_pending(result, queue, index, skip_buffer_km)
            break
        square = queue.popleft()
        if not index.any_near(square, skip_buffer_km):
            result.squares_skipped += 1
            continue
        if max_queries is not None and queries >= max_queries:
            _record_pending(result, [square, *queue], index, skip_buffer_km)
            break

        point = index.nearest(*square.center_xy())
        radius_km = query_radius_km(point, square, margin_km)
        res = ask(square, point, radius_km)
        queries += 1

        corner_dist = farthest_corner_km(res.center_lat, res.center_lng, square)
        all_returned = res.count == res.max_count
        covered = radius_km >= corner_dist
        requeried = False
        if (
            requery_uncovered
            and all_returned
            and not covered
            and (max_queries is None or queries < max_queries)
        ):
            first_note = (
                f"Count {res.count} == MaxCount; radius {radius_km} km < {corner_dist:.1f} km from actual centre;"
                " re-queried from the actual centre"
            )
            result.log.append(_log_row(len(result.log) + 1, "requery", square, point, radius_km, res, corner_dist, first_note))
            radius_km = math.ceil(corner_dist + margin_km)
            res = ask(square, point, radius_km)
            queries += 1
            corner_dist = farthest_corner_km(res.center_lat, res.center_lng, square)
            all_returned = res.count == res.max_count
            covered = radius_km >= corner_dist
            requeried = True

        note = "re-queried from the actual centre" if requeried else None
        if all_returned and covered:
            status = "complete"
        else:
            note = "; ".join(
                ([note] if note else [])
                + ([f"Count {res.count} < MaxCount {res.max_count}"] if not all_returned else [])
                + ([f"radius {radius_km} km < {corner_dist:.1f} km from actual centre"] if not covered else [])
            )
            if square.size / 2 >= min_size_km:
                status = "split"
                queue.extend(square.split())
            else:
                status = "warning"
                result.coverage_warnings.append(
                    {**square.bounds(), "square_km": round(square.size, 3), "zip": point.zip, "reason": note}
                )
        result.log.append(_log_row(len(result.log) + 1, status, square, point, radius_km, res, corner_dist, note))

    return result


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------

def _offset(lat: float, lng: float, dist_km: float, angle: float) -> tuple[float, float]:
    d_lat = dist_km * math.sin(angle) / KM_PER_DEG_LAT
    d_lng = dist_km * math.cos(angle) / (111.32 * math.cos(math.radians(lat)))
    return lat + d_lat, lng + d_lng


class SimulatedFinder:
    """Mimics a capped finder: nearest `top` locations within the radius, Count, MaxCount,
    and a geocoded centre offset from the query point (deterministic per zip, like a real geocoder)."""

    def __init__(self, locations: pd.DataFrame, top: int = 20, center_offset_km: float = 2.0, seed: int = 0):
        self.ids = locations["id"].astype(str).to_numpy()
        self.lat = locations["lat"].to_numpy(float)
        self.lng = locations["lng"].to_numpy(float)
        self.top = top
        self.center_offset_km = center_offset_km
        self.seed = seed

    def center_for(self, point: QueryPoint) -> tuple[float, float]:
        rng = random.Random(f"{self.seed}:{point.zip}")
        return _offset(point.lat, point.lng, rng.uniform(0, self.center_offset_km), rng.uniform(0, 2 * math.pi))

    def search(self, point: QueryPoint, radius_km: float) -> tuple[float, float, int, list[str]]:
        """Return (center_lat, center_lng, max_count, ids of the nearest `top`)."""
        c_lat, c_lng = self.center_for(point)
        dist = haversine_km(c_lat, c_lng, self.lat, self.lng)
        inside = np.flatnonzero(dist <= radius_km)
        nearest = inside[np.argsort(dist[inside], kind="stable")][: self.top]
        return c_lat, c_lng, int(inside.size), [str(i) for i in self.ids[nearest]]

    def query(self, point: QueryPoint, radius_km: int) -> QueryResult:
        """Grid query function (items are the location ids)."""
        c_lat, c_lng, max_count, ids = self.search(point, radius_km)
        return QueryResult(len(ids), max_count, c_lat, c_lng, {i: i for i in ids}, http_status=200)


BIG_CITIES = {
    "Köln": (50.9375, 6.9603),
    "Berlin": (52.5200, 13.4050),
    "Hamburg": (53.5511, 9.9937),
    "München": (48.1374, 11.5755),
    "Frankfurt am Main": (50.1109, 8.6821),
}


def fake_locations(
    points: pd.DataFrame,
    n_total: int = 1200,
    clusters: dict = BIG_CITIES,
    per_cluster: int = 60,
    cluster_radius_km: float = 10.0,
    scatter_km: float = 2.0,
    seed: int = 0,
) -> pd.DataFrame:
    """Fake locations: `per_cluster` within cluster_radius_km of each city in `clusters`,
    the rest scattered around random query points. Each gets the zip/city of its nearest point."""
    rng = random.Random(seed)
    rows = []
    for name, (lat, lng) in clusters.items():
        for _ in range(per_cluster):
            p_lat, p_lng = _offset(lat, lng, cluster_radius_km * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi))
            rows.append((p_lat, p_lng, name))
    while len(rows) < n_total:
        base = points.iloc[rng.randrange(len(points))]
        p_lat, p_lng = _offset(base["lat"], base["lng"], scatter_km * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi))
        rows.append((p_lat, p_lng, None))

    df = pd.DataFrame(rows, columns=["lat", "lng", "cluster"])
    index = PointIndex(points)
    nearest = [index.nearest(*to_xy(lat, lng)) for lat, lng in zip(df["lat"], df["lng"])]
    df["zip"] = [p.zip for p in nearest]
    df["city"] = [p.city for p in nearest]
    df.insert(0, "id", [str(i) for i in range(len(df))])
    return df
