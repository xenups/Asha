"""Stage 3 contract: one semantic model for every execution path.

The physical event decides the description, on every path:

    ran, exit 0          launched=True   exited            not_required
    ran, exit != 0       launched=True   exited            not_required
    ran, killed at budget launched=True  timed_out         confirmed/unconfirmed
    could not be created launched=False  launch_failed     not_applicable
    never attempted      launched=False  (no execution)    not_applicable

``launched`` answers only "was a process created"; ``exit_code`` is the real
code or None (never a synthetic -1); ``process_termination`` is what was
verified, not what was requested.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from asha import check_runner, execution, validation
from asha.execution import (
    EXECUTION_DEADLINE_EXCEEDED,
    EXECUTION_EXITED,
    EXECUTION_LAUNCH_FAILED,
    EXECUTION_TIMED_OUT,
    ExecutionOutcome,
    spawn_capture,
)
from asha.timeouts import Deadline
from asha.types import (
    TERMINATION_CONFIRMED,
    TERMINATION_NOT_APPLICABLE,
    TERMINATION_NOT_REQUIRED,
    TERMINATION_UNCONFIRMED,
)

PY = sys.executable


def _outcome(outcome: str, *, launched: bool, exit_code: int | None,
             termination: str, timeout_s: float = 5.0,
             stdout: str = '', stderr: str = '') -> ExecutionOutcome:
    return ExecutionOutcome(
        argv=('-c', 'x'), cwd='.', outcome=outcome, exit_code=exit_code,
        stdout=stdout, stderr=stderr, duration_ms=7, timeout_s=timeout_s,
        launched=launched, process_termination=termination,
        termination_detail='synthetic')


# --------------------------------------------------------------------------
# the primitive
# --------------------------------------------------------------------------

def test_exit_zero_is_exited_and_needs_no_termination(tmp_path: Path) -> None:
    result = spawn_capture([PY, '-c', 'print("ok")'], tmp_path, timeout_s=30)
    assert result.outcome == EXECUTION_EXITED
    assert result.exit_code == 0
    assert result.launched is True
    assert result.process_termination == TERMINATION_NOT_REQUIRED
    assert 'ok' in result.stdout


def test_nonzero_exit_keeps_the_real_code(tmp_path: Path) -> None:
    result = spawn_capture([PY, '-c', 'raise SystemExit(3)'], tmp_path,
                           timeout_s=30)
    assert result.outcome == EXECUTION_EXITED
    assert result.exit_code == 3
    assert result.launched is True


def test_timeout_after_launch_is_reported_as_launched_and_confirmed(
        tmp_path: Path) -> None:
    result = spawn_capture([PY, '-c', 'import time; time.sleep(30)'],
                           tmp_path, timeout_s=1)
    assert result.outcome == EXECUTION_TIMED_OUT
    assert result.launched is True                 # a process DID exist
    assert result.exit_code is None                # no synthetic -1
    assert result.process_termination == TERMINATION_CONFIRMED
    assert 'descendants not verified' in result.termination_detail
    assert result.timeout_s == 1.0
    assert 900 <= result.duration_ms <= 5000


def test_launch_failure_is_not_an_execution(tmp_path: Path) -> None:
    result = spawn_capture(['asha-no-such-binary'], tmp_path, timeout_s=5)
    assert result.outcome == EXECUTION_LAUNCH_FAILED
    assert result.launched is False
    assert result.exit_code is None
    assert result.process_termination == TERMINATION_NOT_APPLICABLE


def test_unconfirmed_termination_is_reported_not_swallowed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A kill we cannot verify must not look like a verified kill."""
    real_popen = subprocess.Popen

    class _Unreapable(subprocess.Popen):  # type: ignore[misc]
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(cmd='x', timeout=timeout or 0)

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        return _Unreapable(argv, **kwargs)

    assert real_popen is subprocess.Popen
    monkeypatch.setattr(execution.subprocess, 'Popen', fake_popen)
    result = spawn_capture([PY, '-c', 'import time; time.sleep(30)'],
                           tmp_path, timeout_s=1)
    assert result.outcome == EXECUTION_TIMED_OUT
    assert result.process_termination == TERMINATION_UNCONFIRMED
    assert 'not observed to exit' in result.termination_detail


# --------------------------------------------------------------------------
# validation path
# --------------------------------------------------------------------------

