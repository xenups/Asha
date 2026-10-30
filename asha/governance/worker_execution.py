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

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress as _suppress
from pathlib import Path
from typing import Any

from asha import check_runner, evidence, scope_resolver, scoping, timeouts, validation
from asha.common import paths as common_paths
from asha.conflict import covered
from asha.execution import ExecutionOutcome
from asha.governance import delta
from asha.runner import dispatch_runner
from asha.types import (
    TAIL_CHARS,
    WORKER_EVIDENCE_FIELDS,
    OrchestratorError,
)
from asha.worktree import (
    WorktreeDispatcher,
    _commit_all,
    _git,
    _safe_id,
)

_SHA_RE = re.compile(r"[0-9a-f]{40}")


def execute_with_audit(worker: dict[str, Any], worktree: Path, *,
                       deadline: timeouts.Deadline | None = None
                       ) -> tuple[int, str, dict[str, Any]]:
    """`default_execute` plus the runner's audit record (verify_* facts).

    Same spawn/teardown primitive, same timeout policy, same
    (rc, tail-of-combined-output) values -- the third element is what
    evidence needs to record whether a supplied acceptance command
    actually ran and how it exited. No second execution path.
    """
    # One authoritative budget: an explicit worker override (validated, so a
    # zero, negative, boolean or non-numeric value is refused here instead of
    # silently meaning "never kill") or the centralized policy.
    timeout = timeouts.resolve("worker", worker.get("timeout"))
    started = time.monotonic()
    result = dispatch_runner(worker).execute(worker, worktree, timeout,
                                             deadline=deadline)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    combined = (result.stdout or "") + (result.stderr or "")
    audit = dict(result.audit_metadata or {})
    audit.setdefault("duration_ms", elapsed_ms)
    return result.exit_code, combined[-TAIL_CHARS:], audit


def execution_facts(outcome: ExecutionOutcome) -> dict[str, Any]:
    """The facts a caller may report about one execution outcome.

    Additive by design: every field describes something that was observed, so
    a report never claims more than the run produced.
    """
    return {
        "execution_outcome": outcome.outcome,
        "launched": outcome.launched,
        "exit_code": outcome.exit_code,
        "timeout_s": outcome.timeout_s,
        "elapsed_ms": outcome.duration_ms,
        "process_termination": outcome.process_termination,
    }


def default_execute(worker: dict[str, Any], worktree: Path, *,
                    deadline: timeouts.Deadline | None = None
                    ) -> tuple[int, str]:
    """Production execution: run the worker's primary action in its
    worktree under the worker's `timeout` (WORKER_TIMEOUT_S when
    unspecified/None -- existing behavior preserved). On timeout the
    child TREE is killed and TimeoutExpired propagates; the caller maps
    it to FAILED/timeout_exceeded. The spawn/teardown primitive lives in
    runner._spawn (shared by every AgentRunner) -- this hook keeps its
    (rc, tail-of-combined-output) contract byte-for-byte. (Extracted
    verbatim from scheduler.py.)"""
    code, tail, _audit = execute_with_audit(worker, worktree,
                                            deadline=deadline)
    return code, tail


_ACCEPTANCE_PASSED_RE = re.compile(r"(\d+) passed")
_ACCEPTANCE_SKIPPED_RE = re.compile(r"(\d+) skipped")
_ACCEPTANCE_DESELECTED_RE = re.compile(r"(\d+) deselected")
ACCEPTANCE_FAILING = ("not_executed", "inconclusive", "failed")


def _last_count(tail: str, pattern: re.Pattern[str]) -> int | None:
    """Last occurrence of a pytest counter in the tail (final summary)."""
    found = pattern.findall(tail)
    return int(found[-1]) if found else None


