"""Phase 3.2 -- deterministic authoritative replay engine.

Hermetic verification boundary: given identical authoritative evidence,
reconstruct the same governance-relevant facts and verdict IN MEMORY --
no Git, subprocess, filesystem, network, clock, randomness, environment,
or AuditMetadata. Canonicalization is reused from ``asha.evidence`` and
never duplicated.

What replay verifies (derived from the existing runtime semantics):

1. Identity      SHA-256(f"{base_tree_sha}:{worker_id}:{generation}")
                 re-derived and compared with ``execution_identity_key``.
2. Scope/facts   every fact endpoint must be covered by the observed
                 scope (reads | writes) using the conflict module's
                 structural ``covered()`` (directory/glob aware -- the
                 same primitive dispatch safety is built on), and fact
                 status must belong to the vocabulary the repository
                 actually produces (``observed`` from the evidence
                 contract, ``declared``/``derived`` from dep_index).
3. Verdict       ``reason_code`` -> expected state, using ONLY reason
                 classes that exist in the runtime today (greped from
                 scheduler state transitions and the Phase 3.0 contract
                 fixtures): FAIL  <- timeout_exceeded, worker_exit_*,
                 verification_failed:*, execution_error:*, scope_*,
                 no_verification_ran;
                 BLOCKED <- upstream_failure, not_dispatchable,
                 dependency_not_finished:*, cycle_detected,
                 owner_ambiguous;
                 PASS    <- evidence_sealed, dependency_verified.
                 A reason outside these classes yields NO verdict claim
                 (the contract allows arbitrary reason strings -- see
                 the Persian fixture in test_evidence_contract.py), so
                 exact re-derivation of FAIL from check results is
                 impossible: the authoritative schema does not carry
                 check outputs. That limitation is reported here, not
                 papered over (Phase 3.2 section 8).
4. Wire          verify_bytes additionally requires the input bytes to
                 BE the canonical serialization of their parse result.

What replay does NOT reconstruct (schema insufficiency, section 10):
historical queue/READY/BLOCKED ordering, wave numbers, dependency edges,
concurrency timing -- records carry no timestamps, dependency lists, or
dispatch order. Cohort replay therefore reports only the strongest sound
invariant: pairwise read/write interference between records (which pairs
could NOT be safe simultaneous execution under the conflict semantics).

Phase 3.3 (hash chains / commitments / signatures) is intentionally
absent: a semantic mismatch here is a divergence, not a crypto failure.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from typing import Any

from .conflict import covered, overlap
from .evidence import (
    AuthoritativeEvidence,
    GovernanceVerdict,
    NormalizedFact,
    ObservedScope,
    ScopeCaptureMode,
    VerdictStatus,
    canonicalize_evidence,
)

_SCHEMA_VERSION = 1
_KEY_RE = re.compile(r'^[0-9a-f]{64}$')
_RECORD_KEYS = frozenset(
    field_.name for field_ in fields(AuthoritativeEvidence))
_SCOPE_KEYS = frozenset(field_.name for field_ in fields(ObservedScope))
_FACT_KEYS = frozenset(field_.name for field_ in fields(NormalizedFact))
_VERDICT_KEYS = frozenset(field_.name for field_ in fields(GovernanceVerdict))

# Fact status vocabulary proven to exist in this repository:
# 'observed' (evidence contract fixture), 'declared'/'derived' (dep_index
# enforces exactly these two on load).
_FACT_STATUSES = frozenset({'observed', 'declared', 'derived'})

# Reason classes actually produced by the runtime today (scheduler state
# transitions + Phase 3.0 contract fixtures). Prefix entries end with ':'.
_FAIL_REASONS = frozenset({'timeout_exceeded', 'no_verification_ran'})
_FAIL_PREFIXES = ('worker_exit_', 'verification_failed:',
                  'execution_error:', 'scope_violation:',
                  'scope_violations')
_BLOCKED_REASONS = frozenset({
    'upstream_failure', 'not_dispatchable', 'cycle_detected',
    'owner_ambiguous',
})
_BLOCKED_PREFIXES = ('dependency_not_finished:', 'worker_cycle:',
                     'virtual_ambiguity:')
_PASS_REASONS = frozenset({'evidence_sealed', 'dependency_verified'})


class ReplayParseError(ValueError):
    """Deterministic fail-closed rejection of malformed wire input.

    Schema/type problems raise this; semantic problems (identity, scope,
    fact, verdict) return a ReplayResult with divergences instead.
    """


@dataclass(frozen=True)
class ReplayDivergence:
    field: str
    expected: str
    reconstructed: str
    reason: str


@dataclass(frozen=True)
class ReplayResult:
    verified: bool
    execution_identity_key: str
    reconstructed_verdict: GovernanceVerdict
    divergences: tuple[ReplayDivergence, ...] = ()


@dataclass(frozen=True)
class CohortReplayResult:
    verified: bool
    results: tuple[ReplayResult, ...]
    conflicting_pairs: tuple[tuple[str, str], ...] = ()
    disjoint_pairs: tuple[tuple[str, str], ...] = ()


def _fail(message: str) -> ReplayParseError:
    return ReplayParseError('replay: ' + message)


def _expect_str(document: Any, key: str, where: str) -> str:
    if not isinstance(document, dict) or key not in document:
        raise _fail(f'missing field {where}.{key}')
    value = document[key]
    if not isinstance(value, str):
        raise _fail(f'invalid type {where}.{key}: expected str')
    return value


def _expect_int(value: Any, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f'invalid type {where}: expected int')
    if value < minimum:
        raise _fail(f'invalid value {where}: must be >= {minimum}')
    return value


def _expect_str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail(f'invalid type {where}: expected array')
    entries: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise _fail(f'invalid entry type {where}: expected str')
        entries.append(entry)
    return tuple(entries)


def _expect_exact_keys(document: Any, keys: frozenset[str],
                       where: str) -> None:
    if not isinstance(document, dict):
        raise _fail(f'invalid type {where}: expected object')
    actual = frozenset(document)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise _fail(f'field set mismatch at {where}: '
                    f'missing={missing} extra={extra}')


def _parse_scope(document: Any) -> ObservedScope:
    _expect_exact_keys(document, _SCOPE_KEYS, 'observed_scope')
    capture = document['capture_mode']
    if not isinstance(capture, str) or capture not in {
            mode.value for mode in ScopeCaptureMode}:
        raise _fail(f'invalid capture_mode: {capture!r}')
    return ObservedScope(
        reads=frozenset(_expect_str_list(document['reads'],
                                         'observed_scope.reads')),
        writes=frozenset(_expect_str_list(document['writes'],
                                          'observed_scope.writes')),
        capture_mode=ScopeCaptureMode(capture),
    )


def _parse_fact(document: Any, index: int) -> NormalizedFact:
    where = f'normalized_facts[{index}]'
    _expect_exact_keys(document, _FACT_KEYS, where)
    for key in ('category', 'source', 'target', 'status'):
        _expect_str(document, key, where)
    attributes: list[tuple[str, str]] = []
    raw = document['attributes']
    if not isinstance(raw, list):
        raise _fail(f'invalid type {where}.attributes: expected array')
    for pair in raw:
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(item, str) for item in pair)):
            raise _fail(f'invalid attribute pair at {where}')
        attributes.append((pair[0], pair[1]))
    return NormalizedFact(
        category=document['category'],
        source=document['source'],
        target=document['target'],
        status=document['status'],
        attributes=tuple(attributes),
    )


def _parse_verdict(document: Any) -> GovernanceVerdict:
    _expect_exact_keys(document, _VERDICT_KEYS, 'verdict')
    status = document['status']
    if not isinstance(status, str) or status not in {
            state.value for state in VerdictStatus}:
        raise _fail(f'invalid verdict status: {status!r}')
    reason = document['reason_code']
    if not isinstance(reason, str):
        raise _fail('invalid type verdict.reason_code: expected str')
    return GovernanceVerdict(status=VerdictStatus(status),
                             reason_code=reason)


def parse_evidence(data: bytes) -> AuthoritativeEvidence:
    """Strict wire -> record ingestion. Deterministic, fail-closed."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise _fail(f'invalid utf-8: {exc.reason}') from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(f'invalid json at line {exc.lineno} col {exc.colno}') \
            from None
    _expect_exact_keys(document, _RECORD_KEYS, 'record')
    schema = _expect_int(document['schema_version'], 'schema_version', 1)
    if schema != _SCHEMA_VERSION:
        raise _fail(f'unsupported schema_version: {schema}')
    worker_id = _expect_str(document, 'worker_id', 'record')
    base_tree_sha = _expect_str(document, 'base_tree_sha', 'record')
    target_tree_sha = _expect_str(document, 'target_tree_sha', 'record')
    key = _expect_str(document, 'execution_identity_key', 'record')
    if not _KEY_RE.match(key):
        raise _fail(f'invalid identity key format: {key!r}')
    generation = _expect_int(document['generation'], 'generation', 0)
    facts_raw = document['normalized_facts']
    if not isinstance(facts_raw, list):
        raise _fail('invalid type normalized_facts: expected array')
    facts = tuple(_parse_fact(entry, index)
                  for index, entry in enumerate(facts_raw))
    return AuthoritativeEvidence(
        schema_version=schema,
        execution_identity_key=key,
        worker_id=worker_id,
        generation=generation,
        base_tree_sha=base_tree_sha,
        target_tree_sha=target_tree_sha,
        observed_scope=_parse_scope(document['observed_scope']),
        normalized_facts=facts,
        verdict=_parse_verdict(document['verdict']),
    )


