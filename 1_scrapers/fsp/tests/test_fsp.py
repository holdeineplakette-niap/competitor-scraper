"""Offline tests for the FSP adapter: synthetic HTML modelled on the real markup, fake client, no network."""

from __future__ import annotations

import csv
import json

import pytest
import requests

from fsp import parse, recon, robots
from fsp.fetch import BlockedError, FetchError, FspClient, Response, detect_block
from fsp.report import render_report, sanity, slug_kind

ROBOTS = """User-agent: *
Disallow: /*?d=
Disallow: /*?f=
Disallow: /*&d=
Disallow: /*&f=
Disallow: /*?search=
Disallow: /*&search=
Disallow: /*?trk=
"""


def cf_encode(text: str, key: int = 0xC9) -> str:
    return (bytes([key]) + bytes(b ^ key for b in text.encode())).hex()


def card_html(slug="pruefstelle-testort", name="Prüfstelle Testort", badges=("Prüfstelle",), dist="12.3",
              plz="12345", email="info@example.org", hours=(("Mo.", "09:00 – 17:30 Uhr"), ("Di.", "geschlossen")),
              booking=True, website=False, phone="0123 456"):
    badge_html = "".join(f'<span class="badge  badge--small">{b}</span>' for b in badges)
    hours_html = "".join(
        f'<div class="grid grid-cols-3 gap-2"><div class="col-span-1"><strong>{d}</strong></div>'
        f'<div class="col-span-2"><span>{v}</span></div></div>' for d, v in hours)
    return f"""
<div class="location card card--hover-yellow">
 <div class="location__image"><img src="/images/location_placeholder.png"/></div>
 <div class="location__body"><div class="location__main">
  <div class="location__topics"><span class="badge badge--highlighted badge--small">{dist}
   km entfernt</span>{badge_html}</div>
  <h2 class="location__title"> {name} </h2>
  <span class="icon-label"><svg></svg><a href="https://www.google.com/maps/search/?api=1&query=x" class="link-none">
    <div>Teststraße 1</div><div>{plz} Testort</div></a></span>
  {f'<span class="icon-label"><a href="/cdn-cgi/l/email-protection#{cf_encode(email)}">E-Mail schreiben</a></span>' if email else ''}
  {f'<span class="icon-label"><a href="tel:{phone}">{phone}</a></span>' if phone else ''}
  {'<span class="icon-label"><a href="https://example.org/web">Website besuchen</a></span>' if website else ''}
  {f'<span class="icon-label"><div>{hours_html}</div></span>' if hours else ''}
 </div>
 <div class="location__footer"><div class="location__buttons">
  {'<a class="button" href="https://example.org/book"><span>Termin buchen</span></a>' if booking else ''}
  <a class="button" href="/standorte/{slug}?lat=1&lng=2&r=100&d=hauptuntersuchung"><span>Mehr erfahren</span></a>
 </div></div></div></div>"""


def page_html(cards, page=1, declared=3, heading=True):
    nav = ""
    if declared:
        btns = "".join(
            f'<li><span aria-current="page">{n}</span></li>' if n == page else
            f'<li><button aria-label="Gehe zu Seite {n}" wire:click="gotoPage({n})">{n}</button></li>'
            for n in range(1, declared + 1))
        nav = f'<nav aria-label="Seitennummerierung" class="pagination"><ul>{btns}</ul></nav>'
    h1 = "<h1>Prüfstellen & Standorte</h1>" if heading else "<h1>Etwas anderes</h1>"
    return f"<html><body><main><section>{h1}<div>{''.join(cards)}</div>{nav}</section></main></body></html>"


# ---------- parsing ----------

def test_decode_cf_email_roundtrip_and_entities():
    assert parse.decode_cf_email(cf_encode("info@example.org")) == "info@example.org"
    assert parse.decode_cf_email(cf_encode("a&#x40;b.de")) == "a@b.de"


def test_slug_from_href_strips_query():
    assert parse.slug_from_href("/standorte/pruefstelle-x?lat=1&d=hauptuntersuchung") == "pruefstelle-x"
    assert parse.slug_from_href("/standorte") is None
    assert parse.slug_from_href("/standorte/a/b") is None


