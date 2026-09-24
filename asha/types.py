"""Data contracts shared across the orchestrator package: execution
vocabulary (states), worker-evidence field requirements, timeout/tail
constants, the hook contract, and the fail-closed base exception."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATES = ('PENDING', 'DEFERRED', 'RUNNING', 'DONE', 'FAILED', 'BLOCKED',
          'INVALID_EVIDENCE')
WORKER_TIMEOUT_S = 600
TAIL_CHARS = 2000
# Worker-evidence identity fields required by the Phase-1 evidence model.
WORKER_EVIDENCE_FIELDS = (
    'task_id', 'worker_id', 'base_tree_sha', 'target_tree_sha',
    'declared_scope', 'observed_scope', 'read_set', 'write_set', 'diff',
    'checks', 'exit_status',
)

ExecuteHook = Callable[[dict[str, Any], Path], Any]
"""Hook contract: (worker, worktree) -> exit code, or (exit code, tail).
A hook raising subprocess.TimeoutExpired maps to FAILED/timeout_exceeded."""


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