def build_acceptance(worker: dict[str, Any],
                     audit: dict[str, Any] | None) -> dict[str, Any]:
    """The acceptance record carried into evidence and the CLI payload.

    Outcomes: not_required | not_executed | inconclusive | failed |
    passed. A command that did not run never gets a fabricated exit
    code, and "ran but collected nothing" is not a pass.
    """
    command = worker.get("verify_command")
    supplied = isinstance(command, str) and bool(command.strip())
    meta: dict[str, Any] = audit or {}
    ran = bool(meta.get("verify_ran"))
    exit_code = meta.get("verify_exit") if ran else None
    record: dict[str, Any] = {
        "command": str(command) if supplied else None,
        "supplied": supplied,
        "ran": ran,
        "exit_code": int(exit_code) if isinstance(exit_code, int) else None,
        "outcome": "not_required",
    }
    if not supplied:
        return record
    if not ran:
        record["outcome"] = "not_executed"
        record["detail"] = "acceptance command was supplied but never ran"
        return record
    tail = str(meta.get("verify_output_tail") or "")
    passed = _last_count(tail, _ACCEPTANCE_PASSED_RE)
    skipped = _last_count(tail, _ACCEPTANCE_SKIPPED_RE)
    deselected = _last_count(tail, _ACCEPTANCE_DESELECTED_RE)
    # pytest's own statuses: 4 = usage/file error, 5 = no tests collected
    if record["exit_code"] in (4, 5):
        record["outcome"] = "inconclusive"
        record["detail"] = (f"runner exit {record['exit_code']}: "
                            "no usable acceptance tests")
    elif record["exit_code"] != 0:
        record["outcome"] = "failed"
        record["detail"] = f"acceptance command exited {record['exit_code']}"
    elif not passed and ((skipped or 0) > 0 or (deselected or 0) > 0):
        record["outcome"] = "inconclusive"
        record["detail"] = (f"0 passed, {skipped or 0} skipped, "
                            f"{deselected or 0} deselected")
    else:
        record["outcome"] = "passed"
        if passed is not None:
            record["detail"] = f"{passed} passed"
    return record


def acceptance_failed(acceptance: dict[str, Any] | None) -> bool:
    """True when a supplied acceptance command did not demonstrably pass."""
    if not acceptance:
        return False
    return str(acceptance.get("outcome")) in ACCEPTANCE_FAILING


COMPARISON_REASONS = ("COMPARED", "NOT_COMPARED", "BASE_EQUALS_TARGET",
                      "INVENTORY_MISMATCH")


def _check_inventory(checks: list[dict] | None) -> list[str]:
    """Check-name inventory (the comparable part of a check run)."""
    return sorted({str(entry.get("name")) for entry in (checks or [])})


def build_comparison(*, base_tree: str | None, candidate_tree: str | None,
                     baseline_checks: list[dict] | None,
                     candidate_checks: list[dict] | None,
                     baseline_scope: str | None = None,
                     candidate_scope: str | None = None,
                     verdict: str | None = None) -> dict[str, Any]:
    """Truthful baseline-comparison status for one sealed result.

    `performed` is true only when a candidate-versus-baseline delta was
    actually computed over two compatible inventories AND the trees are
    genuinely distinct. Inspecting tree ids is not a comparison, and an
    identical baseline/candidate pair never yields "no regression".
    """
    record: dict[str, Any] = {
        "performed": False,
        "baseline_tree": base_tree,
        "candidate_tree": candidate_tree,
        "comparable": False,
        "reason": "NOT_COMPARED",
        "verdict": None,
        "detail": None,
    }
    if not base_tree or not candidate_tree:
        # Identity is mandatory: comparing without both tree ids would
        # have to invent them, so the answer is "not compared".
        record["detail"] = "missing_tree_identifier"
        return record
    if baseline_checks and delta.unparsed_failures(baseline_checks):
        # The baseline check ran and failed, but produced no identity
        # (pytest collection error, crash, timeout). That is unknown
        # attribution, never an empty baseline: no verdict is possible.
        record["detail"] = "baseline_failures_unparsed"
        return record
    if not baseline_checks or candidate_checks is None:
        return record                      # no baseline inventory at all
    if base_tree == candidate_tree:
        record["reason"] = "BASE_EQUALS_TARGET"
        return record
    if _check_inventory(baseline_checks) != _check_inventory(candidate_checks):
        record["reason"] = "INVENTORY_MISMATCH"
        return record
    if (baseline_scope is not None and candidate_scope is not None
            and baseline_scope != candidate_scope):
        record["reason"] = "INVENTORY_MISMATCH"
        return record
    record.update({"performed": True, "comparable": True,
                   "reason": "COMPARED", "verdict": verdict})
    return record


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
    """Remove verification cache artifacts (pytest/ruff/mypy, plus the
    K.6 codegraph cache) that the baseline check pass leaves in the
    worktree. Without this, untracked __pycache__/.pytest_cache/.ruff_cache
    dirs would surface in collect_worker_evidence's observed scope as
    violations for workers whose declared scope does not include them.
    The codegraph cache (.k6graph-test) is removed from the worktree root
    ONLY -- its exact test-pinned location; no recursive sweep, so a
    same-named directory anywhere else is never touched."""
    import shutil
    shutil.rmtree(path / ".k6graph-test", ignore_errors=True)
    for name in ("__pycache__", ".pytest_cache", ".ruff_cache",
                 ".mypy_cache"):
        for target in path.rglob(name):
            shutil.rmtree(target, ignore_errors=True)