def _reconstruct_identity(evidence: AuthoritativeEvidence) -> str:
    """Phase 3.0 contract, verbatim: SHA-256(UTF-8(base:worker:generation))."""
    material = (f'{evidence.base_tree_sha}:{evidence.worker_id}:'
                f'{evidence.generation}')
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _expected_verdict_state(reason_code: str) -> VerdictStatus | None:
    """Reason class -> expected state. None = not derivable from the
    current schema (arbitrary reason strings are contract-valid)."""
    if reason_code in _FAIL_REASONS or reason_code.startswith(_FAIL_PREFIXES):
        return VerdictStatus.FAIL
    if (reason_code in _BLOCKED_REASONS
            or reason_code.startswith(_BLOCKED_PREFIXES)):
        return VerdictStatus.BLOCKED
    if reason_code in _PASS_REASONS:
        return VerdictStatus.PASS
    return None


def verify_record(evidence: AuthoritativeEvidence) -> ReplayResult:
    """Pure record-level verification: identity, scope/fact soundness,
    verdict coherence. No I/O, no clock, deterministic ordering."""
    divergences: list[ReplayDivergence] = []

    # 1. identity integrity (section 6)
    reconstructed_key = _reconstruct_identity(evidence)
    if reconstructed_key != evidence.execution_identity_key:
        divergences.append(ReplayDivergence(
            field='execution_identity_key',
            expected=evidence.execution_identity_key,
            reconstructed=reconstructed_key,
            reason='ERR_IDENTITY_MISMATCH',
        ))

    # 2. scope/fact soundness (section 7): both endpoints of every fact
    # must be covered by the observed scope -- an endpoint outside means
    # the fact could not have been derived from this execution's
    # authoritative scope (undeclared dependency). Coverage uses the
    # conflict module's structural primitive (directory/glob aware), the
    # same semantics dispatch safety relies on.
    scope_entries = tuple(sorted(evidence.observed_scope.reads
                                 | evidence.observed_scope.writes))
    facts = tuple(sorted(
        evidence.normalized_facts,
        key=lambda fact: (fact.category, fact.source, fact.target,
                          fact.status, fact.attributes),
    ))
    for fact in facts:
        if not any(covered(fact.source, entry)
                   for entry in scope_entries):
            divergences.append(ReplayDivergence(
                field='normalized_facts.source',
                expected=f'covered by scope {scope_entries}',
                reconstructed=fact.source,
                reason='ERR_FACT_SOURCE_OUT_OF_SCOPE',
            ))
        if not any(covered(fact.target, entry)
                   for entry in scope_entries):
            divergences.append(ReplayDivergence(
                field='normalized_facts.target',
                expected=f'covered by scope {scope_entries}',
                reconstructed=fact.target,
                reason='ERR_FACT_TARGET_OUT_OF_SCOPE',
            ))
        if fact.status not in _FACT_STATUSES:
            divergences.append(ReplayDivergence(
                field='normalized_facts.status',
                expected=f'one of {sorted(_FACT_STATUSES)}',
                reconstructed=fact.status,
                reason='ERR_FACT_STATUS_INVALID',
            ))

    # 3. independent verdict re-derivation (section 8): only reason
    # classes the runtime actually produces are claimed; anything else
    # is an explicit non-derivable (documented limitation), never a guess.
    expected_state = _expected_verdict_state(evidence.verdict.reason_code)
    if expected_state is not None and expected_state is not \
            evidence.verdict.status:
        # orientation matches identity: expected = the recorded claim,
        # reconstructed = the value independently re-derived here.
        divergences.append(ReplayDivergence(
            field='verdict.status',
            expected=evidence.verdict.status.value,
            reconstructed=expected_state.value,
            reason='ERR_VERDICT_DIVERGENCE',
        ))

    ordered = tuple(sorted(divergences,
                           key=lambda item: (item.field, item.reason,
                                             item.expected,
                                             item.reconstructed)))
    reconstructed = GovernanceVerdict(
        status=expected_state if expected_state is not None
        else evidence.verdict.status,
        reason_code=evidence.verdict.reason_code,
    )
    return ReplayResult(
        verified=not ordered,
        execution_identity_key=reconstructed_key,
        reconstructed_verdict=reconstructed,
        divergences=ordered,
    )


