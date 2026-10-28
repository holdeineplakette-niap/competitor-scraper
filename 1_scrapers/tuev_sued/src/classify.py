"""TÜV SÜD classification: site_type, in_scope, evidence, doubt flags (sources/tuev_sued.md §8).

  in_scope   = huau == 1 and tscStatus == 1 and country Germany   -> site_type pruefstelle
  otherwise                                                        -> site_type unknown + needs_review (the row is kept)
The finder also lists partner-operated sites and TÜV Hessen / TÜV Hanse stations (companyId). They show the same HU signals
as the own Service-Center (own opening hours, booking system) and count as Prüfstellen (decision 2026-10-06, Nikos): there is
no flag for them and no partner_office class; `operator_group` (parse.py) only says who runs the site. Flags never overrule
the rule and nothing is dropped; every flag below sets needs_review.
"""

from __future__ import annotations

import re

import pandas as pd

from core import enrich
from tuev_sued import parse

SITE_TYPES = ("pruefstelle", "unknown")

FLAG_UO_NOTICE = "uo_notice"  # a current notice says "Prüfstelle der Überwachungsorganisation" (seen at Wolnzach; meaning undecided)
FLAG_FAR_FROM_PLZ = "coordinate_far_from_plz"  # more than 25 km from the centroid of its PLZ (core.enrich)
FLAG_HUAU_NOT_1 = "huau_not_1"
FLAG_STATUS_NOT_1 = "tsc_status_not_1"
FLAG_NOT_GERMANY = "not_germany"
REVIEW_FLAGS = (FLAG_UO_NOTICE, FLAG_FAR_FROM_PLZ, FLAG_HUAU_NOT_1, FLAG_STATUS_NOT_1, FLAG_NOT_GERMANY)

SCOPE_REASONS = {
    FLAG_HUAU_NOT_1: "HU/AU not offered (huau != 1)",
    FLAG_STATUS_NOT_1: "Service-Center not active (tscStatus != 1)",
    FLAG_NOT_GERMANY: "not in Germany (countryId)",
}
_UO_NOTICE = re.compile(r"pr(?:ü|ue|u)fstelle\s+der\s+(?:ü|ue|u)berwachungsorganisation", re.IGNORECASE)


def scope_flags(row) -> list[str]:
    """The reasons a row fails the scope rule (empty = in scope)."""
    flags = []
    if row["huau"] != 1:
        flags.append(FLAG_HUAU_NOT_1)
    if row["tsc_status"] != 1:
        flags.append(FLAG_STATUS_NOT_1)
    if row["country"] != parse.HOME_COUNTRY:
        flags.append(FLAG_NOT_GERMANY)
    return flags


def classify_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Set site_type, site_type_evidence, in_scope, scope_reason, doubt_flags and extend needs_review(_reason).
    Run after core.enrich: its "coordinate far from PLZ" reason becomes the flag coordinate_far_from_plz."""
    out = df.copy()
    if out.empty:
        return out
    flags, in_scope = [], []
    for _, row in out.iterrows():
        row_flags = scope_flags(row)
        in_scope.append(not row_flags)
        notice = row["current_notices"]
        if isinstance(notice, str) and _UO_NOTICE.search(notice):
            row_flags.append(FLAG_UO_NOTICE)
        if enrich.REASON_FAR_FROM_PLZ in enrich.split_reasons(row["needs_review_reason"]):
            row_flags.append(FLAG_FAR_FROM_PLZ)
        flags.append(row_flags)
    out["in_scope"] = in_scope
    out["site_type"] = ["pruefstelle" if ins else "unknown" for ins in in_scope]
    out["site_type_evidence"] = [
        f"huau:{h};tscStatus:{s};country:{c}" for h, s, c in zip(out["huau"], out["tsc_status"], out["country"])
    ]
    out["scope_reason"] = [
        None if ins else "; ".join(SCOPE_REASONS[f] for f in fl if f in SCOPE_REASONS) for ins, fl in zip(in_scope, flags)
    ]
    out["doubt_flags"] = ["; ".join(fl) or None for fl in flags]
    reasons = out["needs_review_reason"].astype(object).where(out["needs_review_reason"].notna(), None)
    review = out["needs_review"].astype(bool)
    for idx, row_flags in zip(out.index, flags):
        for flag in row_flags:
            if flag in REVIEW_FLAGS and flag != FLAG_FAR_FROM_PLZ:  # that reason is already there, set by core.enrich
                reasons[idx] = enrich.add_reason(reasons[idx], flag)
            if flag in REVIEW_FLAGS:
                review[idx] = True
    out["needs_review_reason"] = reasons
    out["needs_review"] = review
    return out
