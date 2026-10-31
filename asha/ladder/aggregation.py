"""Aggregation: independence classification, finding relations, decision.

The decision is derived from recorded facts only -- stage statuses,
severities, the independence the providers actually reported, and the
canonical Ship Gate result. Nothing here can turn a missing stage into a
pass, and a reviewer's own ``blocking`` claim never overrides severity.
"""

from __future__ import annotations

import re
from typing import Any

from asha.ladder.models import (
    INDEPENDENCE_RANK,
    Decision,
    Finding,
    Independence,
    ReviewTarget,
    StageResult,
    StageStatus,
    StageType,
)
from asha.ladder.policy import ReviewPolicy

#: Stage types whose verdicts are about the code (semantic review is not).
CODE_STAGES = (StageType.CODE_REVIEW, StageType.SENIOR_REVIEW)


def classify_independence(first: StageResult, second: StageResult
                          ) -> Independence:
    """Classify two review passes from reported metadata only.

    Different providers -> distinct. Different model names on one provider
    is a separate pass, not an independent reviewer. Missing metadata is
    ``unknown`` -- never upgraded to a claim.
    """
    if not first.provider or not second.provider:
        return Independence.UNKNOWN
    if not first.execution_id or not second.execution_id:
        return Independence.UNKNOWN
    if first.execution_id == second.execution_id:
        return Independence.SAME_SESSION
    if first.session_id and first.session_id == second.session_id:
        return Independence.SAME_SESSION
    if first.provider != second.provider:
        return Independence.DISTINCT_PROVIDER
    return Independence.SAME_PROVIDER_ISOLATED_CONTEXT


def meets_independence(actual: Independence,
                       required: Independence) -> bool:
    return INDEPENDENCE_RANK[actual] >= INDEPENDENCE_RANK[required]


def _normalized(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def relate_findings(findings: tuple[Finding, ...]) -> tuple[dict[str, Any], ...]:
    """Record relations between findings; never merge or drop one."""
    relations: list[dict[str, Any]] = []
    by_location: dict[tuple[str, int], list[Finding]] = {}
    for finding in findings:
        key = (finding.file_path, (finding.line_start or 0) // 10)
        by_location.setdefault(key, []).append(finding)
    for (path, _bucket), group in sorted(by_location.items()):
        if len(group) < 2:
            continue
        ids = sorted(finding.finding_id for finding in group)
        relations.append({
            'kind': 'related',
            'finding_ids': ids,
            'file_path': path,
            'reason': 'findings overlap within ten lines of one file',
        })
    by_title: dict[tuple[str, str], list[Finding]] = {}
    for finding in findings:
        title_key = (finding.file_path, _normalized(finding.title))
        by_title.setdefault(title_key, []).append(finding)
    for (path, _title), group in sorted(by_title.items()):
        if len(group) < 2:
            continue
        relations.append({
            'kind': 'duplicate',
            'finding_ids': sorted(finding.finding_id for finding in group),
            'file_path': path,
            'reason': 'reviewers raised the same claim for one file',
        })
    return tuple(relations)


def disagreements(stages: tuple[StageResult, ...]) -> tuple[str, ...]:
    """Reviewer disagreements that the policy must not silently resolve."""
    verdicts = {stage.stage: stage.verdict for stage in stages
                if stage.stage in CODE_STAGES
                and stage.status is StageStatus.COMPLETED}
    if len(verdicts) < 2:
        return ()
    values = set(verdicts.values())
    if len(values) == 1:
        return ()
    return ('reviewers disagreed: '
            + ', '.join(f'{stage.value}={verdict.value}'
                        for stage, verdict in sorted(
                            verdicts.items(), key=lambda item: item[0].value)),)


def decide(*, policy: ReviewPolicy, stages: tuple[StageResult, ...],
           findings: tuple[Finding, ...], independence: Independence,
           target: ReviewTarget, stale_reason: str = ''
           ) -> tuple[Decision, tuple[str, ...]]:
    """Derive the delivery decision; the first unmet condition names the cause."""
    if stale_reason:
        return Decision.STALE, (stale_reason,)

    reasons: list[str] = []
    present = {stage.stage: stage for stage in stages}
    for required in policy.required_stages:
        if required is StageType.SHIP_GATE:
            # The gate is this decision's terminal step, not a precondition
            # of it; the orchestrator records its own stage result.
            continue
        stage = present.get(required)
        if stage is None:
            reasons.append(f'{required.value}: NOT_EXECUTED (missing)')
            continue
        if stage.status is not StageStatus.COMPLETED:
            detail = f': {stage.error}' if stage.error else ''
            reasons.append(f'{required.value}: {stage.status.value}{detail}')
    if reasons:
        return Decision.BLOCKED, tuple(reasons)

    if not meets_independence(independence, policy.required_independence):
        floor = (f'independence {independence.value} is below the policy '
                 f'floor {policy.required_independence.value} for '
                 f'{policy.policy_id}')
        return Decision.BLOCKED, (floor,)

    blocking = tuple(finding for finding in findings
                     if finding.severity in policy.blocking_severities)
    if blocking:
        return Decision.CHANGES_REQUESTED, tuple(
            f'{finding.finding_id} {finding.severity.value} '
            f'{finding.file_path}: {finding.title}' for finding in blocking)

    inconclusive = [stage for stage in stages
                    if stage.stage in CODE_STAGES
                    and stage.verdict is stage.verdict.INCONCLUSIVE]
    if inconclusive:
        return Decision.BLOCKED, tuple(
            f'{stage.stage.value}: reviewer was INCONCLUSIVE'
            for stage in inconclusive)

    conflicted = disagreements(stages)
    if conflicted:
        return Decision.CHANGES_REQUESTED, conflicted

    blocking_names = '/'.join(sorted(severity.value
                                     for severity in policy.blocking_severities))
    clean = (f'all {len(policy.required_stages)} mandatory stages completed; '
             f'no {blocking_names} finding; independence '
             f'{independence.value} meets '
             f'{policy.required_independence.value}')
    return Decision.APPROVED, (clean,)