def verify_bytes(data: bytes) -> ReplayResult:
    """Wire ingestion pipeline: canonical bytes -> parse -> verify_record,
    plus byte-for-byte canonical round-trip (section 9)."""
    evidence = parse_evidence(data)
    result = verify_record(evidence)
    canonical = canonicalize_evidence(evidence)
    if canonical != data:
        divergence = ReplayDivergence(
            field='canonical_wire',
            expected=repr(canonical[:120]),
            reconstructed=repr(data[:120]),
            reason='ERR_WIRE_ROUNDTRIP',
        )
        divergences = tuple(sorted(
            result.divergences + (divergence,),
            key=lambda item: (item.field, item.reason, item.expected,
                              item.reconstructed),
        ))
        return ReplayResult(
            verified=False,
            execution_identity_key=result.execution_identity_key,
            reconstructed_verdict=result.reconstructed_verdict,
            divergences=divergences,
        )
    return result


def _pair_interference(left: AuthoritativeEvidence,
                       right: AuthoritativeEvidence) -> bool:
    """Conflict module semantics verbatim: WW/WR/RW structural overlap;
    read/read never conflicts; unprovable coverage counts as overlap."""
    left_scope = {'reads': tuple(sorted(left.observed_scope.reads)),
                  'writes': tuple(sorted(left.observed_scope.writes))}
    right_scope = {'reads': tuple(sorted(right.observed_scope.reads)),
                   'writes': tuple(sorted(right.observed_scope.writes))}
    ww = _intersect(left_scope['writes'], right_scope['writes'])
    wr = _intersect(left_scope['writes'], right_scope['reads'])
    rw = _intersect(right_scope['writes'], left_scope['reads'])
    return ww or wr or rw


