"""Parse vuek-gmbh.de pages (Jimdo CMS, static HTML). Pure functions, no network.

Every value that parsing can lose information on is kept twice: the parsed value and the raw text. Text the parser does
not recognise is not dropped: it goes to `unparsed_lines` and becomes a flag.
"""

from __future__ import annotations

import copy
import hashlib
import html as html_lib
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup
from fsp.parse import decode_cf_email  # the Cloudflare e-mail decoder is shared with the FSP adapter

BASE = "https://www.vuek-gmbh.de"
HOST = "vuek-gmbh.de"
KONTAKT_PATH = "/kontakt/"
GERMANY_BOX = (47.2, 55.1, 5.8, 15.1)  # lat_min, lat_max, lng_min, lng_max

_WS = re.compile(r"\s+")
_INVISIBLE = dict.fromkeys([0x00AD, 0x200B, 0x200C, 0x200D, 0xFEFF], None)  # soft hyphen, zero-width characters

NUMBER_WORDS = {
    "ein": 1, "eine": 1, "einen": 1, "einem": 1, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12,
}
_STATED = re.compile(
    r"(?<![\w-])(\d{1,2}|[a-zäöüß]+)\s+eigene[nr]?\s+(?:Standort\w*|Pr(?:ü|ue)fstelle\w*)", re.IGNORECASE
)

_RED = re.compile(r"color\s*:\s*(?:#[c-f][0-9a-f]0000\b|#f00\b|red\b|rgb\(\s*2\d\d\s*,\s*0\s*,\s*0\s*\))", re.IGNORECASE)
_PLZ_LINE = re.compile(r"^(\d{5})\s+(\S.*)$")
_PHONE_LINE = re.compile(r"^(?:Tel(?:efon)?|Fon)\b\.?\s*:?\s*(.*)$", re.IGNORECASE)
_FAX_LINE = re.compile(r"^(?:Tele)?fax\b\.?\s*:?\s*(.*)$", re.IGNORECASE)
_MOBILE_LINE = re.compile(r"^(?:Mobil|Handy|Mobiltelefon)\b\.?\s*:?\s*(.*)$", re.IGNORECASE)
_MAIL_LINE = re.compile(r"^(?:E-?Mail|Mail)\b\.?\s*:?\s*(.*)$", re.IGNORECASE)
_HOURS_HEADING = re.compile(r"^Öffnungszeiten\b", re.IGNORECASE)

_DAY = r"(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag|Mo|Di|Mi|Do|Fr|Sa|So)"
_DAY_LABEL = re.compile(rf"^({_DAY})\.?(?:\s*(?:bis|-|–|—)\s*({_DAY})\.?)?\s*:?$", re.IGNORECASE)
_TIME_RANGE = re.compile(
    r"(\d{1,2})[.:](\d{2})\s*(?:bis|-|–|—)\s*(\d{1,2})[.:](\d{2})\s*(?:Uhr)?(?:\s*\(([^)]*)\))?", re.IGNORECASE
)
_CLOSED = re.compile(r"^(?:ganztägig\s+)?geschlossen$", re.IGNORECASE)
_DAY_ABBR = {"mo": "Mo", "di": "Di", "mi": "Mi", "do": "Do", "fr": "Fr", "sa": "Sa", "so": "So"}

SERVICE_PATTERNS = [
    ("Hauptuntersuchung (HU)", re.compile(r"hauptuntersuchung", re.IGNORECASE)),
    ("Änderungsabnahme", re.compile(r"fahrzeug\s*änderung|änderungsabnahme", re.IGNORECASE)),
    ("Weitere Fahrzeugprüfungen", re.compile(r"weitere\s+fahrzeug\s*prüfung", re.IGNORECASE)),
]


def clean(text: str | None) -> str:
    """Collapse whitespace (incl. non-breaking spaces), drop soft hyphens and zero-width characters."""
    return _WS.sub(" ", (text or "").translate(_INVISIBLE)).strip()


