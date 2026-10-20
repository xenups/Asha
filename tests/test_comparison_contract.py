"""M3 -- explicit, truthful baseline-comparison evidence.

Observable contract pinned here:

* every applicable sealed result carries ``comparison`` with
  ``performed`` / ``baseline_tree`` / ``candidate_tree`` /
  ``comparable`` / ``reason`` (+ ``verdict`` only when performed);
* ``reason`` is one of COMPARED, NOT_COMPARED, BASE_EQUALS_TARGET,
  INVENTORY_MISMATCH;
* an identical baseline/candidate pair is never reported as
  regression-free, and a missing baseline is never an empty delta;
* the record exists on success and on failure payloads alike.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from asha import cli, evidence
from asha.governance import worker_execution as we

_PASSING = {'name': 'pytest', 'status': 'passed', 'exit_code': 0,
            'output_tail': '1 passed in 0.01s\n'}
_FAILING = {'name': 'pytest', 'status': 'failed', 'exit_code': 1,
            'output_tail': 'FAILED src/mod.py::test_x - AssertionError: b\n'}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f'git {args} failed: {proc.stderr.strip()}')
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'src').mkdir(parents=True)
    _git(root, 'init', '-q', '.')
    _git(root, 'config', 'user.email', 't@example.invalid')
    _git(root, 'config', 'user.name', 'test')
    (root / 'src' / 'mod.py').write_text('VALUE = 1\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    return root


def _checks(monkeypatch: pytest.MonkeyPatch, entries: list[dict]) -> None:
    monkeypatch.setattr(we.check_runner, 'run',
                        lambda *a, **k: [dict(e) for e in entries])
    monkeypatch.setattr(we.check_runner, 'run_scoped',
                        lambda *a, **k: [dict(e) for e in entries])


def _worker(writes: dict[str, str] | None = None) -> dict[str, Any]:
    writes = writes or {}
    worker: dict[str, Any] = {
        'id': 'w1', 'deps': [],
        'declared_scope': sorted(writes) or ['src/mod.py'],
        'reads': ['src/mod.py'], 'writes': sorted(writes),
        'cmd': [sys.executable, '-c', 'pass']}
    if writes:
        # test-only payload: _collect writes these before evidence is
        # collected (the worker command itself is never spawned here)
        worker['_test_files'] = dict(writes)
    return worker


def _collect(repo: Path, tmp_path: Path, worker: dict[str, Any],
             baseline_journal: dict[str, Any] | None = None,
             rc: int = 0, audit: dict[str, Any] | None = None,
             base: str | None = None,
             files: dict[str, str] | None = None
             ) -> tuple[dict, dict | None]:
    for rel, body in (files or worker.pop('_test_files', None)
                      or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding='utf-8')
    base = base or _git(repo, 'rev-parse', 'HEAD')
    tree = _git(repo, 'rev-parse', f'{base}^{{tree}}')
    outcome = we.collect_worker_evidence(
        worker, repo, rc, '', 'task-m3', tmp_path / 'evidence',
        base=base, base_tree=tree, audit=audit,
        baseline_journal=baseline_journal)
    payload = None
    if outcome.get('evidence'):
        payload = json.loads(Path(outcome['evidence']).read_text(
            encoding='utf-8'))
    return outcome, payload


def _journal(checks: list[dict], scope: str | None = None) -> dict[str, Any]:
    journal: dict[str, Any] = {'checks': checks}
    if scope is not None:
        journal['resolved'] = {'scope': scope}
    return journal


# ------------------------------------------------------- success semantics
def test_success_with_identical_trees_is_base_equals_target(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    base = _git(repo, 'rev-parse', 'HEAD')
    _outcome, payload = _collect(repo, tmp_path, _worker(),
                                 baseline_journal=_journal([_PASSING]),
                                 base=base)
    assert payload is not None
    comparison = payload['comparison']
    assert comparison['performed'] is False
    assert comparison['comparable'] is False
    assert comparison['reason'] == 'BASE_EQUALS_TARGET'
    assert comparison['baseline_tree'] == comparison['candidate_tree']
    assert comparison['verdict'] is None      # no "no regression" claim


def test_success_without_baseline_is_not_compared(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    _outcome, payload = _collect(repo, tmp_path, _worker())
    assert payload is not None
    comparison = payload['comparison']
    assert comparison['reason'] == 'NOT_COMPARED'
    assert comparison['performed'] is False
    assert comparison['comparable'] is False
    assert comparison['verdict'] is None


def test_inventory_mismatch_refuses_comparison(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 2\n'})
    _outcome, payload = _collect(
        repo, tmp_path, worker, base=base,
        baseline_journal=_journal([{'name': 'ruff', 'status': 'passed'}]))
    assert payload is not None
    comparison = payload['comparison']
    assert comparison['reason'] == 'INVENTORY_MISMATCH'
    assert comparison['performed'] is False
    assert comparison['verdict'] is None


def test_scope_mismatch_refuses_comparison(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 3\n'})
    _outcome, payload = _collect(
        repo, tmp_path, worker, base=base,
        baseline_journal=_journal([_PASSING], scope='S9_NOT_A_SCOPE'))
    assert payload is not None
    assert payload['comparison']['reason'] == 'INVENTORY_MISMATCH'


def test_distinct_trees_are_actually_compared(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 4\n'})
    _outcome, payload = _collect(repo, tmp_path, worker, base=base,
                                 baseline_journal=_journal([_PASSING]))
    assert payload is not None
    comparison = payload['comparison']
    assert comparison['performed'] is True
    assert comparison['comparable'] is True
    assert comparison['reason'] == 'COMPARED'
    assert comparison['verdict'] == 'NO_FAILURES'
    assert comparison['baseline_tree'] != comparison['candidate_tree']
    assert comparison['candidate_tree'] == payload['tree_hash']


def test_detected_regression_is_recorded(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_FAILING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 5\n'})
    outcome, _payload = _collect(repo, tmp_path, worker, base=base,
                                 baseline_journal=_journal([_PASSING]))
    assert outcome['state'] == 'FAILED'
    comparison = outcome['comparison']
    assert comparison['performed'] is True
    assert comparison['reason'] == 'COMPARED'
    assert comparison['verdict'] == 'NEW_FAILURES'
    assert [d['location'] for d in outcome['delta_failures']] == [
        'src/mod.py::test_x']
    assert outcome['baseline_failures'] == []


def test_not_compared_never_claims_no_regression(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Identical trees + failing check: legacy verdict exists, but the
    comparison record refuses to describe it as regression-free."""
    _checks(monkeypatch, [_FAILING])
    base = _git(repo, 'rev-parse', 'HEAD')
    outcome, _payload = _collect(repo, tmp_path, _worker(), base=base,
                                 baseline_journal=_journal([_PASSING]))
    assert outcome['state'] == 'FAILED'
    comparison = outcome['comparison']
    assert comparison['reason'] == 'BASE_EQUALS_TARGET'
    assert comparison['performed'] is False
    assert comparison['verdict'] is None
    assert outcome['verdict'] != 'NO_FAILURES'