def _failed_check_details(checks: list[dict]) -> list[dict]:
    """Compact retained diagnostics for checks that failed: name, exit
    code, explicit note (e.g. a timeout) and a trimmed diagnostic tail.
    Attached to the FAILED worker outcome so a rejected worker keeps the
    evidence needed to explain WHY verification failed -- without it the
    `evidence=None` branch discards every check output (rehearsal v2)."""
    details: list[dict] = []
    for entry in checks:
        if entry.get("status") != "failed":
            continue
        details.append({
            "name": entry.get("name"),
            "exit_code": entry.get("exit_code"),
            "note": entry.get("note"),
            "output_tail": (entry.get("output_tail") or "")[-500:],
        })
    return details


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
    checks = _run_checks(
        decision, path, resolved,
        timeout_s=check_runner.BASELINE_TIMEOUT_S,
        deps_root=_deps_root(path, repo))
    return {
        "checks": checks,
        "resolved": resolved,
        "decision_mode": getattr(decision, "mode", None),
        "repo": str(Path(repo).resolve()),
        "base": base,
        "base_tree": base_tree,
        "task_id": task_id,
    }


def persist_baseline_evidence(
    journal: dict | None,
    *,
    error: str | None,
    worker: dict[str, Any],
    path: Path,
    base: str,
    base_tree: str,
    repo: Path,
    task_id: str,
) -> None:
    """Best-effort baseline diagnostic artifact. NEVER raises.

    Persists the in-memory baseline journal BEFORE worker evidence
    collection can discard it (the rehearsal-v3 gap): raw check records
    (status, exit code, timeout note, bounded/redacted output tail), the
    parse result, and the execution outcome, independently of the worker
    verdict.

    State model — the three layers stay separate and there is NO
    ``clean``/verdict field in the payload:
      * ``execution``: "ok" | "exception" (the baseline pass itself;
        null ``checks`` only when it failed before any check ran)
      * ``checks``: raw records preserved verbatim (malformed entries
        are kept marked, never dropped)
      * ``parse``: "completed" | "error" | "not_run" + identities
    An empty identity list is NEVER proof of a clean baseline; the only
    verified-clean reading is: execution ok AND checks non-empty AND all
    passed AND parse completed AND zero identities. A MISSING artifact
    means unknown — never success, never "baseline skipped".
    Write failure: reported on stderr with the ``Asha:`` prefix (the
    CLI's established convention) and swallowed — it can never alter the
    worker result, verdict, or authorization, and a success-shaped file
    is never fabricated on failure.
    Path: <state>/<repo-id>/orchestrator/<task>/<worker>.baseline.json
    (atomic replace, last-writer-wins — same convention as
    worker-status.json).
    """
    try:
        checks: list | None = None
        if journal is not None:
            raw = journal.get("checks")
            checks = []
            if isinstance(raw, list):
                for entry in raw:
                    try:
                        record = dict(entry)
                        tail = record.get("output_tail")
                        if isinstance(tail, str) and tail:
                            # idempotent defense: run() already redacts and
                            # bounds this tail — byte-identical when pre-processed
                            record["output_tail"] = check_runner._redact_env(
                                tail[-check_runner.TAIL_CHARS:]
                            )
                        checks.append(record)
                    except Exception:
                        checks.append(
                            {"malformed": repr(entry)[:500]}
                        )
        parse: dict[str, Any] = {
            "status": "not_run",
            "identity_count": 0,
            "identities": [],
            "error": None,
        }
        if journal is not None:
            try:
                from asha.governance import delta

                identities = delta.extract_failures(checks or [])
                parse = {
                    "status": "completed",
                    "identity_count": len(identities),
                    "identities": [
                        f.to_dict()
                        for f in sorted(identities, key=lambda i: i.location)
                    ],
                    "error": None,
                }
            except Exception as exc:
                parse = {
                    "status": "error",
                    "identity_count": 0,
                    "identities": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
        payload = {
            "schema": "baseline-evidence/v1",
            "observed_at": evidence.now_iso(),
            "task_id": task_id,
            "worker_id": worker.get("id"),
            "worktree": str(Path(path).resolve()),
            "base_commit": base,
            "base_tree": base_tree,
            "execution": "ok" if journal is not None else "exception",
            "error": error,
            "checks": checks,
            "parse": parse,
        }
        run_dir = (
            common_paths.get_orchestrator_dir(Path(repo)) / _safe_id(task_id)
        )
        _atomic_json(
            run_dir
            / (_safe_id(str(worker.get("id") or "worker")) + ".baseline.json"),
            payload,
        )
    except Exception as exc:
        print(
            f"Asha: baseline evidence write failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def baseline_checks_recorded(
    worker: dict[str, Any],
    path: Path,
    base: str,
    base_tree: str,
    repo: Path,
    task_id: str,
) -> dict | None:
    """``baseline_checks()`` + best-effort diagnostic artifact, preserving
    the exact exception contract of BOTH original call sites.

    * A baseline exception is swallowed here (``except Exception`` →
      None) exactly as ``dag._run_one`` and ``run_worker_in_worktree``
      did, but recorded in the artifact as ``execution:"exception"``.
    * ``persist_baseline_evidence`` never raises, so persistence can
      never change the worker result, verdict, or authorization.
    * A missing artifact means unknown — never clean, never skipped.
    """
    journal: dict | None = None
    error: str | None = None
    try:
        journal = baseline_checks(
            worker, path, base, base_tree, repo, task_id
        )
    except Exception as exc:
        journal = None
        error = f"{type(exc).__name__}: {exc}"
    persist_baseline_evidence(
        journal,
        error=error,
        worker=worker,
        path=path,
        base=base,
        base_tree=base_tree,
        repo=repo,
        task_id=task_id,
    )
    return journal


def _baseline_worktree(repo: Path, base: str) -> Path:
    """Clean, isolated worktree checked out at `base` (baseline pass)."""
    root = Path(tempfile.mkdtemp(prefix='asha-baseline-'))
    wt = root / 'wt'
    _git(repo, "worktree", "add", "--detach", str(wt), base)
    return wt


def _drop_baseline_worktree(repo: Path, wt: Path) -> None:
    with _suppress(Exception):
        _git(repo, "worktree", "remove", "--force", str(wt))
    with _suppress(OSError):
        shutil.rmtree(wt.parent, ignore_errors=True)


def deferred_baseline_checks(
    worker: dict[str, Any],
    path: Path,
    base: str,
    base_tree: str | None,
    repo: Path,
    task_id: str,
    resolved: dict[str, Any],
    decision: Any,
    deps_root: Path | None = None,
) -> dict[str, Any] | None:
    """Baseline pass that reuses the CANDIDATE's authoritative decision.

    The candidate worktree already proved which files change and which
    scoping decision that implies; running the baseline with that same
    decision object (same mode, targeted tests, mypy targets, affected
    files) is what makes the two check inventories comparable. The
    checks themselves run on a fresh worktree at `base`, so the baseline
    still sees the pre-worker source.

    Never raises: a failed baseline is recorded as an error artifact and
    yields ``None`` (unknown, never an empty baseline).
    """
    journal: dict[str, Any] | None = None
    error: str | None = None
    wt: Path | None = None
    try:
        wt = _baseline_worktree(repo, base)
        head = _git(wt, "rev-parse", "HEAD").strip()
        tree = _git(wt, "rev-parse", "HEAD^{tree}").strip()
        if head != base:
            raise OrchestratorError(
                f"baseline worktree at {head!r}, expected {base!r}")
        if base_tree and tree != base_tree:
            raise OrchestratorError(
                f"baseline worktree tree {tree!r}, expected {base_tree!r}")
        affected = list(resolved.get("affected_files") or [])
        present = [item for item in affected if (wt / item).exists()]
        # A file the worker ADDS cannot be linted/type-checked at the base
        # revision: dropping it keeps the SAME checks (same inventory)
        # evaluated on the subset that exists at base, instead of
        # inventing findings or silently skipping a whole check.
        base_resolved = dict(resolved)
        base_resolved["affected_files"] = present
        targeted = list(getattr(decision, "targeted_tests", ()) or ())
        mypy_targets = list(getattr(decision, "mypy_targets", ()) or ())
        base_targets = [item for item in targeted if (wt / item).exists()]
        if targeted and not base_targets:
            # The whole test selection lives in files the worker created:
            # there is no meaningful baseline for it.
            raise OrchestratorError("baseline_selection_unavailable_at_base")
        base_mypy = [item for item in mypy_targets if (wt / item).exists()]
        if mypy_targets and not base_mypy:
            raise OrchestratorError("baseline_selection_unavailable_at_base")
        base_decision = decision
        if (base_targets != targeted or base_mypy != mypy_targets):
            base_decision = dataclasses.replace(
                decision, targeted_tests=tuple(base_targets),
                mypy_targets=tuple(base_mypy))
        checks = _run_checks(
            base_decision, wt, base_resolved,
            timeout_s=check_runner.BASELINE_TIMEOUT_S, deps_root=deps_root)
        journal = {
            "checks": checks,
            "resolved": resolved,
            "decision_mode": getattr(decision, "mode", None),
            "repo": str(Path(repo).resolve()),
            "base": base,
            "base_tree": base_tree,
            "task_id": task_id,
            "worktree_tree": tree,
        }
    except Exception as exc:
        journal, error = None, f"{type(exc).__name__}: {exc}"
    finally:
        if wt is not None:
            _drop_baseline_worktree(repo, wt)
    persist_baseline_evidence(
        journal, error=error, worker=worker, path=path, base=base,
        base_tree=base_tree or "", repo=repo, task_id=task_id)
    return journal


def _deps_root(path: Path, repo: Path) -> Path | None:
    """Dependency root for a check run: the original checkout when the
    checks execute in an isolated worktree, else None.

    A git worktree contains only TRACKED files, so a gitignored ``.venv``
    never exists there. Without this the resolver fell back to the
    interpreter that launched Asha, which imported an unrelated installed
    package and produced EXECUTION_FAILED on a green repository.
    """
    try:
        if Path(path).resolve() == Path(repo).resolve():
            return None
    except OSError:
        return None
    return Path(repo)


def _run_checks(decision, path: Path, resolved: dict,
                timeout_s: float = check_runner.CHECK_TIMEOUT_S,
                deps_root: Path | None = None) -> list[dict]:
    """Run the check matrix for a resolved scope (shared by baseline and
    current passes; identical machinery, identical environment)."""
    # Only add the kwarg when it is actually set: unbound runs keep the
    # pre-existing call shape (monkeypatched runners, external callers).
    extra: dict[str, Any] = (
        {'deps_root': deps_root} if deps_root is not None else {})
    if decision.eligible:
        return check_runner.run_scoped(
            path,
            changed_files=list(resolved["affected_files"]),
            targeted_tests=list(decision.targeted_tests),
            mypy_targets=list(decision.mypy_targets),
            **extra,
        )
    return check_runner.run(path, resolved, timeout_s=timeout_s, **extra)


def run_worker_in_worktree(
    repo: Path,
    worker: dict[str, Any],
    *,
    task_id: str,
    keep_worktrees: bool = False,
    fast_path: bool = False,
    fast_path_classification: str = "UNKNOWN",
    preserve_on_failure: bool = False,
    execution_id: str | None = None,
    execution_id_factory: Callable[[], str] | None = None,
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
    # one identity per worker-command invocation: the caller may own it
    # (MCP dispatch already minted one), otherwise it is minted here.
    if execution_id is None:
        execution_id = (execution_id_factory
                        or validation.new_execution_id)()
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
                result = execute_with_audit(worker, repo)
            except subprocess.TimeoutExpired:
                outcome = {"state": "FAILED",
                           "reason": "timeout_exceeded",
                           "evidence": None,
                           "execution_id": execution_id}
                states[wid] = outcome
                return _report(task_id, states, evidence_paths,
                               authoritative, dispatcher)
            rc, tail, audit = result
            outcome = collect_worker_evidence(
                worker, repo, rc, tail, task_id,
                common_paths.get_orchestrator_dir(repo) / _safe_id(task_id),
                base=base, base_tree=base_tree,
                deps_root=_deps_root(repo, repo),
                audit=audit)
        else:
            path = dispatcher.create(wid)
            created_path = path
            try:
                result = execute_with_audit(worker, path)
            except subprocess.TimeoutExpired:
                outcome = {"state": "FAILED",
                           "reason": "timeout_exceeded",
                           "evidence": None,
                           "execution_id": execution_id}
                states[wid] = outcome
                return _report(task_id, states, evidence_paths,
                               authoritative, dispatcher, created_path,
                               fast_path_classification=fast_path_classification)
            except Exception as exc:
                outcome = {"state": "FAILED",
                           "reason": f"execution_error:{type(exc).__name__}: "
                                     f"{exc}",
                           "evidence": None,
                           "execution_id": execution_id}
                states[wid] = outcome
                return _report(task_id, states, evidence_paths,
                               authoritative, dispatcher, created_path,
                               fast_path_classification=fast_path_classification)
            rc, tail, audit = result
            outcome = collect_worker_evidence(
                worker, path, rc, tail, task_id,
                common_paths.get_orchestrator_dir(repo) / _safe_id(task_id),
                base=base_commit, base_tree=base_tree,
                deps_root=_deps_root(path, repo),
                audit=audit,
                baseline_deferred=True,
                repo=repo)
        outcome["execution_id"] = execution_id
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
                            baseline_journal: dict[str, Any] | None = None,
                            baseline_deferred: bool = False,
                            repo: Path | None = None,
                            deps_root: Path | None = None,
                            audit: dict[str, Any] | None = None,
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
    acceptance = build_acceptance(worker, audit)
    if baseline_journal is not None and baseline_checks is None:
        baseline_checks = baseline_journal.get("checks")
    baseline_scope = ((baseline_journal or {}).get("resolved") or {}).get(
        "scope")
    comparison = build_comparison(
        base_tree=base_tree, candidate_tree=None,
        baseline_checks=baseline_checks, candidate_checks=None)
    if rc != 0:
        reason = "acceptance_failed" if acceptance_failed(acceptance) \
            else f"worker_exit_{rc}"
        return {"state": "FAILED", "reason": reason,
                "evidence": None, "exit_status": rc,
                "acceptance": acceptance,
                "comparison": comparison}
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
        declared_writes = [str(entry) for entry in (worker.get("writes") or [])]
        writes_observed = [entry for entry in declared_writes
                           if entry in observed]
        violations = [item for item in observed
                      if not any(covered(item, entry)
                                 for entry in declared)]
        if violations:
            return {"state": "INVALID_EVIDENCE",
                    "reason": "scope_violation:"
                    + ",".join(violations[:5]),
                    "evidence": None,
                    "comparison": build_comparison(
                        base_tree=effective_base_tree,
                        candidate_tree=target_tree,
                        baseline_checks=baseline_checks,
                        candidate_checks=None)}
        resolved = scope_resolver.resolve(
            path, base=effective_base, paths=observed)
        cls = classification
        if cls is None:
            by = by_id if by_id is not None else {wid: worker}
            cls = classify_independence(
                str(wid), resolved, set(uncertain), by,
                workers if workers is not None else [worker])
        decision = scoping_decision(path, resolved, cls)
        if baseline_deferred:
            if repo is None:
                raise OrchestratorError(
                    "baseline_deferred requires repo (baseline worktree)")
            # Same authoritative decision, clean tree at `base`: the only
            # way the two check inventories are genuinely comparable.
            baseline_journal = deferred_baseline_checks(
                worker, path, effective_base, effective_base_tree,
                Path(repo), task_id, resolved, decision, deps_root=deps_root)
            baseline_checks = (baseline_journal or {}).get("checks")
            baseline_scope = (
                (baseline_journal or {}).get("resolved") or {}).get("scope")
        checks = _run_checks(decision, path, resolved, deps_root=deps_root)
        failed = [entry["name"] for entry in checks
                  if entry["status"] == "failed"]
        validation_state, _validation_rule = validation.validation_status(
            checks)
        if checks and all(entry["status"] == "skipped"
                          for entry in checks):
            return {"state": "INVALID_EVIDENCE",
                    "reason": "no_verification_ran", "evidence": None,
                    "comparison": build_comparison(
                        base_tree=effective_base_tree,
                        candidate_tree=target_tree,
                        baseline_checks=baseline_checks,
                        candidate_checks=None)}
        from asha.governance import delta
        baseline_list = (list(baseline_checks)
                         if baseline_checks is not None else None)
        unparsed_baseline = (baseline_list is not None
                             and delta.unparsed_failures(baseline_list))
        cur_fails = delta.extract_failures(checks)
        base_fails = (delta.extract_failures(baseline_list)
                      if baseline_list is not None and not unparsed_baseline
                      else None)
        new_fails = delta.delta_failures(cur_fails, base_fails)
        verdict = delta.verdict_for(cur_fails, base_fails,
                                    has_failed_checks=bool(failed))
        comparison = build_comparison(
            base_tree=effective_base_tree, candidate_tree=target_tree,
            baseline_checks=baseline_list, candidate_checks=checks,
            baseline_scope=baseline_scope,
            candidate_scope=resolved["scope"], verdict=verdict)
        # Governed runs require attribution: a passing candidate cannot
        # authorize DONE when the baseline ran and its failures could not be
        # identified. Unknown is neither "no failures" nor "pre-existing
        # only". A baseline that was never collected (NOT_COMPARED with no
        # failure-identity detail) keeps the documented DONE contract --
        # there is nothing to attribute, and no failed check to misread.
        if (baseline_deferred
                and comparison["detail"] == "baseline_failures_unparsed"):
            return {
                # not DONE: an unattributable baseline authorizes nothing
                "state": "FAILED",
                "reason": "comparison_inconclusive:" + str(
                    comparison["detail"] or comparison["reason"]),
                "evidence": None,
                "exit_status": rc,
                "verification_status": "COMPARISON_INCONCLUSIVE",
                "verdict": verdict,
                "comparison": comparison,
                "current_failures": [
                    f.to_dict() for f in sorted(cur_fails,
                                                key=lambda i: i.location)],
            }
        # Failure always records the delta decision (fail-closed, even
        # without a baseline). Success records it only when a comparison
        # was actually performed, so an empty list never impersonates
        # data that was never collected.
        delta_ran = bool(failed) or comparison["performed"]
        blocked = False
        if failed:
            outcome: dict[str, Any] = {
                "state": "FAILED",
                "reason": "verification_failed:" + ",".join(failed),
                "evidence": None,
                "verdict": verdict,
                "comparison": comparison,
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
                # retained failure diagnostics (name/exit/note/tail)
                "details": _failed_check_details(checks),
            }
            delta_ran = True
            # A PRE_EXISTING_ONLY verdict only excuses these failures when a
            # candidate-vs-baseline comparison actually happened. Without
            # one the attribution is vacuous (identical trees make every
            # failure "pre-existing") so the worker is not DONE; it is
            # blocked and the sealed evidence records why.
            pre_existing_authorized = (verdict == "PRE_EXISTING_ONLY"
                                       and comparison["performed"])
            if verdict != "PRE_EXISTING_ONLY":
                return outcome
            blocked = not pre_existing_authorized
            # Pre-existing-only failures are not attributed to this worker
            # only when the comparison above was actually performed; the
            # fall-through below seals that decision either way (DONE when
            # authorised, FAILED when not).
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
            # Truthful application facts: an idempotent rewrite and a
            # forged claim look identical on disk, so this reports what
            # the tree shows instead of inventing a verdict.
            "applied": bool(observed),
            "writes_observed": writes_observed,
            "diff": diff,
            "exit_status": rc,
            "execution_ms": (audit or {}).get("duration_ms"),
            "validation_status": validation_state,
            "acceptance": acceptance,
            "comparison": comparison,
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
                "evidence": None, "comparison": comparison}
    done: dict[str, Any] = {
        "state": "FAILED" if blocked else "DONE",
        "reason": ("verification_failed:" + ",".join(failed)) if blocked
        else None,
        "exit_status": rc,
        "verification_status": (
            ("REGRESSION_DETECTED" if new_fails else
             "BASELINE_FAILED" if base_fails else "VERIFICATION_FAILED")
            if blocked else
            ("VERIFIED_SUCCESS" if not comparison["performed"]
             else "VERIFIED_NO_REGRESSION")),
        "evidence": str(evi_path),
        "observed": list(observed), "target_tree": target_tree,
        "applied": bool(observed), "writes_observed": writes_observed,
        "execution_ms": (audit or {}).get("duration_ms"),
        "validation_status": validation_state,
        "ready_to_push": (validation_state == "PASS" and not blocked
                          and bool(observed)),
        "acceptance": acceptance, "comparison": comparison}
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
