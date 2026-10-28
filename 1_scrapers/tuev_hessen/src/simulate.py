"""Offline fake of the TÜV Hessen locator, with known truth and optional injected faults (for `mode="simulated"` and tests).

`simulated_outcome()` returns a `FetchOutcome` shaped like the live one: 'all' (131-style mix, scaled down) and 'hu' answers,
UI and replay copies, counters and the captured request fields. Faults make the run fail in a known way:

  drop_hit          'Alle Standorte' loses one hit but total.value still says the original number
  gte_relation      hits.total.relation is 'gte'
  bad_counter       the UI counter on the button differs from len(hits.hits)
  blocked           the UI answer is a Cloudflare 'Just a moment' page (HTTP 403)
  empty             both answers are valid JSON with zero hits
  replay_differs    the replay answer holds one hit less than the UI answer
  changed_payload   the captured request carries an extra field
  non_json          the UI answer is HTML with HTTP 200
"""

from __future__ import annotations

import copy
import json

from tuev_hessen import fetch

FAULTS = ("drop_hit", "gte_relation", "bad_counter", "blocked", "empty", "replay_differs", "changed_payload", "non_json")

# (uid, title, tags, street, zip, city, lat, lng, phone, fax, hu)
SITES = [
    (1, "TÜV Hessen Service-Center Alsfeld", ["Kfz Prüfstelle", "TÜH Service-Center"], "Grünberger Straße 76", "36304", "Alsfeld", 50.7426, 9.2580, "06631 111111", "06631 111112", True),
    (2, "TÜV Hessen Service-Center Kassel", ["Kfz Prüfstelle"], "Wilhelmshöher Allee 1", "34117", "Kassel", 51.3127, 9.4797, "0561 222222", "", True),
    (3, "TÜV Hessen Partner Autohaus Beispiel Fulda", ["Kfz Prüfstelle", "Auto-Partner"], "Frankfurter Straße 5", "36037", "Fulda", 50.5558, 9.6808, "0661 333333", "", True),
    (4, "TÜV Hessen Partner KFZ Test Marburg", ["Kfz Prüfstelle", "Auto-Partner"], "Biegenstraße 2", "35037", "Marburg", 50.8019, 8.7667, "", "", True),
    (5, "TÜV SÜD Auto Partner GmbH Hanau", ["Kfz Prüfstelle", "TÜV SÜD Auto Partner"], "Nordstraße 9", "63450", "Hanau", 50.1264, 8.9283, "06181 444444", "", True),
    (6, "TÜV Hessen LKW Service-Center Friedberg Süd", ["Kfz Prüfstelle", "TP Preisliste"], "Industriestraße 3", "61169", "Friedberg", 50.3340, 8.7550, "06031 555555", "", True),
    (7, "TÜV Hessen Partner Werkstatt Bad Camberg", ["Kfz Prüfstelle", "Auto-Partner"], "Limburger Straße 8", "65520", "Bad Camberg", 50.2994, 8.2681, "06434 666666", "", True),
    (8, "TÜV Hessen Partner Garage Oberursel", ["Kfz Prüfstelle", "Auto-Partner"], "Hohemarkstraße 4", "61440", "Oberursel", 50.2005, 8.5775, "06171 777777", "", True),
    (9, "TÜV Hessen - MPU Hanau - Begutachtungsstelle für Fahreignung", ["MPU Begutachtung"], "Kurt-Blaum-Platz 1", "63450", "Hanau", 50.1306, 8.9170, "06181 888888", "", False),
    (10, "TÜV Hessen Darmstadt", ["Managementsysteme"], "Rüdesheimer Straße 119", "64295", "Darmstadt", 49.8728, 8.6512, "06151 999999", "", False),
]


def _page_html(uid, street, zip_, city, lat, lng, phone, fax, hu, hours) -> tuple[str, str]:
    tel = f'<br><i class="fa fa-phone fa-fw" title="Telefon"></i>&nbsp; <a href="tel:+49{uid}">{phone}</a>' if phone else ""
    faxline = f'<br><i class="fa fa-fax fa-fw" title="Fax"></i>&nbsp; {fax}' if fax else ""
    address = f"<address>{street}<br>{zip_} {city}<br>{tel}{faxline}</address>"
    route = f'<p><a data-etracker-type="GoogleMapsDirections" href="https://maps.google.de/maps/dir//{lat},{lng}">Route hierher</a></p>'
    preview = f'<div class="location-result">{address}{route}</div>'
    booking = (f'<a data-etracker-type="Terminvereinbarung HU" href="/286/hu-wunschtermin-zur-hauptuntersuchung/?locationSign={700 + uid}">'
               f'<span class="action-icon"><i class="fa fa-calendar"></i></span></a>') if hu else ""
    map_preview = (f'<div class="location-result"><div class="row"><div class="col-sm-4">{address}{route}</div><div class="col-sm-5">'
                   f"<h4>Öffnungszeiten</h4><p><span class=\"period\">{hours}</span></p></div>"
                   f'<div class="location-actions">{booking}</div></div></div>')
    return preview, map_preview


