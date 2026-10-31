"""Unit contracts: findings, policy, target, context, aggregation, no provider."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_support import git, make_repo  # type: ignore[import-not-found]

from asha.ladder import aggregation, policy
from asha.ladder.models import (
    INDEPENDENCE_RANK,
    Confidence,
    Decision,
    Finding,
    FindingError,
    Independence,
    ReviewerInfo,
    ReviewTarget,
    Severity,
    StageResult,
    StageStatus,
    StageType,
    Verdict,
)
from asha.ladder.target import (
    DEFAULT_CONTEXT_BYTES,
    MAX_CONTEXT_BYTES,
    TargetError,
    diff_chunk,
    file_chunk,
    is_secret_path,
    resolve_target,
)


def stage(stage_type: StageType = StageType.CODE_REVIEW, *,
          status: StageStatus = StageStatus.VALIDATED,
          verdict: Verdict = Verdict.APPROVE,
          reviewer: ReviewerInfo | None = None,
          findings: tuple[Finding, ...] = (),
          error: str = '') -> StageResult:
    return StageResult(stage=stage_type, status=status, verdict=verdict,
                       reviewer=reviewer or ReviewerInfo(), findings=findings,
                       error=error)


def target_for(repo) -> ReviewTarget:  # type: ignore[no-untyped-def]
    return resolve_target(repo.root, base=repo.base, head=repo.head,
                          policy=policy.default_policy(), run_id='rv-test',
                          started_at='2026-01-01T00:00:00+00:00')


def finding(**overrides: object) -> Finding:
    fields: dict[str, object] = {
        'finding_id': 'CR-1', 'stage': StageType.CODE_REVIEW,
        'category': 'correctness', 'severity': Severity.HIGH,
        'confidence': Confidence.CONFIRMED, 'title': 'add() subtracts',
        'description': 'add returns a - b', 'file_path': 'app.py',
        'line_start': 2, 'line_end': 2, 'evidence': 'test_add asserts 3',
        'impact': 'wrong sums', 'recommended_action': 'return a + b',
        'blocking': True,
    }
    fields.update(overrides)
    return Finding(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------- findings
def test_finding_rejects_unknown_severity() -> None:
    with pytest.raises(FindingError, match='severity'):
        finding(severity='SEVERE')


def test_finding_rejects_traversal_and_absolute_paths() -> None:
    for path in ('../escape.py', '/etc/passwd', 'a\\b.py'):
        with pytest.raises(FindingError):
            finding(file_path=path)


def test_finding_rejects_an_impossible_line_range() -> None:
    with pytest.raises(FindingError, match='precedes'):
        finding(line_start=9, line_end=3)


# ----------------------------------------------------------------- policy
def test_default_policy_is_host_multipass_without_an_independence_claim() -> None:
    chosen = policy.default_policy()

    assert chosen.policy_id == 'ladder/host-multipass'
    assert chosen.required_stages == (
        StageType.CODE_REVIEW, StageType.SENIOR_REVIEW,
        StageType.SEMANTIC_REVIEW, StageType.SHIP_GATE)
    assert chosen.required_independence is Independence.UNKNOWN
    assert policy.resolve_policy('default').policy_id == 'ladder/host-multipass'


def test_strict_policy_requires_an_attested_isolated_context() -> None:
    chosen = policy.resolve_policy('host-isolated')

    assert chosen.required_independence is (
        Independence.HOST_ATTESTED_ISOLATED_CONTEXT)
    assert chosen.content_hash() != policy.default_policy().content_hash()


def test_unknown_policy_fails_closed() -> None:
    with pytest.raises(ValueError, match='unknown review policy'):
        policy.resolve_policy('distinct-provider')


# ---------------------------------------------------------- independence
def test_two_roles_on_one_client_are_not_independent() -> None:
    left = stage(reviewer=ReviewerInfo(
        host_client='hermes', isolation=Independence.DISTINCT_HOST_CLIENT))
    right = stage(StageType.SENIOR_REVIEW, reviewer=ReviewerInfo(
        host_client='hermes', isolation=Independence.DISTINCT_HOST_CLIENT))

    assert aggregation.classify_independence(left, right) is (
        Independence.SAME_HOST_SESSION)


def test_two_clients_each_claiming_distinct_are_classified_distinct() -> None:
    left = stage(reviewer=ReviewerInfo(
        host_client='hermes', isolation=Independence.DISTINCT_HOST_CLIENT))
    right = stage(StageType.SENIOR_REVIEW, reviewer=ReviewerInfo(
        host_client='other-host', isolation=Independence.DISTINCT_HOST_CLIENT))

    assert aggregation.classify_independence(left, right) is (
        Independence.DISTINCT_HOST_CLIENT)


def test_missing_metadata_stays_unknown_and_is_never_upgraded() -> None:
    left = stage(reviewer=ReviewerInfo())
    right = stage(StageType.SENIOR_REVIEW, reviewer=ReviewerInfo(
        isolation=Independence.HOST_ATTESTED_ISOLATED_CONTEXT))

    assert aggregation.classify_independence(left, right) is Independence.UNKNOWN
    assert not aggregation.meets_independence(
        Independence.UNKNOWN, Independence.HOST_ATTESTED_ISOLATED_CONTEXT)


def test_rank_order_is_explicit() -> None:
    ordered = sorted(INDEPENDENCE_RANK, key=lambda item: INDEPENDENCE_RANK[item])

    assert ordered[0] is Independence.UNKNOWN
    assert ordered[-1] is Independence.DISTINCT_HOST_CLIENT


# -------------------------------------------------------------- decision
def _decide(stages, findings=(), independence=Independence.UNKNOWN,  # type: ignore[no-untyped-def]
            stale_reason: str = '') -> tuple[Decision, tuple[str, ...]]:
    return aggregation.decide(
        policy=policy.default_policy(), stages=tuple(stages),
        findings=tuple(findings), independence=independence,
        target=None, stale_reason=stale_reason)  # type: ignore[arg-type]


def test_a_missing_stage_blocks() -> None:
    decision, reasons = _decide([stage()])

    assert decision is Decision.BLOCKED
    assert any('SENIOR_REVIEW' in reason for reason in reasons)


def test_an_unvalidated_stage_blocks_with_its_status() -> None:
    decision, reasons = _decide([
        stage(), stage(StageType.SENIOR_REVIEW, status=StageStatus.FAILED,
                       error='invalid finding'),
        stage(StageType.SEMANTIC_REVIEW)])

    assert decision is Decision.BLOCKED
    assert 'FAILED: invalid finding' in reasons[0]


def test_blocking_severity_requests_changes_even_if_the_host_says_approve() -> None:
    decision, reasons = _decide(
        [stage(), stage(StageType.SENIOR_REVIEW),
         stage(StageType.SEMANTIC_REVIEW)],
        findings=[finding()])

    assert decision is Decision.CHANGES_REQUESTED
    assert 'CR-1 HIGH' in reasons[0]


def test_medium_only_findings_do_not_block() -> None:
    decision, _ = _decide(
        [stage(), stage(StageType.SENIOR_REVIEW),
         stage(StageType.SEMANTIC_REVIEW)],
        findings=[finding(severity=Severity.MEDIUM, blocking=True)])

    assert decision is Decision.APPROVED


def test_an_inconclusive_reviewer_blocks() -> None:
    decision, reasons = _decide(
        [stage(verdict=Verdict.INCONCLUSIVE), stage(StageType.SENIOR_REVIEW),
         stage(StageType.SEMANTIC_REVIEW)])

    assert decision is Decision.BLOCKED
    assert 'INCONCLUSIVE' in reasons[0]


def test_reviewer_disagreement_is_not_silently_resolved() -> None:
    decision, reasons = _decide(
        [stage(), stage(StageType.SENIOR_REVIEW,
                        verdict=Verdict.REQUEST_CHANGES),
         stage(StageType.SEMANTIC_REVIEW)])

    assert decision is Decision.CHANGES_REQUESTED
    assert 'disagreed' in reasons[0]


def test_stale_target_short_circuits_every_other_check() -> None:
    decision, reasons = _decide([stage()], stale_reason='ref moved')

    assert decision is Decision.STALE
    assert reasons == ('ref moved',)


# ---------------------------------------------------------------- target
def test_target_binds_commits_not_the_working_tree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    target = target_for(repo)
    (repo.root / 'app.py').write_text('def add(a, b):\n    return 0\n',
                                      encoding='utf-8')

    chunk = file_chunk(repo.root, target, 'app.py')

    assert target.head_sha == repo.head
    assert 'return a - b' in str(chunk['content'])
    assert 'return 0' not in str(chunk['content'])
    git(repo.root, 'checkout', '--', 'app.py')


def test_target_refuses_a_dirty_tracked_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo.root / 'app.py').write_text('def add(a, b):\n    return 9\n',
                                      encoding='utf-8')

    with pytest.raises(TargetError, match='uncommitted changes'):
        target_for(repo)


def test_diff_chunk_pages_cover_the_whole_diff(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    target = target_for(repo)
    expected = git(repo.root, 'diff', f'{repo.base}..{repo.head}', '--')

    pieces: list[str] = []
    cursor = 0
    while True:
        chunk = diff_chunk(repo.root, target, cursor=cursor, max_bytes=40)
        pieces.append(str(chunk['content']))
        if chunk['complete']:
            break
        next_cursor = chunk['next_cursor']
        assert isinstance(next_cursor, int)
        cursor = next_cursor

    assert ''.join(pieces) == expected
    assert chunk['diff_sha256'] == target.diff_sha256


def test_file_chunk_reports_line_ranges_and_refuses_escapes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    target = target_for(repo)

    chunk = file_chunk(repo.root, target, 'app.py', max_bytes=10)

    assert chunk['line_start'] == 1
    assert chunk['in_changed_files'] is True
    for bad in ('../etc/passwd', '/etc/passwd', '.env', 'keys/id_rsa'):
        with pytest.raises(TargetError):
            file_chunk(repo.root, target, bad)


def test_secret_shaped_paths_are_recognised() -> None:
    for path in ('.env', '.env.local', 'deploy/id_rsa', 'certs/server.pem',
                 'secrets.json', 'aws_credentials'):
        assert is_secret_path(path), path
    assert not is_secret_path('app.py')


def test_context_bounds_are_sane() -> None:
    assert 0 < DEFAULT_CONTEXT_BYTES <= MAX_CONTEXT_BYTES


# ------------------------------------------------------- no provider path
def test_ladder_never_reads_provider_credentials_or_calls_a_model() -> None:
    """The review path is the host's: no key, no endpoint, no transport."""
    package = Path(__file__).resolve().parents[1] / 'asha' / 'ladder'
    sources = {path.name: path.read_text(encoding='utf-8')
               for path in package.glob('*.py')}

    for name, text in sources.items():
        assert 'ASHA_LLM' not in text, name
        assert 'live_reviewer' not in text, name
        assert 'urllib' not in text and 'requests' not in text, name
        assert 'http://' not in text and 'https://' not in text, name
    assert not (package / 'providers.py').exists()
    assert not (package / 'stages.py').exists()
