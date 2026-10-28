import json
import re
from datetime import datetime

import pandas as pd
import pytest

from core import geocode, master_run, paths
from core import master_schema as ms
from master_fixtures import CENTROIDS, all_sources, source_row, write_source

T1 = datetime(2026, 10, 6, 12, 0)
T2 = datetime(2026, 11, 3, 9, 30)
CENT = {r.zip: (r.lat, r.lng) for r in CENTROIDS.itertuples()}


class CentroidSession:
    """Fake Nominatim: answers every query with the centroid of the asked PLZ (or nothing for PLZ 99999)."""

    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        plz = params.get("postalcode", "")

        class R:
            status_code = 200

            def json(self_inner):
                if plz not in CENT:
                    return []
                return [{"lat": str(CENT[plz][0]), "lon": str(CENT[plz][1]), "display_name": "x"}]

        return R()


def geocoder(root, session=None):
    return geocode.Geocoder(paths.geocode_cache_path(root), session=session or CentroidSession(), sleep=lambda s: None)


def rows(prefix, n, **kw):
    return [source_row("gtue", f"{prefix}-{i}", name=f"GTÜ {i}", street=f"Teststr{chr(97 + i)} {i + 1}", zip="01067", city="Dresden",
                       lat="51.06", lng="13.74", **kw) for i in range(n)]


def world(root, **extra):
    """A small synthetic world: SÜD/Hessen pairs (sign and address), a Bad Camberg trap, FSP/Rheinland pairs, FSP-only rows."""
    data = {
        "tuev_sued": [
            source_row("tuev_sued", "TUEV_SUED-1", name="TÜV SÜD Service-Center Marburg", street="Hauptstr. 5", zip="35037", city="Marburg",
                       lat="50.81", lng="8.77", location_sign="008", phone="111"),
            source_row("tuev_sued", "TUEV_SUED-2", name="TÜV SÜD Service-Center Bad Camberg", street="Am Bahnhof 1", zip="65520", city="Bad Camberg",
                       lat="50.296", lng="8.265", location_sign="5", operator_group="tuev_hessen"),
            source_row("tuev_sued", "TUEV_SUED-3", name="TÜV SÜD Service-Center München", street="Isarstr. 1", zip="80331", city="München",
                       lat="48.137", lng="11.575", location_sign="77"),
        ],
        "tuev_hessen": [
            source_row("tuev_hessen", "TUEV_HESSEN-1", name="TÜV Hessen Marburg", street="Hauptstraße 5", zip="35037", city="Marburg",
                       lat="50.8101", lng="8.7701", location_sign="008", phone="222", website="https://h"),
            source_row("tuev_hessen", "TUEV_HESSEN-2", name="TÜV Hessen Bad Camberg", street="Am Bahnhof 1", zip="65520", city="Bad Camberg",
                       lat="50.2000", lng="8.5800", location_sign="5"),
        ],
        "tuev_rheinland": [
            source_row("tuev_rheinland", "TUEV_RHEINLAND-1", name="Prüfstelle Eggesin", street="Am Grenzberg 7", zip="17367", city="Eggesin",
                       lat="53.69", lng="14.07", operator_group="fsp", site_type="fsp_partner"),
            source_row("tuev_rheinland", "TUEV_RHEINLAND-2", name="Prüfstelle Hannover", street="Davenstedter Straße 95", zip="", city="",
                       lat="52.35", lng="9.70", operator_group="fsp", site_type="fsp_partner", needs_review="True", needs_review_reason="no PLZ in address - to be filled from the FSP list in the aggregator; unknown PLZ"),
            source_row("tuev_rheinland", "TUEV_RHEINLAND-3", name="Prüfstelle Lage", street="Lindenallee 4", zip="32791", city="Lage",
                       lat="51.99", lng="8.79"),
        ],
        "fsp": [
            source_row("fsp", "FSP-eggesin", name="FSP Eggesin", street="Am Grenzberg 7c", zip="17367", city="Eggesin", phone="333"),
            source_row("fsp", "FSP-hannover", name="FSP Hannover", street="Davenstedter Str. 95", zip="30453", city="Hannover"),
            source_row("fsp", "FSP-neustrelitz", name="FSP Neustrelitz", street="Waldweg 3", zip="17235", city="Neustrelitz"),
        ],
    }
    data.update(extra)
    all_sources(root, extra=data)
    ov = paths.overrides_path(root)
    ov.parent.mkdir(parents=True, exist_ok=True)
    ov.write_text("location_id_a;location_id_b;decision;decided_by;date;note\n"
                  "TUEV_RHEINLAND-1;FSP-eggesin;same;Nikos;2026-10-06;house number letter\n", encoding="utf-8")


def go(root, mode="dev", at=T1, **kw):
    kw.setdefault("check_volume", False)  # the synthetic world is tiny; the volume gate has its own test
    kw.setdefault("hdp_removal_enabled", False)  # the HDP own-site removal has its own tests (test_hdp_removal.py)
    return master_run.run(mode, root=root, started_at=at, centroids=CENTROIDS, geocoder=kw.pop("geocoder", None) or geocoder(root), **kw)


@pytest.fixture
def root(tmp_path):
    world(tmp_path)
    return tmp_path


