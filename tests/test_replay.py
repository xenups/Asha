"""Phase 3.2 -- deterministic authoritative replay invariants.

Every expected value here is specified INDEPENDENTLY of the replay
implementation: identity keys are recomputed from the Phase 3.0 contract
formula inline, expected verdict states are hardcoded tables, expected
divergences are literal field/reason constants. No test calls replay to
produce its own expected result.
"""

from __future__ import annotations

import builtins
import dataclasses
import hashlib
import json
import os
import socket
import statistics
import subprocess
import time
from typing import Any

import pytest

from asha import replay
from asha.evidence import (
    AuditMetadata,
    AuthoritativeEvidence,
    GovernanceVerdict,
    NormalizedFact,
    ObservedScope,
    ScopeCaptureMode,
    VerdictStatus,
    canonicalize_evidence,
)
from asha.replay import (
    ReplayDivergence,
    ReplayParseError,
    verify_bytes,
    verify_cohort,
    verify_record,
)

# ---------------------------------------------------------------- fixtures
# Independent identity expectation: Phase 3.0 contract formula applied by
# hand inside the test (never via replay).
EXPECTED_KEY = hashlib.sha256(
    b'base42base42base42:worker_b:7').hexdigest()

# Hardcoded reason-class table: the states the runtime actually assigns
# (greped from scheduler state transitions + Phase 3.0 contract
# fixtures). Independent of replay's internal sets.
EXPECTED_STATE_BY_REASON = {
    'evidence_sealed': 'PASS',
    'dependency_verified': 'PASS',
    'timeout_exceeded': 'FAIL',
    'worker_exit_3': 'FAIL',
    'verification_failed:ruff': 'FAIL',
    'upstream_failure': 'BLOCKED',
    'cycle_detected': 'BLOCKED',
    'dependency_not_finished:worker_a': 'BLOCKED',
}


def _scope(reads: tuple[str, ...] = ('pkg/a.py',),
           writes: tuple[str, ...] = ('pkg/b.py',)) -> ObservedScope:
    return ObservedScope(reads=frozenset(reads),
                         writes=frozenset(writes),
                         capture_mode=ScopeCaptureMode.STRICT)


def _fact(source: str = 'pkg/b.py',
          target: str = 'pkg/a.py',
          status: str = 'observed') -> NormalizedFact:
    return NormalizedFact(category='ast_dependency', source=source,
                          target=target, status=status,
                          attributes=(('kind', 'ImportFrom'),
                                      ('line', '3')))


def _record(
        worker_id: str = 'worker_b',
        generation: int = 7,
        base_tree_sha: str = 'base42base42base42',
        target_tree_sha: str = 'target99target99',
        scope: ObservedScope | None = None,
        facts: tuple[NormalizedFact, ...] | None = None,
        verdict: GovernanceVerdict | None = None,
) -> AuthoritativeEvidence:
    return AuthoritativeEvidence.create(
        worker_id=worker_id,
        generation=generation,
        base_tree_sha=base_tree_sha,
        target_tree_sha=target_tree_sha,
        observed_scope=scope if scope is not None else _scope(),
        normalized_facts=() if facts is None else facts,
        verdict=verdict if verdict is not None else GovernanceVerdict(
            status=VerdictStatus.PASS, reason_code='evidence_sealed'),
    )


def _wire(**kwargs: Any) -> bytes:
    return canonicalize_evidence(_record(**kwargs))


def _tamper(evidence: AuthoritativeEvidence,
            **changes: Any) -> AuthoritativeEvidence:
    """Record-level mutation: the schema is frozen, so dataclasses.replace
    is the only mutation channel a tamper has."""
    return dataclasses.replace(evidence, **changes)


