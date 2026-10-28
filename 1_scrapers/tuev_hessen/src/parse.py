"""Parse and classify the Elasticsearch hits of the TÜV Hessen locator. Pure functions, no network.

One hit = `hits.hits[]`: `_id`, `_index`, `_score`, `sort`, optional `_ignored`, and `_source` with the site fields. Every
field of the hit is kept in the output row (the raw JSON stays untouched in `raw.jsonl`); the row adds parsed and derived
columns. Nothing is dropped and nothing is fixed: doubts are flags.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

SLUG = "tuev_hessen"
BASE_URL = "https://www.tuev-hessen.de"
LISTED_BY = "TÜV Hessen"

# fields of `_source` the parser knows. Anything else lands in `unknown_source_fields` (schema drift is reported).
KNOWN_SOURCE_KEYS = {
    "uid", "pid", "title", "street", "zip", "city", "latitude", "longitude", "coordinates", "phone", "infotext",
    "infotext_contents", "is_service_center", "tags", "searchable_meta", "searchable_score_factor",
}
KNOWN_META_KEYS = {"preview", "mapPreview", "renderedLink", "linkTitle"}
KNOWN_HIT_KEYS = {"_index", "_type", "_id", "_score", "_source", "sort", "_ignored"}

HU_TAG = "Kfz Prüfstelle"
OWN_TAG = "TÜH Service-Center"
PARTNER_TAG = "Auto-Partner"
SUED_TAG = "TÜV SÜD Auto Partner"

# (title prefix, site_type, operator); matched on the whitespace-normalised, case-folded title, first match wins
TITLE_RULES = [
    ("tüv hessen service-center", "own_station", "TÜV Hessen"),
    ("tüv hessen partner", "partner", "Partner"),
    ("tüv süd auto partner gmbh", "tuev_sued_auto_partner", "TÜV SÜD Auto Partner"),
]
EXPECTED_TAG = {"own_station": OWN_TAG, "partner": PARTNER_TAG, "tuev_sued_auto_partner": SUED_TAG}
SITE_TYPE_TAGS = {OWN_TAG: "own_station", PARTNER_TAG: "partner", SUED_TAG: "tuev_sued_auto_partner"}

HU_TEXT = re.compile(r"Hauptuntersuch|Haupt-\s*und\s*Abgasuntersuch|§\s*29\s*StVZO|\bHU\b", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')]+", re.I)
# best effort: "Betreiber: <name>" up to the next sentence of the standard text; the raw text stays in infotext_text
BETREIBER_RE = re.compile(r"Betreiber:\s*(.+?)(?=\s+(?:Vertiefende|Weitere Informationen|An unserem|Am TÜV|Haupt|Abgas|Fahrzeug)|\s\*|$)")
GERMANY_BOX = (47.2, 55.1, 5.8, 15.1)  # lat min, lat max, lng min, lng max


def norm_ws(text) -> str:
    """Unescape, replace non-breaking spaces, collapse whitespace."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(str(text)).replace("\xa0", " ")).strip()


def html_text(markup, sep: str = " ") -> str:
    if not markup:
        return ""
    return norm_ws(BeautifulSoup(markup, "html.parser").get_text(sep))


def classify_title(title: str) -> tuple[str, str]:
    """(site_type, operator) from the title; anything unexpected is ('unclassified', '')."""
    low = norm_ws(title).casefold()
    for prefix, site_type, operator in TITLE_RULES:
        if low.startswith(prefix):
            return site_type, operator
    return "unclassified", ""


def title_rest(title: str, site_type: str) -> str:
    """The title without its type prefix (partner or place name), for reading only."""
    if site_type == "unclassified":
        return ""
    norm = norm_ws(title)
    for prefix, st, _ in TITLE_RULES:
        if st == site_type and norm.casefold().startswith(prefix):
            return norm[len(prefix):].strip(" -–")
    return ""


def tag_title_mismatch(site_type: str, tags: list[str]) -> list[str]:
    """Reasons the tags and the title disagree (reported, never fixed)."""
    reasons = []
    expected = EXPECTED_TAG.get(site_type)
    if expected and expected not in tags:
        reasons.append(f"{site_type} without tag '{expected}'")
    for tag, st in SITE_TYPE_TAGS.items():
        if tag in tags and st != site_type:
            reasons.append(f"tag '{tag}' on a {site_type} title")
    return reasons


