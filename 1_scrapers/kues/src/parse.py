"""KÜS parsing: one API record -> one row of the candidate schema (project plan §3), plus the extra KÜS columns.

Pure functions, no network. Classification (site_type, in_scope, flags) is in classify.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from core import enrich, paths

SLUG = "kues"
SOURCE_ORG = "KÜS"
ID_PREFIX = "KUES-"
HOME_COUNTRY = "DE"
PARTNER_HOST = "https://partner.kues.de/"  # the site's own link for a partner with an alias but no website
HDP_SITES_FILE = ("hdp", "hdp_sites.csv")  # under 4_reference/, optional, never created by the code

COLUMNS = [
    "location_id",
    "source_org",
    "source_id",
    "name",
    "name_addon",
    "display_name",
    "street",
    "zip",
    "city",
    "state",
    "country",
    "lat",
    "lng",
    "location_precision",
    "phone",
    "email",
    "website",
    "source_profile_url",
    "kues_licensee",
    "pruefung_vor_ort",
    "psp",
    "kues_list",
    "name_class",
    "licensee_site_count",
    "licensee_has_inscope_site",
    "same_address_count",
    "plz_distance_km",
    "is_hdp_site",
    "site_type",
    "site_type_evidence",
    "in_scope",
    "scope_reason",
    "doubt_flags",
    "needs_review",
    "needs_review_reason",
    "scraped_at",
]

# Fields whose value depends on the query (which PLZ, how far away), not on the site: ignored when the same
# adressid comes back twice and the content is compared.
QUERY_DEPENDENT_FIELDS = ("entfernung", "inbreite", "inlaenge", "plzorder")

# `name` that is only a salutation (+ title): the person is in `zusatz`.
_SALUTATION = re.compile(
    r"^\s*(Herrn?|Frau|Herr und Frau)(\s+(Dipl\.?-?\s?Ing\.?|Ing\.?|Dr\.?|Prof\.?|Dipl\.?-?\s?Kfm\.?)(\s*\(FH\))?)*\s*$",
    re.IGNORECASE,
)
_LEADING_SALUTATION = re.compile(r"^\s*(Herrn?|Frau)\s+", re.IGNORECASE)


def clean(value) -> str | None:
    """Strip, collapse inner whitespace; empty strings and None become None."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def zip_of(value) -> str | None:
    z = clean(value)
    return z.zfill(5) if z and z.isdigit() else z


def is_salutation_only(name: str | None) -> bool:
    return bool(name) and bool(_SALUTATION.match(name))


def display_name(name: str | None, addon: str | None) -> str | None:
    """`name`, unless it is only a salutation ("Herrn", "Herrn Dipl.-Ing. (FH)"): then name and addon are joined
    and the leading salutation word dropped ("Herrn" + "Dipl.-Ing. (FH) Michael Beer" -> "Dipl.-Ing. (FH) Michael Beer")."""
    if not is_salutation_only(name):
        return name
    joined = clean(f"{name or ''} {addon or ''}")
    joined = _LEADING_SALUTATION.sub("", joined) if joined else joined
    return clean(joined) or name


def parse_record(rec: dict, scraped_at: str) -> dict:
    """The mapped row (no classification, no counts, no GeoNames enrichment yet)."""
    source_id = clean(rec.get("adressid"))
    name, addon = clean(rec.get("name")), clean(rec.get("zusatz"))
    street = clean(rec.get("strasse"))
    lat, lng = to_float(rec.get("lat")), to_float(rec.get("lon"))
    no_street = str(rec.get("keinestrasse")) == "1"
    alias = clean(rec.get("partneralias"))
    vorort = {"1": True, "0": False}.get(str(rec.get("pruefungvorortmoeglich")))
    return {
        "location_id": f"{ID_PREFIX}{source_id}",
        "source_org": SOURCE_ORG,
        "source_id": source_id,
        "name": name,
        "name_addon": addon,
        "display_name": display_name(name, addon),
        "street": street,
        "zip": zip_of(rec.get("plz")),
        "city": clean(rec.get("ort")),
        "state": None,  # filled from the PLZ (core.enrich)
        "country": None,  # set in enrich_german: DE only if the PLZ is known and the coordinates fit
        "lat": lat,
        "lng": lng,
        "location_precision": "plz_only" if (no_street or lat is None or lng is None or not street) else "address",
        "phone": clean(rec.get("tel1")),
        "email": clean(rec.get("email")),
        "website": clean(rec.get("http")),
        "source_profile_url": f"{PARTNER_HOST}{alias}" if alias else None,
        "kues_licensee": clean(rec.get("lizenznehmer")),
        "pruefung_vor_ort": vorort,
        "psp": None,  # set by classify.classify_frame: "Yes" = vorort 0 (inspects at PSPs), "No" = vorort 1
        "kues_list": None,  # set by the run (which list(s) returned the record)
        "name_class": None,  # set by classify.classify_frame (information only)
        "licensee_site_count": None,
        "licensee_has_inscope_site": None,  # set by classify.classify_frame
        "same_address_count": None,
        "plz_distance_km": None,
        "is_hdp_site": None,
        "site_type": "unknown",
        "site_type_evidence": None,
        "in_scope": False,
        "scope_reason": None,
        "doubt_flags": None,
        "needs_review": False,
        "needs_review_reason": None,
        "scraped_at": scraped_at,
    }


