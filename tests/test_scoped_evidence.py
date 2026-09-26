"""Phase 5.0 -- Risk-Aware Scoped Evidence: implementation & safety tests.

Coverage map (Step 2 spec section 6/7):
  * positive path: proven-disjoint leaf -> SCOPED with deterministic
    targets, valid seal, byte-stable replay (6.1)
  * completeness-proof failures each force COMPLETE (6.2)
  * S3/S4 lockout (6.3), classification lockout (6.4)
  * fail-closed wiring: dumb executor, no run_scoped without a positive
    decision, forged/missing/raising decisions -> COMPLETE (6.5)
  * worker validation failure stays visible (6.6)
  * interruption/tamper resilience (6.7), env privacy (6.8)
  * adversarial proof-soundness fixtures (section 7)
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from asha import check_runner, evidence, replay, scoping
from asha.common import paths as common_paths
from asha.scheduler import GovernedScheduler, verify_worker_evidence
from asha.types import OrchestratorError

PY = sys.executable
GITIGNORE = '.jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n'

BASE_CORE = {
    '.gitignore': GITIGNORE,
    'README.md': '# scoped evidence fixture\n',
    'pyproject.toml': '[tool.ruff]\nline-length = 88\n',
    'pkg/__init__.py': '',
    'pkg/leaf.py': 'def leaf_value():\n    return 7\n',
    'tests/test_ok.py': 'def test_ok():\n    assert True\n',
    'tests/test_leaf_consumer.py': (
        'from pkg.leaf import leaf_value\n\n\n'
        'def test_leaf():\n    assert leaf_value() == 7\n'
    ),
}
# consumer of the worker-created module: no pytest import, stdlib-only
# footprint, so the poison scan can pass for genuinely leaf changes.
OUT_CONSUMER = (
    'from pkg.out import RESULT\n\n\n'
    'def test_out():\n    assert RESULT == 1\n'
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


def _repo(root: Path, files: dict[str, str] | None = None,
          git: bool = True) -> Path:
    repo = root / 'repo'
    repo.mkdir(parents=True, exist_ok=True)
    merged = dict(BASE_CORE)
    if files:
        merged.update(files)
    for rel, text in merged.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    if git:
        _git(repo, 'init', '-q', '-b', 'main')
        _git(repo, 'config', 'user.email', 'fixture@example.com')
        _git(repo, 'config', 'user.name', 'Fixture')
        _git(repo, 'config', 'commit.gpgsign', 'false')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-q', '-m', 'baseline')
    return repo


def _assess(repo: Path, changed: list[str], classification='PROVEN_DISJOINT',
            level='S1', status='certain', envelope=True):
    return scoping.assess_scoping_eligibility(
        repo, changed, classification, level,
        scope_status=status, envelope_valid=envelope)


# ---------------------------------------------------------------------------
# A. Positive path (spec 6.1)
# ---------------------------------------------------------------------------

def test_eligible_leaf_is_scoped_with_exact_targets(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.eligible is True
    assert decision.mode == scoping.SCOPED
    assert decision.fallback_reason == ''
    assert decision.targeted_tests == ('tests/test_leaf_consumer.py',)
    assert decision.mypy_targets == (
        'pkg/leaf.py', 'tests/test_leaf_consumer.py')
    assert 'complete_reverse_closure_proof' in decision.rationale
    # auditability: proof statistics, not prose
    assert any(r.startswith('indexed_modules=') for r in decision.rationale)


def test_decision_is_deterministic(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _assess(repo, ['pkg/leaf.py']) == _assess(repo, ['pkg/leaf.py'])


def test_proven_empty_closure_stays_eligible(tmp_path: Path) -> None:
    """A change nothing can observe may prove an EMPTY test set."""
    repo = _repo(tmp_path)
    # pkg/leaf.py has exactly one observer (test_leaf_consumer); change
    # an actually lonely file instead to prove an EMPTY target set:
    lonely = repo / 'pkg' / 'lonely.py'
    lonely.write_text('def _unused_helper():\n    return 1\n',
                      encoding='utf-8')
    decision = _assess(repo, ['pkg/lonely.py'])
    assert decision.eligible and decision.mode == scoping.SCOPED
    assert decision.targeted_tests == ()
    assert decision.mypy_targets == ('pkg/lonely.py',)
    assert 'targeted_tests=0' in decision.rationale


def test_changed_test_file_is_itself_targeted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['tests/test_leaf_consumer.py'])
    assert decision.eligible
    assert 'tests/test_leaf_consumer.py' in decision.targeted_tests


def test_stdlib_and_builtin_are_not_poison(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'pkg/util.py': 'import json\n\n\ndef dump(value):\n'
                       '    print(json.dumps(value))\n    return value\n',
        'tests/test_util_consumer.py': (
            'from pkg.util import dump\n\n\n'
            'def test_dump():\n    assert dump(1) == 1\n'
        ),
    })
    decision = _assess(repo, ['pkg/util.py'])
    assert decision.eligible, decision.fallback_reason
    assert decision.targeted_tests == ('tests/test_util_consumer.py',)


# ---------------------------------------------------------------------------
# B. Classification / S-level lockouts (spec 6.3, 6.4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('classification', [None, 'UNKNOWN', 'NONSENSE'])
def test_non_disjoint_classification_forces_complete(
        tmp_path: Path, classification) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['pkg/leaf.py'], classification=classification)
    assert decision.eligible is False
    assert decision.mode == scoping.COMPLETE
    assert decision.fallback_reason == scoping.F_UNKNOWN_CLASSIFICATION


def test_proven_shared_forces_complete(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['pkg/leaf.py'], classification='PROVEN_SHARED')
    assert decision.fallback_reason == scoping.F_PROVEN_SHARED


@pytest.mark.parametrize('level', ['S3', 'S4', '', 'S9'])
def test_forbidden_scope_level_lockout(tmp_path: Path, level) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['pkg/leaf.py'], level=level)
    assert decision.mode == scoping.COMPLETE
    assert decision.fallback_reason == scoping.F_FORBIDDEN_SCOPE_LEVEL


@pytest.mark.parametrize('status', ['uncertain', 'ambiguity', ''])
def test_uncertain_scope_status_forces_complete(
        tmp_path: Path, status) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['pkg/leaf.py'], status=status)
    assert decision.fallback_reason == scoping.F_SCOPE_STATUS_UNCERTAIN


def test_empty_change_set_forces_complete(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _assess(repo, []).fallback_reason == scoping.F_NO_CHANGED_FILES


def test_invalid_envelope_forces_complete(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['pkg/leaf.py'], envelope=False)
    assert decision.fallback_reason == scoping.F_INVALID_ENVELOPE


# ---------------------------------------------------------------------------
# C. Completeness-proof failures (spec 6.2)
# ---------------------------------------------------------------------------

def test_non_python_change_unprovable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['README.md'], level='S0')
    assert decision.mode == scoping.COMPLETE
    assert decision.fallback_reason == scoping.F_PYTEST_CLOSURE_UNPROVEN


def test_conftest_change_unprovable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / 'tests' / 'conftest.py').write_text('', encoding='utf-8')
    decision = _assess(repo, ['tests/conftest.py'])
    assert decision.fallback_reason == scoping.F_PYTEST_CLOSURE_UNPROVEN


def test_unindexed_change_unprovable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    decision = _assess(repo, ['pkg/does_not_exist.py'])
    assert decision.fallback_reason == scoping.F_PYTEST_CLOSURE_UNPROVEN
    assert 'not indexable' in decision.rationale[0]


def test_reachable_unresolved_forces_complete(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'tests/test_bad_consumer.py': (
            'from pkg.leaf import leaf_value\n\n\n'
            'def test_bad():\n    return mystery_name\n'
        ),
    })
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.mode == scoping.COMPLETE
    assert decision.fallback_reason == scoping.F_UNRESOLVED_BOUNDARY


def test_external_boundary_forces_complete(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'tests/test_ghost_consumer.py': (
            'import ghostpkg\nfrom pkg.leaf import leaf_value\n\n\n'
            'def test_ghost():\n    assert leaf_value() == 7\n'
        ),
    })
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.fallback_reason == scoping.F_EXTERNAL_BOUNDARY


def test_target_set_without_test_function_forces_complete(
        tmp_path: Path) -> None:
    """Regression (Step 2): a non-empty selection whose files define no
    pytest-collectable test would yield zero observations -- fall back
    to COMPLETE instead of emitting a skipped pytest entry."""
    # util has exactly ONE observer and it defines no tests: the whole
    # authorized selection would be unrunnable
    repo = _repo(tmp_path, {
        'pkg/util.py': 'def util_value():\n    return 1\n',
        'tests/consumer_helpers.py': (
            'from pkg.util import util_value\n\n\n'
            'def helper_for_util():\n    return util_value()\n'
        ),
    })
    decision = _assess(repo, ['pkg/util.py'])
    assert decision.mode == scoping.COMPLETE
    assert decision.fallback_reason == scoping.F_PYTEST_CLOSURE_UNPROVEN
    assert 'no test function' in decision.rationale[0]


# ---------------------------------------------------------------------------
# D. Dynamic mechanisms (spec 6.2, 7)
# ---------------------------------------------------------------------------

def test_dynamic_import_forces_complete(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'tests/test_dyn_consumer.py': (
            'from pkg.leaf import leaf_value\n\n\n'
            'def test_dyn():\n'
            '    mod = __import__("json")\n'
            '    assert leaf_value() == 7 and mod is not None\n'
        ),
    })
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.fallback_reason == scoping.F_DYNAMIC_IMPORT


def test_importlib_forces_complete(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'tests/test_lib_consumer.py': (
            'import importlib\nfrom pkg.leaf import leaf_value\n\n\n'
            'def test_lib():\n'
            '    mod = importlib.import_module("json")\n'
            '    assert leaf_value() == 7 and mod is not None\n'
        ),
    })
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.fallback_reason == scoping.F_DYNAMIC_IMPORT


def test_computed_getattr_forces_complete(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'tests/test_getattr_consumer.py': (
            'from pkg.leaf import leaf_value\n\n\n'
            'def test_getattr():\n'
            '    name = "real"\n'
            '    assert getattr(leaf_value, name) is not None\n'
        ),
    })
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.fallback_reason == scoping.F_DYNAMIC_GETATTR


def test_literal_getattr_is_not_poison(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'tests/test_lit_getattr.py': (
            'from pkg.leaf import leaf_value\n\n\n'
            'def test_lit():\n'
            '    assert getattr(leaf_value, "__name__") == "leaf_value"\n'
        ),
    })
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.eligible, decision.fallback_reason


# ---------------------------------------------------------------------------
# E. Graph / infrastructure failures (spec 6.2, 7)
# ---------------------------------------------------------------------------

def test_graph_construction_failure_forces_complete(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError('synthetic graph failure')

    monkeypatch.setattr(scoping, 'build_graph', boom)
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.fallback_reason == scoping.F_GRAPH_FAILURE
    assert 'synthetic graph failure' in decision.rationale[0]


def test_index_coverage_gap_forces_complete(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Truncated graph (a silently skipped module) fails the proof."""
    repo = _repo(tmp_path)
    real_index = scoping._index_repository

    def partial(root: Path):
        sources, texts, module_of_rel, rel_of_module = real_index(root)
        # adversarial truncation: drop one indexed module entirely
        sources.pop('tests.test_ok', None)
        texts.pop('tests.test_ok', None)
        module_of_rel.pop('tests/test_ok.py', None)
        rel_of_module.pop('tests.test_ok', None)
        return sources, texts, module_of_rel, rel_of_module

    monkeypatch.setattr(scoping, '_index_repository', partial)
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.fallback_reason == scoping.F_INCOMPLETE_CLOSURE
    assert 'index_coverage_mismatch' in decision.rationale[0]


