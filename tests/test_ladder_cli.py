"""CLI entry points: start, context, submit, finalize, status, exit codes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_support import make_repo, stub_gate  # type: ignore[import-not-found]

from asha import cli


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ASHA_STATE_DIR', str(tmp_path / 'state'))
    for name in ('ASHA_LLM_API_KEY', 'ASHA_LLM_BASE_URL', 'ASHA_LLM_MODEL'):
        monkeypatch.delenv(name, raising=False)


def run_cli(*argv: str) -> tuple[int, str]:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


def start(repo) -> tuple[int, dict]:  # type: ignore[no-untyped-def]
    code, text = run_cli('review', '--root', str(repo.root), '--base',
                         repo.base, '--head', repo.head, '--json')
    return code, json.loads(text)


def write_submission(tmp_path: Path, packet: dict, *, verdict: str = 'APPROVE',
                     findings: list[dict] | None = None) -> str:
    path = tmp_path / f'{packet["stage"]}.json'
    path.write_text(json.dumps({
        'stage': packet['stage'], 'stage_id': packet['stage_id'],
        'verdict': verdict, 'summary': 'cli submission',
        'findings': findings or [],
        'reviewer_metadata': {'host_client': 'cli-test',
                              'isolation': 'HOST_ATTESTED_ISOLATED_CONTEXT',
                              'attestation_source': 'separate CLI process'},
    }), encoding='utf-8')
    return str(path)


def test_review_start_is_pending_not_approved(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    code, payload = start(repo)

    assert code == 5
    assert payload['run']['state'] == 'AWAITING_HOST_REVIEW'
    assert payload['packet']['stage'] == 'CODE_REVIEW'
    assert 'ASHA_LLM' not in json.dumps(payload)


def test_review_status_reads_the_persisted_run(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _, payload = start(repo)
    run_id = payload['run']['run_id']

    code, text = run_cli('review-status', '--root', str(repo.root),
                         '--run-id', run_id, '--json')

    body = json.loads(text)
    assert code == 0
    assert body['run_id'] == run_id
    assert body['state'] == 'AWAITING_HOST_REVIEW'


def test_submit_and_finalize_through_the_cli(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    _, payload = start(repo)
    run_id = payload['run']['run_id']
    packet = payload['packet']

    for _ in range(3):
        file_path = write_submission(tmp_path, packet)
        code, text = run_cli('review-submit', '--root', str(repo.root),
                             '--run-id', run_id, '--file', file_path,
                             '--json')
        assert code in (0, 5), text
        if code == 0:
            break
        code, text = run_cli('review-next', '--root', str(repo.root),
                             '--run-id', run_id, '--json')
        packet = json.loads(text)

    code, text = run_cli('review-finalize', '--root', str(repo.root),
                         '--run-id', run_id, '--json')

    assert code == 0
    body = json.loads(text)
    assert body['decision'] == 'APPROVED'
    assert body['gate']['authorized_to_ship'] is True


def test_review_context_prints_a_bounded_chunk(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _, payload = start(repo)

    code, text = run_cli('review-context', '--root', str(repo.root),
                         '--run-id', payload['run']['run_id'], '--json',
                         '--max-bytes', '80')

    chunk = json.loads(text)
    assert code == 0
    assert chunk['kind'] == 'diff'
    assert chunk['total_bytes'] > 80


def test_unknown_policy_is_a_usage_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    code, _ = run_cli('review', '--root', str(repo.root), '--base', repo.base,
                      '--head', repo.head, '--policy', 'distinct-provider')

    assert code == 4


def test_unknown_run_is_an_execution_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    code, _ = run_cli('review-status', '--root', str(repo.root),
                      '--run-id', 'rv-missing')

    assert code == 3
