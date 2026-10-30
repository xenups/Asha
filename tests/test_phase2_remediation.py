"""Phase 2 remediation regressions: F-05 / F-05b / F-01 / F-02 / F-06 / F-04.

Every test names the finding it pins. Budgets are short (≤1s) and the slow
child is a real subprocess, so nothing waits on the 900/1800s policy values.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from asha import check_runner, execution, runner, timeouts, validation
from asha.governance import dag, worker_execution

SLEEPER = ['sh', '-c', 'sleep 5']


def _repo(tmp_path: Path, *, slow_test: bool = False) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    for argv in (['git', 'init', '-q', '-b', 'main'],
                 ['git', 'config', 'user.email', 'a@b.c'],
                 ['git', 'config', 'user.name', 'A']):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    (repo / 'probe.py').write_text('x = 1\n', encoding='utf-8')
    (repo / 'tests').mkdir()
    body = ('import time\n\n\ndef test_leaf():\n'
            + ('    time.sleep(4)\n' if slow_test else '    assert True\n'))
    (repo / 'tests' / 'test_leaf.py').write_text(body, encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True,
                   capture_output=True)
    return repo


def _worker(cmd: list[str], **extra) -> dict:
    return {'id': 'w1', 'deps': [], 'declared_scope': ['probe.py'],
            'reads': [], 'writes': ['probe.py'], 'cmd': cmd, **extra}


# ---------------------------------------------------------------- F-05
def test_scoped_check_is_capped_by_the_task_deadline(tmp_path: Path) -> None:
    """A 4s pytest run under a 0.4s task budget is killed and fails."""
    repo = _repo(tmp_path, slow_test=True)
    started = time.monotonic()
    checks = check_runner.run_scoped(
        repo, changed_files=['probe.py'],
        targeted_tests=['tests/test_leaf.py'], mypy_targets=[],
        timeout=30, deadline=timeouts.Deadline(0.4))
    wall = time.monotonic() - started
    pytest_entry = next(c for c in checks if c['name'] == 'pytest')
    assert pytest_entry['status'] == 'failed'
    assert pytest_entry['termination'] == execution.EXECUTION_DEADLINE_EXCEEDED
    assert pytest_entry['launched'] is True
    assert wall < 4.0, 'the deadline must cap the check, not the 30s budget'


def test_expired_deadline_launches_no_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path, slow_test=True)
    deadline = timeouts.Deadline(0.01)
    time.sleep(0.05)
    checks = check_runner.run_scoped(
        repo, changed_files=['probe.py'],
        targeted_tests=['tests/test_leaf.py'], mypy_targets=[],
        timeout=30, deadline=deadline)
    pytest_entry = next(c for c in checks if c['name'] == 'pytest')
    assert pytest_entry['launched'] is False
    assert pytest_entry['termination'] == execution.EXECUTION_DEADLINE_EXCEEDED
    assert 'deadline exceeded' in pytest_entry['note']
    assert not (repo / '.pytest_cache').exists(), 'nothing may be launched'


def test_full_check_matrix_respects_the_deadline(tmp_path: Path) -> None:
    repo = _repo(tmp_path, slow_test=True)
    started = time.monotonic()
    checks = check_runner.run(
        repo, {'checks': ['pytest'], 'affected_files': ['tests/test_leaf.py']},
        timeout_s=30, deadline=timeouts.Deadline(0.4))
    wall = time.monotonic() - started
    assert checks and all(c['status'] == 'failed' for c in checks)
    assert wall < 4.0


def test_validation_commands_respect_the_deadline(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    started = time.monotonic()
    entries = validation.run_validation_commands(
        [SLEEPER], repo, deadline=timeouts.Deadline(0.4))
    wall = time.monotonic() - started
    assert entries[0]['status'] == 'failed'
    assert entries[0]['termination'] == execution.EXECUTION_DEADLINE_EXCEEDED
    assert entries[0]['launched'] is True
    assert wall < 4.0


def test_validation_refuses_to_launch_after_the_deadline(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    deadline = timeouts.Deadline(0.01)
    time.sleep(0.05)
    entries = validation.run_validation_commands(
        [[sys.executable, '-c',
          'from pathlib import Path; Path("ran.txt").write_text("y")']],
        repo, deadline=deadline)
    assert entries[0]['launched'] is False
    assert entries[0]['termination'] == execution.EXECUTION_DEADLINE_EXCEEDED
    assert 'execution_id' not in entries[0]
    assert not (repo / 'ran.txt').exists()


def test_operation_timeout_wins_when_it_is_shorter(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    entries = validation.run_validation_commands(
        [SLEEPER], repo, timeout_s=0.3, deadline=timeouts.Deadline(30))
    assert entries[0]['termination'] == execution.EXECUTION_TIMED_OUT
    assert 'timed out after' in entries[0]['note']


def test_deadline_wins_when_it_is_shorter(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    entries = validation.run_validation_commands(
        [SLEEPER], repo, timeout_s=30, deadline=timeouts.Deadline(0.3))
    assert entries[0]['termination'] == execution.EXECUTION_DEADLINE_EXCEEDED


# ---------------------------------------------------------------- F-05b
def test_eligible_baseline_keeps_its_operation_budget(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}

    def fake_run_scoped(root, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(check_runner, 'run_scoped', fake_run_scoped)
    decision = SimpleNamespace(eligible=True, targeted_tests=(),
                               mypy_targets=())
    worker_execution._run_checks(
        decision, tmp_path, {'affected_files': []},
        timeout_s=check_runner.BASELINE_TIMEOUT_S)
    assert seen['timeout'] == 1800.0, 'baseline budget must survive'
    assert seen['deadline'] is None


def test_eligible_baseline_is_capped_by_the_deadline(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}

    def fake_run_scoped(root, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(check_runner, 'run_scoped', fake_run_scoped)
    deadline = timeouts.Deadline(5)
    decision = SimpleNamespace(eligible=True, targeted_tests=(),
                               mypy_targets=())
    worker_execution._run_checks(
        decision, tmp_path, {'affected_files': []},
        timeout_s=check_runner.BASELINE_TIMEOUT_S, deadline=deadline)
    assert seen['deadline'] is deadline
    assert seen['timeout'] == 1800.0


def test_collect_evidence_threads_one_deadline_object(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    deadline = timeouts.Deadline(60)
    seen: dict = {}

    def fake_checks(decision, path, resolved, timeout_s=None, deps_root=None,
                    deadline=None):
        seen['checks'] = deadline
        return [{'name': 'pytest', 'scope': 'package', 'status': 'failed',
                 'launched': True, 'exit_code': None,
                 'termination': execution.EXECUTION_DEADLINE_EXCEEDED,
                 'note': 'deadline exceeded', 'output_tail': '',
                 'duration_ms': 1}]

    def fake_baseline(*args, **kwargs):
        seen['baseline'] = kwargs.get('deadline')
        return {'checks': [], 'resolved': {'scope': 'x'}}

    monkeypatch.setattr(worker_execution, '_run_checks', fake_checks)
    monkeypatch.setattr(worker_execution, 'deferred_baseline_checks',
                        fake_baseline)
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=repo,
                          check=True, capture_output=True, text=True
                          ).stdout.strip()
    state = worker_execution.collect_worker_evidence(
        _worker(['true']), repo, 0, '', 'tid',
        tmp_path / 'orchestrator', base=base, base_tree=tree,
        repo=repo, baseline_deferred=True, deadline=deadline)
    assert seen['checks'] is deadline
    assert seen['baseline'] is deadline, 'never a fresh deadline per stage'
    assert state['state'] == 'FAILED'


def test_task_cannot_be_ok_when_checks_were_skipped(
        tmp_path: Path) -> None:
    """Worker finishes, the deadline dies before the checks: not ok."""
    repo = _repo(tmp_path, slow_test=True)
    report = dag.run_workers_dag(
        repo, [_worker(['sh', '-c', 'sleep 0.6'])], task_id='t-skip',
        deadline=timeouts.Deadline(0.8))
    entry = report['states']['w1']
    assert entry['state'] == 'FAILED', entry
    assert entry['reason'] == 'deadline_exceeded', entry
    assert entry['verification_status'] == 'DEADLINE_EXCEEDED', entry
    assert entry.get('evidence') is None, entry
    assert report['status'] == 'failed', report


# ---------------------------------------------------------------- F-01
def test_deadline_with_an_injected_hook_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    calls: list = []

    def hook(worker: dict, path: Path) -> tuple[int, str]:
        calls.append(worker)
        return 0, ''

    with pytest.raises(Exception) as caught:
        dag.run_workers_dag(repo, [_worker(['true'])], task_id='t-hook',
                            deadline=timeouts.Deadline(5),
                            execute_hook=hook)
    assert 'execute_hook' in str(caught.value)
    assert calls == [], 'no worker may run before the refusal'


def test_hook_still_works_without_a_deadline(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = dag.run_workers_dag(
        repo, [_worker(['true'])], task_id='t-hook-ok',
        execute_hook=lambda worker, path: (0, ''))
    assert report['status'] == 'ok'


# ---------------------------------------------------------------- F-02
_REAL_POPEN = subprocess.Popen


class _CollectFails:
    """A real child that starts, then fails while being read."""

    def __init__(self, argv, **kwargs):
        self._proc = _REAL_POPEN(argv, **kwargs)
        self.pid = self._proc.pid

    def communicate(self, timeout=None):
        time.sleep(0.05)
        raise OSError(5, 'simulated collection failure')

    def kill(self):
        return self._proc.kill()

    def wait(self, timeout=None):
        return self._proc.wait(timeout=timeout)

    @property
    def returncode(self):
        return self._proc.returncode


def test_launch_failure_and_collection_failure_are_distinct(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    never = execution.spawn_capture(['/nonexistent/asha-binary'], tmp_path,
                                    timeout_s=5)
    assert never.outcome == execution.EXECUTION_LAUNCH_FAILED
    assert never.launched is False

    monkeypatch.setattr(execution.subprocess, 'Popen', _CollectFails)
    collected = execution.spawn_capture(['sh', '-c', 'sleep 0.2'], tmp_path,
                                        timeout_s=30)
    assert collected.outcome == execution.EXECUTION_FAILED_TO_COLLECT
    assert collected.launched is True
    assert collected.exit_code is None
    assert 'simulated collection failure' in collected.stderr


def test_collection_failure_is_not_reported_as_a_launch_failure(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = execution.ExecutionOutcome(
        argv=('x',), cwd=str(tmp_path),
        outcome=execution.EXECUTION_FAILED_TO_COLLECT, exit_code=None,
        stdout='', stderr='OSError: collection', duration_ms=1,
        timeout_s=30, launched=True,
        process_termination='confirmed', termination_detail='reaped')
    monkeypatch.setattr(check_runner, 'spawn_capture',
                        lambda *a, **k: outcome)
    checks = check_runner.run_scoped(
        tmp_path, changed_files=['probe.py'],
        targeted_tests=['tests/test_leaf.py'], mypy_targets=[])
    entry = next(c for c in checks if c['name'] == 'pytest')
    assert entry['status'] == 'failed'
    assert entry['note'].startswith('output collection failed')
    assert entry['launched'] is True

    monkeypatch.setattr(validation, 'spawn_capture', lambda *a, **k: outcome)
    entries = validation.run_validation_commands([['x']], tmp_path)
    assert entries[0]['note'].startswith('output collection failed')

    monkeypatch.setattr(runner, 'spawn_capture', lambda *a, **k: outcome)
    with pytest.raises(execution.ExecutionCollectFailed):
        runner._spawn(['x'], tmp_path, 5)


# ---------------------------------------------------------------- F-04
def test_a_missing_exit_code_is_never_zero(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = execution.ExecutionOutcome(
        argv=('x',), cwd=str(tmp_path), outcome=execution.EXECUTION_EXITED,
        exit_code=None, stdout='', stderr='', duration_ms=1, timeout_s=5,
        launched=True, process_termination='not_required',
        termination_detail='n/a')
    monkeypatch.setattr(runner, 'spawn_capture', lambda *a, **k: outcome)
    with pytest.raises(Exception) as caught:
        runner._spawn(['x'], tmp_path, 5)
    assert 'exit_code' in str(caught.value)


# ---------------------------------------------------------------- F-06
def _timeout_execution() -> execution.ExecutionOutcome:
    return execution.ExecutionOutcome(
        argv=('x',), cwd='.', outcome=execution.EXECUTION_TIMED_OUT,
        exit_code=None, stdout='', stderr='', duration_ms=1234,
        timeout_s=600, launched=True, process_termination='confirmed',
        termination_detail='reaped')


@pytest.mark.parametrize('fast_path', [True, False])
def test_mcp_timeout_keeps_its_facts(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        fast_path: bool) -> None:
    repo = _repo(tmp_path)

    def boom(worker, path, *, deadline=None):
        raise execution.ExecutionTimeout(_timeout_execution())

    monkeypatch.setattr(worker_execution, 'execute_with_audit', boom)
    report = worker_execution.run_worker_in_worktree(
        repo, _worker(['true']), task_id='t-mcp', fast_path=fast_path)
    state = report['states']['w1']
    assert state['state'] == 'FAILED'
    assert state['reason'] == 'timeout_exceeded'
    assert state['timeout_s'] == 600
    assert state['elapsed_ms'] == 1234
    assert state['execution_outcome'] == execution.EXECUTION_TIMED_OUT
    assert state['process_termination'] == 'confirmed'


def test_untyped_timeout_still_maps_to_timeout_exceeded(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def boom(worker, path, *, deadline=None):
        raise subprocess.TimeoutExpired(cmd=['x'], timeout=1)

    monkeypatch.setattr(worker_execution, 'execute_with_audit', boom)
    report = worker_execution.run_worker_in_worktree(
        repo, _worker(['true']), task_id='t-mcp2')
    state = report['states']['w1']
    assert state['state'] == 'FAILED'
    assert state['reason'] == 'timeout_exceeded'
    assert 'timeout_s' not in state


# ------------------------------------------------- N-07 (execution boundary)
_POPEN_SEEN: list = []


def _fake_popen(monkeypatch, budgets: list) -> None:
    """Boundary capture: what budget does ``communicate`` actually get?"""

    class _Proc:
        returncode = 0
        pid = 4242

        def communicate(self, timeout=None, input=None):
            budgets.append(timeout)
            return (b'', b'')

        def kill(self) -> None:
            return None

        def wait(self, timeout=None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: _Proc())


def test_no_process_is_created_when_the_deadline_is_spent(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Proof at the creation boundary, not from the result it produced."""
    repo = _repo(tmp_path, slow_test=True)
    created: list = []
    real_popen = subprocess.Popen

    def spy(argv, *args, **kwargs):
        created.append(list(argv))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', spy)
    deadline = timeouts.Deadline(0.01)
    time.sleep(0.05)
    checks = check_runner.run_scoped(
        repo, changed_files=['probe.py'],
        targeted_tests=['tests/test_leaf.py'], mypy_targets=[],
        timeout=30, deadline=deadline)
    assert created == [], 'a spent deadline must refuse before Popen'
    pytest_entry = next(c for c in checks if c['name'] == 'pytest')
    assert pytest_entry['launched'] is False
    assert pytest_entry['termination'] == execution.EXECUTION_DEADLINE_EXCEEDED


