"""Ladder core: finding contract, policy, independence and aggregation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_support import finding, make_repo  # type: ignore[import-not-found]

from asha.ladder import aggregation, prompts, stages
from asha.ladder.models import (
    Confidence,
    Decision,
    Finding,
    FindingError,
    Independence,
    ReviewTarget,
    Severity,
    StageResult,
    StageStatus,
    StageType,
    Verdict,
)
from asha.ladder.policy import (
    default_policy,
    distinct_provider_policy,
    resolve_policy,
)
from asha.ladder.target import resolve_target


def target_for(tmp_path: Path) -> ReviewTarget:
    repo = make_repo(tmp_path)
    return resolve_target(repo.root, base=repo.base, head=repo.head,
                          policy=default_policy(), run_id='rv-test',
                          started_at='2026-01-01T00:00:00+00:00')


def stage(stage_type: StageType, status: StageStatus = StageStatus.COMPLETED,
          verdict: Verdict = Verdict.APPROVE, **kwargs) -> StageResult:
    fields = {'reviewer': 'reviewer', 'execution_id': 'exec',
              'provider': 'p', 'model': 'm'}
    fields.update(kwargs)
    return StageResult(stage=stage_type, status=status, verdict=verdict,
                       **fields)  # type: ignore[arg-type]


def finding_obj(**overrides) -> Finding:
    payload = {
        'finding_id': 'code-001',
        'stage': StageType.CODE_REVIEW,
        'category': 'correctness',
        'severity': Severity.HIGH,
        'confidence': Confidence.CONFIRMED,
        'title': 'add() returns a difference',
        'description': 'add subtracts instead of adding',
        'file_path': 'app.py',
        'line_start': 2,
        'evidence': 'add(1, 2) is -1',
        'impact': 'wrong sums',
        'recommended_action': 'return a + b',
    }
    payload.update(overrides)
    return Finding(**payload)  # type: ignore[arg-type]


# --------------------------------------------------------------- finding contract
def test_finding_requires_real_severity_enum() -> None:
    with pytest.raises(FindingError, match='severity must be a Severity'):
        finding_obj(severity='HIGH')


@pytest.mark.parametrize('path', ['../etc/passwd', '/etc/passwd',
                                  'pkg\\mod.py', 'pkg/./mod.py'])
def test_finding_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(FindingError):
        finding_obj(file_path=path)


def test_finding_rejects_inverted_range() -> None:
    with pytest.raises(FindingError, match='precedes line_start'):
        finding_obj(line_start=9, line_end=3)


def test_finding_rejects_line_end_without_start() -> None:
    with pytest.raises(FindingError, match='line_end requires line_start'):
        finding_obj(line_start=None, line_end=3)


def test_finding_rejects_empty_evidence() -> None:
    with pytest.raises(FindingError, match="'evidence' must be a non-empty"):
        finding_obj(evidence='   ')


def test_finding_severity_decides_blocking_not_reviewer_claim() -> None:
    honest = finding_obj(severity=Severity.CRITICAL, blocking=False)
    lenient = finding_obj(severity=Severity.LOW, blocking=True)
    assert honest.blocks_delivery is True
    assert lenient.blocks_delivery is False


def test_finding_renders_canonical_semantic_review_payload() -> None:
    payload = finding_obj(severity=Severity.CRITICAL).gate_payload()
    assert payload['severity'] == 'BLOCKER'
    assert payload['line_range'] == '2'
    assert payload['problem'].startswith('add() returns a difference:')
    assert set(payload) == {'severity', 'file_path', 'line_range', 'problem',
                            'why_it_matters', 'failure_scenario',
                            'recommended_fix'}


def test_info_severity_does_not_block_the_gate() -> None:
    assert finding_obj(severity=Severity.INFO).gate_payload()['severity'] == 'LOW'


# ---------------------------------------------------------------------- policy
def test_default_policy_requires_every_mandatory_stage() -> None:
    policy = default_policy()
    assert set(policy.required_stages) == set(StageType)
    assert policy.required_independence is (
        Independence.SAME_PROVIDER_ISOLATED_CONTEXT)


def test_policy_hash_is_content_addressed() -> None:
    assert default_policy().content_hash() == default_policy().content_hash()
    assert (default_policy().content_hash()
            != distinct_provider_policy().content_hash())


def test_unknown_policy_fails_closed() -> None:
    with pytest.raises(ValueError, match='unknown review policy'):
        resolve_policy('whatever-sounds-nice')


def test_policy_without_stages_is_rejected() -> None:
    with pytest.raises(ValueError, match='at least one stage'):
        type(default_policy())(policy_id='empty', required_stages=(),
                               required_independence=Independence.UNKNOWN)


# --------------------------------------------------------------- independence
def test_independence_distinct_providers() -> None:
    first = stage(StageType.CODE_REVIEW, provider='a', execution_id='e1')
    second = stage(StageType.SENIOR_REVIEW, provider='b', execution_id='e2')
    assert aggregation.classify_independence(first, second) is (
        Independence.DISTINCT_PROVIDER)


def test_independence_same_provider_different_models_is_not_independent() -> None:
    first = stage(StageType.CODE_REVIEW, provider='a', model='m1',
                  execution_id='e1')
    second = stage(StageType.SENIOR_REVIEW, provider='a', model='m2',
                   execution_id='e2')
    assert aggregation.classify_independence(first, second) is (
        Independence.SAME_PROVIDER_ISOLATED_CONTEXT)


def test_independence_shared_session_is_same_session() -> None:
    first = stage(StageType.CODE_REVIEW, provider='a', execution_id='e1',
                  session_id='s1')
    second = stage(StageType.SENIOR_REVIEW, provider='a', execution_id='e2',
                   session_id='s1')
    assert aggregation.classify_independence(first, second) is (
        Independence.SAME_SESSION)


def test_independence_without_provider_metadata_is_unknown() -> None:
    first = stage(StageType.CODE_REVIEW, provider='', execution_id='e1')
    second = stage(StageType.SENIOR_REVIEW, provider='', execution_id='e2')
    assert aggregation.classify_independence(first, second) is (
        Independence.UNKNOWN)


def test_independence_floor_is_enforced_by_rank() -> None:
    assert aggregation.meets_independence(
        Independence.DISTINCT_PROVIDER,
        Independence.DISTINCT_PROVIDER) is True
    assert aggregation.meets_independence(
        Independence.SAME_PROVIDER_ISOLATED_CONTEXT,
        Independence.DISTINCT_PROVIDER) is False


# ---------------------------------------------------------------- aggregation
def test_relations_preserve_every_finding_identity() -> None:
    first = finding_obj(finding_id='code-001')
    second = finding_obj(finding_id='senior-001',
                         stage=StageType.SENIOR_REVIEW)
    relations = aggregation.relate_findings((first, second))
    kinds = {relation['kind'] for relation in relations}
    assert kinds == {'related', 'duplicate'}
    for relation in relations:
        assert relation['finding_ids'] == ['code-001', 'senior-001']


def _decide(stages_: tuple[StageResult, ...], findings: tuple[Finding, ...] = (),
            independence: Independence = Independence.DISTINCT_PROVIDER,
            stale: str = '', target: ReviewTarget | None = None
            ) -> tuple[Decision, tuple[str, ...]]:
    return aggregation.decide(
        policy=default_policy(), stages=stages_, findings=findings,
        independence=independence,
        target=target or ReviewTarget(repo_root='/r', branch='main',
                                      base_sha='a' * 40, head_sha='b' * 40,
                                      diff_sha256='c' * 64, policy_id='p',
                                      run_id='rv'),
        stale_reason=stale)


def _complete_stages() -> tuple[StageResult, ...]:
    return (stage(StageType.CODE_REVIEW),
            stage(StageType.SENIOR_REVIEW),
            stage(StageType.SEMANTIC_REVIEW, verdict=Verdict.NOT_APPLICABLE))


def test_decide_approves_only_a_complete_clean_run() -> None:
    decision, reasons = _decide(_complete_stages())
    assert decision is Decision.APPROVED
    assert 'mandatory stages completed' in reasons[0]


@pytest.mark.parametrize('missing', [StageType.CODE_REVIEW,
                                     StageType.SENIOR_REVIEW,
                                     StageType.SEMANTIC_REVIEW])
def test_decide_blocks_when_a_mandatory_stage_is_absent(
        missing: StageType) -> None:
    stages_ = tuple(item for item in _complete_stages()
                    if item.stage is not missing)
    decision, reasons = _decide(stages_)
    assert decision is Decision.BLOCKED
    assert f'{missing.value}: NOT_EXECUTED' in reasons[0]


@pytest.mark.parametrize('status', [StageStatus.BLOCKED, StageStatus.FAILED,
                                    StageStatus.NOT_EXECUTED,
                                    StageStatus.STALE])
def test_decide_blocks_on_any_unfinished_stage(status: StageStatus) -> None:
    stages_ = list(_complete_stages())
    stages_[1] = stage(StageType.SENIOR_REVIEW, status=status,
                       verdict=Verdict.INCONCLUSIVE, error='boom')
    decision, reasons = _decide(tuple(stages_))
    assert decision is Decision.BLOCKED
    assert f'SENIOR_REVIEW: {status.value}: boom' in reasons[0]


def test_decide_blocks_below_the_independence_floor() -> None:
    decision, reasons = _decide(
        _complete_stages(), independence=Independence.SAME_SESSION)
    assert decision is Decision.BLOCKED
    assert 'independence same_session' in reasons[0]


@pytest.mark.parametrize('severity', [Severity.CRITICAL, Severity.HIGH])
def test_decide_requests_changes_for_blocking_severity(
        severity: Severity) -> None:
    decision, reasons = _decide(
        _complete_stages(), findings=(finding_obj(severity=severity),))
    assert decision is Decision.CHANGES_REQUESTED
    assert 'code-001' in reasons[0]


@pytest.mark.parametrize('severity', [Severity.MEDIUM, Severity.LOW,
                                      Severity.INFO])
def test_decide_lets_non_blocking_severity_through(severity: Severity) -> None:
    decision, _reasons = _decide(
        _complete_stages(), findings=(finding_obj(severity=severity),))
    assert decision is Decision.APPROVED


def test_decide_blocks_an_inconclusive_reviewer() -> None:
    stages_ = list(_complete_stages())
    stages_[0] = stage(StageType.CODE_REVIEW, verdict=Verdict.INCONCLUSIVE)
    decision, reasons = _decide(tuple(stages_))
    assert decision is Decision.BLOCKED
    assert 'INCONCLUSIVE' in reasons[0]


def test_decide_surfaces_reviewer_disagreement() -> None:
    stages_ = list(_complete_stages())
    stages_[1] = stage(StageType.SENIOR_REVIEW,
                       verdict=Verdict.REQUEST_CHANGES)
    decision, reasons = _decide(tuple(stages_))
    assert decision is Decision.CHANGES_REQUESTED
    assert 'reviewers disagreed' in reasons[0]


def test_decide_reports_stale_before_anything_else() -> None:
    decision, reasons = _decide(_complete_stages(), stale='head moved')
    assert decision is Decision.STALE
    assert reasons == ('head moved',)


# -------------------------------------------------------------- reviewer output
def test_parse_document_accepts_one_markdown_fence() -> None:
    text = '```json\n{"verdict": "APPROVE", "findings": []}\n```'
    assert stages.parse_document(text)['verdict'] == 'APPROVE'


@pytest.mark.parametrize('text', ['not json', '[]', '{"findings": []}'])
def test_parse_document_rejects_unusable_output(text: str) -> None:
    with pytest.raises(stages.StageFailure):
        stages.parse_verdict(stages.parse_document(text))


def test_parse_verdict_rejects_invented_values() -> None:
    with pytest.raises(stages.StageFailure, match='unknown verdict'):
        stages.parse_verdict({'verdict': 'SHIP IT'})


def test_build_findings_accepts_a_valid_finding(tmp_path: Path) -> None:
    target = target_for(tmp_path)
    findings = stages.build_findings(
        {'findings': [finding()]}, stage=StageType.CODE_REVIEW,
        target=target, root=Path(target.repo_root))
    assert findings[0].finding_id == 'code-001'
    assert findings[0].severity is Severity.HIGH


def test_build_findings_rejects_a_file_outside_the_change_set(
        tmp_path: Path) -> None:
    target = target_for(tmp_path)
    with pytest.raises(stages.StageFailure, match='not part of the reviewed'):
        stages.build_findings({'findings': [finding(file_path='setup.py')]},
                              stage=StageType.CODE_REVIEW, target=target,
                              root=Path(target.repo_root))


def test_build_findings_rejects_a_line_past_the_end(tmp_path: Path) -> None:
    target = target_for(tmp_path)
    with pytest.raises(stages.StageFailure, match='past the end'):
        stages.build_findings({'findings': [finding(line_start=99,
                                                    line_end=99)]},
                              stage=StageType.CODE_REVIEW, target=target,
                              root=Path(target.repo_root))


def test_build_findings_rejects_unknown_severity(tmp_path: Path) -> None:
    target = target_for(tmp_path)
    with pytest.raises(stages.StageFailure, match='unknown severity'):
        stages.build_findings({'findings': [finding(severity='SEVERE')]},
                              stage=StageType.CODE_REVIEW, target=target,
                              root=Path(target.repo_root))


def test_build_findings_rejects_traversal(tmp_path: Path) -> None:
    target = target_for(tmp_path)
    with pytest.raises(stages.StageFailure, match='not part of the reviewed'):
        stages.build_findings({'findings': [finding(file_path='../app.py')]},
                              stage=StageType.CODE_REVIEW, target=target,
                              root=Path(target.repo_root))


def test_prompts_are_stage_specific_and_versioned() -> None:
    code = prompts.system_prompt(StageType.CODE_REVIEW.value)
    senior = prompts.system_prompt(StageType.SENIOR_REVIEW.value)
    rendered = prompts.render_user_content(
        stage=StageType.CODE_REVIEW.value,
        target={'base_sha': 'a' * 40, 'head_sha': 'b' * 40,
                'diff_sha256': 'c' * 64, 'files': ()},
        diff_text='diff', snippets='context')
    assert code != senior
    assert f'prompt_version: {prompts.PROMPT_VERSION}' in rendered
    assert prompts.CONTEXT_MARKER in rendered
    assert prompts.prompt_sha256(StageType.CODE_REVIEW.value) != (
        prompts.prompt_sha256(StageType.SENIOR_REVIEW.value))
