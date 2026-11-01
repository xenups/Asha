#!/usr/bin/env python3
"""Phase 11 (corrected): F1 target-identity scenarios against integrated main.

The first attempt finalized the review *before* mutating the checkout, which
tested nothing: it reproduced the review-then-move order instead of the
move-then-finalize order that the defect lives in. This version separates them:

    prepare()  -> asha_review + three stages, NO finalize
    mutate     -> move the ref / change the tree / dirty the worktree
    finalize() -> asha_review_finalize

Scenarios
  A  SHA-pinned review, branch moved to an identical tree   -> authorized
  B  SHA-pinned review, candidate tree changed              -> STALE, no gate
  C  ref-pinned review, ref moved                           -> STALE
  D1 annotated tag as head, tree changed afterwards         -> STALE
  D2 HEAD~1 as head while HEAD is checked out                -> refused
  D3 detached HEAD exactly at the reviewed commit            -> authorized
  E  tracked file modified after the review                  -> fail closed
  F  untracked user file created after the review             -> fail closed
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p11")
STATE = BENCH / "state2"
ASHA_PY = "/home/amir/src/Asha/.venv/bin/python"
ASHA_MCP = BENCH / "asha_mcp_home.py"
ASHA_HOME = "/home/amir/asha-phase11"
BASE = "899ab1c3692067c4f13c28365cc19ce8183624f1"


def run(cmd, cwd=BENCH):
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["ASHA_BENCH_STATE"] = str(STATE)
    env["ASHA_HOME"] = ASHA_HOME
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=900, env=env)
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


def fresh_clone(name: str) -> Path:
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


def commit(root: Path, text: str) -> str:
    (root / "pydetactl" / "README.md").write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", f"content {text.strip()}")
    return git(root, "rev-parse", "HEAD")


def prepare(root: Path, head: str) -> dict:
    started = asha("asha_review", {"root": str(root), "base": BASE,
                                   "head": head})
    if "run" not in started:
        return {"error": started}
    run_id = started["run"]["run_id"]
    packet = started["packet"]
    stages = []
    for _ in range(3):
        submitted = asha("asha_review_submit_stage", {
            "root": str(root), "review_run_id": run_id,
            "stage": packet["stage"], "stage_id": packet["stage_id"],
            "verdict": "APPROVE", "summary": "phase 11 scenario",
            "findings": [], "reviewer_metadata": {"host_client": "phase11"}})
        stages.append(submitted.get("status"))
        nxt = asha("asha_review_next_stage",
                   {"root": str(root), "review_run_id": run_id})
        packet = nxt if isinstance(nxt, dict) and "stage" in nxt else packet
    return {"run_id": run_id, "target": started["run"].get("target"),
            "stages": stages}


def finalize(root: Path, run_id: str) -> dict:
    final = asha("asha_review_finalize",
                 {"root": str(root), "review_run_id": run_id})
    gate = final.get("gate") or {}
    return {"state": final.get("state"), "decision": final.get("decision"),
            "approved": final.get("approved"),
            "blocking_reason": final.get("blocking_reason"),
            "gate_state": gate.get("gate_state"),
            "authorized_to_ship": gate.get("authorized_to_ship"),
            "snapshot_id": gate.get("snapshot_id")}


def identity(root: Path) -> dict:
    return {"head": git(root, "rev-parse", "HEAD"),
            "head_tree": git(root, "rev-parse", "HEAD^{tree}"),
            "status": git(root, "status", "--porcelain").splitlines()[:5]}


def scenario(name: str, root: Path, head: str, mutate) -> dict:
    prepared = prepare(root, head)
    if "error" in prepared:
        return {"scenario": name, "error": prepared["error"]}
    reviewed = {"head_ref": head,
                "commit": prepared["target"]["head_sha"],
                "tree": git(root, "rev-parse",
                            f"{prepared['target']['head_sha']}^{{tree}}")}
    mutate(root)
    return {"scenario": name, "reviewed": reviewed,
            "review_stages": prepared["stages"],
            "candidate": identity(root),
            "review": finalize(root, prepared["run_id"])}


def main() -> int:
    BENCH.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}

    root = fresh_clone("a")
    first = commit(root, "# S1\n")
    out["A"] = scenario("A sha-pinned, branch moved, identical tree", root,
                        first,
                        lambda r: git(r, "commit", "-q", "--allow-empty",
                                      "-m", "same tree, new commit"))
    out["A"]["expected"] = "FINALIZED/APPROVED: trees identical, no false stale"

    root = fresh_clone("b")
    first = commit(root, "# S1\n")
    out["B"] = scenario("B sha-pinned, candidate tree changed", root, first,
                        lambda r: commit(r, "# S2 different\n"))
    out["B"]["expected"] = "STALE, gate NOT_EXECUTED, unauthorized"

    root = fresh_clone("c")
    commit(root, "# S1\n")
    out["C"] = scenario("C ref-pinned, ref moved", root, "HEAD",
                        lambda r: commit(r, "# S2 different\n"))
    out["C"]["expected"] = "STALE, unauthorized"

    root = fresh_clone("d1")
    first = commit(root, "# S1\n")
    git(root, "tag", "-a", "reviewed-tag", "-m", "annotated tag", first)
    out["D1"] = scenario("D1 annotated tag, tree changed", root, "reviewed-tag",
                         lambda r: commit(r, "# S2 different\n"))
    out["D1"]["expected"] = "STALE after the tree changes"

    root = fresh_clone("d2")
    commit(root, "# S1\n")
    commit(root, "# S2\n")
    out["D2"] = scenario("D2 HEAD~1 reviewed while HEAD is checked out", root,
                         "HEAD~1", lambda r: None)
    out["D2"]["expected"] = "refused: reviewed content is not the checked-out tree"

    root = fresh_clone("d3")
    first = commit(root, "# S1\n")
    git(root, "checkout", "-q", "--detach", first)
    out["D3"] = scenario("D3 detached HEAD at the reviewed commit", root, first,
                         lambda r: None)
    out["D3"]["expected"] = "FINALIZED/APPROVED: identical content"

    root = fresh_clone("e")
    first = commit(root, "# S1\n")
    out["E"] = scenario(
        "E tracked file modified after the review", root, first,
        lambda r: (r / "pydetactl" / "README.md").write_text("# local edit\n",
                                                            encoding="utf-8"))
    out["E"]["expected"] = "REFUSED, unauthorized"

    root = fresh_clone("f")
    first = commit(root, "# S1\n")
    out["F"] = scenario(
        "F untracked user file created after the review", root, first,
        lambda r: (r / "user_note.txt").write_text("unreviewed\n",
                                                  encoding="utf-8"))
    out["F"]["expected"] = "REFUSED, unauthorized"

    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