def test_missing_baseline_is_not_an_empty_delta(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_FAILING])
    outcome, _payload = _collect(repo, tmp_path, _worker())
    assert outcome['verdict'] == 'UNKNOWN'
    assert outcome['baseline_failures'] is None
    assert [d['location'] for d in outcome['delta_failures']] == [
        'src/mod.py::test_x']                 # current != empty
    assert outcome['comparison']['performed'] is False
    assert outcome['comparison']['verdict'] is None


# ------------------------------------------------------- failure semantics
def test_acceptance_failure_retains_acceptance_and_comparison(
        repo: Path, tmp_path: Path) -> None:
    worker = _worker()
    worker['verify_command'] = f'{sys.executable} -c "raise SystemExit(2)"'
    rc, _tail, audit = we.execute_with_audit(worker, repo)
    assert rc == 2
    outcome, payload = _collect(repo, tmp_path, worker, rc=rc, audit=audit)
    assert payload is None
    assert outcome['state'] == 'FAILED'
    assert outcome['reason'] == 'acceptance_failed'
    assert outcome['acceptance']['outcome'] == 'failed'
    assert outcome['acceptance']['exit_code'] == 2
    assert outcome['comparison']['reason'] == 'NOT_COMPARED'
    assert outcome['comparison']['performed'] is False


