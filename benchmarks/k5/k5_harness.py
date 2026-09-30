"""K.5.2 benchmark harness — execution against the frozen K.5.1 spec.

Implements Path A (blind), Path B (semi-governed: scope + verify, no
delta), Path C (full governance via the PRODUCTION asha entry point).
Path C MUST go through asha.governance.dag.run_workers_dag; governance
logic is never re-implemented here. Path B reuses the same check_runner
invocation but applies the pre-K.3 rule (any failure blocks) against the
worker's worktree; Path A applies the patch to a scratch copy and runs
the check matrix once, blocking on ANY failure.

Do not modify after first valid run (K.5.1 freeze).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path("/home/amir/src/Asha")
sys.path.insert(0, str(REPO_ROOT))

from asha import (
    check_runner,
    scope_resolver,
)
from asha.conflict import covered
from asha.governance.dag import run_workers_dag

K5 = Path(__file__).resolve().parent
SPEC = K5 / "FROZEN_SPEC.md"
OUT = K5 / "results"
TMP = Path("/tmp/asha-k5")
REPS = 3          # measured repetitions after 1 warm-up
WARMUP = 1


# --------------------------------------------------------------------------
# small git helpers
# --------------------------------------------------------------------------
def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args],
                          check=True, capture_output=True, text=True).stdout


def git_ok(root: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True).returncode == 0


def mkrepo(name: str, files: dict[str, str],
           gitignore="__pycache__/\n*.pyc\n.jspace/\n") -> Path:
    root = TMP / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(root, "init", "-q", "-b", "main", ".")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def patch_file(root: Path, task: str, patch: dict) -> None:
    """Apply the task patch (list of {path, new_content | delete}) into
    `root` (the patch paths are repo-relative)."""
    for op in patch["ops"]:
        p = root / op["path"]
        if op.get("delete"):
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(op["content"], encoding="utf-8")


def patch_hash(patch: dict) -> str:
    return hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()[:16]


def full_check_matrix(root: Path, base: str | None = None) -> list[dict]:
    """Run the check matrix A/B use on a tree, scoped to the actual
    changed files (mirrors the production resolve path)."""
    affected = scope_resolver.changed_files(root, base=base) or ["."]
    resolved = scope_resolver.resolve(
        root, base=base, paths=affected)
    return check_runner.run(root, resolved)


# --------------------------------------------------------------------------
# Path A — blind execute
# --------------------------------------------------------------------------
def path_a(root: Path, patch: dict) -> dict:
    t0 = time.monotonic()
    # scratch repo at SAME base, apply patch, run matrix once
    scratch = root.parent / (root.name + "-A")
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(root, scratch, ignore=shutil.ignore_patterns(
        ".git", ".venv", ".jspace"))
    git(scratch, "init", "-q", "-b", "main", ".")
    git(scratch, "config", "user.email", "t@t")
    git(scratch, "config", "user.name", "t")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", "base")
    patch_file(scratch, "t", patch)
    if git(scratch, "status", "--porcelain").strip():
        git(scratch, "add", "-A")
        git(scratch, "commit", "-qm", "patch")
    checks = full_check_matrix(scratch)
    failed = [c["name"] for c in checks if c["status"] == "failed"]
    elapsed = (time.monotonic() - t0) * 1000
    return {
        "observed_execution_verdict": "BLOCKED" if failed else "DONE",
        "observed_detection_classification":
            "REJECT_BLIND" if failed else "CLEAN_PASS",
        "metrics": {
            "execution_time_ms": round(elapsed, 1),
            "execution_started": True,
            "execution_prevented": bool(failed),
            "delta_calculated": False,
            "evidence_reproducible": False,
        },
        "_failed_checks": failed,
    }


# --------------------------------------------------------------------------
# Path B — semi-governed (scope + verify, NO delta)
# --------------------------------------------------------------------------
def path_b(root: Path, worker: dict, patch: dict, task_id: str) -> dict:
    t0 = time.monotonic()
    # worktree from base, apply patch, run check matrix, ANY failure blocks
    base = git(root, "rev-parse", "HEAD").strip()
    wt_dir = TMP / f"{task_id}-B-wt"
    if wt_dir.exists():
        shutil.rmtree(wt_dir)
    git(root, "worktree", "add", "-q", str(wt_dir), base)
    try:
        patch_file(wt_dir, "t", patch)
        if git(wt_dir, "status", "--porcelain").strip():
            git(wt_dir, "add", "-A")
            git(wt_dir, "commit", "-qm", "worker change")
        # scope enforcement (mirrors worker_execution: every observed
        # change must be covered by a declared scope entry)
        declared = list(worker.get("declared_scope") or ())
        observed = scope_resolver.changed_files(wt_dir, base=base)
        scope_ok = all(any(covered(item, entry) for entry in declared)
                       for item in observed)
        if not scope_ok:
            return {
                "observed_execution_verdict": "BLOCKED",
                "observed_detection_classification":
                    "REJECT_SCOPE_VIOLATION",
                "metrics": {"execution_time_ms":
                            round((time.monotonic() - t0) * 1000, 1),
                            "scope_violated": True,
                            "scope_violation_detected": True,
                            "execution_started": True,
                            "execution_prevented": True,
                            "delta_calculated": False,
                            "evidence_reproducible": False},
            }
        checks = full_check_matrix(wt_dir, base=base)
        failed = [c["name"] for c in checks if c["status"] == "failed"]
        # PRE-K.3 rule: any failure blocks, no baseline distinction.
        # Classification: when the worker's own change is non-empty a
        # failure is attributed to it (REJECT_NEW_REGRESSION); when the
        # worker changed nothing, a failing matrix is a blind block
        # (pre-existing failures B cannot distinguish).
        worker_changed = bool(
            scope_resolver.changed_files(wt_dir, base=base))
        if failed:
            obs_d = ("REJECT_NEW_REGRESSION" if worker_changed
                     else "REJECT_BLIND")
        else:
            obs_d = "CLEAN_PASS"
        return {
            "observed_execution_verdict":
                "BLOCKED" if failed else "DONE",
            "observed_detection_classification": obs_d,
            "metrics": {
                "execution_time_ms":
                    round((time.monotonic() - t0) * 1000, 1),
                "execution_started": True,
                "execution_prevented": bool(failed),
                "delta_calculated": False,
                "evidence_reproducible": False,
            },
            "_failed_checks": failed,
        }
    finally:
        git(root, "worktree", "remove", "--force", str(wt_dir))


# --------------------------------------------------------------------------
# Path C — full governance (PRODUCTION entry point)
# --------------------------------------------------------------------------
def path_c(root: Path, worker: dict, task_id: str) -> tuple[dict, Path | None]:
    t0 = time.monotonic()
    os.environ["ASHA_STATE_DIR"] = str(root / ".jspace")
    report = run_workers_dag(root, [worker], task_id=task_id)
    elapsed = (time.monotonic() - t0) * 1000
    st = (report.get("states") or {}).get(worker["id"]) or {}
    ev = (report.get("evidence") or {}).get(worker["id"])
    ev_path = Path(ev) if ev and Path(ev).exists() else None
    verdict_map = {
        "DONE": "DONE",
        "FAILED": "BLOCKED",
        "INVALID_EVIDENCE": "BLOCKED",
    }
    det_map = {
        "NEW_FAILURES": "REJECT_NEW_REGRESSION",
        "PRE_EXISTING_ONLY": "CLEAN_PASS",
        "NO_FAILURES": "CLEAN_PASS",
        "UNKNOWN": "REJECT_BLIND",
    }
    obs_v = verdict_map.get(st.get("state"), "BLOCKED")
    verdict = st.get("verdict")
    # DONE with no explicit verdict = clean pass (no failures, no delta)
    if obs_v == "DONE" and verdict is None:
        obs_d = "CLEAN_PASS"
    else:
        obs_d = det_map.get(verdict, "REJECT_BLIND")
    if "scope_violation" in str(st.get("reason") or ""):
        obs_d = "REJECT_SCOPE_VIOLATION"
    if ("no service-local Python" in str(st.get("reason") or "")
            or "ALLOW_SYSTEM_PYTHON_FALLBACK" in str(st.get("reason") or "")):
        obs_d = "REJECT_ENV_VIOLATION"
    return {
        "observed_execution_verdict": obs_v,
        "observed_detection_classification": obs_d,
        "metrics": {
            "execution_time_ms": round(elapsed, 1),
            "execution_started": True,
            "execution_prevented": obs_v == "BLOCKED",
            "delta_calculated": st.get("verdict") is not None,
            "evidence_reproducible": ev_path is not None,
        },
        "_state": st,
        "_evidence_path": str(ev_path) if ev_path else None,
    }, ev_path