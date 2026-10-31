"""Typed vocabulary for one Ladder run.

Three axes are kept apart on purpose, because each can move independently:

* :class:`StageStatus` -- did the stage actually execute and finish;
* :class:`Verdict` -- what the reviewer concluded;
* :class:`Decision` -- what Asha is allowed to do with the delivery.

A provider call that returns ``REQUEST_CHANGES`` is ``COMPLETED``; a
missing provider is ``BLOCKED``; malformed output is ``FAILED``. None of
those is approval, and approval is never inferred from the absence of a
finding.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA = 'asha-ladder/v1'

#: Bumped when the shape of a persisted artifact changes.
ARTIFACT_SCHEMA = 1


class LadderError(Exception):
    """Base class for Ladder refusals (fail closed)."""


class TargetError(LadderError):
    """The review target could not be established as an immutable source."""


class FindingError(LadderError):
    """A reviewer finding does not satisfy the finding contract."""


class StageType(str, Enum):
    CODE_REVIEW = 'CODE_REVIEW'
    SENIOR_REVIEW = 'SENIOR_REVIEW'
    SEMANTIC_REVIEW = 'SEMANTIC_REVIEW'
    SHIP_GATE = 'SHIP_GATE'


class StageStatus(str, Enum):
    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    BLOCKED = 'BLOCKED'
    NOT_EXECUTED = 'NOT_EXECUTED'
    STALE = 'STALE'


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


class Independence(str, Enum):
    """How much independence two review passes actually have.

    Ordered strongest first. ``same_provider_isolated_context`` is a real
    separate pass, not an independent reviewer: different model names on
    one account do not create independence.
    """

    DISTINCT_PROVIDER = 'distinct_provider'
    SAME_PROVIDER_ISOLATED_CONTEXT = 'same_provider_isolated_context'
    SAME_SESSION = 'same_session'
    UNKNOWN = 'unknown'


INDEPENDENCE_RANK: dict[Independence, int] = {
    Independence.UNKNOWN: 0,
    Independence.SAME_SESSION: 1,
    Independence.SAME_PROVIDER_ISOLATED_CONTEXT: 2,
    Independence.DISTINCT_PROVIDER: 3,
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
class StageResult:
    """One stage's execution record plus what it concluded."""

    stage: StageType
    status: StageStatus
    verdict: Verdict = Verdict.NOT_APPLICABLE
    reviewer: str = ''
    execution_id: str = ''
    provider: str = ''
    model: str = ''
    session_id: str = ''
    prompt_sha256: str = ''
    context_sha256: str = ''
    started_at: str = ''
    ended_at: str = ''
    findings: tuple[Finding, ...] = ()
    error: str = ''
    artifact: str = ''

    @property
    def completed(self) -> bool:
        return self.status is StageStatus.COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return {
            'stage': self.stage.value,
            'status': self.status.value,
            'verdict': self.verdict.value,
            'reviewer': self.reviewer,
            'execution_id': self.execution_id,
            'provider': self.provider,
            'model': self.model,
            'session_id': self.session_id,
            'prompt_sha256': self.prompt_sha256,
            'context_sha256': self.context_sha256,
            'started_at': self.started_at,
            'ended_at': self.ended_at,
            'findings': [finding.as_dict() for finding in self.findings],
            'error': self.error,
            'artifact': self.artifact,
        }


@dataclass(frozen=True)
class ReviewRunResult:
    """What ``run_review`` returns and what ``load_run`` re-reads."""

    run_id: str
    target: ReviewTarget
    status: StageStatus
    decision: Decision
    reasons: tuple[str, ...] = ()
    stages: tuple[StageResult, ...] = ()
    findings: tuple[Finding, ...] = ()
    relations: tuple[dict[str, Any], ...] = ()
    independence: Independence = Independence.UNKNOWN
    gate: dict[str, Any] = field(default_factory=dict)
    artifacts: str = ''
    started_at: str = ''
    ended_at: str = ''

    @property
    def approved(self) -> bool:
        return self.decision is Decision.APPROVED

    def as_dict(self) -> dict[str, Any]:
        return {
            'schema': SCHEMA,
            'artifact_schema': ARTIFACT_SCHEMA,
            'run_id': self.run_id,
            'status': self.status.value,
            'decision': self.decision.value,
            'approved': self.approved,
            'reasons': list(self.reasons),
            'independence': self.independence.value,
            'target': self.target.as_dict(),
            'stages': [stage.as_dict() for stage in self.stages],
            'findings': [finding.as_dict() for finding in self.findings],
            'relations': list(self.relations),
            'gate': self.gate,
            'artifacts': self.artifacts,
            'started_at': self.started_at,
            'ended_at': self.ended_at,
        }
