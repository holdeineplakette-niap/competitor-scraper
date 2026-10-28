"""The master schema and every table that decides how source values become master values.

An unmapped value is a hard error, never a default: a new operator_group or site_type in a source export
stops the run until someone adds it here on purpose.
"""

from __future__ import annotations

import re

from core.normalise import fold

# ---- switches ------------------------------------------------------------------------------------------------
INCLUDE_OUT_OF_SCOPE = False  # False: the master takes only in-scope rows
MERGE_ACROSS_UO = False  # False: no automatic merge across merge families; shared addresses only flagged

# ---- columns -------------------------------------------------------------------------------------------------
MASTER_COLUMNS = [
    "master_id", "uo", "listed_by", "source_ids",
    "site_type", "site_type_evidence", "operator_name", "operator_group", "psp",
    "name", "street", "zip", "city", "state", "country",
    "lat", "lng", "coord_source", "plz_distance_km",
    "phone", "email", "website", "booking_url", "profile_url",
    "opening_hours", "services",
    "in_scope", "scope_reason", "doubt_flags", "needs_review", "needs_review_reason", "is_hdp_site",
    "status", "retired_on",
    "scraped_at", "first_seen", "last_seen",
]
CONTACT_FIELDS = ("phone", "email", "website", "booking_url")
# Decision (Nikos, 2026-10-08): `needs_review` is True ONLY for a row whose status changed in this run (closed list below).
# Everything that used to set it is a doubt flag now: the same tokens, listed in `doubt_flags` and in the review files.
REVIEW_REASONS = ("retired", "reactivated")  # closed list
DOUBT_REASONS = ("match_candidate_unresolved", "coords_unresolved", "address_incomplete", "plz_invalid",
                 "hdp_match_unresolved", "hdp_own_site_kept")  # closed list, tokens in doubt_flags
STATUS_VALUES = ("active", "retired")
LIFECYCLE_FLAGS = ("not_refreshed", "possibly_rekeyed")  # doubt-flag prefixes added after the merge (info, never needs_review)

# ---- families and fixed source order --------------------------------------------------------------------------
FAMILIES = {"tuev_sued": "sued_hessen", "tuev_hessen": "sued_hessen", "tuev_rheinland": "rheinland_fsp", "fsp": "rheinland_fsp"}
FIXED_ORDER = ("tuev_sued", "tuev_hessen", "tuev_rheinland", "fsp")  # ties break by this, then slug alphabetical
OPERATOR_ORDER = ("tuev_hessen", "tuev_sued", "tuev_rheinland", "fsp")  # the source that names the operator comes first
CONSTRUCTED_NAME_SOURCES = ("tuev_sued",)  # builds its display name from the town
SIGN_SOURCES = ("tuev_sued", "tuev_hessen")  # carry the booking `location_sign` (column location_sign)
WITHIN_SOURCE_VETO = {"kues": "kues_licensee"}  # a different value here means a different business at the same address
NAME_EVIDENCE_EXCLUDED = ("gtue", "kues")  # generic names ("GTÜ-Prüfstelle", "Ingenieurbüro"): an equal name proves nothing there
CANDIDATE_WITHIN_SOURCE = ("dekra", "kues", "gtue")  # sources where same-source near-duplicates are known to occur


def family_of(slug: str) -> str:
    return FAMILIES.get(slug, slug)


def rank(slug: str) -> tuple[int, str]:
    return (FIXED_ORDER.index(slug), slug) if slug in FIXED_ORDER else (len(FIXED_ORDER), slug)


def operator_rank(slug: str) -> tuple[int, str]:
    return (OPERATOR_ORDER.index(slug), slug) if slug in OPERATOR_ORDER else (len(OPERATOR_ORDER), slug)


# ---- uo -------------------------------------------------------------------------------------------------------
UO_VALUES = ("GTÜ", "KÜS", "DEKRA", "TÜV SÜD", "TÜV NORD", "TÜV Rheinland", "TÜV Hessen", "TÜV Thüringen",
             "TÜV Saarland", "VÜK")

