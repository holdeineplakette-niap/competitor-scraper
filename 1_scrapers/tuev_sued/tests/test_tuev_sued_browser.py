"""TÜV SÜD browser side: the snippet text and the validation of the file it downloads.

The snippet is JavaScript and cannot be run here (no JS engine in the test environment), so these tests pin what it must
contain and what must stay out of it; the simulated browser (simulate.browser_download) is the Python twin of its logic
and produces the files that the import code is tested with. The first real pass is the first execution of the JS.
"""

import json
import re
from datetime import datetime

import pytest

from core import paths
from tuev_sued import browser, parse, simulate

FIXTURE = paths.fixtures_dir("tuev_sued") / "response_muenchen_80636_synthetic_2026-10-05.json"
START = datetime(2026, 10, 5, 12, 0)


def make_plan(zips=("80636", "24937", "01067"), pass_no=1):
    return browser.PassPlan(pass_no=pass_no, queries=[(z, "plan") for z in zips], mode="full")


@pytest.fixture(scope="module")
def answer():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class OneAnswerApi:
    """Stands in for the fake finder: every PLZ gets the fixture's answer."""

    def __init__(self, answer):
        self.answer = answer

    def query(self, plz):
        return self.answer


def download_text(plan, answer, **options):
    return simulate.browser_download(plan, OneAnswerApi(answer), START, **options)


