"""Host-driven Ladder service: one review workflow for CLI, MCP and code.

The host LLM is the reviewer. Asha mints the run, serves the exact
committed context, validates what the host submits, and owns the final
delivery decision::

    start -> packet(CODE_REVIEW) -> submit -> packet(SENIOR_REVIEW)
          -> submit -> packet(SEMANTIC_REVIEW) -> submit -> finalize
          -> canonical Ship Gate -> decision

No step calls a model, and no step needs a provider credential: nothing
here reads an API key, an endpoint or a model name. A missing stage, an
invalid submission, a stale target, an expired deadline or a gate refusal
can only produce BLOCKED / CHANGES_REQUESTED / STALE / FAILED -- never
APPROVED.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
from dataclasses import replace
from pathlib import Path
from typing import Any

from asha import evidence as evidence_mod
from asha import telemetry
from asha.governance import semantic_review, ship_gate
from asha.ladder import aggregation, artifacts, instructions
from asha.ladder.models import (
    EXECUTION_MODE,
    Confidence,
    Decision,
    Finding,
    FindingError,
    Independence,
    ReviewerInfo,
    ReviewRun,
    RunState,
    Severity,
    StageResult,
    StageStatus,
    StageType,
    SubmissionError,
    Verdict,
)
from asha.ladder.policy import ReviewPolicy, resolve_policy
from asha.ladder.target import (
    DEFAULT_CONTEXT_BYTES,
    MAX_CONTEXT_BYTES,
    StaleTargetError,
    assert_target_unchanged,
    diff_chunk,
    file_chunk,
    file_line_count,
    resolve_target,
    tracked_at,
)

STAGE_ORDER: tuple[StageType, ...] = (
    StageType.CODE_REVIEW,
    StageType.SENIOR_REVIEW,
    StageType.SEMANTIC_REVIEW,
)

DEFAULT_DEADLINE_S = 3600.0
MAX_DEADLINE_S = 86400.0

#: Payload bounds: a submission is untrusted input from the host.
MAX_FINDINGS = 200
MAX_TEXT = 4000

JOURNAL_EVENTS = {
    'started': 'review_started',
    'requested': 'review_stage_requested',
    'submitted': 'review_stage_submitted',
    'validated': 'review_stage_validated',
    'blocked': 'review_blocked',
    'stale': 'review_stale',
    'finalized': 'review_finalized',
    'gate': 'ship_gate_evaluated',
}

_TERMINAL = (RunState.FINALIZED, RunState.BLOCKED, RunState.STALE,
             RunState.FAILED)


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f'timestamp without timezone: {value!r}')
    return parsed


def stage_id_for(run_id: str, stage: StageType) -> str:
    """Server-minted stage id: derived from the server-minted run id."""
    return f'{run_id}:{stage.value}'


def _journal(root: Path, run_id: str, event: str,
             payload: dict[str, Any], *, critical: bool = False) -> None:
    """Journal errors propagate: a broken journal is not a success."""
    writer = telemetry.EventJournalWriter(root, run_id)
    try:
        writer.append(JOURNAL_EVENTS[event],
                      {'run_id': run_id, 'execution_mode': EXECUTION_MODE,
                       **payload}, critical=critical)
    finally:
        writer.close()


def _expired(run: ReviewRun) -> bool:
    try:
        return dt.datetime.now(dt.UTC) >= _parse_iso(run.expires_at)
    except (TypeError, ValueError):
        # An unreadable expiry is not a licence to continue.
        return True


def _fail_expired(root: Path, run: ReviewRun) -> ReviewRun:
    reason = (f'deadline_exceeded: review expired at {run.expires_at}; '
              'no stage is accepted and the Ship Gate is not invoked')
    stages = tuple(
        replace(stage, status=StageStatus.FAILED, error=reason)
        if stage.status is StageStatus.VALIDATED and stage.stage is not StageType.SHIP_GATE
        else stage for stage in run.stages)
    failed = replace(run, state=RunState.FAILED, decision=Decision.FAILED,
                     reasons=(reason,), stages=stages, finalized_at=now_iso())
    artifacts.save_run(root, failed)
    _journal(root, run.run_id, 'blocked',
             {'stage': 'DEADLINE', 'status': 'FAILED', 'error': reason},
             critical=True)
    return failed


def _load(root: Path, run_id: str) -> ReviewRun:
    """Load a run and enforce the persisted absolute deadline."""
    run = artifacts.load_run(root, run_id)
    if run.state not in _TERMINAL and _expired(run):
        return _fail_expired(root, run)
    return run


def start_review(root: Path, *, base: str = 'origin/main',
                 head: str = 'HEAD', policy: ReviewPolicy | str | None = None,
                 deadline_seconds: float | None = None,
                 host_client: str = 'unknown') -> ReviewRun:
    """Create a review run and persist the first packet. No inference."""
    root = Path(root).resolve()
    review_policy = (resolve_policy(policy) if isinstance(policy, str)
                     or policy is None else policy)
    seconds = DEFAULT_DEADLINE_S if deadline_seconds is None else float(
        deadline_seconds)
    if not 0 < seconds <= MAX_DEADLINE_S:
        raise ValueError(
            f'deadline_seconds must be within (0, {MAX_DEADLINE_S:g}]')
    run_id = artifacts.new_run_id()
    created = now_iso()
    expires = (dt.datetime.fromisoformat(created)
               + dt.timedelta(seconds=seconds)).isoformat()
    target = resolve_target(root, base=base, head=head, policy=review_policy,
                            run_id=run_id, started_at=created)
    run = ReviewRun(
        run_id=run_id, target=target, state=RunState.AWAITING_HOST_REVIEW,
        artifacts=str(artifacts.run_dir(root, run_id)),
        created_at=created, expires_at=expires)
    artifacts.save_run(root, run)
    _journal(root, run_id, 'started', {
        'base': target.base_sha, 'head': target.head_sha,
        'base_ref': base, 'head_ref': head,
        'policy_id': review_policy.policy_id,
        'policy_hash': review_policy.content_hash(),
        'expires_at': expires, 'host_client': host_client}, critical=True)
    _write_packet(root, run, review_policy, StageType.CODE_REVIEW)
    return run


def _pending_stage(run: ReviewRun) -> StageType | None:
    for stage in STAGE_ORDER:
        record = run.stage(stage)
        if record is None or not record.validated:
            return stage
    return None


def _write_packet(root: Path, run: ReviewRun, policy: ReviewPolicy,
                  stage: StageType) -> dict[str, Any]:
    """Build (and persist the identity of) one stage packet."""
    prior: tuple[dict[str, Any], ...] = ()
    if stage is StageType.SEMANTIC_REVIEW:
        # Only the reconciliation pass may see the earlier findings.
        prior = tuple(finding.as_dict() for finding in run.findings)
    packet = instructions.build_packet(
        run, stage, stage_id_for(run.run_id, stage), policy,
        prior_findings=prior)
    body = json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()
    document = {
        'run_id': run.run_id,
        'stage': stage.value,
        'stage_id': packet['stage_id'],
        'prompt_version': instructions.PROMPT_VERSION,
        'packet_sha256': hashlib.sha256(body).hexdigest(),
        'created_at': now_iso(),
        'target': run.target.identity(),
    }
    path = artifacts.write_packet(root, run.run_id, stage, document)
    _journal(root, run.run_id, 'requested',
             {'stage': stage.value, 'stage_id': packet['stage_id'],
              'packet_sha256': document['packet_sha256'],
              'artifact': path})
    return packet


def next_stage(root: Path, run_id: str) -> dict[str, Any]:
    """Return the packet for the next stage the host must perform."""
    root = Path(root).resolve()
    run = _load(root, run_id)
    if run.state is RunState.FINALIZED or run.decision is not None:
        raise SubmissionError(
            f'run {run_id} is already finalized with '
            f'{run.decision.value if run.decision else run.state.value}')
    if run.state in _TERMINAL:
        raise SubmissionError(
            f'run {run_id} is {run.state.value}: {"; ".join(run.reasons)}')
    stage = _pending_stage(run)
    if stage is None:
        return {
            'review_run_id': run.run_id,
            'state': run.state.value,
            'required_next_action': (
                'all review stages are validated; call asha_review_finalize '
                'to run deterministic validation and the Ship Gate'),
        }
    policy = resolve_policy(run.target.policy_id)
    return _write_packet(root, run, policy, stage)


def review_context(root: Path, run_id: str, *, stage_id: str = '',
                   file_path: str | None = None, cursor: int = 0,
                   max_bytes: int = DEFAULT_CONTEXT_BYTES) -> dict[str, Any]:
    """Serve one bounded chunk of the pinned target; read-only, no commands."""
    root = Path(root).resolve()
    run = _load(root, run_id)
    if stage_id:
        allowed = {stage_id_for(run.run_id, stage) for stage in STAGE_ORDER}
        if stage_id not in allowed:
            raise SubmissionError(
                f'unknown stage id {stage_id!r} for run {run_id}')
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise SubmissionError('cursor must be a non-negative integer')
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not (
            0 < max_bytes <= MAX_CONTEXT_BYTES):
        raise SubmissionError(
            f'max_bytes must be within (0, {MAX_CONTEXT_BYTES}]')
    if file_path is None:
        chunk = diff_chunk(root, run.target, cursor=cursor,
                           max_bytes=max_bytes)
    else:
        if not isinstance(file_path, str) or not file_path.strip():
            raise SubmissionError('file_path must be a non-empty string')
        chunk = file_chunk(root, run.target, file_path, cursor=cursor,
                           max_bytes=max_bytes)
    chunk.update({'stage_id': stage_id, 'state': run.state.value,
                  'expires_at': run.expires_at})
    return chunk


def _coerce_findings(stage: StageType, payload: Any) -> tuple[Finding, ...]:
    if payload is None:
        payload = []
    if not isinstance(payload, list):
        raise SubmissionError('findings must be a list')
    if len(payload) > MAX_FINDINGS:
        raise SubmissionError(f'too many findings ({len(payload)} > {MAX_FINDINGS})')
    findings: list[Finding] = []
    for item in payload:
        if not isinstance(item, dict):
            raise SubmissionError('each finding must be an object')
        unknown = set(item) - set(Finding.__dataclass_fields__)
        if unknown:
            raise SubmissionError(
                'unknown finding fields: ' + ', '.join(sorted(unknown)))
        try:
            finding = Finding(
                finding_id=str(item.get('finding_id', '')),
                stage=stage,
                category=str(item.get('category', '')),
                severity=_enum(item.get('severity'), 'severity'),
                confidence=_enum(item.get('confidence'), 'confidence'),
                title=str(item.get('title', '')),
                description=str(item.get('description', '')),
                file_path=str(item.get('file_path', '')),
                line_start=item.get('line_start'),
                line_end=item.get('line_end'),
                evidence=str(item.get('evidence', '')),
                impact=str(item.get('impact', '')),
                recommended_action=str(item.get('recommended_action', '')),
                blocking=item.get('blocking', False),
            )
        except (FindingError, ValueError, TypeError) as exc:
            raise SubmissionError(f'invalid finding: {exc}') from exc
        findings.append(finding)
    return tuple(findings)


def _enum(value: Any, name: str) -> Any:
    members: Any = {'severity': Severity, 'confidence': Confidence}[name].__members__
    key = str(getattr(value, 'name', value)).strip().upper()
    if key in members:
        return members[key]
    if not isinstance(value, str):
        raise FindingError(f'{name} must be a string, got {type(value).__name__}')
    raise FindingError(f'unknown {name} {value!r}; allowed: '
                       + ', '.join(members))


def _validate_against_target(root: Path, run: ReviewRun,
                             findings: tuple[Finding, ...]) -> list[dict[str, Any]]:
    """Ground each finding in the pinned commit; failures reject the stage."""
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.finding_id in seen:
            raise SubmissionError(
                f'duplicate finding_id {finding.finding_id!r} in one stage')
        seen.add(finding.finding_id)
        if not tracked_at(root, run.target.head_sha, finding.file_path):
            raise SubmissionError(
                f'finding {finding.finding_id}: {finding.file_path!r} is not '
                f'a file in the reviewed commit {run.target.head_sha[:12]}')
        lines = file_line_count(root, run.target.head_sha, finding.file_path)
        if finding.line_start is not None:
            if lines is None:
                raise SubmissionError(
                    f'finding {finding.finding_id}: cannot read line count')
            last = finding.line_end if finding.line_end is not None else finding.line_start
            if last > lines:
                raise SubmissionError(
                    f'finding {finding.finding_id}: lines '
                    f'{finding.line_start}-{last} exceed the {lines} lines of '
                    f'{finding.file_path} at the reviewed head')
        checked.append({
            'finding_id': finding.finding_id,
            'file_path': finding.file_path,
            'in_changed_files': finding.file_path in run.target.files,
            'lines_exist': True,
        })
    return checked


def submit_stage(root: Path, run_id: str, *, stage_id: str, stage: str,
                 verdict: str, summary: str, findings: Any = None,
                 host_client: str = '', isolation: str = '',
                 attestation_source: str = '') -> dict[str, Any]:
    """Validate and persist one host-generated stage. No inference."""
    root = Path(root).resolve()
    run = _load(root, run_id)
    if run.state in _TERMINAL:
        raise SubmissionError(
            f'run {run_id} is {run.state.value}: {"; ".join(run.reasons)}')
    try:
        stage_type = StageType(str(stage).strip().upper())
    except ValueError as exc:
        raise SubmissionError(f'unknown stage {stage!r}') from exc
    if stage_id != stage_id_for(run.run_id, stage_type):
        raise SubmissionError(
            f'unknown stage id {stage_id!r}; expected '
            f'{stage_id_for(run.run_id, stage_type)!r}')
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_TEXT:
        raise SubmissionError(
            f'summary must be a non-empty string (<= {MAX_TEXT} chars)')
    try:
        verdict_value = Verdict(str(verdict).strip().upper())
    except ValueError as exc:
        raise SubmissionError(f'unknown verdict {verdict!r}') from exc
    if verdict_value is Verdict.NOT_APPLICABLE:
        raise SubmissionError(
            'verdict must be APPROVE, REQUEST_CHANGES or INCONCLUSIVE')

    coarsed = _coerce_findings(stage_type, findings)
    grounded = _validate_against_target(root, run, coarsed)
    reviewer = ReviewerInfo(
        host_client=(host_client or 'unknown')[:200],
        isolation=_isolation(isolation),
        attestation_source=(attestation_source or '')[:500])

    submission = {
        'stage': stage_type.value,
        'verdict': verdict_value.value,
        'summary': summary,
        'findings': [finding.as_dict() for finding in coarsed],
        'reviewer': reviewer.as_dict(),
    }
    digest = hashlib.sha256(json.dumps(
        submission, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    # The stored submission is checked before the stage order: replaying the
    # identical result is a read, not an out-of-order write.
    existing = run.stage(stage_type)
    if existing is not None and existing.validated:
        if existing.submission_sha256 == digest:
            return _receipt(run, existing, idempotent=True)
        raise SubmissionError(
            f'{stage_type.value} already validated for run {run_id}; a '
            'different submission cannot overwrite an immutable stage result')

    expected = _pending_stage(run)
    if expected is None:
        raise SubmissionError(
            'all review stages are already validated; call finalize')
    if stage_type is not expected:
        raise SubmissionError(
            f'out of order: expected {expected.value}, got {stage_type.value}')

    stamp = now_iso()
    record = StageResult(
        stage=stage_type, status=StageStatus.VALIDATED,
        stage_id=stage_id, verdict=verdict_value, summary=summary,
        reviewer=reviewer, execution_id=f'st-{stage_type.value.lower()}-'
                                       f'{secrets.token_hex(6)}',
        submission_sha256=digest, submitted_at=stamp, validated_at=stamp,
        findings=coarsed,
        artifact=str(artifacts.stage_path(root, run.run_id, stage_type)))
    _journal(root, run.run_id, 'submitted',
             {'stage': stage_type.value, 'stage_id': stage_id,
              'verdict': verdict_value.value, 'findings': len(coarsed),
              'submission_sha256': digest,
              'host_client': reviewer.host_client,
              'isolation': reviewer.isolation.value})
    _journal(root, run.run_id, 'validated',
             {'stage': stage_type.value, 'stage_id': stage_id,
              'grounded_findings': len(grounded),
              'execution_id': record.execution_id})

    stages = tuple(item for item in run.stages if item.stage is not stage_type
                   ) + (record,)
    stages = tuple(sorted(stages, key=lambda item: STAGE_ORDER.index(item.stage)
                          if item.stage in STAGE_ORDER else 99))
    findings_all = tuple(finding for item in stages
                         for finding in item.findings)
    independence = run.independence
    code = [item for item in stages if item.stage in aggregation.CODE_STAGES
            and item.validated]
    if len(code) == 2:
        independence = aggregation.classify_independence(code[0], code[1])
    state = (RunState.AWAITING_HOST_REVIEW if _pending_stage(
        replace(run, stages=stages)) is not None else RunState.AWAITING_FINALIZE)
    updated = replace(run, stages=stages, findings=findings_all,
                      relations=aggregation.relate_findings(findings_all),
                      independence=independence, state=state)
    artifacts.save_run(root, updated)
    return _receipt(updated, record, idempotent=False)


def _isolation(value: str) -> Independence:
    """Parse the host's isolation claim; anything odd is ``UNKNOWN``."""
    if not isinstance(value, str) or not value.strip():
        return Independence.UNKNOWN
    try:
        return Independence(value.strip().upper())
    except ValueError:
        return Independence.UNKNOWN


