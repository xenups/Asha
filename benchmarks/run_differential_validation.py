"""Phase 5.0 -- Offline differential qualification of SCOPED vs COMPLETE.

Offline QUALIFICATION mechanism (spec section 5), never a runtime
authority: both paths are executed on byte-identical fixture
repositories and compared at the semantic evidence level across nine
dimensions:

  1 classification  2 declared_scope  3 observed_scope
  4 eligibility_verdict  5 validation_outcome  6 worker_state
  7 target_tree_hash  8 AuthoritativeEvidence canonical semantics
  9 evidence.seal + replay verification (verify_record + verify_bytes)

Differential Invariance Rule: any mismatch FAILS the SCOPED
qualification for that case; COMPLETE remains authoritative. This
script never weakens COMPLETE validation to remove a discrepancy.

Also prints the section-8 cost-model timings measured on these runs.

Usage: python benchmarks/run_differential_validation.py
Exit code 0 = all cases qualified; 1 = at least one mismatch.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asha import evidence, replay, scoping
from asha.scheduler import GovernedScheduler, verify_worker_evidence
from asha.types import OrchestratorError

PY = sys.executable
GITIGNORE = '.jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n'

BASE = {
    '.gitignore': GITIGNORE,
    'README.md': '# differential fixture\n',
    'pyproject.toml': '[tool.ruff]\nline-length = 88\n',
    'pkg/__init__.py': '',
    'pkg/leaf.py': 'def leaf_value():\n    return 7\n',
    'pkg/helper.py': 'from pkg.leaf import leaf_value\n\n\n'
                     'def helper():\n    return leaf_value()\n',
    'tests/test_ok.py': 'def test_ok():\n    assert True\n',
    'tests/test_leaf_consumer.py': (
        'from pkg.leaf import leaf_value\n\n\n'
        'def test_leaf():\n    assert leaf_value() == 7\n'
    ),
    'tests/test_transitive_consumer.py': (
        'from pkg.helper import helper\n\n\n'
        'def test_transitive():\n    assert helper() == 7\n'
    ),
}

# leaf change: private zero-caller append -> S1 certain (stable per
# scope_resolver's documented classification, unlike public-body edits)
LEAF_CHANGE = ('pkg/leaf.py', (
    'def leaf_value():\n    return 7\n\n\n'
    'def _leaf_internal():\n    return None\n'))
UNRES_HELPER = ('tests/helpers_unres.py', (
    'from pkg.leaf import leaf_value\n\n\n'
    'def unres_helper():\n    return never_imported_name\n'))
GHOST_HELPER = ('tests/helpers_ghost.py', (
    'import ghostpkg\nfrom pkg.leaf import leaf_value\n\n\n'
    'def ghost_helper():\n    return (leaf_value, ghostpkg)\n'))

TEST_CHANGE = ('tests/test_leaf_consumer.py', (
    'from pkg.leaf import leaf_value\n\n\n'
    'def test_leaf():\n    assert leaf_value() == 7\n\n\n'
    'def _test_helper_marker():\n    return None\n'))


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


def _build_base(root: Path) -> Path:
    """One canonical fixture; every corpus copy derives from it so tree
    identity is byte-comparable across modes."""
    repo = root / 'fixture-base'
    for rel, text in BASE.items():
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


def _copy_case(base: Path, dest: Path) -> Path:
    shutil.copytree(base, dest)
    return dest


def _worker(wid: str, declared: list[str], code: str) -> dict:
    return {
        'id': wid,
        'declared_scope': list(declared),
        'reads': [],
        'writes': list(declared),
        'deps': [],
        'cmd': [PY, '-c', code],
        'verify': 'pytest tests/ -q',
    }


def _write_code(rel: str, content: str) -> str:
    # newlines must be ESCAPED for embedding inside a python -c literal
    escaped = (content.replace('\\', '\\\\')
               .replace('\n', '\\n').replace("'", "\\'"))
    return (f"import pathlib;p=pathlib.Path('{rel}');"
            f"p.parent.mkdir(parents=True,exist_ok=True);"
            f"p.write_text('{escaped}',encoding='utf-8')")


@dataclass
class Run:
    mode: str
    state: str
    reason: str
    payload: dict
    authoritative: bytes
    evidence_path: Path
    worktree: Path
    checks: dict[str, str]
    elapsed_ms: float
    validation_ms: float


def _run(repo: Path, worker: dict, task_id: str,
         force_complete: bool = False) -> Run:
    original = scoping.assess_scoping_eligibility
    if force_complete:
        scoping.assess_scoping_eligibility = (  # type: ignore[assignment]
            lambda *_a, **_k: scoping.complete_decision(
                'FORCED_DIFFERENTIAL'))
    started = time.perf_counter()
    try:
        sched = GovernedScheduler(repo, [worker], task_id=task_id,
                                  keep_worktrees=True)
        worktree = sched.dispatcher.create(worker['id'])
        result = sched._run_one(worker, worktree)
        elapsed_ms = (time.perf_counter() - started) * 1000
        evidence_path = (repo / '.jspace' / 'cache' / 'orchestrator'
                         / task_id / f"{worker['id']}.json")
        payload = (json.loads(evidence_path.read_text(encoding='utf-8'))
                   if evidence_path.is_file() else {})
        checks = {entry['name']: entry['status']
                  for entry in payload.get('checks', [])}
        validation_ms = sum(entry.get('duration_ms', 0)
                            for entry in payload.get('checks', []))
        return Run(
            mode='COMPLETE' if force_complete else payload.get(
                'validation_mode', '?'),
            state=result.get('state', '?'),
            reason=result.get('reason') or payload.get('fallback_reason', ''),
            payload=payload,
            authoritative=sched.authoritative.get(worker['id'], b''),
            evidence_path=evidence_path,
            worktree=worktree,
            checks=checks,
            elapsed_ms=elapsed_ms,
            validation_ms=float(validation_ms),
        )
    finally:
        scoping.assess_scoping_eligibility = original  # type: ignore[assignment]


def _seal_and_replay(run: Run) -> dict:
    seal_ok = False
    replay_record = False
    replay_bytes_ok = False
    if run.evidence_path.is_file():
        try:
            verify_worker_evidence(run.evidence_path,
                                   worktree=run.worktree)
            seal_ok = True
        except Exception:
            seal_ok = False
    if run.authoritative:
        try:
            record = replay.parse_evidence(run.authoritative)
            replay_record = replay.verify_record(record).verified
            replay_bytes_ok = replay.verify_bytes(run.authoritative).verified
        except Exception:  # noqa: S110 -- failed verification is encoded
            pass           # as False in the returned flags, not hidden
    return {'seal': seal_ok, 'verify_record': replay_record,
            'verify_bytes': replay_bytes_ok}


def _nine_dims(run: Run) -> dict:
    payload = run.payload
    return {
        'classification': payload.get('validation_mode', '?'),
        'declared_scope': payload.get('declared_scope'),
        'observed_scope': payload.get('observed_scope'),
        'eligibility_verdict': {
            'validation_mode': payload.get('validation_mode'),
            'fallback_reason': payload.get('fallback_reason'),
        },
        'validation_outcome': dict(run.checks),
        'worker_state': run.state,
        'target_tree_hash': payload.get('target_tree_sha'),
        'authoritative_canonical': run.authoritative,
        'seal_and_replay': _seal_and_replay(run),
    }


# ---------------------------------------------------------------------------
# Corpus runners
# ---------------------------------------------------------------------------

@dataclass
class CaseReport:
    name: str
    kind: str
    eligible: bool
    mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def _paired_case(name: str, base: Path, work: Path,
                 rel: str, content: str) -> CaseReport:
    """Same change through SCOPED (engine decides) and COMPLETE
    (forced offline) on two byte-identical copies; compare nine dims."""
    report = CaseReport(name=name, kind='paired', eligible=True)
    scoped_repo = _copy_case(base, work / f'{name}-scoped')
    complete_repo = _copy_case(base, work / f'{name}-complete')
    # the WORKER writes the scenario change (plus its marker): observed
    # scope measures worker delta vs base, so pre-committing the change
    # would hide it from the very scope the engine is judged on
    code = (_write_code(rel, content) + ';'
            + _write_code('pkg/marker.py', 'MARKER = 1\n'))
    worker = _worker('w1', [rel, 'pkg/marker.py'], code)

    scoped = _run(scoped_repo, worker, f'diff-{name}-scoped')
    complete = _run(complete_repo, worker, f'diff-{name}-complete',
                    force_complete=True)
    report.timings = {
        'T_scoped_total_ms': scoped.elapsed_ms,
        'T_scoped_validation_ms': scoped.validation_ms,
        'T_complete_total_ms': complete.elapsed_ms,
        'T_complete_validation_ms': complete.validation_ms,
    }
    if scoped.payload.get('validation_mode') != 'SCOPED':
        report.mismatches.append(
            f"expected SCOPED, got {scoped.payload.get('validation_mode')}"
            f" ({scoped.payload.get('fallback_reason')})")
        return report

    dims_scoped = _nine_dims(scoped)
    dims_complete = _nine_dims(complete)

    equal_dims = ['declared_scope', 'observed_scope', 'validation_outcome',
                  'worker_state', 'target_tree_hash',
                  'authoritative_canonical', 'seal_and_replay']
    for dim in equal_dims:
        if dims_scoped[dim] != dims_complete[dim]:
            left, right = dims_scoped[dim], dims_complete[dim]
            if isinstance(left, bytes):
                left, right = left[:160], right[:160]
            report.mismatches.append(
                f'{dim}: scoped={left!r} complete={right!r}')
    if not all(dims_scoped['seal_and_replay'].values()):
        report.mismatches.append(
            f"scoped seal/replay incomplete: {dims_scoped['seal_and_replay']}")
    if not all(dims_complete['seal_and_replay'].values()):
        report.mismatches.append(
            f"complete seal/replay incomplete: {dims_complete['seal_and_replay']}")
    report.notes.append(
        f"canonical_byte_equal="
        f"{dims_scoped['authoritative_canonical'] == dims_complete['authoritative_canonical']}"
        f" seal={dims_scoped['seal_and_replay']}"
        f" outcome={dims_scoped['validation_outcome']}"
        f" target_tree={scoped.payload.get('target_tree_sha', '')[:12]}")
    return report


def _single_case(name: str, base: Path, work: Path, rel: str,
                 content: str, expected_reason: str,
                 prep: dict[str, str] | None = None) -> CaseReport:
    """Ineligible change: engine must fall back with the expected
    machine-readable reason; the COMPLETE path must still succeed.

    ``prep`` files are committed into the fixture BASELINE before the
    worker runs (they are pre-existing state, not the change under
    test): this is how poison boundaries that must not break the check
    matrix (uncollected helper modules) are injected.
    """
    report = CaseReport(name=name, kind='ineligible', eligible=False)
    repo = _copy_case(base, work / f'{name}-single')
    if prep:
        for prep_rel, prep_text in prep.items():
            path = repo / prep_rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prep_text, encoding='utf-8')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-q', '-m', 'baseline helpers')
    code = (_write_code(rel, content) + ';'
            + _write_code('pkg/marker.py', 'MARKER = 1\n'))
    worker = _worker('w1', [rel, 'pkg/marker.py'], code)
    run = _run(repo, worker, f'diff-{name}')
    report.timings = {
        'T_total_ms': run.elapsed_ms,
        'T_validation_ms': run.validation_ms,
    }
    if run.payload.get('validation_mode') != 'COMPLETE':
        report.mismatches.append(
            f"expected COMPLETE, got {run.payload.get('validation_mode')}")
    fallback = run.payload.get('fallback_reason')
    if fallback != expected_reason:
        report.mismatches.append(
            f'fallback {fallback!r} != {expected_reason!r}')
    if run.state != 'DONE':
        report.mismatches.append(f'worker state {run.state!r}')
    integrity = _seal_and_replay(run)
    if not all(integrity.values()):
        report.mismatches.append(f'integrity {integrity}')
    report.notes.append(f'fallback={fallback} checks={run.checks}')
    return report


def main() -> int:
    work = Path(__file__).resolve().parent / 'results' / 'differential'
    if work.exists():
        # git objects are read-only on Windows: chmod before unlink
        def _writable(func, path, _exc):  # pragma: no cover - win specific
            os.chmod(path, stat.S_IWRITE)
            func(path)
        shutil.rmtree(work, onerror=_writable)
    work.mkdir(parents=True)
    base = _build_base(work)

    reports: list[CaseReport] = []

    # --- eligible pairs (SCOPED vs COMPLETE on identical copies) ---
    reports.append(_paired_case(
        'disjoint_leaf', base, work, *LEAF_CHANGE))
    reports.append(_paired_case(
        'changed_test', base, work, *TEST_CHANGE))
    reports.append(_paired_case(
        'transitive_dependency', base, work, *LEAF_CHANGE))

    # --- ineligible singles (engine verdict + healthy COMPLETE run) ---
    reports.append(_single_case(
        's3_surface', base, work, 'pkg/__init__.py',
        '\ndef public_api():\n    return 1\n',
        scoping.F_FORBIDDEN_SCOPE_LEVEL))
    reports.append(_single_case(
        'dynamic_import', base, work, 'pkg/leaf.py',
        'def leaf_value():\n    return 7\n\n\ndef _hidden():\n'
        '    return __import__("json")\n',
        # runtime ordering: the dynamic marker makes scope status
        # uncertain -> classification UNKNOWN -> engine refuses at the
        # classification gate (the DYNAMIC_IMPORT poison path is
        # covered by tests/test_scoped_evidence.py unit tests)
        scoping.F_UNKNOWN_CLASSIFICATION))
    reports.append(_single_case(
        'unresolved_consumer', base, work, 'pkg/leaf.py',
        LEAF_CHANGE[1], scoping.F_UNRESOLVED_BOUNDARY,
        prep={UNRES_HELPER[0]: UNRES_HELPER[1]}))
    reports.append(_single_case(
        'external_boundary', base, work, 'pkg/leaf.py',
        LEAF_CHANGE[1], scoping.F_EXTERNAL_BOUNDARY,
        prep={GHOST_HELPER[0]: GHOST_HELPER[1]}))
    reports.append(_single_case(
        'non_python_change', base, work, 'README.md',
        '# differential fixture changed\n',
        scoping.F_PYTEST_CLOSURE_UNPROVEN))

    # --- malformed evidence: tamper sealed record, verification dies ---
    malformed = CaseReport(name='malformed_evidence', kind='resilience',
                           eligible=False)
    repo = _copy_case(base, work / 'malformed-evidence')
    code = (_write_code('pkg/leaf.py', LEAF_CHANGE[1]) + ';'
            + _write_code('pkg/marker.py', 'MARKER = 1\n'))
    worker = _worker('w1', ['pkg/leaf.py', 'pkg/marker.py'], code)
    run = _run(repo, worker, 'diff-malformed')
    malformed.notes.append(f'pre-tamper state={run.state} '
                           f'reason={run.reason!r} checks={run.checks} '
                           f'mode={run.payload.get("validation_mode")} '
                           f'fallback={run.payload.get("fallback_reason")}')
    if not run.evidence_path.is_file():
        malformed.mismatches.append(
            f'no sealed evidence produced (state={run.state}, '
            f'reason={run.reason})')
    else:
        run.evidence_path.write_text('{"schema": 1, "sta',
                                     encoding='utf-8')
        try:
            verify_worker_evidence(run.evidence_path,
                                   worktree=run.worktree)
            malformed.mismatches.append(
                'tampered evidence verified cleanly')
        except (OrchestratorError, evidence.EvidenceError,
                json.JSONDecodeError):
            malformed.notes.append(
                'tampered evidence rejected (fail-closed)')
    reports.append(malformed)

    # --- report (spec section 5 + section 8) ---
    print('=' * 72)
    print('PHASE 5.0 DIFFERENTIAL VALIDATION (offline qualification)')
    print('=' * 72)
    pairs = [r for r in reports if r.kind == 'paired']
    singles = [r for r in reports if r.kind == 'ineligible']
    mismatch_total = sum(len(r.mismatches) for r in reports)
    for r in reports:
        status = 'OK' if not r.mismatches else 'MISMATCH'
        print(f'[{status:8}] {r.kind:10} {r.name}')
        for note in r.notes:
            print(f'            {note}')
        for m in r.mismatches:
            print(f'            !! {m}')
        for key in sorted(r.timings):
            print(f'            {key}={r.timings[key]:.1f}')

    print('-' * 72)
    print(f'corpus_size={len(reports)} '
          f'(paired={len(pairs)}, ineligible={len(singles)}, '
          f'resilience={len(reports) - len(pairs) - len(singles)})')
    equivalent = sum(1 for r in pairs if not r.mismatches)
    print(f'scoped_complete_equivalent={equivalent}/{len(pairs)}')
    print(f'mismatches={mismatch_total}')
    print('dimensions_compared='
          'classification,declared_scope,observed_scope,'
          'eligibility_verdict,validation_outcome,worker_state,'
          'target_tree_hash,authoritative_canonical,seal+replay')
    print('replay.verify_record+verify_bytes asserted per case; '
          'seal asserted via verify_worker_evidence(worktree)')

    print('-' * 72)
    print('SECTION 8 COST MODEL (fixture-repo measurements)')
    for r in pairs:
        t = r.timings
        if not t or not t['T_scoped_total_ms']:
            continue
        speedup = t['T_complete_total_ms'] / t['T_scoped_total_ms']
        print(f"  {r.name}: "
              f"T_validation SCOPED={t['T_scoped_validation_ms']:.0f}ms "
              f"COMPLETE={t['T_complete_validation_ms']:.0f}ms | "
              f"T_total SCOPED={t['T_scoped_total_ms']:.0f}ms "
              f"COMPLETE={t['T_complete_total_ms']:.0f}ms | "
              f"speedup={speedup:.1f}x")
    print('  (real-repo numbers: benchmarks/run_scoped_dogfood.py)')

    if mismatch_total:
        print('DIFFERENTIAL RESULT: FAILED -- SCOPED qualification '
              'rejected for mismatching cases; COMPLETE remains '
              'authoritative (nothing weakened).')
        return 1
    print('DIFFERENTIAL RESULT: ALL CASES QUALIFIED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