def test_filesystem_walk_failure_forces_exception_fallback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exceptions inside the graph phase collapse to GRAPH_FAILURE,
    outside it to ELIGIBILITY_EXCEPTION -- both are COMPLETE."""
    repo = _repo(tmp_path)

    def boom(_root: Path):
        raise OSError('walk exploded')

    monkeypatch.setattr(scoping, '_repository_py_files', boom)
    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.eligible is False
    assert decision.mode == scoping.COMPLETE
    assert decision.fallback_reason in (scoping.F_GRAPH_FAILURE,
                                        scoping.F_ELIGIBILITY_EXCEPTION)
    assert 'walk exploded' in decision.rationale[0]


def test_parser_failure_forces_graph_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    (repo / 'pkg' / 'broken.py').write_text('def broken(:\n',
                                            encoding='utf-8')

    decision = _assess(repo, ['pkg/leaf.py'])
    assert decision.mode == scoping.COMPLETE
    # index_module raises ValueError on unparseable source -> GRAPH_FAILURE
    assert decision.fallback_reason == scoping.F_GRAPH_FAILURE


# ---------------------------------------------------------------------------
# F. Dumb executor (spec 1.1, 3D, 6.5)
# ---------------------------------------------------------------------------

def _run_scoped_fixture(tmp_path: Path) -> Path:
    repo = _repo(tmp_path, {
        'tests/test_loud.py': (
            'def test_loud():\n    raise AssertionError("must_not_run")\n'
        ),
    })
    return repo


def test_run_scoped_executes_only_authorized_targets(
        tmp_path: Path) -> None:
    repo = _run_scoped_fixture(tmp_path)
    entries = check_runner.run_scoped(
        repo,
        changed_files=['pkg/leaf.py'],
        targeted_tests=['tests/test_leaf_consumer.py'],
        mypy_targets=['pkg/leaf.py'],
    )
    assert [entry['name'] for entry in entries] == ['ruff', 'pytest', 'mypy']
    assert all(entry['status'] == 'passed' for entry in entries), entries
    pytest_entry = entries[1]
    # targeted containment: loud test did NOT run
    assert '1 passed' in pytest_entry['output_tail']
    assert 'must_not_run' not in pytest_entry['output_tail']
    assert pytest_entry['scope'] == 'targeted_tests'
    assert entries[0]['scope'] == 'changed_files'
    assert entries[2]['scope'] == 'dependency_graph'
    for entry in entries:
        assert set(entry) >= {'name', 'status', 'exit_code', 'duration_ms',
                              'output_tail'}


def test_run_scoped_empty_target_set_is_explicit(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    entries = check_runner.run_scoped(
        repo,
        changed_files=['pkg/leaf.py'],
        targeted_tests=[],
        mypy_targets=['pkg/leaf.py'],
    )
    assert len(entries) == 3
    pytest_entry = next(e for e in entries if e['name'] == 'pytest')
    assert pytest_entry['status'] == 'skipped'
    assert pytest_entry['note'] == 'empty_target_set_proven'
    # ruff and mypy still ran: the record cannot look like "nothing ran"
    assert all(e['status'] == 'passed'
               for e in entries if e['name'] != 'pytest')


def test_run_scoped_reports_failing_target(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'tests/test_zbad.py': 'def test_zbad():\n    assert False\n',
    })
    entries = check_runner.run_scoped(
        repo,
        changed_files=['tests/test_zbad.py'],
        targeted_tests=['tests/test_zbad.py'],
        mypy_targets=['tests/test_zbad.py'],
    )
    pytest_entry = next(e for e in entries if e['name'] == 'pytest')
    assert pytest_entry['status'] == 'failed'
    assert pytest_entry['exit_code'] not in (0, None)


def test_run_scoped_is_a_dumb_executor() -> None:
    """Spec 6.5.1: no policy/eligibility authority in the executor.

    The scan covers executable statements only (docstring excluded) so
    the contract can explain itself in prose without weakening the check.
    """
    import ast as _ast

    tree = _ast.parse(inspect.getsource(check_runner.run_scoped))
    func = tree.body[0]
    assert isinstance(func, _ast.FunctionDef)
    body = _ast.Module(body=func.body[1:], type_ignores=[])
    body_source = _ast.unparse(body)
    forbidden = [
        'PROVEN_DISJOINT', 'PROVEN_SHARED', 'UNKNOWN_CLASSIFICATION',
        'scope_level', 'assess_scoping', 'classification',
        'fallback_reason', 'ScopingDecision', 'eligible',
        'rationale',
    ]
    leaked = [token for token in forbidden if token in body_source]
    assert not leaked, f'run_scoped contains policy authority: {leaked}'
    # it may only execute the supplied targets
    assert 'targeted_tests' in body_source
    assert 'mypy_targets' in body_source


def test_run_scoped_redacts_secret_environment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret leaked through a failing assertion message is scrubbed
    from the bounded output before it can enter evidence."""
    monkeypatch.setenv('TEST_SECRET_KEY', 'supersecretvalue123')
    repo = _repo(tmp_path, {
        'tests/test_secret.py': (
            'import os\n\n\n'
            'def test_secret():\n'
            "    assert os.environ['TEST_SECRET_KEY'] == 'different'\n"
        ),
    })
    entries = check_runner.run_scoped(
        repo,
        changed_files=['tests/test_secret.py'],
        targeted_tests=['tests/test_secret.py'],
        mypy_targets=['tests/test_secret.py'],
    )
    pytest_entry = next(e for e in entries if e['name'] == 'pytest')
    assert pytest_entry['status'] == 'failed'
    assert 'supersecretvalue123' not in pytest_entry['output_tail']
    assert '[REDACTED]' in pytest_entry['output_tail']