def test_parse_card_all_fields():
    lp = parse.parse_list_page(page_html([card_html(website=True)]), 1)
    c = lp.cards[0]
    assert (c["name"], c["slug"], c["detail_url"]) == ("Prüfstelle Testort", "pruefstelle-testort",
                                                       "https://www.fsp.de/standorte/pruefstelle-testort")
    assert (c["street"], c["plz"], c["city"]) == ("Teststraße 1", "12345", "Testort")
    assert c["email"] == "info@example.org" and c["phone"] == "0123 456"
    assert c["distance_km"] == 12.3 and c["type_badges"] == "Prüfstelle"
    assert c["booking_url"] == "https://example.org/book" and c["website_url"] == "https://example.org/web"
    assert c["hours_mo"] == "09:00 – 17:30 Uhr" and c["hours_di"] == "geschlossen" and c["hours_mi"] == ""
    assert c["opening_hours_raw"] == "Mo. 09:00 – 17:30 Uhr | Di. geschlossen"
    assert c["other_icon_labels"] == "" and c["parse_warnings"] == ""


def test_parse_card_missing_parts_are_flagged_not_dropped():
    lp = parse.parse_list_page(page_html([card_html(email="", phone="", hours=(), booking=False,
                                                    badges=("Prüfstelle", "Neuer Typ"))]), 1)
    c = lp.cards[0]
    assert c["email"] == "" and c["phone"] == "" and c["booking_url"] == ""
    assert "unknown badge: Neuer Typ" in c["parse_warnings"]
    assert "no opening hours block" in c["parse_warnings"]


def test_two_slots_and_unknown_day():
    lp = parse.parse_list_page(page_html([card_html(hours=(("Mo.", "10:00 – 12:00 Uhr & 13:00 – 17:00 Uhr"),
                                                           ("Feiertag", "nach Vereinbarung")))]), 1)
    c = lp.cards[0]
    assert c["hours_mo"] == "10:00 – 12:00 Uhr & 13:00 – 17:00 Uhr"
    assert c["hours_other_days"] == "Feiertag nach Vereinbarung" and "unknown day label" in c["parse_warnings"]


def test_six_digit_plz_is_kept_and_flagged_not_lost():
    c = parse.parse_list_page(page_html([card_html(plz="553340")]), 1).cards[0]
    assert (c["plz"], c["city"], c["street"]) == ("553340", "Testort", "Teststraße 1")
    assert "PLZ is not 5 digits: '553340'" in c["parse_warnings"]


def test_address_without_street_and_blank_hours_row():
    html = card_html(hours=(("", ""),)).replace("<div>Teststraße 1</div>", "")
    c = parse.parse_list_page(page_html([html]), 1).cards[0]
    assert (c["street"], c["plz"], c["city"]) == ("", "12345", "Testort")
    assert "no street line" in c["parse_warnings"] and "no opening hours block" in c["parse_warnings"]
    assert "unknown day label" not in c["parse_warnings"]


def test_parse_detail_reuses_card_blocks_and_detects_hu():
    page = ("<html><body><main><h1>Kfz-Prüfstützpunkt Test</h1><span class=\"badge\">Prüfstelle</span>"
            "<p>Dienstleistungen Hauptuntersuchung (HU) nach §29 StVZO amtlich</p>"
            + card_html().split('<div class="location__main">')[1].split('<div class="location__footer">')[0]
            + "</main></body></html>")
    r = parse.parse_detail(page, "kfz-pruefstuetzpunkt-test")
    assert (r["name"], r["slug"], r["plz"], r["hu_on_detail_page"]) == (
        "Kfz-Prüfstützpunkt Test", "kfz-pruefstuetzpunkt-test", "12345", True)
    assert r["email"] == "info@example.org" and r["hours_mo"] == "09:00 – 17:30 Uhr"
    assert "no slug" not in r["parse_warnings"] and "no name" not in r["parse_warnings"]
    assert parse.parse_detail(page.replace("Hauptuntersuchung", "Gutachten"), "x")["hu_on_detail_page"] is False


def test_pagination_bar_and_heading():
    lp = parse.parse_list_page(page_html([card_html()], page=2, declared=27), 2)
    assert lp.declared_pages == 27 and lp.active_page == 2 and lp.has_results_heading
    assert parse.parse_list_page(page_html([card_html()], declared=0), 1).declared_pages is None


def test_empty_real_page_vs_unknown_page():
    assert parse.parse_list_page(page_html([], heading=True), 1).has_results_heading is True
    assert parse.parse_list_page(page_html([], heading=False), 1).has_results_heading is False


def test_sitemap_urlset_and_index():
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    kind, e = parse.parse_sitemap(f'<urlset {ns}><url><loc>https://www.fsp.de/standorte/a</loc>'
                                  f'<lastmod>2026-01-01</lastmod></url><url><loc>https://www.fsp.de/standorte</loc></url></urlset>')
    assert kind == "urlset" and e[0]["lastmod"] == "2026-01-01" and len(e) == 2
    assert parse.standorte_slug(e[0]["loc"]) == "a" and parse.standorte_slug(e[1]["loc"]) is None
    kind, e = parse.parse_sitemap(f'<sitemapindex {ns}><sitemap><loc>https://www.fsp.de/s1.xml</loc></sitemap></sitemapindex>')
    assert kind == "index" and e[0]["loc"].endswith("s1.xml")


