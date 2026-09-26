"""Phase 5.2.2 -- CLI contract tests (spec section 10).

Invocation classes, stream hygiene, JSON schema, every documented exit
code, evidence propagation, and property invariants A-E. Execution
boundaries are mocked where noted so the suite stays fast; the dogfood
(Section 11) exercises the real sealed path end to end.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from asha import cli
from asha.ast_indexer import clear_session_cache, index_module, prime_session_cache
from asha.evidence import IndependenceClassification


class _FakeClassification:
    classification = IndependenceClassification.PROVEN_DISJOINT
    reason_code = 'test_fixture'
    confidence_basis = 'test'
    signals: tuple = ()


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(
            f'git {args} failed: {proc.stderr.strip()}')
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'tests').mkdir(parents=True)
    _git(root, 'init', '-q', '.')
    (root / '.gitignore').write_text('.jspace/\n', encoding='utf-8')
    _git(root, 'config', 'user.email', 't@example.invalid')
    _git(root, 'config', 'user.name', 'test')
    (root / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    return root


def _run(root: Path, *flags: str, capsys: Any) -> tuple[int, str, str]:
    code = cli.main(['--root', str(root), *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _direct(root: Path) -> tuple[str, bool, str]:
    _resolved, decision, _cls = cli.evaluate(root, None)
    assert decision is not None
    return decision.mode, decision.eligible, decision.fallback_reason


# ---------------------------------------------------------------- streams

def test_no_changes_human(repo: Path, capsys: Any) -> None:
    code, out, err = _run(repo, capsys=capsys)
    assert code == cli.EXIT_OK
    assert out == 'Asha\nNo changes detected.\n'
    assert err == ''


def test_no_changes_json_status(repo: Path, capsys: Any) -> None:
    code, out, err = _run(repo, '--json', capsys=capsys)
    payload = json.loads(out)          # exactly one document, no filtering
    assert code == cli.EXIT_OK
    assert err == ''
    assert payload['status'] == 'NO_CHANGES'
    assert payload['decision'] is None          # no fabricated verdict
    assert payload['schema_version'] == 1


def test_json_stdout_is_pure_single_document(repo: Path,
                                             capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# dirty\n', encoding='utf-8')
    code, out, err = _run(repo, '--json', '--no-execute', capsys=capsys)
    payload = json.loads(out)          # raises if any log line leaked
    assert code == cli.EXIT_OK
    assert err == ''
    assert out.lstrip().startswith('{')
    assert payload['error'] is None


# ------------------------------------------------------- invariant A (1/2)

def test_invariant_a_cli_equals_direct_engine(repo: Path,
                                              capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change\n', encoding='utf-8')
    expected = _direct(repo)
    code, out, _err = _run(repo, '--json', '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert (payload['decision'], payload['eligible'],
            payload['fallback_reason'] or '') == expected


# ------------------------------------------------------- invariant B

def test_invariant_b_presentation_never_changes_decision(
        repo: Path, capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change\n', encoding='utf-8')
    code_h, out_h, _ = _run(repo, '--no-execute', capsys=capsys)
    code_j, out_j, _ = _run(repo, '--json', '--no-execute',
                            capsys=capsys)
    payload = json.loads(out_j)
    assert code_h == code_j
    # human block carries the same semantic trio as the JSON
    assert f'Decision      {payload["decision"]}' in out_h
    assert '\x1b' not in out_h and '\x1b' not in out_j  # no color channel
    if payload['fallback_reason']:
        assert f'Reason        {payload["fallback_reason"]}' in out_h


# ------------------------------------------------------- invariant C

def test_invariant_c_cache_states_same_decision(repo: Path,
                                                capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change\n', encoding='utf-8')
    decisions = []
    import os
    for state in ('cold', 'warm', 'recovered'):
        clear_session_cache()
        if state == 'warm':
            # seed the session memo with the repo's real index
            text = (repo / 'tests' / 'test_thing.py').read_text(
                encoding='utf-8')
            prime_session_cache([('tests.test_thing', text,
                                  index_module('tests.test_thing', text))])
        if state == 'recovered':
            junk = repo / 'cli-cache-junk'
            junk.mkdir(exist_ok=True)
            (junk / 'graph.json').write_text('{broken', encoding='utf-8')
            os.environ['ASHA_GRAPH_CACHE_DIR'] = str(junk)
        code, out, _err = _run(repo, '--json', '--no-execute',
                               capsys=capsys)
        os.environ.pop('ASHA_GRAPH_CACHE_DIR', None)
        assert code == cli.EXIT_OK
        payload = json.loads(out)
        decisions.append((payload['decision'], payload['eligible'],
                          payload['fallback_reason']))
        clear_session_cache()
    assert decisions[0] == decisions[1] == decisions[2]


# ------------------------------------------------------- invariant D

def test_invariant_d_complete_stays_complete(repo: Path,
                                             capsys: Any) -> None:
    flows = repo / '.github' / 'workflows'
    flows.mkdir(parents=True)
    ci = flows / 'ci.yml'
    ci.write_text('name: ci\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'ci')
    ci.write_text('name: ci\non: push\n', encoding='utf-8')
    runs = [(('--no-execute',), 'human'), (('--json', '--no-execute'),
            'json'), (('--json',), 'json-refusing-exec')]
    for flags, kind in runs:
        code, out, _err = _run(repo, *flags, capsys=capsys)
        if kind == 'json-refusing-exec':
            # uncommitted canonical target may refuse execution (exit 2);
            # the INVARIANT is the verdict, never the exit path
            assert code in (cli.EXIT_OK, cli.EXIT_ERROR)
            payload = json.loads(out)
            if code == cli.EXIT_ERROR:
                assert payload['error'] == (
                    'EXECUTION_REQUIRES_COMMITTED_TARGET')
            assert payload['decision'] == 'COMPLETE'
            assert payload['eligible'] is False
            assert payload['execution_mode'] == 'canonical'
            continue
        assert code == cli.EXIT_OK
        if kind == 'human':
            assert 'Decision      COMPLETE' in out
            continue
        payload = json.loads(out)
        assert payload['decision'] == 'COMPLETE'
        assert payload['eligible'] is False
        assert payload['execution_mode'] == 'canonical'


# ------------------------------------------------------- invariant E

def test_invariant_e_verification_failure_never_renders_pass(
        repo: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # committed clean target so the sealed path actually executes
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# dirty\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'target')
    evidence_file = repo.parent / 'ev.json'   # outside the repo: the
    evidence_file.write_text(json.dumps(       # target must stay clean
        {'checks': [{'name': 'pytest', 'status': 'passed'}],
         'evidence_sha256': 'f' * 64}), encoding='utf-8')

    class _FakeScheduler:
        def __init__(self, _repo: Any, workers: Any, **_kwargs: Any
                     ) -> None:
            self._wid = workers[0]['id']
            self.authoritative = {self._wid: b'{}'}

        def run(self) -> dict[str, Any]:
            return {'states': {self._wid: {'state': 'DONE'}},
                    'evidence': {self._wid: str(evidence_file)},
                    'worktrees': {}}

    def _fake_verify_record(_record: Any) -> Any:
        class R:
            verified = True
        return R()

    monkeypatch.setattr(cli, 'GovernedScheduler', _FakeScheduler)
    monkeypatch.setattr(cli, 'verify_worker_evidence', lambda *_a, **_k: True)
    monkeypatch.setattr(cli, 'parse_evidence', lambda _b: object())
    monkeypatch.setattr(cli, 'verify_record', _fake_verify_record)
    monkeypatch.setattr(cli, 'verify_bytes', lambda _b: type(
        'R', (), {'verified': True})())

    # happy path: everything verifies
    code, out, _err = _run(repo, '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['evidence_verification'] == 'PASS'
    assert payload['replay_verification'] == 'PASS'

    # seal breaks: must surface an error, never PASS
    monkeypatch.setattr(cli, 'verify_worker_evidence', lambda *_a, **_k: False)
    code2, out2, _err2 = _run(repo, '--json', capsys=capsys)
    assert code2 == cli.EXIT_ERROR
    p2 = json.loads(out2)
    assert p2['evidence_verification'] != 'PASS'
    assert p2['error'] == 'EVIDENCE_VERIFICATION_FAILED'


# ------------------------------------------------------- exit classes

def test_exit_2_not_a_repository(tmp_path: Path, capsys: Any) -> None:
    plain = tmp_path / 'plain'
    plain.mkdir()
    code, out, _err = _run(plain, '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'REPOSITORY_ERROR'
    assert payload['decision'] is None
    # human mode: diagnostics on stderr only
    code2, out2, err2 = _run(plain, capsys=capsys)
    assert code2 == cli.EXIT_ERROR
    assert out2 == ''
    assert err2.startswith('Asha: ')


def test_exit_1_validation_failed(repo: Path, capsys: Any,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# dirty\n', encoding='utf-8')
    monkeypatch.setattr(cli, 'execute', lambda *_a, **_k: {
        'validation_result': 'FAIL', 'evidence_id': None,
        'evidence_verification': None, 'replay_verification': None,
        'error': None})
    code, out, _err = _run(repo, '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_VALIDATION_FAILED
    assert payload['validation_result'] == 'FAIL'
    assert payload['error'] is None


def test_exit_0_validation_pass(repo: Path, capsys: Any,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# dirty\n', encoding='utf-8')
    monkeypatch.setattr(cli, 'execute', lambda *_a, **_k: {
        'validation_result': 'PASS', 'evidence_id': 'e' * 64,
        'evidence_verification': 'PASS', 'replay_verification': 'PASS',
        'error': None})
    code, out, _err = _run(repo, '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['validation_result'] == 'PASS'
    assert payload['evidence_id'] == 'e' * 64


def test_exit_130_interrupted_never_passes(repo: Path, capsys: Any,
                                           monkeypatch: pytest.MonkeyPatch
                                           ) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# dirty\n', encoding='utf-8')

    def _interrupt(*_a: Any, **_k: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, 'execute', _interrupt)
    code, out, err = _run(repo, capsys=capsys)
    assert code == cli.EXIT_INTERRUPTED
    assert out == ''                       # nothing reported as PASS
    assert err.startswith('Asha: interrupted')


# ------------------------------------------------------- execution paths

def test_dirty_canonical_refuses_without_commit(repo: Path,
                                                capsys: Any) -> None:
    flows = repo / '.github' / 'workflows'
    flows.mkdir(parents=True)
    ci = flows / 'ci.yml'
    ci.write_text('name: ci\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'ci')
    ci.write_text('name: ci\non: push\n', encoding='utf-8')
    code, out, err = _run(repo, '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'EXECUTION_REQUIRES_COMMITTED_TARGET'
    assert payload['decision'] == 'COMPLETE'      # verdict still shown
    assert payload['validation_result'] is None
    assert 'commit the change' in err


def test_scoped_dirty_refuses_execution_keeps_verdict(
        repo: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 6.5.5: the CLI never becomes an alternate runtime path.
    An eligible verdict on an uncommitted target still evaluates to
    SCOPED, but execution is refused (single authority = scheduler)."""
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# dirty\n', encoding='utf-8')
    monkeypatch.setattr(cli, 'classify_task',
                        lambda *_a, **_k: _FakeClassification())
    monkeypatch.setattr(cli, '_dispatch_context', lambda _r: ({'id': 'p'},))
    code, out, err = _run(repo, '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['decision'] == 'SCOPED'
    assert payload['eligible'] is True
    assert payload['execution_mode'] == 'targeted'
    assert payload['error'] == 'EXECUTION_REQUIRES_COMMITTED_TARGET'
    assert payload['validation_result'] is None
    assert 'commit the change' in err


def test_committed_target_executes_via_scheduler_authority(
        repo: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The only execution path: one validation worker through
    GovernedScheduler (evidence fields propagate)."""
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# dirty\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'target')
    evidence_file = repo.parent / 'ev.json'   # outside the repo: the
    evidence_file.write_text(json.dumps(       # target must stay clean
        {'checks': [{'name': 'pytest', 'status': 'passed'}],
         'evidence_sha256': 'a' * 64}), encoding='utf-8')
    created: dict[str, Any] = {}

    class _FakeScheduler:
        def __init__(self, _repo: Any, workers: Any, **kwargs: Any) -> None:
            created['workers'] = workers
            created['kwargs'] = kwargs
            self.authoritative = {workers[0]['id']: b'{}'}

        def run(self) -> dict[str, Any]:
            wid = created['workers'][0]['id']
            return {'states': {wid: {'state': 'DONE'}},
                    'evidence': {wid: str(evidence_file)},
                    'worktrees': {}}

    monkeypatch.setattr(cli, 'GovernedScheduler', _FakeScheduler)
    monkeypatch.setattr(cli, 'verify_worker_evidence', lambda *_a, **_k: True)
    monkeypatch.setattr(cli, 'parse_evidence', lambda _b: object())
    monkeypatch.setattr(cli, 'verify_record',
                        lambda _r: type('R', (), {'verified': True})())
    monkeypatch.setattr(cli, 'verify_bytes',
                        lambda _b: type('R', (), {'verified': True})())
    code, out, _err = _run(repo, '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['validation_result'] == 'PASS'
    assert payload['evidence_id'] == 'a' * 64
    assert payload['evidence_verification'] == 'PASS'
    assert payload['replay_verification'] == 'PASS'
    worker = created['workers'][0]
    assert worker['writes'] == []
    assert worker['reads'] == worker['declared_scope']
    assert created['kwargs']['classification_context'] == (
        cli._dispatch_context(repo))


# ---------------------------------------- 5.2.3 git-aware discovery

def _make_conflict(root: Path) -> None:
    _git(root, 'checkout', '-qb', 'side')
    (root / 'c.py').write_text('def f():\n    return 2\n', encoding='utf-8')
    _git(root, 'commit', '-qam', 'side')
    _git(root, 'checkout', '-q', 'main')
    (root / 'c.py').write_text('def f():\n    return 3\n', encoding='utf-8')
    _git(root, 'commit', '-qam', 'main2')
    _git(root, 'merge', 'side', check=False)   # conflicts exit non-zero


def test_conflict_state_is_operational_error(tmp_path: Path,
                                             capsys: Any) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main', '.')
    _git(root, 'config', 'user.email', 't@example.invalid')
    _git(root, 'config', 'user.name', 'test')
    (root / 'c.py').write_text('def f():\n    return 1\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    _make_conflict(root)
    assert _git(root, 'ls-files', '-u')          # genuinely unmerged

    code, out, err = _run(root, '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'REPOSITORY_CONFLICT'
    assert payload['decision'] is None           # no synthesized verdict
    assert payload['eligible'] is None
    assert 'unmerged paths' in err

    code2, out2, _err2 = _run(root, capsys=capsys)
    assert code2 == cli.EXIT_ERROR
    assert out2 == ''                            # human: stderr only


def test_semantic_equivalence_explicit_paths_vs_auto(repo: Path,
                                                     capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change\n', encoding='utf-8')
    (repo / 'extra.py').write_text('x = 1\n', encoding='utf-8')

    code_a, out_a, _ = _run(repo, '--json', '--no-execute', capsys=capsys)
    code_e, out_e, _ = _run(
        repo, '--paths', 'extra.py', 'tests/test_thing.py',
        '--json', '--no-execute', capsys=capsys)
    auto = json.loads(out_a)
    explicit = json.loads(out_e)
    assert code_a == code_e == cli.EXIT_OK
    mismatches = [
        field for field in ('decision', 'eligible', 'fallback_reason',
                            'execution_mode')
        if auto[field] != explicit[field]
    ]
    assert mismatches == []
    assert sorted(auto['changed_files']) == sorted(explicit['changed_files'])


def test_deleted_file_verdict_is_engine_owned(repo: Path,
                                              capsys: Any) -> None:
    (repo / 'gone.py').write_text('def gone():\n    return 0\n',
                                  encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'add target')
    # move the diff base past gone.py so the deletion is a net change
    (repo / 'anchor.py').write_text('a = 1\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'anchor')
    (repo / 'gone.py').unlink()                  # uncommitted deletion

    code, out, _err = _run(repo, '--json', '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert 'gone.py' in payload['changed_files']
    states = payload['change_set']['states']
    assert 'deleted' in states['gone.py']
    # the verdict comes from the engine, byte-identical to a direct
    # engine call on the same target -- the CLI never branches on state
    _res, decision, _cls = cli.evaluate(repo, None)
    assert decision is not None
    assert payload['decision'] == decision.mode
    assert payload['eligible'] == decision.eligible
    assert payload['fallback_reason'] == decision.fallback_reason


def test_deleted_file_through_runtime_authority(tmp_path: Path,
                                                capsys: Any) -> None:
    """A COMMITTED deletion executed through the real scheduler path:
    the single authority evaluates the change itself."""
    root = tmp_path / 'auth'
    (root / 'tests').mkdir(parents=True)
    _git(root, 'init', '-q', '-b', 'main', '.')
    _git(root, 'config', 'user.email', 't@example.invalid')
    _git(root, 'config', 'user.name', 'test')
    (root / '.gitignore').write_text('.jspace/\n', encoding='utf-8')
    (root / 'keep.py').write_text('def keep():\n    return 1\n',
                                  encoding='utf-8')
    (root / 'gone.py').write_text('def gone():\n    return 0\n',
                                  encoding='utf-8')
    (root / 'tests' / 'test_keep.py').write_text(
        'import keep\n\ndef test_keep():\n    assert keep.keep() == 1\n',
        encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    (root / 'gone.py').unlink()
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'delete target')

    _res, direct, _cls = cli.evaluate(root, None)
    assert direct is not None                    # engine sees the change

    code, out, _err = _run(root, '--json', capsys=capsys)
    payload = json.loads(out)
    assert payload['decision'] == direct.mode    # engine-owned, unchanged
    assert payload['eligible'] == direct.eligible
    assert code in (cli.EXIT_OK, cli.EXIT_VALIDATION_FAILED)
    assert payload['validation_result'] in ('PASS', 'FAIL', None)
    if payload['validation_result'] == 'PASS':
        assert payload['evidence_verification'] in ('PASS', 'FAIL')


def test_change_set_snapshot_states(repo: Path, capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change\n', encoding='utf-8')
    _git(repo, 'add', 'tests/test_thing.py')     # staged only
    (repo / 'staged.py').write_text('x = 1\n', encoding='utf-8')
    (repo / 'loose.py').write_text('y = 2\n', encoding='utf-8')
    _git(repo, 'add', 'staged.py')
    # loose.py stays untracked; test_thing has a further unstaged edit
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change2\n', encoding='utf-8')
    code, out, _err = _run(repo, '--json', '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    states = payload['change_set']['states']
    assert states['tests/test_thing.py'] == ['staged', 'unstaged']
    assert states['staged.py'] == ['staged']
    assert states['loose.py'] == ['untracked']
    assert payload['change_set']['untracked'] == ['loose.py']
    assert payload['change_set']['conflicts'] == []
    # single snapshot: uncommitted count matches the per-path states
    assert payload['change_set']['uncommitted_files'] == len(states)


def test_paths_outside_repository_operational_error(repo: Path,
                                                    capsys: Any) -> None:
    code, out, err = _run(repo, '--paths', str(repo.parent / 'x.py'),
                          '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'REPOSITORY_ERROR'
    assert 'outside the repository' in err


def test_auto_discovery_from_subdirectory(repo: Path,
                                          capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change\n', encoding='utf-8')
    sub = repo / 'tests'
    code = cli.main(['--root', str(sub), '--json', '--no-execute'])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == cli.EXIT_OK
    assert payload['change_set'] is not None     # toplevel discovered
    assert sorted(payload['changed_files']) == ['tests/test_thing.py']


def test_no_color_flag_and_no_ansi(repo: Path,
                                   capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change\n', encoding='utf-8')
    code_a, out_a, _ = _run(repo, '--no-color', '--no-execute',
                            capsys=capsys)
    code_b, out_b, _ = _run(repo, '--no-execute', capsys=capsys)
    assert code_a == code_b == cli.EXIT_OK
    assert '\x1b' not in out_a and '\x1b' not in out_b
    assert 'Decision      ' in out_a and 'Decision      ' in out_b


def test_git_context_reports_facts_only() -> None:
    """Discovery module carries zero governance vocabulary and its
    snapshot object is immutable after normalization."""
    import dataclasses

    from asha import git_context as gc

    source = (Path(gc.__file__)).read_text(encoding='utf-8').lower()
    for token in ('scoped', 'complete', 'eligible', 'fallback_reason',
                  'scopingdecision'):
        assert token not in source, token
    snap = object.__new__(gc.WorkingState)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.paths = ()          # type: ignore[misc]


# ------------------------------------------------------- legacy + schema

def test_main_module_import_has_no_side_effect(tmp_path: Path) -> None:
    """Regression: an unguarded SystemExit(main()) in asha/__main__.py
    ran the whole CLI (417s canonical execution) on plain import and
    killed any importing process with exit 0."""
    import sys as _sys
    proc = subprocess.run(
        [_sys.executable, '-c',
         'import asha.__main__; print("IMPORT_OK")'],
        cwd=tmp_path, capture_output=True, text=True, check=False,
        encoding='utf-8', errors='replace', timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == 'IMPORT_OK\n'   # nothing else may be emitted
    assert 'Asha' not in proc.stdout


def test_legacy_run_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def _sched(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(cli, 'scheduler_main', _sched)
    assert cli.main(['run', '--spec', 'x.json']) == 0
    assert seen == [['run', '--spec', 'x.json']]
    # --paths keeps an explicitly named path in our mode
    assert cli._legacy_run(['--paths', 'run']) is False
    assert cli._legacy_run(['--root', 'x', 'run', '--spec', 'y']) is True


def test_json_schema_contract(repo: Path, capsys: Any) -> None:
    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# dirty\n', encoding='utf-8')
    _code, out, _err = _run(repo, '--json', '--no-execute',
                            capsys=capsys)
    payload = json.loads(out)
    required = {'schema_version', 'repository', 'change_set',
                'changed_files', 'decision', 'eligible', 'fallback_reason',
                'execution_mode', 'validation_result', 'duration_ms',
                'evidence_id', 'evidence_verification',
                'replay_verification', 'error', 'status'}
    assert required <= set(payload)
    assert payload['schema_version'] == 1
    # no absolute local paths anywhere in the payload
    assert str(repo) not in out
    assert isinstance(payload['duration_ms'], int)
    assert payload['duration_ms'] >= 0
