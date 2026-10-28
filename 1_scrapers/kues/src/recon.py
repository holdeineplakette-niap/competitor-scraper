"""One-off KÜS recon: probe the partner-search API with a handful of named requests.

Not an adapter. Run from the project root:

    .venv\\Scripts\\python.exe 1_scrapers/kues/src/recon.py list                         # the planned steps
    .venv\\Scripts\\python.exe 1_scrapers/kues/src/recon.py fetch <step> [<step> ...]    # LIVE, one request per step
    .venv\\Scripts\\python.exe 1_scrapers/kues/src/recon.py fetch <step> --run <folder>  # append to an existing recon run
    .venv\\Scripts\\python.exe 1_scrapers/kues/src/recon.py analyze <run_folder>         # offline -> summary.md

Output goes to 1_scrapers/kues/test_output/<YYYY-MM-DD_HHMM>_recon/ (git-ignored, contains contact data):
`requests/<step>_response.json` (+ `_meta.json` with the request and the response headers) and `query_log.csv`.
Every live request of the recon is one line in query_log.csv; a run never overwrites (files are opened with "x").
The recon budget is RECON_MAX_REQUESTS live requests per run folder, the page/script fetches of the discovery
(`p01`..`p04`, see query_log.csv) included.
"""

from __future__ import annotations

import csv
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from core import paths

SLUG = "kues"
API = "https://webservice.kues.de/api/kues"
ENDPOINT_KUES = "partnersuche"
ENDPOINT_TECHNIK = "partnersuche-kues-technik"
ENDPOINT_PLZDATEN = "plzdaten"
PAGE = "https://kues.de/standortsuche/"
PAUSE_S = 3.0
JITTER_S = 1.0
TIMEOUT_S = 30
RECON_MAX_REQUESTS = 22  # live requests per recon run folder, discovery included (session budget: 25 incl. the 2 of the sample run)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HEADER_SETS = {
    "bare": {},  # no headers of our own (python-requests defaults), no cookies
    "ua": {"User-Agent": UA},
    "browser": {"User-Agent": UA, "Accept": "application/json", "Origin": "https://kues.de", "Referer": PAGE},
}

# step id -> (description, endpoint, params, header set)
STEPS: dict[str, tuple[str, str, dict, str]] = {
    "a01": ("80636 partnersuche, bare headers (compare with the saved file)", ENDPOINT_KUES, {"plz": "80636"}, "bare"),
    "a02": ("80636 partnersuche-kues-technik", ENDPOINT_TECHNIK, {"plz": "80636"}, "bare"),
    "a03": ("17213 partnersuche (rural Mecklenburg)", ENDPOINT_KUES, {"plz": "17213"}, "bare"),
    "a04": ("66111 partnersuche (Saarbruecken, KÜS home region)", ENDPOINT_KUES, {"plz": "66111"}, "bare"),
    "a05": ("44135 partnersuche (Dortmund, Ruhr)", ENDPOINT_KUES, {"plz": "44135"}, "bare"),
    "a06": ("66111 partnersuche-kues-technik", ENDPOINT_TECHNIK, {"plz": "66111"}, "bare"),
    "a07": ("85221 partnersuche (Dachau: sites inside the PLZ -> plzorder; overlaps 80636)", ENDPOINT_KUES, {"plz": "85221"}, "bare"),
    "b01": ("coordinates only (Berlin centre), no plz", ENDPOINT_KUES, {"latitude": "52.52", "longitude": "13.405"}, "bare"),
    "b02": ("plz 80636 plus Berlin coordinates: which one wins?", ENDPOINT_KUES, {"plz": "80636", "latitude": "52.52", "longitude": "13.405"}, "bare"),
    "b03": ("80636 plus guessed radius/limit parameters (all at once)", ENDPOINT_KUES,
            {"plz": "80636", "radius": "200", "umkreis": "200", "entfernung": "200", "km": "200", "limit": "500", "max": "500", "top": "500"}, "bare"),
    "x01": ("80636 plus candidate radius names with DIFFERENT values: the max entfernung shows which one counts", ENDPOINT_KUES,
            {"plz": "80636", "radius": "60", "umkreis": "70", "entfernung": "80", "km": "90",
             "distance": "55", "maxdistance": "65", "range": "75"}, "bare"),
    "e01": ("44135 Dortmund with km=100: more than 100 records means no count cap", ENDPOINT_KUES, {"plz": "44135", "km": "100"}, "bare"),
    "e02": ("10115 partnersuche (Berlin centre, dense)", ENDPOINT_KUES, {"plz": "10115"}, "bare"),
    "f01": ("00001 (not a PLZ): how does the server answer an unknown PLZ?", ENDPOINT_KUES, {"plz": "00001"}, "bare"),
    "g01": ("80636 technik list with km=100: is km honoured there too?", ENDPOINT_TECHNIK, {"plz": "80636", "km": "100"}, "bare"),
    "g02": ("99986 Niederdorla (Germany's centre) with km=1000: is it capped at 100?", ENDPOINT_KUES, {"plz": "99986", "km": "1000"}, "bare"),
    "c01": ("plzdaten autocomplete, term=80636", ENDPOINT_PLZDATEN, {"term": "80636"}, "bare"),
    "d01": ("54290 partnersuche (Trier, Luxembourg border)", ENDPOINT_KUES, {"plz": "54290"}, "bare"),
    "h01": ("80636 partnersuche, browser headers (Origin/Referer)", ENDPOINT_KUES, {"plz": "80636"}, "browser"),
}

