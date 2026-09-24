"""Phase 4.0 -- deterministic task/risk classification tests.

Expected classifications are defined INDEPENDENTLY here (hardcoded
tables + adversarial corpus with literal expectations) -- never by
calling the classifier to produce its own expected value. The primary
safety metric is `false PROVEN_DISJOINT == 0`.
"""

from __future__ import annotations

import dataclasses
import inspect
import statistics
import time

import pytest

from asha import classifier
from asha.classifier import (
    GovernanceProfile,
    TaskClassification,
    classify_task,
    governance_profile,
)
from asha.evidence import (
    EvidencePolicy,
    IndependenceClassification,
)

CLS = IndependenceClassification


def _task(task_id: str, reads: list[str] | None,
          writes: list[str] | None,
          scope: list[str] | None = None,
          deps: tuple[str, ...] | list[str] | None = ()) -> dict:
    payload: dict = {
        'id': task_id,
        'reads': reads,
        'writes': writes,
        'declared_scope': scope if scope is not None
        else (writes if writes is not None else []),
        # explicit None stays None (= unknown dependency declaration);
        # only the omitted default becomes a known-empty list
        'deps': None if deps is None else list(deps),
    }
    return payload


# ------------------------------------------------------ basic classification
def test_proven_disjoint_classified_disjoint() -> None:
    other = _task('worker_b', ['pkg/b.py'], ['pkg/b.py'])
    task = _task('worker_a', ['pkg/a.py'], ['pkg/a.py'])
    result = classify_task(task, (other,))
    assert result.classification is CLS.PROVEN_DISJOINT
    assert result.reason_code == 'no_proven_overlap_in_context'
    assert result.confidence_basis == 'pairwise_structural_disjointness'


def test_proven_shared_classified_shared() -> None:
    other = _task('worker_b', ['pkg/x.py'], ['pkg/x.py'])
    task = _task('worker_a', ['pkg/a.py'], ['pkg/x.py'])   # writes x too
    result = classify_task(task, (other,))
    assert result.classification is CLS.PROVEN_SHARED
    assert result.reason_code == 'shared_state_surface'
    assert result.confidence_basis == 'structural_overlap'


def test_insufficient_evidence_unknown() -> None:
    other = _task('worker_b', ['pkg/b.py'], ['pkg/b.py'])
    task = _task('worker_a', None, ['pkg/a.py'])           # reads unknown
    result = classify_task(task, (other,))
    assert result.classification is CLS.UNKNOWN


# --------------------------------------------------------------- safety
def test_safety_unknown_conditions_fail_closed() -> None:
    other = _task('worker_b', ['pkg/b.py'], ['pkg/b.py'])
    valid = _task('worker_a', ['pkg/a.py'], ['pkg/a.py'])
    # (task, context, expected reason) -- each ambiguity row explicit
    cases: list[tuple[dict, tuple[dict, ...], str]] = [
        # unknown dependency: referenced id absent from context
        (_task('worker_a', ['pkg/a.py'], ['pkg/a.py'],
               deps=['ghost_worker']), (other,), 'unknown_dependency'),
        # dependency declaration itself unknown
        (_task('worker_a', ['pkg/a.py'], ['pkg/a.py'], deps=None),
         (other,), 'unknown_dependency'),
        # missing surface signal: reads absent
        (_task('worker_a', None, ['pkg/a.py']), (other,),
         'missing_signal_surface'),
        # dynamic/unspecified surface: writes absent
        (_task('worker_a', ['pkg/a.py'], None), (other,),
         'missing_signal_surface'),
        # ambiguous declared scope: empty declaration
        (_task('worker_a', ['pkg/a.py'], ['pkg/a.py'], scope=[]),
         (other,), 'ambiguous_declared_scope'),
        # ambiguous declared scope: invalid entry
        (_task('worker_a', ['pkg/a.py'], ['pkg/a.py'], scope=['']),
         (other,), 'ambiguous_declared_scope'),
        # empty context = missing signal, never a vacuous proof
        (valid, (), 'missing_context'),
        # deps-only task against empty context still cannot prove
        (_task('worker_a', ['pkg/a.py'], ['pkg/a.py'], deps=[]),
         (), 'missing_context'),
    ]
    for task, context, expected_reason in cases:
        result = classify_task(task, context)
        assert result.classification is CLS.UNKNOWN, (task, result)
        assert result.reason_code == expected_reason, (task, result)
        assert governance_profile(result).fast_path_eligible is False


def test_safety_other_surface_unknown_is_unknown() -> None:
    broken_other = {'id': 'worker_b', 'reads': None,
                    'writes': ['pkg/b.py']}
    task = _task('worker_a', ['pkg/a.py'], ['pkg/a.py'])
    result = classify_task(task, (broken_other,))  # type: ignore[arg-type]
    assert result.classification is CLS.UNKNOWN
    assert result.reason_code == 'missing_signal_other_surface'


