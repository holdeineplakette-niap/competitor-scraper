"""Offline tests for fsp.browser: the console snippet in a real (headless) Chromium against the simulated fsp.de, and the
import of its download into an output/ full run. No request leaves the machine: every URL is answered by SimulatedFsp."""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from fsp import browser
from fsp import run as fsp_run
from fsp.fetch import BlockedError, FetchError
from fsp.simulate import EXPECTED_IN_SCOPE, SimulatedFsp

T0 = datetime(2026, 10, 30, 9, 21)


# ---------------------------------------------------------------- helpers
def python_raw_files(tmp_path: Path, fault=None) -> dict[str, str]:
    """The raw files a Python simulated run fetches (the reference the snippet must match)."""
    res = fsp_run.run("simulated", root=tmp_path / "py", now=T0, client=SimulatedFsp(fault), allow_robots_disallowed=True)
    out = {}
    for f in (res.run_dir / "raw").rglob("*"):
        if f.is_file():
            rel = f.relative_to(res.run_dir).as_posix()
            if rel.endswith(".gz"):
                out[rel[:-3]] = gzip.open(f, "rt", encoding="utf-8").read()
            else:
                out[rel] = f.read_text(encoding="utf-8")
    return out


def pack(path: Path, files: dict[str, str], **summary) -> Path:
    """A download as the snippet writes it."""
    s = {"snippet_version": browser.SNIPPET_VERSION, "started": T0.isoformat(), "finished": T0.isoformat(), "requests": len(files),
         "files": len(files), "complete": True, "stopped_reason": None, "user_agent": "test", **summary}
    lines = [json.dumps({"file": k, "url": f"https://www.fsp.de/x/{i}", "text": v}) for i, (k, v) in enumerate(files.items())]
    lines.append(json.dumps({"file": browser.SUMMARY_FILE, "response": s}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_snippet_in_browser(fault=None) -> tuple[str, str, SimulatedFsp]:
    """(file name, downloaded text, the fake site) after running the snippet in headless Chromium against SimulatedFsp."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    sim = SimulatedFsp(fault)

    def answer(route):
        u = urlsplit(route.request.url)
        if u.hostname != browser.HOSTNAME:
            return route.abort()  # nothing leaves the machine
        params = {k: int(v) if k == "page" else v for k, v in parse_qsl(u.query)}
        url = f"https://{u.hostname}{u.path}"
        if u.path == "/standorte" and not params:  # the page the person opens before pasting the snippet
            return route.fulfill(status=200, content_type="text/html", body="<html><body><h1>Prüfstellen & Standorte</h1></body></html>")
        try:
            r = sim.get(url, params)
        except BlockedError:
            return route.fulfill(status=403, content_type="text/html", body="<html><title>Just a moment...</title></html>")
        except FetchError:
            return route.fulfill(status=404, content_type="text/html", body="not found")
        kind = "text/plain" if u.path.endswith(".txt") else ("application/xml" if u.path.endswith(".xml") else "text/html")
        return route.fulfill(status=200, content_type=f"{kind}; charset=utf-8", body=r.text)

    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("run: python -m playwright install chromium")
            raise
        try:
            page = b.new_page(accept_downloads=True)
            page.route("**/*", answer)
            page.goto(browser.PAGE_URL)
            page.evaluate(browser.render_snippet(delay_range=(0, 0), backoff_s=(0, 0, 0)))
            page.wait_for_function("window.fspDone === true", timeout=60_000)
            name, text = page.evaluate("[window.fspFileName, window.fspResult]")
        finally:
            b.close()
    return name, text, sim


@pytest.fixture(scope="module")
def browser_download():
    return run_snippet_in_browser()


# ---------------------------------------------------------------- the snippet in a real browser
def test_snippet_fetches_exactly_what_fsp_run_fetches(browser_download, tmp_path):
    name, text, sim = browser_download
    assert browser.UPLOAD_RE.match(name)
    lines = [json.loads(x) for x in text.splitlines() if x.strip()]
    got = {x["file"]: x["text"] for x in lines[:-1]}
    ref = python_raw_files(tmp_path)
    assert set(got) == set(ref)  # same pages, same detail pages, same file names
    for k in ref:  # same content (the simulated pages carry no SVG to cut)
        assert got[k] == ref[k], k
    s = lines[-1]["response"]
    assert s["complete"] is True and s["stopped_reason"] is None
    assert s["requests"] == sim.requests_made == len(got)  # one request per file, no retries


def test_browser_download_imports_as_a_passing_full_run(browser_download, tmp_path):
    name, text, _ = browser_download
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    res = browser.import_download(f, root=tmp_path, say=lambda m: None)
    assert res.outcome == "PASS", res.summary["checks"]
    assert len(res.frame) == EXPECTED_IN_SCOPE
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}_full", res.run_dir.name)
    assert (res.run_dir / "raw" / "pages" / "hu_page_01.html.gz").exists()
    assert (res.run_dir / "raw" / "sitemap" / "sitemap.xml").exists()
    o = json.loads((res.run_dir / "run_summary.json").read_text(encoding="utf-8"))["origin"]
    assert o["fetched_by"] == "browser snippet" and o["upload_file"] == name
    assert browser.imported_before(browser.file_sha256(f), root=tmp_path) == res.run_dir


def test_snippet_stops_at_a_block_and_the_import_says_blocked(tmp_path):
    name, text, sim = run_snippet_in_browser(fault="blocked")
    s = json.loads(text.splitlines()[-1])["response"]
    assert s["complete"] is False and s["stopped_reason"]["reason"] == "blocked" and s["stopped_reason"]["status"] == 403
    assert sim.requests_made == s["requests"]  # stopped at the block: no retry of a 403
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    with pytest.raises(browser.Blocked):
        browser.import_download(f, root=tmp_path)
    assert not (tmp_path / "1_scrapers").exists()  # nothing written


def test_snippet_only_runs_on_fsp_de():
    assert 'location.hostname !== HOSTNAME' in browser.render_snippet()
    assert '"www.fsp.de"' in browser.render_snippet()


def test_snippet_cuts_inline_svgs_like_the_proof_on_real_pages():
    js = browser.render_snippet()
    assert r"t.replace(/<svg\b[\s\S]*?<\/svg>/gi, " in js  # the regex proven on the 2026-11-02 pages (identical CSV)


# ---------------------------------------------------------------- the import (no browser needed)
def test_import_of_the_python_files_passes(tmp_path):
    f = pack(tmp_path / "fsp_2026-10-30_0921.jsonl", python_raw_files(tmp_path))
    res = browser.import_download(f, root=tmp_path / "data", say=lambda m: None)
    assert res.outcome == "PASS" and len(res.frame) == EXPECTED_IN_SCOPE
    assert res.run_dir.name == "2026-10-30_0921_full"
    assert res.summary["requests_made"] == len(python_raw_files(tmp_path))


def test_a_lost_list_page_fails_the_run_like_a_live_run(tmp_path):
    f = pack(tmp_path / "fsp_2026-10-30_0921.jsonl", python_raw_files(tmp_path, fault="drop_hu_page"))
    res = browser.import_download(f, root=tmp_path / "data", say=lambda m: None)
    assert res.outcome == "FAIL"
    assert any(p.name.startswith("fsp_pruefstellen_unverified_") for p in res.run_dir.iterdir())


@pytest.mark.parametrize("change, message", [
    (lambda files: {**files, "raw/../../evil.html": "x"}, "unexpected file name"),
    (lambda files: {**files, "raw/pages/../../../x.html": "x"}, "unexpected file name"),
    (lambda files: {k: v for k, v in files.items() if k != "raw/sitemap/sitemap.xml"}, "lacks raw/sitemap/sitemap.xml"),
])
def test_bad_file_sets_are_refused(tmp_path, change, message):
    f = pack(tmp_path / "fsp_2026-10-30_0921.jsonl", change(python_raw_files(tmp_path)))
    with pytest.raises(browser.ImportProblem, match=re.escape(message)):
        browser.import_download(f, root=tmp_path / "data")
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("summary, message", [
    ({"snippet_version": "0"}, "snippet version 0"),
    ({"complete": False, "stopped_reason": {"reason": "user_stop"}}, "stopped before the end"),
    ({"started": "yesterday"}, "no valid start time"),
])
def test_bad_summaries_are_refused(tmp_path, summary, message):
    f = pack(tmp_path / "fsp_2026-10-30_0921.jsonl", python_raw_files(tmp_path), **summary)
    with pytest.raises(browser.ImportProblem, match=message):
        browser.read_download(f)


def test_truncated_download_is_refused(tmp_path):
    f = pack(tmp_path / "fsp_2026-10-30_0921.jsonl", python_raw_files(tmp_path))
    f.write_text("".join(f.read_text(encoding="utf-8").splitlines(keepends=True)[:-1]), encoding="utf-8")
    with pytest.raises(browser.ImportProblem, match="incomplete"):
        browser.read_download(f)