QUERY_LOG_COLUMNS = ["seq", "step", "method", "url", "params", "headers", "http_status", "bytes", "records", "elapsed_s", "time", "note"]


def _log_rows(run: Path) -> list[dict]:
    path = run / paths.QUERY_LOG_FILE
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _append_log(run: Path, row: dict) -> None:
    path = run / paths.QUERY_LOG_FILE
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=QUERY_LOG_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def _write_new(path: Path, data: bytes | str) -> None:
    mode = "xb" if isinstance(data, bytes) else "x"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as fh:
        fh.write(data)


def fetch_step(run: Path, step: str, say=print) -> dict:
    """One live request of a named step, saved and logged. Stops (raises) on budget, 401/403/429 and 5xx."""
    desc, endpoint, params, header_set = STEPS[step]
    done = _log_rows(run)
    if len(done) >= RECON_MAX_REQUESTS:
        raise RuntimeError(f"recon budget of {RECON_MAX_REQUESTS} requests reached in {run.name}")
    if any(r["step"] == step for r in done):
        raise RuntimeError(f"step {step} was already requested in {run.name}")
    if done:
        time.sleep(PAUSE_S + random.uniform(0, JITTER_S))
    url = f"{API}/{endpoint}"
    say(f"{step}: {desc}\n    GET {url} {params} headers={header_set}")
    t0 = time.time()
    resp = requests.get(url, params=params, headers=HEADER_SETS[header_set], timeout=TIMEOUT_S)
    elapsed = round(time.time() - t0, 2)
    records = None
    try:
        body = resp.json()
        records = len(body) if isinstance(body, list) else None
    except ValueError:
        body = None
    (run / "requests").mkdir(exist_ok=True)
    _write_new(run / "requests" / f"{step}_response.json", resp.content)
    _write_new(
        run / "requests" / f"{step}_meta.json",
        json.dumps(
            {
                "step": step,
                "description": desc,
                "url": resp.request.url,
                "request_headers": dict(resp.request.headers),
                "status": resp.status_code,
                "response_headers": dict(resp.headers),
                "cookies_set": dict(resp.cookies),
            },
            indent=2,
            ensure_ascii=False,
        ),
    )
    _append_log(
        run,
        {
            "seq": len(done) + 1,
            "step": step,
            "method": "GET",
            "url": url,
            "params": json.dumps(params),
            "headers": header_set,
            "http_status": resp.status_code,
            "bytes": len(resp.content),
            "records": records,
            "elapsed_s": elapsed,
            "time": datetime.now().isoformat(timespec="seconds"),
            "note": "",
        },
    )
    say(f"    HTTP {resp.status_code}, {len(resp.content)} bytes, {records} records, {elapsed}s, cookies set: {dict(resp.cookies) or 'none'}")
    if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
        raise RuntimeError(f"stop: HTTP {resp.status_code} on step {step}: {resp.text[:200]!r}")
    return {"status": resp.status_code, "records": records, "body": body}


def latest_run() -> Path | None:
    folder = paths.output_dir(SLUG, "recon")
    runs = sorted(p for p in folder.glob("*_recon*") if p.is_dir()) if folder.is_dir() else []
    return runs[-1] if runs else None


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in ("list", "fetch", "analyze"):
        print(__doc__)
        return
    if argv[0] == "list":
        for step, (desc, endpoint, params, header_set) in STEPS.items():
            print(f"{step}  {endpoint:<26} {params}  [{header_set}]  {desc}")
        return
    if argv[0] == "fetch":
        args = argv[1:]
        run = None
        if "--run" in args:
            i = args.index("--run")
            run = Path(args[i + 1])
            args = args[:i] + args[i + 2:]
        run = run or paths.new_run_dir(SLUG, "recon", datetime.now())
        print(f"run folder: {run}")
        for step in args:
            fetch_step(run, step)
        return
    from kues import recon_analysis  # offline analysis, kept apart from the live code

    recon_analysis.analyze(Path(argv[1]))


if __name__ == "__main__":
    main(sys.argv[1:])
