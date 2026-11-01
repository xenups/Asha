"""F3 reproduction: who creates the untracked file that blocks the Ship Gate?

Steps, all on a disposable clone of the benchmark target:
 1. clean clone at the benchmark base commit, one committed change
 2. run the target's own test suite with the repository root as cwd
 3. record every path the run created that git now reports as untracked
 4. review + finalize through a real MCP process and record the refusal
 5. repeat the pytest run in a second clone that never touches Asha

Nothing is deleted anywhere: a dirty tree is the finding.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p10")
CLONE = BENCH / "neg" / "f3_asha"
PLAIN = BENCH / "neg" / "f3_plain"
STATE = BENCH / "neg" / "f3state"
ASHA_PY = "/home/amir/src/Asha/.venv/bin/python"
PYD_PY = "/home/amir/src/detailist/pydetactl/.venv/bin/python"
ASHA_MCP = BENCH / "asha_mcp.py"
BASE = "899ab1c3692067c4f13c28365cc19ce8183624f1"


def run(cmd, cwd=BENCH, extra=None):
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["ASHA_BENCH_STATE"] = str(STATE)
    env.update(extra or {})
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=900, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git(*args, cwd=CLONE / "pydetactl"):
    code, out, err = run(["git", *args], cwd=cwd)
    if code:
        raise SystemExit(f"git {args}: {err or out}")
    return out


def asha(tool, args):
    code, out, err = run([ASHA_PY, str(ASHA_MCP), "call", tool, json.dumps(args)])
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out, "_stderr": err[-200:]}


def clone_at(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    code, out, err = run(["git", "clone", "-q", "--depth", "1",
                          "file:///home/amir/src/detailist", str(dest)],
                         cwd=BENCH.parent)
    if code:
        raise SystemExit(err or out)
    git("fetch", "-q", "origin", BASE, "--depth", "1", cwd=dest)
    git("checkout", "-q", BASE, cwd=dest)
    git("config", "user.email", "bench@example.invalid", cwd=dest)
    git("config", "user.name", "Bench", cwd=dest)


def main() -> int:
    out: dict[str, object] = {}
    STATE.mkdir(parents=True, exist_ok=True)

    # 5 first: the plain clone, to prove the file is not Asha's
    clone_at(PLAIN)
    code, tail, _ = run([PYD_PY, "-m", "pytest", "tests", "-q"],
                        cwd=PLAIN / "pydetactl")
    out["plain_pytest"] = {"exit": code,
                           "tail": tail.splitlines()[-1] if tail else "",
                           "untracked": git("status", "--porcelain",
                                            cwd=PLAIN).splitlines()}

    # 1-4: the governed clone
    clone_at(CLONE)
    (CLONE / "pydetactl" / "README.md").write_text("# change\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "a committed, reviewable change")
    head = git("rev-parse", "HEAD")

    before = git("status", "--porcelain").splitlines()
    code, tail, _ = run([PYD_PY, "-m", "pytest", "tests", "-q"], cwd=CLONE / "pydetactl")
    after = git("status", "--porcelain").splitlines()
    created = sorted(set(after) - set(before))
    out["governed_pytest"] = {"exit": code,
                              "tail": tail.splitlines()[-1] if tail else "",
                              "created_paths": created}

    started = asha("asha_review", {"root": str(CLONE), "base": BASE,
                                   "head": head})
    run_id = started["run"]["run_id"]
    packet = started["packet"]
    for _ in range(3):
        asha("asha_review_submit_stage", {
            "root": str(CLONE), "review_run_id": run_id,
            "stage": packet["stage"], "stage_id": packet["stage_id"],
            "verdict": "APPROVE", "summary": "clean change, dirty worktree",
            "findings": [], "reviewer_metadata": {"host_client": "bench-host"},
        })
        nxt = asha("asha_review_next_stage",
                   {"root": str(CLONE), "review_run_id": run_id})
        packet = nxt if isinstance(nxt, dict) and "stage" in nxt else packet
    final = asha("asha_review_finalize",
                 {"root": str(CLONE), "review_run_id": run_id})
    out["finalize"] = {
        "run_id": run_id,
        "state": final.get("state"),
        "decision": final.get("decision"),
        "gate_state": (final.get("gate") or {}).get("gate_state"),
        "authorized_to_ship": (final.get("gate") or {}).get("authorized_to_ship"),
        "reason": final.get("blocking_reason"),
    }
    out["worktree_after"] = git("status", "--porcelain").splitlines()
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