# slug -> (source column the table keys on, {value: uo}); "*" = the whole source
UO_MAP: dict[str, tuple[str, dict[str, str]]] = {
    "gtue": ("*", {"*": "GTÜ"}),
    "kues": ("*", {"*": "KÜS"}),
    "dekra": ("*", {"*": "DEKRA"}),
    "tuev_nord": ("*", {"*": "TÜV NORD"}),
    "tuev_thueringen": ("*", {"*": "TÜV Thüringen"}),
    "vuek": ("*", {"*": "VÜK"}),
    "fsp": ("*", {"*": "TÜV Rheinland"}),  # FSP is not a uo: its rows are TÜV Rheinland, operator_group fsp
    "tuev_rheinland": ("operator_group", {"tuev_rheinland": "TÜV Rheinland", "fsp": "TÜV Rheinland",
                                          "tuev_saarland": "TÜV Saarland",
                                          "": "TÜV Rheinland"}),  # "" = the Wörth row (decided 2026-10-06), flagged
    "tuev_sued": ("operator_group", {"own": "TÜV SÜD", "tuev_sued_partner": "TÜV SÜD",
                                     "tuev_hessen": "TÜV Hessen", "tuev_hessen_partner": "TÜV Hessen",
                                     "tuev_hanse": "TÜV SÜD"}),  # tuev_hanse: assumed (listed in the TÜV SÜD finder), flagged
    "tuev_hessen": ("site_type", {"own_station": "TÜV Hessen", "partner": "TÜV Hessen", "unclassified": "TÜV Hessen",
                                  "tuev_sued_auto_partner": "TÜV SÜD"}),
}
# (slug, value) pairs whose uo is an assumption: the row gets a doubt flag uo_assumed:<value>
UO_ASSUMED = {("tuev_sued", "tuev_hanse"): "tuev_hanse", ("tuev_rheinland", ""): "operator_group_empty"}

# ---- site_type -------------------------------------------------------------------------------------------------
# slug -> (source column, {value: master site_type}); the DEKRA table is refined by the non_dekra_operator flag
SITE_TYPE_MAP: dict[str, tuple[str, dict[str, str]]] = {
    "gtue": ("site_type", {"pruefstelle": "unknown", "partner_office": "unknown", "unknown": "unknown"}),
    "kues": ("site_type", {"pruefstelle": "unknown", "partner_office": "unknown", "unknown": "unknown"}),
    "dekra": ("site_type", {"pruefstelle": "own_station", "unknown": "unknown"}),
    "fsp": ("*", {"*": "partner"}),
    "tuev_nord": ("site_type", {"own_station_probable": "own_station", "partner_station": "partner",
                                "unclassified": "unknown", "office": "unknown"}),
    "tuev_rheinland": ("site_type", {"own_station": "own_station", "fsp_partner": "partner", "unknown": "unknown"}),
    "tuev_hessen": ("site_type", {"own_station": "own_station", "partner": "partner",
                                  "tuev_sued_auto_partner": "partner", "unclassified": "unknown"}),
    "tuev_sued": ("operator_group", {"own": "own_station", "tuev_sued_partner": "partner", "tuev_hessen": "own_station",
                                     "tuev_hessen_partner": "partner", "tuev_hanse": "unknown"}),
    "tuev_thueringen": ("site_type", {"own_station": "own_station"}),
    "vuek": ("site_type", {"pruefstelle": "own_station"}),
}

# ---- operator_group (master column) ------------------------------------------------------------------------------
# Sources that carry operator_group keep it (tuev_sued: "own" is renamed to tuev_sued); the rest derive it.
OPERATOR_GROUP_RENAME = {("tuev_sued", "own"): "tuev_sued"}
OPERATOR_GROUP_FROM_SITE_TYPE = {
    "tuev_hessen": {"own_station": "tuev_hessen", "partner": "tuev_hessen_partner",
                    "tuev_sued_auto_partner": "tuev_sued_partner", "unclassified": "tuev_hessen"},
}
OPERATOR_GROUP_CONSTANT = {"gtue": "gtue", "kues": "kues", "dekra": "dekra", "fsp": "fsp", "tuev_nord": "tuev_nord",
                           "tuev_thueringen": "tuev_thueringen", "vuek": "vuek"}

