"""Stage 5 contract: a timeout is reconstructable from the run journal.

The facts live inside the existing events (execution_started / sealed /
execution_failed) -- no timeout-specific event type exists. A timed-out worker
never gets a `sealed` event, and a run whose termination could not be verified
records that, so a reader can tell "killed and confirmed" from "kill requested".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from asha import telemetry
from asha.common import paths as common_paths
from asha.governance import dag


def _git(repo: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 'a@example.invalid')
    _git(repo, 'config', 'user.name', 'A')
    (repo / 'base.txt').write_text('base\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'base')
    return repo


def _worker(wid: str, cmd: str, *, timeout: float | None = None
            ) -> dict[str, Any]:
    worker: dict[str, Any] = {'id': wid, 'deps': [], 'declared_scope': ['out.txt'],
              'reads': [], 'writes': ['out.txt'],
              'cmd': ['sh', '-c', cmd]}
    if timeout is not None:
        worker['timeout'] = timeout
    return worker


def _events(repo: Path, task_id: str) -> list[dict]:
    path = common_paths.get_journal_dir(repo) / (task_id + '.jsonl')
    return [json.loads(line) for line in
            path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _run(repo: Path, workers: list[dict], task_id: str) -> list[dict]:
    journal = telemetry.EventJournalWriter(repo, task_id)
    try:
        dag.run_workers_dag(repo, workers, task_id=task_id, journal=journal)
    finally:
        journal.close()
    return _events(repo, task_id)


def test_timeout_is_reconstructable_from_the_journal(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    events = _run(repo, [_worker('w1', 'sleep 30', timeout=1)], 'task-to')
    terminal = [e for e in events
                if e['event_type'] in ('sealed', 'execution_failed')
                and e['payload'].get('worker_id') == 'w1']
    assert len(terminal) == 1
    payload = terminal[0]['payload']
    # never a sealed success for a timed-out worker
    assert terminal[0]['event_type'] == 'execution_failed'
    assert payload['state'] == 'FAILED'
    assert payload['execution_outcome'] == 'timed_out'
    assert payload['timeout_s'] == 1.0
    assert payload['elapsed_ms'] >= 900
    assert payload['process_termination'] == 'confirmed'
    assert payload['launched'] is True
    assert 'exit_code' not in payload or payload['exit_code'] is None
    # correlation ids stay independent and correct
    assert payload['task_id'] == 'task-to'
    assert payload['worker_id'] == 'w1'
    assert payload['execution_id'] is not None
    assert payload['execution_id'].startswith('exec-')


def test_a_non_timeout_failure_invents_no_timeout_facts(
        tmp_path: Path) -> None:
    """A worker that fails for another reason reports no timeout at all.

    The command here exits normally, so the run cannot even produce a baseline
    inventory and the verdict is inconclusive -- a plain FAILED, with no
    timeout_s, no process_termination and no execution_outcome to mislead a
    reader into thinking a budget ran out. (Sealed success records on real
    runs are pinned by tests/test_dag_journal.py, unchanged by this phase.)
    """
    repo = _repo(tmp_path)
    events = _run(repo, [_worker('w1', 'printf ok > out.txt')], 'task-ok')
    terminal = [e for e in events
                if e['event_type'] in ('sealed', 'execution_failed')
                and e['payload'].get('worker_id') == 'w1']
    assert len(terminal) == 1
    payload = terminal[0]['payload']
    assert payload['state'] == 'FAILED'
    assert 'timeout' not in str(payload['reason'])
    for absent in ('execution_outcome', 'timeout_s', 'process_termination',
                   'elapsed_ms'):
        assert absent not in payload


def test_no_timeout_specific_event_type_exists() -> None:
    """The vocabulary is the lifecycle's, not a parallel one."""
    for name in ('TIMEOUT', 'DEADLINE', 'PROCESS_TERMINATED'):
        assert not hasattr(telemetry, name)


def test_legacy_records_without_timeout_fields_still_read(
        tmp_path: Path) -> None:
    """An old journal has no timeout keys and must still be valid."""
    repo = _repo(tmp_path)
    legacy = {'event_id': '0011223344556677', 'run_id': 'run-old',
              'timestamp': '2026-01-01T00:00:00+00:00',
              'event_type': 'execution_failed',
              'payload': {'task_id': 'old', 'worker_id': 'w1',
                          'execution_id': 'exec-0123456789ab',
                          'state': 'FAILED', 'reason': 'timeout_exceeded',
                          'evidence': None}}
    path = common_paths.get_journal_dir(repo) / 'old.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy) + '\n', encoding='utf-8')
    events = [json.loads(line) for line in
              path.read_text(encoding='utf-8').splitlines() if line.strip()]
    telemetry.validate_event(events[0])      # the old shape is still valid
    assert events[0]['payload']['reason'] == 'timeout_exceeded'
    assert 'timeout_s' not in events[0]['payload']