def address_key(street, zip_code) -> tuple[str, str]:
    """Normalised street + PLZ, for same-address counts."""
    s = (street if isinstance(street, str) else "").lower().replace("ß", "ss")
    s = re.sub(r"stra(ss|ß)e|str\.", "str", s)
    z = zip_code if isinstance(zip_code, str) else ""
    return re.sub(r"[^a-z0-9äöü]", "", s), z.replace(" ", "")


def add_counts(df: pd.DataFrame) -> pd.DataFrame:
    """licensee_site_count (rows of the same licence code) and same_address_count (rows with the same normalised
    street + PLZ, itself included; a row without street or PLZ counts 1). Never used to merge or drop rows."""
    out = df.copy()
    if out.empty:
        return out
    out["licensee_site_count"] = out.groupby("kues_licensee", dropna=False)["source_id"].transform("count")
    keys = [address_key(s, z) for s, z in zip(out["street"], out["zip"])]
    freq: dict = {}
    for k in keys:
        freq[k] = freq.get(k, 0) + 1
    out["same_address_count"] = [freq[k] if k[0] and k[1] else 1 for k in keys]
    return out


def build_frame(records: list[dict], scraped_at: str) -> pd.DataFrame:
    return pd.DataFrame([parse_record(r, scraped_at) for r in records], columns=COLUMNS)


# --------------------------------------------------------------------------
# Enrichment (GeoNames) and the optional HDP hook
# --------------------------------------------------------------------------

def enrich_german(df: pd.DataFrame, centroids: pd.DataFrame) -> pd.DataFrame:
    """core.enrich (state, plz_distance_km, "coordinate far from PLZ" / "unknown PLZ"), then `country`:
    DE only if the PLZ is a known German PLZ and the coordinates (when there are any) are plausible for it.
    Every other row gets country None and the reason "country unverified"."""
    out = enrich.enrich(df, centroids)
    far = out["needs_review_reason"].map(lambda v: enrich.REASON_FAR_FROM_PLZ in enrich.split_reasons(v))
    unknown = out["needs_review_reason"].map(lambda v: enrich.REASON_UNKNOWN_PLZ in enrich.split_reasons(v))
    ok = ~(far | unknown)
    out["country"] = ok.map({True: HOME_COUNTRY, False: None})
    for idx in out.index[~ok]:
        out.at[idx, "needs_review_reason"] = enrich.add_reason(out.at[idx, "needs_review_reason"], "country unverified")
    return out


def hdp_path(root=None) -> Path:
    return paths.reference_dir(root).joinpath(*HDP_SITES_FILE)


def load_hdp_rules(path: str | Path | None = None, root=None) -> list[dict] | None:
    """The optional file 4_reference/hdp/hdp_sites.csv (columns kues_licensee, adressid, address; any may be empty).
    None if the file does not exist. Rows with all three empty are ignored."""
    path = Path(path) if path is not None else hdp_path(root)
    if not path.exists():
        return None
    frame = pd.read_csv(path, sep=None, engine="python", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    rules = []
    for row in frame.to_dict("records"):
        rule = {k: (row.get(k) or "").strip() for k in ("kues_licensee", "adressid", "address")}
        if any(rule.values()):
            rules.append(rule)
    return rules


def mark_hdp(df: pd.DataFrame, rules: list[dict] | None) -> pd.DataFrame:
    """is_hdp_site: True when a rule of hdp_sites.csv matches. A rule matches when ALL its non-empty columns match:
    adressid equals source_id, kues_licensee equals the licence code, address is contained in street + PLZ + city
    (normalised). Without the file (rules None) the column stays empty (None), not False."""
    out = df.copy()
    if rules is None:
        out["is_hdp_site"] = None
        return out

    def norm(text) -> str:
        return re.sub(r"[^a-z0-9äöü]", "", address_key(text, "")[0])

    hits = []
    for _, r in out.iterrows():
        full = norm(f"{r['street'] or ''}{r['zip'] or ''}{r['city'] or ''}")
        hit = False
        for rule in rules:
            if rule["adressid"] and rule["adressid"] != r["source_id"]:
                continue
            if rule["kues_licensee"] and rule["kues_licensee"] != r["kues_licensee"]:
                continue
            if rule["address"] and norm(rule["address"]) not in full:
                continue
            hit = True
            break
        hits.append(hit)
    out["is_hdp_site"] = hits
    return out
