"""KÜS classification: site_type, in_scope, evidence, doubt flags.

Decision (management via Nikos, 2026-10-05): only actual Prüfstellen are in scope.
  pruefungvorortmoeglich = "1"  -> site_type pruefstelle,    in_scope True, psp "No",  evidence "vorort:1"
  pruefungvorortmoeglich = "0"  -> site_type partner_office, in_scope True, psp "Yes", evidence "vorort:0"
                                   (the engineer inspects at partner workshops (PSPs), not at this address)
  anything else                 -> site_type unknown,        in_scope False, psp empty (needs_review)
Change (management via Nikos, 2026-10-07): the vorort = 0 records (~500) are now in scope too, tagged psp = "Yes",
so the PSP-type locations are in the data. The column `psp` is the tag: "Yes" = vorort 0, "No" = vorort 1. The info
flags below keep their old meaning by looking at the psp = "No" rows (the own Prüfstellen) instead of at in_scope.
Exception to the exception (Nikos, 2026-10-06/07: certain cause): a vorort = 0 record whose own name says
Prüfstelle / Prüfzentrum / Prüfingenieur contradicts the flag; it stays in scope as site_type unknown, needs_review
(flag name_pruefstelle_vorort0), unless Nikos checked it by hand (a Gutachter, a workshop that is a PSP, a
non-Prüfstelle): those are listed in VERIFIED_NOT_PRUEFSTELLE and stay out, with the reason named.
Otherwise the flags never overrule the rule and nothing is dropped: every record stays in kues_all, filtering is
the in_scope column. Two kinds of flags go into `doubt_flags`:
  review flags (REVIEW_FLAGS)  conflicts between the flag and other fields; they also set needs_review
  info flags                   evidence for the later validation (pattern analysis 2026-10-05, sources/kues.md);
                               no needs_review, no scope effect
Which side of the flag is wrong is not known yet: the analysis of one region found the flag plausible but unproven.
"""

from __future__ import annotations

import re

import pandas as pd

from core import enrich
from kues import parse

SITE_TYPES = ("pruefstelle", "partner_office", "unknown")

PSP_YES, PSP_NO = "Yes", "No"  # values of the `psp` column

REASON_VORORT_INVALID = "pruefungvorortmoeglich missing or not 0/1"
REASON_VERIFIED_NOT_PRUEFSTELLE = "checked by hand: not a Prüfstelle (pruefungvorortmoeglich = 0)"

# adressid of vorort = 0 records whose name says Prüfstelle but that Nikos checked by hand on 2026-10-06 and found
# not to be one (Gutachter without HU, workshop = PSP, non-Prüfstelle). Certain cause: they stay out.
VERIFIED_NOT_PRUEFSTELLE = frozenset({"56492", "28704", "51603"})

# review flags (set needs_review)
FLAG_NAME_PRUEFSTELLE_VORORT0 = "name_pruefstelle_vorort0"  # the name says Prüfstelle, the flag says no
FLAG_VORORT1_NO_ADDRESS = "vorort1_no_address"  # flag says on-site inspection, but no street or no coordinates
FLAG_VORORT_INVALID = "vorort_invalid"
FLAG_NAME_PSP_LIKE = "name_psp_like"  # in scope, but the name looks like a workshop or dealer (none seen in 449 records)
REVIEW_FLAGS = (FLAG_NAME_PRUEFSTELLE_VORORT0, FLAG_VORORT1_NO_ADDRESS, FLAG_VORORT_INVALID, FLAG_NAME_PSP_LIKE)

# info flags (evidence only)
FLAG_WEAK_EVIDENCE = "vorort1_weak_evidence"  # in scope, but no station word in the name, no website, no business email
FLAG_STRONG_EVIDENCE = "vorort0_strong_evidence"  # psp = Yes, but two of: station word, website, business email
FLAG_CONTACT_MISSING = "contact_missing"  # in scope, no phone, no email, no website
FLAG_SAME_ADDRESS_AS_INSCOPE = "same_address_as_inscope_site"  # psp = Yes, same street + PLZ as a psp = No row
FLAG_SHARED_ADDRESS_INSCOPE = "shared_address_in_scope"  # psp = No, same street + PLZ as another psp = No row
INFO_FLAGS = (FLAG_WEAK_EVIDENCE, FLAG_STRONG_EVIDENCE, FLAG_CONTACT_MISSING, FLAG_SAME_ADDRESS_AS_INSCOPE, FLAG_SHARED_ADDRESS_INSCOPE)