def test_validation_timeout_is_fail_not_blocked(tmp_path: Path) -> None:
    """A check that ran and was killed is FAIL; BLOCKED means it never ran."""
    entries = validation.run_validation_commands(
        [[PY, '-c', 'import time; time.sleep(30)']], tmp_path,
        timeout_s=1)
    entry = entries[0]
    assert entry['status'] == 'failed'
    assert entry['launched'] is True
    assert entry['exit_code'] is None
    assert entry['termination'] == EXECUTION_TIMED_OUT
    assert entry['process_termination'] == TERMINATION_CONFIRMED
    assert entry['timeout_s'] == 1.0
    assert entry['duration_ms'] >= 900
    assert entry['note'].startswith('timed out after 1s')
    assert entry['execution_id'].startswith('exec-')
    status, _rule = validation.validation_status(entries)
    assert status == validation.VALIDATION_FAIL


def test_validation_launch_failure_stays_blocked(tmp_path: Path) -> None:
    entries = validation.run_validation_commands(
        [['asha-no-such-binary']], tmp_path, timeout_s=5)
    entry = entries[0]
    assert entry['launched'] is False
    assert 'execution_id' not in entry       # nothing executed, no fake id
    assert entry['exit_code'] is None
    assert entry['termination'] == EXECUTION_LAUNCH_FAILED
    assert entry['process_termination'] == TERMINATION_NOT_APPLICABLE
    status, rule = validation.validation_status(entries)
    assert status == validation.VALIDATION_BLOCKED
    assert 'could not be executed' in rule


def test_validation_failure_after_launch_is_fail_not_blocked(
        tmp_path: Path) -> None:
    entries = validation.run_validation_commands(
        [[PY, '-c', 'raise SystemExit(4)']], tmp_path, timeout_s=30)
    assert entries[0]['launched'] is True
    assert entries[0]['exit_code'] == 4
    assert validation.validation_status(entries)[0] == validation.VALIDATION_FAIL


# --------------------------------------------------------------------------
# matrix + acceptance mapping (synthetic outcomes; E2E covers real runs)
# --------------------------------------------------------------------------

def test_check_runner_maps_a_timeout_without_a_synthetic_exit_code(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_runner, 'spawn_capture',
        lambda *a, **kw: _outcome(EXECUTION_TIMED_OUT, launched=True,
                                  exit_code=None,
                                  termination=TERMINATION_CONFIRMED,
                                  timeout_s=900, stdout='partial'))
    entries = check_runner.run(tmp_path / 'repo',
                               {'checks': ['pytest'], 'affected_files': []},
                               timeout_s=900)
    entry = entries[0]
    assert entry['status'] == 'failed'
    assert entry['exit_code'] is None            # was a synthetic -1
    assert entry['launched'] is True
    assert entry['termination'] == EXECUTION_TIMED_OUT
    assert entry['note'] == 'timed out after 900s'
    assert 'partial' in entry['output_tail']
    assert 'process_termination=confirmed' in entry['output_tail']


def test_check_runner_launch_failure_is_not_launched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_runner, 'spawn_capture',
        lambda *a, **kw: _outcome(EXECUTION_LAUNCH_FAILED, launched=False,
                                  exit_code=None,
                                  termination=TERMINATION_NOT_APPLICABLE))
    entries = check_runner.run(tmp_path / 'repo',
                               {'checks': ['ruff'], 'affected_files': []})
    entry = entries[0]
    assert entry['status'] == 'failed'
    assert entry['launched'] is False
    assert entry['exit_code'] is None
    assert entry['note'].startswith('launch failed')


def test_deadline_exhausted_before_launch_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / 'ran.txt'
    deadline = Deadline(0.01)
    time.sleep(0.05)
    result = spawn_capture([PY, '-c', f'open({str(marker)!r}, "w")'],
                           tmp_path, timeout_s=30, deadline=deadline)
    assert result.outcome == EXECUTION_DEADLINE_EXCEEDED
    assert result.launched is False
    assert result.process_termination == TERMINATION_NOT_APPLICABLE
    assert not marker.exists()


def test_task_deadline_caps_a_running_command(tmp_path: Path) -> None:
    result = spawn_capture([PY, '-c', 'import time; time.sleep(30)'],
                           tmp_path, timeout_s=30, deadline=Deadline(1.0))
    assert result.outcome == EXECUTION_DEADLINE_EXCEEDED
    assert result.launched is True
    assert result.timeout_s == 30.0          # the operation budget is kept
    assert result.duration_ms < 5000         # but the deadline decided
