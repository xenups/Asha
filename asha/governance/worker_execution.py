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
import subprocess
import tempfile
from contextlib import suppress as _suppress
from pathlib import Path
from typing import Any

from asha import check_runner, evidence, scope_resolver, scoping
from asha.common import paths as common_paths
from asha.conflict import covered
from asha.runner import dispatch_runner
from asha.types import (
    TAIL_CHARS,
    WORKER_EVIDENCE_FIELDS,
    WORKER_TIMEOUT_S,
    OrchestratorError,
)
from asha.worktree import (
    WorktreeDispatcher,
    _commit_all,
    _git,
    _safe_id,
)

_SHA_RE = re.compile(r"[0-9a-f]{40}")


def default_execute(worker: dict[str, Any], worktree: Path
                    ) -> tuple[int, str]:
    """Production execution: run the worker's primary action in its
    worktree under the worker's `timeout` (WORKER_TIMEOUT_S when
    unspecified/None -- existing behavior preserved). On timeout the
    child TREE is killed and TimeoutExpired propagates; the caller maps
    it to FAILED/timeout_exceeded. The spawn/teardown primitive lives in
    runner._spawn (shared by every AgentRunner) -- this hook keeps its
    (rc, tail-of-combined-output) contract byte-for-byte. (Extracted
    verbatim from scheduler.py.)"""
    timeout = worker.get("timeout")
    if timeout is None:
        timeout = WORKER_TIMEOUT_S
    result = dispatch_runner(worker).execute(worker, worktree, timeout)
    combined = (result.stdout or "") + (result.stderr or "")
    return result.exit_code, combined[-TAIL_CHARS:]


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


def _strip_check_caches(path: Path) -> None:
    """Remove verification cache artifacts (pytest/ruff/mypy) that the
    baseline check pass leaves in the worktree, at any depth. Without
    this, untracked __pycache__/.pytest_cache/.ruff_cache dirs would
    surface in collect_worker_evidence's observed scope as violations
    for workers whose declared scope does not include them."""
    import shutil
    for name in ("__pycache__", ".pytest_cache", ".ruff_cache",
                 ".mypy_cache"):
        for target in path.rglob(name):
            shutil.rmtree(target, ignore_errors=True)


def baseline_checks(
    worker: dict[str, Any],
    path: Path,
    base: str,
    base_tree: str,
    repo: Path,
    task_id: str,
) -> dict:
    """Run verification on the CLEAN worktree (== base tree) BEFORE the
    worker executes. Captures pre-existing failures with the same
    environment, check command, configuration and relevant scope as the
    current pass -- the baseline for Delta Check.

    The worktree at this point is at base_commit, so the baseline
    corresponds exactly to the repository's pre-worker state. Identity
    fields (repo, service env, command, config) are recorded so a
    baseline is never silently reused for a different environment.
    """
    resolved = scope_resolver.resolve(
        path, base=base, paths=scope_resolver.changed_files(path, base=base))
    cls = classify_independence(str(worker.get("id")), resolved,
                                set(), {worker["id"]: worker}, [worker])
    decision = scoping_decision(path, resolved, cls)
    checks = _run_checks(decision, path, resolved)
    return {
        "checks": checks,
        "resolved": resolved,
        "decision_mode": getattr(decision, "mode", None),
        "repo": str(Path(repo).resolve()),
        "base": base,
        "base_tree": base_tree,
        "task_id": task_id,
    }


def _run_checks(decision, path: Path, resolved: dict) -> list[dict]:
    """Run the check matrix for a resolved scope (shared by baseline and
    current passes; identical machinery, identical environment)."""
    if decision.eligible:
        return check_runner.run_scoped(
            path,
            changed_files=list(resolved["affected_files"]),
            targeted_tests=list(decision.targeted_tests),
            mypy_targets=list(decision.mypy_targets),
        )
    return check_runner.run(path, resolved)


