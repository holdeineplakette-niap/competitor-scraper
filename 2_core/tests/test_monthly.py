"""core.monthly offline: fake source runs in a temporary root (plus one end-to-end run of every source's simulated mode)."""

import json
import shutil
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import master_fixtures as mf
from core import master_inputs as mi
from core import master_run, monthly, paths
from test_master_run import geocoder

OCT = datetime(2026, 10, 20, 10, 0)  # last month's runs
FRESH_SUED = datetime(2026, 10, 30, 9, 0)  # on the reminder day of the November cycle
NOV1 = datetime(2026, 11, 1, 2, 0)

ROWS = {
    "gtue": [mf.source_row("gtue", "GTUE-1", name="GTÜ Prüfstelle A", street="Hauptstr. 5", zip="01067", city="Dresden", lat="51.06", lng="13.74")],
    "kues": [mf.source_row("kues", "KUES-1", name="KÜS Prüfstelle", street="Gewerbering 2", zip="80331", city="München", lat="48.137", lng="11.575"),
             mf.source_row("kues", "KUES-9", name="KÜS HDP Berlin", street="Ringstr. 1", zip="10115", city="Berlin", lat="52.532", lng="13.385")],
    "dekra": [mf.source_row("dekra", "DEKRA-1", name="DEKRA Niederlassung", street="Industriestr. 9", zip="20095", city="Hamburg", lat="53.55", lng="10.0")],
    "fsp": [],
    "tuev_sued": [mf.source_row("tuev_sued", "TUEV_SUED-1", name="TÜV SÜD Service-Center Marburg", street="Hauptstr. 5", zip="35037",
                                city="Marburg", lat="50.81", lng="8.77", location_sign="008")],
    "tuev_nord": [mf.source_row("tuev_nord", "TUEV_NORD-1", name="TÜV NORD Station", street="Hafenstr. 1", zip="10115", city="Berlin",
                                latitude="52.532", longitude="13.385")],
    "tuev_rheinland": [],
    "tuev_hessen": [],
    "tuev_thueringen": [mf.source_row("tuev_thueringen", "TUEV_THUERINGEN-1", name="TÜV Thüringen Station", street="Lindenweg 3", zip="01067",
                                      city="Dresden", lat="51.061", lng="13.741")],
    "vuek": [mf.source_row("vuek", "VUEK-lage", name="VÜK Prüfstelle Lage", street="Sylbacher Str. 1", zip="32791", city="Lage", lat="51.99", lng="8.79")],
}


def write_run(root, slug, started, status="PASS"):
    """A finished run folder in output/ as the source's own run would leave it."""
    d = paths.new_run_dir(slug, "full", started, root)
    rows = ROWS[slug]
    name = mi.SOURCES[slug].in_scope_pattern.replace("*", f"{started:%Y-%m-%d}" + ("_unverified" if status != "PASS" else ""))
    pd.DataFrame(rows, columns=mf.header(slug)).to_csv(d / name, sep=";", index=False, encoding="utf-8-sig")
    key = "outcome" if slug == "fsp" else "status"
    s = {"mode": "full", key: status, "started_at": started.isoformat(), "in_scope": len(rows), "requests_made": 3}
    (d / paths.RUN_SUMMARY_JSON).write_text(json.dumps(s), encoding="utf-8")
    return d


def write_hdp(root, started, status="PASS"):
    d = paths.new_run_dir("hdp", "full", started, root)
    pd.DataFrame([{"source_id": "T1", "location_id": "HDP-T1", "street": "Ringstr. 1", "zip": "10115", "city": "Berlin", "lat": "52.532",
                   "lng": "13.385"}]).to_csv(d / "hdp_sites_x.csv", sep=";", index=False, encoding="utf-8-sig")
    s = {"mode": "full", "status": status, "started": started.isoformat(), "data_started": started.isoformat(), "n_sites": 1,
         "written": {"sites_csv": {"file": "hdp_sites_x.csv", "rows": 1}}, "requests_made": 1, "error": "" if status == "PASS" else "fake failure"}
    (d / paths.RUN_SUMMARY_JSON).write_text(json.dumps(s), encoding="utf-8")
    return d


class Crash(BaseException):
    """A crash of the whole job (not a source failure)."""


