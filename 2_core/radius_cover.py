"""Coverage planning and proof for fixed-radius locators ("all sites within R km of a point").

For finders that answer one query with every site inside a fixed radius and no count field (KÜS: 50 km), so
the adaptive grid in grid.py (which needs a total per query) does not apply. Source-independent: a finder is
reached through a query function passed in by the caller. The first use is KÜS; any other fixed-radius
locator can reuse it.

Terms
  query point   a regular PLZ centroid (GeoNames) that we send to the finder
  centre        the point the SERVER actually searched around (it geocodes the PLZ itself, usually a few km off
                the GeoNames centroid). Coverage is always judged from the centres, never from the query points.
  observation   one answered query: its centre and the ids it returned

Rules (all distances in km, derived from the radius R in `CoverConfig`)
  A  coverage     every query point lies within cover_km of at least one centre
  B  fringe       every collected site with coordinates lies within fringe_km of at least one centre
                  (a site seen only at the rim of one circle may have unseen neighbours: query its own PLZ)
  C  consistency  every centre within consistency_km of a collected site returned that site. A miss means a
                  count cap, a flaky server or a changed radius. Our substitute for a count field.
                  Sites farther than consistency_km (the boundary zone up to R) are not checked.
  D  cap detector a response at or above `cap_fail_count`, or the same large count in several responses
                  (a plateau, the signature of a cap), is a failure.

A second family, "Variable radius" below, covers finders that return the N nearest sites (TÜV SÜD): the proven
radius differs per answer (`Observation.radius_km`), coverage is judged with a margin M, and a premise check
replaces rules B-D.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from core.geo import haversine_km
from core.grid import GERMANY_BBOX, to_latlng, to_xy


@dataclass(frozen=True)
class CoverConfig:
    """All numbers of the coverage design in one place. Defaults are for a 50 km finder (KÜS)."""

    radius_km: float = 50.0  # the finder's fixed search radius
    cover_km: float = 25.0  # rule A: every query point within this of a centre. radius - cover = room for the distance
    #                         between a site and the centroid of its own PLZ (measured max for KÜS: 12.8 km)
    fringe_km: float = 45.0  # rule B
    consistency_km: float = 48.0  # rule C (2 km below the radius: distance rounding, boundary zone)
    lattice_factor: float = 0.85  # spacing = lattice_factor * sqrt(3) * cover_km; < 1 leaves room for snapping to a PLZ
    snap_max_km: float = 15.0  # a lattice point with no PLZ this close (sea, abroad) is skipped
    max_fill_requests: int = 100  # bound on the fill-in queries (rule A and B); a simulated crawl needs ~10
    cap_fail_count: int = 500  # rule D: a single response this large is a failure (no cap known for KÜS up to 231)
    plateau_min_count: int = 40  # rule D: the max count of the run, seen this often and at least this big = cap signature
    plateau_repeats: int = 3

    @classmethod
    def for_radius(cls, radius_km: float, **overrides) -> "CoverConfig":
        """Proportions of the 50 km design scaled to another radius (cover 50 %, fringe 90 %, consistency 96 %)."""
        base = {
            "radius_km": radius_km,
            "cover_km": radius_km / 2,
            "fringe_km": 0.9 * radius_km,
            "consistency_km": 0.96 * radius_km,
            "snap_max_km": 15.0,
        }
        base.update(overrides)
        return cls(**base)

    @property
    def lattice_spacing_km(self) -> float:
        return self.lattice_factor * math.sqrt(3) * self.cover_km


@dataclass
class Observation:
    """One answered query. centre is None when the response was empty (the server tells no centre then)."""

    key: str  # the queried PLZ
    centre: tuple[float, float] | None
    ids: frozenset
    count: int
    max_dist_km: float | None = None  # largest distance the server reported in this response
    radius_km: float | None = None  # variable-radius finders only: the radius this answer proves (see "Variable radius" below)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def hex_lattice(bbox=GERMANY_BBOX, spacing_km: float = 40.0) -> np.ndarray:
    """Points (lat, lng) of a hexagonal lattice over bbox; rows are spacing*sqrt(3)/2 apart, every second row is
    shifted by half a spacing. Laid out in the same lat-51 projection as grid.py."""
    lat_min, lat_max, lng_min, lng_max = bbox
    x0, y0 = to_xy(lat_min, lng_min)
    x1, y1 = to_xy(lat_max, lng_max)
    row_h = spacing_km * math.sqrt(3) / 2
    xs, ys = [], []
    for j in range(math.ceil((y1 - y0) / row_h) + 1):
        shift = spacing_km / 2 if j % 2 else 0.0
        for i in range(math.ceil((x1 - x0) / spacing_km) + 1):
            xs.append(x0 + shift + i * spacing_km)
            ys.append(y0 + j * row_h)
    lat, lng = to_latlng(np.array(xs), np.array(ys))
    return np.column_stack([lat, lng])


def _dist(lat1, lng1, lat2, lng2) -> np.ndarray:
    """Distance matrix in km, shape (len(1), len(2))."""
    return haversine_km(
        np.asarray(lat1, float)[:, None], np.asarray(lng1, float)[:, None],
        np.asarray(lat2, float)[None, :], np.asarray(lng2, float)[None, :],
    )


def snap_to_points(lattice: np.ndarray, points: pd.DataFrame, snap_max_km: float) -> pd.DataFrame:
    """Each lattice point -> its nearest query point (PLZ centroid). Lattice points with none within snap_max_km
    are dropped; a PLZ chosen twice appears once. Returns the rows of `points` in lattice order."""
    d = _dist(lattice[:, 0], lattice[:, 1], points["lat"].to_numpy(), points["lng"].to_numpy())
    nearest = d.argmin(axis=1)
    keep = d[np.arange(len(lattice)), nearest] <= snap_max_km
    chosen = list(dict.fromkeys(int(i) for i in nearest[keep]))
    return points.iloc[chosen].reset_index(drop=True)


def plan_initial(points: pd.DataFrame, cfg: CoverConfig, bbox=GERMANY_BBOX) -> pd.DataFrame:
    """The planned query points: a hex lattice sized from cover_km, snapped to real PLZ."""
    return snap_to_points(hex_lattice(bbox, cfg.lattice_spacing_km), points, cfg.snap_max_km)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def centres_of(observations: Iterable[Observation]) -> np.ndarray:
    return np.array([o.centre for o in observations if o.centre is not None], dtype=float).reshape(-1, 2)


def uncovered_mask(points: pd.DataFrame, centres: np.ndarray, cover_km: float) -> np.ndarray:
    """Rule A: True for every query point farther than cover_km from all centres."""
    if len(centres) == 0:
        return np.ones(len(points), dtype=bool)
    out = np.empty(len(points), dtype=bool)
    lat, lng = points["lat"].to_numpy(), points["lng"].to_numpy()
    for start in range(0, len(points), 2000):  # chunked: 8,300 x 400 floats at once would be wasteful
        d = _dist(lat[start:start + 2000], lng[start:start + 2000], centres[:, 0], centres[:, 1])
        out[start:start + 2000] = d.min(axis=1) > cover_km
    return out


def fringe_sites(sites: pd.DataFrame, centres: np.ndarray, fringe_km: float) -> pd.DataFrame:
    """Rule B: located sites (columns id, lat, lng) with no centre within fringe_km."""
    if len(sites) == 0:
        return sites
    if len(centres) == 0:
        return sites
    d = _dist(sites["lat"].to_numpy(), sites["lng"].to_numpy(), centres[:, 0], centres[:, 1])
    return sites[d.min(axis=1) > fringe_km]


def appearance_misses(sites: pd.DataFrame, observations: list[Observation], consistency_km: float) -> list[dict]:
    """Rule C: for every located site (id, lat, lng) and every observation whose centre is within consistency_km
    of it, the site must be among the ids that observation returned. Returns one dict per miss."""
    misses = []
    if len(sites) == 0:
        return misses
    lat, lng, ids = sites["lat"].to_numpy(), sites["lng"].to_numpy(), sites["id"].to_numpy()
    for obs in observations:
        if obs.centre is None:
            continue
        d = haversine_km(obs.centre[0], obs.centre[1], lat, lng)
        for i in np.flatnonzero(d <= consistency_km):
            if ids[i] not in obs.ids:
                misses.append({"query": obs.key, "site_id": ids[i], "distance_km": round(float(d[i]), 2)})
    return misses


def cap_findings(observations: list[Observation], cfg: CoverConfig) -> list[str]:
    """Rule D: reasons to suspect a count cap (empty list = none)."""
    counts = [o.count for o in observations]
    findings = []
    if not counts:
        return findings
    top = max(counts)
    if top >= cfg.cap_fail_count:
        findings.append(f"a response has {top} records (>= {cfg.cap_fail_count})")
    repeats = counts.count(top)
    if top >= cfg.plateau_min_count and repeats >= cfg.plateau_repeats:
        findings.append(f"the largest count {top} occurs in {repeats} responses (cap signature)")
    return findings


def evaluate(
    points: pd.DataFrame,
    observations: list[Observation],
    sites: pd.DataFrame,
    cfg: CoverConfig,
    empty_zips: Iterable[str] = (),
) -> dict:
    """Rules A-D over everything collected so far.

    points: all regular query points (zip, lat, lng). sites: located sites (id, lat, lng).
    empty_zips: queried PLZ whose response was empty. An uncovered point that was itself queried and answered
    empty is reported as `assumed_empty` (a warning: probably nothing there, or the server does not know the PLZ),
    not as a rule A failure.
    """
    centres = centres_of(observations)
    unc = uncovered_mask(points, centres, cfg.cover_km)
    queried_empty = set(empty_zips)
    uncovered_zips = points.loc[unc, "zip"].tolist()
    assumed_empty = [z for z in uncovered_zips if z in queried_empty]
    a_fail = [z for z in uncovered_zips if z not in queried_empty]
    b = fringe_sites(sites, centres, cfg.fringe_km)
    c = appearance_misses(sites, observations, cfg.consistency_km)
    d = cap_findings(observations, cfg)
    return {
        "centres": int(len(centres)),
        "rule_a": {"ok": not a_fail, "uncovered": a_fail, "assumed_empty": assumed_empty},
        "rule_b": {"ok": len(b) == 0, "fringe_sites": b["id"].tolist() if len(b) else []},
        "rule_c": {"ok": not c, "misses": c},
        "rule_d": {"ok": not d, "findings": d},
    }


# --------------------------------------------------------------------------
# Fill-in loop (rules A and B)
# --------------------------------------------------------------------------

def _best_cover_candidate(points: pd.DataFrame, unc: np.ndarray, skip: set, cover_km: float) -> str | None:
    """The not yet queried PLZ whose own centroid would cover the most uncovered points."""
    cand = points.loc[unc & ~points["zip"].isin(skip).to_numpy()]
    if cand.empty:
        return None
    unc_pts = points.loc[unc]
    best, best_n = None, -1
    lat, lng = unc_pts["lat"].to_numpy(), unc_pts["lng"].to_numpy()
    for start in range(0, len(cand), 500):
        chunk = cand.iloc[start:start + 500]
        n = (_dist(chunk["lat"].to_numpy(), chunk["lng"].to_numpy(), lat, lng) <= cover_km).sum(axis=1)
        i = int(n.argmax())
        if n[i] > best_n:
            best, best_n = str(chunk.iloc[i]["zip"]), int(n[i])
    return best


def next_fill_queries(
    points: pd.DataFrame,
    observations: list[Observation],
    sites: pd.DataFrame,
    site_zips: dict,
    cfg: CoverConfig,
    queried: set,
) -> list[tuple[str, str]]:
    """The next batch of fill-in queries as (zip, reason): rule B (the own PLZ of each fringe site, all at once),
    else rule A (the one PLZ that covers the most uncovered points). Empty when nothing is left to do."""
    centres = centres_of(observations)
    batch: list[tuple[str, str]] = []
    for site_id in fringe_sites(sites, centres, cfg.fringe_km)["id"]:
        z = site_zips.get(site_id)
        if z and z not in queried and z not in {b[0] for b in batch}:
            batch.append((z, "fill_b"))
    if batch:
        return batch
    unc = uncovered_mask(points, centres, cfg.cover_km)
    if unc.any():
        z = _best_cover_candidate(points, unc, queried, cfg.cover_km)
        if z:
            return [(z, "fill_a")]
    return []


# --------------------------------------------------------------------------
# Variable radius: "the N nearest" finders (TÜV SÜD: 50 nearest, sorted by distance, no count field)
# --------------------------------------------------------------------------
#
# An answer lists the N nearest sites to the server's centre, strictly sorted by distance. It therefore proves
# "every site closer to the centre than the last listed one was listed": a disk of radius r_q = last distance -
# tie_margin_km around the centre. The radius differs per query (about 64 km around Munich, 200 km or more in the
# north). A query point t counts as covered when some disk reaches it with room to spare:
# dist(t, centre) <= r_q - cover_margin_km. The room is the distance between a site and the centroid of its own PLZ,
# so a site near a covered query point lies inside the disk and must have been listed.

@dataclass(frozen=True)
class NearestNConfig:
    """All numbers of the nearest-N coverage design in one place (defaults: TÜV SÜD, sources/tuev_sued.md §4-5)."""

    tie_margin_km: float = 0.5  # r_q = last distance - this (ties at the rim)
    cover_margin_km: float = 15.0  # M: a target is covered at dist <= r_q - M
    lattice_spacing_km: float = 60.0  # first pass: hex lattice, snapped to regular PLZ
    snap_max_km: float = 15.0
    prior_radius_km: float = 40.0  # effective radius assumed where no answered query is near (fill-in estimate)
    neighbours: int = 3  # the estimate is the smallest effective radius among this many nearest centres
    max_fill_per_pass: int = 60
    max_candidates: int = 2500  # bound on the fill-in candidates (an evenly thinned subset above this)


def proven_radius_km(last_distance_km: float | None, tie_margin_km: float) -> float:
    """r_q of one answer: 0 when nothing was returned."""
    if last_distance_km is None:
        return 0.0
    return max(0.0, float(last_distance_km) - tie_margin_km)


def _disks(observations: Iterable[Observation], margin_km: float) -> tuple[np.ndarray, np.ndarray]:
    """(centres, effective radii r_q - margin) of the answers that cover anything at all."""
    rows = [(o.centre, o.radius_km - margin_km) for o in observations
            if o.centre is not None and o.radius_km is not None and o.radius_km - margin_km > 0]
    if not rows:
        return np.empty((0, 2)), np.empty(0)
    return np.array([r[0] for r in rows], dtype=float), np.array([r[1] for r in rows], dtype=float)


def uncovered_mask_radii(points: pd.DataFrame, observations: Iterable[Observation], margin_km: float) -> np.ndarray:
    """True for every query point (zip, lat, lng) that no answer's disk reaches with `margin_km` to spare."""
    centres, eff = _disks(observations, margin_km)
    if len(centres) == 0:
        return np.ones(len(points), dtype=bool)
    out = np.empty(len(points), dtype=bool)
    lat, lng = points["lat"].to_numpy(), points["lng"].to_numpy()
    for start in range(0, len(points), 2000):
        d = _dist(lat[start:start + 2000], lng[start:start + 2000], centres[:, 0], centres[:, 1])
        out[start:start + 2000] = ~(d <= eff[None, :]).any(axis=1)
    return out


