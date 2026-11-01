"""A review authorizes the reviewed content, not whatever is checked out.

Phase 10 (SHA-pinned reproduction): a review pinned to a commit id went
``FINALIZED / APPROVED`` with ``authorized_to_ship: true`` after ``HEAD`` had
moved to different content, because the only identities compared were the
recorded commit ids and the refs the caller named -- and a commit id never
moves. The gate then audited the *current* worktree.

The fix compares tree to tree: the tree of the reviewed commit against the
tree of the commit that is actually checked out. Commits with identical
content stay interchangeable, so reviewing a commit that is not ``HEAD`` keeps
working when the content is the same.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_support import (  # type: ignore[import-not-found]
    finalize,
    git,
    make_repo,
    next_stage,
    start_review_via_mcp,
    status,
    stub_gate,
    submit_via_mcp,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ASHA_STATE_DIR', str(tmp_path / 'state'))
    for name in ('ASHA_LLM_API_KEY', 'ASHA_LLM_BASE_URL', 'ASHA_LLM_MODEL',
                 'ASHA_SEMANTIC_REVIEW'):
        monkeypatch.delenv(name, raising=False)


def approve_all(repo, run_id: str, packet: dict) -> None:  # type: ignore[no-untyped-def]
    submit_via_mcp(repo, run_id, packet)
    submit_via_mcp(repo, run_id, next_stage(repo, run_id))
    submit_via_mcp(repo, run_id, next_stage(repo, run_id))


def commit_new_content(repo) -> str:  # type: ignore[no-untyped-def]
    (repo.root / 'app.py').write_text('def add(a, b):\n    return a + b + 1\n',
                                      encoding='utf-8')
    git(repo.root, 'add', '-A')
    git(repo.root, 'commit', '-qm', 'content the review never saw')
    return git(repo.root, 'rev-parse', 'HEAD').strip()


def test_sha_pinned_review_goes_stale_when_head_moves(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reproduction: SHA-pinned review, then HEAD moves."""
    repo = make_repo(tmp_path)
    stub = stub_gate(monkeypatch)
    started = start_review_via_mcp(repo, head=repo.head)
    run_id = started['run']['run_id']
    approve_all(repo, run_id, started['packet'])

    moved = commit_new_content(repo)
    assert moved != repo.head

    finished = finalize(repo, run_id)

    assert finished['state'] == 'STALE'
    assert finished['decision'] == 'STALE'
    assert finished['approved'] is False
    assert finished['gate']['authorized_to_ship'] is False
    assert finished['gate']['gate_state'] == 'NOT_EXECUTED'
    assert 'checked-out tree is not the reviewed tree' in \
        finished['blocking_reason']
    assert stub.calls == []                  # the gate never ran
    assert status(repo, run_id)['state'] == 'STALE'


def test_sha_pinned_review_still_authorizes_unchanged_head(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: nothing moved, so the gate still runs."""
    repo = make_repo(tmp_path)
    stub = stub_gate(monkeypatch)
    started = start_review_via_mcp(repo, head=repo.head)
    run_id = started['run']['run_id']
    approve_all(repo, run_id, started['packet'])

    finished = finalize(repo, run_id)

    assert finished['decision'] == 'APPROVED'
    assert finished['gate']['authorized_to_ship'] is True
    assert len(stub.calls) == 1


def test_same_tree_at_a_new_commit_is_not_stale(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Content identity, not commit identity: an empty commit is harmless."""
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    started = start_review_via_mcp(repo, head=repo.head)
    run_id = started['run']['run_id']
    approve_all(repo, run_id, started['packet'])

    git(repo.root, 'commit', '-q', '--allow-empty', '-m', 'same content')
    same_tree = git(repo.root, 'rev-parse', 'HEAD').strip()
    assert same_tree != repo.head
    assert git(repo.root, 'rev-parse', 'HEAD^{tree}') == \
        git(repo.root, 'rev-parse', f'{repo.head}^{{tree}}')

    finished = finalize(repo, run_id)

    assert finished['decision'] == 'APPROVED'
    assert finished['gate']['authorized_to_ship'] is True


def test_ref_pinned_review_still_reports_the_ref_move(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The existing ref check keeps its own, more specific message."""
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    started = start_review_via_mcp(repo, head='HEAD')
    run_id = started['run']['run_id']
    approve_all(repo, run_id, started['packet'])

    commit_new_content(repo)
    finished = finalize(repo, run_id)

    assert finished['state'] == 'STALE'
    assert 'moved during the review' in finished['blocking_reason']
