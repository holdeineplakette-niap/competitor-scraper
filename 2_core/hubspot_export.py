"""HubSpot import files from the master (companies, one row per master_id).

    first import   hubspot_import_YYYY-MM.csv    every ACTIVE row (the first master has no earlier month)
    every month    hubspot_changes_YYYY-MM.csv   only new / changed / retired / reactivated rows against the previous master
                   hubspot_changes_overview_YYYY-MM.csv   one line per change (for people; not for the import)
    both           hubspot_excluded_YYYY-MM.csv  rows kept out of the import and why

Rules
- CSV with commas, UTF-8 (no BOM), everything as text (PLZ and ids keep their zeros). Never open and save it in Excel.
- Headers are the internal names of the HubSpot company properties (`PROPERTIES` below, created in HubSpot on 2026-10-21), so
  the import wizard maps them by name. `prufstellenid_datascraper` (= master_id) is the unique match key of every import.
- `company_datasource` is `competitor_scraper` on every row. Country is `Germany`. ÜO, PSP and the status are written as the
  internal values of the HubSpot options (`UO_OPTIONS`, `YES_NO`, `STATUS_VALUES`); lat / lng with a decimal comma.
- `website` goes to the standard Website URL property, never to the company domain. 403 rows share a domain with another
  row: the first test import must show that HubSpot does not merge them.
- Only scraped fields are exported. Owner, lifecycle stage, notes and every other field edited by Sales or M&A never are.
- Changed rows carry the id plus ONLY the changed fields; retired rows carry the id, the status and the retired-on date. The
  other cells stay empty, so the import must not overwrite existing values with blanks (HubSpot's default).
- Nothing is ever deleted: a retired site only gets `business_status = scraper-geschlossen` and the date in `compscraper_closedondate`.
  Known limit: a REACTIVATED site gets `business_status = aktiv`, but an empty cell never clears the old date in HubSpot (blanks do
  not overwrite), so its `compscraper_closedondate` has to be cleared by hand; the overview file lists every reactivation.
- Rows whose own-site status is unresolved (`hdp_match_unresolved`) are kept out until a human decides, and listed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from core import master_lifecycle

DATASOURCE = "competitor_scraper"
EXCLUDE_FLAG_PREFIX = "hdp_match_unresolved"  # may be HDP's own site: not imported until decided
KEPT_FLAG_PREFIX = "hdp_own_site_kept"  # HDP's own site kept in the master by decision (hdp_removal.KEPT_OWN_ROWS): never imported
EXCLUDE_REASONS = {
    EXCLUDE_FLAG_PREFIX: "may be HDP's own site (hdp_match_unresolved): not imported until a human decides",
    KEPT_FLAG_PREFIX: "HDP's own site kept in the master by decision (hdp_own_site_kept): never imported",
}

# master column -> HubSpot company property (internal name), as created in HubSpot on 2026-10-21. Only properties that exist
# are exported (not yet created: opening hours, services, booking / profile URL, site type, retired-on date).
PROPERTIES: dict[str, str] = {
    "master_id": "prufstellenid_datascraper",  # unique property: the match key of every import
    "name": "name",
    "street": "address",
    "zip": "zip",
    "city": "city",
    "state": "state",
    "country": "country",
    "phone": "phone",
    "email": "unternehmens_e_mail",
    "website": "website",  # the standard Website URL (single-line text); NOT the company domain
    "uo": "stutzpunkt_pruforganisation",  # checkbox property: the value must be the option's internal name
    "psp": "stutzpunkt",  # dropdown Ja / Nein
    "lat": "geo_lat",  # numbers in the German decimal format (comma)
    "lng": "geo_lng",
    "address_incomplete": "compscraper_adresse_unvollstandig",  # dropdown Ja / Nein
    "status": "business_status",  # single-line text: aktiv / scraper-geschlossen
    "retired_on": "compscraper_closedondate",  # date the site was retired (YYYY-MM-DD); empty for active sites
}
DATASOURCE_PROPERTY = "company_datasource"  # dropdown, one option with the internal value below
ID = PROPERTIES["master_id"]
STATUS = PROPERTIES["status"]
RETIRED_ON = PROPERTIES["retired_on"]
HEADERS = [ID, DATASOURCE_PROPERTY] + [h for c, h in PROPERTIES.items() if c != "master_id"]
SCRAPED = [h for h in HEADERS if h not in (ID, DATASOURCE_PROPERTY, STATUS, RETIRED_ON)]  # fields that can "change"

# master value -> internal value of the HubSpot option / text. An ÜO without an entry stops the export (a new ÜO needs a decision).
UO_OPTIONS = {"GTÜ": "GTÜ", "KÜS": "KÜS", "DEKRA": "DEKRA", "TÜV SÜD": "TÜV", "TÜV NORD": "TÜV Nord", "TÜV Rheinland": "TÜV Rheinland",
              "TÜV Hessen": "TÜV Hessen", "TÜV Thüringen": "TÜV Thüringen", "TÜV Saarland": "TÜV Saarland", "VÜK": "VÜK"}
YES_NO = {"Yes": "Ja", "No": "Nein"}
STATUS_VALUES = {"active": "aktiv", "retired": "scraper-geschlossen"}
DECIMAL_COMMA = True  # the portal reads geo_lat / geo_lng in the German number format
OVERVIEW_COLUMNS = ["change", ID, "name", "uo", "city", "fields"]
EXCLUDED_COLUMNS = ["master_id", "name", "zip", "city", "reason"]


def _flags(row) -> list[str]:
    """Doubt tokens of a row; a master written before 2026-10-08 kept the old reasons in needs_review_reason."""
    return [x for col in ("doubt_flags", "needs_review_reason") for x in str(row.get(col, "")).split("; ") if x]


def exclude_reason(row) -> str:
    """Why a row is kept out of HubSpot ("" = it is imported)."""
    return next((EXCLUDE_REASONS[p] for p in EXCLUDE_REASONS for x in _flags(row) if x.startswith(p)), "")


def is_excluded(row) -> bool:
    return bool(exclude_reason(row))


def to_hubspot(master: pd.DataFrame) -> pd.DataFrame:
    """The master rows as HubSpot rows (all mapped columns, constants added)."""
    m = master_lifecycle.with_lifecycle_columns(master).fillna("")
    unknown = {k: sorted(set(m[k]) - set(allowed)) for k, allowed in (("uo", UO_OPTIONS), ("psp", YES_NO), ("status", STATUS_VALUES))
               if set(m[k]) - set(allowed)}
    if unknown:
        raise ValueError(f"master values without a HubSpot value: {unknown}")
    out = pd.DataFrame(index=m.index)
    for col, head in PROPERTIES.items():
        if col == "country":
            out[head] = "Germany"
        elif col == "address_incomplete":
            out[head] = ["Ja" if "address_incomplete" in _flags(r) else "Nein" for r in m.to_dict("records")]
        elif col == "uo":
            out[head] = m[col].map(UO_OPTIONS)
        elif col == "psp":
            out[head] = m[col].map(YES_NO)
        elif col == "status":
            out[head] = m[col].map(STATUS_VALUES)
        elif col in ("lat", "lng") and DECIMAL_COMMA:
            out[head] = m[col].astype(str).str.replace(".", ",", regex=False)
        else:
            out[head] = m[col].astype(str)
    out[DATASOURCE_PROPERTY] = DATASOURCE
    return out[HEADERS]


def exportable(master: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(rows that are imported, rows kept out with the reason): active rows only, minus unresolved own-site candidates."""
    m = master_lifecycle.with_lifecycle_columns(master).fillna("")
    active = m[m["status"] == "active"]
    ex = pd.Series([is_excluded(r) for r in active.to_dict("records")], index=active.index, dtype=bool)
    kept = active[ex].copy()
    kept["reason"] = [exclude_reason(r) for r in kept.to_dict("records")]
    return active[~ex], kept[["master_id", "name", "zip", "city", "reason"]].rename(columns=str)