def radius_misses(sites: pd.DataFrame, observations: Iterable[Observation]) -> list[dict]:
    """Premise check for nearest-N answers: every located site (id, lat, lng) that lies within an answer's proven
    radius must be among the ids that answer listed. A miss means the finder is not returning the true nearest
    sites (a flaky server, a filter, an order that is not by distance) and the coverage proof does not hold."""
    misses = []
    if len(sites) == 0:
        return misses
    lat, lng, ids = sites["lat"].to_numpy(), sites["lng"].to_numpy(), sites["id"].to_numpy()
    for obs in observations:
        if obs.centre is None or not obs.radius_km:
            continue
        d = haversine_km(obs.centre[0], obs.centre[1], lat, lng)
        for i in np.flatnonzero(d <= obs.radius_km):
            if ids[i] not in obs.ids:
                misses.append({"query": obs.key, "site_id": ids[i], "distance_km": round(float(d[i]), 2)})
    return misses


def estimate_effective_radii(lat, lng, centres: np.ndarray, eff_radii: np.ndarray, neighbours: int, prior_km: float) -> np.ndarray:
    """Expected effective radius of a new query at each (lat, lng): the smallest effective radius among the
    `neighbours` nearest answered centres (the dense side wins, so the estimate errs towards too small), or the
    prior where there is no answer yet."""
    n = len(np.atleast_1d(lat))
    if len(centres) == 0:
        return np.full(n, prior_km)
    k = min(neighbours, len(centres))
    out = np.empty(n)
    lat, lng = np.atleast_1d(lat), np.atleast_1d(lng)
    for start in range(0, n, 1000):
        d = _dist(lat[start:start + 1000], lng[start:start + 1000], centres[:, 0], centres[:, 1])
        nearest = np.argsort(d, axis=1)[:, :k]
        out[start:start + 1000] = eff_radii[nearest].min(axis=1)
    return out


