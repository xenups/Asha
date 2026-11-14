"""Trusted verification profiles (0.7.4).

The limitation these pin down: the S3 matrix is repository-agnostic, so a
project that configures neither ruff nor mypy is permanently BLOCKED, and a
missing executable is indistinguishable from a project that never declared
the check. A profile makes that declaration explicit, operator-owned and
digest-bound -- and a NOT_CONFIGURED entry is never a pass.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from asha import check_runner, validation
from asha.governance import dag
from asha.governance import worker_execution as we
from asha.verification_profile import (
    NOT_CONFIGURED,
    PROFILE_ENV,
    ProfileError,
    load,
    load_selected,
    statement,
    verify_unchanged,
)

REMOTE = 'git@github.com:void-detailist/detailist.git'


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
    _git(root, 'config', 'commit.gpgsign', 'false')
    _git(root, 'remote', 'add', 'origin', REMOTE)
    (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n',
                                     encoding='utf-8')
    (root / 'src' / 'mod.py').write_text('VALUE = 1\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    return root


def _profile_file(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        'schema': 1,
        'id': 'detailist',
        'repository': {
            'remotes': [REMOTE],
            'ci_workflows': ['.github/workflows/ci.yml'],
        },
        'services': {
            '.': {
                'root': '.',
                'checks': {'pytest': ['-m', 'pytest', 'tests', '-q']},
                'not_configured': {
                    'ruff': 'no [tool.ruff] in any pyproject; CI runs no ruff',
                    'mypy': 'no mypy config; CI runs no mypy',
                },
            },
        },
        'provenance': {'declared_by': 'operator', 'reviewed': '2026-11-14'},
    }
    payload.update(overrides)
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def _entries(**statuses: str) -> list[dict[str, Any]]:
    return [{'name': name, 'status': status, 'launched': True}
            for name, status in statuses.items()]


# ------------------------------------------------------------- default policy
def test_default_matrix_is_unchanged_without_a_profile(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_selected(repo, {}) is None
    captured: list[Any] = []

    def fake_group(root, names, changed, label, timeout_s, deps_root,
                   deadline=None, env_overrides=None, plan=None):
        captured.append((names, plan))
        return _entries(pytest='passed')

    monkeypatch.setattr(check_runner, '_run_group', fake_group)
    resolved = {'checks': ['ruff', 'pytest', 'mypy'],
                'affected_files': ['src/mod.py']}
    check_runner.run(repo, resolved)
    assert captured and captured[0][0] == ['ruff', 'pytest', 'mypy']
    assert captured[0][1] is None            # scope matrix, not a profile


# --------------------------------------------------------- profile semantics
def test_not_configured_entries_are_recorded_not_passed(tmp_path: Path) -> None:
    profile = load(_profile_file(tmp_path))
    plan = profile.service_for('.')
    entries = check_runner._not_configured_entries(
        plan, '.', sys.executable)
    assert [entry['status'] for entry in entries] == [NOT_CONFIGURED] * 2
    assert entries[0]['required'] is False
    assert 'tool_ruff' not in json.dumps(entries[0])   # provenance, not a probe
    provenance = {name: text for name, text in plan.not_configured}
    assert provenance['ruff'].startswith('no [tool.ruff]')
    state, rule = validation.validation_status(
        entries + _entries(pytest='passed'))
    assert state == validation.VALIDATION_PASS
    assert 'not configured: mypy, ruff' in rule


def test_not_configured_alone_verifies_nothing(tmp_path: Path) -> None:
    profile = load(_profile_file(tmp_path))
    entries = check_runner._not_configured_entries(
        profile.service_for('.'), '.', sys.executable)
    state, rule = validation.validation_status(entries)
    assert state == validation.VALIDATION_BLOCKED
    assert rule.startswith('no required check')


def test_missing_required_tool_stays_blocked() -> None:
    """A required executable that could not run is BLOCKED, never
    NOT_CONFIGURED: only a signed declaration is unconfigured."""
    state, rule = validation.validation_status(
        _entries(pytest='skipped'))
    assert state == validation.VALIDATION_BLOCKED
    assert 'not executed: pytest' in rule


def test_required_failing_command_is_fail() -> None:
    state, rule = validation.validation_status(_entries(pytest='failed'))
    assert state == validation.VALIDATION_FAIL
    assert rule == 'failed: pytest'


# ------------------------------------------------------------ trust rules
def test_empty_profile_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / 'empty.json'
    path.write_text('', encoding='utf-8')
    with pytest.raises(ProfileError):
        load(path)


@pytest.mark.parametrize('bad', [
    {'schema': 2},
    {'id': ''},
    {'repository': {}},
    {'repository': {'remotes': []}},
    {'services': {}},
    {'services': {'.': {'checks': {}}}},
    {'services': {'.': {'checks': {'pytest': ['-m', 'pytest']},
                        'not_configured': {'pytest': 'both'}}}},
])
def test_invalid_profiles_fail_closed(tmp_path: Path, bad: dict[str, Any]
                                      ) -> None:
    with pytest.raises(ProfileError):
        load(_profile_file(tmp_path, **bad))


def test_repository_identity_mismatch_fails_closed(
        tmp_path: Path, repo: Path) -> None:
    path = _profile_file(tmp_path, repository={
        'remotes': ['git@github.com:someone/else.git']})
    with pytest.raises(ProfileError, match='identity mismatch'):
        load_selected(repo, {PROFILE_ENV: str(path)})


def test_remote_url_forms_are_one_identity(tmp_path: Path, repo: Path) -> None:
    path = _profile_file(tmp_path, repository={
        'remotes': ['https://github.com/void-detailist/detailist']})
    assert load_selected(repo, {PROFILE_ENV: str(path)}) is not None


def test_profile_cannot_be_replaced_after_snapshot(tmp_path: Path) -> None:
    path = _profile_file(tmp_path)
    profile = load(path)
    verify_unchanged(profile)                      # unchanged: no raise
    path.write_text(path.read_text(encoding='utf-8').replace(
        'operator', 'someone-else'), encoding='utf-8')
    with pytest.raises(ProfileError, match='changed during the run'):
        verify_unchanged(profile)


def test_worker_spec_cannot_weaken_the_matrix(
        tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _profile_file(tmp_path)
    monkeypatch.setenv(PROFILE_ENV, str(path))
    captured: list[Any] = []

    def fake_group(root, names, changed, label, timeout_s, deps_root,
                   deadline=None, env_overrides=None, plan=None):
        captured.append(plan)
        return _entries(pytest='passed')

    monkeypatch.setattr(check_runner, '_run_group', fake_group)
    resolved = {
        'checks': ['pytest'],
        'affected_files': ['src/mod.py'],
        # a spec field that claims a different (weaker) matrix
        'verification_profile': {'services': {'.': {'checks': {}}}},
    }
    check_runner.run(repo, resolved)
    plan = captured[0]
    assert plan is not None
    assert [name for name, _ in plan.checks] == ['pytest']
    assert [name for name, _ in plan.not_configured] == ['mypy', 'ruff']


def test_undeclared_service_fails_closed(tmp_path: Path) -> None:
    profile = load(_profile_file(tmp_path))
    with pytest.raises(ProfileError, match='declares no checks'):
        profile.service_for('compiler')


def test_statement_records_digest_and_ci_sources(tmp_path: Path) -> None:
    path = _profile_file(tmp_path)
    profile = load(path)
    record = statement(profile)
    assert record['digest'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record['ci_workflows'] == ['.github/workflows/ci.yml']
    assert record['services']['.']['required'] == ['pytest']
    assert record['services']['.']['not_configured'] == ['mypy', 'ruff']


def test_dag_and_single_worker_share_one_collection_path() -> None:
    """Parity is structural: the DAG entrypoint seals through the same call
    the single-worker path uses, so profile results cannot diverge."""
    assert dag.worker_execution.collect_worker_evidence is (
        we.collect_worker_evidence)


# ------------------------------------------------------------- integration
def _worker(script: str) -> dict[str, Any]:
    return {'id': 'w1', 'deps': [], 'declared_scope': ['src/'],
            'reads': ['src/mod.py'], 'writes': ['src/mod.py'],
            'cmd': [sys.executable, '-c', script]}


def _collect(repo: Path, tmp_path: Path, worker: dict[str, Any]
             ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    rc, tail, _audit = we.execute_with_audit(worker, repo)
    head = _git(repo, 'rev-parse', 'HEAD')
    tree = _git(repo, 'rev-parse', 'HEAD^{tree}')
    outcome = we.collect_worker_evidence(
        worker, repo, rc, tail, 'task-vp', tmp_path / 'evidence',
        base=head, base_tree=tree)
    payload = None
    if outcome.get('evidence'):
        payload = json.loads(Path(outcome['evidence']).read_text(
            encoding='utf-8'))
    return outcome, payload


def test_profiled_run_seals_profile_digest_and_pass(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _profile_file(tmp_path)
    monkeypatch.setenv(PROFILE_ENV, str(path))
    monkeypatch.setattr(check_runner, 'run', lambda *a, **k: (
        _entries(pytest='passed') + check_runner._not_configured_entries(
            load(path).service_for('.'), '.', sys.executable)))
    monkeypatch.setattr(check_runner, 'run_scoped',
                        lambda *a, **k: pytest.fail('scoping must not narrow '
                                                    'a profiled run'))
    worker = _worker("import pathlib; p = pathlib.Path('src/mod.py');"
                     "p.write_text(p.read_text() + 'VALUE2 = 2\\n')")
    outcome, payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'DONE', outcome
    assert payload is not None
    assert payload['validation_status'] == 'PASS'
    assert payload['verification_profile']['id'] == 'detailist'
    assert payload['verification_profile']['digest'] == hashlib.sha256(
        path.read_bytes()).hexdigest()
    assert payload['authorized_to_ship'] is False


def test_failed_profile_check_cannot_pass(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _profile_file(tmp_path)
    monkeypatch.setenv(PROFILE_ENV, str(path))
    monkeypatch.setattr(check_runner, 'run',
                        lambda *a, **k: _entries(pytest='failed'))
    worker = _worker("import pathlib; p = pathlib.Path('src/mod.py');"
                     "p.write_text(p.read_text() + 'VALUE2 = 2\\n')")
    outcome, payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'FAILED'
    assert outcome['reason'].startswith('verification_failed')
    assert payload is None                      # a failed check seals nothing
