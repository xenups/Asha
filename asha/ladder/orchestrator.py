"""The shared Ladder service: one review pipeline for CLI, MCP and code.

Flow (each step records a fact, never an inference)::

    resolve target -> run Code Review -> run Senior Review (isolated)
    -> semantic validation -> aggregate -> confirm source unchanged
    -> canonical Ship Gate -> persist -> return

The Ship Gate is ``asha.governance.ship_gate.gate_ship``; Ladder has no
second authority. When a precondition fails, the gate is recorded as
``NOT_EXECUTED`` with the reason, and the run can only be BLOCKED or
CHANGES_REQUESTED -- never APPROVED.
"""

from __future__ import annotations

import datetime as dt
import secrets
from pathlib import Path
from typing import Any

from asha import evidence as evidence_mod
from asha import telemetry
from asha.governance import semantic_review, ship_gate
from asha.ladder import aggregation, artifacts
from asha.ladder import stages as stage_mod
from asha.ladder.models import (
    Decision,
    ReviewRunResult,
    StageResult,
    StageStatus,
    StageType,
    Verdict,
)
from asha.ladder.policy import ReviewPolicy, resolve_policy
from asha.ladder.providers import (
    Provider,
    ProviderUnavailable,
    resolve_provider,
)
from asha.ladder.target import (
    StaleTargetError,
    assert_target_unchanged,
    diff_identity,
    resolve_target,
    source_snippets,
)
from asha.timeouts import Deadline

JOURNAL_EVENTS = {
    'started': 'review_started',
    'stage': 'review_stage_completed',
    'blocked': 'review_stage_blocked',
    'stale': 'review_stale',
    'decided': 'review_decided',
}


def new_run_id() -> str:
    stamp = dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')
    return f'{artifacts.RUN_ID_PREFIX}{stamp}-{secrets.token_hex(4)}'


def _journal(journal: Any, event: str, payload: dict[str, Any],
             *, critical: bool = False) -> None:
    """Journal errors propagate: a broken journal is not a success."""
    if journal is not None:
        journal.append(JOURNAL_EVENTS[event], payload, critical=critical)


def _gate_reviewer(findings: tuple, policy_version: str) -> Any:
    """Adapter: hand Ladder findings to the canonical gate as its artifact."""
    payload = [finding.gate_payload() for finding in findings]

    def reviewer(_root: Path, snapshot_id: str, _timeout: int) -> dict:
        return {
            'schema': semantic_review.REVIEW_SCHEMA,
            'snapshot_id': snapshot_id,
            'policy_version': policy_version,
            'status': 'ladder',
            'model': 'ladder/aggregate',
            'findings': list(payload),
        }

    return reviewer


def _run_gate(root: Path, findings: tuple) -> dict[str, Any]:
    """Invoke the canonical Ship Gate; its refusal is data, not an exception."""
    try:
        sealed = ship_gate.gate_ship(
            root, reviewer=_gate_reviewer(findings, ship_gate.POLICY_VERSION))
    except evidence_mod.EvidenceError as exc:
        # gate_ship persists the sealed decision BEFORE refusing, so the
        # typed state and reasons survive the refusal; reading them back
        # keeps the recorded cause instead of the exception text alone.
        sealed = evidence_mod.read(root) or {}
        reasons = list(sealed.get('gate_reasons') or [])
        if not reasons:
            reasons = [str(exc)]
        return {
            'gate_state': str(sealed.get('gate_state', 'REFUSED')),
            'authorized_to_ship': False,
            'reasons': reasons,
            'blocking_findings': list(sealed.get('blocking_findings') or []),
            'policy_version': ship_gate.POLICY_VERSION,
            'snapshot_id': str(sealed.get('snapshot_id', '')),
        }
    except Exception as exc:  # a broken gate never authorizes
        return {
            'gate_state': 'ERROR',
            'authorized_to_ship': False,
            'reasons': [f'{type(exc).__name__}: {exc}'],
            'policy_version': ship_gate.POLICY_VERSION,
        }
    return {
        'gate_state': str(sealed.get('gate_state', 'UNKNOWN')),
        'authorized_to_ship': bool(sealed.get('authorized_to_ship')),
        'reasons': list(sealed.get('gate_reasons') or []),
        'blocking_findings': list(sealed.get('blocking_findings') or []),
        'policy_version': str(sealed.get('policy_version', '')),
        'snapshot_id': str(sealed.get('snapshot_id', '')),
    }


def _blocked_stage(stage: StageType, reason: str) -> StageResult:
    now = stage_mod.now_iso()
    return StageResult(stage=stage, status=StageStatus.BLOCKED,
                       reviewer=stage_mod.REVIEWER_ROLES[stage],
                       started_at=now, ended_at=now, error=reason)