def plan_fill_radii(points: pd.DataFrame, observations: list[Observation], queried: set, cfg: NearestNConfig) -> list[tuple[str, str]]:
    """The next fill-in pass as (zip, "fill"), at most cfg.max_fill_per_pass, greedy: among the uncovered, not yet
    queried query points pick the one whose estimated disk covers the most still uncovered points, mark those as
    provisionally covered, repeat. A PLZ that was queried and whose own point is still uncovered is never picked again
    (its area has to be reached from elsewhere)."""
    unc = uncovered_mask_radii(points, observations, cfg.cover_margin_km)
    cand = points.loc[unc & ~points["zip"].isin(queried).to_numpy()]
    if cand.empty:
        return []
    if len(cand) > cfg.max_candidates:
        cand = cand.iloc[:: math.ceil(len(cand) / cfg.max_candidates)]
    centres, eff = _disks(observations, cfg.cover_margin_km)
    est = estimate_effective_radii(cand["lat"].to_numpy(), cand["lng"].to_numpy(), centres, eff, cfg.neighbours, cfg.prior_radius_km)
    unc_pts = points.loc[unc]
    cover = np.empty((len(cand), len(unc_pts)), dtype=bool)
    for start in range(0, len(cand), 250):
        chunk = cand.iloc[start:start + 250]
        d = _dist(chunk["lat"].to_numpy(), chunk["lng"].to_numpy(), unc_pts["lat"].to_numpy(), unc_pts["lng"].to_numpy())
        cover[start:start + 250] = d <= est[start:start + 250, None]
    left = np.ones(len(unc_pts), dtype=bool)
    taken = np.zeros(len(cand), dtype=bool)
    batch: list[tuple[str, str]] = []
    while len(batch) < cfg.max_fill_per_pass and left.any():
        gain = (cover & left[None, :]).sum(axis=1)
        gain[taken] = -1
        best = int(gain.argmax())
        if gain[best] <= 0:
            break
        taken[best] = True
        left &= ~cover[best]
        batch.append((str(cand.iloc[best]["zip"]), "fill"))
    return batch