def test_comparison_survives_serialization(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 6\n'})
    outcome, payload = _collect(repo, tmp_path, worker, base=base,
                                baseline_journal=_journal([_PASSING]))
    assert payload is not None
    reloaded = json.loads(Path(outcome['evidence']).read_text(
        encoding='utf-8'))
    assert reloaded['comparison'] == payload['comparison']
    assert reloaded['evidence_sha256'] == evidence.compute_digest(reloaded)


# ------------------------------------------------------------- CLI surface
def _install_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                  comparison: dict[str, Any]) -> None:
    sealed = evidence.seal({
        'schema': evidence.SCHEMA, 'stage': 'worker', 'scope': 'S0',
        'commit': 'a' * 40, 'tree_hash': 'a' * 40,
        'observed_at': evidence.now_iso(), 'checks': [dict(_PASSING)],
        'authorized_to_ship': False, 'target_tree_sha': 'a' * 40,
        'comparison': comparison,
        'acceptance': {'command': None, 'supplied': False, 'ran': False,
                       'exit_code': None, 'outcome': 'not_required'},
    })
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps(sealed), encoding='utf-8')

    def _report(root: Any, worker: dict[str, Any], **kw: Any) -> dict:
        wid = worker['id']
        return {'states': {wid: {'state': 'DONE', 'reason': None,
                                 'evidence': str(path),
                                 'acceptance': sealed['acceptance'],
                                 'comparison': comparison}},
                'evidence': {wid: str(path)},
                'authoritative': {wid: b'record'}}

    monkeypatch.setattr(cli, 'governance_profile', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'route',
                        lambda *a, **k: __import__('types').SimpleNamespace(
                            mode=cli.RuntimeMode.FAST_PATH))
    monkeypatch.setattr(cli, 'run_worker_in_worktree', _report)
    monkeypatch.setattr(cli, 'verify_worker_evidence', lambda *a, **k: True)
    monkeypatch.setattr(cli, 'parse_evidence', lambda raw: raw)
    monkeypatch.setattr(cli, 'verify_record',
                        lambda parsed: type('R', (), {'verified': True})())
    monkeypatch.setattr(cli, 'verify_bytes',
                        lambda raw: type('R', (), {'verified': True})())


@pytest.mark.parametrize('reason', ['BASE_EQUALS_TARGET', 'NOT_COMPARED',
                                    'INVENTORY_MISMATCH', 'COMPARED'])
def test_cli_result_always_carries_comparison(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        reason: str, capsys: Any) -> None:
    comparison = {'performed': reason == 'COMPARED',
                  'baseline_tree': 'a' * 40,
                  'candidate_tree': 'b' * 40,
                  'comparable': reason == 'COMPARED',
                  'reason': reason,
                  'verdict': 'NO_FAILURES' if reason == 'COMPARED' else None}
    _install_fake(monkeypatch, tmp_path, comparison)
    result = cli._run_sealed(repo, ['src/mod.py'], classification=None)
    assert result['comparison'] == comparison
    assert result['validation_result'] == 'PASS'


def test_cli_payload_key_present_even_without_execution(repo: Path,
                                                        capsys: Any) -> None:
    code = cli.main(['--root', str(repo), '--no-execute', '--json'])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert 'comparison' in payload
    assert payload['comparison'] is None


# ------------------------------------------------ R3: mandatory identities
@pytest.mark.parametrize('base_tree,candidate_tree', [
    (None, 'b' * 40), ('a' * 40, None), (None, None),
    ('', 'b' * 40), ('a' * 40, ''), ('', ''),
])
def test_missing_tree_identifiers_are_not_compared(
        base_tree: str | None, candidate_tree: str | None) -> None:
    record = we.build_comparison(
        base_tree=base_tree, candidate_tree=candidate_tree,
        baseline_checks=[dict(_PASSING)], candidate_checks=[dict(_PASSING)])
    assert record['performed'] is False
    assert record['comparable'] is False
    assert record['reason'] == 'NOT_COMPARED'
    assert record['detail'] == 'missing_tree_identifier'
    assert record['verdict'] is None
    assert record['baseline_tree'] == base_tree
    assert record['candidate_tree'] == candidate_tree   # no synthetic ids


def test_comparison_detail_is_null_when_performed() -> None:
    record = we.build_comparison(
        base_tree='a' * 40, candidate_tree='b' * 40,
        baseline_checks=[dict(_PASSING)], candidate_checks=[dict(_PASSING)],
        verdict='NO_FAILURES')
    assert record['performed'] is True
    assert record['reason'] == 'COMPARED'
    assert record['detail'] is None


