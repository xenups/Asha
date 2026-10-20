"""M1 -- a supplied acceptance command must actually pass, and say so.

Observable contracts pinned here:

* the acceptance command runs in the CANDIDATE worktree, exactly once;
* its outcome is sealed into worker evidence as ``acceptance``
  (command, supplied, ran, exit_code, outcome) and survives reload;
* supplied-but-not-run, exit 4/5, and skip/deselect-only tails are never
  a pass, and never get a fabricated exit code;
* a green suite cannot override a failing acceptance command;
* the CLI reports FAIL instead of PASS.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from asha import cli, evidence
from asha.governance import worker_execution as we

_PASSING_CHECK = {'name': 'pytest', 'status': 'passed', 'exit_code': 0,
                  'output_tail': '1 passed in 0.01s\n'}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f'git {args} failed: {proc.stderr.strip()}')
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'tests').mkdir(parents=True)
    _git(root, 'init', '-q', '.')
    _git(root, 'config', 'user.email', 't@example.invalid')
    _git(root, 'config', 'user.name', 'test')
    (root / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    return root


@pytest.fixture()
def fast_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence collection stays honest but fast: the matrix 'ran' green."""
    monkeypatch.setattr(we.check_runner, 'run',
                        lambda *a, **k: [dict(_PASSING_CHECK)])
    monkeypatch.setattr(we.check_runner, 'run_scoped',
                        lambda *a, **k: [dict(_PASSING_CHECK)])


def _script(directory: Path, name: str, body: str) -> str:
    """A verify_command that is one argv string pointing at a real file."""
    path = directory / name
    path.write_text(body, encoding='utf-8')
    return f'{sys.executable} {path}'


def _worker(verify: str | None = None) -> dict[str, Any]:
    worker: dict[str, Any] = {
        'id': 'w1', 'deps': [], 'declared_scope': ['tests/test_thing.py'],
        'reads': ['tests/test_thing.py'], 'writes': [],
        'cmd': [sys.executable, '-c', 'pass']}
    if verify is not None:
        worker['verify_command'] = verify
    return worker