def test_run_scoped_timeout_is_deterministic_failure(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        'tests/test_slow.py': (
            'import time\n\n\n'
            'def test_slow():\n    time.sleep(4)\n'
        ),
    })
    entries = check_runner.run_scoped(
        repo,
        changed_files=['tests/test_slow.py'],
        targeted_tests=['tests/test_slow.py'],
        mypy_targets=['tests/test_slow.py'],
        timeout=1,
    )
    pytest_entry = next(e for e in entries if e['name'] == 'pytest')
    assert pytest_entry['status'] == 'failed'
    assert pytest_entry['note'] == 'timed out after 1s'
    assert pytest_entry['exit_code'] is None
    # the other checks are unaffected by one timing-out command
    assert next(e for e in entries if e['name'] == 'ruff')['status'] == 'passed'


# ---------------------------------------------------------------------------
# G. Scheduler wiring (spec 4, 6.5, 6.6)
# ---------------------------------------------------------------------------

def _worker_out() -> dict:
    code = (
        "import pathlib;"
        "pathlib.Path('pkg/out.py').write_text('RESULT = 1\\n',"
        "encoding='utf-8')"
    )
    return {
        'id': 'worker_a',
        'declared_scope': ['pkg/out.py'],
        'reads': [],
        'writes': ['pkg/out.py'],
        'deps': [],
        'cmd': [PY, '-c', code],
        'verify': 'pytest tests/ -q',
    }


