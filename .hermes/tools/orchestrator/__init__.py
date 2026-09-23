"""Asha Orchestrator -- Phase 1 governed worker scheduling (stdlib only).

Dependency readiness and conflict safety are separate mechanisms:

    graphlib.TopologicalSorter -> "are declared dependencies complete?"
    ConflictManager            -> "is it safe to run concurrently?"

TopologicalSorter never answers the second question. Dispatch invariant
(fail-closed, UNKNOWN != SAFE):

    dependency-ready AND scope-safe (known declared scope)
    AND conflict-safe (known read/write sets proven disjoint vs running)
        -> dispatch
    unknown read/write set or unknown/invalid declared scope
        -> defer / block; never optimistically dispatched on
           post-execution evidence that cannot exist beforehand.

After execution a worker reaches done() only with evidence proving:

    exit status 0 AND observed scope within declared scope AND
    verification passed AND Evidence.target_tree_sha == the git tree
    actually verified (re-bound against live git before completion).

Reuses the existing Asha subsystems -- no second evidence system, no
second scope system, no ledger of its own:

    evidence.seal / compute_digest        worker evidence integrity (the
                                          canonical §7 digest machinery;
                                          §7 identity fields ride along
                                          and are covered by the digest)
    scope_resolver.resolve/changed_files  observed scope, S0-S4, checks
    check_runner.run                      isolated verification capture
    .jspace/control.py                    the only ship authorization
                                          (existing check --stage ship)

Merge law: PASS(A) + PASS(B) != PASS(A U B). Worker evidence always seals
authorized_to_ship=false; integration verification belongs to the existing
ship gate run against the integration tree. Full automatic merge
orchestration is an explicit Phase-1 non-goal -- the boundary is this
paragraph, not a pretend merge.

Worktree isolation: git worktrees share object storage, but each has its
own working directory and consumes disk (not zero-disk). Lifecycle rules:
create at dispatch -> execute -> commit working state so a tree identity
exists -> collect evidence -> remove unless --keep-worktrees; removal and
prune errors are reported in the run report, never silenced.

States (execution vocabulary, deliberately NOT ledger keys):
    PENDING, DEFERRED, RUNNING, DONE, FAILED, BLOCKED, INVALID_EVIDENCE

CLI:
    python .hermes/tools/orchestrator.py --root R run --spec spec.json
Exit 0 only when every worker is DONE; every other outcome exits 1.
"""

from .conflict import ConflictManager, _intersection, covered, overlap, scope_status
from .integrator import IntegrationResult, TreeIntegrator
from .runner import (
                        AntigravityRunner,
                        BaseAgentRunner,
                        CommandRunner,
                        dispatch_runner,
)
from .scheduler import (
                        GovernedScheduler,
                        default_execute,
                        main,
                        validate_workers,
                        verify_worker_evidence,
)
from .types import (
                        STATES,
                        TAIL_CHARS,
                        WORKER_EVIDENCE_FIELDS,
                        WORKER_TIMEOUT_S,
                        ExecuteHook,
                        OrchestratorError,
                        RunnerResult,
)
from .worktree import WorktreeDispatcher

__all__ = [
                        "STATES",
                        "TAIL_CHARS",
                        "WORKER_EVIDENCE_FIELDS",
                        "WORKER_TIMEOUT_S",
                        "AntigravityRunner",
                        "BaseAgentRunner",
                        "CommandRunner",
                        "ConflictManager",
                        "ExecuteHook",
                        "GovernedScheduler",
                        "IntegrationResult",
                        "OrchestratorError",
                        "RunnerResult",
                        "TreeIntegrator",
                        "WorktreeDispatcher",
                        "_intersection",
                        "covered",
                        "default_execute",
                        "dispatch_runner",
                        "main",
                        "overlap",
                        "scope_status",
                        "validate_workers",
                        "verify_worker_evidence",
]
