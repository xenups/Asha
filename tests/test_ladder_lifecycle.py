"""The host-driven MCP lifecycle, end to end through real JSON-RPC calls.

This harness *is* the MCP client. It performs the three review passes as a
host LLM would (the findings are fixtures, not model output) and drives
``tools/call`` through the real ``handle_message`` dispatch. Nothing here
proves a live external LLM reviewed anything: it proves the workflow,
validation, persistence and gate wiring.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_support import (  # type: ignore[import-not-found]
    finalize,
    finding,
    git,
    make_repo,
    next_stage,
    start_review_via_mcp,
    status,
    stub_gate,
    submit_via_mcp,
    tool,
    tool_error,
)

from asha.ladder import artifacts


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ASHA_STATE_DIR', str(tmp_path / 'state'))
    for name in ('ASHA_LLM_API_KEY', 'ASHA_LLM_BASE_URL', 'ASHA_LLM_MODEL',
                 'ASHA_SEMANTIC_REVIEW'):
        monkeypatch.delenv(name, raising=False)


def approve_all(repo, run_id: str, packet: dict, *,  # type: ignore[no-untyped-def]
                findings: list[dict] | None = None) -> dict:
    submit_via_mcp(repo, run_id, packet, findings=findings or [])
    senior = next_stage(repo, run_id)
    submit_via_mcp(repo, run_id, senior)
    semantic = next_stage(repo, run_id)
    submit_via_mcp(repo, run_id, semantic)
    return semantic


# ------------------------------------------------------------------- start
def test_start_creates_a_host_awaiting_run_without_any_provider(
        tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    started = start_review_via_mcp(repo)

    run = started['run']
    packet = started['packet']
    assert run['state'] == 'AWAITING_HOST_REVIEW'
    assert run['decision'] is None
    assert run['execution_mode'] == 'HOST_DRIVEN_MULTIPASS'
    assert packet['stage'] == 'CODE_REVIEW'
    assert packet['stage_id'] == f'{run["run_id"]}:CODE_REVIEW'
    assert packet['target']['head_sha'] == repo.head
    assert packet['target']['diff_sha256'] == run['target']['diff_sha256']
    assert 'prior_findings' not in packet
    assert 'CODE_REVIEW' in packet['required_next_action']
    # The old architecture failed here; a host-driven run never mentions a key.
    assert 'ASHA_LLM' not in json.dumps(started)


def test_start_is_refused_for_an_unknown_ref(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    message = tool_error('asha_review', root=str(repo.root),
                         base='origin/nope', head=repo.head)

    assert 'cannot resolve ref' in message


# ----------------------------------------------------------------- context
def test_context_pages_the_exact_reviewed_diff(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']
    stage_id = started['packet']['stage_id']
    expected = git(repo.root, 'diff', f'{repo.base}..{repo.head}', '--')

    pieces: list[str] = []
    cursor = 0
    while True:
        chunk = tool('asha_review_context', root=str(repo.root),
                     review_run_id=run_id, stage_id=stage_id, cursor=cursor,
                     max_bytes=50)
        pieces.append(chunk['content'])
        assert chunk['head_sha'] == repo.head
        if chunk['complete']:
            break
        cursor = chunk['next_cursor']

    assert ''.join(pieces) == expected


def test_context_serves_the_pinned_commit_not_the_worktree(
        tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']
    (repo.root / 'app.py').write_text('def add(a, b):\n    return 42\n',
                                      encoding='utf-8')

    chunk = tool('asha_review_context', root=str(repo.root),
                 review_run_id=run_id, file_path='app.py')

    assert 'return a - b' in chunk['content']
    assert 'return 42' not in chunk['content']
    git(repo.root, 'checkout', '--', 'app.py')


def test_context_refuses_secrets_and_traversal(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']

    for bad in ('.env', '../etc/passwd', '/etc/passwd'):
        message = tool_error('asha_review_context', root=str(repo.root),
                             review_run_id=run_id, file_path=bad)
        assert 'refusing' in message or 'not a file' in message


# ------------------------------------------------------- stage progression
def test_the_full_sequence_approves_persists_and_reads_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    gate = stub_gate(monkeypatch)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']

    code = submit_via_mcp(repo, run_id, started['packet'], findings=[finding()])
    assert code['status'] == 'VALIDATED'
    assert code['state'] == 'AWAITING_HOST_REVIEW'

    senior_packet = next_stage(repo, run_id)
    assert senior_packet['stage'] == 'SENIOR_REVIEW'
    assert 'prior_findings' not in senior_packet
    senior = submit_via_mcp(repo, run_id, senior_packet)
    assert senior['state'] == 'AWAITING_HOST_REVIEW'

    semantic_packet = next_stage(repo, run_id)
    assert semantic_packet['stage'] == 'SEMANTIC_REVIEW'
    priors = semantic_packet['prior_findings']
    assert [item['finding_id'] for item in priors] == ['CR-1']
    semantic = submit_via_mcp(repo, run_id, semantic_packet)
    assert semantic['state'] == 'AWAITING_FINALIZE'

    final = finalize(repo, run_id)

    assert final['decision'] == 'CHANGES_REQUESTED'
    assert final['state'] == 'FINALIZED'
    assert [stage['stage'] for stage in final['stages']] == [
        'CODE_REVIEW', 'SENIOR_REVIEW', 'SEMANTIC_REVIEW', 'SHIP_GATE']
    assert len(gate.calls) == 1
    assert [item['severity'] for item in gate.calls[0]['findings']] == ['HIGH']

    persisted = status(repo, run_id)
    assert persisted['decision'] == final['decision']
    assert persisted['target']['diff_sha256'] == final['target']['diff_sha256']
    on_disk = artifacts.load_run(repo.root, run_id)
    assert on_disk.as_dict() == persisted
    assert Path(final['artifacts'], 'run.json').is_file()
    assert Path(final['artifacts'], 'stages', 'SEMANTIC_REVIEW.json').is_file()


def test_a_clean_review_approves(tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']

    approve_all(repo, run_id, started['packet'])
    final = finalize(repo, run_id)

    assert final['decision'] == 'APPROVED'
    assert final['gate']['authorized_to_ship'] is True
    assert final['independence'] == 'UNKNOWN'
    assert final['semantic_validation']['problems'] == []


def test_out_of_order_submission_is_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']
    senior_stage_id = f'{run_id}:SENIOR_REVIEW'

    message = tool_error(
        'asha_review_submit_stage', root=str(repo.root),
        review_run_id=run_id, stage_id=senior_stage_id, stage='SENIOR_REVIEW',
        verdict='APPROVE', summary='skipped ahead', findings=[])

    assert 'out of order' in message


def test_unknown_stage_id_is_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']

    message = tool_error(
        'asha_review_submit_stage', root=str(repo.root),
        review_run_id=run_id, stage_id='rv-forged:CODE_REVIEW',
        stage='CODE_REVIEW', verdict='APPROVE', summary='forged id',
        findings=[])

    assert 'unknown stage id' in message


def test_identical_resubmission_is_idempotent_and_conflicts_are_refused(
        tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']
    packet = started['packet']

    first = submit_via_mcp(repo, run_id, packet, summary='same text')
    again = submit_via_mcp(repo, run_id, packet, summary='same text')
    assert again['idempotent'] is True
    assert again['submission_sha256'] == first['submission_sha256']

    message = tool_error(
        'asha_review_submit_stage', root=str(repo.root),
        review_run_id=run_id, stage_id=packet['stage_id'],
        stage=packet['stage'], verdict='REQUEST_CHANGES',
        summary='different result', findings=[])
    assert 'cannot overwrite' in message


# ---------------------------------------------------------------- refusals
def test_finalize_without_all_stages_blocks(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']
    submit_via_mcp(repo, run_id, started['packet'])

    final = finalize(repo, run_id)

    assert final['decision'] == 'BLOCKED'
    assert final['state'] == 'BLOCKED'
    assert 'SENIOR_REVIEW' in final['reasons'][0]
    assert final['gate']['authorized_to_ship'] is False


def test_a_moved_ref_makes_the_run_stale_and_skips_the_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    gate = stub_gate(monkeypatch)
    started = start_review_via_mcp(repo, base=repo.base, head='main')
    run_id = started['run']['run_id']
    approve_all(repo, run_id, started['packet'])

    (repo.root / 'later.py').write_text('x = 1\n', encoding='utf-8')
    git(repo.root, 'add', '-A')
    git(repo.root, 'commit', '-qm', 'moved after review')
    final = finalize(repo, run_id)

    assert final['decision'] == 'STALE'
    assert gate.calls == []


def test_an_expired_review_fails_closed_and_never_reaches_the_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    gate = stub_gate(monkeypatch)
    started = start_review_via_mcp(repo, deadline_seconds=60)
    run_id = started['run']['run_id']
    run = artifacts.load_run(repo.root, run_id)
    artifacts.save_run(repo.root, replace(
        run, expires_at='2000-01-01T00:00:00+00:00'))

    message = tool_error(
        'asha_review_submit_stage', root=str(repo.root),
        review_run_id=run_id, stage_id=started['packet']['stage_id'],
        stage='CODE_REVIEW', verdict='APPROVE', summary='late', findings=[])
    expired_state = status(repo, run_id)

    assert 'deadline_exceeded' in message
    assert expired_state['decision'] == 'FAILED'
    assert gate.calls == []


def test_a_gate_refusal_cannot_become_an_approval(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch, authorized=False)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']
    approve_all(repo, run_id, started['packet'])

    final = finalize(repo, run_id)

    assert final['decision'] == 'BLOCKED'
    assert final['gate']['authorized_to_ship'] is False
    assert final['gate']['gate_state'] == 'ERROR'


def test_a_blocking_finding_requests_changes(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']

    submit_via_mcp(repo, run_id, started['packet'], findings=[finding()])
    senior = next_stage(repo, run_id)
    submit_via_mcp(repo, run_id, senior, verdict='APPROVE')
    semantic = next_stage(repo, run_id)
    submit_via_mcp(repo, run_id, semantic, verdict='APPROVE')
    final = finalize(repo, run_id)

    assert final['decision'] == 'CHANGES_REQUESTED'
    assert 'CR-1 HIGH' in final['reasons'][0]
    assert 'semantic review approved' in final[
        'semantic_validation']['problems'][0]


@pytest.mark.parametrize('overrides, needle', [
    ({'severity': 'SEVERE'}, 'invalid finding'),
    ({'file_path': '../escape.py'}, 'invalid finding'),
    ({'line_end': 9999}, 'exceed the'),
    ({'file_path': 'missing.py'}, 'is not a file'),
])
def test_ungrounded_findings_are_rejected(tmp_path: Path,
                                         overrides: dict,
                                         needle: str) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']

    message = tool_error(
        'asha_review_submit_stage', root=str(repo.root),
        review_run_id=run_id, stage_id=started['packet']['stage_id'],
        stage='CODE_REVIEW', verdict='APPROVE', summary='bad finding',
        findings=[finding(**overrides)])

    assert needle in message


def test_tampered_state_is_an_error_not_an_approval(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']
    run_file = Path(started['run']['artifacts'], 'run.json')
    document = json.loads(run_file.read_text(encoding='utf-8'))
    document['state'] = 'FINALIZED'
    document['decision'] = 'APPROVED'
    document.pop('stages')
    run_file.write_text(json.dumps(document), encoding='utf-8')

    message = tool_error('asha_review_status', root=str(repo.root),
                         run_id=run_id)

    assert 'incomplete' in message

@pytest.mark.parametrize('tool_name, arguments', [
    ('asha_review', {'bogus': 1}),
    ('asha_review_context', {'review_run_id': 'rv-x', 'bogus': 1}),
    ('asha_review_submit_stage', {'review_run_id': 'rv-x', 'bogus': 1}),
    ('asha_review_next_stage', {'review_run_id': 'rv-x', 'bogus': 1}),
    ('asha_review_finalize', {'review_run_id': 'rv-x', 'bogus': 1}),
    ('asha_review_status', {'run_id': 'rv-x', 'bogus': 1}),
])
def test_unknown_arguments_are_refused(tmp_path: Path, tool_name: str,
                                       arguments: dict) -> None:
    repo = make_repo(tmp_path)

    message = tool_error(tool_name, root=str(repo.root), **arguments)

    assert 'unknown argument(s): bogus' in message


def test_a_review_tool_never_accepts_a_command(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    message = tool_error('asha_review', root=str(repo.root),
                         base=repo.base, head=repo.head,
                         shell='rm -rf /')

    assert 'unknown argument(s): shell' in message

def test_repeated_finalize_reads_the_recorded_decision(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    started = start_review_via_mcp(repo)
    run_id = started['run']['run_id']
    submit_via_mcp(repo, run_id, started['packet'])

    first = finalize(repo, run_id)
    second = finalize(repo, run_id)

    assert first['decision'] == second['decision'] == 'BLOCKED'
    assert second['reasons'] == first['reasons']
    assert second['gate'] == first['gate']