def make_hit(site: tuple, shared_coords_with: tuple | None = None) -> dict:
    uid, title, tags, street, zip_, city, lat, lng, phone, fax, hu = site
    if shared_coords_with:
        lat, lng = shared_coords_with
    preview, map_preview = _page_html(uid, street, zip_, city, lat, lng, phone, fax, hu, "MO - FR: 8:00 - 17:00 Uhr<br />SA: 8:00 - 13:00 Uhr")
    infotext = (f"\r\nAn unserem Service-Center in {city}&nbsp;erhalten Sie folgende Dienstleistungen:\r\nHauptuntersuchung (HU) &amp;&nbsp;Abgasuntersuchung (AU)"
                if hu else "\r\nMPU Beratung und Begutachtung")
    source = {
        "uid": uid, "pid": 12, "title": title, "street": street, "zip": zip_, "city": city,
        "infotext": infotext, "latitude": str(lat), "longitude": str(lng), "infotext_contents": None, "tags": [
            {"uid": i + 1, "pid": 12, "title": t} for i, t in enumerate(tags)],
        "searchable_meta": {"preview": preview, "mapPreview": map_preview, "renderedLink": f"/287/standort/site-{uid}/", "linkTitle": title},
        "searchable_score_factor": 1.4 if hu else 1, "coordinates": {"lat": str(lat), "lon": str(lng)},
    }
    if phone:
        source["phone"] = phone
    if hu:
        source["is_service_center"] = "1"
    return {"_index": "german_website_locations", "_type": "_doc", "_id": str(uid), "_score": None, "sort": [city, 1.4], "_source": source}


def make_payload(hits: list[dict], total: int | None = None, relation: str = "eq") -> dict:
    return {"took": 4, "timed_out": False, "_shards": {"total": 2, "successful": 2, "skipped": 0, "failed": 0},
            "hits": {"total": {"value": len(hits) if total is None else total, "relation": relation}, "max_score": None, "hits": hits}}


def _capture(key: str, payload: dict, counter: str | None, extra_field: bool = False) -> fetch.Capture:
    tag_value, label = fetch.DATASETS[key]
    cap = fetch.Capture(dataset=key, tag_value=tag_value, ui_label=label)
    cap.request_method, cap.request_url = "POST", fetch.ENDPOINT_URL
    cap.request_content_type = "multipart/form-data; boundary=<…>"
    cap.request_fields = [("language", "0")] + [("fixedTags[]", t) for t in fetch.EXPECTED_FIXED_TAGS] + [("address", ""), ("tags[]", tag_value)]
    if extra_field:
        cap.request_fields.append(("size", "10"))
    cap.request_body_bytes = 680
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    cap.ui_status, cap.ui_content_type, cap.ui_body = 200, "application/json; charset=utf-8", body
    cap.ui_counter = counter if counter is not None else str(len(payload["hits"]["hits"]))
    cap.replay_status, cap.replay_content_type, cap.replay_body = 200, "application/json; charset=utf-8", body
    return cap


def simulated_outcome(fault: str | None = None, with_detail: bool = True) -> fetch.FetchOutcome:
    """The fake locator. `fault` (one of FAULTS) injects a known failure."""
    if fault is not None and fault not in FAULTS:
        raise ValueError(f"fault must be one of {FAULTS}")
    # uid 8 shares the coordinates of uid 7 (a copy-paste error, like the real Bad Camberg / Oberursel case)
    hits_all = [make_hit(s, shared_coords_with=(50.2994, 8.2681) if s[0] == 8 else None) for s in SITES]
    hits_hu = [h for h, s in zip(hits_all, SITES) if s[-1]]
    all_payload, hu_payload = make_payload(copy.deepcopy(hits_all)), make_payload(copy.deepcopy(hits_hu))
    if fault == "drop_hit":
        all_payload = make_payload(hits_all[:-1], total=len(hits_all))
    elif fault == "gte_relation":
        all_payload = make_payload(hits_all, relation="gte")
    elif fault == "empty":
        all_payload, hu_payload = make_payload([]), make_payload([])
    out = fetch.FetchOutcome()
    out.access = {"headless": True, "browser": "simulated", "consent": "declined (simulated)", "blocked": False, "cookie_names": ["cf_clearance"]}
    out.form_state = {"select_options": [["", "Alle Standorte"], ["Kfz Prüfstelle", "Kfz Prüfstelle"], ["MPU Begutachtung", "MPU Begutachtung"]],
                      "hidden": [["language", "0"]] + [["fixedTags[]", t] for t in fetch.EXPECTED_FIXED_TAGS], "button_text": "Standorte finden"}
    cap_all = _capture("all", all_payload, "999" if fault == "bad_counter" else None, extra_field=fault == "changed_payload")
    cap_hu = _capture("hu", hu_payload, None)
    if fault == "replay_differs":
        cap_all.replay_body = json.dumps(make_payload(hits_all[:-1]), ensure_ascii=False).encode("utf-8")
    if fault == "blocked":
        cap_all.ui_status, cap_all.ui_content_type = 403, "text/html"
        cap_all.ui_body = b"<html><title>Just a moment...</title></html>"
        cap_all.replay_body = b""
    if fault == "non_json":
        cap_all.ui_content_type, cap_all.ui_body = "text/html", b"<html><body>maintenance</body></html>"
    out.captures = {"all": cap_all, "hu": cap_hu}
    for what in ("page", "search_ui:all", "replay:all", "search_ui:hu", "replay:hu"):
        out.log.append({"time": "2026-01-01T00:00:00", "what": what, "url": fetch.LOCATOR_URL, "status": 200, "seconds": 0.1, "note": "simulated"})
    if with_detail:
        out.detail_pages = [
            {"url": f"{fetch.BASE_URL}/287/standort/site-{uid}/", "status": 200,
             "html": "<html><head><title>Detail</title></head><body><main><h1>Standort</h1><h2>Öffnungszeiten</h2><p>MO - FR: 8:00 - 17:00 Uhr</p></main></body></html>"}
            for uid in (1, 3)]
    return out
