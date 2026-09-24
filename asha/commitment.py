"""Phase 3.3 -- cryptographic hash chain over canonical evidence bytes.

Minimal, deterministic commitment layer: a linear SHA-256 chain detects
cryptographic inconsistency when authoritative records are modified,
removed, inserted, or reordered AFTER commitment.

Protocol (exactly as specified):

    GENESIS_DOMAIN = b"ASHA-COMMITMENT-V1\\x00"
    C0             = SHA256(GENESIS_DOMAIN || base_tree_sha.utf8)
    Cn             = SHA256(C(n-1) || canonicalize_evidence(En))

Internal chaining uses RAW 32-BYTE digests; hexadecimal exists only at
the API boundary (CommitmentNode / function returns). The commitment
input is exactly ``previous_digest || canonical_evidence_bytes`` --
``execution_identity_key`` is carried as node metadata and is never
concatenated into the hash input (it already lives inside the canonical
bytes). Canonicalization is consumed from ``asha.evidence`` and never
duplicated.

Hermetic by construction: stdlib ``hashlib`` + ``dataclasses`` only; no
Git, subprocess, filesystem, network, clock, randomness, environment.

Security boundary -- what this layer does NOT provide:
    commitment integrity != semantic validity   (replay owns semantics)
    commitment integrity != authenticity        (no signatures here)
    commitment integrity != non-repudiation     (no keys here)
A hash chain detects inconsistency relative to a TRUSTED expected head;
an attacker who changes evidence AND the trusted head value is not
caught by this layer alone. Merkle trees, signatures, and key
management are deliberately out of scope (Phase 3.3 boundary).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .evidence import AuthoritativeEvidence, canonicalize_evidence

GENESIS_DOMAIN = b'ASHA-COMMITMENT-V1\x00'


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'commitment: {name} must be str')
    return value


def _require_hex32(name: str, value: str) -> bytes:
    _require_text(name, value)
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise ValueError(
            f'commitment: {name} must be hex') from None
    if len(raw) != 32:
        raise ValueError(
            f'commitment: {name} must be a 32-byte digest')
    return raw


def compute_genesis_commitment(base_tree_sha: str) -> str:
    """C0 = SHA256(GENESIS_DOMAIN || base_tree_sha) as hex.

    Deterministic for (protocol version, base_tree_sha); differs when
    the base tree identity differs. No time/randomness/environment.
    """
    _require_text('base_tree_sha', base_tree_sha)
    digest = hashlib.sha256(
        GENESIS_DOMAIN + base_tree_sha.encode('utf-8')).digest()
    return digest.hex()


def compute_record_commitment(
        previous_commitment: str,
        evidence: AuthoritativeEvidence,
) -> str:
    """Cn = SHA256(C(n-1) || canonicalize_evidence(En)) as hex.

    The previous commitment enters as its RAW digest bytes (hex is the
    display boundary only); the evidence enters as the exact Phase 3.0
    canonical wire bytes -- nothing else is hashed.
    """
    previous = _require_hex32('previous_commitment', previous_commitment)
    payload = canonicalize_evidence(evidence)
    return hashlib.sha256(previous + payload).hexdigest()


@dataclass(frozen=True)
class CommitmentNode:
    index: int
    execution_identity_key: str
    previous_commitment: str
    commitment_hash: str


@dataclass(frozen=True)
class ChainVerificationResult:
    valid: bool
    head_commitment: str
    verified_count: int
    divergence_index: int | None = None
    reason: str = ''


def build_evidence_chain(
        base_tree_sha: str,
        records: tuple[AuthoritativeEvidence, ...],
) -> tuple[CommitmentNode, ...]:
    """Chronological chain: genesis + records -> nodes.

    ``verified_count``/node order follow the input order exactly --
    chronological order IS part of the commitment.
    """
    genesis = compute_genesis_commitment(base_tree_sha)
    previous_digest = bytes.fromhex(genesis)
    nodes: list[CommitmentNode] = []
    for index, record in enumerate(records):
        if not isinstance(record, AuthoritativeEvidence):
            raise TypeError(
                f'commitment: record {index} is not AuthoritativeEvidence')
        payload = canonicalize_evidence(record)
        digest = hashlib.sha256(previous_digest + payload).digest()
        nodes.append(CommitmentNode(
            index=index,
            execution_identity_key=record.execution_identity_key,
            previous_commitment=previous_digest.hex(),
            commitment_hash=digest.hex(),
        ))
        previous_digest = digest
    return tuple(nodes)


def verify_evidence_chain(
        base_tree_sha: str,
        records: tuple[AuthoritativeEvidence, ...],
        expected_head: str,
) -> ChainVerificationResult:
    """Recompute the ENTIRE chain from (base_tree_sha, records) and
    compare the head with ``expected_head`` (exact equality).

    Divergence semantics (fail-closed, no overclaim): the verifier holds
    only a single trusted head, NOT per-record expected digests, so a
    head mismatch CANNOT localize which historical record changed --
    ``divergence_index`` stays None for head mismatches. It is populated
    only where position IS determinable from available information: the
    index of a record that made recomputation impossible (MALFORMED).

    reason: 'VALID' | 'INVALID' | 'MALFORMED:<detail>'.
    """
    if not isinstance(base_tree_sha, str):
        return ChainVerificationResult(
            valid=False, head_commitment='', verified_count=0,
            divergence_index=None,
            reason='MALFORMED:base_tree_sha_not_text')
    if not isinstance(expected_head, str):
        return ChainVerificationResult(
            valid=False, head_commitment='', verified_count=0,
            divergence_index=None,
            reason='MALFORMED:expected_head_not_text')
    try:
        bytes.fromhex(expected_head)
        if len(expected_head) != 64:
            raise ValueError('length')
    except ValueError:
        return ChainVerificationResult(
            valid=False, head_commitment=expected_head,
            verified_count=0, divergence_index=None,
            reason='MALFORMED:expected_head_encoding')
    if not isinstance(records, tuple):
        return ChainVerificationResult(
            valid=False, head_commitment='', verified_count=0,
            divergence_index=None,
            reason='MALFORMED:records_not_tuple')
    for index, record in enumerate(records):
        if not isinstance(record, AuthoritativeEvidence):
            return ChainVerificationResult(
                valid=False, head_commitment='', verified_count=index,
                divergence_index=index,
                reason=f'MALFORMED:record_{index}_structure')
    try:
        nodes = build_evidence_chain(base_tree_sha, records)
    except (ValueError, TypeError, AttributeError) as exc:
        return ChainVerificationResult(
            valid=False, head_commitment='', verified_count=0,
            divergence_index=None,
            reason=f'MALFORMED:{type(exc).__name__}')
    head = nodes[-1].commitment_hash if nodes else \
        compute_genesis_commitment(base_tree_sha)
    if head == expected_head:
        return ChainVerificationResult(
            valid=True, head_commitment=head,
            verified_count=len(records), divergence_index=None,
            reason='VALID')
    return ChainVerificationResult(
        valid=False, head_commitment=head,
        verified_count=len(records), divergence_index=None,
        reason='INVALID')
