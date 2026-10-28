"""Parse fsp.de list pages (server-rendered cards) and the sitemap. No network access here.

Every value that parsing can lose information on is kept twice: the parsed value and the raw text. Anything the
parser does not recognise is not dropped: it goes to `other_*` columns and to `parse_warnings`.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

BASE = "https://www.fsp.de"
KNOWN_BADGES = ("Prüfstelle", "Gutachtenbüro")
DAYS = ("mo", "di", "mi", "do", "fr", "sa", "so")
RESULTS_HEADING = "Prüfstellen & Standorte"
_WS = re.compile(r"\s+")
_PLZ_CITY = re.compile(r"^(\d{4,6})\s+(.+)$")  # tolerant on purpose: a typo'd PLZ is kept and flagged, not lost
_DISTANCE = re.compile(r"(\d+(?:[.,]\d+)?)")
_SLUG = re.compile(r"^/standorte/([^/?#]+)/?$")
_GOTO = re.compile(r"gotoPage\((\d+)\)")
_TOTAL = re.compile(r"\b\d[\d.]*\s+(?:Standorte|Ergebnisse|Treffer|Prüfstellen|Einträge)\b")


def clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


def decode_cf_email(hex_string: str) -> str:
    """Cloudflare email obfuscation: the first byte is the XOR key for all following bytes.

    The decoded text is sometimes HTML-entity encoded, so it is unescaped afterwards.
    """
    raw = bytes.fromhex(hex_string)
    key = raw[0]
    return html.unescape(bytes(b ^ key for b in raw[1:]).decode("utf-8"))


def slug_from_href(href: str) -> str | None:
    """'/standorte/<slug>?lat=...' -> '<slug>' (query string dropped); None if it is not a detail link."""
    path = urlparse(href).path
    m = _SLUG.match(path)
    return m.group(1) if m else None


@dataclass
class ListPage:
    cards: list[dict]
    has_results_heading: bool
    declared_pages: int | None  # highest page number the pagination bar shows; None = no pagination bar
    active_page: int | None
    pagination_labels: list[int] = field(default_factory=list)
    total_text: str = ""  # a "123 Standorte"-style total, if the page has one


def _day_key(label: str) -> str | None:
    key = clean(label).rstrip(".").lower()[:2]
    return key if key in DAYS else None


def parse_card(card, page: int, position: int) -> dict:
    warnings: list[str] = []
    row: dict = {"page": page, "position_on_page": position}

    # badges: the highlighted one is the distance, the others are type tags
    dist_raw, types = "", []
    for badge in card.select("span.badge"):
        text = clean(badge.get_text(" "))
        if "badge--highlighted" in (badge.get("class") or []):
            dist_raw = text
        elif text:
            types.append(text)
    m = _DISTANCE.search(dist_raw)
    row["distance_raw"] = dist_raw
    row["distance_km"] = float(m.group(1).replace(",", ".")) if m else None
    row["type_badges"] = " | ".join(types)
    unknown_badges = [t for t in types if t not in KNOWN_BADGES]
    if unknown_badges:
        warnings.append("unknown badge: " + ", ".join(unknown_badges))
    if not types:
        warnings.append("no type badge")

    title = card.select_one("h2.location__title")
    row["name"] = clean(title.get_text(" ")) if title else ""

    row.update(street="", plz="", city="", address_raw="", maps_url="")
    row.update(email="", email_raw_href="", phone="", phone_raw="", website_url="", booking_url="")
    row.update(opening_hours_raw="", **{f"hours_{d}": "" for d in DAYS}, hours_other_days="")
    other_labels: list[str] = []

    for label in card.select("span.icon-label"):
        maps = label.find("a", href=re.compile(r"google\.[a-z.]+/maps"))
        email = label.find("a", href=re.compile(r"email-protection#"))
        tel = label.find("a", href=re.compile(r"^tel:", re.I))
        hours_rows = label.select("div.grid")
        if maps:
            lines = [clean(d.get_text(" ")) for d in maps.find_all("div") if clean(d.get_text(" "))]
            if not lines:
                lines = [clean(maps.get_text(" "))]
            row["address_raw"] = " | ".join(lines)
            row["maps_url"] = maps["href"]
            m = _PLZ_CITY.match(lines[-1]) if lines else None
            if m:
                row["plz"], row["city"] = m.group(1), clean(m.group(2))
                row["street"] = ", ".join(lines[:-1])
                if not re.fullmatch(r"\d{5}", row["plz"]):
                    warnings.append(f"PLZ is not 5 digits: {row['plz']!r} (as published by FSP)")
                if not row["street"]:
                    warnings.append("address: no street line (PLZ and city only)")
            else:
                row["street"] = ", ".join(lines)
                warnings.append(f"address: no 'PLZ City' line in {lines!r}")
        elif email:
            href = email["href"]
            row["email_raw_href"] = href
            try:
                row["email"] = decode_cf_email(href.split("email-protection#", 1)[1])
            except (ValueError, UnicodeDecodeError) as exc:
                warnings.append(f"email decode failed: {exc}")
        elif tel:
            row["phone_raw"] = clean(tel.get_text(" "))
            row["phone"] = clean(tel["href"][4:])
        elif label.find("a", string=re.compile("Website")):
            row["website_url"] = label.find("a", string=re.compile("Website"))["href"]
        elif hours_rows:
            parts = []
            for r in hours_rows:
                cells = [clean(c.get_text(" ")) for c in r.find_all("div", recursive=False)]
                if len(cells) < 2:
                    cells = [clean(r.get_text(" "))]
                    parts.append(cells[0])
                    warnings.append(f"hours row not split in 2 cells: {cells[0]!r}")
                    continue
                day, value = cells[0], cells[1]
                if not day and not value:  # empty placeholder row (seen on a few partner offices)
                    continue
                parts.append(f"{day} {value}")
                key = _day_key(day)
                if key and not row[f"hours_{key}"]:
                    row[f"hours_{key}"] = value
                elif key:  # same day twice: keep both
                    row[f"hours_{key}"] += f" & {value}"
                else:
                    row["hours_other_days"] += f"{day} {value}; "
                    warnings.append(f"hours: unknown day label {day!r}")
            row["opening_hours_raw"] = " | ".join(parts)
        else:
            other_labels.append(clean(label.get_text(" ")))
    row["other_icon_labels"] = " | ".join(other_labels)
    row["hours_other_days"] = row["hours_other_days"].strip("; ")
    if not row["opening_hours_raw"]:
        warnings.append("no opening hours block")

    # links: detail page, booking, website, anything else
    row["slug"], row["detail_url"], row["detail_href_raw"] = "", "", ""
    other_links: list[str] = []
    for a in card.find_all("a", href=True):
        href, text = a["href"], clean(a.get_text(" "))
        if a.find_parent("span", class_="icon-label"):
            if "Website" in text:
                row["website_url"] = href
            continue
        if "Mehr erfahren" in text:
            row["detail_href_raw"] = href  # raw href incl. query string; the slug is our ID
            slug = slug_from_href(href)
            if slug:
                row["slug"] = slug
                row["detail_url"] = f"{BASE}/standorte/{slug}"
        elif "Termin buchen" in text:
            row["booking_url"] = href
        elif "Website" in text:
            row["website_url"] = href
        else:
            other_links.append(f"{text} -> {href}")
    row["other_links"] = " | ".join(other_links)

    img = card.select_one(".location__image img")
    src = img.get("src", "") if img else ""
    row["has_photo"] = bool(src) and "location_placeholder" not in src
    row["image_url"] = src if row["has_photo"] else ""
    row["card_text_raw"] = clean(card.get_text(" "))

    if not row["slug"]:
        warnings.append("no slug (no usable 'Mehr erfahren' link)")
    if not row["name"]:
        warnings.append("no name")
    if not row["address_raw"]:
        warnings.append("no address block")
    row["parse_warnings"] = "; ".join(warnings)
    return row


def parse_list_page(html_text: str, page: int = 1) -> ListPage:
    soup = BeautifulSoup(html_text, "html.parser")
    h1 = soup.find("h1")
    has_heading = bool(h1 and RESULTS_HEADING in clean(h1.get_text(" ")))
    cards = [
        parse_card(c, page, i)
        for i, c in enumerate(soup.select("div.location.card"), start=1)
    ]
    # is there a "N Standorte"-style total anywhere outside the cards? (none seen so far)
    for c in soup.select("div.location.card"):
        c.decompose()
    section = h1.find_parent("section") if h1 else None
    m = _TOTAL.search(clean(section.get_text(" ")) if section else "")
    total_text = m.group(0) if m else ""
    labels: list[int] = []
    active = None
    nav = soup.find("nav", attrs={"aria-label": "Seitennummerierung"})
    if nav:
        for btn in nav.find_all("button"):
            m = _GOTO.search(btn.get("wire:click", ""))
            if m:
                labels.append(int(m.group(1)))
        cur = nav.find(attrs={"aria-current": "page"})
        if cur and clean(cur.get_text()).isdigit():
            active = int(clean(cur.get_text()))
            labels.append(active)
    return ListPage(
        cards=cards,
        has_results_heading=has_heading,
        declared_pages=max(labels) if labels else None,
        active_page=active,
        pagination_labels=sorted(set(labels)),
        total_text=total_text,
    )


def parse_detail(html_text: str, slug: str) -> dict:
    """A detail page as one row. It uses the same address/contact/hours blocks as a list card, so `parse_card` is reused.

    Used only for sitemap-only slugs (sites the locator does not list). `hu_on_detail_page` is True when the page's
    service list names Hauptuntersuchung.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup
    row = parse_card(main, 0, 0)
    h1 = main.find("h1")
    row["name"] = (clean(h1.get_text(" ")) if h1 else "") or row["name"]  # the page title is the h1
    row.update(slug=slug, detail_url=f"{BASE}/standorte/{slug}", distance_raw="", distance_km=None)
    row["parse_warnings"] = "; ".join(w for w in row["parse_warnings"].split("; ")
                                      if w and not w.startswith(("no slug", "no name")) and w != "no type badge")
    row["hu_on_detail_page"] = "Hauptuntersuchung" in clean(main.get_text(" "))
    return row


def parse_sitemap(xml_text: str) -> tuple[str, list[dict]]:
    """('index' | 'urlset', [{'loc': ..., 'lastmod': ...}, ...]). Raises ET.ParseError on non-XML input."""
    root = ET.fromstring(xml_text)
    kind = "index" if root.tag.endswith("sitemapindex") else "urlset"
    entries = []
    for el in root:
        loc = lastmod = ""
        for child in el:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "loc":
                loc = (child.text or "").strip()
            elif tag == "lastmod":
                lastmod = (child.text or "").strip()
        if loc:
            entries.append({"loc": loc, "lastmod": lastmod})
    return kind, entries


def standorte_slug(url: str) -> str | None:
    """Slug of a '/standorte/<slug>' URL (the bare '/standorte' list page gives None)."""
    return slug_from_href(urlparse(unquote(url)).path)