def run_worker_in_worktree(
    repo: Path,
    worker: dict[str, Any],
    *,
    task_id: str,
    keep_worktrees: bool = False,
    fast_path: bool = False,
    fast_path_classification: str = "UNKNOWN",
    preserve_on_failure: bool = False,
) -> dict[str, Any]:
    """Execute ONE worker through the modular path (H.2.1-C/D).

    OLD: GovernedScheduler(repo, [worker]).run()  (single worker)
    NEW: this function -- same observable contract:
      * isolated worktree (fast_path skips the worktree, direct repo)
      * worker cmd executed (default subprocess)
      * collect_worker_evidence -> sealed worker evidence (external)
      * verify + authoritative (ledger-derived) available for replay
      * worktree cleaned up unless keep_worktrees

    Returns the scheduler-shaped report: {
      'states': {wid: {state, reason, evidence}},
      'evidence': {wid: ev_path},
      'authoritative': {wid: bytes},
      'worktrees': cleared,
      'cleanup_errors': [],
      'task_id': task_id,
    }
    """
    repo = Path(repo).resolve()
    dispatcher = WorktreeDispatcher(repo, keep=keep_worktrees)
    base_commit = dispatcher.base_commit
    base_tree = dispatcher.base_tree
    wid = worker["id"]
    states: dict[str, Any] = {}
    evidence_paths: dict[str, str] = {}
    authoritative: dict[str, bytes] = {}
    outcome: dict[str, Any] | None = None
    created_path: Path | None = None

    try:
        if fast_path:
            # direct repo execution, serialized evidence binding
            base = _git(repo, "rev-parse", "HEAD").strip()
            base_tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
            try:
                result = default_execute(worker, repo)
            except subprocess.TimeoutExpired:
                outcome = {"state": "FAILED",
                           "reason": "timeout_exceeded", "evidence": None}
                states[wid] = outcome
                return _report(task_id, states, evidence_paths,
                               authoritative, dispatcher)
            rc, tail = _split_result(result)
            outcome = collect_worker_evidence(
                worker, repo, rc, tail, task_id,
                common_paths.get_orchestrator_dir(repo) / _safe_id(task_id),
                base=base, base_tree=base_tree)
        else:
            path = dispatcher.create(wid)
            created_path = path
            baseline_journal: dict | None = None
            try:
                baseline_journal = baseline_checks(
                    worker, path, base_commit, base_tree, repo, task_id)
                _strip_check_caches(path)
            except Exception:
                # baseline must never mask the worker failure; a failed
                # baseline yields UNKNOWN (fail closed) downstream
                baseline_journal = None
            try:
                result = default_execute(worker, path)
            except subprocess.TimeoutExpired:
                outcome = {"state": "FAILED",
                           "reason": "timeout_exceeded", "evidence": None}
                states[wid] = outcome
                return _report(task_id, states, evidence_paths,
                               authoritative, dispatcher, created_path,
                               fast_path_classification=fast_path_classification)
            except Exception as exc:
                outcome = {"state": "FAILED",
                           "reason": f"execution_error:{type(exc).__name__}: "
                                     f"{exc}",
                           "evidence": None}
                states[wid] = outcome
                return _report(task_id, states, evidence_paths,
                               authoritative, dispatcher, created_path,
                               fast_path_classification=fast_path_classification)
            rc, tail = _split_result(result)
            outcome = collect_worker_evidence(
                worker, path, rc, tail, task_id,
                common_paths.get_orchestrator_dir(repo) / _safe_id(task_id),
                base=base_commit, base_tree=base_tree,
                baseline_checks=(
                    (baseline_journal or {}).get("checks")
                    if baseline_journal else None))
        states[wid] = outcome
        if outcome.get("evidence"):
            evidence_paths[wid] = outcome["evidence"]
            authoritative[wid] = _derive_authoritative(
                outcome, worker, base_tree)
    finally:
        if preserve_on_failure and outcome and outcome.get("state") in (
                "FAILED", "INVALID_EVIDENCE"):
            run_dir = (common_paths.get_orchestrator_dir(repo)
                       / _safe_id(task_id))
            exec_path = created_path if created_path is not None else repo
            with _suppress(Exception):
                persist_failure_evidence(
                    worker, exec_path, outcome, run_dir)
        dispatcher.cleanup()

    return _report(task_id, states, evidence_paths, authoritative,
                   dispatcher, created_path, fast_path=fast_path,
                   fast_path_classification=fast_path_classification)




