#!/usr/bin/env python3
"""F1 reproduction: what does a SHA-pinned review actually authorize?

Case A -- review head pinned to a COMMIT id; the branch then moves.
Case B -- review head pinned to a REF; the branch then moves.

For each case the script records, in git-native identities (commit ids compare
with commit ids, tree ids with tree ids, and `git status` for uncommitted or
untracked content):

  reviewed_commit, reviewed_tree, head_commit_at_finalize, head_tree_at_finalize,
  worktree_dirty, gate snapshot_id, authorized_to_ship, final state

Read-only: it drives the real MCP server against a disposable clone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p10")
CLONE = BENCH / "neg" / "f1repro"
STATE = BENCH / "neg" / "f1state"
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


def commit_tree(rev: str) -> dict:
    return {"commit": git("rev-parse", f"{rev}^{{commit}}"),
            "tree": git("rev-parse", f"{rev}^{{tree}}")}


def worktree_state() -> dict:
    status = git("status", "--porcelain")
    return {"dirty": bool(status.strip()),
            "porcelain": status.splitlines()[:5],
            "head": git("rev-parse", "HEAD")}


def asha(tool, args):
    code, out, err = run([ASHA_PY, str(ASHA_MCP), "call", tool, json.dumps(args)])
    try:
        return {"exit": code, "payload": json.loads(out)}
    except json.JSONDecodeError:
        return {"exit": code, "payload": {"_raw": out}, "stderr": err[-200:]}


def reset():
    if CLONE.exists():
        shutil.rmtree(CLONE)
    code, out, err = run(["git", "clone", "-q", "--depth", "1",
                          "file:///home/amir/src/detailist", str(CLONE)],
                         cwd=BENCH.parent)
    if code:
        raise SystemExit(f"clone failed: {err or out}")
    git("fetch", "-q", "origin", BASE, "--depth", "1")
    git("checkout", "-q", BASE)
    git("config", "user.email", "bench@example.invalid")
    git("config", "user.name", "Bench")


def change(text: str) -> str:
    (CLONE / "pydetactl" / "README.md").write_text(text, encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", f"content: {text.strip()}")
    return git("rev-parse", "HEAD")


def drive(head_arg: str) -> dict:
    started = asha("asha_review", {"root": str(CLONE), "base": BASE,
                                   "head": head_arg})
    body = started["payload"]
    run_id = body.get("run", {}).get("run_id", "")
    target = body.get("run", {}).get("target", {})
    packet = body.get("packet", {})
    stages = []
    for _ in range(3):
        submitted = asha("asha_review_submit_stage", {
            "root": str(CLONE), "review_run_id": run_id,
            "stage": packet.get("stage"), "stage_id": packet.get("stage_id"),
            "verdict": "APPROVE", "summary": "F1 reproduction",
            "findings": [], "reviewer_metadata": {"host_client": "bench-host"},
        })
        stages.append(submitted["payload"].get("status")
                      if isinstance(submitted["payload"], dict) else None)
        nxt = asha("asha_review_next_stage",
                   {"root": str(CLONE), "review_run_id": run_id})
        packet = nxt["payload"] if nxt["exit"] == 0 else packet
    return {"run_id": run_id, "target": target, "stages": stages}


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}

    # ---- Case A: head pinned to a commit id
    reset()
    s1 = change("# S1\n")
    reviewed = commit_tree(s1)
    case = drive(s1)
    s2 = change("# S2\n")
    moved = commit_tree("HEAD")
    final = asha("asha_review_finalize",
                 {"root": str(CLONE), "review_run_id": case["run_id"]})
    payload = final["payload"]
    gate = payload.get("gate") or {}
    out["A_commit_pinned"] = {
        "reviewed": reviewed,
        "head_at_finalize": moved["commit"],
        "head_tree_at_finalize": moved["tree"],
        "worktree": worktree_state(),
        "target_persisted": {"head_sha": case["target"].get("head_sha"),
                             "head_ref": case["target"].get("head_ref"),
                             "diff_sha256": case["target"].get("diff_sha256")},
        "stages": case["stages"],
        "state": payload.get("state"), "decision": payload.get("decision"),
        "gate_state": gate.get("gate_state"),
        "authorized_to_ship": gate.get("authorized_to_ship"),
        "gate_snapshot_id": gate.get("snapshot_id"),
        "reviewed_tree_equals_authorized_tree":
            reviewed["tree"] == moved["tree"],
        "verdict": "authorization applied to content that was not reviewed"
        if gate.get("authorized_to_ship") and reviewed["tree"] != moved["tree"]
        else "authorization stayed with the reviewed content",
    }

    # ---- Case B: head pinned to a ref
    reset()
    s1 = change("# S1\n")
    reviewed = commit_tree(s1)
    case = drive("HEAD")
    s2 = change("# S2\n")
    moved = commit_tree("HEAD")
    final = asha("asha_review_finalize",
                 {"root": str(CLONE), "review_run_id": case["run_id"]})
    payload = final["payload"]
    gate = payload.get("gate") or {}
    out["B_ref_pinned"] = {
        "reviewed": reviewed,
        "head_at_finalize": moved["commit"],
        "head_tree_at_finalize": moved["tree"],
        "worktree": worktree_state(),
        "target_persisted": {"head_sha": case["target"].get("head_sha"),
                             "head_ref": case["target"].get("head_ref")},
        "stages": case["stages"],
        "state": payload.get("state"), "decision": payload.get("decision"),
        "reasons": payload.get("reasons"),
        "gate_state": gate.get("gate_state"),
        "authorized_to_ship": gate.get("authorized_to_ship"),
        "s2_inherited_approval": bool(gate.get("authorized_to_ship")),
    }
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
