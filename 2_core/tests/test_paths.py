"""core.paths: run folder names, output vs test_output, and the never-overwrite rule."""

from datetime import datetime

import pytest

from core import paths

AT = datetime(2026, 9, 28, 14, 12, 55)


@pytest.mark.parametrize(
    "mode, area",
    [("full", "output"), ("rebuild", "output"), ("sample", "test_output"), ("simulated", "test_output"), ("recon", "test_output")],
)
def test_run_folder_name_and_area_per_mode(tmp_path, mode, area):
    folder = paths.run_dir("gtue", mode, AT, tmp_path)
    assert folder == tmp_path / "1_scrapers" / "gtue" / area / f"2026-09-28_1412_{mode}"


def test_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="mode must be one of"):
        paths.run_dir("gtue", "weekly", AT, tmp_path)


def test_new_run_dir_creates_the_folder(tmp_path):
    folder = paths.new_run_dir("gtue", "sample", AT, tmp_path)
    assert folder.is_dir() and folder.name == "2026-09-28_1412_sample"


def test_same_minute_clash_takes_the_next_free_suffix_and_never_touches_the_existing_folder(tmp_path):
    first = paths.new_run_dir("gtue", "full", AT, tmp_path)
    (first / "raw.jsonl").write_text("keep me\n", encoding="utf-8")
    second = paths.new_run_dir("gtue", "full", AT, tmp_path)
    third = paths.new_run_dir("gtue", "full", AT.replace(second=1), tmp_path)
    assert [f.name for f in (first, second, third)] == [
        "2026-09-28_1412_full", "2026-09-28_1412_full_2", "2026-09-28_1412_full_3",
    ]
    assert list(second.iterdir()) == [] and (first / "raw.jsonl").read_text(encoding="utf-8") == "keep me\n"


def test_a_different_minute_or_mode_is_a_different_folder(tmp_path):
    a = paths.new_run_dir("gtue", "full", AT, tmp_path)
    b = paths.new_run_dir("gtue", "full", datetime(2026, 9, 28, 14, 13), tmp_path)
    c = paths.new_run_dir("gtue", "rebuild", AT, tmp_path)
    assert len({a, b, c}) == 3 and [f.name for f in (a, b, c)] == [
        "2026-09-28_1412_full", "2026-09-28_1413_full", "2026-09-28_1412_rebuild",
    ]


def test_reference_and_fixture_dirs(tmp_path):
    assert paths.geonames_dir(tmp_path) == tmp_path / "4_reference" / "geonames"
    assert paths.fixtures_dir("gtue", tmp_path) == tmp_path / "1_scrapers" / "gtue" / "tests" / "fixtures"
    assert paths.master_output_dir("2026-10", tmp_path) == tmp_path / "3_master_output" / "2026-10"
    assert (paths.project_root() / "2_core" / "paths.py").exists()
