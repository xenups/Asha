"""Phase 11 extras, attempt 2.

1. test-breaking change that is lint-clean -> expect the gate to fail on tests
   (not on ruff), unauthorized.
2. tampered stage artifact before finalize -> expect the review to refuse on
   evidence integrity.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p11")
STATE = BENCH / "state4"
ASHA_PY = "/home/amir/src/Asha/.venv/bin/python"
ASHA_MCP = BENCH / "p11_mcp.py"
ASHA_HOME = "/home/amir/asha-phase11"
BASE = "899ab1c3692067c4f13c28365cc19ce8183624f1"


def run(cmd, cwd=BENCH):
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["ASHA_BENCH_STATE"] = str(STATE)
    env["ASHA_HOME"] = ASHA_HOME
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=1800, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git(root: Path, *args: str) -> str:
    code, out, err = run(["git", *args], cwd=root)
    if code:
        raise SystemExit(f"git {args}: {err or out}")
    return out


def asha(tool: str, args: dict) -> dict:
    code, out, err = run([ASHA_PY, str(ASHA_MCP), "call", tool,
                          json.dumps(args)])
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out, "_stderr": err[-300:]}


def clone(name: str) -> Path:
    root = BENCH / name
    if root.exists():
        shutil.rmtree(root)
    code, out, err = run(["git", "clone", "-q", "--depth", "1",
                          "file:///home/amir/src/detailist", str(root)],
                         cwd=BENCH.parent)
    if code:
        raise SystemExit(err or out)
    git(root, "fetch", "-q", "origin", BASE, "--depth", "1")
    git(root, "checkout", "-q", BASE)
    git(root, "config", "user.email", "bench@example.invalid")
    git(root, "config", "user.name", "Bench")
    return root


def prepare(root: Path, head: str) -> dict:
    started = asha("asha_review", {"root": str(root), "base": BASE,
                                   "head": head})
    if "run" not in started:
        return {"error": started}
    run_id = started["run"]["run_id"]
    packet = started["packet"]
    for _ in range(3):
        asha("asha_review_submit_stage", {
            "root": str(root), "review_run_id": run_id,
            "stage": packet["stage"], "stage_id": packet["stage_id"],
            "verdict": "APPROVE", "summary": "phase 11 extras",
            "findings": [], "reviewer_metadata": {"host_client": "phase11"}})
        nxt = asha("asha_review_next_stage",
                   {"root": str(root), "review_run_id": run_id})
        packet = nxt if isinstance(nxt, dict) and "stage" in nxt else packet
    return {"run_id": run_id}


def gate_of(final: dict) -> dict:
    gate = final.get("gate") or {}
    return {"state": final.get("state"), "decision": final.get("decision"),
            "gate_state": gate.get("gate_state"),
            "authorized_to_ship": gate.get("authorized_to_ship"),
            "reason": final.get("blocking_reason"),
            "snapshot_id": gate.get("snapshot_id")}


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    out: dict[str, object] = {}

    # 1. lint-clean but failing test
    root = clone("x_bad")
    (root / "pydetactl" / "tests" / "test_deliberate.py").write_text(
        'def test_deliberate_failure():\n'
        '    """A lint-clean assertion that cannot hold."""\n'
        '    assert 1 + 1 == 3\n', encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "a lint-clean failing test")
    bad_head = git(root, "rev-parse", "HEAD")
    prepared = prepare(root, bad_head)
    if "run_id" in prepared:
        final = asha("asha_review_finalize",
                     {"root": str(root), "review_run_id": prepared["run_id"]})
        out["breaking_change"] = {"run_id": prepared["run_id"], "head": bad_head,
                                  **gate_of(final)}
    else:
        out["breaking_change"] = prepared

    # 2. tampered stage artifact before finalize
    root = clone("x_tamper")
    (root / "pydetactl" / "README.md").write_text("# tamper\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "tamper target")
    t_head = git(root, "rev-parse", "HEAD")
    prepared = prepare(root, t_head)
    if "run_id" in prepared:
        run_id = prepared["run_id"]
        candidates = sorted(STATE.glob(f"*/reviews/{run_id}/stages/*.json"))
        stage_file = next((p for p in candidates
                           if p.name == "CODE_REVIEW.json"), None)
        entry: dict[str, object] = {"run_id": run_id, "head": t_head,
                                     "stage_files": [p.name for p in candidates]}
        if stage_file:
            original = json.loads(stage_file.read_text())
            entry["original_verdict"] = original.get("verdict")
            entry["original_digest"] = original.get("digest")
            original["verdict"] = "APPROVE"
            original["summary"] = "tampered by phase 11 audit"
            stage_file.write_text(json.dumps(original, indent=1))
            entry["tampered_file"] = stage_file.name
        final = asha("asha_review_finalize",
                     {"root": str(root), "review_run_id": run_id})
        entry.update(gate_of(final))
        out["tampered_evidence"] = entry
    else:
        out["tampered_evidence"] = prepared

    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
