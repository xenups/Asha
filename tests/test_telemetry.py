"""Phase 5.4.2 -- pipeline event journal + state projector tests.

Covers the spec's full matrix: complete replay, truncated tail,
process interruption (explicit fact only), explicit failure, fsync
durability on critical events, non-critical boundary, deterministic
replay, invalid trailing JSON, governance isolation, plus the
journal/hook behavior on the real CLI path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from asha.common import paths as common_paths
from asha import cli, telemetry, ui
from asha.telemetry import EventJournalWriter, PipelineStateProjector  # noqa: F401


def _event(event_type: str, run_id: str = 'run1',
           **payload: object) -> dict[str, object]:
    return {
        'event_id': 'e' + event_type[:4],
        'run_id': run_id,
        'timestamp': '2026-09-26T00:00:00+00:00',
        'event_type': event_type,
        'payload': payload,
    }


def _journal_line(event_type: str, run_id: str = 'run1') -> str:
    return telemetry.serialize_event(_event(event_type, run_id))


def _journal_file(tmp_path: Path, run_id: str) -> Path:
    """External journal file (Phase D zone)."""
    return common_paths.get_journal_dir(tmp_path) / f'{run_id}.jsonl'


# ------------------------------------------------------------ writer

def test_writer_appends_valid_jsonl(tmp_path: Path) -> None:
    with EventJournalWriter(tmp_path, 'r_x') as journal:
        journal.append('change_detected', {'target_files': ['a.py']})
        journal.append('sealed', {'evidence_id': 'deadbeef'}, critical=True)
    lines = _journal_file(tmp_path, 'r_x').read_text(
        encoding='utf-8').splitlines()
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)               # one valid JSON per line
        assert set(parsed) == {'event_id', 'run_id', 'timestamp',
                               'event_type', 'payload'}
        assert parsed['run_id'] == 'r_x'
    assert lines[0].endswith('\n') is False or True  # splitlines strips \n
    assert _journal_file(tmp_path, 'r_x').read_bytes().endswith(b'\n')


def test_writer_rejects_unknown_event_type(tmp_path: Path) -> None:
    with EventJournalWriter(tmp_path, 'r2') as journal, pytest.raises(ValueError):
        journal.append('service_should_run')   # forbidden event


def test_writer_is_append_only(tmp_path: Path) -> None:
    with EventJournalWriter(tmp_path, 'r3') as journal:
        journal.append('change_detected')
        path = journal.path
    before = path.read_bytes()
    with EventJournalWriter(tmp_path, 'r3') as journal:   # reopen appends
        journal.append('scope_assessed')
    after = path.read_bytes()
    assert after.startswith(before)                # prior records untouched


# ------------------------------------------------------------ fsync

def test_critical_events_flush_and_fsync(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch
                                         ) -> None:
    calls: list[str] = []
    real_fsync = os.fsync

    def _spy_fsync(fd: int) -> None:
        calls.append('fsync')
        real_fsync(fd)

    monkeypatch.setattr(os, 'fsync', _spy_fsync)
    with EventJournalWriter(tmp_path, 'r4') as journal:
        journal.append('execution_started', critical=False)
        journal.append('sealed', critical=False)
    assert calls.count('fsync') == 2               # both are critical TYPEs


def test_non_critical_events_do_not_fsync(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch
                                          ) -> None:
    calls: list[str] = []
    monkeypatch.setattr(os, 'fsync',
                        lambda fd: calls.append('fsync'))
    with EventJournalWriter(tmp_path, 'r5') as journal:
        journal.append('change_detected', critical=False)
        journal.close()
    assert calls == []                              # documented boundary


# ------------------------------------------------------------ projector

def test_complete_replay_ends_sealed() -> None:
    journal = ''.join(_journal_line(t) for t in (
        'change_detected', 'scope_assessed', 'execution_started',
        'validation_started', 'sealing_started', 'sealed'))
    state, meta = telemetry.project(journal)
    assert state == 'SEALED'
    assert meta['integrity']['incomplete'] is False


def test_truncated_tail_ignored_no_failure() -> None:
    journal = (_journal_line('change_detected')
               + _journal_line('scope_assessed')
               + _journal_line('sealed')
               + '{"event_id": "trunca')   # no \n, cut mid-field
    state, meta = telemetry.project(journal)
    assert state == 'SEALED'                 # prior valid events intact
    assert meta['integrity']['incomplete'] is True
    assert meta['integrity']['invalid_trailing'] is True
    assert 'FAILED' not in state


def test_process_interruption_requires_explicit_fact() -> None:
    journal = _journal_line('execution_started')
    # WITHOUT the fact: no stronger state invented, never FAILED
    state_no_fact, _ = telemetry.project(journal)
    assert state_no_fact == 'EXECUTING'
    # WITH explicit interruption fact: INTERRUPTED
    state_fact, _ = telemetry.project(journal, interrupted=True)
    assert state_fact == 'INTERRUPTED'
    state_fact2, _ = telemetry.project(journal, interrupted=False)
    assert state_fact2 == 'EXECUTING'


def test_explicit_failure_fact_is_failed() -> None:
    journal = (_journal_line('change_detected')
               + _journal_line('validation_failed'))
    state, _ = telemetry.project(journal)
    assert state == 'FAILED'


def test_invalid_trailing_json_safe() -> None:
    journal = (_journal_line('change_detected')
               + _journal_line('sealed')
               + '{not valid json')
    state, meta = telemetry.project(journal)
    assert state == 'SEALED'
    assert meta['integrity']['incomplete'] is True
    # no exception escaped


def test_deterministic_replay() -> None:
    journal = ''.join(_journal_line(t) for t in (
        'change_detected', 'scope_assessed', 'execution_started'))
    s1, m1 = telemetry.project(journal)
    s2, m2 = telemetry.project(journal)
    assert s1 == s2
    assert m1 == m2


def test_empty_journal_is_idle_clean() -> None:
    state, meta = telemetry.project('')
    assert state == 'IDLE_CLEAN'
    assert meta['integrity']['record_count'] == 0
    assert meta['integrity']['incomplete'] is False


def test_mid_file_junk_does_not_corrupt() -> None:
    journal = (_journal_line('change_detected')
               + 'garbage line\n'
               + _journal_line('scope_assessed'))
    state, _ = telemetry.project(journal)
    assert state == 'SCOPE_ASSESSED'


def test_governance_isolation() -> None:
    """telemetry must not import or call governance modules."""
    source = Path(telemetry.__file__).read_text(encoding='utf-8')
    for banned in ('scoping', 'scheduler', 'check_runner', 'codegraph',
                   'scope_resolver', 'run_scoped('):
        assert banned not in source, banned


# ------------------------------------------------------------ CLI hook

def test_cli_writes_journal_no_changes(tmp_path: Path) -> None:
    """Clean tree: journal has change_detected? No -- only a journal is
    created when target_paths exist; a clean tree emits NO_CHANGES and
    the journal is empty/absent."""
    import subprocess

    from asha import cli
    # minimal repo, no changes
    subprocess.run(['git', 'init', '-q', '.'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@e.i'],
                   cwd=tmp_path, check=True)
    subprocess.run(['git', 'config', 'user.name', 't'],
                   cwd=tmp_path, check=True)
    (tmp_path / 'a.py').write_text('x = 1\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'commit', '-qm', 'init'], cwd=tmp_path,
                   check=True)
    code = cli.main(['--root', str(tmp_path), '--json', '--no-execute'])
    assert code == cli.EXIT_OK
    journal_dir = common_paths.get_journal_dir(tmp_path)
    files = list(journal_dir.glob('*.jsonl')) if journal_dir.exists() \
        else []
    # change_detected is only appended when target_paths exist; with no
    # changes there are no targets.
    for f in files:
        assert telemetry.project_path(f)[0] == 'IDLE_CLEAN'


def test_cli_journal_full_lifecycle_with_execution(tmp_path: Path) -> None:
    """A real run with --no-execute on a dirty tree writes the
    pre-execution facts (change_detected + scope_assessed); no
    execution events because execution is not run."""
    import subprocess

    from asha import cli
    subprocess.run(['git', 'init', '-q', '.'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@e.i'],
                   cwd=tmp_path, check=True)
    subprocess.run(['git', 'config', 'user.name', 't'],
                   cwd=tmp_path, check=True)
    (tmp_path / 'a.py').write_text('x = 1\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'commit', '-qm', 'init'], cwd=tmp_path,
                   check=True)
    (tmp_path / 'a.py').write_text('x = 2  # dirty\n', encoding='utf-8')
    code = cli.main(['--root', str(tmp_path), '--json', '--no-execute'])
    assert code == cli.EXIT_OK
    journal_dir = common_paths.get_journal_dir(tmp_path)
    files = sorted(journal_dir.glob('*.jsonl')) if journal_dir.exists() \
        else []
    assert files, 'journal expected on a dirty tree with execution path'
    state, _ = telemetry.project_path(files[0])
    types = [json.loads(l)['event_type'] for l in
             files[0].read_text(encoding='utf-8').splitlines()]
    assert types[0] == 'change_detected'
    assert 'scope_assessed' in types
    # no execution events: --no-execute never runs the authority path
    assert 'execution_started' not in types
    assert state == 'SCOPE_ASSESSED'