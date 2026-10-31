"""End-to-end Ladder runs: decisions, provenance, artifacts, fail-closed paths.

Every provider here is a test-only fake. No assertion in this file claims a
live review happened.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_support import (  # type: ignore[import-not-found]
    FakeProvider,
    document,
    finding,
    git,
    make_repo,
    stub_gate,
)

from asha.ladder import artifacts, run_review
from asha.ladder.models import Decision, StageStatus, StageType
from asha.ladder.policy import distinct_provider_policy
from asha.ladder.providers import (
    ProviderTimeout,
    ProviderUnavailable,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every artifact and journal out of the real state directory."""
    monkeypatch.setenv('ASHA_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('ASHA_LLM_API_KEY', raising=False)


def run(repo, provider=None, **kwargs):
    return run_review(repo.root, base=repo.base, head=repo.head,
                      provider=provider, **kwargs)


def journal_events(repo) -> list[str]:
    directory = Path(repo.root).parent / 'state'
    records: list[str] = []
    for path in directory.rglob('*.jsonl'):
        records.extend(json.loads(line)['event_type']
                       for line in path.read_text().splitlines() if line)
    return records


# ------------------------------------------------------------------ happy paths
def test_approved_run_records_every_stage_and_artifact(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider()

    result = run(repo, provider)

    assert result.decision is Decision.APPROVED
    assert result.status is StageStatus.COMPLETED
    assert result.approved is True
    assert [stage.stage for stage in result.stages] == list(StageType)
    assert all(stage.status is StageStatus.COMPLETED
               for stage in result.stages[:3])
    assert result.gate['authorized_to_ship'] is True
    assert result.independence.value == 'same_provider_isolated_context'
    for name in ('run.json', 'target.json', 'findings.json', 'decision.json'):
        assert (Path(result.artifacts) / name).is_file()
    assert (Path(result.artifacts) / 'stages' / 'CODE_REVIEW.json').is_file()


def test_approved_run_survives_a_reload_from_disk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    first = run(repo, FakeProvider())

    again = artifacts.load_run(repo.root, first.run_id)

    assert again.decision is first.decision
    assert again.target.head_sha == first.target.head_sha
    assert again.target.diff_sha256 == first.target.diff_sha256
    assert [stage.stage for stage in again.stages] == [
        stage.stage for stage in first.stages]


def test_blocking_finding_requests_changes_and_gate_refuses(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider(code=document(finding(), verdict='REQUEST_CHANGES'),
                            senior=document(finding(), verdict='REQUEST_CHANGES'))

    result = run(repo, provider)

    assert result.decision is Decision.CHANGES_REQUESTED
    assert result.approved is False
    assert result.gate['authorized_to_ship'] is False
    assert any('HIGH' in reason for reason in result.reasons)
    assert len(result.findings) == 2
    assert result.relations


def test_non_blocking_findings_do_not_stop_delivery(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    medium = document(finding(severity='MEDIUM', blocking=False))
    provider = FakeProvider(code=medium, senior=medium)

    result = run(repo, provider)

    assert result.decision is Decision.APPROVED
    assert result.findings[0].blocks_delivery is False


def test_reviewer_disagreement_keeps_both_verdicts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider(code=document(verdict='APPROVE'),
                            senior=document(verdict='REQUEST_CHANGES'))

    result = run(repo, provider)

    assert result.decision is Decision.CHANGES_REQUESTED
    verdicts = {stage.stage: stage.verdict for stage in result.stages}
    assert verdicts[StageType.CODE_REVIEW].value == 'APPROVE'
    assert verdicts[StageType.SENIOR_REVIEW].value == 'REQUEST_CHANGES'
    assert 'reviewers disagreed' in result.reasons[0]


# ------------------------------------------------------------- reviewer isolation
def test_reviewers_share_the_target_but_not_context(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider(
        code=document(finding(title='code sees arithmetic bug'),
                      verdict='APPROVE'),
        senior=document(verdict='APPROVE'))

    result = run(repo, provider)

    code_call, senior_call = provider.calls
    assert code_call['stage'] == 'CODE_REVIEW'
    assert senior_call['stage'] == 'SENIOR_REVIEW'
    target = result.target
    for call in provider.calls:
        assert target.head_sha in call['user']
        assert target.diff_sha256 in call['user']
    # neither first pass is anchored by the other's conclusion
    assert 'code sees arithmetic bug' not in senior_call['user']
    assert 'Senior Reviewer' in senior_call['system']
    stages = {stage.stage: stage for stage in result.stages}
    assert (stages[StageType.CODE_REVIEW].context_sha256
            != stages[StageType.SENIOR_REVIEW].context_sha256)
    assert (stages[StageType.CODE_REVIEW].execution_id
            != stages[StageType.SENIOR_REVIEW].execution_id)


def test_target_is_read_from_the_commit_not_the_worktree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    # A later commit moves the worktree content past the reviewed head; the
    # reviewer must still see the bytes of the commit it was asked to review.
    (repo.root / 'app.py').write_text(
        'def add(a, b):\n    return a + b\n\n\n# LATER MARKER\n',
        encoding='utf-8')
    git(repo.root, 'commit', '-qam', 'later work')
    provider = FakeProvider()

    result = run(repo, provider)

    context = '\n'.join(call['user'] for call in provider.calls)
    assert 'LATER MARKER' not in context
    assert 'return a - b' in context
    assert result.target.head_sha == repo.head


def test_dirty_worktree_is_rejected(tmp_path: Path) -> None:
    from asha.ladder.models import TargetError

    repo = make_repo(tmp_path)
    (repo.root / 'app.py').write_text('dirty\n', encoding='utf-8')
    git(repo.root, 'add', '-A')

    with pytest.raises(TargetError, match='uncommitted changes'):
        run(repo, FakeProvider())


# ---------------------------------------------------------------- provider failure
def test_missing_provider_blocks_without_inventing_a_review(
        tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    result = run(repo)

    assert result.decision is Decision.BLOCKED
    assert result.gate['gate_state'] == 'NOT_EXECUTED'
    assert 'ASHA_LLM_API_KEY' in result.reasons[0]
    review_stages = [stage for stage in result.stages
                     if stage.stage in (StageType.CODE_REVIEW,
                                        StageType.SENIOR_REVIEW)]
    assert [stage.status for stage in review_stages] == [StageStatus.BLOCKED] * 2
    assert result.findings == ()
    # the blocked run is still auditable later
    assert artifacts.load_run(repo.root, result.run_id).decision is (
        Decision.BLOCKED)


def test_provider_unavailable_is_blocked_not_approved(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider(error=ProviderUnavailable('no credentials'))

    result = run(repo, provider)

    assert result.decision is Decision.BLOCKED
    assert 'unavailable' in result.reasons[0]
    assert result.approved is False


def test_provider_timeout_is_a_failed_stage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider(error=ProviderTimeout('budget spent'))

    result = run(repo, provider)

    assert result.decision is Decision.BLOCKED
    assert result.stages[0].status is StageStatus.FAILED
    assert result.stages[0].error.startswith('timeout')


@pytest.mark.parametrize('payload', ['not json at all', '{"verdict": "LGTM"}',
                                     '{"verdict": "APPROVE", "findings": {}}'])
def test_invalid_reviewer_output_fails_the_stage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider(code=payload)

    result = run(repo, provider)

    assert result.decision is Decision.BLOCKED
    assert result.stages[0].status is StageStatus.FAILED
    assert result.stages[0].error.startswith('invalid_response')


def test_finding_outside_the_change_set_fails_the_stage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider(code=document(finding(file_path='tests/test_app.py')))

    result = run(repo, provider)

    assert result.decision is Decision.BLOCKED
    assert 'not part of the reviewed change set' in result.stages[0].error


# ------------------------------------------------------------------- staleness
def test_source_moving_mid_review_marks_the_run_stale(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)

    class MovingProvider(FakeProvider):
        moved = False

        def complete(self, system_prompt, user_content, *, timeout_s):
            completion = super().complete(system_prompt, user_content,
                                          timeout_s=timeout_s)
            if self.moved:  # only the first pass moves the ref
                return completion
            self.moved = True
            (repo.root / 'app.py').write_text(
                'def add(a, b):\n    return a + b\n\n\ndef drift():\n'
                '    return 2\n',
                encoding='utf-8')
            git(repo.root, 'commit', '-qam', 'moved mid review')
            return completion

    # Refs, not SHAs: only a ref can move, and a moved ref is what makes the
    # review no longer cover what the caller is about to ship.
    result = run_review(repo.root, base='HEAD~1', head='HEAD',
                        provider=MovingProvider())

    assert result.decision is Decision.STALE
    assert result.status is StageStatus.STALE
    assert result.approved is False
    assert result.gate['gate_state'] == 'NOT_EXECUTED'
    assert 'moved during the review' in result.reasons[0]


# -------------------------------------------------------------------- deadlines
def test_expired_deadline_never_launches_a_stage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider()

    result = run(repo, provider, deadline_seconds=0.000001)

    assert result.decision is Decision.BLOCKED
    assert provider.calls == []
    assert result.stages[0].status is StageStatus.NOT_EXECUTED
    assert result.stages[0].error == 'deadline_exceeded'


def test_deadline_caps_the_provider_budget(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch
                                           ) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    provider = FakeProvider()

    run(repo, provider, deadline_seconds=30)

    assert all(call['timeout_s'] <= 30 for call in provider.calls)
    assert provider.calls[0]['timeout_s'] >= 1


# --------------------------------------------------------------- independence
def test_policy_floor_blocks_same_provider_reviews(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)

    result = run(repo, FakeProvider(),
                 policy=distinct_provider_policy())

    assert result.decision is Decision.BLOCKED
    assert 'below the policy floor' in result.reasons[0]
    assert 'distinct_provider' in result.reasons[0]


# ------------------------------------------------------------------- artifacts
def test_unknown_run_id_is_not_found(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(artifacts.ReviewNotFound):
        artifacts.load_run(repo.root, 'rv-does-not-exist')


def test_truncated_artifact_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    result = run(repo, FakeProvider())
    path = Path(result.artifacts) / 'run.json'
    payload = json.loads(path.read_text())
    payload.pop('gate')
    path.write_text(json.dumps(payload))

    with pytest.raises(artifacts.ArtifactError, match='incomplete'):
        artifacts.load_run(repo.root, result.run_id)


def test_run_journal_records_the_lifecycle(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)

    run(repo, FakeProvider())

    events = journal_events(repo)
    assert 'review_started' in events
    assert 'review_decided' in events
    assert events.count('review_stage_completed') >= 3


def test_review_id_is_never_reused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)

    first = run(repo, FakeProvider())
    second = run(repo, FakeProvider())

    assert first.run_id != second.run_id
    assert first.artifacts != second.artifacts