# Patterns tolerate the typos and variants seen (Ing.-Büro, Ing.Büro, Ingenieurbüo, Kfz.-, "KÜS Prüfstelle" in zusatz).
_PRUEFSTELLE = re.compile(r"pr[uü]fstelle|pr[uü]fzentrum|k[uü]s[- ]?team|kfz[- .]?pr[uü]f", re.IGNORECASE)
_STATION_HINT = re.compile(r"pr[uü]fstelle|pr[uü]fzentrum|k[uü]s|sv[- ]?team", re.IGNORECASE)
_PSP_NAME = re.compile(
    r"autohaus|autozentrum|auto[- ]?center|werkstatt|garage|reifen|karosserie|fahrzeughandel|autohandel|kfz[- ]?service|auto[- ]?service|"
    r"tuning|\b(opel|ford|toyota|kia|vw|volkswagen|renault|hyundai|bmw|audi|skoda|seat|honda|mazda|nissan|suzuki|volvo|peugeot|fiat)\b",
    re.IGNORECASE,
)
_NAME_CLASSES = (  # first hit wins
    ("herrn", re.compile(r"^\s*(herrn?|frau)\b", re.IGNORECASE)),
    ("pruefstelle", re.compile(r"pr[uü]fstelle|pr[uü]fzentrum|kfz[- .]?pr[uü]f", re.IGNORECASE)),
    ("team", re.compile(r"k[uü]s[- ]?team|sv[- ]?team", re.IGNORECASE)),
    ("kues_other", re.compile(r"k[uü]s\b|k[uü]s-", re.IGNORECASE)),
    ("ingenieurbuero", re.compile(r"ingenieurb[uü]+r?o|\bing(?:enieur)?\b.{0,40}?b[uü]+r?o", re.IGNORECASE)),
    ("sv_buero", re.compile(r"sv[- ]?b[uü]ro|sachverst[aä]ndig|gutachter", re.IGNORECASE)),
    ("person_title", re.compile(r"^\s*(dipl\.|b\.?\s?eng)", re.IGNORECASE)),
    ("company", re.compile(r"\b(gmbh|ag|gbr|partg|kg|mbh)\b", re.IGNORECASE)),
)
# Consumer mail providers: an email at one of these is "private" (a home office or one-person business).
PRIVATE_EMAIL_DOMAINS = frozenset({
    "t-online.de", "gmx.de", "gmx.net", "gmx.at", "gmx.com", "web.de", "gmail.com", "googlemail.com", "yahoo.de", "yahoo.com", "hotmail.de",
    "hotmail.com", "outlook.de", "outlook.com", "live.de", "freenet.de", "arcor.de", "aol.com", "icloud.com", "mail.de", "online.de",
    "1und1.de", "posteo.de", "protonmail.com", "kabelmail.de", "email.de", "mailbox.org", "vodafone.de", "netcologne.de", "gmx.ch",
})


def present(value) -> bool:
    """A non-empty string. Missing values arrive as None or NaN depending on the column, and bool(NaN) is True."""
    return isinstance(value, str) and bool(value.strip())


def name_text(row) -> str:
    """The text a human reads as the name: name, addon and display_name."""
    return " ".join(row.get(c) for c in ("name", "name_addon", "display_name") if present(row.get(c)))


def name_class(row) -> str:
    """Information only: the kind of name (herrn, pruefstelle, team, kues_other, ingenieurbuero, sv_buero, person_title, company, other).
    `name` first (a salutation is decided there), then the combined text."""
    name = row.get("name") if present(row.get("name")) else ""
    for label, pattern in _NAME_CLASSES[:1]:
        if pattern.search(name):
            return label
    text = name_text(row)
    for label, pattern in _NAME_CLASSES[1:]:
        if pattern.search(text):
            return label
    return "other"


def has_business_email(email) -> bool:
    """True if any address of the field is not at a consumer provider (kues.de counts as business). No email -> False."""
    if not isinstance(email, str):
        return False
    domains = [a.split("@")[-1].strip().lower() for a in re.split(r"[;,\s]+", email) if "@" in a]
    return any(d not in PRIVATE_EMAIL_DOMAINS for d in domains)


def evidence_signals(row) -> int:
    """How many of: a station word in the name, an own website, a business email."""
    return int(bool(_STATION_HINT.search(name_text(row)))) + int(present(row.get("website"))) + int(has_business_email(row.get("email")))


