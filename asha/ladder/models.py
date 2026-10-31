"""Typed vocabulary for one host-driven Ladder run.

Asha is an MCP server: the host LLM performs every review inference and
submits the result as a tool call. Asha owns the state machine, the
evidence and the delivery decision.

Three axes stay apart, because each can move independently:

* :class:`StageStatus` -- did Asha record a validated submission;
* :class:`Verdict` -- what the host reviewer concluded;
* :class:`Decision` -- what Asha is allowed to do with the delivery.

``REQUEST_CHANGES`` submitted by the host is a validated stage; a missing
stage is ``PENDING``/``AWAITING_HOST``; a rejected submission is
``FAILED``. None of those is approval, and approval is never inferred from
the absence of a finding.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA = 'asha-ladder/v1'

#: Bumped when the shape of a persisted artifact changes.
ARTIFACT_SCHEMA = 2

#: The only execution mode this package implements: the host LLM reviews.
EXECUTION_MODE = 'HOST_DRIVEN_MULTIPASS'


class LadderError(Exception):
    """Base class for Ladder refusals (fail closed)."""


class TargetError(LadderError):
    """The review target could not be established as an immutable source."""


class FindingError(LadderError):
    """A submitted finding does not satisfy the finding contract."""


class SubmissionError(LadderError):
    """A stage submission is out of order, malformed or conflicting."""


class RunNotFound(LadderError):
    """No review run exists under that id."""


class RunExpired(LadderError):
    """The persisted review deadline expired; no further stage is accepted."""


class StageType(str, Enum):
    CODE_REVIEW = 'CODE_REVIEW'
    SENIOR_REVIEW = 'SENIOR_REVIEW'
    SEMANTIC_REVIEW = 'SEMANTIC_REVIEW'
    SHIP_GATE = 'SHIP_GATE'


class StageStatus(str, Enum):
    PENDING = 'PENDING'
    AWAITING_HOST = 'AWAITING_HOST'
    SUBMITTED = 'SUBMITTED'
    VALIDATED = 'VALIDATED'
    FAILED = 'FAILED'
    BLOCKED = 'BLOCKED'
    STALE = 'STALE'
    NOT_EXECUTED = 'NOT_EXECUTED'


class RunState(str, Enum):
    AWAITING_HOST_REVIEW = 'AWAITING_HOST_REVIEW'
    AWAITING_FINALIZE = 'AWAITING_FINALIZE'
    FINALIZED = 'FINALIZED'
    BLOCKED = 'BLOCKED'
    STALE = 'STALE'
    FAILED = 'FAILED'


class Verdict(str, Enum):
    APPROVE = 'APPROVE'
    REQUEST_CHANGES = 'REQUEST_CHANGES'
    INCONCLUSIVE = 'INCONCLUSIVE'
    NOT_APPLICABLE = 'NOT_APPLICABLE'


class Decision(str, Enum):
    APPROVED = 'APPROVED'
    CHANGES_REQUESTED = 'CHANGES_REQUESTED'
    BLOCKED = 'BLOCKED'
    STALE = 'STALE'
    FAILED = 'FAILED'


class Independence(str, Enum):
    """How much independence the recorded review passes actually have.

    Ordered strongest first. The host's statement is an attestation, not
    evidence Asha can verify: a single MCP client name never proves which
    model reasoned, so two role labels on one host stay
    ``same_host_session``.
    """

    DISTINCT_HOST_CLIENT = 'DISTINCT_HOST_CLIENT'
    HOST_ATTESTED_ISOLATED_CONTEXT = 'HOST_ATTESTED_ISOLATED_CONTEXT'
    SAME_HOST_SESSION = 'SAME_HOST_SESSION'
    UNKNOWN = 'UNKNOWN'


INDEPENDENCE_RANK: dict[Independence, int] = {
    Independence.UNKNOWN: 0,
    Independence.SAME_HOST_SESSION: 1,
    Independence.HOST_ATTESTED_ISOLATED_CONTEXT: 2,
    Independence.DISTINCT_HOST_CLIENT: 3,
}


class Severity(str, Enum):
    CRITICAL = 'CRITICAL'
    HIGH = 'HIGH'
    MEDIUM = 'MEDIUM'
    LOW = 'LOW'
    INFO = 'INFO'


class Confidence(str, Enum):
    CONFIRMED = 'CONFIRMED'
    LIKELY = 'LIKELY'
    UNVERIFIED = 'UNVERIFIED'


#: Severities that deny delivery regardless of who approved what.
BLOCKING_SEVERITIES: frozenset[Severity] = frozenset(
    {Severity.CRITICAL, Severity.HIGH})

#: Ladder severity -> the canonical semantic-review/v1 ladder. CRITICAL maps
#: to BLOCKER so the existing Ship Gate partition still blocks it.
GATE_SEVERITY: dict[Severity, str] = {
    Severity.CRITICAL: 'BLOCKER',
    Severity.HIGH: 'HIGH',
    Severity.MEDIUM: 'MEDIUM',
    Severity.LOW: 'LOW',
    Severity.INFO: 'LOW',
}

FINDING_TEXT_FIELDS: tuple[str, ...] = (
    'title', 'description', 'evidence', 'impact', 'recommended_action')


@dataclass(frozen=True)
class Finding:
    """One validated reviewer finding.

    Construction is the validation boundary: unknown severity/confidence,
    empty rationale, an absolute or escaping path, or a nonsense line
    range raises :class:`FindingError` instead of being normalized.
    ``blocking`` is the reviewer's own claim and is recorded as a claim --
    the policy decides with :data:`BLOCKING_SEVERITIES`, so a reviewer
    cannot talk its own CRITICAL finding out of blocking.
    """

    finding_id: str
    stage: StageType
    category: str
    severity: Severity
    confidence: Confidence
    title: str
    description: str
    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    evidence: str = ''
    impact: str = ''
    recommended_action: str = ''
    blocking: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.finding_id, str) or not self.finding_id.strip():
            raise FindingError('finding_id must be a non-empty string')
        if not isinstance(self.stage, StageType):
            raise FindingError(
                f'stage must be a StageType, got {type(self.stage).__name__}')
        if not isinstance(self.severity, Severity):
            raise FindingError(
                f'severity must be a Severity, got {type(self.severity).__name__}')
        if not isinstance(self.confidence, Confidence):
            raise FindingError(
                'confidence must be a Confidence, got '
                f'{type(self.confidence).__name__}')
        for name in ('category', 'file_path') + FINDING_TEXT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise FindingError(
                    f'finding field {name!r} must be a non-empty string, '
                    f'got {value!r}')
        _validate_repo_path(self.file_path)
        if self.line_start is not None and (
                not isinstance(self.line_start, int) or self.line_start < 1):
            raise FindingError(
                f'line_start must be a positive int, got {self.line_start!r}')
        if self.line_end is not None and (
                not isinstance(self.line_end, int) or self.line_end < 1):
            raise FindingError(
                f'line_end must be a positive int, got {self.line_end!r}')
        if self.line_end is not None:
            if self.line_start is None:
                raise FindingError('line_end requires line_start')
            if self.line_end < self.line_start:
                raise FindingError(
                    f'line_end {self.line_end} precedes line_start '
                    f'{self.line_start}')
        if not isinstance(self.blocking, bool):
            raise FindingError('blocking must be a bool')

    @property
    def blocks_delivery(self) -> bool:
        """Policy view: severity decides, not the reviewer's ``blocking``."""
        return self.severity in BLOCKING_SEVERITIES

    @property
    def line_range(self) -> str:
        if self.line_start is None:
            return ''
        if self.line_end is None or self.line_end == self.line_start:
            return str(self.line_start)
        return f'{self.line_start}-{self.line_end}'

    def gate_payload(self) -> dict[str, str]:
        """Render as the canonical ``semantic-review/v1`` finding shape."""
        return {
            'severity': GATE_SEVERITY[self.severity],
            'file_path': self.file_path,
            'line_range': self.line_range or '1',
            'problem': f'{self.title}: {self.description}',
            'why_it_matters': self.impact,
            'failure_scenario': self.evidence,
            'recommended_fix': self.recommended_action,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            'finding_id': self.finding_id,
            'stage': self.stage.value,
            'category': self.category,
            'severity': self.severity.value,
            'confidence': self.confidence.value,
            'title': self.title,
            'description': self.description,
            'file_path': self.file_path,
            'line_start': self.line_start,
            'line_end': self.line_end,
            'evidence': self.evidence,
            'impact': self.impact,
            'recommended_action': self.recommended_action,
            'blocking': self.blocking,
        }