def _collect(repo: Path, tmp_path: Path, worker: dict[str, Any],
             rc: int = 0, audit: dict[str, Any] | None = None
             ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    head = _git(repo, 'rev-parse', 'HEAD')
    tree = _git(repo, 'rev-parse', 'HEAD^{tree}')
    outcome = we.collect_worker_evidence(
        worker, repo, rc, '', 'task-m1', tmp_path / 'evidence',
        base=head, base_tree=tree, audit=audit)
    payload = None
    if outcome.get('evidence'):
        payload = json.loads(Path(outcome['evidence']).read_text(
            encoding='utf-8'))
    return outcome, payload


# ----------------------------------------------------------- real execution
def test_acceptance_runs_in_the_candidate_worktree(tmp_path: Path) -> None:
    marker = tmp_path / 'cwd.txt'
    body = ('import os, pathlib\n'
            f'pathlib.Path(r"{marker}").write_text(os.getcwd())\n'
            'print("1 passed in 0.01s")\n')
    worker = _worker(_script(tmp_path, 'acc.py', body))
    rc, _tail, audit = we.execute_with_audit(worker, tmp_path)
    assert rc == 0
    assert audit['verify_ran'] is True
    assert audit['verify_exit'] == 0
    assert Path(marker.read_text(encoding='utf-8')).resolve() == \
        tmp_path.resolve()


def test_acceptance_failure_executes_exactly_once(tmp_path: Path) -> None:
    marker = tmp_path / 'runs.txt'
    body = ('import pathlib\n'
            f'p = pathlib.Path(r"{marker}")\n'
            'p.write_text((p.read_text() if p.exists() else "") + "x")\n'
            'raise SystemExit(3)\n')
    worker = _worker(_script(tmp_path, 'acc.py', body))
    rc, _tail, audit = we.execute_with_audit(worker, tmp_path)
    assert rc == 3
    assert audit['verify_ran'] is True
    assert marker.read_text(encoding='utf-8') == 'x'


# ------------------------------------------------------- evidence outcomes
def test_passing_acceptance_is_sealed(repo: Path, tmp_path: Path,
                                      fast_checks: None) -> None:
    body = 'print("4 passed in 0.02s")\n'
    worker = _worker(_script(tmp_path, 'acc.py', body))
    _rc, _tail, audit = we.execute_with_audit(worker, repo)
    outcome, payload = _collect(repo, tmp_path, worker, audit=audit)
    assert outcome['state'] == 'DONE'
    assert payload is not None
    acceptance = payload['acceptance']
    assert acceptance['supplied'] is True
    assert acceptance['ran'] is True
    assert acceptance['exit_code'] == 0
    assert acceptance['outcome'] == 'passed'
    assert acceptance['command'] == worker['verify_command']
    # durable: digest recomputes and the record survives a reload
    assert payload['evidence_sha256'] == evidence.compute_digest(payload)
    reloaded = json.loads(Path(outcome['evidence']).read_text(
        encoding='utf-8'))
    assert reloaded['acceptance']['outcome'] == 'passed'


def test_missing_acceptance_is_not_required(repo: Path, tmp_path: Path,
                                            fast_checks: None) -> None:
    worker = _worker()
    outcome, payload = _collect(repo, tmp_path, worker, audit={})
    assert outcome['state'] == 'DONE'
    assert payload is not None
    assert payload['acceptance'] == {
        'command': None, 'supplied': False, 'ran': False,
        'exit_code': None, 'outcome': 'not_required'}


def test_supplied_but_not_run_never_passes(repo: Path, tmp_path: Path,
                                           fast_checks: None) -> None:
    worker = _worker('pytest tests')
    audit = {'verify_command': worker['verify_command'],
             'verify_ran': False, 'verify_exit': None}
    _outcome, payload = _collect(repo, tmp_path, worker, audit=audit)
    assert payload is not None
    acceptance = payload['acceptance']
    assert acceptance['supplied'] is True
    assert acceptance['ran'] is False
    assert acceptance['exit_code'] is None      # never fabricated
    assert acceptance['outcome'] == 'not_executed'
    assert we.acceptance_failed(acceptance) is True


def test_nonzero_exit_is_acceptance_failure(repo: Path, tmp_path: Path) -> None:
    body = 'raise SystemExit(3)\n'
    worker = _worker(_script(tmp_path, 'acc.py', body))
    rc, _tail, audit = we.execute_with_audit(worker, repo)
    assert rc == 3
    outcome, payload = _collect(repo, tmp_path, worker, rc=rc, audit=audit)
    assert payload is None                      # no sealed evidence at rc != 0
    assert outcome['state'] == 'FAILED'
    assert outcome['reason'] == 'acceptance_failed'
    assert outcome['acceptance']['exit_code'] == 3
    assert outcome['acceptance']['outcome'] == 'failed'


@pytest.mark.parametrize('code', [4, 5])
def test_runner_4_and_5_are_inconclusive(repo: Path, tmp_path: Path,
                                         code: int) -> None:
    worker = _worker('pytest whatever')
    audit = {'verify_ran': True, 'verify_exit': code,
             'verify_output_tail': 'no tests ran in 0.01s'}
    outcome, _payload = _collect(repo, tmp_path, worker, rc=code, audit=audit)
    assert outcome['state'] == 'FAILED'
    assert outcome['acceptance']['outcome'] == 'inconclusive'
    assert outcome['acceptance']['exit_code'] == code


@pytest.mark.parametrize('tail', [
    '1 skipped in 0.01s',
    '1 deselected in 0.01s',
    '0 passed, 2 skipped in 0.01s',
])
def test_skip_or_deselect_only_is_inconclusive(repo: Path, tmp_path: Path,
                                               fast_checks: None,
                                               tail: str) -> None:
    worker = _worker('pytest -k nothing')
    audit = {'verify_ran': True, 'verify_exit': 0, 'verify_output_tail': tail}
    _outcome, payload = _collect(repo, tmp_path, worker, audit=audit)
    assert payload is not None
    assert payload['acceptance']['outcome'] == 'inconclusive'
    assert we.acceptance_failed(payload['acceptance']) is True


def test_sealed_worker_path_records_acceptance(repo: Path, tmp_path: Path,
                                               fast_checks: None) -> None:
    """Every sealed worker path carries the record, not only the CLI."""
    body = 'print("2 passed in 0.01s")\n'
    worker = _worker(_script(tmp_path, 'acc.py', body))
    report = we.run_worker_in_worktree(
        repo, worker, task_id='task-m1-wt', keep_worktrees=False)
    ev_path = (report.get('evidence') or {}).get(worker['id'])
    assert ev_path, report
    payload = json.loads(Path(ev_path).read_text(encoding='utf-8'))
    assert payload['acceptance']['outcome'] == 'passed'
    assert payload['acceptance']['ran'] is True


def test_real_pytest_acceptance_does_not_pollute_the_worktree(
        repo: Path, fast_checks: None) -> None:
    """A pytest acceptance run must not commit __pycache__/.pytest_cache
    into the candidate tree -- those would read back as undeclared writes
    and turn a passing acceptance into INVALID_EVIDENCE."""
    worker = _worker(f'{sys.executable} -m pytest tests -q')
    report = we.run_worker_in_worktree(
        repo, worker, task_id='task-m1-pytest', keep_worktrees=False)
    outcome = (report.get('states') or {}).get(worker['id']) or {}
    assert outcome.get('state') == 'DONE', outcome
    payload = json.loads(Path(outcome['evidence']).read_text(
        encoding='utf-8'))
    assert payload['acceptance']['outcome'] == 'passed'
    assert payload['acceptance']['exit_code'] == 0


# ---------------------------------------------------------- CLI semantics
def _install_fake_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                         acceptance: dict[str, Any],
                         state: str = 'DONE') -> None:
    sealed = evidence.seal({
        'schema': evidence.SCHEMA, 'stage': 'worker', 'scope': 'S0',
        'commit': 'a' * 40, 'tree_hash': 'a' * 40,
        'observed_at': evidence.now_iso(),
        'checks': [dict(_PASSING_CHECK)],
        'authorized_to_ship': False, 'target_tree_sha': 'a' * 40,
        'acceptance': acceptance,
    })
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps(sealed), encoding='utf-8')

    def _report(root: Any, worker: dict[str, Any], **kw: Any
                ) -> dict[str, Any]:
        wid = worker['id']
        return {
            'states': {wid: {
                'state': state, 'reason': None,
                'evidence': str(path) if state == 'DONE' else None,
                'acceptance': acceptance}},
            'evidence': {wid: str(path)} if state == 'DONE' else {},
            'authoritative': {wid: b'record'},
        }

    monkeypatch.setattr(cli, 'governance_profile', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'route',
                        lambda *a, **k: types.SimpleNamespace(
                            mode=cli.RuntimeMode.FAST_PATH))
    monkeypatch.setattr(cli, 'run_worker_in_worktree', _report)
    monkeypatch.setattr(cli, 'verify_worker_evidence', lambda *a, **k: True)
    monkeypatch.setattr(cli, 'parse_evidence', lambda raw: raw)
    monkeypatch.setattr(cli, 'verify_record',
                        lambda parsed: type('R', (), {'verified': True})())
    monkeypatch.setattr(cli, 'verify_bytes',
                        lambda raw: type('R', (), {'verified': True})())