def _baseline_boundary(monkeypatch, tmp_path: Path,
                       deadline=None) -> list:
    budgets: list = []
    _fake_popen(monkeypatch, budgets)
    decision = SimpleNamespace(eligible=True,
                               targeted_tests=('tests/test_leaf.py',),
                               mypy_targets=())
    worker_execution._run_checks(
        decision, tmp_path, {'affected_files': ['probe.py']},
        timeout_s=check_runner.BASELINE_TIMEOUT_S, deadline=deadline)
    return budgets


def test_baseline_budget_reaches_the_execution_boundary(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    budgets = _baseline_boundary(monkeypatch, tmp_path)
    assert budgets, 'the check must reach the boundary'
    assert set(budgets) == {1800.0}


def test_short_deadline_caps_the_baseline_at_the_boundary(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    budgets = _baseline_boundary(monkeypatch, tmp_path,
                                 timeouts.Deadline(5))
    assert budgets and all(4.9 < budget <= 5.0 for budget in budgets)


def test_long_deadline_does_not_extend_the_baseline(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    budgets = _baseline_boundary(monkeypatch, tmp_path,
                                 timeouts.Deadline(3600))
    assert set(budgets) == {1800.0}


# ------------------------------------------------------------- N-01 (MCP)
def _dispatch(monkeypatch, tmp_path: Path, state: dict, *,
              rpc: bool = False) -> dict:
    from asha import mcp_server

    repo = _repo(tmp_path)

    def fake_run_worker_in_worktree(root, worker, *, task_id, **kwargs):
        return {'task_id': task_id, 'status': 'failed',
                'states': {worker['id']: state}, 'evidence': {},
                'authoritative': {}, 'worktrees': {}, 'routing': {},
                'cleanup_errors': []}

    monkeypatch.setattr(mcp_server, 'run_worker_in_worktree',
                        fake_run_worker_in_worktree)
    arguments = {'root': str(repo), 'id': 'w1',
                 'declared_scope': ['probe.py'], 'reads': [],
                 'writes': ['probe.py'], 'cmd': ['sh', '-c', 'true']}
    if rpc:
        frame = mcp_server.handle_message({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'asha_dispatch_task',
                       'arguments': arguments}})
        assert isinstance(frame, dict), frame
        result = frame['result']
        assert isinstance(result, dict), frame
        return json.loads(result['content'][0]['text'])
    reply = mcp_server.asha_dispatch_task(arguments)
    assert isinstance(reply, dict), reply
    return json.loads(reply['content'][0]['text'])


_TIMEOUT_STATE = {'state': 'FAILED', 'reason': 'timeout_exceeded',
                  'launched': True, 'timeout_s': 30.0,
                  'elapsed_ms': 30123, 'process_termination': 'confirmed',
                  'execution_outcome': 'timed_out'}


def test_dispatch_reports_a_launched_process_that_timed_out(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = _dispatch(monkeypatch, tmp_path, dict(_TIMEOUT_STATE))
    assert payload['executed'] is True, payload
    assert payload['launched'] is True
    assert payload['timeout_s'] == 30.0
    assert payload['elapsed_ms'] == 30123
    assert payload['process_termination'] == 'confirmed'
    assert payload['execution_outcome'] == 'timed_out'
    assert payload['reason'] == 'timeout_exceeded'
    assert payload['reason_code'] is None, 'routing code stays null'


def test_dispatch_reports_a_pre_launch_deadline_refusal(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = _dispatch(monkeypatch, tmp_path, {
        'state': 'BLOCKED', 'reason': 'deadline_exceeded',
        'launched': False, 'execution_outcome': 'deadline_exceeded'})
    assert payload['executed'] is False, payload
    assert payload['launched'] is False
    assert payload['reason'] == 'deadline_exceeded'


def test_executed_keeps_legacy_and_explicit_semantics() -> None:
    from asha import mcp_server
    assert mcp_server._execution_ran({'exit_status': 0}, {}) is True
    assert mcp_server._execution_ran({'state': 'FAILED'}, {}) is False
    assert mcp_server._execution_ran({'launched': True}, {}) is True
    assert mcp_server._execution_ran(
        {'launched': False, 'exit_status': 0}, {}) is False


def test_dispatch_of_a_normal_run_is_unchanged(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = _dispatch(monkeypatch, tmp_path, {
        'state': 'DONE', 'exit_status': 0, 'applied': True,
        'verification_status': 'VERIFIED_NO_REGRESSION'})
    assert payload['executed'] is True
    assert payload.get('reason') is None
    assert payload['verification_status'] == 'VERIFIED_NO_REGRESSION'
    assert payload['applied'] is True


def test_timeout_facts_survive_jsonrpc_serialization(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = _dispatch(monkeypatch, tmp_path, dict(_TIMEOUT_STATE), rpc=True)
    assert payload['executed'] is True
    assert payload['launched'] is True
    assert payload['timeout_s'] == 30.0
    assert payload['elapsed_ms'] == 30123
    assert payload['process_termination'] == 'confirmed'
    assert payload['execution_outcome'] == 'timed_out'


# --------------------------------------------------------------- N-02
def _tree(repo: Path) -> str:
    return subprocess.run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=repo,
                          check=True, capture_output=True,
                          text=True).stdout.strip()


def test_expired_deadline_in_current_checks_names_the_deadline(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Deadline, not the baseline parser, owns the failure."""
    repo = _repo(tmp_path)
    created: list = []
    real_popen = subprocess.Popen

    def spy(argv, *args, **kwargs):
        created.append(list(argv))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', spy)
    deadline = timeouts.Deadline(0.01)
    time.sleep(0.05)
    outcome = worker_execution.collect_worker_evidence(
        _worker(['sh', '-c', 'true']), repo, 0, '', 't-dl',
        tmp_path / 'ev', base='main', base_tree=_tree(repo),
        deadline=deadline)
    assert outcome['state'] == 'FAILED', outcome
    assert outcome['reason'] == 'deadline_exceeded', outcome
    assert outcome['verification_status'] == 'DEADLINE_EXCEEDED'
    assert outcome['evidence'] is None
    launched_checks = [argv for argv in created
                       if any('pytest' in part for part in argv)]
    assert launched_checks == [], 'no check process may exist'


def test_deadline_expiry_during_a_check_names_the_deadline(
        tmp_path: Path) -> None:
    """Mid-execution expiry: the child was killed, the cause is the budget."""
    repo = _repo(tmp_path, slow_test=True)
    deadline = timeouts.Deadline(2.0)
    outcome = worker_execution.collect_worker_evidence(
        _worker(['sh', '-c', 'true']), repo, 0, '', 't-dl2',
        tmp_path / 'ev', base='main', base_tree=_tree(repo),
        deadline=deadline)
    assert outcome['state'] == 'FAILED', outcome
    assert outcome['reason'] == 'deadline_exceeded', outcome
    assert outcome['verification_status'] == 'DEADLINE_EXCEEDED'


def test_deadline_expiry_in_the_deferred_baseline_names_the_deadline(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path, slow_test=True)
    report = dag.run_workers_dag(
        repo, [_worker(['sh', '-c', 'sleep 0.6'])], task_id='t-base-dl',
        deadline=timeouts.Deadline(1.5))
    entry = report['states']['w1']
    assert entry['state'] == 'FAILED', entry
    assert entry['reason'] == 'deadline_exceeded', entry
    assert entry['verification_status'] == 'DEADLINE_EXCEEDED'
    assert entry.get('evidence') is None
    assert report['status'] == 'failed'


def test_an_operation_timeout_is_not_task_deadline_exhaustion() -> None:
    assert worker_execution._deadline_stopped(
        [{'termination': 'timed_out', 'status': 'failed'}]) is False
    assert worker_execution._deadline_stopped(
        [{'termination': 'exited', 'status': 'failed'}]) is False
    assert worker_execution._deadline_stopped(
        [{'termination': 'deadline_exceeded', 'status': 'failed'}]) is True
    assert worker_execution._deadline_stopped(
        None, [{'termination': 'deadline_exceeded'}]) is True


def test_an_ordinary_baseline_failure_keeps_its_own_reason(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / 'tests' / 'test_leaf.py').write_text(
        'def test_broken(syntax error\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'broken'], cwd=repo, check=True,
                   capture_output=True)
    report = dag.run_workers_dag(repo, [_worker(['sh', '-c', 'true'])],
                                 task_id='t-parser')
    entry = report['states']['w1']
    assert entry['state'] == 'FAILED', entry
    assert 'deadline' not in str(entry.get('reason')), entry
    assert 'comparison' in str(entry.get('reason')), entry