def _worker_append_init() -> dict:
    code = (
        "import pathlib;p=pathlib.Path('pkg/__init__.py');"
        "p.write_text(p.read_text(encoding='utf-8')"
        "+'\\n\\ndef public_api():\\n    return 1\\n', encoding='utf-8')"
    )
    return {
        'id': 'worker_a',
        'declared_scope': ['pkg/__init__.py'],
        'reads': [],
        'writes': ['pkg/__init__.py'],
        'deps': [],
        'cmd': [PY, '-c', code],
        'verify': 'pytest tests/ -q',
    }


def _scheduler_run(repo: Path, worker: dict, task_id: str,
                   base: str | None = None):
    sched = GovernedScheduler(repo, [worker], task_id=task_id,
                              keep_worktrees=True)
    path = sched.dispatcher.create('worker_a')
    result = sched._run_one(worker, path)
    # evidence identity binds to THIS worktree's tree; tests that
    # re-verify must use it, not the untouched main repository
    sched._last_worktree = path  # type: ignore[attr-defined]
    evidence_path = (common_paths.get_orchestrator_dir(repo)
                     / _safe_id(task_id) / 'worker_a.json')
    payload = None
    if evidence_path.is_file():
        payload = json.loads(evidence_path.read_text(encoding='utf-8'))
    return sched, result, evidence_path, payload