def persist_failure_evidence(
    worker: dict[str, Any],
    path: Path,
    outcome: dict[str, Any],
    run_dir: Path,
) -> None:
    """Persist execution evidence for a failed worker.

    Called BEFORE worktree cleanup so the real artifacts survive:
      * execution.log      -- combined stdout/stderr of the worker command
      * diff.patch         -- repository/worktree diff at the failure point
      * worker-status.json -- the outcome dict (state, reason, exit code)
    Writes are atomic (temp sibling + os.replace). Best-effort: a
    persistence failure must not mask the worker's own failure.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    tail = outcome.get("output_tail") or ""
    # execution.log: full combined output is not retained (only the tail
    # survives collect_worker_evidence); persist what we have plus the
    # exit code and reason so the failure is reproducible.
    log_lines = [
        f"state: {outcome.get('state')}",
        f"reason: {outcome.get('reason')}",
        f"exit_status: {outcome.get('exit_status')}",
        "",
        tail,
    ]
    log_path = run_dir / "execution.log"
    log_path.write_text("\n".join(log_lines), encoding="utf-8")
    # diff.patch: worktree diff vs base when the worktree still exists.
    try:
        diff = _git(path, "diff", "HEAD", strip=False)
    except Exception:
        diff = ""
    if diff:
        (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
    status_path = run_dir / "worker-status.json"
    _atomic_json(status_path, {
        "worker_id": worker.get("id"),
        "state": outcome.get("state"),
        "reason": outcome.get("reason"),
        "exit_status": outcome.get("exit_status"),
    })

def _split_result(result) -> tuple[int, str]:
    if isinstance(result, tuple):
        return int(result[0]), str(result[1])
    return int(result), ""


def _report(task_id: str, states: dict, evidence_paths: dict,
            authoritative: dict, dispatcher,
            created_path: Path | None = None,
            fast_path: bool = False,
            fast_path_classification: str = "UNKNOWN") -> dict[str, Any]:
    worktrees: dict[str, Any] = {}
    if created_path is not None and states:
        wid = next(iter(states))
        worktrees[wid] = {"path": str(created_path), "state": "created"}
    routing: dict[str, Any] = {}
    if states:
        wid = next(iter(states))
        routing[wid] = {
            "mode": ("fast_path" if fast_path else "full_governance"),
            "reason_code": "executed",
            "classification": fast_path_classification,
        }
    return {
        "task_id": task_id,
        "status": ("ok" if any(s.get("state") == "DONE"
                                for s in states.values()) else "failed"),
        "states": states,
        "evidence": evidence_paths,
        "authoritative": authoritative,
        "worktrees": worktrees,
        "routing": routing,
        "cleanup_errors": list(dispatcher.cleanup_errors),
    }


def _derive_authoritative(outcome: dict, worker: dict,
                          base_tree: str) -> bytes:
    """Ledger-derived authoritative record (extracted from _collect's
    ledger section): replay/ship verification bytes."""
    import json
    evidence_path = outcome.get("evidence")
    if not evidence_path:
        return b""
    try:
        payload = json.loads(Path(evidence_path).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return b""
    return evidence.canonicalize_evidence(
        evidence.AuthoritativeEvidence.create(
            worker_id=worker["id"],
            generation=0,
            base_tree_sha=base_tree,
            target_tree_sha=payload.get("target_tree_sha", ""),
            observed_scope=evidence.ObservedScope(
                reads=frozenset(str(e) for e in (worker.get("reads") or [])),
                writes=frozenset(str(e) for e in
                                 (payload.get("observed_scope") or [])),
                capture_mode=evidence.ScopeCaptureMode.STRICT,
            ),
            verdict=evidence.GovernanceVerdict(
                status=evidence.VerdictStatus.PASS,
                reason_code="evidence_sealed"),
        ))


def collect_worker_evidence(
                            worker: dict[str, Any],
                            path: Path,
                            rc: int,
                            tail: str,
                            task_id: str,
                            evidence_dir: Path,
                            *,
                            base: str | None = None,
                            base_tree: str | None = None,
                            classification=None,
                            uncertain: set[str] | frozenset[str] = frozenset(),
                            by_id: dict[str, dict[str, Any]] | None = None,
                            workers: list[dict[str, Any]] | None = None,
                            baseline_checks: list[dict] | None = None,
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
        delta_ran = False
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
        checks = _run_checks(decision, path, resolved)
        failed = [entry["name"] for entry in checks
                  if entry["status"] == "failed"]
        if checks and all(entry["status"] == "skipped"
                          for entry in checks):
            return {"state": "INVALID_EVIDENCE",
                    "reason": "no_verification_ran", "evidence": None}
        if failed:
            from asha.governance import delta
            baseline_checks = (list(baseline_checks)
                               if baseline_checks is not None else None)
            cur_fails = delta.extract_failures(checks)
            base_fails = (delta.extract_failures(baseline_checks)
                          if baseline_checks is not None else None)
            verdict = delta.verdict_for(
                cur_fails, base_fails, has_failed_checks=bool(failed))
            new_fails = delta.delta_failures(cur_fails, base_fails)
            outcome: dict[str, Any] = {
                "state": "FAILED",
                "reason": "verification_failed:" + ",".join(failed),
                "evidence": None,
                "verdict": verdict,
                "baseline_failures": (
                    [f.to_dict() for f in sorted(base_fails,
                                               key=lambda i: i.location)]
                    if base_fails is not None else None),
                "current_failures": [
                    f.to_dict() for f in sorted(cur_fails,
                                                key=lambda i: i.location)],
                "delta_failures": [
                    f.to_dict() for f in sorted(new_fails,
                                                key=lambda i: i.location)],
            }
            delta_ran = True
            pre_existing_only = verdict == "PRE_EXISTING_ONLY"
            if not pre_existing_only:
                return outcome
            # Pre-existing-only failures are NOT attributed to this worker:
            # the worker introduced no new failures, so its verification
            # still passes. Fall through to the evidence seal below so the
            # delta decision (baseline/current/verdict) is persisted and
            # auditable; the payload carries the failure lists.
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
        if delta_ran:
            payload["verdict"] = verdict
            payload["baseline_failures"] = (
                [f.to_dict() for f in sorted(base_fails,
                                             key=lambda i: i.location)]
                if base_fails is not None else None)
            payload["current_failures"] = [
                f.to_dict() for f in sorted(cur_fails,
                                             key=lambda i: i.location)]
            payload["delta_failures"] = [
                f.to_dict() for f in sorted(new_fails,
                                             key=lambda i: i.location)]
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
    done: dict[str, Any] = {
        "state": "DONE", "reason": None, "evidence": str(evi_path),
        "observed": list(observed), "target_tree": target_tree}
    if delta_ran:
        done["verdict"] = verdict
        done["baseline_failures"] = (
            [f.to_dict() for f in sorted(base_fails,
                                         key=lambda i: i.location)]
            if base_fails is not None else None)
        done["current_failures"] = [
            f.to_dict() for f in sorted(cur_fails,
                                         key=lambda i: i.location)]
        done["delta_failures"] = [
            f.to_dict() for f in sorted(new_fails,
                                         key=lambda i: i.location)]
    return done