# ---- the happy path -------------------------------------------------------------------------------------------------
def test_end_to_end_merges_geocodes_and_conserves_rows(root):
    res = go(root)
    assert res.status == "PASS", res.summary["checks"]
    m = res.master
    s = res.summary
    assert s["rows_in_total"] == len(m) + s["merged_away"]
    by = {r.source_ids: r for r in m.itertuples()}
    # SÜD <-> Hessen by sign (Marburg) and by address with a bad Hessen coordinate (Bad Camberg)
    marburg = next(r for r in m.itertuples() if "TUEV_HESSEN-1" in r.source_ids)
    assert marburg.listed_by == "tuev_hessen|tuev_sued" and marburg.name == "TÜV Hessen Marburg" and marburg.coord_source == "tuev_sued"
    camberg = next(r for r in m.itertuples() if "TUEV_HESSEN-2" in r.source_ids)
    assert (camberg.lat, camberg.lng) == ("50.296000", "8.265000") and camberg.coord_source == "tuev_sued"
    # FSP <-> Rheinland: override (Eggesin), no-PLZ unique street (Hannover)
    eggesin = next(r for r in m.itertuples() if "FSP-eggesin" in r.source_ids)
    assert eggesin.listed_by == "fsp|tuev_rheinland" and eggesin.uo == "TÜV Rheinland" and eggesin.operator_group == "fsp"
    hannover = next(r for r in m.itertuples() if "FSP-hannover" in r.source_ids)
    assert hannover.zip == "30453" and hannover.state == "Niedersachsen" and "plz_missing" not in hannover.doubt_flags
    assert hannover.needs_review == "False" or "coords_unresolved" not in hannover.needs_review_reason
    # an FSP-only row without coordinates is geocoded, the source is named
    neu = next(r for r in m.itertuples() if "FSP-neustrelitz" in r.source_ids)
    assert neu.coord_source == "nominatim" and neu.lat != "" and neu.needs_review == "False"
    assert s["geocoding"]["rows_geocoded"] == 1 and s["geocoding"]["live_requests"] >= 1  # merged rows take the Rheinland coordinates
    assert set(m["site_type"]) <= {"own_station", "partner", "unknown"} and (m["uo"] != "").all()
    assert (m["country"] == "DE").all() and (m["is_hdp_site"] == "").all()
    assert "does not exist" in s["is_hdp_site"]["note"]