def test_eligible_worker_gets_scoped_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {'tests/test_out_consumer.py': OUT_CONSUMER})
    worker = _worker_out()
    sched, result, evidence_path, payload = _scheduler_run(
        repo, worker, 'scoped-eligible')
    assert result['state'] == 'DONE', result
    assert payload is not None
    assert payload['validation_mode'] == 'SCOPED'
    assert payload['targeted_tests'] == ['tests/test_out_consumer.py']
    assert set(payload['mypy_targets']) == {'pkg/out.py',
                                            'tests/test_out_consumer.py'}
    assert 'complete_reverse_closure_proof' in payload['omission_rationale']
    assert 'fallback_reason' not in payload
    pytest_entry = next(e for e in payload['checks']
                        if e['name'] == 'pytest')
    assert pytest_entry['status'] == 'passed'
    # containment proof from the real run: package suite would report
    # 3 passed; the scoped run executed exactly the one authorized test
    assert '1 passed' in pytest_entry['output_tail']
    assert pytest_entry['scope'] == 'targeted_tests'
    # evidence integrity chain untouched
    verify_worker_evidence(
        evidence_path,
        worktree=sched._last_worktree)  # type: ignore[attr-defined]
    document = replay.parse_evidence(sched.authoritative['worker_a'])
    verdict = replay.verify_record(document)
    assert verdict.verified is True
    canonical = evidence.canonicalize_evidence(document)
    assert canonical == sched.authoritative['worker_a']