# ---------- block detection and robots ----------

@pytest.mark.parametrize("status,headers,text,expect", [
    (200, {}, "<html>fine</html>", False),
    (403, {}, "x", True),
    (503, {}, "x", True),
    (200, {"cf-mitigated": "challenge"}, "x", True),
    (200, {}, "<title>Just a moment...</title>", True),
    (200, {}, "<script>window._cf_chl_opt</script> cf-chl-bypass", True),
])
def test_detect_block(status, headers, text, expect):
    assert bool(detect_block(status, headers, text)) is expect


def test_robots_rules_with_wildcards():
    dis, allow, _ = robots.parse_rules(ROBOTS)
    ok = "/standorte?lat=51.1634&lng=10.4477&r=1000&page=3"
    assert robots.disallowing_rule(ok, dis, allow) is None
    assert robots.disallowing_rule("/sitemap.xml", dis, allow) is None
    assert robots.disallowing_rule(ok + "&d=hauptuntersuchung", dis, allow) == "/*&d="
    assert robots.disallowing_rule("/standorte?d=x", dis, allow) == "/*?d="
    assert robots.disallowing_rule("/standorte/slug?search=Berlin", dis, allow) == "/*?search="


def test_guard_blocks_disallowed_unless_overridden():
    dis, allow, _ = robots.parse_rules(ROBOTS)
    g = recon.Guard(dis, allow, allowed=False)
    g.check(recon.LIST_URL, {"lat": 1, "lng": 2, "r": 1000, "page": 1})
    with pytest.raises(robots.RobotsDisallowedError):
        g.check(recon.LIST_URL, {"lat": 1, "lng": 2, "r": 1000, "d": "hauptuntersuchung", "page": 1})
    recon.Guard(dis, allow, allowed=True).check(recon.LIST_URL, {"d": "hauptuntersuchung"})


# ---------- fetch client ----------

class FakeHttp:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeRequestsResponse:
    def __init__(self, status=200, text="ok", headers=None, url="https://www.fsp.de/x"):
        self.status_code, self.text, self.headers, self.url = status, text, headers or {}, url


def make_client(replies):
    c = FspClient(sleep=lambda s: None, delay_range=(0, 0))
    c.session = FakeHttp(replies)
    return c


def test_client_retries_then_succeeds():
    c = make_client([requests.ConnectionError("boom"), FakeRequestsResponse(500), FakeRequestsResponse(200, "fine")])
    r = c.get("https://www.fsp.de/x")
    assert r.text == "fine" and r.attempts == 3 and c.session.calls == 3


def test_client_gives_up_after_three_retries():
    c = make_client([requests.ConnectionError("boom")] * 4)
    with pytest.raises(FetchError):
        c.get("https://www.fsp.de/x")
    assert c.session.calls == 4  # first try + 3 retries


def test_client_never_retries_a_block():
    b = make_client([FakeRequestsResponse(403, "denied")])
    with pytest.raises(BlockedError):
        b.get("https://www.fsp.de/x")
    assert b.session.calls == 1  # no hammering of a block


# ---------- crawl loop and full run with a fake client ----------

class FakeFsp:
    """Stands in for FspClient: serves robots, list pages (cards per page configurable), sitemap, details."""

    def __init__(self, pages: list[list[str]], hu_pages=None, sitemap_extra=(), clamp=False, same_page=False,
                 blocked_at=None, declared=None):
        self.pages, self.hu_pages, self.sitemap_extra = pages, hu_pages or [], sitemap_extra
        self.clamp, self.same_page, self.blocked_at = clamp, same_page, blocked_at
        self.declared = declared if declared is not None else len(pages)
        self.requests_made, self.urls = 0, []

    def _resp(self, url, text):
        return Response(url=url, status=200, text=text, elapsed_s=0.0, attempts=1, content_type="text/html",
                        cf_mitigated=None)

    def get(self, url, params=None):
        self.requests_made += 1
        self.urls.append((url, dict(params or {})))
        full = requests.Request("GET", url, params=params).prepare().url  # like the real server: URL incl. query
        if url.endswith("robots.txt"):
            return self._resp(full, ROBOTS)
        if url.endswith("sitemap.xml"):
            locs = "".join(f"<url><loc>https://www.fsp.de/standorte/{s}</loc><lastmod>2026-02-02</lastmod></url>"
                           for s in sorted({s for p in self.pages for s in p} | set(self.sitemap_extra)))
            return self._resp(full, f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                   f'<url><loc>https://www.fsp.de/standorte</loc></url>{locs}</urlset>')
        if "/standorte/" in url:
            return self._resp(full, "<html><head><title>Detail</title></head><main><h1>Nur Sitemap</h1>"
                                   "<span class='badge'>Prüfstelle</span> Adresse</main></html>")
        n = params["page"]
        if self.blocked_at == n:
            return self._resp(full, "<html><body><h1>Etwas anderes</h1></body></html>")
        pages = self.hu_pages if params.get("d") else self.pages
        if self.same_page:
            n = 1
        elif n > len(pages):
            n = len(pages) if self.clamp else None
        slugs = pages[n - 1] if n else []
        cards = [card_html(slug=s, name=f"Prüfstelle {s}", plz="12345", dist=str(10 + i)) for i, s in enumerate(slugs)]
        return self._resp(full, page_html(cards, page=params["page"], declared=params.get("r") == 2000 and 99 or self.declared))


