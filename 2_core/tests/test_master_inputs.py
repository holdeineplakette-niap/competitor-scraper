import pytest

from core import master_inputs as mi
from master_fixtures import source_row, write_source


def _row(i="DEKRA-1"):
    return [source_row("dekra", i, street="Hauptstr. 5", zip="01067", city="Dresden")]


def test_newest_passing_full_run_wins(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1000_full")
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1200_full")
    assert mi.select_run("dekra", tmp_path).run_dir.name == "2026-10-05_1200_full"


def test_a_newer_failed_run_is_never_picked(tmp_path):
    """The GTÜ baseline selector once picked a FAIL run."""
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1000_full")
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1500_full", status="FAIL")
    c = mi.select_run("dekra", tmp_path)
    assert c.run_dir.name == "2026-10-05_1000_full"
    assert any("not PASS" in x.why for x in mi.candidates("dekra", tmp_path))


def test_a_run_without_summary_does_not_qualify_for_summary_sources(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1000_full", status=None)
    with pytest.raises(LookupError, match="no run_summary"):
        mi.select_run("dekra", tmp_path)


def test_marked_export_files_are_not_used(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1000_full", file_tag="_unverified")
    with pytest.raises(LookupError, match="marked"):
        mi.select_run("dekra", tmp_path)
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1100_full", file_tag="_grid-incomplete")
    with pytest.raises(LookupError):
        mi.select_run("dekra", tmp_path)


def test_rebuild_counts_only_if_its_source_run_passed(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1000_full", status="FAIL")
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1100_rebuild", summary_extra={"source_run": "2026-10-05_1000_full"})
    with pytest.raises(LookupError, match="does not pass"):
        mi.select_run("dekra", tmp_path)


def test_rebuild_of_a_passing_run_qualifies_and_can_be_newest(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1000_full")
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1100_rebuild", summary_extra={"source_run": "1_scrapers\\dekra\\output\\2026-10-05_1000_full"})
    c = mi.select_run("dekra", tmp_path)
    assert c.run_dir.name == "2026-10-05_1100_rebuild" and "rebuilds passing 2026-10-05_1000_full" in c.why


def test_rebuild_of_a_test_output_run_does_not_qualify(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1100_rebuild", summary_extra={"source_run": "1_scrapers/dekra/test_output/2026-10-05_1000_recon"})
    with pytest.raises(LookupError, match="not a run folder in output"):
        mi.select_run("dekra", tmp_path)


def test_a_rebuild_without_status_does_not_qualify(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1000_full")
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1100_rebuild", status=None)
    assert mi.select_run("dekra", tmp_path).run_dir.name == "2026-10-05_1000_full"


def test_summary_started_breaks_a_same_minute_tie(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1131_full", summary_extra={"started": "2026-10-05T11:31:29"})
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1131_rebuild",
                 summary_extra={"started": "2026-10-05T11:31:25", "source_run": "2026-10-05_1131_full"})
    assert mi.select_run("dekra", tmp_path).run_dir.name == "2026-10-05_1131_full"


def test_pin_replaces_newest_but_not_the_qualification_rules(tmp_path):
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1000_full")
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1200_full")
    write_source(tmp_path, "dekra", _row(), run="2026-10-05_1300_full", status="FAIL")
    assert mi.select_run("dekra", tmp_path, pins={"dekra": "2026-10-05_1000_full"}).run_dir.name == "2026-10-05_1000_full"
    with pytest.raises(LookupError, match="does not qualify"):
        mi.select_run("dekra", tmp_path, pins={"dekra": "2026-10-05_1300_full"})
    with pytest.raises(LookupError, match="not found"):
        mi.select_run("dekra", tmp_path, pins={"dekra": "2026-10-05_0900_full"})


def test_sources_without_summary_use_the_file_name_proof(tmp_path):
    write_source(tmp_path, "tuev_rheinland", [source_row("tuev_rheinland", "TUEV_RHEINLAND-1")], run="2026-10-06_1536_full")
    c = mi.select_run("tuev_rheinland", tmp_path)
    assert "no unverified marker" in c.why
    write_source(tmp_path, "tuev_thueringen", [source_row("tuev_thueringen", "TUEV_THUERINGEN-1")], run="2026-10-06_1509_full", file_tag="_unverified")
    with pytest.raises(LookupError):
        mi.select_run("tuev_thueringen", tmp_path)


def test_test_output_is_never_looked_at(tmp_path):
    from core import paths
    d = paths.output_dir("dekra", "sample", root=tmp_path) / "2026-10-05_1000_sample"
    d.mkdir(parents=True)
    with pytest.raises(LookupError, match="no run folders"):
        mi.select_run("dekra", tmp_path)


def test_unused_and_new_columns_are_reported(tmp_path):
    h = mi.column_drift("dekra", ["location_id", "brand_new"])
    assert "name" in h[0] and h[1] == ["brand_new"] and "mobile" in h[2]


def test_every_source_has_config_for_the_master_targets():
    for slug, cfg in mi.SOURCES.items():
        targets = set(cfg.columns.values())
        assert "~location_id" in targets and {"street", "city", "zip"} <= targets, slug
        assert set(cfg.columns) & set(mi.KNOWN_EXTRAS.get(slug, ())) == set(), slug
        assert set(cfg.ignored) & set(mi.KNOWN_EXTRAS.get(slug, ())) == set(), slug
