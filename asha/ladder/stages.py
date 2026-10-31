"""Stage execution: build the prompt, call the provider once, validate output.

Every stage gets its own execution id, request and context hash. The two
review passes are separate calls built from the same immutable target and
never from each other's output, so neither can be anchored by the other.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asha.governance import semantic_review
from asha.ladder import prompts
from asha.ladder.models import (
    Confidence,
    Finding,
    FindingError,
    ReviewTarget,
    Severity,
    StageResult,
    StageStatus,
    StageType,
    Verdict,
)
from asha.ladder.providers import (
    OPERATION_TIMEOUT_S,
    Completion,
    Provider,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from asha.ladder.target import file_line_count
from asha.timeouts import Deadline

#: Reviewer role identities -- role labels, never provider claims.
REVIEWER_ROLES: dict[StageType, str] = {
    StageType.CODE_REVIEW: 'ladder/code-reviewer',
    StageType.SENIOR_REVIEW: 'ladder/senior-reviewer',
    StageType.SEMANTIC_REVIEW: 'asha/governance/semantic_review',
}

SEVERITY_BY_VALUE = {severity.value: severity for severity in Severity}
CONFIDENCE_BY_VALUE = {confidence.value: confidence for confidence in Confidence}
VERDICT_BY_VALUE = {verdict.value: verdict for verdict in Verdict}


class StageFailure(Exception):
    """A stage produced output that cannot be trusted as review evidence."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def parse_document(text: str) -> dict[str, Any]:
    """Strict JSON object; one markdown fence is tolerated, nothing else."""
    body = text.strip()
    if body.startswith('```'):
        first_newline = body.find('\n')
        last_fence = body.rfind('```')
        if first_newline == -1 or last_fence <= first_newline:
            raise StageFailure('unbalanced markdown fence in reviewer output')
        body = body[first_newline + 1:last_fence].strip()
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise StageFailure(f'reviewer output is not JSON: {exc}') from exc
    if not isinstance(document, dict):
        raise StageFailure(
            f'reviewer output must be a JSON object, got '
            f'{type(document).__name__}')
    return document


def parse_verdict(document: dict[str, Any]) -> Verdict:
    raw = document.get('verdict')
    if raw is None:
        raise StageFailure('reviewer output omitted the verdict')
    if not isinstance(raw, str) or raw not in VERDICT_BY_VALUE:
        raise StageFailure(
            f'unknown verdict {raw!r}; expected one of '
            f'{sorted(VERDICT_BY_VALUE)}')
    return VERDICT_BY_VALUE[raw]


def build_findings(document: dict[str, Any], *, stage: StageType,
                   target: ReviewTarget,
                   root: Path) -> tuple[Finding, ...]:
    """Validate reviewer findings; any deviation fails the stage."""
    entries = document.get('findings')
    if not isinstance(entries, list):
        raise StageFailure(
            f'findings must be a list, got {type(entries).__name__}')
    changed = set(target.files)
    findings: list[Finding] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise StageFailure(f'finding #{index} is not an object')
        path = entry.get('file_path')
        if not isinstance(path, str) or path not in changed:
            raise StageFailure(
                f'finding #{index} names {path!r}, which is not part of the '
                'reviewed change set')
        try:
            severity = SEVERITY_BY_VALUE[str(entry.get('severity', ''))]
        except KeyError as exc:
            raise StageFailure(
                f'finding #{index} has unknown severity '
                f'{entry.get("severity")!r}') from exc
        try:
            confidence = CONFIDENCE_BY_VALUE[str(entry.get('confidence', ''))]
        except KeyError as exc:
            raise StageFailure(
                f'finding #{index} has unknown confidence '
                f'{entry.get("confidence")!r}') from exc
        line_start = entry.get('line_start')
        line_end = entry.get('line_end')
        limit = file_line_count(root, target.head_sha, path)
        if limit is not None:
            for label, value in (('line_start', line_start),
                                 ('line_end', line_end)):
                if value is None:
                    continue
                if not isinstance(value, int) or isinstance(value, bool):
                    raise StageFailure(
                        f'finding #{index} {label} must be an int')
                if value > limit:
                    raise StageFailure(
                        f'finding #{index} {label}={value} is past the end '
                        f'of {path} ({limit} lines)')
        try:
            findings.append(Finding(
                finding_id=f'{stage.value.split("_")[0].lower()}-{index:03d}',
                stage=stage,
                category=str(entry.get('category', 'other')),
                severity=severity,
                confidence=confidence,
                title=str(entry.get('title', '')),
                description=str(entry.get('description', '')),
                file_path=path,
                line_start=line_start if isinstance(line_start, int) else None,
                line_end=line_end if isinstance(line_end, int) else None,
                evidence=str(entry.get('evidence', '')),
                impact=str(entry.get('impact', '')),
                recommended_action=str(entry.get('recommended_action', '')),
                blocking=bool(entry.get('blocking', False)),
            ))
        except FindingError as exc:
            raise StageFailure(f'finding #{index} is invalid: {exc}') from exc
    return tuple(findings)


