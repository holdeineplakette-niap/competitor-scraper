"""Offline stand-in for the HDP location API (simulated mode and tests). It has the client interface of `fetch.HdpClient`."""

from __future__ import annotations

import json

from hdp.fetch import URL, Response

_SITES = [  # (id, publicName, street, number, postcode, city, bundesland, lat, lng)
    ("Alpha", "Alpha", "Musterweg", "12", "44143", "Dortmund", "NRW", 51.5205, 7.5159),
    ("Beta", "Beta-Stadt", "Beispielstraße", "7a", "90431", "Nürnberg", "Bayern", 49.4518, 11.0150),
    ("Gamma", None, "Teststr.", "3", "93051", "Regensburg", "Bayern", 49.0109, 12.0688),
    ("Delta", "Delta", "Probeallee", "101", "60386", "Frankfurt", "Hessen", 50.1300, 8.7459),
]


def sample_location(i: int = 0, **override) -> dict:
    sid, name, street, number, plz, city, state, lat, lng = _SITES[i % len(_SITES)]
    loc = {"id": sid, "location": sid, "publicName": name, "slugs": [sid.lower()], "address": f"{street} {number}, {plz} {city}",
           "latitude": lat, "longitude": lng, "city": city, "bundesland": state, "street": street, "number": number,
           "postcode": plz, "mapsUrl": "https://maps.app.goo.gl/x",
           "openingHours": {"monday": {"isOpen": True, "open": "09:00", "close": "17:00"}, "saturday": {"isOpen": False}},
           "disabledServiceIds": [], "disabledServiceTypes": None, "slotAvailability": {"availableCount": 5}}
    loc.update(override)
    return loc


def sample_payload(n: int = 4) -> dict:
    return {"result": {"success": True, "locations": [sample_location(i) for i in range(n)]}}


class SimulatedHdp:
    """Answers with `payload` (default: 4 sites). Faults are injected by passing another payload, a status or an exception."""

    def __init__(self, payload: dict | None = None, status: int = 200, error: Exception | None = None):
        self.payload = sample_payload() if payload is None else payload
        self.status, self.error = status, error
        self.requests_made = 0

    def post_locations(self) -> Response:
        self.requests_made += 1
        if self.error is not None:
            raise self.error
        return Response(url=URL, status=self.status, text=json.dumps(self.payload, ensure_ascii=False), elapsed_s=0.0, attempts=1,
                        content_type="application/json")