# ------------------------------------------------------------------------------------------------ links and paths
def norm_path(href: str | None) -> str | None:
    """'/kontakt/pruefstelle-lage' or 'https://www.vuek-gmbh.de/kontakt/pruefstelle-lage/?x' -> '/kontakt/pruefstelle-lage/'.

    None for other hosts, mailto:/tel:/javascript: links and empty or '#' links."""
    if not href or not href.strip() or href.strip().startswith("#"):
        return None
    p = urlparse(href.strip())
    if p.scheme and p.scheme not in ("http", "https"):
        return None
    if p.netloc and p.netloc.lower().removeprefix("www.") != HOST:
        return None
    path = unquote(p.path).lower()
    if not path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return path if path.endswith("/") else path + "/"


def is_station_path(path: str) -> bool:
    """A station page is anything under /kontakt/ (except the hub itself) or anything with 'pruefstelle' in its path."""
    return "pruefstelle" in path or (path.startswith(KONTAKT_PATH) and path != KONTAKT_PATH)


def station_path(href: str | None) -> str | None:
    path = norm_path(href)
    return path if path and is_station_path(path) else None


def slug_of(path: str) -> str:
    """'/kontakt/pruefstelle-bad-oeynhausen/' -> 'bad-oeynhausen'."""
    last = path.strip("/").split("/")[-1]
    return last.removeprefix("pruefstelle-") or last


def station_url(path: str) -> str:
    return f"{BASE}{path}"


def _soup(html_text: str) -> BeautifulSoup:
    return BeautifulSoup(html_text, "html.parser")


def _content(soup: BeautifulSoup):
    return soup.select_one("div#content_area")


def _station_paths(links) -> set[str]:
    return {p for a in links if (p := station_path(a.get("href")))}


# ------------------------------------------------------------------------------------------------ stated count
def parse_number(token: str | None) -> int | None:
    token = (token or "").strip().lower()
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def stated_counts(text: str) -> list[dict]:
    """Every 'fünf eigenen Standorten'-style statement in the text: {'raw', 'token', 'value'} (value None = unreadable)."""
    return [{"raw": clean(m.group(0)), "token": m.group(1), "value": parse_number(m.group(1))}
            for m in _STATED.finditer(clean(text))]


# ------------------------------------------------------------------------------------------------ /ueber-uns/
def parse_about(html_text: str) -> dict:
    """Station links in the menu (per navigation container), the footer block 'VÜK Standorte' and the body text, plus
    the stated number of own stations (meta description and body text) and the sentence about partner workshops."""
    soup = _soup(html_text)
    out: dict = {"title": clean(soup.title.get_text()) if soup.title else ""}
    meta = soup.find("meta", attrs={"name": "description"})
    out["meta_description"] = clean(meta.get("content")) if meta else ""
    out["stated_meta"] = stated_counts(out["meta_description"])

    menus = []
    for i, nav in enumerate(soup.select('div[data-container="navigation"]')):
        links = nav.find_all("a", href=True)
        if any(norm_path(a["href"]) == KONTAKT_PATH for a in links):  # a container that holds the Kontakt menu
            menus.append({"container": i, "stations": sorted(_station_paths(links))})
    out["menus"] = menus
    out["menu_set"] = sorted({p for m in menus for p in m["stations"]})

    footer_blocks = [s for s in soup.find_all(["strong", "b", "h2", "h3", "h4", "p"])
                     if re.fullmatch(r"(?:VÜK\s+)?Standorte", clean(s.get_text()), re.IGNORECASE)]
    footer_modules = []
    for s in footer_blocks:
        module = s.find_parent("div", class_="j-module")
        if module is not None and module not in footer_modules:
            footer_modules.append(module)
    out["footer_blocks"] = len(footer_modules)
    out["footer_set"] = sorted({p for m in footer_modules for p in _station_paths(m.find_all("a", href=True))})

    content = _content(soup)
    out["content_found"] = content is not None
    body_links = content.find_all("a", href=True) if content is not None else []
    out["body_set"] = sorted(_station_paths(body_links))
    body_text = clean(content.get_text(" ")) if content is not None else ""
    out["stated_body"] = stated_counts(body_text)
    m = re.search(r"[^.]*Partnerwerkst[^.]*\.", body_text)
    out["partner_workshop_text"] = clean(m.group(0)) if m else ""
    return out