def _budget(deadline: Deadline | None, operation_timeout_s: int
            ) -> tuple[int, str]:
    """Effective per-call budget plus the refusal reason when there is none."""
    if deadline is None:
        return max(1, int(operation_timeout_s)), ''
    # cap() is min(operation budget, what is left of the task); 0 means the
    # task budget is spent and the stage must not launch at all.
    capped = deadline.cap(float(max(1, int(operation_timeout_s))))
    if capped < 1:
        return 0, 'deadline_exceeded'
    return int(capped), ''


def run_review_stage(*, stage: StageType, provider: Provider,
                     target: ReviewTarget, diff_text: str, snippets: str,
                     deadline: Deadline | None,
                     operation_timeout_s: int = OPERATION_TIMEOUT_S,
                     root: Path | None = None) -> StageResult:
    """Run one review pass; never raises for expected failures."""
    started = now_iso()
    execution_id = secrets.token_hex(16)
    role = REVIEWER_ROLES[stage]
    prompt_hash = prompts.prompt_sha256(stage.value)
    user_content = prompts.render_user_content(
        stage=stage.value, target=target.as_dict(), diff_text=diff_text,
        snippets=snippets)
    context_hash = hashlib.sha256(user_content.encode('utf-8')).hexdigest()
    budget, refusal = _budget(deadline, operation_timeout_s)
    base: dict[str, Any] = {
        'stage': stage,
        'reviewer': role,
        'execution_id': execution_id,
        'provider': provider.name,
        'model': provider.model,
        'prompt_sha256': prompt_hash,
        'context_sha256': context_hash,
        'started_at': started,
    }
    if refusal:
        return StageResult(**base, status=StageStatus.NOT_EXECUTED,
                           ended_at=now_iso(), error=refusal)
    try:
        completion: Completion = provider.complete(
            prompts.system_prompt(stage.value), user_content,
            timeout_s=budget)
    except ProviderUnavailable as exc:
        return StageResult(**base, status=StageStatus.BLOCKED,
                           ended_at=now_iso(), error=f'unavailable: {exc}')
    except ProviderTimeout as exc:
        return StageResult(**base, status=StageStatus.FAILED,
                           ended_at=now_iso(), error=f'timeout: {exc}')
    except ProviderError as exc:
        return StageResult(**base, status=StageStatus.FAILED,
                           ended_at=now_iso(), error=f'provider_error: {exc}')
    base.update({
        'provider': completion.provider or provider.name,
        'model': completion.model or provider.model,
        'session_id': completion.session_id,
    })
    try:
        document = parse_document(completion.text)
        verdict = parse_verdict(document)
        if root is None:
            raise StageFailure('internal: review stage needs the repo root')
        findings = build_findings(document, stage=stage, target=target,
                                  root=root)
    except StageFailure as exc:
        return StageResult(**base, status=StageStatus.FAILED,
                           ended_at=now_iso(),
                           error=f'invalid_response: {exc}')
    return StageResult(**base, status=StageStatus.COMPLETED, verdict=verdict,
                       findings=findings, ended_at=now_iso())


def semantic_validate(*, target: ReviewTarget,
                      stages: tuple[StageResult, ...]) -> StageResult:
    """Deterministic semantic validation of the completed review passes.

    Uses the canonical ``semantic-review/v1`` contract from
    ``asha.governance.semantic_review``: every finding must render into that
    schema, ids must be unique and stage-attributed, and no review pass may
    be missing from the evidence it claims to provide.
    """
    started = now_iso()
    execution_id = secrets.token_hex(16)
    findings = [finding for stage in stages for finding in stage.findings]
    base: dict[str, Any] = {
        'stage': StageType.SEMANTIC_REVIEW,
        'reviewer': REVIEWER_ROLES[StageType.SEMANTIC_REVIEW],
        'execution_id': execution_id,
        'provider': 'local',
        'model': 'semantic-review/v1',
        'prompt_sha256': hashlib.sha256(
            prompts.PROMPT_VERSION.encode('utf-8')).hexdigest(),
        'context_sha256': hashlib.sha256(
            json.dumps([stage.as_dict() for stage in stages],
                       sort_keys=True).encode('utf-8')).hexdigest(),
        'started_at': started,
    }
    reasons: list[str] = []
    ids = [finding.finding_id for finding in findings]
    if len(ids) != len(set(ids)):
        reasons.append('duplicate finding ids across reviewers')
    missing = [stage.stage.value for stage in stages
               if stage.status is not StageStatus.COMPLETED]
    if missing:
        reasons.append('review passes not completed: ' + ', '.join(missing))
    try:
        semantic_review.validate_findings(
            [finding.gate_payload() for finding in findings])
    except Exception as exc:  # canonical contract violation -> failed stage
        reasons.append(f'finding contract violation: {exc}')
    if reasons:
        return StageResult(**base, status=StageStatus.FAILED,
                           ended_at=now_iso(), error='; '.join(reasons))
    # The validator's own "verdict" is not a code approval: the stage
    # reports NOT_APPLICABLE and the decision reads its status only.
    # The validator carries no findings of its own: it validates the other
    # passes' findings, and duplicating them here would double-count every
    # one of them in the run's finding set.
    return StageResult(**base, status=StageStatus.COMPLETED,
                       verdict=Verdict.NOT_APPLICABLE, ended_at=now_iso())
