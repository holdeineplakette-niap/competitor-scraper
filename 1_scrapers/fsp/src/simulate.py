"""A fake fsp.de for `mode="simulated"` and for tests: same markup as the real site, synthetic data, no network.

Known truth, so a run can be checked against it:
  30 HU sites (badge Prüfstelle, in the HU list)
   1 Prüfstelle-badged Sachverständigenbüro that is NOT in the HU list (like the real Semmelroth)
   8 Gutachtenbüros without HU
   1 sitemap-only site whose own page names HU  (counts: in scope)
   1 sitemap-only site whose page names no HU    (does not count)
  => 31 in scope.
`fault` switches on a defect that a run must NOT pass: "same_page" (&page=N ignored), "blocked" (Cloudflare page),
"drop_hu_page" (the HU list silently loses its last page), "no_sitemap_only_detail" (a sitemap-only page answers 404).
"""

from __future__ import annotations

import requests

from fsp.fetch import BASE, BlockedError, FetchError, Response

ROBOTS = """User-agent: *
Disallow: /*?d=
Disallow: /*?f=
Disallow: /*&d=
Disallow: /*&f=
Disallow: /*?search=
Disallow: /*&search=
"""
PER_PAGE = 12
EXPECTED_IN_SCOPE = 31


def _cf_encode(text: str, key: int = 0xC9) -> str:
    return (bytes([key]) + bytes(b ^ key for b in text.encode())).hex()


def _blocks(i: int, name: str) -> str:
    return (
        f'<span class="icon-label"><a href="https://www.google.com/maps/search/?api=1&query=x" class="link-none">'
        f"<div>Simstraße {i}</div><div>{10000 + i} Simstadt</div></a></span>"
        f'<span class="icon-label"><a href="/cdn-cgi/l/email-protection#{_cf_encode(f"info{i}@example.org")}">E-Mail</a></span>'
        f'<span class="icon-label"><a href="tel:0123 {i:04d}">0123 {i:04d}</a></span>'
        '<span class="icon-label"><div><div class="grid"><div><strong>Mo.</strong></div><div><span>09:00 – 17:00 Uhr</span></div></div>'
        '<div class="grid"><div><strong>Di.</strong></div><div><span>geschlossen</span></div></div></div></span>'
    )


def card_html(slug: str, name: str, badges: tuple[str, ...], dist: float, i: int) -> str:
    badge_html = "".join(f'<span class="badge  badge--small">{b}</span>' for b in badges)
    return (
        '<div class="location card card--hover-yellow"><div class="location__image">'
        '<img src="/images/location_placeholder.png"/></div><div class="location__body"><div class="location__main">'
        f'<div class="location__topics"><span class="badge badge--highlighted badge--small">{dist:.1f} km entfernt</span>{badge_html}</div>'
        f'<h2 class="location__title">{name}</h2>{_blocks(i, name)}</div><div class="location__footer">'
        f'<div class="location__buttons"><a class="button" href="/standorte/{slug}?lat=1&lng=2&r=1000"><span>Mehr erfahren</span></a>'
        "</div></div></div></div>"
    )


def page_html(cards: list[str], page: int, declared: int) -> str:
    btns = "".join(
        f'<li><span aria-current="page">{n}</span></li>' if n == page else
        f'<li><button aria-label="Gehe zu Seite {n}" wire:click="gotoPage({n})">{n}</button></li>'
        for n in range(1, declared + 1)
    )
    nav = f'<nav aria-label="Seitennummerierung" class="pagination"><ul>{btns}</ul></nav>' if declared else ""
    return (f"<html><body><main><section><h1>Prüfstellen & Standorte</h1><div>{''.join(cards)}</div>{nav}"
            "</section></main></body></html>")


def detail_html(name: str, badge: str, services: str, i: int) -> str:
    return (f'<html><head><title>{name} – FSP</title></head><body><main><h1>{name}</h1>'
            f'<span class="badge">{badge}</span><p>Dienstleistungen {services}</p>{_blocks(i, name)}</main></body></html>')


class SimulatedFsp:
    """Same interface as FspClient.get(url, params)."""

    def __init__(self, fault: str | None = None):
        self.fault = fault
        self.requests_made = 0
        self.urls: list[tuple[str, dict]] = []
        self.sites: list[dict] = []
        for i in range(1, 31):
            self.sites.append({"slug": f"pruefstelle-sim-{i:02d}", "name": f"Prüfstelle Sim-{i:02d}",
                               "badges": ("Prüfstelle",), "hu": True})
        self.sites.append({"slug": "kfz-sachverstaendigenbuero-sim-x", "name": "Kfz-Sachverständigenbüro Sim X",
                           "badges": ("Prüfstelle",), "hu": False})
        for i in range(32, 40):
            self.sites.append({"slug": f"kfz-sachverstaendigenbuero-sim-{i}", "name": f"Kfz-Sachverständigenbüro Sim {i}",
                               "badges": ("Gutachtenbüro",), "hu": False})
        for k, s in enumerate(self.sites, start=1):
            s["i"] = k
        self.only_sitemap = {
            "kfz-pruefstuetzpunkt-sim": ("Kfz-Prüfstützpunkt Sim", "Prüfstelle",
                                         "Hauptuntersuchung (HU) nach §29 StVZO amtlich", 91),
            "kfz-sachverstaendigenbuero-sim-nohu": ("Kfz-Sachverständigenbüro Sim NoHU", "Gutachtenbüro",
                                                    "Wertgutachten freiwirtschaftlich", 92),
        }

    def _resp(self, full: str, text: str) -> Response:
        return Response(url=full, status=200, text=text, elapsed_s=0.0, attempts=1, content_type="text/html",
                        cf_mitigated=None)

    def get(self, url: str, params: dict | None = None) -> Response:
        self.requests_made += 1
        params = dict(params or {})
        self.urls.append((url, params))
        full = requests.Request("GET", url, params=params).prepare().url
        if url.endswith("robots.txt"):
            return self._resp(full, ROBOTS)
        if url.endswith("sitemap.xml"):
            slugs = [s["slug"] for s in self.sites] + list(self.only_sitemap)
            locs = "".join(f"<url><loc>{BASE}/standorte/{s}</loc><lastmod>2026-01-01</lastmod></url>" for s in slugs)
            return self._resp(full, f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                    f"<url><loc>{BASE}/standorte</loc></url>{locs}</urlset>")
        if "/standorte/" in url:
            slug = url.rsplit("/", 1)[1]
            if self.fault == "no_sitemap_only_detail":
                raise FetchError(f"{url} returned HTTP 404")
            name, badge, services, i = self.only_sitemap[slug]
            return self._resp(full, detail_html(name, badge, services, i))
        if self.fault == "blocked" and params.get("page", 1) >= 2:
            raise BlockedError(f"{full} was blocked (HTTP 403; 'Just a moment' page)")
        sites = [s for s in self.sites if s["hu"]] if params.get("d") else self.sites
        pages = [sites[k:k + PER_PAGE] for k in range(0, len(sites), PER_PAGE)]
        if self.fault == "drop_hu_page" and params.get("d"):
            pages = pages[:-1]  # the last page vanishes, the pagination bar still counts it
        declared = len(pages) + (1 if self.fault == "drop_hu_page" and params.get("d") else 0)
        n = params.get("page", 1)
        if self.fault == "same_page":
            n = 1
        chunk = pages[n - 1] if 1 <= n <= len(pages) else []
        cards = [card_html(s["slug"], s["name"], s["badges"], 10.0 + s["i"], s["i"]) for s in chunk]
        return self._resp(full, page_html(cards, params.get("page", 1), declared))