# ------------------------------------------------------------------------------------------------ sitemaps, hubs
def parse_sitemap_xml(xml_text: str) -> list[str]:
    """All <loc> URLs of a sitemap urlset. Raises ET.ParseError on non-XML input and ValueError on a sitemap index."""
    root = ET.fromstring(xml_text)
    if root.tag.endswith("sitemapindex"):
        raise ValueError("sitemap index (nested sitemaps) is not handled: stop and look")
    return [clean(el.text) for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and clean(el.text)]


def station_paths_of_urls(urls) -> list[str]:
    return sorted({p for u in urls if (p := station_path(u))})


def parse_link_page(html_text: str) -> dict:
    """The HTML sitemap and the Kontakt hub: every internal link in the page content and the station links among them."""
    soup = _soup(html_text)
    content = _content(soup)
    links = content.find_all("a", href=True) if content is not None else []
    paths = sorted({p for a in links if (p := norm_path(a["href"]))})
    return {"content_found": content is not None, "paths": paths, "station_set": sorted(p for p in paths if is_station_path(p))}


def parse_hu_page(html_text: str) -> dict:
    soup = _soup(html_text)
    content = _content(soup)
    text = clean(content.get_text(" ")) if content is not None else ""
    return {"content_found": content is not None, "text": text,
            "mentions_hu": bool(re.search(r"hauptuntersuchung", text, re.IGNORECASE))}


# ------------------------------------------------------------------------------------------------ opening hours
def _day_abbr(word: str) -> str:
    return _DAY_ABBR[word.lower()[:2]]


def normalize_hours(lines: list[str]) -> tuple[str, list[str]]:
    """('Mo-Fr 09:00-12:00, 12:30-17:00; Sa 08:00-12:00', unparsed_lines). The text is rebuilt only from what was understood;
    anything else is returned in unparsed_lines so the caller keeps the raw text and flags the row."""
    groups: list[list] = []
    unparsed: list[str] = []
    for line in lines:
        m = _DAY_LABEL.match(line)
        if m:
            label = _day_abbr(m.group(1)) + (f"-{_day_abbr(m.group(2))}" if m.group(2) else "")
            groups.append([label, []])
            continue
        if _CLOSED.match(line) and groups:
            groups[-1][1].append("geschlossen")
            continue
        times = list(_TIME_RANGE.finditer(line))
        leftover = _TIME_RANGE.sub("", line)
        leftover = re.sub(r"[\s,;&/+]|\bund\b", "", leftover, flags=re.IGNORECASE)
        valid = times and groups and not leftover and all(
            int(t.group(1)) <= 24 and int(t.group(3)) <= 24 and int(t.group(2)) < 60 and int(t.group(4)) < 60 for t in times)
        if not valid:
            unparsed.append(line)
            continue
        for t in times:
            rng = f"{int(t.group(1)):02d}:{t.group(2)}-{int(t.group(3)):02d}:{t.group(4)}"
            groups[-1][1].append(rng + (f" ({clean(t.group(5))})" if t.group(5) else ""))
    for label, ranges in groups:
        if not ranges:
            unparsed.append(label)  # a day label without any time: information would be lost
    text = "; ".join(f"{label} {', '.join(ranges)}" for label, ranges in groups if ranges)
    return text, unparsed


# ------------------------------------------------------------------------------------------------ a station page
def _block_lines(block) -> list[str]:
    return [t for t in (clean(x) for x in block.get_text("").split("\n")) if t]


def _is_red(tag) -> bool:
    return bool(_RED.search(tag.get("style") or ""))


def _email_of_link(a) -> tuple[str | None, str | None]:
    """(address, problem) of one link: Cloudflare-obfuscated (data-cfemail or '#hex' in the href) or mailto:."""
    href = a.get("href", "")
    hex_value = a.get("data-cfemail") or (href.split("email-protection#", 1)[1] if "email-protection#" in href else "")
    try:
        if hex_value:
            return decode_cf_email(hex_value), None
        if href.lower().startswith("mailto:"):
            return clean(unquote(href[7:].split("?")[0])), None
        if "email-protection" in href or "__cf_email__" in (a.get("class") or []):
            return None, "an obfuscated e-mail link without hex data"
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"e-mail decode failed: {exc}"
    return None, None


