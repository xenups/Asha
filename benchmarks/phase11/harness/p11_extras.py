"""Phase 11 extras against integrated main: server identity, FAILED path,
and evidence-integrity refusal. Plain MCP stdio, one process per call.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p11")
STATE = BENCH / "state3"
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


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    out: dict[str, object] = {}

    # --- server identity
    code, version, err = run([ASHA_PY, str(ASHA_MCP), "info"])
    out["server"] = {"python": ASHA_PY, "asha_home": ASHA_HOME,
                     "info_exit": code, "info": version[:400],
                     "stderr": err[:200]}
    code, head_sha, _ = run(["git", "rev-parse", "HEAD"], cwd=Path(ASHA_HOME))
    out["server"]["code_head"] = head_sha

    # --- healthy clean change -> APPROVED
    root = clone("h_ok")
    (root / "pydetactl" / "README.md").write_text("# healthy\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "healthy change")
    ok_head = git(root, "rev-parse", "HEAD")
    prepared = prepare(root, ok_head)
    final = asha("asha_review_finalize",
                 {"root": str(root), "review_run_id": prepared["run_id"]})
    gate = final.get("gate") or {}
    out["healthy_change"] = {"run_id": prepared["run_id"], "head": ok_head,
                             "state": final.get("state"),
                             "decision": final.get("decision"),
                             "gate_state": gate.get("gate_state"),
                             "authorized_to_ship": gate.get("authorized_to_ship"),
                             "snapshot_id": gate.get("snapshot_id")}

    # --- test-breaking change -> FAILED, unauthorized
    root = clone("h_bad")
    (root / "pydetactl" / "README.md").write_text("# breaking\n", encoding="utf-8")
    test_path = root / "pydetactl" / "tests" / "test_breaks_on_purpose.py"
    test_path.write_text(
        "def test_deliberate_failure():\n    assert 1 == 2\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "breaking change")
    bad_head = git(root, "rev-parse", "HEAD")
    prepared = prepare(root, bad_head)
    if "run_id" in prepared:
        final = asha("asha_review_finalize",
                     {"root": str(root), "review_run_id": prepared["run_id"]})
        gate = final.get("gate") or {}
        out["breaking_change"] = {"run_id": prepared["run_id"], "head": bad_head,
                                  "state": final.get("state"),
                                  "decision": final.get("decision"),
                                  "gate_state": gate.get("gate_state"),
                                  "authorized_to_ship": gate.get("authorized_to_ship"),
                                  "reason": final.get("blocking_reason")}
    else:
        out["breaking_change"] = prepared

    # --- tampered review artifact -> evidence refusal
    root = clone("h_tamper")
    (root / "pydetactl" / "README.md").write_text("# tamper\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "tamper target")
    t_head = git(root, "rev-parse", "HEAD")
    prepared = prepare(root, t_head)
    if "run_id" in prepared:
        run_id = prepared["run_id"]
        artifacts = list((STATE / "reviews" / run_id).glob("decision.json"))
        runs_root = list(STATE.rglob(f"reviews/{run_id}/decision.json"))
        target = runs_root[0] if runs_root else (artifacts[0] if artifacts else None)
        out["tampered_evidence"] = {"run_id": run_id, "artifact": str(target)}
        if target:
            payload = json.loads(target.read_text())
            payload["decision"] = "APPROVED"
            payload["approved"] = True
            target.write_text(json.dumps(payload))
        final = asha("asha_review_finalize",
                     {"root": str(root), "review_run_id": run_id})
        gate = final.get("gate") or {}
        out["tampered_evidence"].update({
            "state": final.get("state"), "decision": final.get("decision"),
            "gate_state": gate.get("gate_state"),
            "authorized_to_ship": gate.get("authorized_to_ship"),
            "reason": final.get("blocking_reason")})

    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