class Fakes:
    def __init__(self, fail=(), crash=(), hdp_status="PASS"):
        self.fail, self.crash, self.hdp_status = set(fail), set(crash), hdp_status
        self.calls = []

    def runners(self):
        return {slug: self._make(slug) for slug in monthly.SCRAPE_ORDER}

    def _make(self, slug):
        def fn(mode, root, started, centroids):
            assert mode == "full"
            self.calls.append(slug)
            if slug in self.crash:
                raise Crash()
            if slug == "hdp":
                write_hdp(root, started, self.hdp_status)
            else:
                write_run(root, slug, started, "FAIL" if slug in self.fail else "PASS")
        return fn


class Clock:
    """Every reading is one minute later: run folders never share a minute."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        self.t += timedelta(minutes=1)
        return self.t


def seed(root, fresh_sued=False):
    """Last month's passing run of every source (and a TÜV SÜD run of this cycle if asked)."""
    for slug in mi.SOURCE_ORDER:
        write_run(root, slug, OCT)
    if fresh_sued:
        write_run(root, "tuev_sued", FRESH_SUED)


def make_ctx(root, fakes, at=NOV1, centroids=None, **kw):
    return monthly.Context(root=root, clock=Clock(at), isolation="inline", runners=fakes.runners(), centroids=centroids, say=lambda m: None,
                           master_kwargs={"centroids": mf.CENTROIDS if centroids is None else centroids, "geocoder": geocoder(root),
                                          "check_volume": False, "live_geocoding": False}, **kw)


