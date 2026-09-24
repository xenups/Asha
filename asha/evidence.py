"""Tamper-resistant ship evidence engine.

Clean-tree invariant: ship evidence may only be produced (or consumed) when
`git status --porcelain` is strictly empty.

Tree binding: every artifact records commit (HEAD) and tree_hash
(HEAD^{tree}), so evidence is cryptographically tied to the exact git tree
it describes -- not to mutable file contents.

Canonical digest: evidence_sha256 = sha256(canonical JSON of the payload
with sort_keys, compact separators, excluding the digest field itself).
Any manual edit of a field (e.g. authorized_to_ship false -> true) breaks
the digest and the gate rejects the artifact.

Writes are atomic (temp sibling + os.replace) into .jspace/evidence.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

SCHEMA = 1
EVIDENCE_NAME = 'evidence.json'


class EvidenceError(Exception):
    """Clean-tree / digest / structure violation (fail-closed)."""


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=root, capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        raise EvidenceError('git ' + ' '.join(args) + ': ' + proc.stderr.strip())
    return proc.stdout.strip()


def head_hash(root: Path) -> str:
    return _git(root, 'rev-parse', 'HEAD')


def tree_hash(root: Path) -> str:
    return _git(root, 'rev-parse', 'HEAD^{tree}')


def require_clean_tree(root: Path) -> None:
    proc = subprocess.run(['git', 'status', '--porcelain'], cwd=root,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise EvidenceError('git status failed: ' + proc.stderr.strip())
    dirty = [line for line in proc.stdout.splitlines() if line.strip()]
    if dirty:
        raise EvidenceError(
            'working tree is dirty; ship evidence requires a clean tree: '
            + ', '.join(dirty[:10]))


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)


def compute_digest(payload: dict) -> str:
    body = {key: value for key, value in payload.items()
            if key != 'evidence_sha256'}
    return hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()


def seal(payload: dict) -> dict:
    """Validate required fields, attach the canonical digest."""
    required = ('schema', 'stage', 'scope', 'commit', 'tree_hash',
                'observed_at', 'checks', 'authorized_to_ship')
    missing = [key for key in required if key not in payload]
    if missing:
        raise EvidenceError('evidence payload missing fields: ' + ', '.join(missing))
    sealed = dict(payload)
    sealed['evidence_sha256'] = compute_digest(sealed)
    return sealed


def evidence_path(root: Path) -> Path:
    return Path(root) / '.jspace' / EVIDENCE_NAME


def write(root: Path, sealed: dict) -> Path:
    path = evidence_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(sealed, indent=2, sort_keys=True,
                       ensure_ascii=False) + '\n').encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix='.evidence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def read(root: Path) -> dict | None:
    path = evidence_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise EvidenceError('evidence unreadable: ' + str(exc)) from exc
    if not isinstance(payload, dict):
        raise EvidenceError('evidence must be a JSON object')
    return payload


def verify(root: Path) -> dict | None:
    """Return the artifact when its digest is intact; None when absent.

    Any digest mismatch, missing required field, or boolean drift raises
    EvidenceError -- tampering can never authorize a ship.
    """
    payload = read(root)
    if payload is None:
        return None
    recorded = payload.get('evidence_sha256')
    if not isinstance(recorded, str):
        raise EvidenceError('evidence_sha256 missing')
    actual = compute_digest(payload)
    if actual != recorded:
        raise EvidenceError(
            'evidence_sha256 mismatch: artifact was modified after sealing '
            f'(recorded {recorded[:12]}..., actual {actual[:12]}...)')
    if not isinstance(payload.get('authorized_to_ship'), bool):
        raise EvidenceError('authorized_to_ship must be a boolean')
    return payload


# ------------------------------------------------ Phase 3.0: authoritative
# Contract-only section. Data models + canonicalization rules; no scheduler,
# worktree, or execution-routing code depends on anything below.


class IndependenceClassification(Enum):
    """Total fail-closed classification of cross-worker overlap."""
    PROVEN_DISJOINT = 'PROVEN_DISJOINT'
    PROVEN_SHARED = 'PROVEN_SHARED'
    UNKNOWN = 'UNKNOWN'


class EvidencePolicy(Enum):
    MINIMAL = 'MINIMAL'
    COMPLETE = 'COMPLETE'


class ScopeCaptureMode(Enum):
    STRICT = 'STRICT'
    BEST_EFFORT = 'BEST_EFFORT'
    DECLARED = 'DECLARED'


class VerdictStatus(Enum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    BLOCKED = 'BLOCKED'


def resolve_evidence_policy(
        classification: IndependenceClassification) -> EvidencePolicy:
    """Disjoint proves minimal capture is sound; everything else must be
    COMPLETE (fail-closed default for PROVEN_SHARED and UNKNOWN)."""
    if classification == IndependenceClassification.PROVEN_DISJOINT:
        return EvidencePolicy.MINIMAL
    return EvidencePolicy.COMPLETE


@dataclass(frozen=True)
class ObservedScope:
    reads: frozenset[str]
    writes: frozenset[str]
    capture_mode: ScopeCaptureMode


@dataclass(frozen=True)
class NormalizedFact:
    category: str
    source: str
    target: str
    status: str
    attributes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class GovernanceVerdict:
    status: VerdictStatus
    reason_code: str


@dataclass(frozen=True)
class AuthoritativeEvidence:
    schema_version: int
    execution_identity_key: str
    worker_id: str
    generation: int
    base_tree_sha: str
    target_tree_sha: str
    observed_scope: ObservedScope
    normalized_facts: tuple[NormalizedFact, ...]
    verdict: GovernanceVerdict

    @classmethod
    def create(
            cls, *,
            worker_id: str,
            generation: int,
            base_tree_sha: str,
            target_tree_sha: str,
            observed_scope: ObservedScope,
            normalized_facts: tuple[NormalizedFact, ...] = (),
            verdict: GovernanceVerdict,
            schema_version: int = 1,
    ) -> AuthoritativeEvidence:
        """Derive the identity key internally; callers cannot supply one."""
        material = f'{base_tree_sha}:{worker_id}:{generation}'
        key = hashlib.sha256(material.encode('utf-8')).hexdigest()
        return cls(
            schema_version=schema_version,
            execution_identity_key=key,
            worker_id=worker_id,
            generation=generation,
            base_tree_sha=base_tree_sha,
            target_tree_sha=target_tree_sha,
            observed_scope=observed_scope,
            normalized_facts=normalized_facts,
            verdict=verdict,
        )


@dataclass(frozen=True)
class AuditMetadata:
    ephemeral_execution_id: str
    wall_start_iso: str
    wall_end_iso: str
    t_engine_ms: float
    t_workload_ms: float
    raw_stdout_sample: str = ''
    raw_stderr_sample: str = ''
    host_metadata: tuple[tuple[str, str], ...] = ()

    @property
    def gor_ratio(self) -> float:
        if self.t_workload_ms <= 0:
            raise ValueError('t_workload_ms must be positive to compute GOR')
        return self.t_engine_ms / self.t_workload_ms


def canonicalize_evidence(
        evidence: AuthoritativeEvidence) -> bytes:
    """Deterministic, bit-for-bit identical UTF-8 compact-JSON bytes.

    Rules: sort_keys + compact separators + ensure_ascii=False (via
    canonical()); frozensets -> lexicographically sorted arrays; facts
    sorted by (category, source, target, status, attributes) with each
    fact's attribute pairs sorted lexicographically. AuditMetadata is
    intentionally not part of this contract.
    """
    facts = [
        {
            'category': fact.category,
            'source': fact.source,
            'target': fact.target,
            'status': fact.status,
            'attributes': [list(pair)
                           for pair in sorted(fact.attributes)],
        }
        for fact in evidence.normalized_facts
    ]
    facts.sort(key=lambda fact: (
        fact['category'], fact['source'], fact['target'], fact['status'],
        tuple(tuple(pair) for pair in fact['attributes'])))
    payload = {
        'schema_version': evidence.schema_version,
        'execution_identity_key': evidence.execution_identity_key,
        'worker_id': evidence.worker_id,
        'generation': evidence.generation,
        'base_tree_sha': evidence.base_tree_sha,
        'target_tree_sha': evidence.target_tree_sha,
        'observed_scope': {
            'reads': sorted(evidence.observed_scope.reads),
            'writes': sorted(evidence.observed_scope.writes),
            'capture_mode': evidence.observed_scope.capture_mode.value,
        },
        'normalized_facts': facts,
        'verdict': {
            'status': evidence.verdict.status.value,
            'reason_code': evidence.verdict.reason_code,
        },
    }
    return canonical(payload).encode('utf-8')


# ------------------------------------------- Phase 3.0-b: in-memory ledger


@dataclass(frozen=True)
class LedgerEvent:
    """One discrete runtime milestone; payload pairs are strictly sorted."""
    event_type: str
    payload: tuple[tuple[str, str], ...]


class ExecutionLedger:
    """In-memory execution event ledger (no disk I/O).

    Bridge/derivation abstraction only: events accumulate here and are
    never injected into AuthoritativeEvidence or the canonical wire
    format -- derive_authoritative() delegates to
    AuthoritativeEvidence.create() with context + explicit arguments.
    """

    def __init__(self, *, worker_id: str, generation: int,
                 base_tree_sha: str) -> None:
        self._worker_id = worker_id
        self._generation = generation
        self._base_tree_sha = base_tree_sha
        self._events: list[LedgerEvent] = []

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def base_tree_sha(self) -> str:
        return self._base_tree_sha

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    def record(self, event_type: str, **attributes: str) -> None:
        """Defensive snapshot: kwargs copied into a sorted frozen payload."""
        self._events.append(LedgerEvent(
            event_type=event_type,
            payload=tuple(sorted(attributes.items()))))

    def derive_authoritative(
            self, *,
            target_tree_sha: str,
            observed_scope: ObservedScope,
            normalized_facts: tuple[NormalizedFact, ...] = (),
            verdict: GovernanceVerdict,
            schema_version: int = 1,
    ) -> AuthoritativeEvidence:
        """Bridge raw milestones to the authoritative contract.

        self._events is deliberately never passed: ledger events stay
        ledger-only by construction.
        """
        return AuthoritativeEvidence.create(
            worker_id=self._worker_id,
            generation=self._generation,
            base_tree_sha=self._base_tree_sha,
            target_tree_sha=target_tree_sha,
            observed_scope=observed_scope,
            normalized_facts=normalized_facts,
            verdict=verdict,
            schema_version=schema_version,
        )
