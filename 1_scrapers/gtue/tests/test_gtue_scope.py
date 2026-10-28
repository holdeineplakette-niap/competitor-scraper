"""Scope rule of 2026-10-07 (only a certain cause excludes a row), the same-site information and the
PASS a rebuild inherits from a passing source run. No network."""

import json
from datetime import datetime

import pandas as pd
import pytest

from core import paths
from gtue import adapter as gtue
from gtue.adapter import (
    IN_SCOPE_TIERS,
    TIER_OFFICE_HU,
    TIER_OFFICE_NO_HU,
    TIER_PRUEFSTELLE,
    TIER_UNKNOWN,
    add_same_site_ids,
    classify_scope,
    load_response,
    parse_response,
)

from conftest import SAMPLE
from test_gtue_rebuild import _response, _source_run


@pytest.mark.parametrize(
    "site_type, offers_hu, tier, in_scope, reason",
    [
        ("pruefstelle", True, TIER_PRUEFSTELLE, True, None),
        ("pruefstelle", False, TIER_PRUEFSTELLE, True, None),  # E + code: stays, even without HU in the list
        ("unknown", True, TIER_UNKNOWN, True, None),
        ("unknown", False, TIER_UNKNOWN, True, None),  # conflicting signals are no certain cause
        ("partner_office", True, TIER_OFFICE_HU, True, None),
        ("partner_office", False, TIER_OFFICE_NO_HU, False, gtue.REASON_NO_HU),
    ],
)
def test_classify_scope(site_type, offers_hu, tier, in_scope, reason):
    assert classify_scope(site_type, offers_hu) == (tier, in_scope, reason)


def test_only_the_no_hu_tier_is_out_of_scope():
    assert set(gtue.SCOPE_TIERS) - set(IN_SCOPE_TIERS) == {TIER_OFFICE_NO_HU}


@pytest.fixture(scope="module")
def sample_df():
    if not SAMPLE.exists():
        pytest.skip(f"Sample file missing: {SAMPLE}")
    return parse_response(load_response(SAMPLE), scraped_at="2026-09-28T11:26:10")


def test_sample_tiers_and_reasons(sample_df):
    assert sample_df["scope_tier"].value_counts().to_dict() == {
        TIER_PRUEFSTELLE: 13, TIER_OFFICE_HU: 6, TIER_OFFICE_NO_HU: 1,
    }
    out = sample_df[~sample_df["in_scope"]]
    assert len(out) == 1 and out["scope_reason"].iloc[0] == gtue.REASON_NO_HU and not out["offers_hu"].iloc[0]
    assert sample_df.loc[sample_df["in_scope"], "scope_reason"].isna().all()
    # an in-scope office keeps its site_type: the tier is the scope decision, site_type the finder's signals
    office = sample_df[sample_df["scope_tier"] == TIER_OFFICE_HU]
    assert (office["site_type"] == "partner_office").all() and office["in_scope"].all() and not office["needs_review"].any()


def test_abroad_stays_out_whatever_the_tier():
    item = {"BasicData": {"Identifiers": {"ProviderForeignKey": "100000001"}, "Address": {"Country": "A", "Zip": "6020"},
                          "BranchListElements": [{"BranchCode": "GTUP", "BranchText": "GTÜ-Ingenieurbüro mit amtlicher Fahrzeugprüfung vor Ort"}],
                          "MemoItems": [{"Type": "JSON_INFO", "Text": json.dumps({"HiddenPoiInfos": {"Herkunft": "E", "Stand": "2026-09-29"}})}],
                          "ObjectListItems": [{"Headline": gtue.HU_HEADLINE}]}}
    row = gtue.parse_item(item, "2026-10-07T10:00:00")
    assert row["scope_tier"] == TIER_PRUEFSTELLE and row["in_scope"] is False
    assert row["scope_reason"] == gtue.REASON_OUTSIDE_GERMANY


# --------------------------------------------------------------------------
# Same-site information
# --------------------------------------------------------------------------

def _rows(*specs):
    cols = ["location_id", "country", "street", "zip", "lat", "lng"]
    return pd.DataFrame(specs, columns=cols)


def test_same_site_by_normalised_address_and_by_coordinates():
    df = _rows(
        ("GTUE-A", "D", "Schlachthofstraße 43", "99085", 50.9700, 11.0300),
        ("GTUE-B", "D", "Schlachthofstr. 43", "99085", None, None),  # same address, other spelling, no coordinates
        ("GTUE-C", "D", "Hinterhof 1", "99085", 50.9708, 11.0300),  # ~89 m from A, other street
        ("GTUE-D", "D", "Ganz woanders 7", "80331", 48.1, 11.5),
        ("GTUE-E", "A", "Schlachthofstraße 43", "99085", 50.9700, 11.0300),  # abroad: ignored
    )
    out = add_same_site_ids(df).set_index("location_id")["same_site_ids"]
    assert out["GTUE-A"] == "GTUE-B; GTUE-C"
    assert out["GTUE-B"] == "GTUE-A"
    assert out["GTUE-C"] == "GTUE-A"
    assert out["GTUE-D"] is None
    assert out["GTUE-E"] is None