def _receipt(run: ReviewRun, record: StageResult, *,
             idempotent: bool) -> dict[str, Any]:
    pending = _pending_stage(run)
    return {
        'review_run_id': run.run_id,
        'stage': record.stage.value,
        'stage_id': record.stage_id,
        'status': record.status.value,
        'verdict': record.verdict.value,
        'execution_id': record.execution_id,
        'submission_sha256': record.submission_sha256,
        'idempotent': idempotent,
        'findings_accepted': len(record.findings),
        'state': run.state.value,
        'required_next_action': (
            'call asha_review_finalize to run deterministic validation and '
            'the Ship Gate' if pending is None
            else f'call asha_review_next_stage for {pending.value}'),
    }


def _gate_reviewer(findings: tuple[Finding, ...], policy_version: str) -> Any:
    """Adapter: hand the validated findings to the canonical gate."""
    payload = [finding.gate_payload() for finding in findings]

    def reviewer(_root: Path, snapshot_id: str, _timeout: int) -> dict:
        return {
            'schema': semantic_review.REVIEW_SCHEMA,
            'snapshot_id': snapshot_id,
            'policy_version': policy_version,
            'status': 'ladder',
            'model': 'ladder/host-submitted',
            'findings': list(payload),
        }

    return reviewer


def _run_gate(root: Path, findings: tuple[Finding, ...]) -> dict[str, Any]:
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