def _emails(root) -> tuple[list[str], list[str]]:
    """(addresses, problems) of every e-mail link below root."""
    found, problems = [], []
    for a in root.find_all("a"):
        address, problem = _email_of_link(a)
        if address:
            found.append(address)
        if problem:
            problems.append(problem)
    return list(dict.fromkeys(found)), problems


def map_info(soup: BeautifulSoup, raw_html: str) -> dict:
    """Address query of the embedded Google map and coordinates, if the page carries any (it has not so far)."""
    out: dict = {"map_query": "", "page_lat": None, "page_lng": None, "page_coord_source": ""}
    for tag in soup.find_all(attrs={"data-src": re.compile("google")}) + soup.find_all("iframe", src=re.compile("google")):
        q = parse_qs(urlparse(html_lib.unescape(tag.get("data-src") or tag.get("src") or "")).query).get("q")
        if q:
            out["map_query"] = clean(q[0])
            break
    patterns = [
        (r"@(-?\d{1,2}\.\d{3,}),(-?\d{1,3}\.\d{3,})", "map link @lat,lng"),
        (r"[?&;](?:ll|sll|center|q)=(-?\d{1,2}\.\d{3,}),(-?\d{1,3}\.\d{3,})", "map link ll/center/q=lat,lng"),
        (r"data-lat=[\"'](-?\d{1,2}\.\d{3,})[\"']\s+data-lng=[\"'](-?\d{1,3}\.\d{3,})[\"']", "data-lat/data-lng"),
        (r"\"latitude\"\s*:\s*\"?(-?\d{1,2}\.\d{3,})\"?\s*,\s*\"longitude\"\s*:\s*\"?(-?\d{1,3}\.\d{3,})", "JSON latitude/longitude"),
    ]
    text = html_lib.unescape(raw_html)
    for pattern, label in patterns:
        for m in re.finditer(pattern, text):
            lat, lng = float(m.group(1)), float(m.group(2))
            if GERMANY_BOX[0] <= lat <= GERMANY_BOX[1] and GERMANY_BOX[2] <= lng <= GERMANY_BOX[3]:
                out.update(page_lat=lat, page_lng=lng, page_coord_source=label)
                return out
    return out


def normalize_street(text: str) -> str:
    t = clean(text).lower().replace("ß", "ss").replace("strasse", "str").replace("straße", "str")
    t = re.sub(r"str\.", "str", t)
    return re.sub(r"[^a-z0-9äöü]", "", t)


def content_hash(content) -> tuple[str, int]:
    """sha256 of the normalized content text of the page (scripts, cookie texts and the map placeholder removed, e-mail
    addresses decoded because Cloudflare re-keys the obfuscation on every request). Returns (hash, text length)."""
    c = copy.copy(content)
    for t in c(["script", "style", "svg", "noscript", "iframe"]):
        t.decompose()
    for t in c.select(".cc-map-embed-placeholder"):
        t.decompose()
    for a in c.find_all("a"):
        address, _ = _email_of_link(a)
        if address:
            a.replace_with(address)
    text = clean(c.get_text(" "))
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text)