def classify_row(row) -> tuple[str, bool, str | None, str | None, list[str], str | None]:
    """(site_type, in_scope, scope_reason, evidence, flags, psp) for one parsed row (the flags that need only this row)."""
    vorort = row["pruefung_vor_ort"]
    flags: list[str] = []
    psp = None
    if vorort is True:
        site_type, in_scope, reason, evidence, psp = "pruefstelle", True, None, "vorort:1", PSP_NO
        if row["location_precision"] != "address":
            flags.append(FLAG_VORORT1_NO_ADDRESS)
        if _PSP_NAME.search(name_text(row)):
            flags.append(FLAG_NAME_PSP_LIKE)
        if evidence_signals(row) == 0:
            flags.append(FLAG_WEAK_EVIDENCE)
        if not any(present(row.get(c)) for c in ("phone", "email", "website")):
            flags.append(FLAG_CONTACT_MISSING)
    elif vorort is False:
        site_type, in_scope, reason, evidence, psp = "partner_office", True, None, "vorort:0", PSP_YES
        if _PRUEFSTELLE.search(name_text(row)):
            flags.append(FLAG_NAME_PRUEFSTELLE_VORORT0)
            if row.get("source_id") in VERIFIED_NOT_PRUEFSTELLE:  # certain cause: the only vorort = 0 records left out
                in_scope, reason = False, REASON_VERIFIED_NOT_PRUEFSTELLE
            else:  # the name contradicts the flag: keep the record and let a human look
                site_type = "unknown"
        if evidence_signals(row) >= 2:
            flags.append(FLAG_STRONG_EVIDENCE)
    else:
        site_type, in_scope, reason, evidence = "unknown", False, REASON_VORORT_INVALID, "vorort:invalid"
        flags.append(FLAG_VORORT_INVALID)
    return site_type, in_scope, reason, evidence, flags, psp


def classify_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Set site_type, site_type_evidence, in_scope, scope_reason, psp, name_class, licensee_has_inscope_site, doubt_flags
    and extend needs_review(_reason) with the review flags. Rows are never dropped or merged."""
    out = df.copy()
    if out.empty:
        return out
    results = [classify_row(r) for _, r in out.iterrows()]
    out["site_type"] = [r[0] for r in results]
    out["in_scope"] = [r[1] for r in results]
    out["scope_reason"] = [r[2] for r in results]
    out["site_type_evidence"] = [r[3] for r in results]
    out["psp"] = [r[5] for r in results]
    out["name_class"] = [name_class(r) for _, r in out.iterrows()]
    flags = [list(r[4]) for r in results]

    # flags that need the whole frame: licence siblings and shared addresses (never used to merge rows). "In scope"
    # here means an own Prüfstelle (psp = No), as before the vorort = 0 records came into scope.
    in_scope = out["psp"] == PSP_NO
    per_licensee = out.loc[in_scope].groupby("kues_licensee", dropna=False).size()
    own = in_scope.astype(int)
    out["licensee_has_inscope_site"] = [
        per_licensee.get(lic, 0) - o > 0 for lic, o in zip(out["kues_licensee"], own)
    ]
    keys = [parse.address_key(s, z) for s, z in zip(out["street"], out["zip"])]
    valid = [bool(k[0] and k[1]) for k in keys]
    inscope_at: dict = {}
    for k, v, ins in zip(keys, valid, in_scope):
        if v and ins:
            inscope_at[k] = inscope_at.get(k, 0) + 1
    for i, (k, v, ins) in enumerate(zip(keys, valid, in_scope)):
        if not v:
            continue
        if ins and inscope_at.get(k, 0) >= 2:
            flags[i].append(FLAG_SHARED_ADDRESS_INSCOPE)
        if not ins and inscope_at.get(k, 0) >= 1:
            flags[i].append(FLAG_SAME_ADDRESS_AS_INSCOPE)

    out["doubt_flags"] = ["; ".join(f) or None for f in flags]
    reasons = out["needs_review_reason"].astype(object).where(out["needs_review_reason"].notna(), None)
    review = out["needs_review"].astype(bool)
    for idx, row_flags in zip(out.index, flags):
        for flag in row_flags:
            if flag in REVIEW_FLAGS:
                reasons[idx] = enrich.add_reason(reasons[idx], flag)
                review[idx] = True
    out["needs_review_reason"] = reasons
    out["needs_review"] = review
    return out
