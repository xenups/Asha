"""Phase 3.0 -- authoritative evidence contract & wire-format tests.

Verification matrix from the phase spec:
  - policy resolution totality (fail-closed)
  - deterministic, caller-independent identity-key derivation
  - canonicalization determinism under shuffled inputs
  - canonicalization excludes AuditMetadata entirely
  - GOR calculation + non-positive workload rejection
  - frozen-model immutability / structural type safety
"""

import dataclasses
import hashlib
import inspect

import pytest

from asha.evidence import (
    AuditMetadata,
    AuthoritativeEvidence,
    EvidencePolicy,
    GovernanceVerdict,
    IndependenceClassification,
    NormalizedFact,
    ObservedScope,
    ScopeCaptureMode,
    VerdictStatus,
    canonicalize_evidence,
    resolve_evidence_policy,
)


def _scope(reads: tuple[str, ...] = ('pkg/b.py', 'pkg/a.py'),
           writes: tuple[str, ...] = ('pkg/out.py',)) -> ObservedScope:
    return ObservedScope(
        reads=frozenset(reads),
        writes=frozenset(writes),
        capture_mode=ScopeCaptureMode.STRICT,
    )


def _fact(category: str = 'ast_dependency',
          source: str = 'pkg/b.py',
          target: str = 'pkg/a.py',
          status: str = 'observed',
          attributes: tuple[tuple[str, str], ...] = (
              ('kind', 'ImportFrom'), ('line', '3')),
) -> NormalizedFact:
    return NormalizedFact(category=category, source=source, target=target,
                          status=status, attributes=attributes)


def _verdict(status: VerdictStatus = VerdictStatus.PASS,
             reason_code: str = 'dependency_verified',
             ) -> GovernanceVerdict:
    return GovernanceVerdict(status=status, reason_code=reason_code)


def _evidence(worker_id: str = 'worker_b',
              generation: int = 7,
              base_tree_sha: str = 'base42base42base42',
              target_tree_sha: str = 'target99target99',
              scope: ObservedScope | None = None,
              facts: tuple[NormalizedFact, ...] = (),
              verdict: GovernanceVerdict | None = None,
              ) -> AuthoritativeEvidence:
    return AuthoritativeEvidence.create(
        worker_id=worker_id,
        generation=generation,
        base_tree_sha=base_tree_sha,
        target_tree_sha=target_tree_sha,
        observed_scope=scope if scope is not None else _scope(),
        normalized_facts=facts,
        verdict=verdict if verdict is not None else _verdict(),
    )


def test_policy_resolution_totality() -> None:
    assert (resolve_evidence_policy(IndependenceClassification.PROVEN_DISJOINT)
            is EvidencePolicy.MINIMAL)
    assert (resolve_evidence_policy(IndependenceClassification.PROVEN_SHARED)
            is EvidencePolicy.COMPLETE)
    assert (resolve_evidence_policy(IndependenceClassification.UNKNOWN)
            is EvidencePolicy.COMPLETE)


def test_identity_key_deterministic_derivation() -> None:
    first = _evidence()
    second = _evidence(target_tree_sha='completely-different')
    assert first.execution_identity_key == second.execution_identity_key
    expected = hashlib.sha256(
        b'base42base42base42:worker_b:7').hexdigest()
    assert first.execution_identity_key == expected
    assert len(first.execution_identity_key) == 64


def test_identity_key_sensitivity() -> None:
    base = _evidence().execution_identity_key
    assert _evidence(worker_id='worker_x').execution_identity_key != base
    assert _evidence(generation=8).execution_identity_key != base
    assert _evidence(base_tree_sha='other-base').execution_identity_key != base