def parse_station(html_text: str, path: str) -> dict:
    """One station page as one row. A page that does not yield street + PLZ + city has `address_ok` False (the run FAILS)."""
    soup = _soup(html_text)
    content = _content(soup)
    row: dict = {
        "path": path, "slug": slug_of(path), "source_profile_url": station_url(path), "name": "", "street": "",
        "zip": "", "city": "", "phone": "", "fax": "", "mobile": "", "email": "", "emails_found": [], "services": "",
        "services_text": "", "opening_hours": "", "opening_hours_raw": "", "hours_heading": "", "notice": "",
        "hours_highlight": "", "unparsed_lines": [], "warnings": [], "address_ok": False, "hu_on_page": False,
        "title": clean(soup.title.get_text()) if soup.title else "", "content_hash": "", "content_text_length": 0,
        "map_query": "", "page_lat": None, "page_lng": None, "page_coord_source": "", "address_raw": "",
    }
    row.update(map_info(soup, html_text))
    if content is None:
        row["warnings"].append("no content area (div#content_area) on the page")
        return row
    h1 = content.find("h1")
    row["name"] = clean(h1.get_text(" ")) if h1 else ""
    if not row["name"]:
        row["warnings"].append("no h1")
    row["content_hash"], row["content_text_length"] = content_hash(content)
    row["hu_on_page"] = bool(re.search(r"hauptuntersuchung", clean(content.get_text(" ")), re.IGNORECASE))

    # the contact module: the first text module that has a 'PLZ City' line
    module = None
    for cand in content.select("div.j-text"):
        probe = copy.copy(cand)
        for br in probe.find_all("br"):
            br.replace_with("\n")
        if any(_PLZ_LINE.match(line) for b in probe.find_all(["p", "h2", "h3", "h4", "li"]) for line in _block_lines(b)):
            module = cand
            break
    if module is None:
        row["warnings"].append("no text module with a 'PLZ City' line")
        return row

    module = copy.copy(module)
    for br in module.find_all("br"):
        br.replace_with("\n")
    emails, problems = _emails(module)
    row["emails_found"], row["email"] = emails, (emails[0] if emails else "")
    row["warnings"] += problems

    plain_lines: list[str] = []  # before the hours heading, red spans removed (they are notices, not address)
    hours_lines: list[str] = []
    notice, hours_highlight = [], []
    seen_heading = False
    for block in module.find_all(["p", "h2", "h3", "h4", "li"]):
        if block.find_parent(["p", "li"]) is not None and block.name in ("p", "li"):
            continue  # nested block: its parent already covered it
        full = _block_lines(block)
        if not seen_heading and any(_HOURS_HEADING.match(x) for x in full):
            seen_heading = True
            row["hours_heading"] = next(x for x in full if _HOURS_HEADING.match(x))
            continue
        red_texts = [clean(s.get_text(" ")) for s in block.find_all("span") if _is_red(s) and clean(s.get_text(" "))]
        if seen_heading:
            hours_lines += full
            hours_highlight += red_texts
        else:
            plain = copy.copy(block)
            for s in plain.find_all("span"):
                if _is_red(s):
                    s.decompose()
            plain_lines += _block_lines(plain)
            notice += red_texts
    row["notice"] = clean(" ".join(notice))
    row["hours_highlight"] = clean(" ".join(hours_highlight))

    plz_i = next((i for i, line in enumerate(plain_lines) if _PLZ_LINE.match(line)), None)
    if plz_i is not None:
        m = _PLZ_LINE.match(plain_lines[plz_i])
        row["zip"], row["city"] = m.group(1), clean(m.group(2))
        before = plain_lines[:plz_i]
        street = before[-1] if before else ""
        intro = before[:-1] if before else []
        if len(street) > 60 or re.search(r"hauptuntersuchung|führen wir", street, re.IGNORECASE) or not re.search(r"[A-Za-zÄÖÜäöüß]", street):
            intro, street = before, ""  # the line before the PLZ is not a street
        row["street"] = street
        row["address_raw"] = " | ".join(x for x in [street, plain_lines[plz_i]] if x)
        row["services_text"] = " ".join(intro)
        other = []
        for line in plain_lines[plz_i + 1:]:
            if (m := _PHONE_LINE.match(line)):
                row["phone"] = clean(m.group(1))
            elif (m := _FAX_LINE.match(line)):
                row["fax"] = clean(m.group(1))
            elif (m := _MOBILE_LINE.match(line)):
                row["mobile"] = clean(m.group(1))
            elif _MAIL_LINE.match(line):
                pass  # the address itself comes from the link
            else:
                other.append(line)
        row["unparsed_lines"] += other
    else:
        row["warnings"].append("no 'PLZ City' line in the contact module")

    services = [name for name, pattern in SERVICE_PATTERNS if pattern.search(row["services_text"])]
    services += [clean(li.get_text(" ")) for li in module.find_all("li") if clean(li.get_text(" "))]
    row["services"] = "; ".join(dict.fromkeys(services))

    row["opening_hours_raw"] = " | ".join(hours_lines)
    if hours_lines:
        normalized, unparsed = normalize_hours(hours_lines)
        row["opening_hours"] = normalized if not unparsed else row["opening_hours_raw"]
        row["unparsed_hours_lines"] = unparsed
    else:
        row["unparsed_hours_lines"] = []

    row["address_ok"] = bool(row["street"] and re.fullmatch(r"\d{5}", row["zip"] or "") and row["city"])
    return row