# ------------------------------------- R1: delta details on success paths
def test_successful_comparison_records_empty_delta_explicitly(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 7\n'})
    outcome, payload = _collect(repo, tmp_path, worker, base=base,
                                baseline_journal=_journal([_PASSING]))
    assert payload is not None
    assert payload['comparison'] == {**payload['comparison'],
                                     'performed': True, 'reason': 'COMPARED',
                                     'verdict': 'NO_FAILURES'}
    assert payload['baseline_failures'] == []
    assert payload['current_failures'] == []
    assert payload['delta_failures'] == []
    assert outcome['delta_failures'] == []


def test_successful_comparison_keeps_baseline_failures(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 8\n'})
    _outcome, payload = _collect(repo, tmp_path, worker, base=base,
                                 baseline_journal=_journal([_FAILING]))
    assert payload is not None
    assert payload['comparison']['verdict'] == 'NO_FAILURES'
    assert [f['location'] for f in payload['baseline_failures']] == [
        'src/mod.py::test_x']
    assert payload['delta_failures'] == []


def test_success_without_comparison_omits_delta_keys(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_PASSING])
    _outcome, payload = _collect(repo, tmp_path,
                                 _worker({'src/mod.py': 'VALUE = 9\n'}))
    assert payload is not None
    assert payload['comparison']['reason'] in ('BASE_EQUALS_TARGET',
                                              'NOT_COMPARED')
    assert 'baseline_failures' not in payload    # uncomputed, not empty
    assert 'delta_failures' not in payload


def test_pre_existing_only_semantics_preserved(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_FAILING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 10\n'})
    outcome, payload = _collect(repo, tmp_path, worker, base=base,
                                baseline_journal=_journal([_FAILING]))
    assert outcome['state'] == 'DONE'
    assert outcome['verdict'] == 'PRE_EXISTING_ONLY'
    assert outcome['delta_failures'] == []
    assert payload is not None
    assert payload['verdict'] == 'PRE_EXISTING_ONLY'
    assert payload['comparison']['performed'] is True
    assert payload['comparison']['verdict'] == 'PRE_EXISTING_ONLY'


# ------------------------------------- D1: attribution gates DONE/FAILED
def test_same_tree_failures_do_not_authorize_done(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_FAILING])
    base = _git(repo, 'rev-parse', 'HEAD')
    outcome, payload = _collect(repo, tmp_path, _worker(), base=base,
                                baseline_journal=_journal([_FAILING]))
    assert outcome['verdict'] == 'PRE_EXISTING_ONLY'
    assert outcome['comparison']['reason'] == 'BASE_EQUALS_TARGET'
    assert outcome['state'] == 'FAILED'
    assert outcome['evidence']                      # sealed, not dropped
    assert payload is not None and payload['checks'][0]['status'] == 'failed'


def test_inventory_mismatch_blocks_pre_existing_excuse(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_FAILING])
    base = _git(repo, 'rev-parse', 'HEAD')
    baseline = [dict(_FAILING),
                {'name': 'ruff', 'status': 'passed', 'exit_code': 0,
                 'output_tail': 'All checks passed!\n'}]
    worker = _worker({'src/mod.py': 'VALUE = 11\n'})
    outcome, payload = _collect(repo, tmp_path, worker, base=base,
                                baseline_journal=_journal(baseline))
    assert outcome['verdict'] == 'PRE_EXISTING_ONLY'   # identities identical
    assert outcome['comparison']['reason'] == 'INVENTORY_MISMATCH'
    assert outcome['comparison']['performed'] is False
    assert outcome['state'] == 'FAILED'
    assert payload is not None