@pytest.mark.parametrize('acceptance, expected', [
    ({'command': 'x', 'supplied': True, 'ran': True, 'exit_code': 2,
      'outcome': 'failed'}, 'FAIL'),
    ({'command': 'x', 'supplied': True, 'ran': False, 'exit_code': None,
      'outcome': 'not_executed'}, 'FAIL'),
    ({'command': 'x', 'supplied': True, 'ran': True, 'exit_code': 0,
      'outcome': 'inconclusive'}, 'FAIL'),
    ({'command': 'x', 'supplied': True, 'ran': True, 'exit_code': 0,
      'outcome': 'passed'}, 'PASS'),
    (None, 'PASS'),
])
def test_cli_validation_follows_acceptance(repo: Path, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch,
                                           acceptance: dict[str, Any] | None,
                                           expected: str) -> None:
    record = acceptance or {'command': None, 'supplied': False, 'ran': False,
                            'exit_code': None, 'outcome': 'not_required'}
    _install_fake_worker(monkeypatch, tmp_path, record)
    result = cli._run_sealed(repo, ['tests/test_thing.py'],
                             classification=None,
                             acceptance=(acceptance or {}).get('command'))
    assert result['validation_result'] == expected
    assert result['acceptance']['outcome'] == record['outcome']


def test_cli_acceptance_failure_has_no_evidence_id(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed acceptance at execution stage seals nothing, claims nothing."""
    record = {'command': 'x', 'supplied': True, 'ran': True, 'exit_code': 7,
              'outcome': 'failed'}
    _install_fake_worker(monkeypatch, tmp_path, record, state='FAILED')
    result = cli._run_sealed(repo, ['tests/test_thing.py'],
                             classification=None, acceptance='x')
    assert result['validation_result'] == 'FAIL'
    assert result['evidence_id'] is None
    assert result['acceptance']['exit_code'] == 7


def test_green_suite_cannot_override_acceptance_failure(
        repo: Path, tmp_path: Path, fast_checks: None) -> None:
    body = 'raise SystemExit(1)\n'
    worker = _worker(_script(tmp_path, 'acc.py', body))
    rc, _tail, audit = we.execute_with_audit(worker, repo)
    outcome, _payload = _collect(repo, tmp_path, worker, rc=rc, audit=audit)
    assert outcome['state'] == 'FAILED'
    assert outcome['reason'] == 'acceptance_failed'   # never a suite PASS


def test_blank_acceptance_flag_is_rejected(repo: Path,
                                           capsys: Any) -> None:
    code = cli.main(['--root', str(repo), '--acceptance', '   ', '--json'])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'INVALID_ARGUMENTS'
    assert '--acceptance' in captured.err
    assert payload['acceptance'] is None
