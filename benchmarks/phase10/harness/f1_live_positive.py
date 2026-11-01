"""Live positive control for the F1 fix: valid work must still be authorized.

Clean commit, review via a real MCP process, finalize -- expect APPROVED with
authorized_to_ship true. Guards against "fixing" stale detection by blocking
everything.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p10")
CLONE = BENCH / "neg" / "f1live"
STATE = BENCH / "neg" / "f1live-state"
ASHA_PY = "/home/amir/src/Asha/.venv/bin/python"
ASHA_MCP = BENCH / "asha_mcp_home.py"
BASE = "899ab1c3692067c4f13c28365cc19ce8183624f1"


def run(cmd, cwd=BENCH):
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["ASHA_BENCH_STATE"] = str(STATE)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=600, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git(*args):
    code, out, err = run(["git", *args], cwd=CLONE)
    if code:
        raise SystemExit(f"git {args}: {err or out}")
    return out


def asha(tool, args):
    code, out, err = run([ASHA_PY, str(ASHA_MCP), "call", tool, json.dumps(args)])
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise SystemExit(f"{tool} produced no JSON: {out[:200]} {err[:300]}")


def main() -> int:
    if CLONE.exists():
        shutil.rmtree(CLONE)
    STATE.mkdir(parents=True, exist_ok=True)
    code, out, err = run(["git", "clone", "-q", "--depth", "1",
                          "file:///home/amir/src/detailist", str(CLONE)],
                         cwd=BENCH.parent)
    if code:
        raise SystemExit(err or out)
    git("fetch", "-q", "origin", BASE, "--depth", "1")
    git("checkout", "-q", BASE)
    git("config", "user.email", "bench@example.invalid")
    git("config", "user.name", "Bench")
    (CLONE / "pydetactl" / "README.md").write_text("# valid change\n",
                                                   encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "a clean in-scope change")
    head = git("rev-parse", "HEAD")

    started = asha("asha_review", {"root": str(CLONE), "base": BASE, "head": head})
    run_id = started["run"]["run_id"]
    packet = started["packet"]
    for _ in range(3):
        asha("asha_review_submit_stage", {
            "root": str(CLONE), "review_run_id": run_id,
            "stage": packet["stage"], "stage_id": packet["stage_id"],
            "verdict": "APPROVE", "summary": "clean in-scope change",
            "findings": [], "reviewer_metadata": {"host_client": "bench-host"},
        })
        nxt = asha("asha_review_next_stage",
                   {"root": str(CLONE), "review_run_id": run_id})
        packet = nxt if isinstance(nxt, dict) and "stage" in nxt else packet
    final = asha("asha_review_finalize",
                 {"root": str(CLONE), "review_run_id": run_id})
    print(json.dumps({
        "head": head, "run_id": run_id,
        "state": final.get("state"), "decision": final.get("decision"),
        "approved": final.get("approved"),
        "gate_state": (final.get("gate") or {}).get("gate_state"),
        "authorized_to_ship": (final.get("gate") or {}).get("authorized_to_ship"),
        "reason": final.get("blocking_reason"),
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
