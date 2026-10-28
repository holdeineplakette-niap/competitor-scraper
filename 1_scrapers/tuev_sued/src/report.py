"""Helpers for the TÜV SÜD control notebook: load a run, result tables and maps. No network, no run logic.

matplotlib is imported inside the map functions (it is a notebook-only dependency).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from core import paths
from tuev_sued import run as tuev_run

# What TÜV SÜD publishes about its network. Nothing current was found yet (see sources/tuev_sued.md §10).
PUBLISHED_FIGURES = [
    {
        "claim": "über 300 eigene Service-Center",
        "source": "interview from 2015",
        "checked": "OUTDATED, not found on a current page",
        "comparable_with": "our in_scope count (huau = 1, tscStatus = 1, Germany)",
    },
]


def load_run(folder: str | Path, root=None) -> tuev_run.RunResult:
    """An earlier run folder as a RunResult: run_summary.json plus the frame from its tuev_sued_all (or _pruefstellen) CSV."""
    folder = Path(folder)
    if not folder.is_absolute() and not folder.exists():
        for mode in ("output", "test_output"):
            candidate = paths.scraper_dir(tuev_run.SLUG, root) / mode / folder
            if candidate.exists():
                folder = candidate
                break
    summary = json.loads((folder / paths.RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
    csvs = sorted(folder.glob("tuev_sued_all_*.csv")) or sorted(folder.glob("tuev_sued_pruefstellen_*.csv"))
    text_columns = {c: str for c in ("zip", "source_id", "location_sign", "aspro_id", "market_area_id", "federal_state_id", "company_id")}
    frame = pd.read_csv(csvs[0], sep=";", encoding="utf-8-sig", dtype=text_columns) if csvs else pd.DataFrame()
    return tuev_run.RunResult(run_folder=folder, summary=summary, frame=frame, files={})


def rule_table(summary: dict) -> pd.DataFrame:
    """PASS/FAIL of the two parts of the proof, with the number of issues."""
    cov = summary["coverage"]
    rows = [
        {"check": f"every target covered (disk radius - {cov['cover_margin_km']:g} km)", "result": "ok" if not cov["uncovered"]["total"] else "FAIL",
         "issues": cov["uncovered"]["total"]},
        {"check": "premise: sorted by distance, no listed-site missing from a disk", "result": "ok" if cov["premise"]["ok"] else "FAIL",
         "issues": cov["premise"]["misses"]["total"] + cov["premise"]["unsorted_answers"]["total"]},
    ]
    return pd.DataFrame(rows, columns=["check", "result", "issues"])


def pass_table(summary: dict) -> pd.DataFrame:
    return pd.DataFrame(summary["passes"])


def counts_table(result) -> pd.Series:
    s = result.summary
    types = s.get("site_types", {})
    return pd.Series(
        {
            "collected (unique Service-Center)": s["rows_total"],
            "in scope (pruefstelle)": s["in_scope"],
            **{f"  in scope: {k}": v for k, v in s.get("in_scope_by_operator", {}).items()},
            "unknown (outside the scope rule)": types.get("unknown", 0),
            "needs_review": s["needs_review"],
            "answers (queries)": s["requests_made"],
            "passes": len(s["passes"]),
        }
    )


def per_state(frame: pd.DataFrame) -> pd.DataFrame:
    """Service-Center per Bundesland: all, in scope."""
    if frame.empty:
        return pd.DataFrame()
    state = frame["state"].fillna("unknown")
    out = pd.DataFrame({"all": state.value_counts(), "in_scope": state[frame["in_scope"].astype(bool)].value_counts()}).fillna(0).astype(int)
    return out.sort_values("in_scope", ascending=False)


def published_comparison(summary: dict) -> pd.DataFrame:
    return pd.DataFrame([{**p, "our_in_scope": summary["in_scope"]} for p in PUBLISHED_FIGURES])


def key_fill_table(summary: dict, top: int = 40) -> pd.DataFrame:
    """The `location` keys seen, with fill rates (the schema is not final)."""
    fill = pd.DataFrame(summary.get("location_key_fill", {})).T
    return fill.sort_values("rate", ascending=False).head(top) if not fill.empty else fill


def review_tables(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """The tables a human looks at after a run. Emails can be personal: do not save notebook outputs of real runs."""
    if frame.empty:
        return {}
    cols = ["source_id", "name", "city", "zip", "operator_group", "huau", "tsc_status", "doubt_flags", "needs_review_reason"]
    review = frame[frame["needs_review"].astype(bool)]
    reasons = review["needs_review_reason"].str.split("; ").explode().value_counts().rename("rows")
    return {
        "needs_review_by_reason": reasons.to_frame(),
        "needs_review_rows": review[cols],
        "uo_notice": frame[frame["doubt_flags"].fillna("").str.contains("uo_notice")][cols + ["current_notices"]],
        "by_operator": frame.groupby(frame["operator_group"].fillna("unknown")).agg(all=("source_id", "count"), in_scope=("in_scope", "sum")),
        "closed_now": frame[frame["currently_closed"].astype(bool)][cols],
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
    ax.set_aspect(1 / math.cos(math.radians(51)))
    ax.set_xlim(5.4, 15.5)
    ax.set_ylim(47.0, 55.2)
    ax.set_title(title)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    return fig, ax


def planned_map(centroids: pd.DataFrame, planned: pd.DataFrame):
    """The PLZ of pass 1 over all regular PLZ centroids."""
    fig, ax = _axes(f"{len(planned)} PLZ in pass 1")
    ax.scatter(centroids["lng"], centroids["lat"], s=1, color="#bbbbbb", label=f"{len(centroids)} PLZ centroids (targets)")
    ax.scatter(planned["lng"], planned["lat"], s=8, color="#d62728", label="pass 1 query PLZ")
    ax.legend(loc="lower left", fontsize=8)
    return fig


def coverage_map(result, centroids: pd.DataFrame):
    """The proven disks (radius - margin) around the server centres, per pass, and every target that stayed open."""
    log = pd.read_csv(result.run_folder / paths.QUERY_LOG_FILE)
    margin = result.summary["coverage"]["cover_margin_km"]
    fig, ax = _axes(f"{len(log)} answers, circles = proven radius - {margin:g} km")
    ax.scatter(centroids["lng"], centroids["lat"], s=1, color="#dddddd")
    colours = ["#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd"]
    for pass_no, group in log[log["radius_km"] > margin].groupby("pass"):
        colour = colours[(int(pass_no) - 1) % len(colours)]
        for lat, lng, radius in zip(group["centre_lat"], group["centre_lng"], group["radius_km"]):
            la, lo = circle_latlng(lat, lng, radius - margin)
            ax.plot(lo, la, color=colour, linewidth=0.4, alpha=0.5)
        ax.scatter(group["centre_lng"], group["centre_lat"], s=6, color=colour, label=f"pass {pass_no} ({len(group)})")
    open_file = result.run_folder / tuev_run.OPEN_TARGETS_FILE
    if open_file.exists():
        open_targets = pd.read_csv(open_file, dtype={"zip": str})
        ax.scatter(open_targets["lng"], open_targets["lat"], s=18, marker="x", color="black", label=f"{len(open_targets)} targets not covered")
    ax.legend(loc="lower left", fontsize=8)
    return fig


def sites_map(frame: pd.DataFrame):
    """Located Service-Center, coloured by in_scope."""
    located = frame[frame["lat"].notna()]
    fig, ax = _axes(f"{len(located)} located Service-Center ({int(frame['in_scope'].sum())} in scope of {len(frame)})")
    out = located[~located["in_scope"].astype(bool)]
    inn = located[located["in_scope"].astype(bool)]
    ax.scatter(out["lng"], out["lat"], s=8, color="#9e9e9e", label=f"outside the scope rule ({len(out)})")
    ax.scatter(inn["lng"], inn["lat"], s=5, color="#d62728", label=f"in scope ({len(inn)})")
    ax.legend(loc="lower left", fontsize=8)
    return fig