def _validate_repo_path(path: str) -> None:
    """Reject traversal, absolute paths and Windows separators."""
    if path.startswith(('/', '\\')) or '\\' in path:
        raise FindingError(
            f'file_path must be repository-relative POSIX, got {path!r}')
    normalized = posixpath.normpath(path)
    if normalized.startswith('../') or normalized == '..':
        raise FindingError(f'file_path escapes the repository: {path!r}')
    if normalized != path:
        raise FindingError(
            f'file_path is not normalized ({path!r} -> {normalized!r})')


@dataclass(frozen=True)
class ReviewTarget:
    """The immutable source identity one run is bound to."""

    repo_root: str
    branch: str
    base_sha: str
    head_sha: str
    diff_sha256: str
    files: tuple[str, ...] = ()
    base_ref: str = ''
    head_ref: str = ''
    policy_id: str = ''
    policy_hash: str = ''
    run_id: str = ''
    started_at: str = ''

    def __post_init__(self) -> None:
        for name in ('repo_root', 'branch', 'base_sha', 'head_sha',
                     'diff_sha256', 'policy_id', 'run_id'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise TargetError(f'target field {name!r} must be non-empty')

    def identity(self) -> dict[str, Any]:
        """The fields a later stage must still agree with (no timestamps)."""
        return {
            'base_sha': self.base_sha,
            'head_sha': self.head_sha,
            'diff_sha256': self.diff_sha256,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.identity()
        payload.update({
            'schema': SCHEMA,
            'artifact_schema': ARTIFACT_SCHEMA,
            'repo_root': self.repo_root,
            'branch': self.branch,
            'base_ref': self.base_ref,
            'head_ref': self.head_ref,
            'files': list(self.files),
            'policy_id': self.policy_id,
            'policy_hash': self.policy_hash,
            'run_id': self.run_id,
            'started_at': self.started_at,
        })
        return payload


@dataclass(frozen=True)
class ReviewerInfo:
    """Who submitted a stage, as far as Asha can actually know.

    ``host_client`` is whatever the MCP client reported (often nothing);
    ``isolation`` is the host's own attestation and is recorded as a
    claim, with its source, never as verified independence.
    """

    host_client: str = 'unknown'
    isolation: Independence = Independence.UNKNOWN
    attestation_source: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {
            'host_client': self.host_client,
            'isolation_claim': self.isolation.value,
            'attestation_source': self.attestation_source,
        }


@dataclass(frozen=True)
class StageResult:
    """One stage's recorded submission plus what it concluded."""

    stage: StageType
    status: StageStatus
    stage_id: str = ''
    verdict: Verdict = Verdict.NOT_APPLICABLE
    summary: str = ''
    reviewer: ReviewerInfo = field(default_factory=ReviewerInfo)
    execution_id: str = ''
    submission_sha256: str = ''
    submitted_at: str = ''
    validated_at: str = ''
    findings: tuple[Finding, ...] = ()
    error: str = ''
    artifact: str = ''

    @property
    def validated(self) -> bool:
        return self.status is StageStatus.VALIDATED

    def as_dict(self) -> dict[str, Any]:
        return {
            'stage': self.stage.value,
            'stage_id': self.stage_id,
            'status': self.status.value,
            'verdict': self.verdict.value,
            'summary': self.summary,
            'reviewer': self.reviewer.as_dict(),
            'execution_id': self.execution_id,
            'submission_sha256': self.submission_sha256,
            'submitted_at': self.submitted_at,
            'validated_at': self.validated_at,
            'findings': [finding.as_dict() for finding in self.findings],
            'error': self.error,
            'artifact': self.artifact,
        }


@dataclass(frozen=True)
class ReviewRun:
    """The persisted state of one host-driven review."""

    run_id: str
    target: ReviewTarget
    state: RunState
    stages: tuple[StageResult, ...] = ()
    decision: Decision | None = None
    reasons: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    relations: tuple[dict[str, Any], ...] = ()
    independence: Independence = Independence.UNKNOWN
    semantic_validation: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)
    artifacts: str = ''
    execution_mode: str = EXECUTION_MODE
    created_at: str = ''
    expires_at: str = ''
    finalized_at: str = ''

    def stage(self, stage: StageType) -> StageResult | None:
        for record in self.stages:
            if record.stage is stage:
                return record
        return None

    @property
    def approved(self) -> bool:
        return self.decision is Decision.APPROVED

    def as_dict(self) -> dict[str, Any]:
        return {
            'schema': SCHEMA,
            'artifact_schema': ARTIFACT_SCHEMA,
            'run_id': self.run_id,
            'state': self.state.value,
            'decision': self.decision.value if self.decision else None,
            'approved': self.approved,
            'execution_mode': self.execution_mode,
            'reasons': list(self.reasons),
            'independence': self.independence.value,
            'target': self.target.as_dict(),
            'stages': [stage.as_dict() for stage in self.stages],
            'findings': [finding.as_dict() for finding in self.findings],
            'relations': list(self.relations),
            'semantic_validation': self.semantic_validation,
            'gate': self.gate,
            'artifacts': self.artifacts,
            'created_at': self.created_at,
            'expires_at': self.expires_at,
            'finalized_at': self.finalized_at,
        }
