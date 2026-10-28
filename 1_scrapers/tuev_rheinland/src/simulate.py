"""Offline fake of tuv.com for tuev_rheinland.run: the finder JSON and the three landing pages, with known truth.

The truth mirrors the real source of 2026-10-06 in shape: own stations (pool DE) and FSP partners (pool FSP), foreign and
non-VIS records that must not be exported, a Saarland page with 24 rows and the text "24 TÜV Rheinland Prüfstellen", of
which three are not plain VIS matches: Völklingen (finder record 45371 without address, filled), Losheim (added) and
Wadern-Lockweiler (known, unresolved). Faults can be injected to prove that a run does not pass.
"""

from __future__ import annotations

import json

from tuev_rheinland import adapter
from tuev_rheinland.adapter import FetchError

# 21 own Saarland stations that the Saarland page lists and the JSON has as VIS (street, PLZ, city)
SAARLAND_VIS = [(f"Prüfweg {i}", f"66{100 + i * 7:03d}", f"Saarort {i}") for i in range(1, 22)]
OTHER_OWN = [(f"Hauptstraße {i}", f"5{i:04d}", f"Rheinort {i}") for i in range(1, 9)]  # own stations elsewhere
FSP = [(f"Werkstattweg {i}", f"4{i:04d}", f"Fspstadt {i}") for i in range(1, 7)]
BERLIN = [("Berliner Allee 1", "10115", "Berlin"), ("Spreeweg 2", "10117", "Berlin")]
PFALZ = [("Weinstraße 3", "67433", "Neustadt"), ("Domplatz 4", "67346", "Speyer")]
VOELKLINGEN = ("Saarwiesenstraße 7", "66333", "Völklingen")  # FILL_FROM_LANDING: finder record 45371 has no address
LOSHEIM = ("Saarbrücker Straße 221", "66679", "Losheim am See")  # ADD_FROM_LANDING: not in the finder JSON
WADERN = ("Mittelbachweg 1", "66687", "Wadern")  # known unmatched landing row
NEW_LANDING = ("Neue Straße 9", "66780", "Rehlingen")  # fault "new_landing_row"

# in-scope truth of the clean simulation: every own/FSP VIS + the Losheim row added from the landing page
EXPECTED_IN_SCOPE = len(SAARLAND_VIS) + len(OTHER_OWN) + len(FSP) + len(BERLIN) + len(PFALZ) + 1 + 1  # + 45371 + Losheim

FAULTS = ("json_403", "not_json", "missing_field", "saarland_23", "saarland_page_down", "berlin_page_down",
          "new_landing_row", "losheim_in_json", "duplicate_id", "drop_half")


def _rec(id_, title, street, zip_, city, vic, ext, typ="VIS", lat=49.3, lng=7.0) -> dict:
    address = f"<p>{street}<br/>{zip_} {city}</p>" if street else ""
    return {"id": id_, "title": title, "type": typ, "address": address, "lat": lat, "long": lng, "phone": "+49 221 0000",
            "opening_times": [{"0": ["Mo.-Fr.", "08:00-17:00"]}], "county": "", "vic_id": vic, "external_db": ext,
            "appointment_available": "true", "appointment_link": f"/termin/{id_}", "detail_url": f"/standort/{id_}",
            "directions_link": "", "email_link": "", "headquarter": False, "external": False, "company": "TÜV Rheinland"}


def records(fault: str | None = None) -> list[dict]:
    recs, n = [], 43000
    own = SAARLAND_VIS + OTHER_OWN + BERLIN + PFALZ
    if fault == "drop_half":
        own = own[: len(own) // 2]
    for street, zip_, city in own:
        n += 1
        recs.append(_rec(n, f"TÜV Rheinland Prüfstelle {city}", street, zip_, city, f"{n}_de-DE", "tuev"))
    for street, zip_, city in FSP:
        n += 1
        recs.append(_rec(n, f"FSP Prüfstelle {city}", street, zip_, city, f"{n}_de-FSP", "fsp", lat=51.0, lng=7.5))
    recs.append(_rec(45371, "TÜV Rheinland Prüfstelle Völklingen Saarwiesenstraße", "", "", "", "45371_de-DE", "tuev"))
    if fault == "losheim_in_json":
        recs.append(_rec(45999, "TÜV Rheinland Prüfstelle Losheim", *LOSHEIM, "45999_de-DE", "tuev"))
    # must not be exported: a foreign VIS, a German non-VIS record
    recs.append(_rec(90001, "TÜV Rheinland Luxembourg", "Rue 1", "5326", "Contern", "90001_lu-LU", "", lat=49.6, lng=6.2))
    recs.append(_rec(90002, "TÜV Rheinland Akademie", "Am Grauen Stein 1", "51105", "Köln", "", "", typ="TEC", lat=50.9, lng=7.0))
    if fault == "missing_field":
        for r in recs:
            r.pop("vic_id")
    if fault == "duplicate_id":
        recs.append(dict(recs[0]))
    return recs


def landing_html(region: str, fault: str | None = None) -> str:
    if region == "saarland":
        rows = [(f"Saarort {i}", s) for i, s in enumerate(SAARLAND_VIS, 1)]
        rows += [("Völklingen", VOELKLINGEN), ("Wadern-Lockweiler", WADERN), ("Losheim", LOSHEIM)]
        if fault == "saarland_23":
            rows = rows[:-4] + rows[-3:]
        if fault == "new_landing_row":
            rows = rows[:-1] + [("Rehlingen", NEW_LANDING)]  # still 24 rows: Losheim is replaced by an unknown station
        claim = "24 TÜV Rheinland Prüfstellen"
    else:
        rows = [(c, (s, z, c)) for s, z, c in (BERLIN if region == "berlin" else PFALZ)]
        claim = f"{len(rows)} Prüfstellen"
    body = "".join(f"<tr><td>{name}</td><td>{s}, {z} {c}</td><td><a href='/lp/{z}'>Website</a></td></tr>" for name, (s, z, c) in rows)
    return f"<html><body><p>Im Saarland und Umgebung: {claim}.</p><table><tr><th>Standort</th></tr>{body}</table></body></html>"


class SimulatedTuv:
    """The client interface of tuev_rheinland.run (get_json, get_page) without a request."""

    def __init__(self, fault: str | None = None):
        if fault is not None and fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}; choose from {FAULTS}")
        self.fault = fault
        self.requests_made = 0

    def get_json(self, url: str) -> tuple[object, bytes]:
        self.requests_made += 1
        if self.fault == "json_403":
            raise FetchError("HTTP 403 (content-type text/html); stopping, no bypass")
        if self.fault == "not_json":
            raise FetchError("not JSON (content-type text/html, starts '<html>'); stopping, no bypass")
        content = json.dumps(records(self.fault), ensure_ascii=False).encode("utf-8")
        return json.loads(content), content

    def get_page(self, url: str) -> bytes:
        self.requests_made += 1
        region = next(r for r, u in adapter.LANDING_PAGES.items() if u == url)
        if self.fault == f"{region}_page_down":
            raise FetchError(f"HTTP 503 for {url}; stopping, no bypass")
        return landing_html(region, self.fault).encode("utf-8")