def test_the_run_folder_holds_every_file(root):
    res = go(root)
    names = {p.name for p in res.out_dir.iterdir()}
    assert {"pruefstellen_master_2026-10.csv", "diff_2026-10.csv", "merge_log.csv", "input_manifest.csv", "master_summary.json",
            "master_summary.md"} <= names
    assert {f"pruefstellen_extra_{s}_2026-10.csv" for s in ("gtue", "tuev_sued", "tuev_hessen", "fsp", "vuek")} <= names
    assert res.out_dir.parent.name == "_dev" and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}", res.out_dir.name)
    manifest = pd.read_csv(res.out_dir / "input_manifest.csv", sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert len(manifest) == 10 and manifest["sha256"].str.len().eq(64).all()
    assert json.loads((res.out_dir / "master_summary.json").read_text(encoding="utf-8"))["status"] == "PASS"
    assert "PASS" in (res.out_dir / "master_summary.md").read_text(encoding="utf-8")


def test_csv_format_bom_separator_columns_and_leading_zeros(root):
    res = go(root)
    path = res.out_dir / "pruefstellen_master_2026-10.csv"
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert text.splitlines()[0] == ";".join(ms.MASTER_COLUMNS)
    df = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert list(df.columns) == ms.MASTER_COLUMNS and len(df) == len(text.splitlines()) - 1  # no line break inside a field
    assert "01067" in set(df["zip"]) and ";01067;" in text
    assert not re.search(r";\d{5}\.0;", text) and not df["zip"].str.contains(r"\.").any()
    assert df.loc[df["lat"] != "", "lat"].str.fullmatch(r"-?\d+\.\d+").all()  # decimal point, no comma
    sign = pd.read_csv(res.out_dir / "pruefstellen_extra_tuev_sued_2026-10.csv", sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert "008" in set(sign["location_sign"]) and list(sign.columns[:2]) == ["master_id", "location_id"]


def test_spot_check_shows_every_contributing_source_row_side_by_side(root):
    res = go(root)
    mid = res.master[res.master["source_ids"].str.contains("TUEV_HESSEN-1")].iloc[0]["master_id"]
    view = master_run.spot_check(mid, res.out_dir, root=root)
    assert list(view.columns) == [f"MASTER {mid}", "TUEV_HESSEN-1", "TUEV_SUED-1"]
    assert view.loc["location_sign", "TUEV_SUED-1"] == "008"
    with pytest.raises(KeyError):
        master_run.spot_check("PS-999999", res.out_dir, root=root)


def test_merge_log_records_every_merge_and_the_dropped_values(root):
    res = go(root)
    log = pd.read_csv(res.out_dir / "merge_log.csv", sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    merges = log[log["action"] == "merge"]
    assert set(merges["rule"]) == {"location_sign", "override", "address_city"}
    assert len(merges) == res.summary["merged_away"]
    coords = log[(log["field"] == "lat/lng") & log["dropped"].str.contains("tuev_hessen")]
    assert coords["dropped"].str.contains(r"\(50\.20000,8\.58000\)").sum() == 1  # the wrong Bad Camberg coordinates are in the log
    assert list(log.columns) == master_run.LOG_COLUMNS


def test_running_twice_gives_identical_master_log_and_ids(root):
    a = go(root, at=T1)
    b = go(root, at=datetime(2026, 10, 6, 12, 5), geocoder=geocoder(root, CentroidSession()))
    assert b.summary["geocoding"]["live_requests"] == 0 and b.summary["geocoding"]["cache_hits"] > 0  # the cache made the rerun free
    for name in ("pruefstellen_master_2026-10.csv", "merge_log.csv"):
        assert (a.out_dir / name).read_bytes() == (b.out_dir / name).read_bytes()
    assert a.out_dir != b.out_dir


def test_same_minute_dev_runs_never_share_a_folder(root):
    a = go(root)
    b = go(root)
    assert a.out_dir != b.out_dir and b.out_dir.name.endswith("_2")


# ---- official runs, ids over months, the diff -------------------------------------------------------------------------------
def test_official_needs_confirm_and_never_overwrites(root):
    with pytest.raises(master_run.MasterRunError, match="confirm"):
        go(root, "official")
    res = go(root, "official", confirm=True)
    assert res.out_dir == paths.master_output_dir("2026-10", root) and res.status == "PASS"
    assert res.summary["diff"] == {"note": "no previous official master: headers only"}
    with pytest.raises(master_run.MasterRunError, match="already exists"):
        go(root, "official", confirm=True)


def test_second_month_keeps_ids_reports_changes_and_first_seen(root):
    first = go(root, "official", confirm=True)
    ids = dict(zip(first.master["source_ids"], first.master["master_id"]))
    # month 2: a phone changes, a site disappears, a new one appears
    sued = [source_row("tuev_sued", "TUEV_SUED-1", name="TÜV SÜD Service-Center Marburg", street="Hauptstr. 5", zip="35037", city="Marburg",
                       lat="50.81", lng="8.77", location_sign="008", phone="111"),
            source_row("tuev_sued", "TUEV_SUED-2", name="TÜV SÜD Service-Center Bad Camberg", street="Am Bahnhof 1", zip="65520", city="Bad Camberg",
                       lat="50.296", lng="8.265", location_sign="5", operator_group="tuev_hessen"),
            source_row("tuev_sued", "TUEV_SUED-9", name="TÜV SÜD Service-Center Hamburg", street="Elbstr. 1", zip="20095", city="Hamburg",
                       lat="53.55", lng="10.0", location_sign="99")]
    write_source(root, "tuev_sued", sued, run="2026-11-02_1000_full")  # München gone, Hamburg new
    gtue = [source_row("gtue", "GTUE-1", name="GTÜ Prüfstelle A", street="Hauptstr. 5", zip="01067", city="Dresden", lat="51.06", lng="13.74",
                       phone="0351 2")]
    write_source(root, "gtue", gtue, run="2026-11-02_1000_full")  # a phone number changes
    second = go(root, "official", at=T2, confirm=True)
    assert second.status == "PASS", second.summary["checks"]
    new_ids = dict(zip(second.master["source_ids"], second.master["master_id"]))
    marburg_old = next(v for k, v in ids.items() if "TUEV_SUED-1" in k)
    marburg_new = next(v for k, v in new_ids.items() if "TUEV_SUED-1" in k)
    assert marburg_old == marburg_new
    d = second.diff
    assert ("changed", "phone") in set(zip(d["change"], d["field"]))
    assert {"new", "retired"} <= set(d["change"]) and "disappeared" not in set(d["change"])  # a vanished site is retired, never dropped
    gone = second.master[second.master["status"] == "retired"]
    assert len(gone) == 1 and gone.iloc[0]["retired_on"] == "2026-11-03" and gone.iloc[0]["needs_review"] == "True"
    assert gone.iloc[0]["needs_review_reason"] == "retired" and gone.iloc[0]["master_id"] in set(first.master["master_id"])
    assert (second.master["needs_review"] == "True").sum() == 1  # nothing else is ever flagged
    assert second.master.set_index("master_id").loc[marburg_new, "first_seen"] == "2026-10-06"
    assert second.master.set_index("master_id").loc[marburg_new, "last_seen"] == "2026-11-03"
    assert second.out_dir == paths.master_output_dir("2026-11", root)
    assert not any(r for r in second.summary["checks"] if not r["ok"])


def test_a_source_that_shrinks_by_more_than_ten_percent_fails_the_run_and_marks_the_file(tmp_path):
    world(tmp_path, gtue=rows("G", 10))
    first = go(tmp_path, "official", confirm=True)
    assert first.status == "PASS"
    write_source(tmp_path, "gtue", rows("G", 5), run="2026-11-02_1000_full")
    second = go(tmp_path, "official", at=T2, confirm=True)
    assert second.status == "FAIL"
    assert any("10 %" in c["check"] and not c["ok"] and "gtue: 10 -> 5" in c["detail"] for c in second.summary["checks"])
    assert (second.out_dir / "pruefstellen_master_unverified_2026-11.csv").exists()
    assert not (second.out_dir / "pruefstellen_master_2026-11.csv").exists()
    assert paths.latest_official_master(tmp_path) == paths.master_output_dir("2026-10", tmp_path) / "pruefstellen_master_2026-10.csv"


# ---- failure paths: nothing fails silently -------------------------------------------------------------------------------------
def test_a_missing_source_fails_loudly_and_names_the_source(root):
    import shutil
    shutil.rmtree(paths.output_dir("vuek", "full", root=root))
    res = go(root)
    assert res.status == "FAIL" and "vuek" in res.summary["error"] and res.master is None
    assert "FAIL" in (res.out_dir / "master_summary.md").read_text(encoding="utf-8")


def test_a_source_without_a_passing_run_fails(root):
    write_source(root, "dekra", [source_row("dekra", "DEKRA-1", street="A 1", zip="20095", city="Hamburg")], run="2026-10-07_1000_full", status="FAIL")
    shutil_target = paths.output_dir("dekra", "full", root=root) / "2026-10-06_1000_full"
    import shutil
    shutil.rmtree(shutil_target)
    res = go(root)
    assert res.status == "FAIL" and "dekra" in res.summary["error"] and "not PASS" in res.summary["error"]


def test_schema_drift_is_a_hard_error_not_an_empty_column(tmp_path):
    world(tmp_path)
    write_source(tmp_path, "gtue", [source_row("gtue", "GTUE-1", street="Hauptstr. 5", zip="01067", city="Dresden")],
                 run="2026-10-07_1000_full", drop_columns=("phone",))
    res = go(tmp_path)
    assert res.status == "FAIL" and "lacks column" in res.summary["error"] and "phone" in res.summary["error"]


def test_a_new_column_is_a_warning_and_goes_to_the_extra_file(tmp_path):
    world(tmp_path)
    p = write_source(tmp_path, "gtue", [source_row("gtue", "GTUE-1", street="Hauptstr. 5", zip="01067", city="Dresden")], run="2026-10-07_1000_full")
    df = pd.read_csv(p, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df["brand_new"] = "x"
    df.to_csv(p, sep=";", index=False, encoding="utf-8-sig")
    res = go(tmp_path)
    assert res.status == "PASS" and any("brand_new" in w for w in res.summary["warnings"])
    extra = pd.read_csv(res.out_dir / "pruefstellen_extra_gtue_2026-10.csv", sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert "brand_new" in extra.columns


def test_an_unmapped_operator_group_is_a_hard_error(tmp_path):
    world(tmp_path, tuev_sued=[source_row("tuev_sued", "TUEV_SUED-1", street="A 1", zip="80331", city="München", operator_group="tuev_neu")])
    res = go(tmp_path)
    assert res.status == "FAIL" and "UO_MAP" in res.summary["error"] and "tuev_neu" in res.summary["error"]


def test_an_empty_or_duplicate_location_id_is_a_hard_error(tmp_path):
    world(tmp_path, gtue=[source_row("gtue", "GTUE-1", street="A 1", zip="01067", city="Dresden"),
                          source_row("gtue", "GTUE-1", street="B 2", zip="01067", city="Dresden")])
    res = go(tmp_path)
    assert res.status == "FAIL" and "not unique" in res.summary["error"]


def test_override_conflict_fails_instead_of_picking_a_side(root):
    ov = paths.overrides_path(root)
    ov.write_text("location_id_a;location_id_b;decision;decided_by;date;note\n"
                  "TUEV_SUED-1;TUEV_HESSEN-1;different;Nikos;2026-10-06;test\n"
                  "TUEV_SUED-1;TUEV_HESSEN-1;same;Nikos;2026-10-06;test\n", encoding="utf-8")
    res = go(root)
    assert res.status == "FAIL" and "contradicts itself" in res.summary["error"]


def test_row_conservation_failure_is_caught_and_the_file_is_marked_unverified(root, monkeypatch):
    real = master_run.dedupe.build

    def lossy(*a, **k):
        dd = real(*a, **k)
        for i in dd.comps[-1]:
            dd.comp_of[i] = 0
        dd.comps = dd.comps[:-1]
        return dd

    monkeypatch.setattr(master_run.dedupe, "build", lossy)
    res = go(root)
    assert res.status == "FAIL"
    failed = [c["check"] for c in res.summary["checks"] if not c["ok"]]
    assert any("row conservation" in c for c in failed) and any("exactly one master row" in c for c in failed)
    assert (res.out_dir / "pruefstellen_master_unverified_2026-10.csv").exists()


def test_out_of_scope_rows_are_excluded_and_counted_unless_the_switch_is_on(tmp_path):
    out = [source_row("gtue", "GTUE-1", name="A", street="Hauptstr. 5", zip="01067", city="Dresden", lat="51.06", lng="13.74"),
           source_row("gtue", "GTUE-2", name="B", street="Nebenstr. 6", zip="01067", city="Dresden", lat="51.07", lng="13.75", in_scope="False")]
    world(tmp_path, gtue=out)
    off = go(tmp_path)  # an in-scope export that holds out-of-scope rows is an anomaly: the rows are dropped AND the run fails
    assert off.status == "FAIL" and off.summary["excluded_out_of_scope"] == {"gtue": 1}
    assert any("contain no out-of-scope rows" in c["check"] and not c["ok"] and "gtue: 1" in c["detail"] for c in off.summary["checks"])
    assert not off.master["source_ids"].str.contains("GTUE-2").any()
    on = go(tmp_path, include_out_of_scope=True)
    row = on.master[on.master["source_ids"] == "GTUE-2"].iloc[0]
    assert on.status == "PASS" and row["in_scope"] == "False"


def test_unresolved_candidate_pair_becomes_a_review_row_on_both(tmp_path):
    a = source_row("dekra", "DEKRA-1", name="DEKRA A", street="Wallersheimer Weg 63-67", zip="20095", city="Hamburg", lat="53.5500", lng="10.0000")
    b = source_row("dekra", "DEKRA-2", name="DEKRA B", street="Wallersheimer Weg", zip="20095", city="Hamburg", lat="53.5501", lng="10.0001")
    world(tmp_path, dekra=[a, b])
    res = go(tmp_path)
    m = res.master
    both = m[m["source_ids"].isin(["DEKRA-1", "DEKRA-2"])]
    assert len(both) == 2 and (both["needs_review"] == "False").all() and both["doubt_flags"].str.contains("match_candidate_unresolved").all()
    assert both["doubt_flags"].str.contains("possible_duplicate_of").all()
    assert res.summary["doubt_flags_by_reason"]["match_candidate_unresolved"]["rows"] == 2 and res.summary["needs_review_rows"] == 0


def test_geocoding_stays_unresolved_and_flagged_if_nominatim_finds_nothing(tmp_path):
    world(tmp_path, fsp=[source_row("fsp", "FSP-x", name="X", street="Waldweg 3", zip="17235", city="Neustrelitz")])

    class Empty:
        def get(self, *a, **k):
            class R:
                status_code = 200

                def json(self):
                    return []
            return R()

    res = go(tmp_path, geocoder=geocoder(tmp_path, Empty()))
    row = res.master[res.master["source_ids"] == "FSP-x"].iloc[0]
    assert row["lat"] == "" and row["coord_source"] == "" and "coords_unresolved" in row["doubt_flags"] and row["needs_review"] == "False"
    assert res.summary["geocoding"]["rows_without_coordinates"] >= 1


def test_geocoding_cap_is_reported_and_leaves_rows_unresolved(tmp_path):
    many = [source_row("fsp", f"FSP-{i}", name=f"F{i}", street=f"Weg{chr(97 + i)} {i + 1}", zip="17235", city="Neustrelitz") for i in range(5)]
    world(tmp_path, fsp=many)
    g = geocode.Geocoder(paths.geocode_cache_path(tmp_path), session=CentroidSession(), sleep=lambda s: None, cap=2)
    res = go(tmp_path, geocoder=g)
    assert res.summary["geocoding"]["cap_reached"] and res.summary["geocoding"]["live_requests"] == 2
    assert res.master["doubt_flags"].str.contains("coords_unresolved").sum() >= 3 and res.summary["needs_review_rows"] == 0


def test_same_site_with_two_plz_is_merged_after_geocoding_and_never_reviewed(tmp_path):
    t = source_row("tuev_rheinland", "TUEV_RHEINLAND-9", name="Dresden", street="Österreicher Straße 95", zip="01309", city="Dresden",
                   lat="51.0400", lng="13.8000", operator_group="fsp", site_type="fsp_partner")
    f = source_row("fsp", "FSP-dresden", name="FSP Dresden", street="Österreicher Str. 95", zip="01279", city="Dresden")
    world(tmp_path, tuev_rheinland=[t], fsp=[f], tuev_sued=[], tuev_hessen=[])
    res = go(tmp_path)
    row = res.master[res.master["source_ids"].str.contains("FSP-dresden")]
    assert len(row) == 1 and row.iloc[0]["listed_by"] == "fsp|tuev_rheinland" and row.iloc[0]["needs_review"] == "False"
    assert "zip_conflict:01279/01309" in row.iloc[0]["doubt_flags"] and row.iloc[0]["zip"] == "01279"
    assert "address_proximity" in res.summary["merges"]["edges_by_rule"]
    assert res.status == "PASS"


def test_plz_correction_is_applied_and_listed(tmp_path):
    f = source_row("fsp", "FSP-meckenheim", name="FSP Meckenheim", street="Hauptstr. 9", zip="553340", city="Meckenheim")
    world(tmp_path, fsp=[f])
    res = go(tmp_path)
    row = res.master[res.master["source_ids"] == "FSP-meckenheim"].iloc[0]
    assert row["zip"] == "53340" and row["state"] == "Nordrhein-Westfalen"
    assert res.summary["plz_corrections"] == [{"slug": "fsp", "location_id": "FSP-meckenheim", "published": "553340", "corrected": "53340", "city": "Meckenheim"}]


def test_cross_family_shared_address_is_counted_not_merged(tmp_path):
    k = source_row("kues", "KUES-1", name="K", street="Hauptstraße 5", zip="01067", city="Dresden", lat="51.06", lng="13.74")
    world(tmp_path, kues=[k])
    res = go(tmp_path)
    assert res.summary["cross_family_shared_addresses"]["count_by_family_pair"] == {"gtue|kues": 1}
    both = res.master[(res.master["zip"] == "01067") & res.master["street"].str.contains("Hauptstr")]
    assert len(both) == 2 and both["doubt_flags"].str.contains("shared_address_other_uo").all()
    with_switch = go(tmp_path, merge_across_uo=True)
    assert with_switch.summary["merges"]["merged_away_by_family"]


# ---- review cycle 1: inputs must be fresh and complete -------------------------------------------------------------------------
def _stale_runs(root):
    """A newer GTÜ run that FAILED, next to the good one."""
    write_source(root, "gtue", [source_row("gtue", "GTUE-9", name="x", street="A 1", zip="01067", city="Dresden")],
                 run="2026-10-07_0900_full", status="FAIL")


def test_a_newer_rejected_run_is_reported_and_blocks_an_official_run(root):
    _stale_runs(root)
    dev = go(root)
    check = next(c for c in dev.summary["checks"] if "no newer run" in c["check"])
    assert not check["ok"] and "gtue" in check["detail"] and "2026-10-07_0900_full" in check["detail"] and check["severity"] == "review"
    assert dev.status == "PASS" and dev.summary["input_notes"]["gtue"]["newer_not_qualifying"]
    off = go(root, "official", confirm=True)
    assert off.status == "FAIL" and (off.out_dir / "pruefstellen_master_unverified_2026-10.csv").exists()


def test_a_pin_accepts_the_older_run_on_purpose(root):
    _stale_runs(root)
    res = go(root, "official", confirm=True, pins={"gtue": "2026-10-06_1000_full"})
    assert res.status == "PASS", [c for c in res.summary["checks"] if not c["ok"]]


def test_an_old_input_run_fails_an_official_run(root):
    res = go(root, "official", at=datetime(2026, 12, 20, 9, 0), confirm=True)
    assert res.status == "FAIL" and any("days old" in c["check"] and not c["ok"] for c in res.summary["checks"])


def test_rows_must_equal_the_in_scope_count_of_the_sources_own_summary(tmp_path):
    world(tmp_path)
    write_source(tmp_path, "gtue", rows("G", 3), run="2026-10-07_1000_full", summary_extra={"in_scope": 99})
    res = go(tmp_path)
    assert res.status == "FAIL" and any("own run_summary" in c["check"] and "gtue: file 3 vs its run_summary 99" in c["detail"] for c in res.summary["checks"])


def test_first_master_volume_gate_catches_a_truncated_or_empty_source(root):
    res = go(root, check_volume=True)  # the synthetic sources are tiny against the real anchors
    check = next(c for c in res.summary["checks"] if c["check"].startswith("first master"))
    assert res.status == "FAIL" and not check["ok"] and "scraper_status.md" in check["detail"]
    write_source(root, "tuev_thueringen", [], run="2026-10-07_1000_full")  # header only
    assert "tuev_thueringen: 0 rows vs 12" in next(c for c in go(root, check_volume=True).summary["checks"] if c["check"].startswith("first master"))["detail"]


# ---- review cycle 1: coordinates, ids, names, formats ---------------------------------------------------------------------------
class FixedSession:
    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon

    def get(self, url, params=None, headers=None, timeout=None):
        outer = self

        class R:
            status_code = 200

            def json(self_inner):
                return [{"lat": str(outer.lat), "lon": str(outer.lon), "display_name": "x"}]

        return R()


def test_a_far_source_coordinate_that_nominatim_confirms_is_kept_and_flagged(tmp_path):
    far = source_row("gtue", "GTUE-FAR", name="Landgemeinde", street="Dorfstr. 39", zip="01067", city="Dresden", lat="51.23", lng="13.74")  # ~19 km
    world(tmp_path, gtue=[far])
    res = go(tmp_path, geocoder=geocoder(tmp_path, FixedSession(51.2301, 13.7401)))
    row = res.master[res.master["source_ids"] == "GTUE-FAR"].iloc[0]
    assert row["lat"] == "51.230000" and row["coord_source"] == "gtue" and float(row["plz_distance_km"]) > 15
    assert "coords_far_from_plz_confirmed_by_geocoder" in row["doubt_flags"] and row["needs_review"] == "False"
    nothing = go(tmp_path, geocoder=geocoder(tmp_path / "other", FixedSession(48.0, 11.0)))  # a hit elsewhere does not confirm it
    assert nothing.master[nothing.master["source_ids"] == "GTUE-FAR"].iloc[0]["doubt_flags"].endswith("coords_unresolved")


def test_identical_coordinates_on_different_addresses_are_flagged(tmp_path):
    a = source_row("dekra", "DEKRA-A", name="A", street="Hauptstr. 1", zip="01067", city="Dresden", lat="51.06", lng="13.74")
    b = source_row("dekra", "DEKRA-B", name="B", street="Nebenstr. 9", zip="01067", city="Dresden", lat="51.06", lng="13.74")
    world(tmp_path, dekra=[a, b])
    res = go(tmp_path)
    flagged = res.master[res.master["doubt_flags"].str.contains("coords_shared_with_other_address")]
    assert {"DEKRA-A", "DEKRA-B"} <= set(flagged["source_ids"])  # (the fixture world has other rows on shared coordinates too)


def test_kues_uses_the_composed_display_name_not_the_salutation(tmp_path):
    k = source_row("kues", "KUES-9", street="Gewerbering 2", zip="80331", city="München", lat="48.137", lng="11.575", display_name="Prüfstelle Müller")
    k["name"] = "Herrn"
    world(tmp_path, kues=[k])
    res = go(tmp_path)
    assert res.master[res.master["source_ids"] == "KUES-9"].iloc[0]["name"] == "Prüfstelle Müller"
    extra = pd.read_csv(res.out_dir / "pruefstellen_extra_kues_2026-10.csv", sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert extra.loc[extra["location_id"] == "KUES-9", "name"].iloc[0] == "Herrn"  # the raw value is not lost


def test_psp_is_yes_for_kues_psp_rows_and_no_for_every_other_source(tmp_path):
    k_yes = source_row("kues", "KUES-Y", street="Gewerbering 2", zip="80331", city="München", lat="48.137", lng="11.575", psp="Yes")
    k_no = source_row("kues", "KUES-N", street="Ring 7", zip="20095", city="Hamburg", lat="53.55", lng="10.0")
    p = source_row("tuev_hessen", "TUEV_HESSEN-P", street="Lahnstr. 99", zip="35037", city="Marburg", lat="50.79", lng="8.75", site_type="partner")
    world(tmp_path, kues=[k_yes, k_no], tuev_hessen=[p])
    res = go(tmp_path)
    psp = dict(zip(res.master["source_ids"], res.master["psp"]))
    assert psp["KUES-Y"] == "Yes" and psp["KUES-N"] == "No"
    assert psp["TUEV_HESSEN-P"] == "No"  # a partner station is not a PSP
    assert set(res.master["psp"]) == {"Yes", "No"} and res.summary["master_by_psp"]["Yes"] == 1
    assert not any("psp" in w for w in res.summary["warnings"])  # a known column: no "new column" warning


def test_an_unknown_psp_value_is_a_hard_error(tmp_path):
    world(tmp_path, kues=[source_row("kues", "KUES-1", street="A 1", zip="80331", city="München", psp="maybe")])
    res = go(tmp_path)
    assert res.status == "FAIL" and "psp" in res.summary["error"]


def test_location_sign_is_a_required_column_of_sued_and_hessen(root):
    s = [source_row("tuev_sued", "TUEV_SUED-1", street="A 1", zip="80331", city="München")]
    write_source(root, "tuev_sued", s, run="2026-10-07_1000_full", drop_columns=("location_sign",))
    res = go(root)
    assert res.status == "FAIL" and "location_sign" in res.summary["error"]


def test_line_breaks_in_raw_text_never_break_the_csv_rows(tmp_path):
    h = source_row("tuev_hessen", "TUEV_HESSEN-9", name="H", street="Hauptstr. 5", zip="35037", city="Marburg", lat="50.81", lng="8.77",
                   infotext_text="line one\nline two\r\nline three")
    world(tmp_path, tuev_hessen=[h], tuev_sued=[])
    res = go(tmp_path)
    for p in res.out_dir.glob("*.csv"):
        text = p.read_bytes().decode("utf-8-sig")
        n = len(pd.read_csv(p, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig"))
        assert len(text.splitlines()) == n + 1, p.name
    assert "line one line two line three" in (res.out_dir / "pruefstellen_extra_tuev_hessen_2026-10.csv").read_text(encoding="utf-8-sig")


def test_the_registry_keeps_a_vanished_id_from_being_reissued_across_months(tmp_path):
    world(tmp_path, gtue=rows("G", 3))
    m1 = go(tmp_path, "official", confirm=True)
    write_source(tmp_path, "gtue", rows("G", 2) + [source_row("gtue", "GTUE-NEW", name="n", street="Neustr. 1", zip="01067", city="Dresden", lat="51.06", lng="13.74")],
                 run="2026-11-02_1000_full")
    m2 = go(tmp_path, "official", at=T2, confirm=True, check_volume=False)
    gone = set(m2.master.loc[m2.master["status"] == "retired", "master_id"])  # the vanished id stays in the master, retired
    new = set(m2.master["master_id"]) - set(m1.master["master_id"])
    assert len(gone) == 1 and len(new) == 1 and gone.isdisjoint(new) and gone <= set(m1.master["master_id"])
    assert m2.summary["max_master_id_issued"] == max(set(m2.master["master_id"]) | set(m1.master["master_id"]))
    write_source(tmp_path, "gtue", rows("G", 2), run="2026-12-02_1000_full")
    m3 = go(tmp_path, "official", at=datetime(2026, 12, 3, 9, 0), confirm=True, check_volume=False)
    assert set(m3.master["master_id"]) == set(m2.master["master_id"])  # nothing dropped, nothing reissued
    status = dict(zip(m3.master["master_id"], m3.master["status"]))
    assert all(status[i] == "retired" for i in gone | new)  # the first one was retired in month 2, the new one in month 3
    retired_on = dict(zip(m3.master["master_id"], m3.master["retired_on"]))
    assert {retired_on[i] for i in gone} == {"2026-11-03"} and {retired_on[i] for i in new} == {"2026-12-03"}


def test_a_malformed_overrides_file_or_previous_master_fails_with_a_summary_not_a_traceback(root):
    paths.overrides_path(root).write_text("location_id_a;location_id_b;decision;decided_by;date;note\nA;B;same;n;;x;y\n", encoding="utf-8")
    res = go(root)
    assert res.status == "FAIL" and "more fields" in res.summary["error"] and (res.out_dir / "master_summary.md").exists()


def test_offline_run_without_geonames_does_not_download(tmp_path):
    world(tmp_path)
    with pytest.raises(master_run.MasterRunError, match="DE.txt"):
        master_run.run("dev", root=tmp_path, started_at=T1, live_geocoding=False)


# ---- review cycle 2 ----------------------------------------------------------------------------------------------------------------
def test_a_rebuild_does_not_make_old_data_young(tmp_path):
    world(tmp_path)
    write_source(tmp_path, "gtue", rows("G", 3), run="2026-06-01_1000_full")
    write_source(tmp_path, "gtue", rows("G", 3), run="2026-10-05_1000_rebuild", summary_extra={"source_run": "2026-06-01_1000_full"})
    import shutil
    shutil.rmtree(paths.output_dir("gtue", "full", root=tmp_path) / "2026-10-06_1000_full")
    res = go(tmp_path, "official", confirm=True)
    assert res.summary["input_notes"]["gtue"]["age_days"] >= 120 and res.status == "FAIL"
    assert any("days old" in c["check"] and not c["ok"] for c in res.summary["checks"])
    manifest = pd.read_csv(res.out_dir / "input_manifest.csv", sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert manifest.loc[manifest["slug"] == "gtue", "data_started"].iloc[0].startswith("2026-06-01")


def test_the_id_registry_does_not_depend_on_the_summary_file(tmp_path, monkeypatch):
    monkeypatch.setattr(master_run, "MAX_INPUT_AGE_DAYS", 1000)  # the synthetic sources stay from October
    world(tmp_path, gtue=rows("G", 3))
    m1 = go(tmp_path, "official", confirm=True)
    write_source(tmp_path, "gtue", rows("G", 2), run="2026-11-02_1000_full")
    m2 = go(tmp_path, "official", at=T2, confirm=True)
    gone = m2.master.loc[m2.master["status"] == "retired", "master_id"].iloc[0]
    for p in paths.master_output_dir("2026-11", tmp_path).glob("master_summary.json"):
        p.unlink()  # the registry file is gone
    write_source(tmp_path, "gtue", rows("G", 2) + [source_row("gtue", "GTUE-NEW", name="n", street="Neustr. 1", zip="01067", city="Dresden", lat="51.06", lng="13.74")],
                 run="2026-12-02_1000_full")
    m3 = go(tmp_path, "official", at=datetime(2026, 12, 3, 9, 0), confirm=True)
    new_id = m3.master.loc[m3.master["source_ids"] == "GTUE-NEW", "master_id"].iloc[0]
    assert new_id != gone and m3.status == "PASS"  # the retired id is not handed out again
    assert m3.master.set_index("master_id").loc[gone, "status"] == "retired"


def test_a_street_less_row_with_an_unknown_plz_gets_both_reasons_and_the_run_still_passes(tmp_path):
    k = source_row("kues", "KUES-9", display_name="K", street="", zip="99999", city="Nirgendwo")
    world(tmp_path, kues=[k])
    res = go(tmp_path)
    row = res.master[res.master["source_ids"] == "KUES-9"].iloc[0]
    assert [x for x in row["doubt_flags"].split("; ") if x in ms.DOUBT_REASONS] == ["address_incomplete", "plz_invalid"]
    assert row["needs_review"] == "False" and res.status == "PASS"


def test_a_stale_override_blocks_an_official_run_and_is_a_review_item_in_dev(root):
    with open(paths.overrides_path(root), "a", encoding="utf-8") as fh:
        fh.write("GONE-1;GONE-2;same;Nikos;2026-10-06;the site closed\n")
    dev = go(root)
    check = next(c for c in dev.summary["checks"] if "manual decision" in c["check"])
    assert not check["ok"] and check["severity"] == "review" and dev.status == "PASS"
    assert go(root, "official", confirm=True).status == "FAIL"


def test_a_source_that_doubles_is_reported_after_the_first_master(tmp_path):
    world(tmp_path, gtue=rows("G", 3))
    go(tmp_path, "official", confirm=True)
    more = [source_row("gtue", f"GTUE-X{i}", name="n", street=f"Zusatzstr{chr(97 + i)} {i + 1}", zip="01067", city="Dresden",
                       lat="51.0600", lng=f"13.74{i}") for i in range(3)]
    write_source(tmp_path, "gtue", rows("G", 3) + more, run="2026-11-02_1000_full")
    res = go(tmp_path, "official", at=T2, confirm=True)
    assert any("more master rows" in c["check"] and not c["ok"] and "gtue: 3 -> 6" in c["detail"] for c in res.summary["checks"])


def test_a_truncated_cache_line_is_skipped_not_fatal(tmp_path):
    p = paths.geocode_cache_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text('{"key": "a@b@c", "result": null, "fetched_at": "2026-10-06T10:00:00"}\n{"key": "trunc', encoding="utf-8")
    g = geocode.Geocoder(p, session=CentroidSession(), sleep=lambda s: None)
    assert any("unreadable" in e for e in g.errors)


def test_a_failed_run_folder_with_a_suffix_is_still_seen(root):
    write_source(root, "gtue", [source_row("gtue", "GTUE-9", street="A 1", zip="01067", city="Dresden")], run="2026-10-07_0900_full_FAILED", status="FAIL")
    dev = go(root)
    check = next(c for c in dev.summary["checks"] if "no newer run" in c["check"])
    assert not check["ok"] and "FAILED" in check["detail"]


# ---- review cycle 3 ----------------------------------------------------------------------------------------------------------------
def test_an_unknown_in_scope_token_is_schema_drift_not_a_silently_dropped_source(tmp_path):
    rows_x = [source_row("dekra", f"DEKRA-{i}", name="n", street=f"Weg{chr(97 + i)} 1", zip="20095", city="Hamburg", lat="53.55", lng="10.0", in_scope="x")
              for i in range(3)]
    world(tmp_path, dekra=rows_x)
    res = go(tmp_path)
    assert res.status == "FAIL" and "in_scope value 'x'" in res.summary["error"]


def test_a_newer_failed_run_stays_visible_behind_a_rebuild_of_an_older_run(root):
    write_source(root, "gtue", rows("G", 2), run="2026-10-01_1000_full")
    write_source(root, "gtue", rows("G", 2), run="2026-10-06_0800_full", status="FAIL")
    write_source(root, "gtue", rows("G", 2), run="2026-10-06_0900_rebuild", summary_extra={"source_run": "2026-10-01_1000_full"})
    import shutil
    shutil.rmtree(paths.output_dir("gtue", "full", root=root) / "2026-10-06_1000_full")
    off = go(root, "official", confirm=True)
    assert off.status == "FAIL" and "2026-10-06_0800_full" in str(off.summary["input_notes"]["gtue"]["newer_not_qualifying"])


def test_an_official_run_for_an_older_month_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(master_run, "MAX_INPUT_AGE_DAYS", 1000)
    world(tmp_path)
    go(tmp_path, "official", at=T2, confirm=True)
    with pytest.raises(master_run.MasterRunError, match="later months"):
        go(tmp_path, "official", at=T1, confirm=True)


def test_an_empty_previous_master_is_not_a_baseline(tmp_path):
    world(tmp_path)
    d = paths.master_output_dir("2026-10", tmp_path)
    d.mkdir(parents=True)
    (d / "pruefstellen_master_2026-10.csv").write_text(";".join(ms.MASTER_COLUMNS) + "\n", encoding="utf-8-sig")
    res = go(tmp_path, "official", at=T2, confirm=True)
    assert res.status == "FAIL" and "no rows" in res.summary["error"]


def test_first_master_tolerance_is_tight_for_sources_without_a_run_summary(root):
    res = go(root, check_volume=True)
    detail = next(c["detail"] for c in res.summary["checks"] if c["check"].startswith("first master"))
    assert "tuev_rheinland" in detail and "tuev_thueringen" in detail  # synthetic rows against the anchors
    assert master_run.TIGHT_TOLERANCE < master_run.VOLUME_TOLERANCE
