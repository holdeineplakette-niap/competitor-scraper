"""TÜV Hessen locator access: Playwright (Chromium), UI-driven capture of the search request, replay from the page.

The locator page (TYPO3, behind Cloudflare) posts the form as multipart to `/?eID=location_search` and shows
`hits.hits.length` on the search button. This module

  1. loads the page (headless first; headed only if the headless load is blocked and `fallback_headed` is on),
  2. declines the cookie banner ("Nur erforderliche Cookies erlauben"),
  3. runs each dataset through the UI (select the type, click "Standorte finden") and records the real request
     (all form fields) and the real response bytes and the counter text on the button,
  4. replays the same request from inside the page (`fetch` with the page's cookies) and keeps that answer too,
  5. optionally opens a few detail pages.

Nothing here decides whether the data is complete: it only collects evidence. A Cloudflare block, a non-JSON answer or
a changed page raises `FetchError` / `BlockedError`; it is never turned into "0 sites". No stealth plugins, no faked
fingerprints, no challenge solver: if a challenge page appears, the run stops.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

BASE_URL = "https://www.tuev-hessen.de"
LOCATOR_URL = f"{BASE_URL}/38/alle-standorte-von-tuev-hessen-seinen-partnern-und-den-tuev-sued-auto-partnern/"
ENDPOINT_PATH = "/?eID=location_search"
ENDPOINT_URL = BASE_URL + ENDPOINT_PATH

FORM = "form.location-search-form"
TYPE_SELECT = f"{FORM} select[name='tags[]']"
SUBMIT = f"{FORM} button[type=submit]"
COUNTER = f"{FORM} .location-search-results-count"
CONSENT_DENY = "a.cc-deny"
CONSENT_OVERLAY = "#dr_blocking_div"

# dataset key -> (UI option value of tags[], label in the UI)
DATASETS = {
    "all": ("", "Alle Standorte"),
    "hu": ("Kfz Prüfstelle", "Kfz Prüfstelle"),
}
# the form posts these on every search (hidden inputs); they define what "Alle Standorte" means
EXPECTED_FIXED_TAGS = ["Kfz Prüfstelle", "Managementsysteme", "MPU Begutachtung"]
EXPECTED_STATIC_FIELDS = {"language": "0", "address": ""}

PAUSE_S = (2.0, 4.0)  # between page actions, seconds (min, max)
NAV_TIMEOUT_MS = 60_000
SEARCH_TIMEOUT_MS = 60_000
BLOCK_STATUSES = (403, 429, 503)
BLOCK_MARKERS = ("just a moment", "cf-chl", "challenge-platform/h/b/orchestrate", "attention required", "cf-error")


class FetchError(RuntimeError):
    """The locator did not answer as expected (page changed, non-JSON, HTTP error ...)."""


class BlockedError(FetchError):
    """Cloudflare (or similar) answered with a block or a challenge page instead of the locator."""


@dataclass
class Capture:
    """Everything recorded for one dataset."""

    dataset: str
    tag_value: str
    ui_label: str
    request_method: str = ""
    request_url: str = ""
    request_content_type: str = ""
    request_fields: list = field(default_factory=list)  # [(name, value), ...] in the order sent
    request_body_bytes: int = 0
    ui_status: int = 0
    ui_content_type: str = ""
    ui_body: bytes = b""
    ui_counter: str = ""
    replay_status: int = 0
    replay_content_type: str = ""
    replay_body: bytes = b""
    replay_error: str = ""
    elapsed_s: float = 0.0


@dataclass
class FetchOutcome:
    captures: dict = field(default_factory=dict)  # dataset key -> Capture
    form_state: dict = field(default_factory=dict)  # select options, hidden fields, button text before the search
    detail_pages: list = field(default_factory=list)  # [{"url", "status", "html"}]
    access: dict = field(default_factory=dict)  # headless / headed, blocked?, consent, cookie names, user agent
    log: list = field(default_factory=list)  # one dict per request / page load


def parse_multipart(post_data: str | None) -> list[tuple[str, str]]:
    """The (name, value) pairs of a multipart/form-data body, in order. Empty list if it is not multipart."""
    if not post_data:
        return []
    out = []
    for part in re.split(r"\r?\n?--[-\w]+(?:--)?\r?\n?", post_data):
        m = re.match(r'\s*Content-Disposition:\s*form-data;\s*name="([^"]*)"\s*\r?\n\r?\n(.*)\Z', part, re.S)
        if m:
            out.append((m.group(1), m.group(2).rstrip("\r\n")))
    return out


def looks_blocked(status: int, headers: dict | None, body: bytes | str | None) -> str:
    """A reason string if this answer is a Cloudflare block / challenge, else ''."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if headers.get("cf-mitigated"):
        return f"cf-mitigated: {headers['cf-mitigated']}"
    head = body[:6000] if body else b""
    text = (head.decode("utf-8", "replace") if isinstance(head, bytes) else str(head)).lower()
    for marker in BLOCK_MARKERS:
        if marker in text:
            return f"block marker {marker!r} in the answer"
    if status in BLOCK_STATUSES:
        return f"HTTP {status}"
    return ""


