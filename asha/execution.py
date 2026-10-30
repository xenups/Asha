"""One process primitive: spawn, capture, terminate, and report honestly.

Every execution path -- worker command, validation check, matrix check,
baseline pass, in-place command, acceptance command -- runs its child through
``spawn_capture`` so that a timeout is described only by facts Asha can
verify.

The two questions this module keeps apart:

    outcome              what happened to the command
    process_termination  what Asha can PROVE about ending it

A kill request is not proof of termination, and a reaped direct child is not
proof that the child's own descendants exited. ``spawn_capture`` reaps the
direct child itself and reports ``confirmed`` only when that reap was
observed; process-tree termination is never claimed, and the detail string
says so.

Hard kill only (SIGKILL / ``taskkill /T /F``): no graceful phase exists in
this phase of Asha, so none is invented here.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .timeouts import Deadline, check_budget
from .types import (
    NOT_REQUIRED,
    TERMINATION_NOT_APPLICABLE,
    TERMINATION_NOT_REQUIRED,
)
from .worktree import kill_process_tree

#: How a command ended (the physical event, never a verdict).
EXECUTION_EXITED = 'exited'
EXECUTION_TIMED_OUT = 'timed_out'
EXECUTION_DEADLINE_EXCEEDED = 'deadline_exceeded'
EXECUTION_LAUNCH_FAILED = 'launch_failed'

EXECUTION_OUTCOMES = (EXECUTION_EXITED, EXECUTION_TIMED_OUT,
                      EXECUTION_DEADLINE_EXCEEDED, EXECUTION_LAUNCH_FAILED)

# Re-exported here so callers describe outcomes from one module; the
# vocabulary and its typed report live in ``types`` because the process-tree
# primitive (``worktree``) needs them too, and ``execution`` imports that.


@dataclass(frozen=True)
class ExecutionOutcome:
    """One child process, described by what actually happened.

    ``launched`` answers exactly one question: was a process created? It is
    never a verdict about success, completion, or the absence of a timeout.
    """

    argv: tuple[str, ...]
    cwd: str
    outcome: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timeout_s: float
    launched: bool
    process_termination: str
    termination_detail: str

    @property
    def timed_out(self) -> bool:
        return self.outcome in (EXECUTION_TIMED_OUT,
                                EXECUTION_DEADLINE_EXCEEDED)


class ExecutionTimeout(subprocess.TimeoutExpired):
    """A budget was exhausted after launch; carries the verified outcome.

    Subclasses ``TimeoutExpired`` so every existing handler keeps working
    while new handlers can read the facts (which budget, how it was ended).
    """

    def __init__(self, execution: ExecutionOutcome) -> None:
        super().__init__(cmd=list(execution.argv),
                         timeout=execution.timeout_s,
                         output=execution.stdout)
        self.execution = execution

    @property
    def reason(self) -> str:
        """Governance reason string for the exhausted budget."""
        if self.execution.outcome == EXECUTION_DEADLINE_EXCEEDED:
            return 'deadline_exceeded'
        return 'timeout_exceeded'


class ExecutionLaunchFailed(RuntimeError):
    """The child could not be created: no execution happened at all."""

    def __init__(self, execution: ExecutionOutcome) -> None:
        super().__init__(execution.termination_detail or 'launch failed')
        self.execution = execution


class DeadlineExhausted(RuntimeError):
    """The task budget was gone before the child was created."""

    def __init__(self, execution: ExecutionOutcome) -> None:
        super().__init__(execution.termination_detail
                         or 'task deadline exhausted before launch')
        self.execution = execution


def _text(value: object) -> str:
    """Captured stream as text (``communicate`` may hand back bytes)."""
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def spawn_capture(argv: list[str], cwd: Path, *,
                  timeout_s: float,
                  deadline: Deadline | None = None,
                  env: dict[str, str] | None = None,
                  encoding: str = 'utf-8',
                  errors: str = 'replace') -> ExecutionOutcome:
    """Run one child under a budget and report what can be verified.

    The effective budget is ``min(timeout_s, deadline remaining)``. A zero
    effective budget means the task deadline is already spent: nothing is
    launched and the returned outcome says ``deadline_exceeded`` with
    ``launched`` False -- there is no fake execution to describe.
    """
    budget = check_budget('timeout', timeout_s)
    effective = deadline.cap(budget) if deadline is not None else budget
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if effective <= 0.0:
        return ExecutionOutcome(
            argv=tuple(argv), cwd=str(cwd),
            outcome=EXECUTION_DEADLINE_EXCEEDED, exit_code=None,
            stdout='', stderr='', duration_ms=elapsed(),
            timeout_s=budget, launched=False,
            process_termination=TERMINATION_NOT_APPLICABLE,
            termination_detail='task deadline exhausted before launch')

    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding=encoding, errors=errors, env=env,
                                start_new_session=(os.name != 'nt'))
    except (OSError, subprocess.SubprocessError) as exc:
        return ExecutionOutcome(
            argv=tuple(argv), cwd=str(cwd),
            outcome=EXECUTION_LAUNCH_FAILED, exit_code=None,
            stdout='', stderr=f'{type(exc).__name__}: {exc}',
            duration_ms=elapsed(), timeout_s=budget, launched=False,
            process_termination=TERMINATION_NOT_APPLICABLE,
            termination_detail=f'{type(exc).__name__}: {exc}')

    try:
        out, err = proc.communicate(timeout=effective)
    except subprocess.TimeoutExpired as exc:
        report = kill_process_tree(proc)
        partial_out = _text(exc.stdout)
        partial_err = _text(exc.stderr)
        # The task budget, not the operation budget, is what ran out when the
        # deadline capped the operation: the two causes get different names.
        outcome = (EXECUTION_DEADLINE_EXCEEDED
                   if (deadline is not None and effective < budget)
                   else EXECUTION_TIMED_OUT)
        return ExecutionOutcome(
            argv=tuple(argv), cwd=str(cwd), outcome=outcome, exit_code=None,
            stdout=partial_out, stderr=partial_err, duration_ms=elapsed(),
            timeout_s=budget, launched=True,
            process_termination=report.process_termination,
            termination_detail=report.detail)
    except (OSError, subprocess.SubprocessError) as exc:
        report = kill_process_tree(proc)
        return ExecutionOutcome(
            argv=tuple(argv), cwd=str(cwd),
            outcome=EXECUTION_LAUNCH_FAILED, exit_code=None,
            stdout='', stderr=f'{type(exc).__name__}: {exc}',
            duration_ms=elapsed(), timeout_s=budget, launched=True,
            process_termination=report.process_termination,
            termination_detail=report.detail)

    return ExecutionOutcome(
        argv=tuple(argv), cwd=str(cwd), outcome=EXECUTION_EXITED,
        exit_code=proc.returncode, stdout=_text(out), stderr=_text(err),
        duration_ms=elapsed(), timeout_s=budget, launched=True,
        process_termination=TERMINATION_NOT_REQUIRED,
        termination_detail=NOT_REQUIRED.detail)
