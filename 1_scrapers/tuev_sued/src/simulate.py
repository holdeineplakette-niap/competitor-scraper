"""Offline stand-in for the TÜV SÜD finder and for the browser pass (sources/tuev_sued.md §9).

Used by run(mode="simulated"), the tests and the notebook's "expected passes". No real data: names, phones and
emails are fake. The finder part ("the 50 nearest to the server's centre", centre = PLZ centroid + a random offset
of 0-5 km) is the generic core.radius_cover.SimulatedRadiusFinder; this module only adds the real answer shape and
a Python version of the console snippet, so that the whole multi-pass flow (plan -> download -> check -> build) runs
through the same import code as a real pass.
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from core import radius_cover as rc
from tuev_sued import browser, parse

CENTRE_OFFSET_KM = 5.0
CITY_RADIUS_KM = 28.0  # sites scatter around a seed city, denser in the middle
SITE_PLZ_MAX_KM = 8.0  # a simulated site lies at most this far from a PLZ centroid (real maximum seen at KÜS: 12.8 km)
# (name, lat, lng, number of sites): dense in Bayern and Baden-Württemberg, some in Hessen and Sachsen, a few in the north
SEEDS = [
    ("München", 48.1374, 11.5755, 58), ("Nürnberg", 49.4521, 11.0767, 28), ("Augsburg", 48.3705, 10.8978, 16),
    ("Regensburg", 49.0134, 12.1016, 14), ("Würzburg", 49.7913, 9.9534, 12), ("Ingolstadt", 48.7651, 11.4237, 10),
    ("Rosenheim", 47.8561, 12.1289, 8), ("Passau", 48.5667, 13.4319, 6), ("Bamberg", 49.8988, 10.9028, 6),
    ("Kempten", 47.7267, 10.3139, 6), ("Hof", 50.3167, 11.9167, 4), ("Hammelburg", 50.1167, 9.9, 3),
    ("Stuttgart", 48.7758, 9.1829, 28), ("Karlsruhe", 49.0069, 8.4037, 13), ("Mannheim", 49.4875, 8.466, 8),
    ("Freiburg", 47.999, 7.8421, 10), ("Ulm", 48.4011, 9.9876, 10), ("Heilbronn", 49.1427, 9.2109, 8),
    ("Konstanz", 47.6779, 9.1732, 6), ("Tübingen", 48.5216, 9.0576, 5), ("Villingen", 48.0603, 8.4586, 4),
    ("Frankfurt", 50.1109, 8.6821, 12), ("Kassel", 51.3127, 9.4797, 6), ("Wiesbaden", 50.0782, 8.2398, 4), ("Darmstadt", 49.8728, 8.6512, 5),
    ("Dresden", 51.0504, 13.7373, 8), ("Leipzig", 51.3397, 12.3731, 8), ("Chemnitz", 50.8278, 12.9214, 6),
    ("Hamburg", 53.5511, 9.9937, 7), ("Bremen", 53.0793, 8.8017, 3), ("Hannover", 52.3759, 9.732, 6), ("Berlin", 52.52, 13.405, 8),
    ("Kiel", 54.3233, 10.1228, 2), ("Rostock", 54.0924, 12.0991, 2), ("Köln", 50.9375, 6.9603, 6), ("Dortmund", 51.5136, 7.4653, 3),
    ("Wolnzach", 48.6, 11.6, 1),
]
WOLNZACH_NOTICE = "Prüfstelle der Überwachungsorganisation – Leistungen und Entgelte weichen ab"


def simulated_sites(points: pd.DataFrame, seed: int = 0, extra: list[tuple[str, float, float]] | None = None) -> pd.DataFrame:
    """About 350 fake Service-Center. Columns: id (tscId as text), lat, lng (true position), zip, town (of the nearest PLZ
    centroid) plus the record attributes. `extra` adds sites at exact positions: (id, lat, lng)."""
    rng = random.Random(seed)
    spots = []
    p_lat, p_lng = points["lat"].to_numpy(), points["lng"].to_numpy()
    for _, lat, lng, n in SEEDS:
        for _ in range(n):
            while True:  # real sites are built-up places, never in a lake or far from any PLZ centroid
                spot = rc._offset(lat, lng, CITY_RADIUS_KM * rng.random() ** 1.5, rng.uniform(0, 2 * math.pi))
                if rc._dist([spot[0]], [spot[1]], p_lat, p_lng).min() <= SITE_PLZ_MAX_KM:
                    break
            spots.append(spot)
    lat = np.array([s[0] for s in spots] + [e[1] for e in extra or []])
    lng = np.array([s[1] for s in spots] + [e[2] for e in extra or []])
    d = rc._dist(lat, lng, points["lat"].to_numpy(), points["lng"].to_numpy())
    own = d.argmin(axis=1)
    ids = [str(20000 + i) for i in range(len(spots))] + [e[0] for e in extra or []]
    frame = pd.DataFrame({"id": ids, "lat": lat, "lng": lng, "zip": points["zip"].to_numpy()[own], "town": points["city"].to_numpy()[own]})
    attrs = [_attributes(i, rng) for i in range(len(frame))]
    attrs[len(spots) - 1]["notice"] = WOLNZACH_NOTICE  # the last seed city (Wolnzach)
    return pd.concat([frame, pd.DataFrame(attrs)], axis=1)


def _attributes(i: int, rng: random.Random) -> dict:
    return {
        "sign": str(rng.choice([8, 1, 5])).zfill(3) if i % 7 == 0 else str(100 + i),  # some with leading zeros ("008")
        "street": f"Teststraße {rng.randint(1, 99)}",
        "huau": 0 if rng.random() < 0.02 else 1,
        "status": 0 if rng.random() < 0.02 else 1,
        "company": "1255511964821267510075" if rng.random() < 0.4 else "500",  # partner-operated sites are ~40 % in the real finder
        "truck": rng.random() < 0.4,
        "notice": None,
    }


def make_item(site, dist_km: float) -> dict:
    """One `finaltsc` item for this site in the shape of the HU finder answer (field names as in scraper_status.md)."""
    location = {
        "tscId": site["id"], "locationSign": site["sign"], "asproId": site["sign"], "tscStatus": int(site["status"]),
        "companyId": site["company"], "marketAreaId": 7, "federalStateId": 111, "countryId": parse.GERMANY_COUNTRY_ID,
        "street": site["street"].rsplit(" ", 1)[0], "streetnumber": site["street"].rsplit(" ", 1)[1], "zipcode": site["zip"],
        "town": site["town"], "townPart": site["town"], "name": "",
        "lat": f"{site['lat']:.6f}", "lon": f"{site['lng']:.6f}", "tscUrl": f"europe/germany/{site['town'].lower()}/{site['id']}",
        "huau": int(site["huau"]), "carService": 1, "truckService": int(bool(site["truck"])), "trailer_biggest": 2 if site["truck"] else 0,
        "sga": 1, "dkgk": 0, "oldtimer": 1,
        "phone": "0000 000000", "fax": "", "directcall": "", "email": "ksc@example.invalid", "emailtsc": f"tsc{site['id']}@example.invalid",
        "notificationText": site["notice"] or "", "notificationText_start": 0, "notificationText_end": 0,
        "notificationText2": "Abgelaufener Hinweis", "notificationText_start2": 1567029600, "notificationText_end2": 1567029700,
        "currentlyClosed": 0, "currentlyClosedMsg": "",
    }
    times = [{"dayId": d, "morningFrom": "07:30", "morningTo": "12:00", "afternoonFrom": "13:00", "afternoonTo": "16:00", "isSpecialTime": 0} for d in range(1, 6)]
    return {"tscId": site["id"], "distance": f"{dist_km:.3f}", "sortdistance": round(dist_km, 3), "times": times, "location": location, "images": [], "currentlyclosed": 0}


class FakeTuevApi:
    """The finder: query(plz) -> the answer JSON (dict) of the real shape.

    cap: number of records returned (real: 50). radius_limit_km: a hypothetical radius limit (default none).
    drop: drop(plz, site_id) returning True removes the site from that answer. centre_offset_km: the server geocodes each
    PLZ to its own centre, up to this far from the GeoNames centroid.
    """

    def __init__(self, sites: pd.DataFrame, points: pd.DataFrame, cap: int = 50, radius_limit_km: float | None = None,
                 centre_offset_km: float = CENTRE_OFFSET_KM, seed: int = 0, drop=None):
        self.sites = sites.set_index("id", drop=False)
        self.points = points.set_index("zip")
        self.finder = rc.SimulatedRadiusFinder(sites, radius_km=radius_limit_km or 1e9, centre_offset_km=centre_offset_km, seed=seed, cap=cap, drop=drop)
        self.requests_made = 0

    def query(self, plz: str) -> dict:
        self.requests_made += 1
        p = self.points.loc[plz]
        centre, found = self.finder.search(plz, float(p["lat"]), float(p["lng"]))
        return {
            "status": 1, "errorId": 0, "errormsg": "", "locationname": p["city"], "zipcode": plz, "zipcodes": [plz],
            "lat": round(centre[0], 6), "lon": round(centre[1], 6),
            "finaltsc": [make_item(self.sites.loc[i], d) for i, d in found], "geodata": {},
        }


def browser_download(plan: browser.PassPlan, api: FakeTuevApi, started: datetime, *, drop_answers=(), stop_after: int | None = None,
                     stopped_reason: dict | None = None) -> str:
    """The text the snippet would download for this pass (answers + browser_summary line), asked of the fake finder.

    drop_answers: PLZ whose answer is lost (not in the file, not counted as done). stop_after: the pass stops after this many
    answers with `stopped_reason` (default a user stop).
    """
    lines, done, stopped = [], 0, None
    for seq, (plz, _) in enumerate(plan.queries, 1):
        if stop_after is not None and done >= stop_after:
            stopped = stopped_reason or {"reason": "user_stop", "next_plz": plz}
            break
        if plz in set(drop_answers):
            continue
        name = parse.answer_name(plan.pass_no, seq, plz)
        lines.append(json.dumps({"file": name, "response": api.query(plz)}, ensure_ascii=False))
        done += 1
    summary = {
        "pass": plan.pass_no, "planned": len(plan.queries), "done": done, "started": started.isoformat(),
        "finished": (started + timedelta(seconds=7 * len(plan.queries))).isoformat(), "stopped_reason": stopped,
        "snippet_version": plan.snippet_version,
    }
    lines.append(json.dumps({"file": browser.SUMMARY_FILE, "response": summary}))
    return "\n".join(lines) + "\n"