def run_review(root: Path, *, base: str = 'origin/main', head: str = 'HEAD',
               policy: ReviewPolicy | str | None = None,
               provider: Provider | None = None,
               deadline_seconds: float | None = None,
               journal: Any = None) -> ReviewRunResult:
    """Run one governed review of ``base..head`` and return the decision."""
    root = Path(root).resolve()
    review_policy = resolve_policy(policy) if isinstance(policy, str) or (
        policy is None) else policy
    deadline = (Deadline(deadline_seconds)
                if deadline_seconds is not None else None)
    run_id = new_run_id()
    started_at = stage_mod.now_iso()
    owns_journal = journal is None
    writer = telemetry.EventJournalWriter(root, run_id) if owns_journal else journal
    try:
        _journal(writer, 'started', {
            'run_id': run_id, 'base': base, 'head': head,
            'policy_id': review_policy.policy_id,
            'policy_hash': review_policy.content_hash(),
            'deadline_s': deadline_seconds,
        }, critical=True)
        target = resolve_target(root, base=base, head=head,
                                policy=review_policy, run_id=run_id,
                                started_at=started_at)
        diff_text, _, _ = diff_identity(root, target.base_sha, target.head_sha)
        snippets = source_snippets(root, target.head_sha, target.files)

        provider_error = ''
        if provider is None:
            try:
                provider = resolve_provider()
            except ProviderUnavailable as exc:
                provider_error = str(exc)

        review_stages: list[StageResult] = []
        if provider is None:
            for stage_type in (StageType.CODE_REVIEW,
                               StageType.SENIOR_REVIEW):
                review_stages.append(_blocked_stage(stage_type, provider_error))
        else:
            for stage_type in (StageType.CODE_REVIEW,
                               StageType.SENIOR_REVIEW):
                pass_result = stage_mod.run_review_stage(
                    stage=stage_type, provider=provider, target=target,
                    diff_text=diff_text, snippets=snippets, deadline=deadline,
                    root=root)
                review_stages.append(pass_result)
                _journal(
                    writer,
                    'stage' if pass_result.status is StageStatus.COMPLETED
                    else 'blocked',
                    {'run_id': run_id, 'stage': stage_type.value,
                     'status': pass_result.status.value,
                     'execution_id': pass_result.execution_id,
                     'provider': pass_result.provider,
                     'model': pass_result.model,
                     'verdict': pass_result.verdict.value,
                     'findings': len(pass_result.findings),
                     'error': pass_result.error})
        semantic = stage_mod.semantic_validate(
            target=target, stages=tuple(review_stages))
        _journal(writer,
                 'stage' if semantic.status is StageStatus.COMPLETED
                 else 'blocked',
                 {'run_id': run_id, 'stage': StageType.SEMANTIC_REVIEW.value,
                  'status': semantic.status.value,
                  'execution_id': semantic.execution_id,
                  'error': semantic.error})
        executed = tuple(review_stages) + (semantic,)
        # Only the two review passes contribute findings; the semantic
        # stage validates the same findings rather than re-emitting them.
        findings = tuple(finding for stage in review_stages
                         for finding in stage.findings)
        independence = aggregation.classify_independence(
            review_stages[0], review_stages[1])
        relations = aggregation.relate_findings(findings)

        decision, reasons = aggregation.decide(
            policy=review_policy, stages=executed, findings=findings,
            independence=independence, target=target)

        stale_reason = ''
        gate: dict[str, Any] = {'gate_state': 'NOT_EXECUTED',
                                'authorized_to_ship': False, 'reasons': []}
        if decision in (Decision.APPROVED, Decision.CHANGES_REQUESTED):
            try:
                assert_target_unchanged(root, target)
            except StaleTargetError as exc:
                stale_reason = str(exc)
                decision, reasons = Decision.STALE, (stale_reason,)
                _journal(writer, 'stale', {'run_id': run_id,
                                           'reason': stale_reason},
                         critical=True)
        if not stale_reason:
            if decision in (Decision.APPROVED, Decision.CHANGES_REQUESTED):
                gate = _run_gate(root, findings)
                if decision is Decision.APPROVED and not gate.get(
                        'authorized_to_ship'):
                    decision = Decision.BLOCKED
                    refusal = (f'ship gate refused: {gate.get("gate_state")} '
                               f'{"; ".join(gate.get("reasons") or [])}')
                    reasons = (refusal,)
            else:
                gate = {
                    'gate_state': 'NOT_EXECUTED',
                    'authorized_to_ship': False,
                    'reasons': ['prerequisites unmet: '
                                + '; '.join(reasons)],
                    'policy_version': ship_gate.POLICY_VERSION,
                }
        gate_stage = StageResult(
            stage=StageType.SHIP_GATE,
            status=(StageStatus.COMPLETED if gate.get('authorized_to_ship')
                    else StageStatus.NOT_EXECUTED),
            verdict=(Verdict.APPROVE if gate.get('authorized_to_ship')
                     else Verdict.INCONCLUSIVE),
            reviewer='asha/governance/ship_gate',
            provider='local', model=str(gate.get('policy_version', '')),
            started_at=stage_mod.now_iso(), ended_at=stage_mod.now_iso(),
            error='' if gate.get('authorized_to_ship')
            else '; '.join(str(reason) for reason in gate.get('reasons') or []))

        # The run status describes whether the pipeline executed, the
        # decision describes what may be delivered: "changes requested" is
        # a completed run, not a failed one.
        run_status = {
            Decision.APPROVED: StageStatus.COMPLETED,
            Decision.CHANGES_REQUESTED: StageStatus.COMPLETED,
            Decision.BLOCKED: StageStatus.BLOCKED,
            Decision.STALE: StageStatus.STALE,
        }[decision]
        all_stages = executed + (gate_stage,)
        directory = artifacts.run_dir(root, run_id)
        result = ReviewRunResult(
            run_id=run_id, target=target, status=run_status,
            decision=decision, reasons=tuple(reasons), stages=all_stages,
            findings=findings, relations=relations,
            independence=independence, gate=gate,
            artifacts=str(directory), started_at=started_at,
            ended_at=stage_mod.now_iso())
        artifacts.write_run(root, result)
        _journal(writer, 'decided', {
            'run_id': run_id, 'decision': decision.value,
            'gate_state': gate.get('gate_state'),
            'findings': len(findings),
            'reasons': list(reasons),
        }, critical=True)
        return result
    finally:
        if owns_journal and writer is not None:
            writer.close()


def review_status(root: Path, run_id: str) -> ReviewRunResult:
    """Read a persisted run; never re-runs or reconstructs missing data."""
    return artifacts.load_run(Path(root), run_id)
