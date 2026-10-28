"""Which run of which source feeds the master: per-source input config, run selection, inventory, manifest.

A source qualifies with its newest run folder in `output/` that (a) is a `full` or `rebuild` run, (b) carries proof
of a PASS, (c) for a rebuild, rebuilds a run that passed, and (d) holds exactly one in-scope export without a
`unverified` / `grid-incomplete` / `count-mismatch` marker. `test_output/` is never looked at.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from core import paths
from core.export import relative

RUN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(full|rebuild)(?:_([A-Za-z0-9_-]+))?$")  # a suffix such as _2 or _FAILED still counts as a run
BAD_MARKERS = ("unverified", "grid-incomplete", "count-mismatch")
SOURCE_ORDER = (
    "gtue", "kues", "dekra", "fsp", "tuev_sued", "tuev_nord",
    "tuev_rheinland", "tuev_hessen", "tuev_thueringen", "vuek",
)

# Master columns a source column can be renamed to directly (see master_schema for the full list).
# Targets starting with "~" are inputs for the aggregator's logic (classification, flags, checks); they are
# "homed" because the logic consumes them, but they are not master columns.


@dataclass(frozen=True)
class SourceConfig:
    slug: str
    label: str
    in_scope_pattern: str
    proof: str  # "summary": run_summary.json says PASS; "filename": no summary exists, the file name must carry no marker
    status_count: int  # in-scope rows named in scraper_status.md (sanity anchor, not a gate)
    columns: dict[str, str]  # source column -> master column or "~logic" input
    ignored: tuple[str, ...] = ()  # source columns dropped as redundant (constants, recomputed, split copies)
    all_pattern: str | None = None  # export that also holds the out-of-scope rows (INCLUDE_OUT_OF_SCOPE)
    in_scope_default: bool = False  # the in-scope file has no in_scope column: every row counts
    known_gaps: tuple[str, ...] = ()  # coverage gaps stated in scraper_status.md, shown in the summary
    required: tuple[str, ...] = ()  # extra columns the logic reads (merge keys): missing = schema drift, like a mapped column


COMMON_LOGIC = {"location_id": "~location_id", "scraped_at": "scraped_at"}

SOURCES: dict[str, SourceConfig] = {
    "gtue": SourceConfig(
        slug="gtue", label="GTÜ", in_scope_pattern="gtue_pruefstellen_*.csv", all_pattern="gtue_all_*.csv",
        proof="summary", status_count=1394,
        columns={**COMMON_LOGIC, "name": "name", "street": "street", "zip": "zip", "city": "city", "lat": "lat",
                 "lng": "lng", "phone": "phone", "email": "email", "website": "website",
                 "gtue_profile_url": "profile_url", "booking_url": "booking_url", "opening_hours": "opening_hours",
                 "services": "services", "country": "~country", "site_type": "~site_type",
                 "site_type_evidence": "~evidence", "in_scope": "in_scope", "scope_reason": "scope_reason",
                 "needs_review": "~needs_review", "needs_review_reason": "~reasons"},
        ignored=("source_org", "source_id", "state", "plz_distance_km"),
        known_gaps=("Prüfstützpunkte (about 10,400 PSPs) are not listed in the finder (out of scope).",),
    ),
    "kues": SourceConfig(
        slug="kues", label="KÜS", in_scope_pattern="kues_pruefstellen_*.csv", all_pattern="kues_all_*.csv",
        proof="summary", status_count=1099,  # 582 own + 517 psp = Yes (2026-10-07); scraper_status.md still says 587
        columns={**COMMON_LOGIC, "display_name": "name", "street": "street", "zip": "zip", "city": "city", "lat": "lat",
                 "lng": "lng", "phone": "phone", "email": "email", "website": "website",
                 "source_profile_url": "profile_url", "country": "~country", "site_type": "~site_type",
                 "site_type_evidence": "~evidence", "in_scope": "in_scope", "scope_reason": "scope_reason",
                 "psp": "psp", "doubt_flags": "~flags", "needs_review": "~needs_review", "needs_review_reason": "~reasons"},
        ignored=("source_org", "source_id", "state", "plz_distance_km"),
        known_gaps=("Records with pruefungvorortmoeglich = 0 are in scope since 2026-10-07 and carry psp = Yes; the ~8,000 PSPs are not listed.",
                    "East is thin (Sachsen, Thüringen, Mecklenburg-Vorpommern); both radii agree."),
    ),
    "dekra": SourceConfig(
        slug="dekra", label="DEKRA", in_scope_pattern="dekra_pruefstellen_*.csv", all_pattern="dekra_all_*.csv",
        proof="summary", status_count=826,
        columns={**COMMON_LOGIC, "name": "name", "name_addon": "operator_name", "street": "street", "zip": "zip",
                 "city": "city", "lat": "lat", "lng": "lng", "phone": "phone", "email": "email",
                 "website": "website", "dekra_profile_url": "profile_url", "booking_url": "booking_url",
                 "opening_hours": "opening_hours", "services": "services", "country": "~country",
                 "site_type": "~site_type", "site_type_evidence": "~evidence", "in_scope": "in_scope",
                 "scope_reason": "scope_reason", "doubt_flags": "~flags", "needs_review": "~needs_review",
                 "needs_review_reason": "~reasons"},
        ignored=("source_org", "source_id", "state", "plz_distance_km"),
        known_gaps=("Included on purpose (decision 2026-10-05): PSP-like, thin and untyped records, flagged only.",),
    ),
    "fsp": SourceConfig(
        slug="fsp", label="FSP", in_scope_pattern="fsp_pruefstellen_*.csv", proof="summary", status_count=169,
        in_scope_default=True,
        columns={**COMMON_LOGIC, "name": "name", "street": "street", "plz": "zip", "city": "city",
                 "email": "email", "phone": "phone", "website_url": "website", "booking_url": "booking_url",
                 "opening_hours_raw": "opening_hours", "detail_url": "profile_url", "scope_reason": "scope_reason",
                 "doubt_flags": "~flags", "needs_review": "~needs_review"},
        ignored=("source", "source_id", "hours_mo", "hours_di", "hours_mi", "hours_do", "hours_fr", "hours_sa",
                 "hours_so", "hours_other_days"),
        known_gaps=("Coverage proven only between list and sitemap; a site in neither is invisible.",
                    "No coordinates and no PLZ for 1 sitemap-only page; geocoded here."),
    ),
    "tuev_sued": SourceConfig(
        slug="tuev_sued", label="TÜV SÜD", in_scope_pattern="tuev_sued_pruefstellen_*.csv",
        all_pattern="tuev_sued_all_*.csv", proof="summary", status_count=718, required=("location_sign",),
        columns={**COMMON_LOGIC, "name": "name", "street": "street", "zip": "zip", "city": "city", "lat": "lat",
                 "lng": "lng", "phone": "phone", "email": "email", "website": "website",
                 "source_profile_url": "profile_url", "booking_url": "booking_url", "opening_hours": "opening_hours",
                 "services": "services", "operator_group": "operator_group", "country": "~country",
                 "site_type": "~site_type", "site_type_evidence": "~evidence", "in_scope": "in_scope",
                 "scope_reason": "scope_reason", "doubt_flags": "~flags", "needs_review": "~needs_review",
                 "needs_review_reason": "~reasons"},
        ignored=("source_org", "source_id", "state", "plz_distance_km"),
        known_gaps=("HU at workshop PSPs is not listed; the finder seems to cap at ~300 km (proof stays valid).",),
    ),
    "tuev_nord": SourceConfig(
        slug="tuev_nord", label="TÜV NORD", in_scope_pattern="tuev_nord_hu_*.csv", all_pattern="tuev_nord_all_*.csv",
        proof="summary", status_count=497,
        columns={**COMMON_LOGIC, "name_clean": "name", "operator_name": "operator_name", "street": "street",
                 "zip": "zip", "city": "city", "latitude": "lat", "longitude": "lng", "detail_url": "profile_url",
                 "site_type": "~site_type", "site_type_reason": "~evidence", "in_scope": "in_scope",
                 "dq_flags": "~flags", "needs_review": "~needs_review"},
        ignored=("source", "listed_by", "plz_distance_km"),
        known_gaps=("5 list-only pages and 2 pageless stations not decided; HU at workshop PSPs probably not listed.",
                    "site_type is a name heuristic; 'own' is only probable."),
    ),
    "tuev_rheinland": SourceConfig(
        slug="tuev_rheinland", label="TÜV Rheinland", in_scope_pattern="tuev_rheinland_pruefstellen_*.csv",
        proof="filename", status_count=341,
        columns={**COMMON_LOGIC, "name": "name", "street": "street", "zip": "zip", "city": "city", "lat": "lat",
                 "lng": "lng", "phone": "phone", "booking_url": "booking_url", "source_profile_url": "profile_url",
                 "opening_hours": "opening_hours", "operator_group": "operator_group", "site_type": "~site_type",
                 "site_type_evidence": "~evidence", "in_scope": "in_scope", "needs_review": "~needs_review",
                 "needs_review_reason": "~reasons"},
        ignored=("source_org", "state", "plz_distance_km"),
        known_gaps=("Mobile PSPs and non-VIS types are not exported; no pytest suite and no run_summary for this source.",
                    "Losheim Saarbrücker Str. 221 was added from the landing page; Wadern-Lockweiler unresolved."),
    ),
    "tuev_hessen": SourceConfig(
        slug="tuev_hessen", label="TÜV Hessen", in_scope_pattern="tuev_hessen_hu_*.csv",
        all_pattern="tuev_hessen_all_*.csv", proof="summary", status_count=113, required=("location_sign",),
        columns={**COMMON_LOGIC, "title": "name", "operator": "~operator", "street": "street", "zip": "zip",
                 "city": "city", "latitude": "lat", "longitude": "lng", "phone": "phone",
                 "opening_hours": "opening_hours", "booking_url": "booking_url", "websites": "website",
                 "detail_url": "profile_url", "betreiber_text": "operator_name", "site_type": "~site_type",
                 "in_scope": "in_scope", "scope_reason": "scope_reason", "dq_flags": "~flags",
                 "needs_review": "~needs_review"},
        ignored=("source", "source_id", "listed_by", "plz_distance_km"),
        known_gaps=("HU stations inside workshops are probably not listed; 'Alle Standorte' is limited to three fixed tags.",
                    "robots.txt not checked (Nikos's decision)."),
    ),
    "tuev_thueringen": SourceConfig(
        slug="tuev_thueringen", label="TÜV Thüringen", in_scope_pattern="tuev_thueringen_pruefstellen_*.csv",
        proof="filename", status_count=12,
        columns={**COMMON_LOGIC, "name": "name", "street": "street", "zip": "zip", "city": "city", "lat": "lat",
                 "lng": "lng", "phone": "phone", "email": "email", "website": "website", "booking_url": "booking_url",
                 "source_profile_url": "profile_url", "opening_hours": "opening_hours", "site_type": "~site_type",
                 "site_type_evidence": "~evidence", "in_scope": "in_scope", "doubt_flags": "~flags",
                 "needs_review": "~needs_review", "needs_review_reason": "~reasons"},
        ignored=("source_org", "source_id", "state", "plz_distance_km"),
        known_gaps=("HU at workshop PSPs and outside Thüringen not listed; no pytest suite and no run_summary.",),
    ),
    "vuek": SourceConfig(
        slug="vuek", label="VÜK", in_scope_pattern="vuek_pruefstellen_*.csv", proof="summary", status_count=5,
        columns={**COMMON_LOGIC, "name": "name", "street": "street", "zip": "zip", "city": "city", "lat": "lat",
                 "lng": "lng", "coord_source": "~coord_source", "phone": "phone", "email": "email",
                 "website": "website", "source_profile_url": "profile_url", "opening_hours": "opening_hours",
                 "services": "services", "country": "~country", "site_type": "~site_type",
                 "site_type_evidence": "~evidence", "in_scope": "in_scope", "scope_reason": "scope_reason",
                 "doubt_flags": "~flags", "needs_review": "~needs_review", "needs_review_reason": "~reasons"},
        ignored=("source_org", "source_id", "state", "plz_distance_km"),
        known_gaps=("Partner workshops (PSPs) are not listed; the five stations are proven against the site's own number.",),
    ),
}

# Columns with no master home that were present when this config was written (Phase A inventory, 2026-10-06).
# They are written to the per-source extra file. A column that is in neither this list nor the column map is
# reported as new (warning); a listed column that vanished is reported as missing (warning).
KNOWN_EXTRAS: dict[str, tuple[str, ...]] = {
    "gtue": ("name_addon", "name_addon2", "mobile", "offers_hu", "inspects_on_site", "herkunft", "source_data_date",
             "source_last_change", "scope_tier", "same_site_ids"),
    "kues": ("name_addon", "name", "location_precision", "kues_licensee", "pruefung_vor_ort", "kues_list",
             "name_class", "licensee_site_count", "licensee_has_inscope_site", "same_address_count", "is_hdp_site"),
    "dekra": ("name_addon2", "mobile", "dekra_types", "nfz", "hu_mention", "same_address_count"),
    "fsp": ("type_badges", "in_locator"),
    "tuev_sued": ("heavy_vehicles", "location_sign", "aspro_id", "market_area_id", "federal_state_id", "company_id",
                  "tsc_status", "huau", "current_notices", "currently_closed"),
    "tuev_nord": ("source_id", "source_key", "name", "place_name", "is_hu_site", "is_office", "service_type",
                  "station_slug", "link_variant", "link_raw", "in_list_page", "same_id_count", "same_address_count",
                  "psp_suspect", "unknown_source_fields"),
    "tuev_rheinland": ("source_id", "vic_id", "external_db", "appointment_available", "address_raw"),
    "tuev_hessen": ("title_rest", "is_hu_site", "is_lkw_site", "tags", "tag_title_mismatch", "tag_title_mismatch_reason",
                    "mobile", "fax", "location_sign", "contact_url", "hu_evidence", "notices", "infotext_text", "uid",
                    "pid", "es_id", "es_index", "is_service_center", "score_factor", "sort_values", "es_ignored",
                    "tags_json", "unknown_source_fields"),
    "tuev_thueringen": ("name_overlay", "opening_hours_raw"),
    "vuek": ("mobile", "fax", "opening_hours_raw", "notice", "hu_evidence", "content_hash", "is_hdp_site"),
}

# Manual pin per source: a run folder name. A pin replaces "newest", it does not waive the qualification rules.
PINS: dict[str, str] = {}


@dataclass
class Candidate:
    slug: str
    run_dir: Path
    mode: str
    started: datetime
    qualifies: bool
    why: str
    file: Path | None = None
    root_started: datetime | None = None  # start of the full run the data came from (a rebuild does not make data newer)
    all_file: Path | None = None
    summary: dict = field(default_factory=dict)


def read_source_csv(path: Path) -> pd.DataFrame:
    """Every export is read as text: PLZ, location_sign and ids keep their leading zeros."""
    return pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _load_summary(run_dir: Path) -> dict | None:
    f = run_dir / paths.RUN_SUMMARY_JSON
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"_unreadable": True}


def _started(run_dir: Path, summary: dict | None) -> datetime:
    for key in ("started_at", "started"):
        value = (summary or {}).get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).replace(tzinfo=None)  # naive, so runs sort even if one carries an offset
            except ValueError:
                pass
    m = RUN_RE.match(run_dir.name)
    return datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H%M")


def _summary_passed(summary: dict) -> tuple[bool, str]:
    status = summary.get("status")
    outcome = summary.get("outcome")
    if isinstance(status, str):
        return (status.upper() == "PASS"), f"run_summary status={status}"
    if isinstance(outcome, str):
        return outcome.upper().startswith("PASS"), f"run_summary outcome={outcome[:40]}"
    return False, "run_summary has no status/outcome"


def _source_names(summary: dict) -> list[str]:
    raw = summary.get("source_runs")
    if raw is None:
        raw = summary.get("source_run")
    if isinstance(raw, str):
        raw = [raw] if raw else []
    return [re.split(r"[\\/]", str(r).rstrip("\\/"))[-1] for r in (raw or [])]


def summary_in_scope(summary: dict) -> int | None:
    """The in-scope count a scraper wrote into its own run_summary.json (None if it has none or it is not a plain number)."""
    for value in (summary.get("in_scope"), (summary.get("counts") or {}).get("in_scope") if isinstance(summary.get("counts"), dict) else None):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _marked(name: str) -> bool:
    return any(m in name for m in BAD_MARKERS)


def _find_file(run_dir: Path, pattern: str) -> tuple[Path | None, str]:
    hits = [p for p in run_dir.glob(pattern) if not _marked(p.name)]
    marked = [p for p in run_dir.glob(pattern) if _marked(p.name)]
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        return None, f"{len(hits)} files match {pattern}"
    return None, ("only marked file(s): " + ", ".join(p.name for p in marked)) if marked else f"no file matches {pattern}"


def _evaluate(cfg: SourceConfig, run_dir: Path, others: dict[str, Candidate], depth: int = 0) -> Candidate:
    m = RUN_RE.match(run_dir.name)
    summary = _load_summary(run_dir)
    cand = Candidate(cfg.slug, run_dir, m.group(3), _started(run_dir, summary), False, "", summary=summary or {})
    if _marked(run_dir.name):
        cand.why = "run folder name carries a failure marker"
        return cand
    if cfg.proof == "summary":
        if summary is None:
            cand.why = "no run_summary.json"
            return cand
        passed, evidence = _summary_passed(summary)
        if not passed:
            cand.why = f"not PASS ({evidence})"
            return cand
        proof = evidence
    else:
        if summary is not None:
            passed, evidence = _summary_passed(summary)
            if not passed:
                cand.why = f"not PASS ({evidence})"
                return cand
        proof = "file name has no unverified marker"
    if cand.mode == "rebuild":
        names = _source_names(summary or {})
        if not names:
            cand.why = "rebuild names no source run"
            return cand
        out = paths.output_dir(cfg.slug, "full", root=run_dir.parents[3])
        for name in names:
            src = out / name
            if not src.is_dir() or not RUN_RE.match(name):
                cand.why = f"rebuilds {name}, which is not a run folder in output/"
                return cand
            if depth > 3:
                cand.why = "rebuild chain too deep"
                return cand
            if name not in others:
                others[name] = _evaluate(cfg, src, others, depth + 1)
            if not others[name].qualifies:
                cand.why = f"rebuilds {name}, which does not pass ({others[name].why})"
                return cand
        proof += f"; rebuilds passing {', '.join(names)}"
        cand.root_started = min(others[n].root_started or others[n].started for n in names)
    file, problem = _find_file(run_dir, cfg.in_scope_pattern)
    if file is None:
        cand.why = f"no usable in-scope file ({problem})"
        return cand
    cand.file = file
    if cfg.all_pattern:
        cand.all_file, _ = _find_file(run_dir, cfg.all_pattern)
    cand.root_started = cand.root_started or cand.started
    cand.qualifies = True
    cand.why = f"{cand.mode}; {proof}"
    return cand


def candidates(slug: str, root: Path | None = None) -> list[Candidate]:
    """Every full/rebuild run folder of `output/`, newest first, each with its verdict."""
    cfg = SOURCES[slug]
    out = paths.output_dir(slug, "full", root=root)
    if not out.is_dir():
        return []
    cache: dict[str, Candidate] = {}
    for d in sorted(out.iterdir()):
        if d.is_dir() and RUN_RE.match(d.name) and d.name not in cache:
            cache[d.name] = _evaluate(cfg, d, cache)
    return sorted(cache.values(), key=lambda c: (c.started, c.run_dir.name), reverse=True)


def select_run(slug: str, root: Path | None = None, pins: dict[str, str] | None = None) -> Candidate:
    """The qualifying run for a source; raises with the reason of every run if none qualifies."""
    cands = candidates(slug, root)
    pin = (pins if pins is not None else PINS).get(slug)
    if pin:
        hit = [c for c in cands if c.run_dir.name == pin]
        if not hit:
            raise LookupError(f"{slug}: pinned run {pin} not found in output/")
        if not hit[0].qualifies:
            raise LookupError(f"{slug}: pinned run {pin} does not qualify ({hit[0].why})")
        hit[0].why += " (pinned)"
        return hit[0]
    for c in cands:
        if c.qualifies:
            return c
    detail = "; ".join(f"{c.run_dir.name}: {c.why}" for c in cands) or "no run folders in output/"
    raise LookupError(f"{slug}: no qualifying input run ({detail})")


def no_home(slug: str, header: list[str]) -> list[str]:
    """Source columns that have no master column: they go to the per-source extra file."""
    cfg = SOURCES[slug]
    return [c for c in header if c not in cfg.columns and c not in cfg.ignored]


def missing_columns(slug: str, header: list[str]) -> list[str]:
    """Columns the config expects (mapped or ignored-by-design) but the export lacks: schema drift."""
    cfg = SOURCES[slug]
    expected = list(cfg.columns) + list(cfg.ignored) + list(cfg.required)
    optional = {"scope_reason", "country", "state"}
    return [c for c in expected if c not in header and c not in optional]


def column_drift(slug: str, header: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(missing, new, vanished_extras): missing = used columns gone (FAIL); new = unknown columns and
    vanished_extras = known extra columns gone (both only warnings; new columns still go to the extra file)."""
    cfg = SOURCES[slug]
    known = set(cfg.columns) | set(cfg.ignored) | set(KNOWN_EXTRAS.get(slug, ()))
    new = [c for c in header if c not in known]
    gone = [c for c in KNOWN_EXTRAS.get(slug, ()) if c not in header]
    return missing_columns(slug, header), new, gone


