"""Offline stand-in for the KÜS API: synthetic sites over Germany behind the same interface as fetch.KuesClient.

Used by run(mode="simulated"), the tests and the notebook's "expected request count". No real data: names, phones and
emails are fake. The finder part (fixed radius, server centres offset from the GeoNames centroids) is the generic
core.radius_cover.SimulatedRadiusFinder.
"""

from __future__ import annotations

import math
import random

import numpy as np
import pandas as pd

from core import radius_cover as rc
from core.geo import haversine_km
from core.grid import BIG_CITIES
from kues import fetch

CENTRE_OFFSET_KM = 6.0  # recon: the real server centres lie 0.6-5.2 km from the GeoNames centroids
SITE_PLZ_JITTER_KM = 9.0  # a site lies up to this far from the centroid of its own PLZ (recon max: 12.8 km)
TECHNIK_SHARE = 0.25  # share of sites that are also in the technik list (recon: 23 of 83, 3 of 27)
SIM_CLUSTERS = {**BIG_CITIES, "Dortmund": (51.5136, 7.4653), "Stuttgart": (48.7758, 9.1829), "Saarbrücken": (49.2402, 6.9969)}


def simulated_sites(points: pd.DataFrame, n_total: int = 1500, per_cluster: int = 40, seed: int = 0) -> pd.DataFrame:
    """Fake sites: `per_cluster` around each big city, the rest scattered around random PLZ centroids.
    Columns: id (adressid as text), lat, lng (true position), plz, city, plus the record attributes."""
    rng = random.Random(seed)
    spots = []
    for lat, lng in SIM_CLUSTERS.values():
        for _ in range(per_cluster):
            spots.append(rc._offset(lat, lng, 10 * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)))
    while len(spots) < n_total:
        base = points.iloc[rng.randrange(len(points))]
        spots.append(rc._offset(base["lat"], base["lng"], SITE_PLZ_JITTER_KM * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)))
    lat = np.array([s[0] for s in spots])
    lng = np.array([s[1] for s in spots])
    d = rc._dist(lat, lng, points["lat"].to_numpy(), points["lng"].to_numpy())
    own = d.argmin(axis=1)
    sites = pd.DataFrame(
        {
            "id": [str(10000 + i) for i in range(len(spots))],
            "lat": lat,
            "lng": lng,
            "plz": points["zip"].to_numpy()[own],
            "city": points["city"].to_numpy()[own],
        }
    )
    attrs = [_attributes(i, rng) for i in range(len(sites))]
    return pd.concat([sites, pd.DataFrame(attrs)], axis=1)


def _attributes(i: int, rng: random.Random) -> dict:
    kind = rng.random()
    vorort = "1" if rng.random() < 0.4 else "0"
    no_street = rng.random() < 0.06
    if kind < 0.12:  # person-named site: the salutation is in `name`
        name, addon = rng.choice(["Herrn", "Herrn Dipl.-Ing. (FH)", "Frau"]), f"Max Muster{i}"
    elif kind < 0.30:
        name, addon = f"Ingenieurbüro Test{i}", None
    elif kind < 0.50:
        name, addon = f"KÜS-Prüfstelle Testort{i}", "Kfz-Prüfstelle"
    else:
        name, addon = f"SV-Büro Beispiel{i}", None
    return {
        "licensee": f"I{rng.randint(1, 400):04d}",  # about 400 licence codes: some have several sites
        "name": name,
        "zusatz": addon,
        "vorort": vorort,
        "keinestrasse": "1" if no_street else "0",
        "in_technik": rng.random() < TECHNIK_SHARE,
        "street": f"Teststraße {rng.randint(1, 99)}",
    }


def make_record(site, query_zip: str, centre: tuple[float, float], dist_km: float) -> dict:
    """One API record in the real response shape for this site, as seen from `centre`."""
    no_street = site["keinestrasse"] == "1"
    return {
        "adressid": int(site["id"]),
        "lizenznehmer": site["licensee"],
        "name": site["name"],
        "zusatz": None if pd.isna(site["zusatz"]) else site["zusatz"],  # a DataFrame turns None into NaN
        "plz": site["plz"],
        "ort": site["city"],
        "strasse": None if no_street else site["street"],
        "tel1": "0000-000000",
        "fax": None,
        "email": f"test{site['id']}@example.invalid",
        "http": None,
        "lat": None if no_street else f"{site['lat']:.12f}",
        "lon": None if no_street else f"{site['lng']:.12f}",
        "inbreite": f"{centre[0]:.6f}",
        "inlaenge": f"{centre[1]:.6f}",
        "pruefungvorortmoeglich": site["vorort"],
        "keinestrasse": site["keinestrasse"],
        "entfernung": f"{dist_km:.13f}",
        "plzorder": 1 if site["plz"] == query_zip else 2,
    }


class FakeKuesApi:
    """Same interface as fetch.KuesClient: get(list_name, plz, radius_km) -> (records, info).

    cap / drop / unknown_zips are the failure hooks of the generic finder (see core.radius_cover.SimulatedRadiusFinder);
    cap and drop apply to both lists.
    """

    def __init__(
        self,
        sites: pd.DataFrame,
        points: pd.DataFrame,
        radius_km: float = 50.0,
        centre_offset_km: float = CENTRE_OFFSET_KM,
        seed: int = 0,
        cap: int | None = None,
        drop=None,
        unknown_zips=(),
        max_requests: int | None = None,
    ):
        self.sites = sites.set_index("id", drop=False)
        self.points = points.set_index("zip")
        kwargs = dict(radius_km=radius_km, centre_offset_km=centre_offset_km, seed=seed, cap=cap, drop=drop, unknown_zips=unknown_zips)
        self.finders = {
            fetch.LIST_KUES: rc.SimulatedRadiusFinder(sites, **kwargs),
            fetch.LIST_TECHNIK: rc.SimulatedRadiusFinder(sites[sites["in_technik"]], **kwargs),
        }
        self.radius_km = radius_km
        self.max_requests = max_requests
        self.requests_made = 0

    def get(self, list_name: str, plz: str, radius_km: int | None = None):
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise fetch.RequestLimitReached(f"request limit of {self.max_requests} reached")
        self.requests_made += 1
        if plz not in self.points.index:  # like the real server: a PLZ it does not know gets an empty list
            return [], {"http_status": 200, "bytes": 2, "elapsed_s": 0.0}
        p = self.points.loc[plz]
        radius = self.radius_km if radius_km is None else min(radius_km, fetch.RADIUS_CAP_KM)
        centre, found = self.finders[list_name].search(plz, float(p["lat"]), float(p["lng"]), radius)
        records = [make_record(self.sites.loc[i], plz, centre, d) for i, d in found]
        return records, {"http_status": 200, "bytes": 400 * len(records) + 2, "elapsed_s": 0.0}
