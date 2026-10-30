"""Data contracts shared across the orchestrator package: execution
vocabulary (states), worker-evidence field requirements, timeout/tail
constants, the hook contract, and the fail-closed base exception."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .timeouts import DEFAULT_TIMEOUTS

STATES = ('PENDING', 'DEFERRED', 'RUNNING', 'DONE', 'FAILED', 'BLOCKED',
          'INVALID_EVIDENCE')

# What Asha can PROVE about ending the process it created. A kill request is
# not proof of termination; a reaped direct child is not proof about its
# descendants.
TERMINATION_NOT_REQUIRED = 'not_required'
TERMINATION_CONFIRMED = 'confirmed'
TERMINATION_UNCONFIRMED = 'unconfirmed'
TERMINATION_NOT_APPLICABLE = 'not_applicable'
TERMINATION_STATES = (TERMINATION_NOT_REQUIRED, TERMINATION_CONFIRMED,
                      TERMINATION_UNCONFIRMED, TERMINATION_NOT_APPLICABLE)


@dataclass(frozen=True)
class ProcessTermination:
    """Result of one termination attempt, as observed (not as requested)."""

    process_termination: str
    detail: str

    def __post_init__(self) -> None:
        if self.process_termination not in TERMINATION_STATES:
            raise ValueError('unknown process_termination '
                             f'{self.process_termination!r}')


NOT_APPLICABLE = ProcessTermination(TERMINATION_NOT_APPLICABLE,
                                   'no process was created')
NOT_REQUIRED = ProcessTermination(TERMINATION_NOT_REQUIRED,
                                 'process completed on its own')
#: Compatibility alias: the budget itself lives in asha.timeouts.
WORKER_TIMEOUT_S = DEFAULT_TIMEOUTS.worker
TAIL_CHARS = 2000
# Worker-evidence identity fields required by the Phase-1 evidence model.
WORKER_EVIDENCE_FIELDS = (
    'task_id', 'worker_id', 'base_tree_sha', 'target_tree_sha',
    'declared_scope', 'observed_scope', 'read_set', 'write_set', 'diff',
    'checks', 'exit_status',
)

ExecuteHook = Callable[[dict[str, Any], Path], Any]
"""Hook contract: (worker, worktree) -> exit code, or (exit code, tail).
A hook raising subprocess.TimeoutExpired maps to FAILED/timeout_exceeded.
A hook cannot honour a task deadline, so run_workers_dag refuses a deadline
combined with an injected hook instead of claiming enforcement."""


class OrchestratorError(Exception):
    """Structural / governance violation -- fail-closed, never degraded."""


@dataclass(frozen=True)
class RunnerResult:
    """Phase 2 return contract of BaseAgentRunner.execute(): full
    stdout/stderr for audit logs, wall time, and runner metadata
    (worker_id, runner name, cwd, argv0, verify_* facts)."""
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    audit_metadata: dict[str, Any] = field(default_factory=dict)


#: Optional worker-spec fields served by the runner dispatch (Phase 2).
RUNNER_SPEC_OPTIONAL_FIELDS: tuple[str, ...] = (
    'prompt', 'agent', 'verify_command', 'runner')
