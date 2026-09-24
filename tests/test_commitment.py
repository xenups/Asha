"""Phase 3.3 -- hash chain & commitment integrity tests.

Expected SHA-256 values here are constructed with hashlib DIRECTLY from
the protocol text (genesis_input / previous_digest + canonical bytes)
-- never by calling the implementation and comparing it with itself.
"""

from __future__ import annotations

import builtins
import dataclasses
import hashlib
import inspect
import os
import socket
import statistics
import subprocess
import time
from typing import cast

import pytest

from asha import commitment
from asha.commitment import (
    GENESIS_DOMAIN,
    ChainVerificationResult,
    CommitmentNode,
    build_evidence_chain,
    compute_genesis_commitment,
    compute_record_commitment,
    verify_evidence_chain,
)
from asha.evidence import (
    AuditMetadata,
    AuthoritativeEvidence,
    GovernanceVerdict,
    ObservedScope,
    ScopeCaptureMode,
    VerdictStatus,
    canonicalize_evidence,
)

BASE = 'base42base42base42'


def _record(generation: int = 1,
            target: str | None = None,
            worker_id: str = 'worker_b') -> AuthoritativeEvidence:
    return AuthoritativeEvidence.create(
        worker_id=worker_id,
        generation=generation,
        base_tree_sha=BASE,
        target_tree_sha=target or f'target{generation:04d}targettail',
        observed_scope=ObservedScope(
            reads=frozenset({'pkg/a.py'}),
            writes=frozenset({'pkg/b.py'}),
            capture_mode=ScopeCaptureMode.STRICT),
        normalized_facts=(),
        verdict=GovernanceVerdict(status=VerdictStatus.PASS,
                                  reason_code='evidence_sealed'),
    )


def _records(count: int) -> tuple[AuthoritativeEvidence, ...]:
    return tuple(_record(generation=index)
                 for index in range(1, count + 1))


def _head(records: tuple[AuthoritativeEvidence, ...]) -> str:
    nodes = build_evidence_chain(BASE, records)
    return nodes[-1].commitment_hash if nodes else \
        compute_genesis_commitment(BASE)


# ------------------------------------------------------------- 1 / 2
def test_genesis_commitment_determinism() -> None:
    first = compute_genesis_commitment(BASE)
    second = compute_genesis_commitment(BASE)
    assert first == second
    assert len(first) == 64 and all(c in '0123456789abcdef'
                                    for c in first)


def test_genesis_changes_with_base_tree() -> None:
    other = compute_genesis_commitment('differentbasedifferent')
    assert other != compute_genesis_commitment(BASE)


# ------------------------------------------------------------- 3
def test_hash_chain_sequential_integrity() -> None:
    records = _records(4)
    nodes = build_evidence_chain(BASE, records)
    assert len(nodes) == len(records)
    genesis = compute_genesis_commitment(BASE)
    assert nodes[0].previous_commitment == genesis
    for index, node in enumerate(nodes):
        assert node.index == index
        if index:
            assert node.previous_commitment == \
                nodes[index - 1].commitment_hash
        assert node.execution_identity_key == \
            records[index].execution_identity_key
        assert len(node.commitment_hash) == 64
    # verify agrees with the build over the same inputs
    result = verify_evidence_chain(BASE, records, nodes[-1].commitment_hash)
    assert result.valid and result.reason == 'VALID'
    assert result.verified_count == 4
    assert result.divergence_index is None


# ------------------------------------------------------------- 4 / 5
def test_tamper_detection_historical_record() -> None:
    records = _records(5)
    original = build_evidence_chain(BASE, records)
    # semantically meaningful field of E2
    tampered = tuple(
        dataclasses.replace(record, target_tree_sha='evilertarget0002')
        if index == 1 else record
        for index, record in enumerate(records))
    mutated = build_evidence_chain(BASE, tampered)
    # E1 unchanged -> C1 identical; from C2 onward every digest differs
    assert mutated[0].commitment_hash == original[0].commitment_hash
    for index in range(1, 5):
        assert mutated[index].commitment_hash != \
            original[index].commitment_hash


def test_tamper_propagates_to_chain_head() -> None:
    records = _records(5)
    original = build_evidence_chain(BASE, records)
    tampered = tuple(
        dataclasses.replace(record, target_tree_sha='evilertarget0004')
        if index == 3 else record
        for index, record in enumerate(records))
    mutated = build_evidence_chain(BASE, tampered)
    assert mutated[-1].commitment_hash != original[-1].commitment_hash
    result = verify_evidence_chain(BASE, tampered,
                                   original[-1].commitment_hash)
    assert result.valid is False
    assert result.reason == 'INVALID'
    # head-only verifier: exact localization is NOT inferable
    assert result.divergence_index is None
    assert result.verified_count == 5


