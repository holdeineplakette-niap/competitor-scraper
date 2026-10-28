"""A fake tuev-nord.de with known truth, for the `simulated` run mode and the tests (no network).

`simulated_outcome(fault)` returns a `FetchOutcome` as the live fetch would. `fault` injects one error the run must catch:
  counter_mismatch  the page counter says one more than the records
  filtered_page     the page lacks every UNG-only record (a filtered view) and says so in its counter
  no_map            the map element is missing (page redesign)
  empty_locations   data-locations is an empty list
  bad_json          data-locations is not JSON
  unknown_type      a record with an unknown serviceType
  missing_field     a record without `plz`
  list_missing_half the A-Z list lacks half of the stations
  blocked           a WAF page instead of the site (raises BlockedError)
"""

from __future__ import annotations

import html
import json

from tuev_nord import fetch
from tuev_nord.parse import BASE

FAULTS = ("counter_mismatch", "filtered_page", "no_map", "empty_locations", "bad_json", "unknown_type", "missing_field", "list_missing_half", "blocked")

# id, name, street, plz, city, lat, lng, serviceType, slug, isOffice
SITES = [
    (101, "Alpha", "Hauptstr. 1", "30159", "Hannover", 52.3745, 9.7386, "HU-UNG", "alpha", False),
    (102, "Bravo", "Ring 2", "20095", "Hamburg", 53.5503, 10.0006, "HU", "bravo", False),
    (103, "Charlie (Ingenieurbüro Muster)", "Weg 3", "28195", "Bremen", 53.0793, 8.8017, "HU-UNG", "charlie-ingenieurbuero-muster", False),
    (104, "Delta (Auto GmbH)", "Allee 4", "24103", "Kiel", 54.3233, 10.1228, "HU", "delta-auto-gmbh", False),
    (105, "Echo, Ingenieurbüro Beispiel", "Platz 5", "18055", "Rostock", 54.0924, 12.0991, "HU-UNG", "echo-ingenieurbuero-beispiel", False),
    (106, "Foxtrot (Wambel)", "Gasse 6", "44135", "Dortmund", 51.5136, 7.4653, "HU-UNG", "foxtrot-wambel", False),
    (107, "Halle (Saale)", "Markt 7", "06108", "Halle (Saale)", 51.4825, 11.9697, "HU", "halle-saale", False),
    (108, "Golf", "Damm 8", "38100", "Braunschweig", 52.2689, 10.5268, "HU-UNG", "golf", False),
    (109, "Hotel", "Straße 9", "49074", "Osnabrück", 52.2799, 8.0472, "UNG", "hotel", False),
    (110, "India", "Weg 10", "26122", "Oldenburg", 53.1435, 8.2146, "HU", "india", False),
    (201, "Anna Beispiel", "Büroweg 11", "10115", "Berlin", 52.5322, 13.3849, "UNG", "", True),
    (202, "Berta Muster", "Büroweg 12", "80331", "München", 48.1374, 11.5755, "UNG", "", True),
    (202, "Berta Muster", "Neuer Weg 1", "90402", "Nürnberg", 49.4539, 11.0775, "UNG", "", True),  # id reused for another address
    (203, "Alpha Gutachter", "Hauptstr. 1", "30159", "Hannover", 52.3745, 9.7386, "UNG", "", True),  # office at the address of a station
]
SITES += [(300 + i, f"Musterort {i}", f"Teststr. {i}", f"{31000 + i}", f"Musterort {i}", round(52.0 + 0.01 * i, 4), round(9.0 + 0.01 * i, 4),
           "HU" if i % 2 else "HU-UNG", f"musterort-{i}", False) for i in range(60)]  # enough stations that the two list-only pages stay below 5 %
TRUTH = {"records": len(SITES), "hu": sum(1 for s in SITES if s[7] in ("HU", "HU-UNG")), "offices": sum(1 for s in SITES if s[9]),
         "stations": sum(1 for s in SITES if not s[9]), "unclassified": ["Foxtrot (Wambel)"],
         "partner": 3, "own_probable": 6 + 60, "list_only": ["only-in-list", "special-page"]}


def record(s) -> dict:
    sid, name, street, plz, city, lat, lng, service, slug, office = s
    link = f"/de/stationen/kontakt-aufnehmen/?token=tok{sid}x{plz}&cHash=h{sid}" if office else f"/de/stationen/{slug}/"
    return {"id": sid, "name": name, "street": street, "plz": plz, "city": city, "latitude": lat, "longitude": lng,
            "serviceType": service, "link": link, "isOffice": office}


def stations_html(fault: str | None = None) -> str:
    records = [record(s) for s in SITES]
    counter = len(records)
    if fault == "filtered_page":
        records = [r for r in records if r["serviceType"] != "UNG"]
        counter = len(records)
    if fault == "unknown_type":
        records[0]["serviceType"] = "XYZ"
    if fault == "missing_field":
        del records[1]["plz"]
    if fault == "counter_mismatch":
        counter += 1
    data = json.dumps(records, ensure_ascii=False)
    if fault == "empty_locations":
        data = "[]"
    if fault == "bad_json":
        data = "[{not json"
    attr = html.escape(data, quote=True)
    map_div = "" if fault == "no_map" else (f'<div class="h-full" data-station-map="station-results" data-lat="52.3" data-lng="9.7" '
                                            f'data-locations="{attr}"></div>')
    return f"<html><head><title>TÜV NORD Stationen</title></head><body><main>{map_div}<div> {counter} Stationen gefunden </div></main></body></html>"