def test_valid_pre_existing_only_still_done(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checks(monkeypatch, [_FAILING])
    base = _git(repo, 'rev-parse', 'HEAD')
    worker = _worker({'src/mod.py': 'VALUE = 12\n'})
    outcome, _payload = _collect(repo, tmp_path, worker, base=base,
                                 baseline_journal=_journal([_FAILING]))
    assert outcome['comparison']['performed'] is True
    assert outcome['state'] == 'DONE'


# ------------------------------------------- D2: baseline scope forwarded
def test_baseline_scope_equal_permits_comparison() -> None:
    record = we.build_comparison(
        base_tree='a' * 40, candidate_tree='b' * 40,
        baseline_checks=[dict(_PASSING)], candidate_checks=[dict(_PASSING)],
        baseline_scope='S0', candidate_scope='S0', verdict='NO_FAILURES')
    assert record['performed'] is True
    assert record['reason'] == 'COMPARED'


def test_baseline_scope_unknown_is_not_fabricated() -> None:
    record = we.build_comparison(
        base_tree='a' * 40, candidate_tree='b' * 40,
        baseline_checks=[dict(_PASSING)], candidate_checks=[dict(_PASSING)],
        baseline_scope=None, candidate_scope='S2')
    assert record['performed'] is True        # unknown scope is not a guess
    assert record['reason'] == 'COMPARED'


def test_dag_baseline_reuses_candidate_decision(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from asha.governance.dag import run_workers_dag

    _checks(monkeypatch, [_PASSING])
    captured: dict[str, Any] = {}
    real = we.deferred_baseline_checks

    def _spy(worker: dict[str, Any], path: Any, base: str,
             base_tree: str, repo_: Path, task_id: str,
             resolved: dict[str, Any], decision: Any,
             **kw: Any) -> dict | None:
        captured.update(base=base, base_tree=base_tree, resolved=resolved,
                        decision=decision, repo=repo_)
        captured['journal'] = real(worker, path, base, base_tree, repo_,
                                   task_id, resolved, decision, **kw)
        return captured['journal']

    monkeypatch.setattr(we, 'deferred_baseline_checks', _spy)
    monkeypatch.setenv('ASHA_STATE_DIR', str(tmp_path / 'state'))
    worker = _worker()
    worker.pop('_test_files', None)
    report = run_workers_dag(repo, [worker], task_id='t-forward')
    assert report['states']['w1']['state'] in ('DONE', 'FAILED')
    journal = captured['journal']
    assert isinstance(journal, dict) and 'resolved' in journal
    assert 'checks' in journal
    # the baseline ran the CANDIDATE's decision, not a fresh canonical one
    assert journal['resolved'] is captured['resolved']
    assert journal['decision_mode'] == getattr(captured['decision'], 'mode',
                                               None)
    assert captured['base_tree'] == journal['base_tree']


# ------------------------- D1 <-> CLI: blocked run reports a real failure
def test_cli_reports_blocked_run_with_sealed_evidence(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: Any) -> None:
    sealed = evidence.seal({
        'schema': evidence.SCHEMA, 'stage': 'worker', 'scope': 'S0',
        'commit': 'a' * 40, 'tree_hash': 'a' * 40,
        'observed_at': evidence.now_iso(), 'checks': [dict(_FAILING)],
        'authorized_to_ship': False, 'target_tree_sha': 'a' * 40,
        'comparison': {'performed': False, 'baseline_tree': 'a' * 40,
                       'candidate_tree': 'a' * 40, 'comparable': False,
                       'reason': 'BASE_EQUALS_TARGET', 'verdict': None,
                       'detail': None},
        'acceptance': {'command': None, 'supplied': False, 'ran': False,
                       'exit_code': None, 'outcome': 'not_required'},
    })
    path = tmp_path / 'blocked.json'
    path.write_text(json.dumps(sealed), encoding='utf-8')

    def _report(root: Any, worker: dict[str, Any], **kw: Any) -> dict:
        return {'states': {worker['id']: {
                    'state': 'FAILED', 'reason': 'verification_failed:pytest',
                    'evidence': str(path), 'comparison':
                    sealed['comparison']}},
                'evidence': {worker['id']: str(path)},
                'authoritative': {worker['id']: b'record'}}

    monkeypatch.setattr(cli, 'governance_profile', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'route',
                        lambda *a, **k: __import__('types').SimpleNamespace(
                            mode=cli.RuntimeMode.FAST_PATH))
    monkeypatch.setattr(cli, 'run_worker_in_worktree', _report)
    monkeypatch.setattr(cli, 'verify_worker_evidence', lambda *a, **k: True)
    monkeypatch.setattr(cli, 'parse_evidence', lambda raw: raw)
    monkeypatch.setattr(cli, 'verify_record',
                        lambda parsed: type('R', (), {'verified': True})())
    monkeypatch.setattr(cli, 'verify_bytes',
                        lambda raw: type('R', (), {'verified': True})())

    result = cli._run_sealed(repo, ['src/mod.py'], classification=None)
    assert result['validation_result'] == 'FAIL'
    assert result['evidence_id'] == sealed['evidence_sha256']
    assert result['comparison']['reason'] == 'BASE_EQUALS_TARGET'
    assert result['error'] is None
