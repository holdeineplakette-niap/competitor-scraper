"""Helpers for the KÜS control notebook: load a run, result tables and maps. No network, no run logic.

matplotlib is imported inside the map functions (it is a notebook-only dependency).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from core import paths
from core import radius_cover as rc
from kues import run as kues_run

# What KÜS publishes about its network (checked 2026-10-05). Only the first one was found on a page.
PUBLISHED_FIGURES = [
    {
        "claim": "über 1.800 Prüfingenieurinnen und Prüfingenieure deutschlandweit",
        "url": "https://kues.de/geschaeftskunden/fahrzeugueberwachung/",
        "checked": "verbatim on the page",
        "comparable_with": "people, not sites: no direct check against our site counts",
    },
    {
        "claim": "rund 1.600 Prüfingenieure an rund 400 eigenen Prüfstellen; 8.000 Prüfstützpunkte",
        "url": "(not found on a page: seen only in a search-result snippet of kues.de)",
        "checked": "UNVERIFIED",
        "comparable_with": "~400 own Prüfstellen vs our in_scope count (pruefungvorortmoeglich = 1); PSPs are not listed in the finder",
    },
]


def load_run(folder: str | Path, root=None) -> kues_run.RunResult:
    """An earlier run folder as a RunResult: run_summary.json plus the frame from its kues_all (or kues_pruefstellen) CSV."""
    folder = Path(folder)
    if not folder.is_absolute() and not folder.exists():
        for mode in ("output", "test_output"):
            candidate = paths.scraper_dir(kues_run.SLUG, root) / mode / folder
            if candidate.exists():
                folder = candidate
                break
    summary = json.loads((folder / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
    csvs = sorted(folder.glob("kues_all_*.csv")) or sorted(folder.glob("kues_pruefstellen_*.csv"))
    frame = (
        pd.read_csv(csvs[0], sep=";", encoding="utf-8-sig", dtype={"zip": str, "source_id": str, "kues_licensee": str})
        if csvs else pd.DataFrame()
    )
    if len(frame) and "psp" not in frame.columns:  # a run from before 2026-10-07: derive the tag from the API flag
        frame["psp"] = frame["pruefung_vor_ort"].map({True: "No", False: "Yes", "True": "No", "False": "Yes"})
    return kues_run.RunResult(run_folder=folder, summary=summary, frame=frame, files={})


COMPARE_COLUMNS = [
    "name", "name_addon", "street", "zip", "city", "lat", "lng", "phone", "email", "website",
    "kues_licensee", "pruefung_vor_ort", "kues_list", "site_type", "in_scope",
]


def compare_runs(a, b, label_a: str = "A", label_b: str = "B") -> dict:
    """Compare two runs by adressid (e.g. a 50 km and a 100 km crawl). Prints a verdict and returns
    {"only_in_a", "only_in_b", "differences", "same"}: the ids found by one run only, a table of the records whose
    content differs (columns of COMPARE_COLUMNS), and whether the two runs agree completely."""
    fa, fb = a.frame.set_index("source_id"), b.frame.set_index("source_id")
    only_a, only_b = sorted(set(fa.index) - set(fb.index)), sorted(set(fb.index) - set(fa.index))
    common = sorted(set(fa.index) & set(fb.index))
    cols = [c for c in COMPARE_COLUMNS if c in fa.columns and c in fb.columns]
    left = fa.loc[common, cols].astype("string").fillna("")
    right = fb.loc[common, cols].astype("string").fillna("")
    diff = left.ne(right)
    rows = [
        {"source_id": i, "field": c, label_a: left.at[i, c], label_b: right.at[i, c]}
        for i in diff.index[diff.any(axis=1)] for c in cols if diff.at[i, c]
    ]
    differences = pd.DataFrame(rows, columns=["source_id", "field", label_a, label_b])
    same = not only_a and not only_b and differences.empty
    print(f"{label_a}: {len(fa)} records, {int(fa['in_scope'].sum())} in scope | {label_b}: {len(fb)} records, {int(fb['in_scope'].sum())} in scope")
    print(f"only in {label_a}: {len(only_a)} | only in {label_b}: {len(only_b)} | in both: {len(common)} | content differences: {differences['source_id'].nunique()} records")
    print("VERDICT: the two runs agree completely." if same else "VERDICT: the runs differ, see the returned tables.")
    return {"only_in_a": only_a, "only_in_b": only_b, "differences": differences, "same": same}


def rule_table(summary: dict) -> pd.DataFrame:
    """PASS/FAIL per coverage rule, with the number of issues."""
    labels = {
        "rule_a": "A  every PLZ within COVER_KM of a server centre",
        "rule_b": "B  every site within 45 km of a centre",
        "rule_c": "C  every centre within 48 km returned the site",
        "rule_d": "D  no count cap",
        "rule_c_technik": "C  (technik list)",
        "rule_d_technik": "D  (technik list)",
    }
    rows = [
        {"rule": labels[k], "result": "ok" if v["ok"] else "FAIL", "issues": kues_run._issue_count(k, v) if not v["ok"] else 0}
        for k, v in (summary.get("coverage") or {}).items() if k in labels
    ]
    return pd.DataFrame(rows, columns=["rule", "result", "issues"])


def counts_table(result) -> pd.Series:
    """The headline counts of a run."""
    s, df = result.summary, result.frame
    types = s.get("site_types", {})
    return pd.Series(
        {
            "collected (unique records)": s["rows_total"],
            "in scope": s["in_scope"],
            "in scope, psp = No (own Prüfstelle)": s.get("in_scope_psp", {}).get("No", 0),
            "in scope, psp = Yes": s.get("in_scope_psp", {}).get("Yes", 0),
            "partner_office": types.get("partner_office", 0),
            "unknown": types.get("unknown", 0),
            "needs_review": s["needs_review"],
            "plz_only (no street / coordinates)": s["plz_only"],
            "kues_list = both": s.get("kues_list", {}).get("both", 0),
            "kues_list = kues_technik only": s.get("kues_list", {}).get("kues_technik", 0),
        }
    )


def per_state(frame: pd.DataFrame) -> pd.DataFrame:
    """Records per Bundesland: all, in scope, psp = Yes, partner offices."""
    if frame.empty:
        return pd.DataFrame()
    state = frame["state"].fillna("unknown")
    out = pd.DataFrame(
        {
            "all": state.value_counts(),
            "in_scope": state[frame["in_scope"].astype(bool)].value_counts(),
            "psp_yes": state[frame["psp"] == "Yes"].value_counts(),
            "partner_office": state[frame["site_type"] == "partner_office"].value_counts(),
        }
    ).fillna(0).astype(int)
    return out.sort_values("in_scope", ascending=False)


def published_comparison(summary: dict) -> pd.DataFrame:
    own = summary.get("in_scope_psp", {}).get("No", summary["in_scope"])  # the published figures count own Prüfstellen only
    rows = [{**p, "our_in_scope": own} for p in PUBLISHED_FIGURES]
    return pd.DataFrame(rows)


def review_tables(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """The tables a human looks at after a run. Names are personal data: do not save notebook outputs."""
    if frame.empty:
        return {}
    cols = ["source_id", "display_name", "city", "kues_licensee", "pruefung_vor_ort", "doubt_flags", "needs_review_reason"]
    review = frame[frame["needs_review"].astype(bool)]
    reasons = review["needs_review_reason"].str.split("; ").explode().value_counts().rename("rows")
    multi = frame[frame["licensee_site_count"] > 1].sort_values(["licensee_site_count", "kues_licensee"], ascending=[False, True])
    shared = frame[frame["same_address_count"] > 1].sort_values(["zip", "street"])
    conflicts = frame[frame["doubt_flags"].notna()]
    return {
        "needs_review_by_reason": reasons.to_frame(),
        "needs_review_rows": review[cols],
        "licensees_with_several_sites": multi[cols + ["licensee_site_count"]],
        "shared_addresses": shared[cols + ["street", "zip", "same_address_count"]],
        "flag_conflicts": conflicts[cols],
    }


# --------------------------------------------------------------------------
# Maps
# --------------------------------------------------------------------------

def circle_latlng(lat: float, lng: float, radius_km: float, n: int = 72) -> tuple[np.ndarray, np.ndarray]:
    angle = np.linspace(0, 2 * math.pi, n)
    return lat + radius_km * np.sin(angle) / 110.57, lng + radius_km * np.cos(angle) / (111.32 * math.cos(math.radians(lat)))


def _axes(title: str):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 8.5))
    ax.set_aspect(1 / math.cos(math.radians(51)))  # lng degrees are shorter than lat degrees
    ax.set_xlim(5.4, 15.5)
    ax.set_ylim(47.0, 55.2)
    ax.set_title(title)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    return fig, ax


def planned_map(centroids: pd.DataFrame, planned: pd.DataFrame, cfg: rc.CoverConfig):
    """The planned query points with their COVER_KM circles over all regular PLZ centroids."""
    fig, ax = _axes(f"{len(planned)} planned query points, circles = COVER_KM {cfg.cover_km:g} km")
    ax.scatter(centroids["lng"], centroids["lat"], s=1, color="#bbbbbb", label=f"{len(centroids)} PLZ centroids")
    for lat, lng in zip(planned["lat"], planned["lng"]):
        la, lo = circle_latlng(lat, lng, cfg.cover_km)
        ax.plot(lo, la, color="#1f77b4", linewidth=0.5, alpha=0.6)
    ax.scatter(planned["lng"], planned["lat"], s=6, color="#d62728", label="planned query points")
    ax.legend(loc="lower left", fontsize=8)
    return fig


def coverage_map(result, centroids: pd.DataFrame, cfg: rc.CoverConfig):
    """The actual server centres with their search circles, and every PLZ that rule A left uncovered."""
    log = pd.read_csv(result.run_folder / paths.QUERY_LOG_FILE)
    main = log[(log["list"] == "partnersuche") & log["centre_lat"].notna()]
    fig, ax = _axes(f"{len(main)} server centres, circles = {cfg.radius_km:g} km search radius")
    ax.scatter(centroids["lng"], centroids["lat"], s=1, color="#dddddd")
    for lat, lng in zip(main["centre_lat"], main["centre_lng"]):
        la, lo = circle_latlng(lat, lng, cfg.radius_km)
        ax.plot(lo, la, color="#1f77b4", linewidth=0.4, alpha=0.5)
    ax.scatter(main["centre_lng"], main["centre_lat"], s=5, color="#d62728", label="server centres")
    rule_a = (result.summary.get("coverage") or {}).get("rule_a", {})
    bad = set(rule_a.get("uncovered", [])) | set(rule_a.get("assumed_empty", []))
    if bad:
        hit = centroids[centroids["zip"].isin(bad)]
        ax.scatter(hit["lng"], hit["lat"], s=25, marker="x", color="black", label=f"{len(hit)} PLZ not covered / answered empty")
    ax.legend(loc="lower left", fontsize=8)
    return fig


def sites_map(frame: pd.DataFrame):
    """Located sites: out of scope grey, in scope psp = Yes blue, in scope psp = No red."""
    located = frame[frame["lat"].notna()]
    fig, ax = _axes(f"{len(located)} located sites ({int(frame['in_scope'].sum())} in scope of {len(frame)})")
    in_scope = located["in_scope"].astype(bool)
    out = located[~in_scope]
    psp = located[in_scope & (located["psp"] == "Yes")]
    own = located[in_scope & (located["psp"] != "Yes")]
    ax.scatter(out["lng"], out["lat"], s=4, color="#9e9e9e", label=f"out of scope ({len(out)})")
    ax.scatter(psp["lng"], psp["lat"], s=4, color="#1f77b4", label=f"in scope, psp = Yes ({len(psp)})")
    ax.scatter(own["lng"], own["lat"], s=5, color="#d62728", label=f"in scope, psp = No ({len(own)})")
    ax.legend(loc="lower left", fontsize=8)
    return fig