def _intersect(left: tuple[str, ...],
               right: tuple[str, ...]) -> bool:
    for litem in left:
        for ritem in right:
            if overlap(litem, ritem):
                return True
    return False


def verify_cohort(
        records: tuple[AuthoritativeEvidence, ...],
) -> CohortReplayResult:
    """Cohort replay to the sound extent the schema supports (section 10).

    Re-derives, for every record pair, whether their scopes interfere
    under the conflict semantics -- pairs that interfere are NOT valid
    simultaneous execution. Historical queue order, wave numbers,
    dependency edges, and actual concurrency timing are NOT present in
    authoritative records and are deliberately NOT reconstructed.
    """
    results: list[ReplayResult] = []
    ordered = tuple(sorted(records, key=lambda record: record.worker_id))
    # results follow the SAME deterministic order as the pair scan so
    # input ordering can never leak into the reconstruction (INV-R7)
    for record in ordered:
        results.append(verify_record(record))
    conflicting: list[tuple[str, str]] = []
    disjoint: list[tuple[str, str]] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            pair = (left.worker_id, right.worker_id)
            if _pair_interference(left, right):
                conflicting.append(pair)
            else:
                disjoint.append(pair)
    return CohortReplayResult(
        verified=all(result.verified for result in results),
        results=tuple(results),
        conflicting_pairs=tuple(conflicting),
        disjoint_pairs=tuple(disjoint),
    )
