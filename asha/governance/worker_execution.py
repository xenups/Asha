"""Worker execution + evidence collection (H.2.1 extraction).

Extracted from asha/scheduler.py WITHOUT semantic change. Natural home:
the governance package (worker evidence is a governance artifact).

This module is the single implementation of the OLD scheduler `_collect`
contract so both the legacy scheduler (H.2.2 deletion target) and the
modular CLI/MCP paths consume IDENTICAL behavior:

* OLD: GovernedScheduler._collect(worker, worktree, rc, tail, ...)
* NEW: worker_execution.collect_worker_evidence(worker, path, rc, tail,
                                               base=, base_tree=)

Observationally identical: worker-scope violations -> INVALID_EVIDENCE /
scope_violation; failing checks -> FAILED/verification_failed; all-skipped
-> INVALID_EVIDENCE/no_verification_ran; seal + verified digest; worker
evidence never authorizes ship (merge law).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from asha import check_runner, evidence, scope_resolver, scoping
from asha.common import paths as common_paths
from asha.conflict import covered
from asha.types import (
    TAIL_CHARS,
    WORKER_EVIDENCE_FIELDS,
    OrchestratorError,
)
from asha.worktree import _commit_all, _git, _safe_id

_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """IO plumbing only: atomic temp-sibling write (same pattern as
    evidence.write, different destination so the ship artifact at the
    evidence dir is never touched)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True,
                       ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".worker-evidence-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def verify_worker_evidence(path: Path | str,
                           worktree: Path | None = None) -> dict[str, Any]:
    """Fail-closed worker-evidence verification: canonical digest intact,
    Phase-1 identity fields present, worker evidence can never authorize a
    ship (merge law), and -- when the worktree still exists -- the sealed
    target_tree_sha re-binds to the live git tree. Raises on anything
    unproven; returns the payload. (Exact scheduler.py:73 body.)"""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise OrchestratorError(f"worker evidence unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise OrchestratorError("worker evidence must be a JSON object")
    recorded = payload.get("evidence_sha256")
    if not isinstance(recorded, str) or \
            evidence.compute_digest(payload) != recorded:
        raise OrchestratorError(
            "worker evidence digest mismatch (modified after sealing)")
    if payload.get("authorized_to_ship") is not False:
        raise OrchestratorError(
            "worker evidence must seal authorized_to_ship=false "
            "(PASS(A)+PASS(B) != PASS(A U B); ship stays with the gate)")
    missing = [field for field in WORKER_EVIDENCE_FIELDS
               if field not in payload]
    if missing:
        raise OrchestratorError(
            "worker evidence missing fields: " + ", ".join(missing))
    target = payload.get("target_tree_sha")
    if not isinstance(target, str) or not _SHA_RE.fullmatch(target):
        raise OrchestratorError("missing/invalid tree identity "
                                f"(target_tree_sha={target!r})")
    if target != payload.get("tree_hash"):
        raise OrchestratorError("target_tree_sha/tree_hash mismatch")
    if worktree is not None:
        live = _git(Path(worktree), "rev-parse", "HEAD^{tree}")
        if live != target:
            raise OrchestratorError(
                "tree identity mismatch: evidence " + target
                + " != live " + live)
    return payload


def classify_independence(wid: str,
                          resolved: dict[str, Any],
                          uncertain: set[str],
                          by_id: dict[str, dict[str, Any]],
                          workers: list[dict[str, Any]],
                          ) -> evidence.IndependenceClassification:
    """Phase 3.1 adaptive-policy hook -- pure (extracted from
    GovernedScheduler._classify_independence). Classifies ONLY from
    information the caller holds authoritatively. Gaps fail closed to
    UNKNOWN; MINIMAL reachable only when disjointness is proven."""
    if resolved.get("status") != "certain":
        return evidence.IndependenceClassification.UNKNOWN
    if wid in uncertain:
        return evidence.IndependenceClassification.UNKNOWN
    mine = by_id[wid]
    my_reads = {str(entry) for entry in (mine.get("reads") or [])}
    my_writes = {str(entry) for entry in (mine.get("writes") or [])}
    for other in workers:
        if str(other["id"]) == wid:
            continue
        other_reads = {str(entry)
                       for entry in (other.get("reads") or [])}
        other_writes = {str(entry)
                        for entry in (other.get("writes") or [])}
        if (my_writes & other_writes
                or my_writes & other_reads
                or my_reads & other_writes):
            return evidence.IndependenceClassification.PROVEN_SHARED
    return evidence.IndependenceClassification.PROVEN_DISJOINT


def scoping_decision(root: Path,
                     resolved: dict[str, Any],
                     classification: evidence.IndependenceClassification,
                     ) -> scoping.ScopingDecision:
    """Fail-closed boundary between classification and validation depth.
    (Extracted verbatim from GovernedScheduler._scoping_decision.)"""
    level = resolved.get("scope")
    try:
        decision = scoping.assess_scoping_eligibility(
            root,
            list(resolved.get("affected_files") or []),
            getattr(classification, "value", str(classification)),
            str(level) if level is not None else "",
            scope_status=str(resolved.get("status") or ""),
            envelope_valid=True,
        )
        if not isinstance(decision, scoping.ScopingDecision):
            raise TypeError(
                f"unexpected decision type {type(decision).__name__}")
        if decision.mode == scoping.SCOPED and not decision.eligible:
            raise ValueError("forged: SCOPED mode without eligibility")
        if decision.eligible and decision.mode != scoping.SCOPED:
            raise ValueError("forged: eligible with non-SCOPED mode")
        if decision.eligible and decision.fallback_reason:
            raise ValueError("forged: eligible with fallback_reason")
        if decision.eligible and not isinstance(decision.mypy_targets, tuple):
            raise ValueError("forged: mypy_targets must be a tuple")
    except ValueError as exc:
        if str(exc).startswith("forged:"):
            return scoping.complete_decision(
                scoping.F_INVALID_DECISION, str(exc))
        return scoping.complete_decision(
            scoping.F_GRAPH_FAILURE, f"{type(exc).__name__}: {exc}"[:200])
    except TypeError as exc:
        return scoping.complete_decision(
            scoping.F_INVALID_DECISION, f"{type(exc).__name__}: {exc}"[:200])
    except Exception as exc:
        return scoping.complete_decision(
            scoping.F_ELIGIBILITY_EXCEPTION,
            f"{type(exc).__name__}: {exc}"[:200])
    return decision


def collect_worker_evidence(worker: dict[str, Any],
                            path: Path,
                            rc: int,
                            tail: str,
                            task_id: str,
                            evidence_dir: Path,
                            *,
                            base: str | None = None,
                            base_tree: str | None = None,
                            classification=None,
                            uncertain: set[str] = frozenset(),
                            by_id: dict[str, dict[str, Any]] | None = None,
                            workers: list[dict[str, Any]] | None = None,
                            ) -> dict[str, Any]:
    """Extracted GovernedScheduler._collect. `evidence_dir` is the
    scheduler-owned orchestrator dir (external Zone-2), NOT the ship
    evidence dir. Returns the run-loop state dict verbatim."""
    wid = worker["id"]
    if base is None or base_tree is None:
        raise OrchestratorError(
            "collect_worker_evidence requires explicit base/base_tree "
            "(run-level identity, captured before the worker commits)")
    effective_base = base
    effective_base_tree = base_tree
    if rc != 0:
        return {"state": "FAILED", "reason": f"worker_exit_{rc}",
                "evidence": None}
    try:
        dirty = [line for line in
                 _git(path, "status", "--porcelain").splitlines()
                 if line.strip()]
        if dirty:
            _commit_all(path, f"orchestrator: worker {wid}")
        commit_line, tree_line = _git(path, "rev-parse", "HEAD",
                                      "HEAD^{tree}").split()
        target_commit, target_tree = commit_line, tree_line
        observed = scope_resolver.changed_files(
            path, base=effective_base)
        declared = worker.get("declared_scope") or []
        violations = [item for item in observed
                      if not any(covered(item, entry)
                                 for entry in declared)]
        if violations:
            return {"state": "INVALID_EVIDENCE",
                    "reason": "scope_violation:"
                    + ",".join(violations[:5]),
                    "evidence": None}
        resolved = scope_resolver.resolve(
            path, base=effective_base, paths=observed)
        cls = classification
        if cls is None:
            by = by_id if by_id is not None else {wid: worker}
            cls = classify_independence(
                str(wid), resolved, set(uncertain), by,
                workers if workers is not None else [worker])
        decision = scoping_decision(path, resolved, cls)
        if decision.eligible:
            checks = check_runner.run_scoped(
                path,
                changed_files=list(resolved["affected_files"]),
                targeted_tests=list(decision.targeted_tests),
                mypy_targets=list(decision.mypy_targets),
            )
        else:
            checks = check_runner.run(path, resolved)
        failed = [entry["name"] for entry in checks
                  if entry["status"] == "failed"]
        if failed:
            return {"state": "FAILED",
                    "reason": "verification_failed:" + ",".join(failed),
                    "evidence": None}
        if checks and all(entry["status"] == "skipped"
                          for entry in checks):
            return {"state": "INVALID_EVIDENCE",
                    "reason": "no_verification_ran", "evidence": None}
        diff = _git(path, "diff", effective_base,
                    target_commit, strip=False)
        payload: dict[str, Any] = {
            "schema": evidence.SCHEMA,
            "stage": "worker",
            "scope": resolved["scope"],
            "commit": target_commit,
            "tree_hash": target_tree,
            "observed_at": evidence.now_iso(),
            "checks": checks,
            "authorized_to_ship": False,
            "task_id": task_id,
            "worker_id": wid,
            "base_commit": effective_base,
            "base_tree_sha": effective_base_tree,
            "target_tree_sha": target_tree,
            "declared_scope": list(declared),
            "observed_scope": observed,
            "read_set": worker.get("reads"),
            "write_set": worker.get("writes"),
            "diff": diff,
            "exit_status": rc,
        }
        payload["validation_mode"] = decision.mode
        if decision.eligible:
            payload["targeted_tests"] = list(decision.targeted_tests)
            payload["mypy_targets"] = list(decision.mypy_targets)
            payload["omission_rationale"] = ";".join(decision.rationale)
        else:
            payload["fallback_reason"] = decision.fallback_reason
        if tail:
            payload["output_tail"] = tail[-TAIL_CHARS:]
        sealed = evidence.seal(payload)
        evi_path = evidence_dir / (_safe_id(wid) + ".json")
        _atomic_json(evi_path, sealed)
        verify_worker_evidence(evi_path, worktree=path)
    except (OrchestratorError, scope_resolver.ScopeError,
            evidence.EvidenceError, OSError) as exc:
        return {"state": "INVALID_EVIDENCE",
                "reason": f"{type(exc).__name__}: {exc}",
                "evidence": None}
    return {"state": "DONE", "reason": None, "evidence": str(evi_path),
            "observed": list(observed), "target_tree": target_tree}