def test_canonicalization_wire_format_determinism() -> None:
    shuffled = AuthoritativeEvidence.create(
        worker_id='worker_b',
        generation=7,
        base_tree_sha='base42base42base42',
        target_tree_sha='target99target99',
        observed_scope=_scope(reads=('pkg/c.py', 'pkg/a.py', 'pkg/b.py'),
                              writes=('pkg/z.py', 'pkg/y.py')),
        normalized_facts=(
            _fact(category='scope', source='z', target='a',
                  attributes=(('z_attr', '1'), ('a_attr', '2'))),
            _fact(),
        ),
        verdict=_verdict(reason_code='دلیل_تأیید'),
    )
    ordered = _evidence(
        scope=ObservedScope(
            reads=frozenset(('pkg/a.py', 'pkg/b.py', 'pkg/c.py')),
            writes=frozenset(('pkg/y.py', 'pkg/z.py')),
            capture_mode=ScopeCaptureMode.STRICT,
        ),
        facts=(
            _fact(),
            _fact(category='scope', source='z', target='a',
                  attributes=(('a_attr', '2'), ('z_attr', '1'))),
        ),
        verdict=_verdict(reason_code='دلیل_تأیید'),
    )
    first = canonicalize_evidence(shuffled)
    second = canonicalize_evidence(ordered)
    assert first == second
    text = first.decode('utf-8')          # strict UTF-8 round-trip
    assert text.encode('utf-8') == first
    assert ', ' not in text                # compact separators
    assert '{ ' not in text and '": ' not in text
    assert '\\u062f' not in text           # ensure_ascii=False: raw UTF-8
    assert 'دلیل_تأیید'.encode() in first
    # reads/writes serialized as lexicographically sorted arrays
    assert '"reads":["pkg/a.py","pkg/b.py","pkg/c.py"]' in text
    assert '"writes":["pkg/y.py","pkg/z.py"]' in text


def test_canonicalization_excludes_audit_metadata() -> None:
    evidence = _evidence()
    before = canonicalize_evidence(evidence)
    audit = AuditMetadata(
        ephemeral_execution_id='eph-1',
        wall_start_iso='2026-09-24T00:00:00+00:00',
        wall_end_iso='2026-09-24T00:00:09+00:00',
        t_engine_ms=1200.0,
        t_workload_ms=800.0,
        raw_stdout_sample='stdout',
        raw_stderr_sample='stderr',
        host_metadata=(('hostname', 'runner-1'),),
    )
    assert audit.gor_ratio == 1.5
    assert canonicalize_evidence(evidence) == before
    params = inspect.signature(canonicalize_evidence).parameters
    assert tuple(params) == ('evidence',)


def test_gor_calculation_and_validation() -> None:
    audit = AuditMetadata(
        ephemeral_execution_id='eph-2',
        wall_start_iso='2026-09-24T00:00:00+00:00',
        wall_end_iso='2026-09-24T00:00:04+00:00',
        t_engine_ms=250.0,
        t_workload_ms=1000.0,
    )
    assert audit.gor_ratio == 0.25
    for bad in (0.0, -5.0):
        broken = AuditMetadata(
            ephemeral_execution_id='eph-3',
            wall_start_iso='2026-09-24T00:00:00+00:00',
            wall_end_iso='2026-09-24T00:00:01+00:00',
            t_engine_ms=10.0,
            t_workload_ms=bad,
        )
        with pytest.raises(ValueError, match='t_workload_ms'):
            _ = broken.gor_ratio


def test_immutability_and_type_safety() -> None:
    evidence = _evidence()
    scope = evidence.observed_scope
    fact = _fact()
    for instance, attr in (
            (evidence, 'worker_id'),
            (evidence, 'execution_identity_key'),
            (scope, 'reads'),
            (fact, 'category'),
            (evidence.verdict, 'status'),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, attr, 'mutated')
    assert isinstance(scope.reads, frozenset)
    assert isinstance(scope.writes, frozenset)
    assert isinstance(evidence.normalized_facts, tuple)
    assert all(isinstance(pair, tuple) and len(pair) == 2
               for pair in fact.attributes)
    audit = AuditMetadata(
        ephemeral_execution_id='eph-4',
        wall_start_iso='2026-09-24T00:00:00+00:00',
        wall_end_iso='2026-09-24T00:00:01+00:00',
        t_engine_ms=1.0,
        t_workload_ms=2.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        audit.t_engine_ms = 999.0  # type: ignore[misc]