def run_fake(tmp_path, fake, **kw):
    from datetime import datetime

    return recon.run(client=fake, root=tmp_path, now=datetime(2026, 10, 6, 12, 0), **kw)


def test_full_fake_run_writes_everything_and_report(tmp_path):
    fake = FakeFsp([["a", "b"], ["c", "d"], ["e"]], sitemap_extra=("only-in-sitemap",))
    run_dir = run_fake(tmp_path, fake)
    assert run_dir.name == "2026-10-06_1200_recon" and "test_output" in str(run_dir)
    for f in ("fsp_all_2026-10-06.csv", "fsp_sitemap_slugs_2026-10-06.csv", "fsp_run_report_2026-10-06.md",
              "query_log.csv", "run_summary.json", "raw/all_page_01.html", "raw/all_page_04.html",
              "raw/all_r2000_page_01.html", "sitemap/sitemap.xml", "detail/only-in-sitemap.html"):
        assert (run_dir / f).exists(), f
    assert not (run_dir / "fsp_hu_2026-10-06.csv").exists()  # HU stage was not requested
    assert (run_dir / "fsp_all_2026-10-06.csv").read_bytes().startswith(b"\xef\xbb\xbf")  # BOM for Excel
    rows = (run_dir / "fsp_all_2026-10-06.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(rows) == 1 + 5 + 1  # 5 listed sites + 1 extra row for the sitemap-only site
    assert "only-in-sitemap" in rows[-1] and "sitemap_only_detail_page" in rows[-1]
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "DONE" and summary["crawls"]["all"]["stop_reason"].startswith("page 4 has zero cards")
    report = (run_dir / "fsp_run_report_2026-10-06.md").read_text(encoding="utf-8")
    assert "only-in-sitemap" in report and "Slugs in the sitemap but in no search" in report
    assert "disallowed" in report
    assert not any(p and p.get("d") for _, p in fake.urls)  # no disallowed URL was requested


def test_rebuild_reproduces_the_live_run_without_any_request(tmp_path):
    live = run_fake(tmp_path, FakeFsp([["a", "b"], ["c", "d"], ["e"]], sitemap_extra=("only-in-sitemap",)))
    from datetime import datetime

    rebuilt = recon.run(root=tmp_path, now=datetime(2026, 10, 6, 13, 0), mode="rebuild", source_runs=[live])
    assert rebuilt.parent.name == "output" and rebuilt.name == "2026-10-06_1300_rebuild"
    assert not (rebuilt / "raw").exists() and not (rebuilt / "query_log.csv").exists()  # no copy of raw files
    def table(path):  # parsed CSV without the scraped_at column (differs by design: it is the run's own start time)
        rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))
        drop = rows[0].index("scraped_at") if "scraped_at" in rows[0] else None
        return [[c for i, c in enumerate(r) if i != drop] for r in rows]

    for f in ("fsp_all_2026-10-06.csv", "fsp_sitemap_slugs_2026-10-06.csv"):
        assert table(live / f) == table(rebuilt / f), f
    assert len(table(live / "fsp_all_2026-10-06.csv")) == 1 + 5 + 1
    summary = json.loads((rebuilt / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "rebuild" and summary["source_runs"] == [str(live)] and summary["requests_made"] == 0
    assert "Offline rebuild: no request was made" in (rebuilt / "fsp_run_report_2026-10-06.md").read_text(encoding="utf-8")


def test_rebuild_needs_source_runs(tmp_path):
    with pytest.raises(ValueError):
        recon.run(root=tmp_path, mode="rebuild")


def test_stops_when_page_returns_only_known_slugs(tmp_path):
    fake = FakeFsp([["a", "b"], ["c", "d"]], clamp=True, declared=2)
    crawl = run_fake(tmp_path, fake, stages=("all",))
    summary = json.loads((crawl / "run_summary.json").read_text(encoding="utf-8"))
    assert "only slugs already seen" in summary["crawls"]["all"]["stop_reason"]


def test_pagination_not_working_stops_the_run(tmp_path):
    fake = FakeFsp([["a", "b"], ["c", "d"]], same_page=True)
    with pytest.raises(recon.PaginationError):
        run_fake(tmp_path, fake, stages=("all",))
    summary = json.loads(next(tmp_path.rglob("run_summary.json")).read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL" and "Pagination via GET is NOT proven" in summary["error"]


def test_page_without_cards_and_without_heading_is_a_block_not_zero_results(tmp_path):
    fake = FakeFsp([["a"], ["b"], ["c"]], blocked_at=2)
    with pytest.raises(BlockedError):
        run_fake(tmp_path, fake, stages=("all",))


def test_hu_stage_is_refused_without_override_and_runs_with_it(tmp_path):
    fake = FakeFsp([["a", "b"], ["c"]], hu_pages=[["a"], ["c"]])
    with pytest.raises(robots.RobotsDisallowedError):
        run_fake(tmp_path, fake, stages=("all", "hu"))
    assert not any(p and p.get("d") for _, p in fake.urls)
    fake2 = FakeFsp([["a", "b"], ["c"]], hu_pages=[["a"], ["c"]])
    run_dir = run_fake(tmp_path, fake2, stages=("all", "hu", "sitemap"), allow_robots_disallowed=True)
    assert (run_dir / "fsp_hu_2026-10-06.csv").exists()
    rows = list(csv.DictReader((run_dir / "fsp_all_2026-10-06.csv").read_text(encoding="utf-8-sig").splitlines(),
                               delimiter=";"))
    assert {r["slug"]: r["offers_hu"] for r in rows} == {"a": "True", "b": "False", "c": "True"}
    report = (run_dir / "fsp_run_report_2026-10-06.md").read_text(encoding="utf-8")
    assert "Slugs only in the all-services run" in report and "`b`" in report


def test_r2000_difference_is_flagged(tmp_path):
    run_dir = run_fake(tmp_path, FakeFsp([["a"], ["b"]]), stages=("all", "r2000"))
    assert "FLAG: r=2000 differs from r=1000" in (run_dir / "fsp_run_report_2026-10-06.md").read_text(encoding="utf-8")


def test_oddities_lists_psp_named_and_mismatched_cards():
    from fsp.report import oddities

    lp = parse.parse_list_page(page_html([
        card_html(slug="pruefstuetzpunkt-x", name="Prüfstützpunkt X"),
        card_html(slug="kfz-sachverstaendigenbuero-y", name="Kfz-Sachverständigenbüro Y", badges=("Prüfstelle",)),
        card_html(slug="pruefstelle-z", name="Prüfstelle Z", badges=("Gutachtenbüro",)),
        card_html(slug="ok", name="Prüfstelle Ok", badges=("Prüfstelle", "Gutachtenbüro")),
    ]), 1)
    o = oddities(lp.cards)
    assert [r["slug"] for r in o["named 'Prüfstützpunkt' but present in the locator"]] == ["pruefstuetzpunkt-x"]
    assert [r["slug"] for r in o["badge 'Prüfstelle' but name says Sachverständigen-/Gutachten-/Schadenbüro"]] == [
        "kfz-sachverstaendigenbuero-y"]
    assert [r["slug"] for r in o["name says Prüfstelle but no 'Prüfstelle' badge"]] == ["pruefstelle-z"]
    assert [r["slug"] for r in o["both badges (Prüfstelle and Gutachtenbüro)"]] == ["ok"]


def test_sanity_catches_bad_cards():
    no_street = card_html(slug="y").replace("<div>Teststraße 1</div>", "")
    lp = parse.parse_list_page(page_html([card_html(plz="1234"), card_html(slug="x", badges=("Werkstatt",)), no_street]), 1)
    s = sanity({"rows": lp.cards, "pages": [{"cards": 3}]})
    assert [r["slug"] for r in s["failing_cards"]] == ["y"]  # a part of the address is missing
    assert len(s["plz_not_5_digits"]) == 1
    assert s["unknown_badges"] == {"Werkstatt": 1}
    assert slug_kind("kfz-sachverstaendigenbuero-x") == "kfz-sachverstaendigenbuero-*"
