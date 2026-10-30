"""Stage 4 contract: an optional task deadline, off by default.

One deadline per task, monotonic, never reset. The effective budget of any
command is min(operation budget, remaining task budget); whichever runs out is
the reason that gets reported. With no deadline supplied nothing changes.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from asha import cli, execution
from asha.execution import (
    EXECUTION_DEADLINE_EXCEEDED,
    EXECUTION_TIMED_OUT,
    spawn_capture,
)
from asha.governance import dag
from asha.timeouts import Deadline, check_budget

PY = sys.executable
SLEEPER = [PY, '-c', 'import time; time.sleep(30)']


def _git(repo: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 'a@example.invalid')
    _git(repo, 'config', 'user.name', 'A')
    (repo / 'probe.py').write_text('x = 1\n', encoding='utf-8')
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_leaf.py').write_text(
        'def test_leaf():\n    assert True\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'base')
    return repo


def _worker(wid: str, cmd: str = 'printf "y = 2\\n" > probe.py',
            deps: list[str] | None = None) -> dict:
    return {'id': wid, 'deps': list(deps or []),
            'declared_scope': ['probe.py'], 'reads': [],
            'writes': ['probe.py'], 'cmd': ['sh', '-c', cmd]}


# --------------------------------------------------------------------------
# the cap: whichever budget runs out first decides the reason
# --------------------------------------------------------------------------

def test_operation_budget_shorter_than_deadline_stays_a_timeout(
        tmp_path: Path) -> None:
    deadline = Deadline(30)
    result = spawn_capture(SLEEPER, tmp_path, timeout_s=1, deadline=deadline)
    assert result.outcome == EXECUTION_TIMED_OUT
    assert result.timeout_s == 1.0
    assert deadline.expired() is False       # the task budget was not the cap


def test_deadline_shorter_than_operation_budget_is_deadline_exceeded(
        tmp_path: Path) -> None:
    result = spawn_capture(SLEEPER, tmp_path, timeout_s=30,
                           deadline=Deadline(1))
    assert result.outcome == EXECUTION_DEADLINE_EXCEEDED
    assert result.timeout_s == 30.0          # the operation budget is kept
    assert result.duration_ms < 5000         # the deadline decided


def test_deadline_expired_before_launch_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / 'ran.txt'
    deadline = Deadline(0.01)
    time.sleep(0.05)
    result = spawn_capture(
        [PY, '-c', f'open({str(marker)!r}, "w").close()'], tmp_path,
        timeout_s=30, deadline=deadline)
    assert result.outcome == EXECUTION_DEADLINE_EXCEEDED
    assert result.launched is False
    assert not marker.exists()


def test_one_deadline_is_shared_across_sequential_commands(
        tmp_path: Path) -> None:
    """The second command gets what the first one left -- not a fresh budget."""
    deadline = Deadline(1.2)
    first = spawn_capture([PY, '-c', 'import time; time.sleep(0.8)'],
                          tmp_path, timeout_s=30, deadline=deadline)
    second = spawn_capture([PY, '-c', 'import time; time.sleep(5)'],
                           tmp_path, timeout_s=30, deadline=deadline)
    assert first.outcome == execution.EXECUTION_EXITED
    assert second.outcome == EXECUTION_DEADLINE_EXCEEDED
    assert second.duration_ms < 1200         # capped by what was left


def test_concurrent_workers_share_the_same_task_deadline(
        tmp_path: Path) -> None:
    deadline = Deadline(0.6)
    started = time.monotonic()
    results: list[execution.ExecutionOutcome] = []
    lock = threading.Lock()

    def run() -> None:
        outcome = spawn_capture(SLEEPER, tmp_path, timeout_s=30,
                                deadline=deadline)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started
    assert len(results) == 2
    assert all(r.outcome == EXECUTION_DEADLINE_EXCEEDED for r in results)
    assert elapsed < 5                       # neither slept the full 30s


def test_no_deadline_keeps_the_unlimited_default(tmp_path: Path) -> None:
    result = spawn_capture([PY, '-c', 'print(1)'], tmp_path, timeout_s=30)
    assert result.outcome == execution.EXECUTION_EXITED
    assert result.exit_code == 0


# --------------------------------------------------------------------------
# the gate: no new work starts once the task budget is spent
# --------------------------------------------------------------------------

def test_expired_deadline_blocks_dispatch_without_executing(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    calls: list[str] = []

    def hook(worker, worktree, *, deadline=None):  # type: ignore[no-untyped-def]
        calls.append(str(worker['id']))
        return 0, ''

    deadline = Deadline(0.01)
    time.sleep(0.05)
    report = dag.run_workers_dag(repo, [_worker('w1')], task_id='task-dl',
                                 execute_hook=hook, deadline=deadline)
    assert calls == []                       # nothing was launched
    entry = report['states']['w1']
    assert entry['state'] == 'BLOCKED'
    assert entry['reason'] == 'deadline_exceeded'
    assert 'execution_id' not in entry       # no execution to identify


def test_a_spent_budget_outranks_a_dependency_reason(tmp_path: Path) -> None:
    """A dependent worker that never started blames the budget, not its peer.

    End-to-end scenario 11 found this: once w1 hit the task deadline, w2 was
    reported BLOCKED / dependency_not_finished -- true about the dependency
    and misleading about the cause.
    """
    repo = _repo(tmp_path)
    report = dag.run_workers_dag(
        repo, [_worker('w1', 'sleep 4; printf "y = 2\\n" > probe.py'),
               _worker('w2', deps=['w1'])],
        task_id='task-between', deadline=Deadline(3))
    w1, w2 = report['states']['w1'], report['states']['w2']
    assert w1['state'] == 'FAILED'
    assert w1['reason'] == 'deadline_exceeded'
    assert w2['state'] == 'BLOCKED'
    assert w2['reason'] == 'deadline_exceeded'
    assert 'execution_id' not in w2


def test_without_a_deadline_work_still_runs(tmp_path: Path) -> None:
    """The baseline: the same spec, no deadline, dispatches normally."""
    repo = _repo(tmp_path)
    calls: list[str] = []

    def hook(worker, worktree, *, deadline=None):  # type: ignore[no-untyped-def]
        calls.append(str(worker['id']))
        return 0, ''

    report = dag.run_workers_dag(repo, [_worker('w1')], task_id='task-nodl',
                                 execute_hook=hook)
    assert calls == ['w1']
    assert report['states']['w1']['state'] != 'BLOCKED'


# --------------------------------------------------------------------------
# plumbing: the budget is validated where it enters, CLI included
# --------------------------------------------------------------------------

def test_deadline_rejects_an_invalid_budget() -> None:
    # wrong type is refused by type, unusable number by value
    for wrong_type in (True, '30', None):
        with pytest.raises(TypeError):
            Deadline(wrong_type)  # type: ignore[arg-type]
    for unusable in (0, -1, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            Deadline(unusable)
    assert check_budget('in_place', 30) == 30.0


def test_cli_exposes_an_optional_deadline(
        capsys: pytest.CaptureFixture[str]) -> None:
    """--deadline-seconds exists, is optional, and defaults to no budget."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(['run', '--help'])
    assert exit_info.value.code == 0
    usage = capsys.readouterr().out
    assert '--deadline-seconds' in usage
    assert 'no task deadline' in usage