def first_import(master: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, excluded = exportable(master)
    return to_hubspot(rows).reset_index(drop=True), excluded.reset_index(drop=True)


def monthly_changes(prev: pd.DataFrame, cur: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(import rows, overview, excluded) of the changes from the previous master to this one.

    Compared are the HubSpot rows, not the master rows: what HubSpot holds is what the previous month exported."""
    prev_rows, _ = exportable(prev)
    cur_rows, excluded = exportable(cur)
    prev_h = to_hubspot(prev_rows).set_index(ID)
    cur_h = to_hubspot(cur_rows).set_index(ID)
    cur_all = master_lifecycle.with_lifecycle_columns(cur).fillna("")
    prev_all = master_lifecycle.with_lifecycle_columns(prev).fillna("")
    cur_status = dict(zip(cur_all["master_id"], cur_all["status"]))
    prev_status = dict(zip(prev_all["master_id"], prev_all["status"]))
    cur_meta = {r["master_id"]: r for r in cur_all.to_dict("records")}
    rows: list[dict] = []
    overview: list[dict] = []

    def add(change: str, mid: str, data: dict, fields: str) -> None:
        rows.append({h: "" for h in HEADERS} | {ID: mid, DATASOURCE_PROPERTY: DATASOURCE} | data)
        m = cur_meta[mid]
        overview.append({"change": change, ID: mid, "name": m["name"], "uo": m["uo"], "city": m["city"], "fields": fields})

    for mid in cur_h.index:
        row = cur_h.loc[mid].to_dict()
        if prev_status.get(mid) == "retired":
            add("reactivated", mid, row, "all")
        elif mid not in prev_h.index:
            add("new", mid, row, "all")
        else:
            diff = [h for h in SCRAPED if prev_h.at[mid, h] != cur_h.at[mid, h]]
            if diff:
                add("changed", mid, {h: row[h] for h in diff}, ", ".join(diff))
    for mid in prev_h.index:  # in HubSpot as active, retired now: only the status is written
        if cur_status.get(mid) == "retired":
            add("retired", mid, {STATUS: STATUS_VALUES["retired"], RETIRED_ON: cur_meta[mid]["retired_on"]}, "status, retired date")
    order = {"new": 0, "reactivated": 1, "changed": 2, "retired": 3}
    imp = pd.DataFrame(rows, columns=HEADERS)
    ov = pd.DataFrame(overview, columns=OVERVIEW_COLUMNS)
    if len(ov):
        idx = sorted(range(len(ov)), key=lambda i: (order[ov.iloc[i]["change"]], ov.iloc[i][ID]))
        imp, ov = imp.iloc[idx].reset_index(drop=True), ov.iloc[idx].reset_index(drop=True)
    return imp, ov, excluded.reset_index(drop=True)


def files_for_run(prev: pd.DataFrame | None, master: pd.DataFrame, month: str, tag: str = "") -> dict[str, pd.DataFrame]:
    """File name -> frame, written next to the master of a run."""
    if prev is None:
        imp, excluded = first_import(master)
        files = {f"hubspot_import{tag}_{month}.csv": imp}
    else:
        imp, ov, excluded = monthly_changes(prev, master)
        files = {f"hubspot_changes{tag}_{month}.csv": imp, f"hubspot_changes_overview{tag}_{month}.csv": ov}
    if len(excluded):
        files[f"hubspot_excluded{tag}_{month}.csv"] = excluded
    return files


def test_sample(imp: pd.DataFrame, n: int = 50) -> pd.DataFrame:
    """~n rows of an import file for the first test import, chosen to meet every risky case (deterministic):
    rows that share a website domain (does HubSpot merge them?), street-less rows, every ÜO, Stützpunkt = Ja, rows without
    phone / email, decimal commas. The rest is filled with evenly spaced rows."""
    if imp.empty:
        return imp
    domain = imp["website"].str.lower().str.replace(r"^https?://(www\.)?", "", regex=True).str.split("/").str[0]
    picked: list[int] = []

    def take(idx, k) -> None:
        picked.extend(i for i in list(idx)[:k] if i not in picked)

    shared = domain[(domain != "") & domain.duplicated(keep=False)]
    if len(shared):
        take(shared[shared == shared.value_counts().index[0]].index, 6)  # the most shared domain
        take(shared[shared != shared.value_counts().index[0]].sort_values().index, 4)
    take(imp.index[imp["compscraper_adresse_unvollstandig"] == "Ja"], 4)
    for uo in sorted(imp["stutzpunkt_pruforganisation"].unique()):
        take(imp.index[imp["stutzpunkt_pruforganisation"] == uo], 2)
    take(imp.index[imp["stutzpunkt"] == "Ja"], 3)
    take(imp.index[(imp["phone"] == "") | (imp["unternehmens_e_mail"] == "")], 3)
    step = max(1, len(imp) // max(1, n - len(picked)))
    take(imp.index[::step], n - len(picked))
    return imp.loc[sorted(picked[:n])].reset_index(drop=True)


def write_hubspot_csv(df: pd.DataFrame, path: Path) -> None:
    out = df.fillna("").astype(str)
    for col in out.columns:
        out[col] = out[col].str.replace(r"[\r\n]+", " ", regex=True)
    out.to_csv(path, sep=",", index=False, encoding="utf-8", lineterminator="\n")


# ---- command line: the same files for masters that already exist (nothing is overwritten) ---------------------------------
def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.hubspot_export", description="Write HubSpot import files for an existing master.")
    ap.add_argument("master_csv", help="the master CSV to export")
    ap.add_argument("--previous", help="the previous month's master CSV (monthly changes); without it: the first-import file")
    ap.add_argument("--out", required=True, help="folder for the files (it must not hold files of the same name)")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="first-import mode only: also write hubspot_test_sample_YYYY-MM.csv with ~N rows covering the risky cases")
    a = ap.parse_args(argv)
    master = _read(Path(a.master_csv))
    prev = _read(Path(a.previous)) if a.previous else None
    month = Path(a.master_csv).stem[-7:]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    files = files_for_run(prev, master, month)
    if a.sample and prev is None:
        files[f"hubspot_test_sample_{month}.csv"] = test_sample(files[f"hubspot_import_{month}.csv"], a.sample)
    clash = [out / n for n in files if (out / n).exists()]
    if clash:  # nothing is written if any file exists: never a half-written set
        print("not overwritten, already exist: " + ", ".join(str(c) for c in clash))
        return 2
    for name, frame in files.items():
        write_hubspot_csv(frame, out / name)
        print(f"{out / name}: {len(frame)} rows")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
