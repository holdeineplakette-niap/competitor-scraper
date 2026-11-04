"""Build the n8n status callback of the monthly Actions job (see 0_instructions/n8n_actions_contract.md).

Standard library only (runs with the runner's python3, also when setup failed). Everything comes from the environment:

    COMMAND        the dispatched command (tuev-sued-plan, fsp-check, ...)
    RUN_MONTH      YYYY-MM or empty (already checked by the workflow)
    RUN_URL        link to this Actions run
    DATA_ROOT      the synced data copy ($PRUEFSTELLEN_DATA_ROOT)
    JOB_STARTED    epoch seconds when the job started (a status file older than that was not written by this run)
    STOP_REASON    set when core.monthly did not run (bad input, sync not set up, pull or setup failed)
    MONTHLY_CODE   exit code of core.monthly; empty when it did not finish (timeout, crash, cancelled)
    PUSH_OUTCOME   outcome of the push step (success, failure, skipped, ...)
    PAYLOAD_FILE   where to write the JSON body
    GITHUB_OUTPUT  receives exit_code=<the job's final exit code>

Prints one line with counts only (phase, exit code, snippet yes/no); never the status contents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SNIPPET_PHASES = ("tuev_sued_waiting_for_upload", "tuev_sued_next_pass")
STATUS_JSON = "monthly_status.json"


def stopped(reason: str) -> dict:
    return {"exit_code": 2, "phase": "stopped", "needs_human": [reason]}


def already_built(status: dict) -> dict:
    """The timeout build found the month's master already built (TÜV SÜD came in time) and wrote nothing: one OK line instead
    of the stored status, so its old needs-human list is not posted again and 02 asks for no HubSpot approval (only phase built)."""
    master = Path((status.get("build") or {}).get("master_dir") or "?").name
    return {"month": status.get("month"), "simulated": status.get("simulated", False), "exit_code": 0, "phase": "already_built",
            "needs_human": [], "next_action": f"nothing to do: the master of {status.get('month')} was already built ({master})"}


def pick_status_file(root: Path, run_month: str, started: float) -> tuple[Path | None, bool]:
    """(file, written by this run). Prefer run_month's file, then any file this run wrote, then the newest by month."""
    monthly = root / "3_master_output" / "_monthly"
    files = sorted(monthly.glob(f"*/{STATUS_JSON}")) if monthly.is_dir() else []
    fresh = [f for f in files if f.stat().st_mtime >= started - 1]
    want = monthly / run_month / STATUS_JSON if run_month else None
    if want is not None and want in fresh:
        return want, True
    if fresh:
        return max(fresh, key=lambda f: f.stat().st_mtime), True
    if want is not None:
        return (want, False) if want.exists() else (None, False)
    return (files[-1], False) if files else (None, False)


def _read_rel(root: Path, rel: str | None) -> str | None:
    if not rel:
        return None
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def read_snippet(root: Path, status: dict) -> str | None:
    if status.get("phase") not in SNIPPET_PHASES:
        return None
    return _read_rel(root, (status.get("tuev_sued") or {}).get("snippet"))


def read_fsp_snippet(root: Path, command: str, status: dict) -> str | None:
    """The FSP snippet goes out once per cycle, with the TÜV SÜD pass 1 (tuev-sued-plan), while FSP waits for its upload."""
    f = status.get("fsp") or {}
    if command != "tuev-sued-plan" or f.get("status") != "WAITING_FOR_UPLOAD":
        return None
    return _read_rel(root, f.get("snippet"))


def build(env: dict) -> tuple[dict, int]:
    root = Path(env.get("DATA_ROOT") or ".")
    command = env.get("COMMAND", "")
    code_s = env.get("MONTHLY_CODE", "").strip()
    if env.get("STOP_REASON"):
        status = stopped(env["STOP_REASON"])
    elif not code_s.isdigit():
        status = stopped(f"core.monthly {command} did not finish (timeout after 330 min, crash or cancelled)")
    else:
        code = int(code_s)
        f, fresh = pick_status_file(root, env.get("RUN_MONTH", ""), float(env.get("JOB_STARTED") or 0))
        if f is None:
            status = stopped(f"no monthly_status.json after core.monthly {command} (exit {code})")
        elif not fresh and code != 0:
            status = stopped(f"core.monthly {command} exited {code} without updating monthly_status.json")
        else:
            try:
                status = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                status = stopped(f"monthly_status.json unreadable ({type(exc).__name__})")
            else:
                if code > int(status.get("exit_code") or 0):  # e.g. a traceback after the last status write
                    status.setdefault("needs_human", []).append(f"core.monthly exited {code}, the status file says {status.get('exit_code')}")
                    status["exit_code"] = code
                elif command == "build-without-tuev-sued" and code == 0 and not fresh and status.get("phase") == "built":
                    status = already_built(status)
    final = int(code_s) if code_s.isdigit() and not env.get("STOP_REASON") else 2  # the job ends with core.monthly's code
    if env.get("PUSH_OUTCOME") == "failure":
        status.setdefault("needs_human", []).append("data push to Google Drive failed: this run's files are not on the Drive")
        status["exit_code"] = final = 2
    snippet = read_snippet(root, status) if not env.get("STOP_REASON") else None
    fsp_snippet = read_fsp_snippet(root, command, status) if not env.get("STOP_REASON") else None
    payload = {"command": command, "run_url": env.get("RUN_URL", ""), "status": status, "snippet_js": snippet, "fsp_snippet_js": fsp_snippet}
    return payload, final


def main() -> int:
    payload, final = build(dict(os.environ))
    Path(os.environ["PAYLOAD_FILE"]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"exit_code={final}\n")
    s = payload["status"]
    print(f"callback: phase {s.get('phase')}, exit {final}, needs_human {len(s.get('needs_human') or [])}, "
          f"snippet {'yes' if payload['snippet_js'] else 'no'}, fsp snippet {'yes' if payload['fsp_snippet_js'] else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