def inventory(root: Path | None = None, pins: dict[str, str] | None = None) -> list[dict]:
    rows = []
    for slug in SOURCE_ORDER:
        cfg = SOURCES[slug]
        try:
            c = select_run(slug, root, pins)
        except LookupError as exc:
            rows.append({"slug": slug, "error": str(exc), "status_count": cfg.status_count})
            continue
        df = read_source_csv(c.file)
        if "in_scope" in df.columns:
            n_scope = int(df["in_scope"].str.strip().str.lower().isin(["true", "1", "yes"]).sum())
        else:
            n_scope = len(df) if cfg.in_scope_default else None
        rows.append({
            "slug": slug, "run": c.run_dir.name, "mode": c.mode, "why": c.why, "file": c.file.name,
            "rows": len(df), "in_scope": n_scope, "status_count": cfg.status_count,
            "matches_status": n_scope == cfg.status_count, "columns": len(df.columns),
            "no_home": no_home(slug, list(df.columns)), "missing": missing_columns(slug, list(df.columns)),
            "newer_not_qualifying": [f"{x.run_dir.name} ({x.why})" for x in candidates(slug, root)
                                      if x.started > c.started and not x.qualifies],
            "location_ids_unique": bool(df["location_id"].is_unique),
        })
    return rows


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_manifest(selected: dict[str, Candidate], rows_in: dict[str, int], root: Path | None = None) -> pd.DataFrame:
    """One line per source: the run used, the PASS evidence, the file, its hash and row count."""
    base = paths.project_root() if root is None else Path(root)
    lines = []
    for slug in SOURCE_ORDER:
        c = selected.get(slug)
        if c is None:
            continue
        lines.append({
            "slug": slug, "run_folder": c.run_dir.name, "mode": c.mode, "pass_evidence": c.why,
            "file": relative(c.file, base), "data_started": (c.root_started or c.started).isoformat(timespec="seconds"),
            "rows": rows_in.get(slug, ""), "sha256": sha256(c.file),
        })
    return pd.DataFrame(lines)


def _print(rows: list[dict]) -> None:
    for r in rows:
        print(json.dumps(r, ensure_ascii=False, default=str))


if __name__ == "__main__":
    _print(inventory())