def on_disk(root, month="2026-11"):
    return json.loads((paths.monthly_dir(month, root) / paths.MONTHLY_STATUS_JSON).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv(monthly.GTUE_KEY, "dummy-for-tests")
    monkeypatch.delenv(paths.DATA_ROOT_ENV, raising=False)


# ---- cycle rules --------------------------------------------------------------------------------------------------
def test_reminder_day_and_cycle_month():
    assert monthly.reminder_day("2026-11") == date(2026, 10, 30)  # 31 Oct 2026 is a Saturday
    assert monthly.reminder_day("2026-12") == date(2026, 11, 30)
    assert monthly.cycle_month(date(2026, 10, 29)) == "2026-10"
    assert monthly.cycle_month(date(2026, 10, 30)) == "2026-11"
    assert monthly.cycle_month(date(2026, 11, 3)) == "2026-11"


def test_a_tuev_sued_run_before_the_reminder_day_is_not_fresh(tmp_path):
    write_run(tmp_path, "tuev_sued", datetime(2026, 10, 29, 18, 0))
    since = datetime(2026, 10, 30)
    assert monthly.fresh_tuev_sued(tmp_path, since) is None
    write_run(tmp_path, "tuev_sued", FRESH_SUED)
    assert monthly.fresh_tuev_sued(tmp_path, since).run_dir.name == "2026-10-30_0900_full"


# ---- without --confirm --------------------------------------------------------------------------------------------
def test_without_confirm_every_command_prints_its_plan_and_changes_nothing(tmp_path, monkeypatch):
    seed(tmp_path)
    fakes = Fakes()
    said = []
    ctx = make_ctx(tmp_path, fakes)
    ctx.say = said.append
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    assert monthly.scrape(ctx).code == 0
    assert monthly.build(ctx).code == 0
    for action in ("plan", "check", "build"):
        assert monthly.tuev_sued(ctx, action).code == 0
    assert monthly.status(ctx).code in (0, 1)
    monkeypatch.setenv(paths.DATA_ROOT_ENV, str(tmp_path))
    assert monthly.main(["scrape"]) == 0 and monthly.main(["build"]) == 0 and monthly.main(["status"]) in (0, 1)
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before
    assert fakes.calls == []
    text = "\n".join(said)
    assert "gtue" in text and "~900 (cap 2,000)" in text and "Nothing was sent" in text and "Nothing was written" in text


# ---- the three trigger moments ------------------------------------------------------------------------------------
def test_scrape_with_a_fresh_tuev_sued_run_builds_in_the_same_run(tmp_path):
    seed(tmp_path, fresh_sued=True)
    fakes = Fakes()
    out = monthly.scrape(make_ctx(tmp_path, fakes), confirm=True)
    assert fakes.calls == list(monthly.SCRAPE_ORDER)
    s = on_disk(tmp_path)
    assert s["phase"] == "built" and s["build"]["status"] == "PASS", s["needs_human"]
    assert out.code == 0 and s["exit_code"] == 0 and s["needs_human"] == []
    assert paths.master_output_dir("2026-11", tmp_path).is_dir()
    assert (tmp_path / s["build"]["hubspot_import"]).exists()  # first master in this root: the import file
    assert s["build"]["tuev_sued_run"] == "2026-10-30_0900_full" and s["build"]["pins"] == {}
    assert all(r["status"] == "PASS" for r in s["scrape"]["sources"].values())
    assert (paths.monthly_dir("2026-11", tmp_path) / paths.MONTHLY_STATUS_MD).read_text(encoding="utf-8").startswith("# Monthly run 2026-11")


def test_scrape_without_tuev_sued_waits_and_the_tuev_sued_step_builds(tmp_path, centroids):
    from tuev_sued import browser, simulate

    seed(tmp_path)
    fakes = Fakes()
    ctx = make_ctx(tmp_path, fakes, centroids=centroids)
    out = monthly.scrape(ctx, confirm=True)
    assert out.code == 0 and out.status["phase"] == "waiting_for_tuev_sued"
    assert not paths.master_output_dir("2026-11", tmp_path).exists()
    # the late upload on the 2nd: plan -> fake browser downloads -> check until PASS (Python sends no request to TÜV SÜD)
    ctx.clock = Clock(datetime(2026, 11, 2, 9, 0))
    assert monthly.tuev_sued(ctx, "plan", confirm=True).status["phase"] == "tuev_sued_waiting_for_upload"
    api = simulate.FakeTuevApi(simulate.simulated_sites(centroids), centroids)
    for _ in range(6):
        s = on_disk(tmp_path)
        if s["tuev_sued"]["status"] not in ("WAITING_FOR_UPLOAD", "NEXT_PASS"):
            break
        p = browser.read_plan(tmp_path / s["tuev_sued"]["snippet"])
        f = tmp_path / f"upload_{p.pass_no}.jsonl"
        f.write_text(simulate.browser_download(p, api, ctx.now()), encoding="utf-8")
        out = monthly.tuev_sued(ctx, "check", confirm=True, file=f)
    s = on_disk(tmp_path)
    assert s["tuev_sued"]["status"] == "PASS"
    assert s["phase"] == "built" and s["build"]["status"] == "PASS", s["needs_human"]
    assert s["build"]["tuev_sued_run"].startswith("2026-11-02")


def test_scrape_and_tuev_sued_finishing_together_build_exactly_once(tmp_path, monkeypatch):
    seed(tmp_path)
    ctx = make_ctx(tmp_path, Fakes())
    assert monthly.scrape(ctx, confirm=True).status["phase"] == "waiting_for_tuev_sued"
    write_run(tmp_path, "tuev_sued", FRESH_SUED)
    real, calls = master_run.run, []

    def slow(*a, **k):
        calls.append(1)
        time.sleep(0.5)
        return real(*a, **k)

    monkeypatch.setattr(master_run, "run", slow)
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(monthly.build(ctx, confirm=True).code)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and codes == [0, 0]
    assert monthly.build(ctx, confirm=True).message == "already built" and len(calls) == 1
    assert on_disk(tmp_path)["phase"] == "built"


def test_without_tuev_sued_after_the_timeout(tmp_path):
    seed(tmp_path)
    ctx = make_ctx(tmp_path, Fakes())
    monthly.scrape(ctx, confirm=True)
    (paths.output_dir("tuev_sued", "full", tmp_path) / "2026-10-30_0900_full").mkdir()  # the open run Nikos never uploaded to
    out = monthly.build(ctx, confirm=True)
    assert out.code == 0 and out.message == "waiting for TÜV SÜD"
    out = monthly.build(ctx, confirm=True, without_tuev_sued=True)
    s = on_disk(tmp_path)
    assert s["build"]["status"] == "PASS" and s["build"]["pins"] == {"tuev_sued": "2026-10-20_1000_full"}
    assert any(x.startswith("TÜV SÜD not refreshed") for x in s["needs_human"])
    assert out.code == 1


# ---- failures -----------------------------------------------------------------------------------------------------
def test_one_failing_source_is_retried_once_and_the_build_still_runs(tmp_path):
    seed(tmp_path, fresh_sued=True)
    fakes = Fakes(fail={"dekra"})
    out = monthly.scrape(make_ctx(tmp_path, fakes), confirm=True)
    s = on_disk(tmp_path)
    assert fakes.calls.count("dekra") == 2 and s["scrape"]["sources"]["dekra"]["attempts"] == 2
    assert s["scrape"]["sources"]["dekra"]["status"] == "FAIL"
    assert s["build"]["status"] == "PASS" and s["build"]["pins"] == {"dekra": "2026-10-20_1000_full"}
    assert any(x.startswith("DEKRA: run failed") for x in s["needs_human"])
    assert any(x.startswith("DEKRA: not refreshed") for x in s["needs_human"])
    assert out.code == 1


def test_a_failing_hdp_run_stops_the_build_with_exit_2(tmp_path):
    seed(tmp_path, fresh_sued=True)
    fakes = Fakes(hdp_status="FAIL")
    out = monthly.scrape(make_ctx(tmp_path, fakes), confirm=True)
    assert fakes.calls.count("hdp") == 3  # first run, the retry at the end of the scrape, the build's one rerun
    s = on_disk(tmp_path)
    assert out.code == 2 and s["exit_code"] == 2 and s["phase"] == "stopped"
    assert s["build"]["status"] == "STOPPED" and "HDP" in s["build"]["reason"]
    assert not paths.master_output_dir("2026-11", tmp_path).exists()


def test_an_input_older_than_45_days_behind_a_failed_run_stops_the_build(tmp_path):
    seed(tmp_path, fresh_sued=True)
    shutil.rmtree(paths.output_dir("vuek", "full", tmp_path))
    write_run(tmp_path, "vuek", datetime(2026, 9, 10, 10, 0))
    out = monthly.scrape(make_ctx(tmp_path, Fakes(fail={"vuek"})), confirm=True)
    s = on_disk(tmp_path)
    assert out.code == 2 and s["build"]["status"] == "STOPPED" and "VÜK" in s["build"]["reason"]


def test_a_master_error_is_recorded_and_stops_with_exit_2(tmp_path, monkeypatch):
    seed(tmp_path, fresh_sued=True)

    def boom(*a, **k):
        raise master_run.MasterRunError("simulated")

    monkeypatch.setattr(master_run, "run", boom)
    out = monthly.scrape(make_ctx(tmp_path, Fakes()), confirm=True)
    assert out.code == 2 and out.status["build"]["status"] == "STOPPED" and "simulated" in out.status["build"]["reason"]


def test_resume_after_a_crash_skips_the_sources_that_passed(tmp_path):
    seed(tmp_path, fresh_sued=True)
    with pytest.raises(Crash):
        monthly.scrape(make_ctx(tmp_path, Fakes(crash={"dekra"})), confirm=True)
    done = list(monthly.SCRAPE_ORDER[:monthly.SCRAPE_ORDER.index("dekra")])
    assert [r["status"] for r in on_disk(tmp_path)["scrape"]["sources"].values()] == ["PASS"] * len(done)
    fakes = Fakes()
    out = monthly.scrape(make_ctx(tmp_path, fakes, at=NOV1 + timedelta(hours=1)), confirm=True)
    assert fakes.calls == list(monthly.SCRAPE_ORDER[len(done):])
    s = on_disk(tmp_path)
    assert all(s["scrape"]["sources"][x].get("skipped") for x in done)
    assert out.code == 0 and s["build"]["status"] == "PASS"


def test_force_reruns_one_source(tmp_path):
    seed(tmp_path, fresh_sued=True)
    monthly.scrape(make_ctx(tmp_path, Fakes()), confirm=True)
    fakes = Fakes()
    out = monthly.scrape(make_ctx(tmp_path, fakes, at=NOV1 + timedelta(hours=1)), confirm=True, force=["dekra"])
    assert fakes.calls == ["dekra"] and out.message == "already built"
    with pytest.raises(ValueError):
        monthly.scrape(make_ctx(tmp_path, fakes), confirm=True, force=["nope"])


def test_a_missing_gtue_key_fails_gtue_cleanly_and_the_others_run(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(monthly.GTUE_KEY)
    seed(tmp_path, fresh_sued=True)
    fakes = Fakes()
    out = monthly.scrape(make_ctx(tmp_path, fakes), confirm=True)
    s = on_disk(tmp_path)
    assert "gtue" not in fakes.calls and len(fakes.calls) == len(monthly.SCRAPE_ORDER) - 1
    g = s["scrape"]["sources"]["gtue"]
    assert g["status"] == "FAIL" and monthly.GTUE_KEY in g["error"] and g["attempts"] == 1
    assert s["build"]["status"] == "PASS" and [x["slug"] for x in s["build"]["not_refreshed"]] == ["gtue"]
    assert out.code == 1


def _sleepy(mode, root, started, centroids):
    time.sleep(120)


def _vuek_pass(mode, root, started, centroids):
    write_run(root, "vuek", started)


def test_each_source_runs_in_its_own_process_with_a_timeout(tmp_path):
    ctx = monthly.Context(root=tmp_path, clock=Clock(NOV1), isolation="process", runners={"vuek": _sleepy}, timeouts={"vuek": 3},
                          say=lambda m: None)
    rec = monthly.run_source(ctx, "vuek", "2026-11")
    assert rec["status"] == "FAIL" and rec["error"].startswith("timeout")
    ctx.runners = {"vuek": _vuek_pass}
    rec = monthly.run_source(ctx, "vuek", "2026-11")
    assert rec["status"] == "PASS" and rec["in_scope"] == 1 and rec["error"] == ""


# ---- status and data root -----------------------------------------------------------------------------------------
def test_status_is_rebuilt_from_disk(tmp_path):
    seed(tmp_path, fresh_sued=True)
    ctx = make_ctx(tmp_path, Fakes())
    monthly.scrape(ctx, confirm=True)
    d = paths.monthly_dir("2026-11", tmp_path)
    for f in d.iterdir():
        f.unlink()
    monthly.status(ctx, month="2026-11")
    assert list(d.iterdir()) == []
    out = monthly.status(ctx, confirm=True, month="2026-11")
    s = on_disk(tmp_path)
    assert s["phase"] == "built" and s["build"]["status"] == "PASS" and out.code == 0
    assert all(s["scrape"]["sources"][x]["status"] == "PASS" for x in monthly.SCRAPE_ORDER)


def test_the_data_root_comes_from_the_environment(tmp_path, monkeypatch):
    code = paths.code_root()
    assert paths.project_root() == code
    monkeypatch.setenv(paths.DATA_ROOT_ENV, str(tmp_path))
    assert paths.project_root() == tmp_path
    assert paths.output_dir("gtue", "full") == tmp_path / "1_scrapers" / "gtue" / "output"
    assert paths.monthly_dir("2026-11") == tmp_path / "3_master_output" / "_monthly" / "2026-11"
    assert paths.fixtures_dir("gtue") == code / "1_scrapers" / "gtue" / "tests" / "fixtures"  # code stays in the code folder
    assert monthly.Context().root == tmp_path
    with pytest.raises(ValueError):
        monthly.Context(simulated=True)  # never fake runs into the data root


# ---- every source's simulated mode, end to end ---------------------------------------------------------------------
def test_simulated_end_to_end(tmp_path, centroids):
    with pytest.raises(ValueError):
        monthly.Context(root=paths.code_root(), simulated=True)
    ctx = monthly.Context(root=tmp_path, simulated=True, clock=Clock(NOV1), centroids=centroids, isolation="inline", say=lambda m: None,
                          master_kwargs={"centroids": centroids})
    out = monthly.scrape(ctx, confirm=True)
    s = out.status
    assert {k: r["status"] for k, r in s["scrape"]["sources"].items()} == {k: "PASS" for k in monthly.SCRAPE_ORDER}
    assert s["phase"] == "waiting_for_tuev_sued" and out.code == 0
    out = monthly.tuev_sued_simulated(ctx)
    s = on_disk(tmp_path)
    assert s["simulated"] and s["tuev_sued"]["status"] == "PASS"
    assert s["build"]["status"] == "PASS", s["build"].get("failed_checks")
    assert s["build"]["master_dir"].startswith("3_master_output/_dev/")
    assert not paths.master_output_dir("2026-11", tmp_path).exists()
    assert monthly.build(ctx, confirm=True).message == "already built"