def write(tmp_path, text, name="download.jsonl"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- snippet

def test_snippet_embeds_the_plz_list_and_the_pass_number():
    js = browser.render_snippet(make_plan(pass_no=2))
    plan = browser.PassPlan(2, [("80636", "plan")])
    assert '"pass": 2' in js and '"plz": ["80636", "24937", "01067"]' in js
    assert "pass 02 of the Pruefstellen sourcing" in js and "tuev_sued_pass02*.jsonl" in js
    assert browser.render_snippet(plan) != js


def test_snippet_has_the_hostname_guard_and_the_endpoint_only_once():
    js = browser.render_snippet(make_plan())
    assert 'location.hostname !== HOSTNAME' in js and f'"{browser.HOSTNAME}"' in js
    assert js.index("location.hostname !== HOSTNAME") < js.index("fetch(")  # the guard comes first
    assert js.count("fetch(") == 1 and js.count(browser.ENDPOINT) == 1
    assert 'searchTerm: plz, hu: 1' in js and 'credentials: "same-origin"' in js


def test_snippet_has_no_other_traffic_storage_or_scripts():
    js = browser.render_snippet(make_plan())
    for forbidden in ("XMLHttpRequest", "sendBeacon", "WebSocket", "localStorage", "sessionStorage", "document.cookie", "importScripts",
                      "eval(", "import(", "indexedDB", "navigator.", "<script"):
        assert forbidden not in js
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert re.findall(r"https?://[^\"'\s]+", code) == [browser.ENDPOINT]


def test_snippet_pacing_retry_stop_and_ceiling():
    js = browser.render_snippet(make_plan())
    assert "const PACE_MS = 5000, JITTER_MS = 2000, TIMEOUT_MS = 60000, RETRY_WAIT_MS = 30000;" in js
    assert "PACE_MS + Math.random() * JITTER_MS" in js and "controller.abort()" in js
    assert "window.tuevSuedStop" in js and 'reason: "user_stop"' in js
    assert "const ceiling = list.length;" in js and "sent >= ceiling" in js and "const MAX_PLZ = 200;" in js
    assert 'Array.isArray(d.finaltsc)' in js and "isNum(d.lat)" in js and "isNum(d.lon)" in js
    assert "first_200_chars" in js and "text.slice(0, 200)" in js
    assert "request_ceiling" in js and "stopped_reason: stopped" in js and "browser_summary.json" in js
    # a challenge stops at once (before the retry wait); anything else gets one retry
    assert js.index("looksBlocked(r)) { stopped") < js.index("await sleep(RETRY_WAIT_MS)")
    assert "[403, 429, 503].includes(r.status)" in js and "/html/i.test(r.type)" in js and "just a moment|challenge" in js
    assert js.count("await ask(plz)") == 2  # the request and its single retry


def test_snippet_downloads_one_jsonl_file_named_with_pass_date_and_time():
    js = browser.render_snippet(make_plan())
    assert '"tuev_sued_pass" + pad(PLAN.pass)' in js and '".jsonl"' in js
    assert "new Blob(" in js and "link.download = fileName" in js
    assert 'file":\' + JSON.stringify(name)' in js and 'name = "pass" + pad(PLAN.pass) + "_q" + String(i + 1).padStart(3, "0") + "_" + plz + ".json"' in js


def test_snippet_keeps_the_answer_as_received():
    js = browser.render_snippet(make_plan())
    assert "r.text.replace(" in js and "JSON.stringify(data" not in js  # the original text is embedded, not a re-serialised copy


def test_snippet_brackets_are_balanced():
    js = browser.render_snippet(make_plan())
    code = re.sub(r"//[^\n]*", "", js)
    code = re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', '""', code)  # string literals
    code = re.sub(r"/\[\\r\\n\]\+/g", "", code)  # the one regex literal
    for open_, close in ("()", "{}", "[]"):
        assert code.count(open_) == code.count(close), (open_, close)
    assert not re.search(r"__[A-Z_]+__", js)  # every placeholder was replaced


def test_snippet_header_has_the_instructions():
    js = browser.render_snippet(make_plan())
    assert js.startswith("// TUEV SUED HU finder - pass 01")
    assert "Open DevTools > Console" in js and "window.tuevSuedStop = true" in js and browser.PAGE_URL in js


def test_content_headers_are_only_the_body_type():
    assert browser.HEADERS == {"Content-Type": "application/json"}
    assert '{"Content-Type": "application/json"}' in browser.render_snippet(make_plan())


def test_snippet_limits_and_validation():
    with pytest.raises(ValueError):
        browser.render_snippet(browser.PassPlan(1, []))
    with pytest.raises(ValueError):
        browser.render_snippet(make_plan(zips=[f"{10000 + i}" for i in range(201)]))
    browser.render_snippet(make_plan(zips=[f"{10000 + i}" for i in range(200)]))  # exactly the ceiling is fine
    with pytest.raises(ValueError):
        browser.render_snippet(make_plan(zips=("8063",)))
    with pytest.raises(ValueError):
        browser.render_snippet(make_plan(zips=('80636"; alert(1); "',)))


def test_plan_roundtrips_through_the_written_file(tmp_path):
    plan = browser.PassPlan(3, [("80636", "plan"), ("01067", "fill")], mode="full")
    path = browser.write_snippet(tmp_path, plan)
    assert path.name == "tuev_sued_pass03.js" and plan.js_path == path
    back = browser.read_plan(path)
    assert (back.pass_no, back.queries, back.mode, back.snippet_version) == (3, plan.queries, "full", browser.SNIPPET_VERSION)
    with pytest.raises(FileExistsError):
        browser.write_snippet(tmp_path, plan)  # never over an existing file
    (tmp_path / "other.js").write_text("// nothing\n", encoding="utf-8")
    with pytest.raises(browser.ImportProblem):
        browser.read_plan(tmp_path / "other.js")


def test_pass_numbers_in_a_folder(tmp_path):
    for n in (1, 2):
        browser.write_snippet(tmp_path, make_plan(pass_no=n))
    (tmp_path / "raw_pass01.jsonl").write_text("", encoding="utf-8")
    assert browser.passes_planned(tmp_path) == [1, 2] and browser.passes_imported(tmp_path) == [1]


# ---- the downloaded file

def test_a_good_pass_file_is_imported_and_copied(tmp_path, answer):
    plan = make_plan()
    src = write(tmp_path, download_text(plan, answer))
    run = tmp_path / "run"
    run.mkdir()
    result = browser.import_pass(src, run, plan)
    assert result.pass_no == 1 and [n for n, _ in result.answers] == ["pass01_q001_80636.json", "pass01_q002_24937.json", "pass01_q003_01067.json"]
    assert result.summary["done"] == 3 and result.stopped_reason is None and not result.blocked
    assert (run / "raw_pass01.jsonl").read_bytes() == src.read_bytes()  # an exact copy
    with pytest.raises(browser.ImportProblem, match="imported before"):
        browser.import_pass(src, run, plan)


def test_a_bad_line_is_rejected_and_nothing_is_copied(tmp_path, answer):
    plan = make_plan()
    lines = download_text(plan, answer).splitlines()
    run = tmp_path / "run"
    run.mkdir()
    for bad, message in (
        ("not json at all", "not JSON"),
        (json.dumps({"file": "pass01_q001_80636.json"}), "file, response"),
        (json.dumps({"file": "pass01_q001_80636.json", "response": answer, "extra": 1}), "file, response"),
        (json.dumps([1, 2]), "file, response"),
    ):
        broken = write(tmp_path, "\n".join([bad] + lines[1:]) + "\n", "broken.jsonl")
        with pytest.raises(browser.ImportProblem, match=message):
            browser.import_pass(broken, run, plan)
    assert list(run.iterdir()) == []


def test_a_file_without_the_summary_line_is_incomplete(tmp_path, answer):
    plan = make_plan()
    cut = "\n".join(download_text(plan, answer).splitlines()[:-1]) + "\n"
    with pytest.raises(browser.ImportProblem, match="incomplete"):
        browser.read_download(write(tmp_path, cut), plan)
    with pytest.raises(browser.ImportProblem, match="empty"):
        browser.read_download(write(tmp_path, "\n\n", "empty.jsonl"), plan)
    with pytest.raises(browser.ImportProblem, match="not found"):
        browser.read_download(tmp_path / "missing.jsonl", plan)


def test_summary_must_belong_to_this_pass_and_this_plan(tmp_path, answer):
    plan = make_plan()
    other_pass = download_text(make_plan(pass_no=2), answer)
    with pytest.raises(browser.ImportProblem, match="is pass 2"):
        browser.read_download(write(tmp_path, other_pass), plan)
    longer = download_text(make_plan(zips=("80636", "24937", "01067", "10115")), answer)
    with pytest.raises(browser.ImportProblem, match="planned 4"):
        browser.read_download(write(tmp_path, longer), plan)
    text = download_text(plan, answer)
    summary = json.loads(text.splitlines()[-1])
    del summary["response"]["stopped_reason"]
    with pytest.raises(browser.ImportProblem, match="lacks 'stopped_reason'"):
        browser.read_download(write(tmp_path, "\n".join(text.splitlines()[:-1] + [json.dumps(summary)]) + "\n"), plan)


def test_answers_must_match_the_plan(tmp_path, answer):
    plan = make_plan()
    text = download_text(plan, answer)
    renamed = text.replace("pass01_q002_24937.json", "pass01_q002_99999.json")
    with pytest.raises(browser.ImportProblem, match="does not match the plan"):
        browser.read_download(write(tmp_path, renamed), plan)
    garbled = text.replace("pass01_q002_24937.json", "answer_two.json")
    with pytest.raises(browser.ImportProblem, match="unexpected answer name"):
        browser.read_download(write(tmp_path, garbled), plan)
    lines = text.splitlines()
    twice = "\n".join([lines[0], lines[0]] + lines[1:]) + "\n"
    with pytest.raises(browser.ImportProblem, match="twice"):
        browser.read_download(write(tmp_path, twice), plan)
    short = "\n".join(lines[:1] + lines[2:]) + "\n"  # an answer line is missing, the summary still says 3 done
    with pytest.raises(browser.ImportProblem, match="3 answers done, the file holds 2"):
        browser.read_download(write(tmp_path, short), plan)


def test_an_answer_with_the_wrong_shape_is_rejected(tmp_path, answer):
    plan = make_plan()
    lines = download_text(plan, answer).splitlines()
    wrong = json.dumps({"file": "pass01_q001_80636.json", "response": {"status": 0, "errormsg": "boom"}})
    with pytest.raises(browser.ImportProblem, match="no finaltsc list"):
        browser.read_download(write(tmp_path, "\n".join([wrong] + lines[1:]) + "\n"), plan)


def test_a_pass_that_stopped_early_keeps_what_it_collected(tmp_path, answer):
    plan = make_plan()
    result = browser.read_download(write(tmp_path, download_text(plan, answer, stop_after=2)), plan)
    assert len(result.answers) == 2 and result.stopped_reason["reason"] == "user_stop" and not result.blocked
    none = browser.read_download(write(tmp_path, download_text(plan, answer, stop_after=0), "none.jsonl"), plan)
    assert none.answers == [] and none.summary["done"] == 0


def test_a_dropped_answer_leaves_a_gap_that_the_plan_still_accepts(tmp_path, answer):
    plan = make_plan()
    result = browser.read_download(write(tmp_path, download_text(plan, answer, drop_answers={"24937"})), plan)
    assert [n for n, _ in result.answers] == ["pass01_q001_80636.json", "pass01_q003_01067.json"]


@pytest.mark.parametrize("reason, blocked", [
    ({"reason": "invalid_answer", "status": 403, "content_type": "text/html; charset=UTF-8", "first_200_chars": "<!DOCTYPE html><title>Just a moment...</title>"}, True),
    ({"reason": "invalid_answer", "status": 429, "content_type": "application/json", "first_200_chars": "{}"}, True),
    ({"reason": "invalid_answer", "status": 200, "content_type": "text/html", "first_200_chars": "<html>"}, True),
    ({"reason": "invalid_answer", "status": 200, "content_type": "application/json", "first_200_chars": "Just a moment"}, True),
    ({"reason": "invalid_answer", "status": 500, "content_type": "application/json", "first_200_chars": "{}"}, False),
    ({"reason": "invalid_answer", "status": 0, "content_type": "", "first_200_chars": "TypeError: Failed to fetch"}, False),
    ({"reason": "user_stop", "next_plz": "01067"}, False),
    ({"reason": "request_ceiling", "next_plz": "01067"}, False),
    ({"reason": "exception", "message": "boom"}, False),
    (None, False),
])
def test_blocks_are_told_from_other_stops(reason, blocked):
    assert browser.is_block(reason) is blocked


def test_a_challenged_pass_is_marked_blocked(tmp_path, answer):
    plan = make_plan()
    stop = {"reason": "invalid_answer", "plz": "24937", "status": 403, "content_type": "text/html", "first_200_chars": "Just a moment..."}
    result = browser.read_download(write(tmp_path, download_text(plan, answer, stop_after=1, stopped_reason=stop)), plan)
    assert result.blocked and len(result.answers) == 1


def test_a_raw_u2028_inside_an_answer_does_not_split_a_line(tmp_path, answer):
    plan = make_plan(zips=("80636",))
    answer = json.loads(json.dumps(answer))
    answer["finaltsc"][0]["location"]["notificationText"] = "vorher nachher"
    text = download_text(plan, answer).replace("\\u2028", " ")  # the snippet embeds the server's text as received
    assert " " in text
    result = browser.read_download(write(tmp_path, text), plan)
    assert result.answers[0][1]["finaltsc"][0]["location"]["notificationText"] == "vorher nachher"


def test_a_utf8_bom_is_tolerated(tmp_path, answer):
    plan = make_plan()
    path = tmp_path / "bom.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + download_text(plan, answer).encode("utf-8"))
    assert len(browser.read_download(path, plan).answers) == 3


def test_the_download_helper_parses_with_the_parse_module(tmp_path, answer):
    plan = make_plan()
    result = browser.read_download(write(tmp_path, download_text(plan, answer)), plan)
    ans = parse.to_answer(*result.answers[0])
    assert ans.count == 50 and ans.zip == "80636"
