"""Phase 3.1 -- evidence acquisition optimization contract tests.

Budgets and equivalences here are MEASURED (benchmarks/run_evidence_bench.py,
n=15 per label), not aspirational:
  baseline   git=13/cycle (state 7, scope 5, facts 1), T_engine 492.8ms
  optimized  git=9/cycle  (state 5, scope 3, facts 1), T_engine 395.5ms
The <175ms figure is a benchmark target only and is never asserted here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Self

import pytest

from asha import evidence, scope_resolver
from asha.common import paths as common_paths
from asha.scheduler import GovernedScheduler
from asha.worktree import WorktreeDispatcher

PY = sys.executable

GITIGNORE = '.jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n'
BASE_CORE = {
    '.gitignore': GITIGNORE,
    'README.md': '# evidence optimization bench\n',
    'pyproject.toml': '[tool.ruff]\nline-length = 88\n',
    'tests/test_ok.py': 'def test_ok():\n    assert True\n',
    'pkg/__init__.py': '',
}
AGENT_ITERS = 200_000
BASELINE_GIT_BUDGET = 13     # measured pre-optimization (stable, n=15)
OPTIMIZED_GIT_BUDGET = 9     # measured post-optimization (stable, n=15)


def _make_repo(root: Path) -> Path:
    repo = root / 'repo'
    repo.mkdir(parents=True)
    for rel, text in BASE_CORE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    _git(repo, 'config', 'user.name', 'Fixture')
    _git(repo, 'config', 'commit.gpgsign', 'false')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'baseline')
    return repo


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


def _worker() -> dict:
    code = (
        'import hashlib,pathlib;'
        f'hashlib.pbkdf2_hmac("sha256",b"asha-bench",b"salt",{AGENT_ITERS});'
        "pathlib.Path('pkg/out.py').parent.mkdir(parents=True,exist_ok=True);"
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


class _CycleSpy:
    """Counts git subprocesses issued DURING _collect only (budget scope,
    identical to the benchmark's phase filter)."""

    def __init__(self) -> None:
        self.counting = False
        self.git_calls = 0
        self._real_run = subprocess.run
        self._real_popen = subprocess.Popen
        self._in_run = False

    def __enter__(self) -> Self:
        real_run = self._real_run

        def run(*args, **kwargs):
            argv = args[0] if args else kwargs.get('args')
            self._in_run = True
            try:
                return real_run(*args, **kwargs)
            finally:
                self._in_run = False
                if self.counting and argv and str(argv[0]) == 'git':
                    self.git_calls += 1

        def popen(*args, **kwargs):
            if self._in_run:
                return self._real_popen(*args, **kwargs)
            argv = args[0] if args else kwargs.get('args')
            proc = self._real_popen(*args, **kwargs)
            if self.counting and argv and str(argv[0]) == 'git':
                self.git_calls += 1
            return proc

        subprocess.run = run          # type: ignore[assignment]
        subprocess.Popen = popen      # type: ignore[misc, assignment]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run = self._real_run
        subprocess.Popen = self._real_popen  # type: ignore[misc]


class _Cycle:
    """One measured worker cycle: scheduler + worktree + evidence kept."""

    def __init__(self, sched: GovernedScheduler, worker: dict,
                 path: Path, result: dict, payload: dict,
                 spy: _CycleSpy) -> None:
        self.sched = sched
        self.worker = worker
        self.path = path
        self.result = result
        self.payload = payload
        self.spy = spy


@pytest.fixture(scope='module')
def cycle(tmp_path_factory: pytest.TempPathFactory) -> _Cycle:
    root = tmp_path_factory.mktemp('evidence-opt')
    repo = _make_repo(root)
    worker = _worker()
    spy = _CycleSpy()
    with spy:
        sched = GovernedScheduler(repo, [worker],
                                  task_id='evidence-opt',
                                  keep_worktrees=True)
        path = sched.dispatcher.create('worker_a')
        real_collect = sched._collect

        def counting_collect(w, p, rc, tail):
            spy.counting = True
            try:
                return real_collect(w, p, rc, tail)
            finally:
                spy.counting = False

        sched._collect = counting_collect  # type: ignore[assignment]
        result = sched._run_one(worker, path)
    assert result['state'] == 'DONE', result
    evidence_path = (common_paths.get_orchestrator_dir(repo)
                     / 'evidence-opt' / 'worker_a.json')
    payload = json.loads(evidence_path.read_text(encoding='utf-8'))
    return _Cycle(sched, worker, path, result, payload, spy)


def test_git_subprocess_call_budget(cycle: _Cycle) -> None:
    """Measured post-optimization budget: 9 git calls inside _collect
    (baseline was 13 with the same fixture; stable across n=15)."""
    assert cycle.spy.git_calls == OPTIMIZED_GIT_BUDGET
    assert cycle.spy.git_calls < BASELINE_GIT_BUDGET


def test_base_tree_cache_isolation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / 'cache-repo')
    first = WorktreeDispatcher(repo)
    base_commit, base_tree = first.base_commit, first.base_tree
    # same immutable revision: every worktree sees the same cached values
    p1 = first.create('w_one')
    p2 = first.create('w_two')
    assert p1.is_dir() and p2.is_dir() and p1 != p2
    assert first.base_commit == base_commit
    assert first.base_tree == base_tree
    first.cleanup()
    # new authoritative base revision -> new dispatcher recomputes;
    # the old cached instance is never mutated across generations
    time.sleep(1.1)                      # git commit timestamps are second-granular
    # a revision that actually changes the TREE (an empty commit would
    # reuse the parent tree and prove nothing)
    (repo / 'pkg' / 'second.py').write_text('X = 2\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'next revision')
    second = WorktreeDispatcher(repo)
    assert second.base_commit != base_commit
    assert second.base_tree != base_tree
    assert first.base_commit == base_commit
    assert first.base_tree == base_tree
    second.cleanup()


def test_in_memory_ledger_integration_in_scheduler(
        cycle: _Cycle) -> None:
    sched = cycle.sched
    ledger = sched.ledgers['worker_a']
    assert isinstance(ledger, evidence.ExecutionLedger)
    assert ledger.worker_id == 'worker_a'
    assert ledger.generation == sched.graph.generation
    assert ledger.base_tree_sha == sched.dispatcher.base_tree
    kinds = [event.event_type for event in ledger.events]
    assert kinds == ['STATE_ACQUIRED', 'SCOPE_OBSERVED',
                     'VALIDATION_PASSED', 'EVIDENCE_SEALED',
                     'POLICY_RESOLVED']
    stored = sched.authoritative['worker_a']
    document = json.loads(stored.decode('utf-8'))
    contract_keys = {
        'schema_version', 'execution_identity_key', 'worker_id',
        'generation', 'base_tree_sha', 'target_tree_sha',
        'observed_scope', 'normalized_facts', 'verdict',
    }
    assert set(document) == contract_keys
    expected_key = hashlib.sha256(
        f'{document["base_tree_sha"]}:worker_a:'
        f'{document["generation"]}'.encode()).hexdigest()
    assert document['execution_identity_key'] == expected_key
    assert document['verdict'] == {'status': 'PASS',
                                   'reason_code': 'evidence_sealed'}
    # scheduler result payload untouched by the ledger integration
    assert set(cycle.result) == {'state', 'reason', 'evidence',
                                 'observed', 'target_tree'}


def test_ledger_events_excluded_from_canonical_wire(
        cycle: _Cycle) -> None:
    sched = cycle.sched
    before = sched.authoritative['worker_a']
    ledger = sched.ledgers['worker_a']
    for index in range(5):
        ledger.record('WORKER_DISPATCHED', step=str(index),
                      note='ledger-only ' + str(index))
    assert len(ledger.events) == 10
    assert sched.authoritative['worker_a'] == before
    assert b'WORKER_DISPATCHED' not in before
    assert b'ledger-only' not in before
    assert b'"events"' not in before


def test_adaptive_policy_preserves_soundness(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / 'policy-repo')
    disjoint = [
        {'id': 'w_a', 'declared_scope': ['pkg/a.py'], 'reads': [],
         'writes': ['pkg/a.py'], 'deps': [], 'cmd': [PY, '-c', 'pass'],
         'verify': 'pytest tests/ -q'},
        {'id': 'w_b', 'declared_scope': ['pkg/b.py'], 'reads': [],
         'writes': ['pkg/b.py'], 'deps': [], 'cmd': [PY, '-c', 'pass'],
         'verify': 'pytest tests/ -q'},
    ]
    sched = GovernedScheduler(repo, disjoint, task_id='policy')
    certain = {'status': 'certain'}
    cls = evidence.IndependenceClassification
    # proven disjoint -> MINIMAL
    assert sched._classify_independence('w_a', certain) is cls.PROVEN_DISJOINT
    assert (evidence.resolve_evidence_policy(cls.PROVEN_DISJOINT)
            is evidence.EvidencePolicy.MINIMAL)
    # declared overlap -> PROVEN_SHARED -> COMPLETE
    shared = dict(disjoint[1])
    shared['writes'] = ['pkg/a.py']          # both write pkg/a.py
    shared['reads'] = ['pkg/a.py']
    sched2 = GovernedScheduler(repo, [disjoint[0], shared],
                               task_id='policy-shared')
    assert sched2._classify_independence('w_a', certain) is cls.PROVEN_SHARED
    assert (evidence.resolve_evidence_policy(cls.PROVEN_SHARED)
            is evidence.EvidencePolicy.COMPLETE)
    # uncertain capture -> UNKNOWN -> COMPLETE (fail-closed)
    assert (sched._classify_independence('w_a', {'status': 'uncertain'})
            is cls.UNKNOWN)
    assert (evidence.resolve_evidence_policy(cls.UNKNOWN)
            is evidence.EvidencePolicy.COMPLETE)
    # graph-level uncertainty wins over certain capture
    sched.worker_graph['uncertain'] = {'w_a'}
    assert sched._classify_independence('w_a', certain) is cls.UNKNOWN


def test_semantic_equivalence_before_after(cycle: _Cycle) -> None:
    """Optimized path vs the former full-recompute path on the same
    authoritative state: identical governance-relevant evidence."""
    payload = cycle.payload
    path = cycle.path
    base = cycle.sched.dispatcher.base_commit
    # OPT-B equivalence: passed-through paths == old recomputation
    fresh_observed = scope_resolver.changed_files(path, base=base)
    assert payload['observed_scope'] == fresh_observed
    fresh_resolved = scope_resolver.resolve(path, base=base)
    assert fresh_resolved['scope'] == payload['scope']
    assert fresh_resolved['affected_files'] == payload['observed_scope']
    assert fresh_resolved['status'] == 'certain'
    # OPT-A equivalence: merged rev-parse == independent calls
    assert payload['tree_hash'] == payload['target_tree_sha']
    assert payload['commit'] == _git(path, 'rev-parse', 'HEAD')
    assert payload['tree_hash'] == _git(path, 'rev-parse', 'HEAD^{tree}')
    # payload diff == independent base..target diff (raw stdout: the
    # payload stores the diff with strip=False)
    raw_diff = subprocess.run(
        ['git', 'diff', base, payload['commit']],
        cwd=path, capture_output=True, text=True, check=True).stdout
    assert payload['diff'] == raw_diff
    # canonical authoritative bytes remain Phase-3.0 wire compatible
    document = json.loads(
        cycle.sched.authoritative['worker_a'].decode('utf-8'))
    assert document['worker_id'] == 'worker_a'
    assert document['target_tree_sha'] == payload['target_tree_sha']
    assert isinstance(cycle.sched.evidence_policy['worker_a'], str)
    assert cycle.sched.evidence_policy['worker_a'] in ('MINIMAL',
                                                       'COMPLETE')
    # frozen contract still refuses mutation post-integration
    ledger = cycle.sched.ledgers['worker_a']
    with pytest.raises(dataclasses.FrozenInstanceError):
        ledger.events[0].event_type = 'MUTATED'  # type: ignore[misc]