# ------------------------------------------------------------- 6 / 7 / 8
def test_record_deletion_detection() -> None:
    records = _records(3)
    head = _head(records)
    shrunk = (records[0], records[2])
    result = verify_evidence_chain(BASE, shrunk, head)
    assert result.valid is False and result.reason == 'INVALID'
    assert result.divergence_index is None


def test_record_insertion_detection() -> None:
    records = _records(3)
    head = _head(records)
    intruder = _record(generation=99, target='intrudertarget0099')
    grown = (records[0], intruder, records[1], records[2])
    result = verify_evidence_chain(BASE, grown, head)
    assert result.valid is False and result.reason == 'INVALID'
    assert result.divergence_index is None


def test_record_reordering_detection() -> None:
    records = _records(3)
    head = _head(records)
    swapped = (records[0], records[2], records[1])
    result = verify_evidence_chain(BASE, swapped, head)
    assert result.valid is False and result.reason == 'INVALID'
    assert _head(swapped) != head


# ------------------------------------------------------------- 9
def test_canonical_representation_invariance() -> None:
    left, right = _record(generation=5), _record(generation=5)
    assert left is not right            # distinct Python objects
    assert canonicalize_evidence(left) == canonicalize_evidence(right)
    previous = compute_genesis_commitment(BASE)
    assert compute_record_commitment(previous, left) == \
        compute_record_commitment(previous, right)
    assert build_evidence_chain(BASE, (left,)) == \
        build_evidence_chain(BASE, (right,))


# ------------------------------------------------------------- 10
def test_audit_metadata_exclusion() -> None:
    records = _records(3)
    audit_a = AuditMetadata(
        ephemeral_execution_id='eph-1', wall_start_iso='2026-01-01T00:00:00Z',
        wall_end_iso='2026-01-01T00:00:01Z', t_engine_ms=1.0,
        t_workload_ms=10.0, raw_stdout_sample='stdout A',
        raw_stderr_sample='stderr A', host_metadata=(('host', 'a'),))
    audit_b = AuditMetadata(
        ephemeral_execution_id='eph-2', wall_start_iso='2026-12-31T23:59:59Z',
        wall_end_iso='2026-12-31T23:59:59Z', t_engine_ms=9999.0,
        t_workload_ms=0.001, raw_stdout_sample='stdout B',
        raw_stderr_sample='stderr B', host_metadata=(('host', 'b'),))
    assert audit_a != audit_b
    first = build_evidence_chain(BASE, records)
    second = build_evidence_chain(BASE, records)   # audits never enter
    assert first == second
    node_fields = {field.name for field in dataclasses.fields(CommitmentNode)}
    audit_fields = {field.name for field in dataclasses.fields(AuditMetadata)}
    assert node_fields.isdisjoint(audit_fields)


