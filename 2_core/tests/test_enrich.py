"""Enrichment against the GTÜ sample and GeoNames centroids."""

import copy

import pandas as pd
import pytest

from core import enrich
from gtue.adapter import enrich_german, export, load_response, parse_response

from conftest import SAMPLE


@pytest.fixture(scope="module")
def sample_df():
    if not SAMPLE.exists():
        pytest.skip(f"Sample file missing: {SAMPLE}")
    return parse_response(load_response(SAMPLE), scraped_at="2026-09-28T11:26:10")


def test_sample_rows_get_state_bayern(sample_df, centroids):
    df = enrich.enrich(sample_df, centroids)
    assert (df["state"] == "Bayern").all()
    assert df["plz_distance_km"].notna().all()
    assert not df["needs_review"].any()


def test_coordinate_moved_100_km_is_flagged(sample_df, centroids):
    moved = sample_df.copy()
    moved.loc[0, "lat"] += 100 / 110.57  # 100 km north
    df = enrich.enrich(moved, centroids)
    assert df.loc[0, "plz_distance_km"] > 95
    assert df.loc[0, "needs_review"]
    assert df.loc[0, "needs_review_reason"] == "coordinate far from PLZ"
    assert not df.loc[1:, "needs_review"].any()


def test_foreign_row_is_out_of_scope_and_not_plz_checked(centroids, tmp_path):
    data = load_response(SAMPLE)
    abroad = copy.deepcopy(data["AddressItems"][1])  # SCHORER + WOLF Aubing, a Prüfstelle
    abroad["BasicData"]["Address"].update({"Country": "A", "Zip": "6020", "City": "Innsbruck"})
    df = parse_response({"AddressItems": [abroad, data["AddressItems"][6]]}, scraped_at="x")
    assert df.loc[0, "site_type"] == "pruefstelle"
    assert not df.loc[0, "in_scope"]
    assert df.loc[0, "needs_review_reason"] == "outside Germany"
    assert not df.loc[0, "needs_review"]

    df = enrich_german(df, centroids)
    assert pd.isna(df.loc[0, "state"]) and pd.isna(df.loc[0, "plz_distance_km"])
    assert df.loc[0, "needs_review_reason"] == "outside Germany"  # no "unknown PLZ" on top
    assert df.loc[1, "state"] == "Bayern" and df.loc[1, "in_scope"]

    written = export(df, tmp_path, run_date="2026-09-28")
    assert written["all_csv"][1] == 2
    assert written["pruefstellen_csv"][1] == 1


def test_unknown_plz_keeps_existing_reason(sample_df, centroids):
    odd = sample_df.copy()
    odd.loc[0, "zip"] = "00000"
    odd.loc[0, "needs_review"] = True
    odd.loc[0, "needs_review_reason"] = "site_type unknown"
    df = enrich.enrich(odd, centroids)
    assert df.loc[0, "needs_review_reason"] == "site_type unknown; unknown PLZ"
    assert enrich.enrich(df, centroids).loc[0, "needs_review_reason"] == "site_type unknown; unknown PLZ"  # idempotent
