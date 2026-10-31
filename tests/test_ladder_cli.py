"""`asha review` / `asha review-status`: exit codes, JSON, refusal handling."""

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
    make_repo,
    stub_gate,
)

from asha import cli
from asha.ladder import orchestrator


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ASHA_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('ASHA_LLM_API_KEY', raising=False)


def use_provider(monkeypatch: pytest.MonkeyPatch,
                 provider: FakeProvider) -> None:
    monkeypatch.setattr(orchestrator, 'resolve_provider', lambda: provider)


def review_argv(repo, *extra: str) -> list[str]:
    return ['review', '--root', str(repo.root), '--base', repo.base,
            '--head', repo.head, *extra]


def test_review_approved_exits_zero(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    use_provider(monkeypatch, FakeProvider())

    code = cli.main(review_argv(repo, '--json'))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['decision'] == 'APPROVED'
    assert payload['approved'] is True
    assert payload['artifact_schema'] == 1


def test_review_changes_requested_exits_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    use_provider(monkeypatch, FakeProvider(
        code=document(finding(), verdict='REQUEST_CHANGES'),
        senior=document(finding(), verdict='REQUEST_CHANGES')))

    assert cli.main(review_argv(repo)) == 1


def test_review_without_provider_exits_blocked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path)

    code = cli.main(review_argv(repo, '--json'))

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['decision'] == 'BLOCKED'
    assert 'ASHA_LLM_API_KEY' in payload['reasons'][0]


def test_review_reports_stage_lines_without_json(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    use_provider(monkeypatch, FakeProvider())

    cli.main(review_argv(repo))

    out = capsys.readouterr().out
    assert 'CODE_REVIEW' in out and 'SENIOR_REVIEW' in out
    assert 'SEMANTIC_REVIEW' in out and 'SHIP_GATE' in out
    assert 'decision' not in out.lower() or 'APPROVED' in out


def test_review_rejects_unknown_policy_as_usage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    assert cli.main(review_argv(repo, '--policy', 'whatever')) == 4


def test_review_rejects_an_invalid_ref(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    argv = ['review', '--root', str(repo.root), '--base', 'no-such-ref',
            '--head', repo.head]
    assert cli.main(argv) == 3


def test_review_status_reads_a_previous_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    use_provider(monkeypatch, FakeProvider())
    cli.main(review_argv(repo, '--json'))
    run_id = json.loads(capsys.readouterr().out)['run_id']

    code = cli.main(['review-status', '--root', str(repo.root),
                     '--run-id', run_id, '--json'])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['run_id'] == run_id
    assert payload['decision'] == 'APPROVED'


def test_review_status_unknown_run_exits_three(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    code = cli.main(['review-status', '--root', str(repo.root),
                     '--run-id', 'rv-nope'])
    assert code == 3


def test_review_status_requires_a_run_id(tmp_path: Path) -> None:
    assert cli.main(['review-status', '--root', str(tmp_path)]) == 4


def test_review_rejects_non_numeric_deadline(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert cli.main(['review', '--root', str(repo.root),
                     '--deadline-seconds', 'soon']) == 4