# ---- psp -------------------------------------------------------------------------------------------------------
# "Yes" only where the source says the record inspects at PSPs (KÜS pruefungvorortmoeglich = 0, column psp). Every
# other source carries no such column and gets "No". "partner" (site_type) is a different thing and has no effect.
PSP_VALUES = ("Yes", "No")
PSP_SOURCES = ("kues",)  # sources whose export has a psp column that is read


def psp_value(slug: str, value: str) -> str:
    """The master psp of one source row. Sources without a psp column: "No". A psp column holds Yes, No or empty
    (empty = KÜS record with an invalid flag, out of scope anyway: treated as No); anything else is an error."""
    if slug not in PSP_SOURCES:
        return "No"
    if value in ("", "No"):
        return "No"
    if value == "Yes":
        return "Yes"
    raise ValueError(f"{slug}: psp value {value!r} is not Yes, No or empty; add it on purpose")


# ---- PLZ corrections (explicit and logged; never a regex repair) ------------------------------------------------
# (source slug, PLZ as published, folded city) -> corrected PLZ
PLZ_CORRECTIONS = {("fsp", "553340", "meckenheim"): "53340"}

# ---- source reasons / flags -> doubt-flag tokens -----------------------------------------------------------------
# Free-text reasons from the exports become tokens. Resolvable tokens are dropped when the merge fixes the issue.
REASON_TOKENS = [
    (re.compile(r"^no PLZ in address"), "plz_missing"),
    (re.compile(r"^unknown PLZ"), "plz_unknown_in_source"),
    (re.compile(r"^coordinates missing"), "coords_missing"),
    (re.compile(r"^coordinate far from PLZ"), "coords_far_from_plz"),
    (re.compile(r"^no vic_id"), "no_vic_id"),
    (re.compile(r"^included because PLZ/coordinates look German"), "included_without_vic_id"),
    (re.compile(r"^vic_id format odd"), "vic_id_format_odd"),
    (re.compile(r"^site_type unknown: external_db"), "site_type_pool_conflict"),
    (re.compile(r"^address filled from landing page"), "address_from_landing_page"),
    (re.compile(r"^not in finder JSON"), "not_in_finder_json"),
    (re.compile(r"^added from landing page"), "added_from_landing_page"),
]
RESOLVED_BY_PLZ = {"plz_missing", "plz_unknown_in_source", "plz_not_5_digits", "plz_not_in_geonames"}
RESOLVED_BY_COORDS = {"coords_missing", "coords_far_from_plz", "coords_outside_germany",
                      "coords_shared_with_other_address", "coordinate_far_from_plz", "coordinates_missing"}

# ---- aggregator flag vocabulary (info flags, never needs_review) ------------------------------------------------------
AGG_FLAGS = ("shared_address_other_uo", "shared_address_same_source", "uo_assumed", "uo_conflict",
             "sign_address_mismatch", "merged_within_source_unproven", "coords_rejected", "country_not_de",
             "possible_duplicate_of")


def slugify(text: str, limit: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "_", fold(text)).strip("_")[:limit]


def reason_token(text: str) -> str:
    for pattern, token in REASON_TOKENS:
        if pattern.search(text):
            return token
    return slugify(text)


def split_flags(value) -> list[str]:
    return [t.strip() for t in re.split(r"[;|]", "" if value is None else str(value)) if t.strip()]


def split_services(value) -> list[str]:
    return [t.strip() for t in ("" if value is None else str(value)).split(";") if t.strip()]


def mapped(table: dict, slug: str, key: str, what: str) -> str:
    """Look a source value up; raise (never default) if it is not in the table."""
    column, mapping = table[slug]
    if column == "*":
        return mapping["*"]
    if key not in mapping:
        raise ValueError(f"{slug}: {what} value {key!r} (column {column}) is not in the mapping table; add it on purpose")
    return mapping[key]