def list_html(fault: str | None = None) -> str:
    stations = [s for s in SITES if not s[9]]
    if fault == "list_missing_half":
        stations = stations[: len(stations) // 2]
    items = [(s[1], f"/de/stationen/{s[8]}/") for s in stations] + [("Only In List", "/de/stationen/only-in-list/"),
                                                                   ("Special Page", "/de/stationen/stationsseiten/special-page/")]
    blocks: dict[str, list] = {}
    for name, href in sorted(items):
        blocks.setdefault(name[0].upper(), []).append(f'<a data-list-teaser="" href="{href}"><span>{html.escape(name)}</span></a>')
    body = "".join(f'<div data-anchor-target="{k}"><span>{k}</span><div>{"".join(v)}</div></div>' for k, v in blocks.items())
    return f'<html><head><title>Liste aller Stationen | TÜV NORD</title></head><body><a href="/de/stationen/">TÜV NORD Stationen</a>{body}</body></html>'


def detail_html(s) -> str:
    sid, name, street, plz, city, lat, lng, service, slug, office = s
    hu = ("<h3>Hauptuntersuchung (HU) inkl. Abgasuntersuchung (AU)</h3>" if service in ("HU", "HU-UNG") else "<h3>TÜV NORD Schadengutachten</h3>")
    partner = "<h3>TÜV NORD-Partner</h3><p>Die TÜV NORD Station gehört als Partnerunternehmen zu X.</p>" if "(" in name else ""
    coords = html.escape(json.dumps([{"name": name, "coords": {"lat": lat, "lng": lng}}]), quote=True)
    return (f'<html><head><title>TÜV NORD Station {name} | TÜV NORD</title><link rel="canonical" href="{BASE}/de/stationen/{slug}/"/></head><body><main>'
            f"<h1>{html.escape(name)}</h1><p>{street}</p><p>{plz} {city}</p><h2>Unsere Öffnungszeiten</h2><dl><dt>Montag–Freitag</dt><dd>09:00–17:00</dd></dl>"
            f"<!-- TYPO3SEARCH_begin --><h2>Unsere Leistungen</h2><h3>Haupt-Dienstleistungen</h3>{hu}<h3>Serviceangebot</h3>{partner}"
            f'<h2>Kontakt</h2><a href="/de/stationen/termin-vereinbaren/?stationId={sid}&cHash=x">Jetzt Termin buchen</a>'
            f'<a href="tel:0800 80 70 600">Tel</a><a href="mailto:info@example.invalid">Mail</a>'
            f'<div data-googlemap-locations="{coords}"></div></main></body></html>').replace("<!-- TYPO3SEARCH_begin -->", "<p>TYPO3SEARCH_begin</p>")


def _resp(url: str, text: str, status: int = 200) -> fetch.Response:
    return fetch.Response(url=url, status=status, text=text, content=text.encode("utf-8"), elapsed_s=0.0, attempts=1,
                          content_type="text/html; charset=utf-8", set_cookie_names=["NSC_SIM"], server="SERVER")


def simulated_outcome(fault: str | None = None) -> fetch.FetchOutcome:
    if fault is not None and fault not in FAULTS:
        raise ValueError(f"unknown fault {fault!r}; choose from {FAULTS}")
    out = fetch.FetchOutcome()
    out.access = {"method": "simulated (no network)", "playwright": False, "cookies_accepted": False, "set_cookie_names": ["NSC_SIM"], "server_header": "SERVER"}
    if fault == "blocked":
        exc = fetch.BlockedError(f"{fetch.STATIONS_URL} was blocked (HTTP 403; 'Access Denied' in the page)")
        exc.outcome = out
        raise exc
    out.stations = _resp(fetch.STATIONS_URL, stations_html(fault))
    out.list_page = _resp(fetch.LIST_URL, list_html(fault))
    stations = [s for s in SITES if not s[9]]
    pick = [("own station", stations[0]), ("partner station", stations[2]), ("HU-only", stations[1])]
    for role, s in pick:
        out.details.append({"role": role, "url": f"{BASE}/de/stationen/{s[8]}/", "status": 200, "html": detail_html(s), "elapsed_s": 0.0})
    out.extra.append({"role": "only in list page", "url": f"{BASE}/de/stationen/only-in-list/", "status": 200,
                      "html": detail_html((999, "Only In List", "Weg 1", "30159", "Hannover", 52.3745, 9.7386, "HU", "only-in-list", False)), "elapsed_s": 0.0})
    out.log = [{"time": "00:00:00", "what": "simulated", "url": fetch.STATIONS_URL, "status": 200, "seconds": 0, "note": "no network"}]
    out.requests_made = 0
    return out