def test_same_site_far_apart_and_same_street_other_plz_are_not_linked():
    df = _rows(
        ("GTUE-A", "D", "Hauptstraße 1", "10115", 52.53, 13.38),
        ("GTUE-B", "D", "Hauptstraße 1", "80331", 48.14, 11.57),
        ("GTUE-C", "D", "Nebenstraße 2", "10115", 52.54, 13.38),  # 1.1 km
    )
    assert add_same_site_ids(df)["same_site_ids"].isna().all()


def test_same_site_does_not_change_or_drop_rows():
    df = _rows(("GTUE-A", "D", "X 1", "99085", 50.97, 11.03), ("GTUE-B", "D", "X 1", "99085", 50.97, 11.03))
    out = add_same_site_ids(df)
    assert len(out) == 2 and out["location_id"].tolist() == ["GTUE-A", "GTUE-B"]
    assert "same_site_ids" not in df.columns  # the input is not modified


# --------------------------------------------------------------------------
# A rebuild of a passing run inherits its PASS
# --------------------------------------------------------------------------

def _passing_source(tmp_path, centroids, summary_extra=None, name="2026-09-30_1100_full"):
    locations = gtue.simulated_locations(centroids, n_total=12, seed=3, clusters={})
    items = [gtue._fake_item(row) for row in locations.itertuples(index=False)]
    raw = _source_run(tmp_path, name, [_response(items, 12, centre=gtue.REFERENCE_CENTRE), _response(items[:4])])
    summary = {"outcome": "PASS (early stop, count match)", "unique_locations": 12, "unique_german_locations": 12,
               "started_at": "2026-09-30T11:00:25"} | (summary_extra or {})
    (raw / paths.RUN_SUMMARY_JSON).write_text(json.dumps(summary), encoding="utf-8")
    return raw


def test_rebuild_of_a_passing_run_inherits_pass_and_writes_untagged_files(tmp_path, centroids):
    raw = _passing_source(tmp_path, centroids)
    s = gtue.rebuild_from_raw(raw, centroids, root=tmp_path, verbose=False, started_at=datetime(2026, 10, 7, 9, 0)).summary
    assert s["outcome"].startswith("PASS (rebuilt from 2026-09-30_1100_full")
    assert s["source_run"] == raw.name and s["mode"] == "rebuild" and s["label"] is None
    for key in ("all_csv", "pruefstellen_csv", "all_xlsx", "pruefstellen_xlsx"):
        assert "grid-incomplete" not in s["files"][key] and "count-mismatch" not in s["files"][key]
    assert s["in_scope_by_tier"].keys() == set(gtue.SCOPE_TIERS)
    assert sum(s["in_scope_by_tier"].values()) == s["in_scope"]
    text = gtue.format_rebuild_summary(s)
    assert "GRID INCOMPLETE" not in text and "PASS is inherited" in text and "Out of scope" in text
    # what the aggregator reads: outcome starts with PASS, source_run names a run folder of output/
    on_disk = json.loads((tmp_path / s["files"]["run_summary"]).read_text(encoding="utf-8"))
    assert on_disk["outcome"].upper().startswith("PASS") and on_disk["source_run"] == raw.name


@pytest.mark.parametrize(
    "extra",
    [
        {"outcome": "FAIL (214 coverage warnings)"},  # the source did not pass
        {"unique_locations": 13},  # the rebuild does not find the source's locations
        {"unique_german_locations": 11},
    ],
)
def test_rebuild_does_not_inherit_when_the_source_did_not_pass_or_counts_differ(tmp_path, centroids, extra):
    raw = _passing_source(tmp_path, centroids, extra)
    s = gtue.rebuild_from_raw(raw, centroids, root=tmp_path, verbose=False).summary
    assert s["outcome"] is None and s["grid_status"] == "incomplete" and s["label"] == "grid-incomplete"
    assert "grid-incomplete" in s["files"]["all_csv"]
    assert "GRID INCOMPLETE" in gtue.format_rebuild_summary(s)


def test_rebuild_without_tag_is_refused_for_a_source_that_did_not_pass(tmp_path, centroids):
    raw = _passing_source(tmp_path, centroids, {"outcome": "FAIL"})
    with pytest.raises(ValueError, match="only allowed for a source run that passed"):
        gtue.rebuild_from_raw(raw, centroids, root=tmp_path, verbose=False, tag=None)