def _deterministic_validation(run: ReviewRun, *,
                              stale_reason: str = '') -> dict[str, Any]:
    """Asha's own checks over the submitted record. Not a review pass."""
    problems: list[str] = []
    per_stage: dict[str, int] = {}
    for stage in STAGE_ORDER:
        record = run.stage(stage)
        if record is None or not record.validated:
            problems.append(f'{stage.value}: not validated')
            continue
        per_stage[stage.value] = len(record.findings)
    blocking = [finding for finding in run.findings if finding.blocks_delivery]
    semantic = run.stage(StageType.SEMANTIC_REVIEW)
    if semantic is not None and semantic.validated:
        if semantic.verdict is Verdict.APPROVE and blocking:
            problems.append(
                'semantic review approved while '
                f'{len(blocking)} blocking finding(s) remain')
        if semantic.verdict is Verdict.INCONCLUSIVE:
            problems.append('semantic review was INCONCLUSIVE')
    if stale_reason:
        problems.append(stale_reason)
    return {
        'checked_at': now_iso(),
        'validator': 'deterministic',
        'stages_validated': list(per_stage),
        'findings_per_stage': per_stage,
        'findings_total': len(run.findings),
        'blocking': [finding.finding_id for finding in blocking],
        'relations': list(run.relations),
        'independence': run.independence.value,
        'problems': problems,
    }