# --------------------------------------------------------------------------
# Simulator (generic): fixed-radius finder with server-side geocoding offsets
# --------------------------------------------------------------------------

def _offset(lat: float, lng: float, dist_km: float, angle: float) -> tuple[float, float]:
    d_lat = dist_km * math.sin(angle) / 110.57
    d_lng = dist_km * math.cos(angle) / (111.32 * math.cos(math.radians(lat)))
    return lat + d_lat, lng + d_lng


class SimulatedRadiusFinder:
    """Fake fixed-radius finder over `sites` (columns id, lat, lng).

    The server geocodes each PLZ to its own centre: a deterministic offset from the GeoNames centroid, up to
    `centre_offset_km` (real KÜS: 0.6-5.2 km in the recon). Answers every site within the radius, sorted by distance.
    Test hooks: `cap` keeps only the N nearest sites; `drop(zip, site_id)` returning True removes a site from that
    response; `unknown_zips` answer with an empty list (the real server does that for a PLZ it does not know).
    """

    def __init__(
        self,
        sites: pd.DataFrame,
        radius_km: float = 50.0,
        centre_offset_km: float = 5.0,
        seed: int = 0,
        cap: int | None = None,
        drop: Callable[[str, str], bool] | None = None,
        unknown_zips: Iterable[str] = (),
    ):
        self.ids = sites["id"].astype(str).to_numpy()
        self.lat = sites["lat"].to_numpy(float)
        self.lng = sites["lng"].to_numpy(float)
        self.radius_km = radius_km
        self.centre_offset_km = centre_offset_km
        self.seed = seed
        self.cap = cap
        self.drop = drop
        self.unknown_zips = set(unknown_zips)
        self.calls: list[tuple[str, float]] = []

    def centre_for(self, zip_code: str, lat: float, lng: float) -> tuple[float, float]:
        rng = random.Random(f"{self.seed}:{zip_code}")
        return _offset(lat, lng, rng.uniform(0, self.centre_offset_km), rng.uniform(0, 2 * math.pi))

    def search(self, zip_code: str, lat: float, lng: float, radius_km: float | None = None):
        """(centre | None, [(site id, distance km), ...] nearest first)."""
        radius = min(radius_km or self.radius_km, 1e9)
        self.calls.append((zip_code, radius))
        if zip_code in self.unknown_zips:
            return None, []
        c_lat, c_lng = self.centre_for(zip_code, lat, lng)
        d = haversine_km(c_lat, c_lng, self.lat, self.lng)
        inside = np.flatnonzero(d <= radius)
        inside = inside[np.argsort(d[inside], kind="stable")]
        found = [(self.ids[i], float(d[i])) for i in inside if not (self.drop and self.drop(zip_code, self.ids[i]))]
        if self.cap is not None:
            found = found[: self.cap]
        return (c_lat, c_lng), found