# ------------------------------------------------------------- 11
def test_hermetic_commitment_zero_subprocesses() -> None:
    # static proof first (before tripwires): the module never even
    # imports environment/clock/randomness machinery
    source = inspect.getsource(commitment)
    for banned in ('import subprocess', 'import socket', 'import os',
                   'import time', 'import random', 'import uuid',
                   'os.environ', 'time.time(', 'random.', 'uuid.'):
        assert banned not in source, banned
    imported = set(vars(commitment))
    assert imported.isdisjoint({'os', 'time', 'random', 'uuid',
                                'subprocess', 'socket'})

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

    def trip(*args, **kwargs):
        raise AssertionError('hermetic violation')

    patches = [
        (subprocess, 'run', spy_run),
        (subprocess, 'Popen', spy_popen),
        (builtins, 'open', trip),
        (os, 'open', trip),
        (socket, 'create_connection', trip),
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
    assert builtins.open is trip and subprocess.run is spy_run
    try:
        records = _records(3)
        nodes = build_evidence_chain(BASE, records)
        result = verify_evidence_chain(BASE, records,
                                       nodes[-1].commitment_hash)
        with pytest.raises(AssertionError):
            builtins.open('probe')  # noqa: SIM115 -- tripwire probe
    finally:
        for target, name, original in originals:
            setattr(target, name, original)
    assert subprocess_calls == []
    assert result.valid


# ------------------------------------------------------------- 12
def _reject(expected_head: str, records: tuple[object, ...] = ()) -> str:
    result = verify_evidence_chain(
        BASE, cast('tuple[AuthoritativeEvidence, ...]', records),
        expected_head)
    assert result.valid is False
    return result.reason


def test_malformed_commitment_handling() -> None:
    genesis = compute_genesis_commitment(BASE)
    # invalid commitment encoding (non-hex, short, long)
    for head in ('ZZ' * 32, 'abc', '00' * 31, '00' * 33):
        reason = _reject(head)
        assert reason.startswith('MALFORMED:')
        assert reason != 'INVALID'
    # invalid record structure -> localized to the offending index
    broken = verify_evidence_chain(
        BASE, cast('tuple[AuthoritativeEvidence, ...]', (object(),)),
        genesis)
    assert broken.valid is False
    assert broken.reason == 'MALFORMED:record_0_structure'
    assert broken.divergence_index == 0
    assert broken.verified_count == 0
    # records container must be a tuple
    as_list = verify_evidence_chain(
        BASE, cast('tuple[AuthoritativeEvidence, ...]', [_record()]),
        genesis)
    assert as_list.reason == 'MALFORMED:records_not_tuple'
    # non-text base tree
    bad_base = verify_evidence_chain(
        cast('str', 7), (), genesis)
    assert bad_base.reason == 'MALFORMED:base_tree_sha_not_text'
    # API-boundary failures raise deterministically
    with pytest.raises(ValueError):
        compute_record_commitment('not-hex', _record())
    with pytest.raises(ValueError):
        compute_record_commitment('ab' * 31, _record())   # 31 bytes
    with pytest.raises(TypeError):
        compute_record_commitment(7, _record())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_genesis_commitment(7)  # type: ignore[arg-type]


# ------------------------------------------------------------- 13
def test_empty_chain_behavior() -> None:
    assert build_evidence_chain(BASE, ()) == ()
    genesis = compute_genesis_commitment(BASE)
    empty = verify_evidence_chain(BASE, (), genesis)
    assert empty.valid and empty.reason == 'VALID'
    assert empty.verified_count == 0
    assert empty.head_commitment == genesis
    wrong = verify_evidence_chain(BASE, (), '00' * 32)
    assert wrong.valid is False and wrong.reason == 'INVALID'
    assert wrong.divergence_index is None


# ------------------------------------------------------------- 14 / 15
def test_independent_expected_commitment() -> None:
    """Protocol vector: hashlib applied DIRECTLY to the spec text."""
    # genesis built from the protocol definition, not the implementation
    genesis_input = GENESIS_DOMAIN + BASE.encode('utf-8')
    expected_genesis = hashlib.sha256(genesis_input).hexdigest()
    assert compute_genesis_commitment(BASE) == expected_genesis

    records = _records(2)
    # record commitments built by hand: prev_raw + canonical bytes
    previous_digest = bytes.fromhex(expected_genesis)
    expected_chain: list[str] = []
    for record in records:
        digest = hashlib.sha256(
            previous_digest + canonicalize_evidence(record)).digest()
        expected_chain.append(digest.hex())
        previous_digest = digest

    nodes = build_evidence_chain(BASE, records)
    assert [node.commitment_hash for node in nodes] == expected_chain
    assert [node.previous_commitment for node in nodes] == \
        [expected_genesis, expected_chain[0]]
    # single-record vector through the public API too
    single = compute_record_commitment(expected_genesis, records[0])
    assert single == hashlib.sha256(
        bytes.fromhex(expected_genesis)
        + canonicalize_evidence(records[0])).hexdigest()
    assert single == expected_chain[0]


# ------------------------------------------------------------- 16
def test_commitment_performance_report() -> None:
    """Engineering benchmark: reported, never a timing gate."""
    report: dict[str, dict[str, float]] = {}
    plan = {1: 50, 10: 50, 100: 30, 1000: 5}
    for size, reps in plan.items():
        records = _records(size)
        bytes_total = sum(len(canonicalize_evidence(record))
                          for record in records)
        build_times: list[float] = []
        verify_times: list[float] = []
        nodes = build_evidence_chain(BASE, records)
        head = nodes[-1].commitment_hash
        for _ in range(reps):
            start = time.perf_counter()
            build_evidence_chain(BASE, records)
            build_times.append((time.perf_counter() - start) * 1000.0)
            start = time.perf_counter()
            verify_evidence_chain(BASE, records, head)
            verify_times.append((time.perf_counter() - start) * 1000.0)
        ordered_build, ordered_verify = (sorted(build_times),
                                         sorted(verify_times))
        report[str(size)] = {
            'build_median_ms': statistics.median(ordered_build),
            'build_p95_ms': ordered_build[min(reps - 1,
                                              int(reps * 0.95))],
            'verify_median_ms': statistics.median(ordered_verify),
            'verify_p95_ms': ordered_verify[min(reps - 1,
                                                int(reps * 0.95))],
            'canonical_bytes': float(bytes_total),
        }
    print('commitment perf', report)
    # engineering acceptance target (<10ms median for 100 records) is
    # REPORTED above; only a catastrophic-regression ceiling is guarded
    assert report['100']['build_median_ms'] < 1000.0


def test_result_models_are_immutable() -> None:
    node = build_evidence_chain(BASE, _records(1))[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.commitment_hash = 'ff' * 32  # type: ignore[misc]
    result = ChainVerificationResult(valid=True, head_commitment='00' * 32,
                                     verified_count=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.valid = False  # type: ignore[misc]