def _tel_lines(soup) -> dict:
    """Phone / mobile / fax from the preview address block, keyed by the icon in front of the number."""
    out = {"phone": "", "mobile": "", "fax": ""}
    for icon, key in (("fa-phone", "phone"), ("fa-mobile-phone", "mobile"), ("fa-fax", "fax")):
        i = soup.select_one(f"i.{icon}")
        if i is None:
            continue
        parts = []  # the number follows the icon up to the next <br>: a tel: link, or plain text (fax)
        for sib in i.next_siblings:
            if getattr(sib, "name", None) == "br":
                break
            parts.append(sib.get_text(" ") if hasattr(sib, "get_text") else str(sib))
        out[key] = norm_ws(" ".join(parts))
    return out


def parse_previews(preview_html: str, map_html: str) -> dict:
    """Fax, mobile, opening hours, booking link, contact link ... from `searchable_meta.preview` / `mapPreview`."""
    pv = BeautifulSoup(preview_html or "", "html.parser")
    mp = BeautifulSoup(map_html or "", "html.parser")
    tel = _tel_lines(pv)
    hours_blocks = []
    for span in mp.select("span.period"):
        lines = [norm_ws(x) for x in span.get_text("\n").split("\n")]
        hours_blocks.append("; ".join(x for x in lines if x))
    booking = mp.select_one("a[data-etracker-type='Terminvereinbarung HU']") or next(
        (a for a in mp.find_all("a") if "wunschtermin" in a.get("href", "")), None)
    booking_href = booking.get("href", "") if booking else ""
    sign = (parse_qs(urlparse(booking_href).query).get("locationSign") or [""])[0]
    contact = next((a for a in mp.find_all("a") if re.search(r"/kontakt", a.get("href", ""))), None)
    route = pv.select_one("a[data-etracker-type='GoogleMapsDirections']")
    return {
        "phone_preview": tel["phone"], "mobile": tel["mobile"], "fax": tel["fax"],
        "opening_hours": " | ".join(hours_blocks), "has_opening_hours": bool(hours_blocks),
        "booking_url": urljoin(BASE_URL, booking_href) if booking_href else "",
        "location_sign": sign,
        "contact_url": urljoin(BASE_URL, contact.get("href", "")) if contact else "",
        "route_url": route.get("href", "") if route else "",
        "map_text": norm_ws(mp.get_text(" ")),
    }


def _walk_text(node, keys=("header", "subheader", "bodytext")) -> list[str]:
    """Text of the notice records in `infotext_contents` (a list that can hold dicts, lists, or empty parts)."""
    if isinstance(node, dict):
        return [t for k in keys if (t := html_text(node.get(k)))]
    if isinstance(node, list):
        return [t for item in node for t in _walk_text(item, keys)]
    return []