def _validated(run: ReviewRun, stage: StageType) -> bool:
    record = run.stage(stage)
    return record is not None and record.validated


def finalize(root: Path, run_id: str) -> ReviewRun:
    """Validate the whole record, then ask the canonical Ship Gate."""
    root = Path(root).resolve()
    run = _load(root, run_id)
    if run.state is RunState.FINALIZED:
        return run
    if run.state in _TERMINAL:
        # A decided run is immutable: finalizing it again reads the recorded
        # decision. It is neither a new attempt nor an approval.
        return run

    missing = [stage.value for stage in STAGE_ORDER
               if not _validated(run, stage)]
    if missing:
        reason = 'mandatory stage(s) not submitted: ' + ', '.join(missing)
        blocked = replace(
            run, state=RunState.BLOCKED, decision=Decision.BLOCKED,
            reasons=(reason,),
            semantic_validation=_deterministic_validation(run),
            gate={'gate_state': 'NOT_EXECUTED', 'authorized_to_ship': False,
                  'reasons': [reason],
                  'policy_version': ship_gate.POLICY_VERSION},
            finalized_at=now_iso())
        artifacts.save_run(root, blocked)
        _journal(root, run_id, 'blocked', {'stage': 'FINALIZE',
                                           'error': reason}, critical=True)
        return blocked

    stale_reason = ''
    try:
        assert_target_unchanged(root, run.target)
    except StaleTargetError as exc:
        stale_reason = str(exc)
        _journal(root, run_id, 'stale', {'reason': stale_reason},
                 critical=True)

    validation = _deterministic_validation(run, stale_reason=stale_reason)
    policy = resolve_policy(run.target.policy_id)
    decision, reasons = aggregation.decide(
        policy=policy, stages=run.stages, findings=run.findings,
        independence=run.independence, target=run.target,
        stale_reason=stale_reason)
    gate: dict[str, Any] = {'gate_state': 'NOT_EXECUTED',
                            'authorized_to_ship': False,
                            'reasons': list(reasons)}
    if stale_reason:
        decision = Decision.STALE
    elif validation['problems'] and decision is Decision.APPROVED:
        decision = Decision.BLOCKED
        reasons = tuple(validation['problems'])
        gate['reasons'] = list(reasons)
    elif decision in (Decision.APPROVED, Decision.CHANGES_REQUESTED):
        gate = _run_gate(root, run.findings)
        _journal(root, run_id, 'gate', {
            'gate_state': gate.get('gate_state'),
            'authorized_to_ship': gate.get('authorized_to_ship'),
            'reasons': list(gate.get('reasons') or [])})
        if decision is Decision.APPROVED and not gate.get('authorized_to_ship'):
            decision = Decision.BLOCKED
            reasons = (
                'ship gate refused: '
                + str(gate.get('gate_state')) + ' '
                + '; '.join(str(item) for item in (gate.get('reasons') or [])),
            )

    # A gate that ran and refused produced a recorded result; only a gate
    # that never ran (missing stage, stale target) is NOT_EXECUTED.
    gate_ran = str(gate.get('gate_state') or '') not in ('', 'NOT_EXECUTED')
    gate_stage = StageResult(
        stage=StageType.SHIP_GATE,
        status=(StageStatus.VALIDATED if gate_ran
                else StageStatus.NOT_EXECUTED),
        stage_id=stage_id_for(run_id, StageType.SHIP_GATE),
        verdict=(Verdict.APPROVE if gate.get('authorized_to_ship')
                 else Verdict.INCONCLUSIVE),
        summary=f'gate_state={gate.get("gate_state")}',
        reviewer=ReviewerInfo(host_client='asha/governance/ship_gate'),
        execution_id=f'st-ship_gate-{secrets.token_hex(6)}',
        error='' if gate.get('authorized_to_ship')
        else '; '.join(str(reason) for reason in gate.get('reasons') or []))
    state = {
        Decision.APPROVED: RunState.FINALIZED,
        Decision.CHANGES_REQUESTED: RunState.FINALIZED,
        Decision.BLOCKED: RunState.BLOCKED,
        Decision.STALE: RunState.STALE,
        Decision.FAILED: RunState.FAILED,
    }[decision]
    result = replace(
        run, state=state, decision=decision, reasons=tuple(reasons),
        stages=tuple(item for item in run.stages
                     if item.stage is not StageType.SHIP_GATE) + (gate_stage,),
        semantic_validation=validation, gate=gate, finalized_at=now_iso())
    artifacts.save_run(root, result)
    _journal(root, run_id, 'finalized', {
        'decision': decision.value, 'state': state.value,
        'gate_state': gate.get('gate_state'),
        'findings': len(result.findings),
        'independence': result.independence.value,
        'reasons': list(reasons)}, critical=True)
    return result


def review_status(root: Path, run_id: str) -> ReviewRun:
    """Read a persisted run; never re-runs or reconstructs missing data."""
    return _load(Path(root).resolve(), run_id)