def _pause(rng: random.Random) -> None:
    time.sleep(rng.uniform(*PAUSE_S))


def _log(outcome: FetchOutcome, what: str, url: str, status, started: float, note: str = "") -> None:
    outcome.log.append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "what": what, "url": url, "status": status,
                        "seconds": round(time.time() - started, 2), "note": note})


_REPLAY_JS = """async ({url, fields}) => {
  const fd = new FormData();
  for (const [k, v] of fields) fd.append(k, v);
  const r = await fetch(url, {method: 'POST', body: fd, credentials: 'same-origin'});
  const text = await r.text();
  return {status: r.status, contentType: r.headers.get('content-type') || '', cfMitigated: r.headers.get('cf-mitigated') || '', text};
}"""


def _load_page(page, outcome: FetchOutcome) -> None:
    started = time.time()
    resp = page.goto(LOCATOR_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    status = resp.status if resp else 0
    html = page.content()
    _log(outcome, "page", LOCATOR_URL, status, started)
    reason = looks_blocked(status, resp.headers if resp else {}, html)
    if reason:
        raise BlockedError(f"locator page blocked ({reason})")
    if status != 200:
        raise FetchError(f"locator page answered HTTP {status}")
    try:
        page.wait_for_selector(FORM, state="attached", timeout=20_000)
    except Exception as exc:  # TimeoutError of playwright
        raise FetchError("the search form (form.location-search-form) is not on the page: the page changed") from exc


def _decline_consent(page, outcome: FetchOutcome) -> str:
    try:
        page.wait_for_selector(CONSENT_DENY, state="visible", timeout=15_000)
    except Exception:
        outcome.access["consent"] = "no banner shown"
        return "no banner shown"
    page.locator(CONSENT_DENY).first.click()
    try:
        page.wait_for_selector(CONSENT_OVERLAY, state="hidden", timeout=10_000)
    except Exception as exc:
        raise FetchError("the cookie banner did not close after 'Nur erforderliche Cookies erlauben'") from exc
    outcome.access["consent"] = "declined ('Nur erforderliche Cookies erlauben')"
    return outcome.access["consent"]


def _read_form_state(page) -> dict:
    return page.evaluate(
        """() => {
          const f = document.querySelector('form.location-search-form');
          const sel = f.querySelector("select[name='tags[]']");
          return {
            action: f.getAttribute('action'), data_search_id: f.dataset.searchId, data_search_on_load: f.dataset.searchOnLoad,
            hidden: Array.from(f.querySelectorAll('input[type=hidden]')).map(i => [i.name, i.value]),
            text_inputs: Array.from(f.querySelectorAll('input[type=text]')).map(i => [i.name, i.value]),
            select_options: sel ? Array.from(sel.options).map(o => [o.value, o.textContent.trim()]) : [],
            button_text: f.querySelector('button[type=submit]').innerText.trim().replace(/\\s+/g, ' '),
          };
        }"""
    )


def _search_via_ui(page, cap: Capture, outcome: FetchOutcome, rng: random.Random) -> None:
    started = time.time()
    page.select_option(TYPE_SELECT, value=cap.tag_value)
    page.evaluate("sel => { document.querySelector(sel).textContent = ''; }", COUNTER)  # no stale counter
    requests_seen = []
    handler = lambda r: requests_seen.append(r) if "location_search" in r.url else None  # noqa: E731
    page.on("request", handler)
    try:
        with page.expect_response(lambda r: "location_search" in r.url, timeout=SEARCH_TIMEOUT_MS) as info:
            page.click(SUBMIT)
        resp = info.value
        body = resp.body()
    except Exception as exc:
        raise FetchError(f"UI search '{cap.ui_label}' got no answer from location_search: {exc}") from exc
    finally:
        page.remove_listener("request", handler)
    cap.ui_status = resp.status
    cap.ui_content_type = resp.headers.get("content-type", "")
    cap.ui_body = body
    req = resp.request
    cap.request_method, cap.request_url = req.method, req.url
    cap.request_content_type = re.sub(r"boundary=.*", "boundary=<…>", req.headers.get("content-type", ""))
    post = req.post_data or ""
    cap.request_body_bytes = len(req.post_data_buffer or b"")
    cap.request_fields = parse_multipart(post)
    _log(outcome, f"search_ui:{cap.dataset}", req.url, resp.status, started, f"{len(body)} bytes")
    reason = looks_blocked(resp.status, resp.headers, body)
    if reason:
        raise BlockedError(f"search '{cap.ui_label}' blocked ({reason})")
    try:  # the page script writes hits.hits.length into the counter once the answer is processed
        page.wait_for_function("sel => (document.querySelector(sel).textContent || '').trim() !== ''", arg=COUNTER,
                               timeout=15_000)
        cap.ui_counter = page.inner_text(COUNTER).strip()
    except Exception:
        cap.ui_counter = ""  # kept empty on purpose: the completeness check reports it, we do not guess


def _replay(page, cap: Capture, outcome: FetchOutcome) -> None:
    started = time.time()
    try:
        res = page.evaluate(_REPLAY_JS, {"url": cap.request_url or ENDPOINT_URL, "fields": cap.request_fields})
    except Exception as exc:
        cap.replay_error = f"replay failed: {exc}"
        _log(outcome, f"replay:{cap.dataset}", cap.request_url, 0, started, cap.replay_error)
        return
    cap.replay_status = int(res["status"])
    cap.replay_content_type = res["contentType"]
    cap.replay_body = res["text"].encode("utf-8")
    _log(outcome, f"replay:{cap.dataset}", cap.request_url, cap.replay_status, started, f"{len(cap.replay_body)} bytes")
    reason = looks_blocked(cap.replay_status, {"cf-mitigated": res.get("cfMitigated", "")}, cap.replay_body)
    if reason:
        cap.replay_error = f"replay blocked ({reason})"


def _detail(page, url: str, outcome: FetchOutcome) -> dict:
    started = time.time()
    resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    status = resp.status if resp else 0
    html = page.content()
    _log(outcome, "detail", url, status, started)
    reason = looks_blocked(status, resp.headers if resp else {}, html)
    if reason:
        raise BlockedError(f"detail page {url} blocked ({reason})")
    return {"url": url, "status": status, "html": html}


def _session(headless: bool, outcome: FetchOutcome, detail_urls: Callable[[dict], list[str]] | None,
             rng: random.Random) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(locale="de-DE", viewport={"width": 1400, "height": 1000})
            page = ctx.new_page()
            outcome.access.update(headless=headless, browser=f"chromium {browser.version}",
                                  user_agent=page.evaluate("navigator.userAgent"))
            _load_page(page, outcome)
            _decline_consent(page, outcome)
            outcome.access["cookie_names"] = sorted(c["name"] for c in ctx.cookies())  # names only, never values
            _pause(rng)
            outcome.form_state = _read_form_state(page)
            options = {value for value, _ in outcome.form_state["select_options"]}
            for key, (tag_value, label) in DATASETS.items():
                if tag_value not in options:
                    raise FetchError(f"the type list has no option for '{label}' (options: {sorted(options)}): page changed")
            for key, (tag_value, label) in DATASETS.items():
                cap = Capture(dataset=key, tag_value=tag_value, ui_label=label)
                started = time.time()
                _search_via_ui(page, cap, outcome, rng)
                _pause(rng)
                _replay(page, cap, outcome)
                cap.elapsed_s = round(time.time() - started, 1)
                outcome.captures[key] = cap
                _pause(rng)
            if detail_urls:
                for url in detail_urls(outcome.captures):
                    outcome.detail_pages.append(_detail(page, url, outcome))
                    _pause(rng)
        finally:
            browser.close()


def _call_outside_event_loop(fn, *args):
    """Playwright's sync API refuses to run in a thread that has an asyncio loop (a Jupyter kernel always has one).
    In that case the call runs in a short-lived worker thread; otherwise it runs directly."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args)

    def in_worker():
        # Jupyter on Windows sets the selector loop policy, which cannot start the browser process (NotImplementedError).
        # The proactor policy is used while the worker runs and the kernel's policy is put back afterwards.
        if sys.platform != "win32":
            return fn(*args)
        old = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        try:
            return fn(*args)
        finally:
            asyncio.set_event_loop_policy(old)

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(in_worker).result()


def fetch_all(headless: bool = True, fallback_headed: bool = True,
              detail_urls: Callable[[dict], list[str]] | None = None, seed: int | None = None) -> FetchOutcome:
    """Run the whole live session. Headless first; if that is blocked, one headed retry (when allowed).

    `detail_urls(captures) -> [absolute URL, ...]` picks the detail pages to open after the searches.
    """
    rng = random.Random(seed)
    outcome = FetchOutcome()

    def session(headless_: bool, target: FetchOutcome) -> FetchOutcome:
        try:
            _call_outside_event_loop(_session, headless_, target, detail_urls, rng)
        except FetchError as exc:
            exc.outcome = target  # the log and access notes of the failed attempt survive for the run summary
            raise
        except Exception as exc:  # playwright errors: browser missing, network down, navigation timeout ...
            err = FetchError(f"{type(exc).__name__}: {exc}")
            err.outcome = target
            raise err from exc
        target.access["blocked"] = False
        return target

    try:
        return session(headless, outcome)
    except BlockedError as exc:
        first = str(exc)
        if not (headless and fallback_headed):
            raise
        retry = FetchOutcome()
        retry.log = outcome.log + [{"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "what": "headless_blocked",
                                    "url": LOCATOR_URL, "status": "", "seconds": 0, "note": first}]
        retry.access["headless_blocked"] = first
        return session(False, retry)


def capture_summary(cap: Capture) -> dict:
    """JSON-safe description of a capture (without the bodies)."""
    return {
        "dataset": cap.dataset, "ui_label": cap.ui_label, "tags_value": cap.tag_value,
        "request": {"method": cap.request_method, "url": cap.request_url, "content_type": cap.request_content_type,
                    "body_bytes": cap.request_body_bytes, "fields": [list(f) for f in cap.request_fields]},
        "ui": {"status": cap.ui_status, "content_type": cap.ui_content_type, "bytes": len(cap.ui_body),
               "counter": cap.ui_counter},
        "replay": {"status": cap.replay_status, "content_type": cap.replay_content_type, "bytes": len(cap.replay_body),
                   "error": cap.replay_error},
        "elapsed_s": cap.elapsed_s,
    }


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
