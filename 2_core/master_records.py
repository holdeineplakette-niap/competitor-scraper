"""Source exports -> uniform records (one dict per input row) for the aggregator.

Reads every export as text, checks the schema against the config (a used column that vanished is a hard error),
cleans PLZ with the explicit corrections table, maps uo and site_type through the tables (unmapped = hard error)
and turns the source's own flags into doubt-flag tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from core import master_inputs as mi
from core import master_schema as ms
from core import normalise as nm


class SchemaDrift(Exception):
    """A column the config relies on is missing from a source export."""


@dataclass
class LoadInfo:
    slug: str
    file: str
    rows: int = 0
    warnings: list[str] = field(default_factory=list)
    plz_corrections: list[dict] = field(default_factory=list)
    plz_padded: int = 0
    header: list[str] = field(default_factory=list)


def _num(value) -> float | None:
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v if v == v else None


TRUE_TOKENS = ("true", "1", "yes", "ja")
FALSE_TOKENS = ("false", "0", "no", "nein", "")


def _truthy(value) -> bool:
    return str(value).strip().lower() in TRUE_TOKENS


def _scope(value, slug: str, lid: str) -> bool:
    """in_scope from an export: a closed vocabulary. An unknown token would drop rows silently, so it is an error."""
    token = str(value).strip().lower()
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    raise SchemaDrift(f"{slug}: in_scope value {value!r} on {lid} is not one of {TRUE_TOKENS + FALSE_TOKENS[:-1]}")


def _clean(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _tokens(src: dict, cols: dict[str, str]) -> list[str]:
    """Doubt-flag tokens of one source row: its flag columns plus its review reasons (free text -> tokens)."""
    tokens: list[str] = []
    for col in [c for c, t in cols.items() if t == "~flags"]:
        tokens += ms.split_flags(src.get(col))
    for col in [c for c, t in cols.items() if t == "~reasons"]:
        for text in ms.split_flags(src.get(col)):
            head = ms.slugify(text.split(":")[0])
            tok = ms.reason_token(text)
            if head in tokens or tok in tokens:
                continue
            tokens.append(tok)
    out = list(dict.fromkeys(tokens))
    if not out and any(_truthy(src.get(c)) for c, t in cols.items() if t == "~needs_review"):
        out = ["needs_review"]
    return out


def load_source(cand: mi.Candidate, include_out_of_scope: bool, plz_known: set[str] | None) -> tuple[list[dict], LoadInfo, pd.DataFrame]:
    cfg = mi.SOURCES[cand.slug]
    slug = cand.slug
    file = cand.file
    info = LoadInfo(slug, file.name if file else "")
    if include_out_of_scope:
        if cand.all_file is not None:
            file = cand.all_file
            info.file = file.name
        elif cfg.all_pattern:
            info.warnings.append(f"{slug}: INCLUDE_OUT_OF_SCOPE is on but the run has no `all` export; only in-scope rows read")
        else:
            info.warnings.append(f"{slug}: INCLUDE_OUT_OF_SCOPE is on but this source exports only in-scope rows")
    df = mi.read_source_csv(file)
    info.header = list(df.columns)
    info.rows = len(df)
    missing, new, gone = mi.column_drift(slug, info.header)
    if missing:
        raise SchemaDrift(f"{slug}: {file.name} lacks column(s) the config expects: {missing}. Fix the adapter export or the config on purpose.")
    if new:
        info.warnings.append(f"{slug}: new column(s) without a home, written to the extra file: {new}")
    if gone:
        info.warnings.append(f"{slug}: known extra column(s) no longer in the export: {gone}")
    if "location_id" not in df.columns or df["location_id"].eq("").any() or not df["location_id"].is_unique:
        dup = df.loc[df["location_id"].duplicated(keep=False), "location_id"].unique().tolist()[:5]
        raise SchemaDrift(f"{slug}: location_id is missing, empty or not unique in {file.name} (e.g. {dup})")

    tgt: dict[str, str] = {}  # master/logic target -> source column
    for col, target in cfg.columns.items():
        tgt.setdefault(target, col)

    def get(src, target):
        col = tgt.get(target)
        return _clean(src[col]) if col else ""

    recs = []
    for src in df.to_dict("records"):
        rec: dict = {"slug": slug, "family": ms.family_of(slug), "rank": ms.rank(slug), "src": src,
                     "location_id": src["location_id"]}
        for f in ("name", "street", "city", "phone", "email", "website", "booking_url", "profile_url",
                  "opening_hours", "services", "operator_name", "scope_reason", "scraped_at"):
            rec[f] = get(src, f)
        rec["opening_hours"] = _clean(src[tgt["opening_hours"]]) if "opening_hours" in tgt else ""
        rec["zip_raw"] = get(src, "zip")
        rec["zip"], how = nm.plz_clean(rec["zip_raw"], slug, rec["city"], ms.PLZ_CORRECTIONS)
        if how == "corrected":
            info.plz_corrections.append({"slug": slug, "location_id": rec["location_id"], "published": rec["zip_raw"],
                                         "corrected": rec["zip"], "city": rec["city"]})
        elif how == "padded":
            info.plz_padded += 1
        rec["lat"], rec["lng"] = _num(src.get(tgt.get("lat", ""))), _num(src.get(tgt.get("lng", "")))
        rec["coord_hint"] = get(src, "~coord_source")
        rec["in_scope"] = True if cfg.in_scope_default and "in_scope" not in tgt else _scope(src.get(tgt.get("in_scope", ""), ""), slug, rec["location_id"])
        flags = _tokens(src, cfg.columns)
        # uo
        col, table = ms.UO_MAP[slug]
        key = "*" if col == "*" else src.get(col, "")
        if key not in table:
            raise ValueError(f"{slug}: {col} value {key!r} has no uo in UO_MAP; add it on purpose")
        rec["uo"] = table[key]
        if (slug, key) in ms.UO_ASSUMED:
            flags.append(f"uo_assumed:{ms.UO_ASSUMED[(slug, key)]}")
        # site type
        scol, _ = ms.SITE_TYPE_MAP[slug]
        skey = src.get(scol, "") if scol != "*" else "*"
        rec["site_type"] = ms.mapped(ms.SITE_TYPE_MAP, slug, skey, "site_type")
        if slug == "dekra" and rec["site_type"] == "own_station" and "non_dekra_operator" in flags:
            rec["site_type"] = "partner"
        src_type = src.get(tgt.get("~site_type", ""), "") if "~site_type" in tgt else ""
        evidence = [f"{slug}:{src_type or 'listed'}"]
        if slug in ("tuev_sued", "tuev_rheinland") and src.get("operator_group", "") != "":
            evidence.append(f"{slug}:operator_group={src['operator_group']}")
        if slug == "tuev_hessen":
            evidence.append(f"{slug}:operator={src.get('operator', '')}")
        ev_col = tgt.get("~evidence")
        if ev_col and src.get(ev_col):
            evidence.append(f"{slug}:{_clean(src[ev_col])}")
        rec["site_type_evidence"] = "; ".join(evidence)
        # operator group
        og = src.get("operator_group") if "operator_group" in tgt else None
        if og is not None and og != "":
            og = ms.OPERATOR_GROUP_RENAME.get((slug, og), og)
        elif slug in ms.OPERATOR_GROUP_FROM_SITE_TYPE:
            og = ms.OPERATOR_GROUP_FROM_SITE_TYPE[slug][src.get("site_type", "")]
        elif og in (None, "") and slug in ms.OPERATOR_GROUP_CONSTANT:
            og = ms.OPERATOR_GROUP_CONSTANT[slug]
        rec["operator_group"] = og or ""
        rec["psp"] = ms.psp_value(slug, get(src, "psp"))
        country = src.get(tgt.get("~country", ""), "") if "~country" in tgt else ""
        if country.strip().upper() not in ("", "D", "DE"):
            flags.append(f"country_not_de:{country.strip()}")
        rec["flags"] = list(dict.fromkeys(flags))
        rec["sign"] = src.get("location_sign", "").strip() if slug in ms.SIGN_SOURCES else ""
        rec["veto"] = src.get(ms.WITHIN_SOURCE_VETO[slug], "").strip().casefold() if slug in ms.WITHIN_SOURCE_VETO else ""
        rec["akey"] = nm.address_key(rec["street"], rec["zip"]) if nm.has_street(rec["street"]) and rec["zip"] else None
        recs.append(rec)
    return recs, info, df


def extras_frame(slug: str, df: pd.DataFrame) -> pd.DataFrame:
    """The columns of a source export that have no master home (known and new ones), keyed by location_id."""
    cols = [c for c in mi.no_home(slug, list(df.columns))]
    return df[["location_id"] + [c for c in cols if c != "location_id"]].copy()


def load_all(selected: dict[str, mi.Candidate], include_out_of_scope: bool, plz_known: set[str] | None):
    recs: list[dict] = []
    infos: dict[str, LoadInfo] = {}
    extras: dict[str, pd.DataFrame] = {}
    for slug in mi.SOURCE_ORDER:
        r, info, df = load_source(selected[slug], include_out_of_scope, plz_known)
        recs += r
        infos[slug] = info
        extras[slug] = extras_frame(slug, df)
    recs.sort(key=lambda r: (r["rank"], r["location_id"]))
    return recs, infos, extras