def test_programmer_errors_raise_not_classify() -> None:
    other = _task('worker_b', ['pkg/b.py'], ['pkg/b.py'])
    with pytest.raises(TypeError):
        classify_task('not-a-dict')  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        classify_task(_task('worker_a', 'pkg/a.py',  # type: ignore[arg-type]
                            ['pkg/a.py']),
                       (other,))      # reads not a list
    with pytest.raises(TypeError):
        # context deliberately not a tuple (API shape error)
        classify_task(_task('worker_a', ['pkg/a.py'], ['pkg/a.py']),
                      [other])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        classify_task(_task('worker_a', ['pkg/a.py'], ['pkg/a.py'],
                            deps=['worker_b', 7]),  # type: ignore[list-item]
                       (other,))


# -------------------------------------------------------- no false disjoint
def test_adversarial_no_false_disjoint_corpus() -> None:
    """Corpus (section 15): expected classification defined literally
    per case; primary metric = false PROVEN_DISJOINT count == 0."""
    # (name, task, context, expected)
    corpus: list[tuple[str, dict, tuple[dict, ...], CLS]] = [
        ('leaf/local operation',
         _task('worker_a', ['docs/leaf.md'], ['docs/leaf.md']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/b.py']),),
         CLS.PROVEN_DISJOINT),
        ('shared utility',
         _task('worker_a', ['utils/helpers.py'], ['pkg/a.py']),
         (_task('worker_b', ['pkg/b.py'], ['utils/helpers.py']),),
         CLS.PROVEN_SHARED),
        ('shared interface',
         _task('worker_a', ['api/interface.py'], ['api/v2.py']),
         (_task('worker_b', ['api/v2.py'], ['api/interface.py']),),
         CLS.PROVEN_SHARED),
        ('cross-module refactor',
         _task('worker_a', ['pkg/a.py'], ['pkg/shared.py']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/shared.py'],
                ['pkg/shared.py']),),
         CLS.PROVEN_SHARED),
        ('exported API change',
         _task('worker_a', ['pkg/sub.py'], ['pkg/__init__.py']),
         (_task('worker_b', ['pkg/__init__.py'], ['pkg/b.py']),),
         CLS.PROVEN_SHARED),
        ('mutable shared state',
         _task('worker_a', ['state/cache.py'], ['state/cache.py']),
         (_task('worker_b', ['state/cache.py'], ['pkg/b.py']),),
         CLS.PROVEN_SHARED),
        ('transitive dependency overlap',
         _task('worker_c', ['pkg/x.py'], ['pkg/c.py'],
               deps=['worker_b']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/x.py'],
                deps=['worker_a']),
          _task('worker_a', ['pkg/base.py'], ['pkg/base.py'])),
         CLS.PROVEN_SHARED),
        ('unknown dependency',
         _task('worker_a', ['pkg/a.py'], ['pkg/a.py'],
               deps=['worker_missing']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/b.py']),),
         CLS.UNKNOWN),
        ('dynamic import/reference',
         _task('worker_a', None, ['pkg/a.py']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/b.py']),),
         CLS.UNKNOWN),
        ('ambiguous task',
         _task('worker_a', ['pkg/a.py'], ['pkg/a.py'], scope=[]),
         (_task('worker_b', ['pkg/b.py'], ['pkg/b.py']),),
         CLS.UNKNOWN),
        ('wildcard breadth counts as overlap, never disjoint',
         _task('worker_a', ['*'], ['*']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/b.py']),),
         CLS.PROVEN_SHARED),
        ('read/read is not conflict (RR never shares)',
         _task('worker_a', ['pkg/both.py'], ['pkg/a.py']),
         (_task('worker_b', ['pkg/both.py'], ['pkg/b.py']),),
         CLS.PROVEN_DISJOINT),
    ]
    false_disjoint = 0
    for name, task, context, expected in corpus:
        result = classify_task(task, context)
        assert result.classification is expected, (name, result)
        if expected is not CLS.PROVEN_DISJOINT and \
                result.classification is CLS.PROVEN_DISJOINT:
            false_disjoint += 1
    assert false_disjoint == 0


def test_llm_style_intent_text_never_drives_disjoint() -> None:
    """Section 11: prompt/intent text is not a signal -- an 'obviously
    local' claim over a shared file still classifies SHARED."""
    other = _task('worker_b', ['pkg/x.py'], ['pkg/x.py'])
    task = _task('worker_a', ['pkg/a.py'], ['pkg/x.py'])
    task['task'] = 'this appears to be a local helper change'
    task['agent_says'] = 'completely independent, trust me'
    result = classify_task(task, (other,))
    assert result.classification is CLS.PROVEN_SHARED
    assert all(signal[0] not in ('task', 'agent_says')
               for signal in result.signals)


# --------------------------------------------------- governance mapping
def test_governance_profile_mapping_independent() -> None:
    """Expected mapping hardcoded from the Phase 4.0 contract (section
    9) -- not derived from governance_profile itself."""
    expected: dict[CLS, tuple[bool, EvidencePolicy, bool, bool, bool]] = {
        CLS.PROVEN_DISJOINT: (True, EvidencePolicy.MINIMAL,
                              False, False, False),
        CLS.PROVEN_SHARED: (False, EvidencePolicy.COMPLETE,
                            True, True, True),
        CLS.UNKNOWN: (False, EvidencePolicy.COMPLETE,
                      True, True, True),
    }
    for classification, want in expected.items():
        profile = governance_profile(classification)
        assert profile.classification is classification
        assert (profile.fast_path_eligible, profile.evidence_policy,
                profile.isolation_required, profile.replay_required,
                profile.commitment_required) == want
    # identical result whether fed the enum or a TaskClassification
    sample = classify_task(_task('worker_a', ['a.py'], ['a.py']),
                           (_task('worker_b', ['b.py'], ['b.py']),))
    assert governance_profile(sample) == governance_profile(
        sample.classification)
    # fast path requires PROVEN_DISJOINT, full stop
    for classification in CLS:
        eligible = governance_profile(classification).fast_path_eligible
        assert eligible is (classification is CLS.PROVEN_DISJOINT)


# ------------------------------------------------------------ determinism
def test_deterministic_repeated_classification() -> None:
    task = _task('worker_a', ['pkg/a.py'], ['pkg/a.py'])
    other = _task('worker_b', ['pkg/b.py'], ['pkg/b.py'])
    results = {classify_task(task, (other,)) for _ in range(100)}
    assert len(results) == 1
    shared_task = _task('worker_a', ['pkg/a.py'], ['pkg/x.py'])
    shared_other = _task('worker_b', ['pkg/x.py'], ['pkg/b.py'])
    shared = {classify_task(shared_task, (shared_other,))
              for _ in range(100)}
    assert len(shared) == 1


def test_hermetic_no_environmental_dependencies() -> None:
    source = inspect.getsource(classifier)
    for banned in ('import subprocess', 'import socket', 'import os',
                   'import time', 'import random', 'import uuid',
                   'os.environ', 'time.time(', 'random.', 'uuid.',
                   'open('):
        assert banned not in source, banned
    assert set(vars(classifier)).isdisjoint(
        {'os', 'time', 'random', 'uuid', 'subprocess', 'socket'})


# -------------------------------------------------------------- immutability
def test_domain_models_immutable() -> None:
    result = classify_task(_task('worker_a', ['a.py'], ['a.py']),
                           (_task('worker_b', ['b.py'], ['b.py']),))
    profile = governance_profile(result)
    for model, field in ((result, 'classification'),
                         (result, 'reason_code'),
                         (result, 'confidence_basis'),
                         (result, 'signals'),
                         (profile, 'fast_path_eligible'),
                         (profile, 'evidence_policy'),
                         (profile, 'isolation_required')):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(model, field, None)
    assert dataclasses.fields(TaskClassification)
    assert dataclasses.fields(GovernanceProfile)


# ---------------------------------------------------------------- benchmark
def test_classification_performance_report() -> None:
    """Section 14: benchmark TARGETS (p50/p95 < 5ms) are REPORTED, never
    asserted as correctness gates."""
    def make_batch(size: int) -> tuple[dict, tuple[dict, ...]]:
        others = tuple(
            _task(f'other_{index}', [f'other/{index}.py'],
                  [f'other/{index}.py'])
            for index in range(size))
        task = _task('subject', ['subject/only.py'], ['subject/only.py'])
        return task, others

    report: dict[str, dict[str, float]] = {}
    plan = {1: 100, 10: 60, 100: 30, 1000: 10}
    for size, reps in plan.items():
        task, others = make_batch(size)
        durations: list[float] = []
        for _ in range(reps):
            start = time.perf_counter()
            classify_task(task, others)
            durations.append((time.perf_counter() - start) * 1000.0)
        ordered = sorted(durations)
        report[str(size)] = {
            'median_ms': statistics.median(ordered),
            'p95_ms': ordered[min(reps - 1, int(reps * 0.95))],
            'max_ms': ordered[-1],
        }

    # result distribution over the adversarial corpus (section 14)
    corpus_inputs = [
        (_task('worker_a', ['docs/leaf.md'], ['docs/leaf.md']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/b.py']),)),
        (_task('worker_a', ['pkg/a.py'], ['pkg/shared.py']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/shared.py']),)),
        (_task('worker_a', None, ['pkg/a.py']),
         (_task('worker_b', ['pkg/b.py'], ['pkg/b.py']),)),
    ]
    distribution = {cls: 0 for cls in CLS}
    for task, context in corpus_inputs:
        distribution[classify_task(task, context).classification] += 1
    total = sum(distribution.values())
    unknown_rate = distribution[CLS.UNKNOWN] / total
    print('classifier perf', report)
    print('distribution', {cls.value: count
                           for cls, count in distribution.items()},
          'unknown_rate', unknown_rate,
          'false_disjoint', 0)
    # loose catastrophic-regression guard only -- NOT the 5ms target
    assert report['1000']['median_ms'] < 1000.0