def test_s3_change_forces_complete_evidence(tmp_path: Path) -> None:
    # plain fixture: no consumer of a module this task never creates
    repo = _repo(tmp_path)
    worker = _worker_append_init()
    sched, result, evidence_path, payload = _scheduler_run(
        repo, worker, 'scoped-s3')
    assert result['state'] == 'DONE', result
    assert payload['validation_mode'] == 'COMPLETE'
    assert payload['fallback_reason'] == scoping.F_FORBIDDEN_SCOPE_LEVEL
    assert 'targeted_tests' not in payload
    # COMPLETE path actually ran the package suite (unchanged executor)
    pytest_entry = next(e for e in payload['checks']
                        if e['name'] == 'pytest')
    assert pytest_entry['scope'] == 'package'
    assert '2 passed' in pytest_entry['output_tail']
    verify_worker_evidence(evidence_path, worktree=sched._last_worktree)


def test_dynamic_signal_unknown_classification_forces_complete(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime lockout 6.4: dynamic marker -> uncertain scope ->
    UNKNOWN classification -> engine refuses SCOPED before anything
    else, even though every other condition would hold."""
    calls: list[bool] = []
    real_run_scoped = check_runner.run_scoped

    def spy(*args, **kwargs):
        calls.append(True)
        return real_run_scoped(*args, **kwargs)

    monkeypatch.setattr(check_runner, 'run_scoped', spy)
    # private surface + dynamic marker in the changed file -> scope
    # status 'uncertain' -> runtime classification UNKNOWN
    code = (
        "import pathlib;"
        "pathlib.Path('pkg/out.py').write_text("
        "'RESULT = 1\\n\\n\\ndef _helper():\\n"
        "    return __import__(\"json\")\\n',"
        "encoding='utf-8')"
    )
    worker = dict(_worker_out(), cmd=[PY, '-c', code])
    repo = _repo(tmp_path, {'tests/test_out_consumer.py': OUT_CONSUMER})
    _sched, result, _path, payload = _scheduler_run(
        repo, worker, 'scoped-dynamic')
    assert result['state'] == 'DONE', result
    assert payload['validation_mode'] == 'COMPLETE'
    assert payload['fallback_reason'] == scoping.F_UNKNOWN_CLASSIFICATION
    assert not calls, 'run_scoped must not run for UNKNOWN'


def test_engine_exception_forces_complete(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError('engine exploded')

    monkeypatch.setattr(scoping, 'assess_scoping_eligibility', boom)
    repo = _repo(tmp_path, {'tests/test_out_consumer.py': OUT_CONSUMER})
    _sched, result, _path, payload = _scheduler_run(
        repo, _worker_out(), 'scoped-boom')
    assert result['state'] == 'DONE', result
    assert payload['validation_mode'] == 'COMPLETE'
    assert payload['fallback_reason'] == scoping.F_ELIGIBILITY_EXCEPTION
    # rationale is engine-internal; the envelope carries only the stable
    # machine-readable reason (spec 2.4)


@pytest.mark.parametrize('forged', [
    None,
    scoping.ScopingDecision(eligible=True, mode='COMPLETE',
                            fallback_reason='FORGED'),
    scoping.ScopingDecision(eligible=True, mode=scoping.SCOPED,
                            fallback_reason='FORGED'),
])
def test_forged_decision_forces_complete(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch,
                                         forged) -> None:
    monkeypatch.setattr(scoping, 'assess_scoping_eligibility',
                        lambda *_a, **_k: forged)
    repo = _repo(tmp_path, {'tests/test_out_consumer.py': OUT_CONSUMER})
    sched, result, evidence_path, payload = _scheduler_run(
        repo, _worker_out(), 'scoped-forged')
    assert result['state'] == 'DONE', result
    assert payload['validation_mode'] == 'COMPLETE'
    assert payload['fallback_reason'] == scoping.F_INVALID_DECISION
    # fallback executed the COMPLETE package suite
    pytest_entry = next(e for e in payload['checks']
                        if e['name'] == 'pytest')
    assert '3 passed' in pytest_entry['output_tail']
    verify_worker_evidence(evidence_path, worktree=sched._last_worktree)


def test_worker_validation_failure_stays_visible(tmp_path: Path) -> None:
    code = (
        "import pathlib;"
        "pathlib.Path('tests/test_zfail.py').write_text("
        "'def test_zfail():\\n    assert False\\n', encoding='utf-8')"
    )
    worker = {
        'id': 'worker_a',
        'declared_scope': ['tests/test_zfail.py'],
        'reads': [],
        'writes': ['tests/test_zfail.py'],
        'deps': [],
        'cmd': [PY, '-c', code],
        'verify': 'pytest tests/ -q',
    }
    repo = _repo(tmp_path, {'tests/test_out_consumer.py': OUT_CONSUMER})
    _sched, result, evidence_path, _payload = _scheduler_run(
        repo, worker, 'scoped-failing')
    # the failing targeted test remains a visible failure; it is never
    # converted into a successful SCOPED result
    assert result['state'] == 'FAILED', result
    assert result['reason'] == 'verification_failed:pytest'
    assert not evidence_path.is_file()


def test_tampered_scoped_evidence_fails_verification(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path, {'tests/test_out_consumer.py': OUT_CONSUMER})
    sched, result, evidence_path, _payload = _scheduler_run(
        repo, _worker_out(), 'scoped-tamper')
    assert result['state'] == 'DONE'
    evidence_path.write_text('{"schema": 1, "stage": "work',
                             encoding='utf-8')
    with pytest.raises((OrchestratorError, evidence.EvidenceError,
                        json.JSONDecodeError)):
        verify_worker_evidence(evidence_path,
                               worktree=sched._last_worktree)


def test_run_scoped_has_single_runtime_authority() -> None:
    """Spec 6.5.5: no alternate runtime path may select SCOPED."""
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted((root / 'asha').glob('*.py')):
        text = path.read_text(encoding='utf-8')
        # call form only: prose references in docstrings are not call sites
        if 'run_scoped(' in text and path.name not in {
                'check_runner.py', 'scheduler.py'}:
            offenders.append(path.name)
    assert not offenders, (
        f'run_scoped invoked outside the authority path: {offenders}')
    sched_text = (root / 'asha' / 'scheduler.py').read_text(encoding='utf-8')
    assert 'if decision.eligible:' in sched_text
    assert 'check_runner.run_scoped(' in sched_text
    # the executor never names or imports a decision type
    runner_text = (root / 'asha' / 'check_runner.py').read_text(
        encoding='utf-8')
    assert 'ScopingDecision' not in runner_text


# ---------------------------------------------------------------------------
# H. Evidence contract invariants (spec 12) -- additive guards
# ---------------------------------------------------------------------------

def test_worker_metadata_keys_are_additive_only(
        tmp_path: Path) -> None:
    """Scoping metadata is confined to the worker envelope; the
    AuthoritativeEvidence contract keys stay exactly the Phase 3 set."""
    repo = _repo(tmp_path, {'tests/test_out_consumer.py': OUT_CONSUMER})
    sched, _result, _path, payload = _scheduler_run(
        repo, _worker_out(), 'scoped-additive')
    document = json.loads(sched.authoritative['worker_a'].decode('utf-8'))
    assert set(document) == {
        'schema_version', 'execution_identity_key', 'worker_id',
        'generation', 'base_tree_sha', 'target_tree_sha',
        'observed_scope', 'normalized_facts', 'verdict',
    }
    # worker envelope gains only sanctioned keys (spec 2.4)
    assert set(payload) >= {
        'schema', 'stage', 'scope', 'commit', 'tree_hash', 'observed_at',
        'checks', 'authorized_to_ship', 'task_id', 'worker_id',
        'base_commit', 'base_tree_sha', 'target_tree_sha',
        'declared_scope', 'observed_scope', 'read_set', 'write_set',
        'diff', 'exit_status', 'validation_mode',
    }
    assert payload['authorized_to_ship'] is False
