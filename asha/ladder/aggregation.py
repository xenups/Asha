"""Aggregation: independence classification, finding relations, decision.

The decision is derived from recorded facts only -- validated stage
submissions, severities, the independence the host actually attested and
the canonical Ship Gate result. Nothing here can turn a missing stage into
a pass, and a reviewer's own ``blocking`` claim never overrides severity.
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
    Verdict,
)
from asha.ladder.policy import ReviewPolicy

#: Stage types whose verdicts are about the code (semantic review is not).
CODE_STAGES = (StageType.CODE_REVIEW, StageType.SENIOR_REVIEW)

_ISOLATED_CLAIMS = (Independence.DISTINCT_HOST_CLIENT,
                    Independence.HOST_ATTESTED_ISOLATED_CONTEXT)


def classify_independence(first: StageResult,
                          second: StageResult) -> Independence:
    """Classify two host passes from what the host reported.

    ``DISTINCT_HOST_CLIENT`` needs two different client names that both
    claim they are distinct. Two role labels on one client are a real
    separate pass but not an independent reviewer; missing metadata is
    ``UNKNOWN`` and is never upgraded into a claim.
    """
    left, right = first.reviewer, second.reviewer
    if left.isolation is Independence.UNKNOWN or (
            right.isolation is Independence.UNKNOWN):
        return Independence.UNKNOWN
    if (left.isolation is Independence.DISTINCT_HOST_CLIENT
            and right.isolation is Independence.DISTINCT_HOST_CLIENT):
        if (left.host_client and right.host_client
                and left.host_client != right.host_client):
            return Independence.DISTINCT_HOST_CLIENT
        # Two claims on one client are one client: do not upgrade it.
        return Independence.SAME_HOST_SESSION
    if left.isolation in _ISOLATED_CLAIMS and right.isolation in _ISOLATED_CLAIMS:
        return Independence.HOST_ATTESTED_ISOLATED_CONTEXT
    return Independence.SAME_HOST_SESSION


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
                and stage.status is StageStatus.VALIDATED}
    if len(verdicts) < 2:
        return ()
    if len(set(verdicts.values())) == 1:
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
            # of it; the service records its own gate result.
            continue
        stage = present.get(required)
        if stage is None:
            reasons.append(f'{required.value}: NOT_SUBMITTED')
            continue
        if stage.status is not StageStatus.VALIDATED:
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
                    and stage.verdict is Verdict.INCONCLUSIVE]
    if inconclusive:
        return Decision.BLOCKED, tuple(
            f'{stage.stage.value}: reviewer was INCONCLUSIVE'
            for stage in inconclusive)

    conflicted = disagreements(stages)
    if conflicted:
        return Decision.CHANGES_REQUESTED, conflicted

    blocking_names = '/'.join(sorted(severity.value
                                     for severity in policy.blocking_severities))
    clean = (f'all {len(policy.required_stages)} mandatory stages validated; '
             f'no {blocking_names} finding; independence '
             f'{independence.value} meets '
             f'{policy.required_independence.value}')
    return Decision.APPROVED, (clean,)