# ------------------------------------------------------- INV-R1 hermetic
def test_hermetic_replay_zero_subprocesses() -> None:
    subprocess_calls: list[tuple[str, ...]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen
    real_open, real_os_open = builtins.open, os.open
    real_connect = socket.create_connection

    def spy_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get('args')
        subprocess_calls.append(tuple(str(a) for a in argv))
        return real_run(*args, **kwargs)

    def spy_popen(*args, **kwargs):
        argv = args[0] if args else kwargs.get('args')
        subprocess_calls.append(tuple(str(a) for a in argv))
        return real_popen(*args, **kwargs)

    def trip_fs(*args, **kwargs):
        raise AssertionError('hermetic violation: filesystem access')

    def trip_net(*args, **kwargs):
        raise AssertionError('hermetic violation: network access')

    # install = wrappers/tripwires; restore = TRUE originals captured
    # BEFORE patching (re-reading them inside finally would restore the
    # tripwire itself and poison every later test in the suite)
    patches = [
        (subprocess, 'run', spy_run),
        (subprocess, 'Popen', spy_popen),
        (builtins, 'open', trip_fs),
        (os, 'open', trip_fs),
        (socket, 'create_connection', trip_net),
    ]
    originals = [
        (subprocess, 'run', real_run),
        (subprocess, 'Popen', real_popen),
        (builtins, 'open', real_open),
        (os, 'open', real_os_open),
        (socket, 'create_connection', real_connect),
    ]
    for target, name, replacement in patches:
        setattr(target, name, replacement)
    # prove the probes are actually installed (an uninstalled tripwire
    # would make every assertion below vacuous)
    assert subprocess.run is spy_run and subprocess.Popen is spy_popen
    assert builtins.open is trip_fs and os.open is trip_fs
    assert socket.create_connection is trip_net
    try:
        with pytest.raises(AssertionError):
            # tripwire probe: must raise BEFORE any file is opened
            builtins.open('probe')  # noqa: SIM115
        wire = _wire()
        record = _record()
        cohort = (_record(worker_id='worker_a',
                          scope=_scope(reads=(), writes=('pkg/a.py',))),
                  record)
        result_bytes = verify_bytes(wire)
        verify_record(record)
        verify_cohort(cohort)
    finally:
        for target, name, original in originals:
            setattr(target, name, original)
    # 0 git subprocesses, 0 python subprocesses, 0 filesystem, 0 network
    assert subprocess_calls == []
    assert result_bytes.verified


# --------------------------------------------------- INV-R2 audit parity
def test_replay_audit_irrelevance() -> None:
    wire = _wire()
    audit_a = AuditMetadata(ephemeral_execution_id='eph-111',
                            wall_start_iso='2026-01-01T00:00:00Z',
                            wall_end_iso='2026-01-01T00:00:09Z',
                            t_engine_ms=1.5, t_workload_ms=9.0,
                            raw_stdout_sample='stdout A',
                            raw_stderr_sample='stderr A',
                            host_metadata=(('host', 'a'),
                                           ('pid', '42')))
    audit_b = AuditMetadata(ephemeral_execution_id='eph-222',
                            wall_start_iso='2026-12-31T23:59:59Z',
                            wall_end_iso='2026-12-31T23:59:59Z',
                            t_engine_ms=9999.0, t_workload_ms=0.001,
                            raw_stdout_sample='stdout B',
                            raw_stderr_sample='stderr B',
                            host_metadata=(('host', 'b'),
                                           ('pid', '999')))
    first = verify_bytes(wire)
    # audits exist and differ wildly, yet replay never receives them
    assert audit_a != audit_b
    second = verify_bytes(wire)
    assert first == second
    result_fields = {field.name for field in
                     dataclasses.fields(replay.ReplayResult)}
    audit_fields = {field.name for field in
                    dataclasses.fields(AuditMetadata)}
    assert result_fields.isdisjoint(audit_fields)


# ------------------------------------------------- INV-R3 tamper checks
def test_semantic_tamper_detection() -> None:
    wire = _wire()
    original = verify_bytes(wire)
    assert original.verified

    # (a) base_tree_sha moved while the identity key stayed put
    moved = _tamper(_record(), base_tree_sha='differentbasedifferent')
    result_a = verify_record(moved)
    expected_key_a = hashlib.sha256(
        b'differentbasedifferent:worker_b:7').hexdigest()
    assert result_a.verified is False
    assert result_a.divergences == (
        ReplayDivergence(
            field='execution_identity_key',
            expected=_record().execution_identity_key,
            reconstructed=expected_key_a,
            reason='ERR_IDENTITY_MISMATCH'),
    )

    # (b) fact source escapes the authoritative scope
    escaped_source = verify_record(_record(
        facts=(_fact(source='pkg/evil.py'),)))
    assert escaped_source.verified is False
    assert {item.reason for item in escaped_source.divergences} == {
        'ERR_FACT_SOURCE_OUT_OF_SCOPE'}

    # (c) fact target inconsistent with the read/write scope
    escaped_target = verify_record(_record(
        facts=(_fact(target='pkg/other/x.py'),)))
    assert escaped_target.verified is False
    assert {item.reason for item in escaped_target.divergences} == {
        'ERR_FACT_TARGET_OUT_OF_SCOPE'}

    # (d) fact status outside the repository's vocabulary breaks the
    # derivation chain (a recorded PASS can no longer be re-derived)
    poisoned_record = _record(facts=(_fact(status='FAIL'),))
    poisoned_status = verify_record(poisoned_record)
    assert poisoned_status.verified is False
    assert {item.reason for item in poisoned_status.divergences} == {
        'ERR_FACT_STATUS_INVALID'}

    # divergence information is deterministic (two runs, identical tuples)
    for mutated in (moved, poisoned_record):
        assert verify_record(mutated) == verify_record(mutated)


# --------------------------------------------- INV-R4 verdict soundness
def test_verdict_rederivation_soundness() -> None:
    # all three states derive CORRECTLY when record and reason agree
    for reason, state in EXPECTED_STATE_BY_REASON.items():
        record = _record(verdict=GovernanceVerdict(
            status=VerdictStatus(state), reason_code=reason))
        result = verify_record(record)
        assert result.verified, (reason, result)
        assert result.reconstructed_verdict.status.value == state

    # contradictions in every direction: claim vs independently derived
    contradictions = (
        (VerdictStatus.FAIL, 'evidence_sealed', 'PASS'),
        (VerdictStatus.PASS, 'timeout_exceeded', 'FAIL'),
        (VerdictStatus.PASS, 'cycle_detected', 'BLOCKED'),
        (VerdictStatus.BLOCKED, 'dependency_verified', 'PASS'),
    )
    for claimed, reason, derived in contradictions:
        record = _record(verdict=GovernanceVerdict(
            status=claimed, reason_code=reason))
        result = verify_record(record)
        assert result.verified is False
        assert result.divergences == (
            ReplayDivergence(
                field='verdict.status',
                expected=claimed.value,
                reconstructed=derived,
                reason='ERR_VERDICT_DIVERGENCE'),
        )
        assert result.reconstructed_verdict.status.value == derived

    # documented limitation: an arbitrary (contract-valid) reason makes
    # NO verdict claim instead of guessing
    unknown = _record(verdict=GovernanceVerdict(
        status=VerdictStatus.PASS, reason_code='دلیل_تأیید'))
    result = verify_record(unknown)
    assert result.verified
    assert result.reconstructed_verdict.status is VerdictStatus.PASS


# -------------------------------------------------- INV-R5 cohort scope
def test_cohort_wave_order_replay() -> None:
    worker_a = _record(worker_id='worker_a', generation=3,
                       scope=_scope(reads=('pkg/shared.py',),
                                   writes=('pkg/x.py',)))
    worker_b = _record(worker_id='worker_b', generation=3,
                       scope=_scope(reads=('pkg/x.py',),
                                   writes=('pkg/shared.py',)))
    worker_c = _record(worker_id='worker_c', generation=3,
                       scope=_scope(reads=(), writes=('pkg/z.py',)))
    result = verify_cohort((worker_a, worker_b, worker_c))
    assert result.verified
    # A writes x / B reads x (and B writes shared / A reads shared):
    # the pair interferes, so it must NOT read as safe simultaneous
    # execution -- strongest invariant the schema actually supports
    assert result.conflicting_pairs == (('worker_a', 'worker_b'),)
    assert result.disjoint_pairs == (('worker_a', 'worker_c'),
                                     ('worker_b', 'worker_c'))
    # documented boundary: records carry no timestamps, dependency
    # edges, or dispatch order -- cohort output exposes ONLY pair
    # interference, no queue/wave state exists to fabricate
    assert [field.name for field in dataclasses.fields(
        replay.CohortReplayResult)] == ['verified', 'results',
                                        'conflicting_pairs',
                                        'disjoint_pairs']
    # input order cannot change the reconstruction (sorted by worker id)
    shuffled = verify_cohort((worker_c, worker_b, worker_a))
    assert shuffled == result


# ------------------------------------------------------ INV-R6 latency
def test_replay_performance_target() -> None:
    wire = _wire()
    durations: list[float] = []
    for _ in range(2000):
        start = time.perf_counter()
        verify_bytes(wire)
        durations.append((time.perf_counter() - start) * 1000.0)
    ordered = sorted(durations)
    n = len(ordered)
    report = {
        'n': n,
        'median_ms': statistics.median(ordered),
        'p95_ms': ordered[min(n - 1, int(n * 0.95))],
        'min_ms': ordered[0],
        'max_ms': ordered[-1],
    }
    print('T_replay', report)
    # performance TARGET only (median < 5 ms) -- reported, and guarded
    # only against pathological regression (generous ceiling), never as
    # a machine-speed correctness gate
    assert report['median_ms'] < 100.0, report


# ------------------------------------------------ INV-R7 determinism
def test_replay_deterministic_reconstruction() -> None:
    wire = _wire()
    results = {verify_bytes(wire) for _ in range(50)}
    assert len(results) == 1
    only = next(iter(results))
    assert only.verified
    assert only.execution_identity_key == EXPECTED_KEY
    assert only.reconstructed_verdict.status is VerdictStatus.PASS
    assert only.divergences == ()

    tampered = _tamper(_record(), base_tree_sha='other')
    divergent = {verify_record(tampered) for _ in range(50)}
    assert len(divergent) == 1
    assert next(iter(divergent)).divergences == \
        verify_record(tampered).divergences


# ----------------------------------------------- wire round-trip (§9)
def test_canonical_wire_round_trip() -> None:
    record = _record()
    wire = canonicalize_evidence(record)
    assert verify_bytes(wire).verified
    # canonicalize(parse(canonicalize(E))) == canonicalize(E), byte-wise
    reparsed = replay.parse_evidence(wire)
    assert canonicalize_evidence(reparsed) == wire

    # same document, non-canonical encoding -> deterministic rejection
    document = json.loads(wire.decode('utf-8'))
    loose = json.dumps(document, ensure_ascii=True,
                       indent=2).encode('utf-8')
    assert loose != wire
    result = verify_bytes(loose)
    assert result.verified is False
    assert {item.reason for item in result.divergences} >= {
        'ERR_WIRE_ROUNDTRIP'}


# ------------------------------------ coverage semantics (actual, §7)
def test_scope_coverage_uses_structural_primitives() -> None:
    # directory entry covers children (naive == would reject this)
    directory = _record(scope=_scope(reads=('pkg/',),
                                    writes=('pkg/b.py',)),
                        facts=(_fact(source='pkg/b.py',
                                     target='pkg/sub/mod.py'),))
    assert verify_record(directory).verified
    # strict glob entry (same primitive dispatch safety uses)
    globbed = _record(scope=_scope(reads=('src/*.py',),
                                  writes=('pkg/b.py',)),
                      facts=(_fact(source='pkg/b.py',
                                   target='src/helper.py'),))
    assert verify_record(globbed).verified
    # genuinely outside every entry still fails
    outside = _record(scope=_scope(reads=('pkg/',),
                                  writes=('pkg/b.py',)),
                      facts=(_fact(target='docs/readme.md'),))
    assert verify_record(outside).verified is False


# --------------------------------------------- malformed wire (§13)
def _rejection(data: bytes) -> str:
    with pytest.raises(ReplayParseError) as excinfo:
        verify_bytes(data)
    return str(excinfo.value)


def test_malformed_wire_deterministic_rejection() -> None:
    wire = _wire()
    document = json.loads(wire.decode('utf-8'))

    # invalid utf-8
    assert _rejection(b'\xff\xfe{"broken"').startswith('replay: ')
    # invalid json
    assert _rejection(b'{not json at all').startswith('replay: ')
    # missing required field
    missing = dict(document)
    del missing['generation']
    _rejection(json.dumps(missing).encode('utf-8'))
    # extra unknown field (strict field set)
    extra = dict(document)
    extra['smuggled'] = '1'
    _rejection(json.dumps(extra).encode('utf-8'))
    # invalid enum values
    bad_verdict = dict(document, verdict={'status': 'NEVER',
                                          'reason_code': 'x'})
    _rejection(json.dumps(bad_verdict).encode('utf-8'))
    bad_capture = dict(document, observed_scope=dict(
        document['observed_scope'], capture_mode='FASTEST'))
    _rejection(json.dumps(bad_capture).encode('utf-8'))
    # invalid generation: string, negative, boolean
    for generation in ('7', -1, True):
        bad_generation = dict(document, generation=generation)
        _rejection(json.dumps(bad_generation).encode('utf-8'))
    # invalid identity key
    bad_key = dict(document, execution_identity_key='ZZ' * 32)
    _rejection(json.dumps(bad_key).encode('utf-8'))
    # invalid fact shape
    bad_fact = dict(document, normalized_facts=[{
        'category': 'c', 'source': 'pkg/b.py', 'target': 'pkg/a.py',
        'status': 'observed', 'attributes': [1, 2]}])
    _rejection(json.dumps(bad_fact).encode('utf-8'))
    # invalid scope type
    bad_scope = dict(document, observed_scope=dict(
        document['observed_scope'], reads='pkg/a.py'))
    _rejection(json.dumps(bad_scope).encode('utf-8'))

    # deterministic messages: identical input -> identical error text
    first = _rejection(b'{not json at all')
    second = _rejection(b'{not json at all')
    assert first == second


# ---------------------------------- known fixtures stay verifiable
def test_contract_fixtures_replay() -> None:
    """Records built exactly like the Phase 3.0 contract fixtures must
    verify (their arbitrary reason string is contract-valid)."""
    for reason in ('dependency_verified', 'دلیل_تأیید'):
        record = _record(
            facts=(_fact(),),
            verdict=GovernanceVerdict(status=VerdictStatus.PASS,
                                      reason_code=reason))
        result = verify_bytes(canonicalize_evidence(record))
        assert result.verified, reason
        assert result.execution_identity_key == EXPECTED_KEY
        assert result.reconstructed_verdict == GovernanceVerdict(
            status=VerdictStatus.PASS, reason_code=reason)


def test_public_surface() -> None:
    """Two-level pipeline + cohort + fail-closed error type."""
    assert callable(replay.verify_bytes)
    assert callable(replay.verify_record)
    assert callable(replay.verify_cohort)
    assert callable(replay.parse_evidence)
    assert issubclass(replay.ReplayParseError, ValueError)
    assert dataclasses.fields(replay.ReplayResult)
    assert dataclasses.fields(replay.ReplayDivergence)