def _num(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_hit(hit: dict, scraped_at: str = "") -> dict:
    """One flat row for one hit. Raises KeyError/TypeError only for a hit without `_source` or uid."""
    s = hit["_source"]
    meta = s.get("searchable_meta") or {}
    tags = [t.get("title", "") if isinstance(t, dict) else str(t) for t in (s.get("tags") or [])]
    title = norm_ws(s.get("title"))
    site_type, operator = classify_title(title)
    pv = parse_previews(meta.get("preview", ""), meta.get("mapPreview", ""))
    infotext_text = html_text(s.get("infotext"))
    notices = _walk_text(s.get("infotext_contents"))
    mismatch = tag_title_mismatch(site_type, tags)
    is_hu = HU_TAG in tags
    hu_evidence = [name for name, found in (
        ("infotext", bool(HU_TEXT.search(infotext_text))),
        ("booking_link_hu", bool(pv["booking_url"])),
        ("map_preview_text", bool(HU_TEXT.search(pv["map_text"]))),
        ("notices", any(HU_TEXT.search(n) for n in notices))) if found]
    websites = list(dict.fromkeys(u.rstrip(".,;*") for u in URL_RE.findall(infotext_text)))
    betreiber = BETREIBER_RE.search(infotext_text)
    lat, lng = _num(s.get("latitude")), _num(s.get("longitude"))
    coords =s.get("coordinates") if isinstance(s.get("coordinates"), dict) else {}
    link = meta.get("renderedLink", "")
    phone = (s.get("phone") or "").strip()
    unknown = sorted((set(s) - KNOWN_SOURCE_KEYS) | {f"searchable_meta.{k}" for k in set(meta) - KNOWN_META_KEYS}
                     | {f"hit.{k}" for k in set(hit) - KNOWN_HIT_KEYS})
    return {
        "location_id": f"{SLUG.upper()}-{s['uid']}", "source": SLUG, "source_id": str(s["uid"]), "scraped_at": scraped_at,
        "title": title, "title_rest": title_rest(title, site_type),
        "site_type": site_type, "operator": operator, "listed_by": LISTED_BY,
        "is_hu_site": is_hu, "is_lkw_site": bool(re.search(r"\bLKW\b", title, re.I)),
        "tags": "; ".join(tags),
        "tag_title_mismatch": bool(mismatch), "tag_title_mismatch_reason": "; ".join(mismatch),
        "street": norm_ws(s.get("street")), "zip": str(s.get("zip") or "").strip(), "city": norm_ws(s.get("city")),
        "latitude": lat, "longitude": lng,
        "phone": phone, "mobile": pv["mobile"], "fax": pv["fax"],
        "opening_hours": pv["opening_hours"], "booking_url": pv["booking_url"], "location_sign": pv["location_sign"],
        "contact_url": pv["contact_url"],
        "websites": "; ".join(websites), "betreiber_text": betreiber.group(1).strip() if betreiber else "",
        "detail_url": urljoin(BASE_URL, link) if link else "",
        "hu_evidence": "; ".join(hu_evidence),
        "notices": " | ".join(notices),
        "infotext_text": infotext_text,
        "uid": s["uid"], "pid": s.get("pid"), "es_id": hit.get("_id"), "es_index": hit.get("_index"),
        "is_service_center": s.get("is_service_center"), "score_factor": s.get("searchable_score_factor"),
        "sort_values": json.dumps(hit.get("sort"), ensure_ascii=False), "es_ignored": json.dumps(hit.get("_ignored"), ensure_ascii=False) if "_ignored" in hit else "",
        "tags_json": json.dumps(s.get("tags"), ensure_ascii=False),
        "unknown_source_fields": "; ".join(unknown),
        # kept for the checks, not exported
        "_phone_preview": pv["phone_preview"], "_coordinates_dict": coords, "_link": link,
    }


def parse_hits(hits: list[dict], scraped_at: str = "") -> list[dict]:
    return [parse_hit(h, scraped_at) for h in hits]


def describe_detail(html: str) -> dict:
    """What a detail page offers, for the report only (the pages are not crawled by the run)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript"]):
        if t.get("type") != "application/ld+json":
            t.decompose()
    ld = []
    for t in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(t.string or "")
            ld += [str(d.get("@type")) for d in (data if isinstance(data, list) else [data]) if isinstance(d, dict)]
        except ValueError:
            ld.append("invalid json")
    main = soup.select_one("main") or soup.body or soup
    text = norm_ws(main.get_text(" "))
    metas = {m.get("name") or m.get("property"): (m.get("content") or "")[:120] for m in soup.find_all("meta") if (m.get("name") or m.get("property"))}
    hours = [norm_ws(s.get_text(" ")) for s in main.select("span.period, .opening-hours, table.opening-hours")]
    return {
        "title": norm_ws(soup.title.get_text()) if soup.title else "",
        "headings": [f"{h.name}: {norm_ws(h.get_text())[:80]}" for h in main.find_all(["h1", "h2", "h3", "h4"])][:30],
        "has_opening_hours_heading": "Öffnungszeiten" in text, "opening_hours_blocks": len(hours),
        "text_chars": len(text), "json_ld_types": ld,
        "tel_links": len(main.select("a[href^='tel:']")), "mailto_links": len(main.select("a[href^='mailto:']")),
        "contact_links": len([a for a in main.find_all("a") if "/kontakt" in a.get("href", "")]),
        "booking_links": len([a for a in main.find_all("a") if "wunschtermin" in a.get("href", "")]),
        "images": len(main.find_all("img")), "address_blocks": len(main.find_all("address")),
        "google_rating_text": (re.search(r"\d[,.]\d\s*/\s*\d\s*Durchschnittliche Bewertung bei Google", text) or [""])[0],
        "betreiber_line": "Betreiber:" in text,
        "meta": {k: metas[k] for k in ("description", "og:title", "og:description", "robots") if k in metas},
        "mentions": {w: (w.lower() in text.lower()) for w in ("Hauptuntersuchung", "Abgasuntersuchung", "Termin", "Preise", "Checkliste", "Bargeldlos", "Barrierefrei", "Parkplatz")},
    }


def normalise_address(street: str, zip_: str, city: str = "") -> str:
    """A comparison key: lower case, no punctuation, 'straße' = 'str.' = 'strasse'."""
    s = norm_ws(street).casefold()
    s = s.replace("ß", "ss").replace("straße", "str").replace("strasse", "str").replace("str.", "str")
    s = re.sub(r"[^0-9a-zäöü]+", "", s)
    return f"{s}|{str(zip_).strip()}"
