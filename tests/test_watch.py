"""Phase 5.3 -- focused watch tests (spec Step 6).

Covers the real failure modes of the thin observation loop: debounce
coalescing, ignore rules, clean shutdown with no leaked threads,
preview/evidence isolation, trigger-only-on-explicit-key, and that an
explicit trigger reaches ONLY the existing public CLI runtime authority.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from asha import cli, ui, watcher

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return proc.stdout


def _repo(tmp_path: Path, *, with_tests: bool = False) -> Path:
    root = tmp_path / 'repo'
    root.mkdir()
    _git(root, 'init', '-q', '.')
    _git(root, 'config', 'user.email', 't@example.invalid')
    _git(root, 'config', 'user.name', 't')
    (root / '.gitignore').write_text('.jspace/\n', encoding='utf-8')
    (root / 'a.py').write_text('x = 1\n', encoding='utf-8')
    if with_tests:
        (root / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
        (root / 'tests').mkdir()
        (root / 'tests' / 'test_smoke.py').write_text(
            'def test_ok():\n    assert True\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    return root


# ---------------------------------------------------------- 1. debounce

def test_debounce_coalesces_rapid_writes_into_one_observation(
        tmp_path: Path) -> None:
    root = _repo(tmp_path)
    loop = watcher.WatchLoop(root, debounce_ms=400, scan_ms=500)
    due_times: list[float] = []
    # clean baseline scan
    assert loop.tick(0.6) is False
    # three rapid writes land BETWEEN scans (0.6 -> next scan at 1.1):
    # they must coalesce into a single logical change, not three
    for i in range(3):
        (root / 'a.py').write_text(
            f'x = {i}  # {"payload" * (i + 1)}\n', encoding='utf-8')
        assert loop.tick(0.7 + 0.1 * i) is False    # no scan window yet
    assert loop.tick(1.1) is False                  # ONE scan notes change
    assert loop.tick(1.3) is False                  # inside quiet window
    if loop.tick(1.51):                             # 400 ms quiet reached
        due_times.append(1.51)
    assert loop.tick(1.6) is False                  # consumed; no re-fire
    assert due_times == [1.51]                      # exactly one refresh


# ------------------------------------------------------- 2. ignore rules

def test_ignored_paths_produce_no_logical_change(tmp_path: Path) -> None:
    root = tmp_path / 'plain'
    root.mkdir()
    (root / 'a.py').write_text('x = 1\n', encoding='utf-8')
    before = watcher.fingerprint(root)
    assert set(before) == {'a.py'}
    # everything on the spec ignore list
    for name in ('.git', '.jspace', '__pycache__', '.hermes', 'venv', 'env',
                 'node_modules'):
        nested = root / name
        nested.mkdir()
        (nested / 'inner.py').write_text('y = 2\n', encoding='utf-8')
    for name in ('a.swp', 'b~', 'c.tmp'):
        (root / name).write_text('noise\n', encoding='utf-8')
    after = watcher.fingerprint(root)
    assert after == before                          # zero logical change
    loop = watcher.WatchLoop(root, debounce_ms=400, scan_ms=500)
    assert loop.tick(0.6) is False
    (root / '.jspace' / 'evidence.json').write_text('{}', encoding='utf-8')
    assert loop.tick(1.1) is False                  # ignored: not observed


# ----------------------------------------------------- 3. clean shutdown

def test_watch_subprocess_quits_cleanly_on_q(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    env = dict(os.environ)
    env['PYTHONPATH'] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, '-m', 'asha', '--watch'], cwd=root,
        input='q\n', capture_output=True, text=True, timeout=30, env=env)
    assert proc.returncode == 0
    assert 'Asha Watch stopped.' in proc.stdout
    assert proc.stderr == ''


def test_inprocess_watch_leaves_no_threads(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture
                                           ) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('q\n'))
    before = threading.enumerate()
    code = watcher.run_watch(root, ui=False)
    capsys.readouterr()
    assert code == 0
    after = threading.enumerate()
    assert len(after) <= len(before)                # reader thread joined


# ------------------------------------------- 4. preview/evidence isolation

def test_observation_never_mutates_authoritative_evidence(
        tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = root / '.jspace' / 'evidence.json'
    evidence.parent.mkdir(parents=True, exist_ok=True)
    sentinel = json.dumps({'schema': 1, 'commit': 'abc',
                           'evidence_sha256': 'deadbeef'})
    evidence.write_text(sentinel, encoding='utf-8')
    orch = root / '.jspace' / 'cache' / 'orchestrator' / 'task1'
    orch.mkdir(parents=True)
    run_ev = json.dumps({'validation_mode': 'COMPLETE',
                         'evidence_sha256': 'cafebabe'})
    (orch / 'wid1.json').write_text(run_ev, encoding='utf-8')

    (root / 'a.py').write_text('x = 9  # dirty\n', encoding='utf-8')
    before_gate = evidence.read_bytes()
    before_run = (orch / 'wid1.json').read_bytes()
    # a full observation refresh cycle + the watch report write
    loop = watcher.WatchLoop(root)
    snapshot = loop.refresh()
    sealed = watcher.read_last_sealed(root)
    payload = watcher.watch_report_payload(root, 'master', snapshot, sealed)
    report = ui.write_report(payload, tmp_path / 'watch.html')
    assert report.is_file()
    # state actually observed (dirty worktree) yet evidence untouched
    assert snapshot.paths                                   # observed work
    assert evidence.read_bytes() == before_gate
    assert (orch / 'wid1.json').read_bytes() == before_run
    assert sealed.get('evidence_sha256') == 'cafebabe'      # read-only view


def test_watch_report_writes_only_to_report_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path)
    evidence = root / '.jspace' / 'evidence.json'
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"schema": 1}', encoding='utf-8')
    before = evidence.read_bytes()
    monkeypatch.setattr(sys, 'stdin', io.StringIO('q\n'))
    out = tmp_path / 'report.html'
    code = watcher.run_watch(root, ui=True, ui_out=out)
    capsys.readouterr()
    assert code == 0
    assert out.is_file()
    assert evidence.read_bytes() == before


# ------------------------------------------- 5. explicit trigger only

def test_dirty_files_alone_never_invoke_authority(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path)
    calls: list[Path] = []

    def _spawn(r: Path) -> None:
        calls.append(r)
        raise AssertionError('dirty tree must never spawn authority')

    monkeypatch.setattr(watcher, 'spawn_authority', _spawn)
    (root / 'a.py').write_text('x = 2  # dirty\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'stdin', io.StringIO('q\n'))
    code = watcher.run_watch(root, ui=False)
    capsys.readouterr()
    assert code == 0
    assert calls == []                              # dirtying: NO run


@pytest.mark.parametrize('keys', ['r\n', '\n'])      # r OR Enter
def test_explicit_key_invokes_authority_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, keys: str) -> None:
    root = _repo(tmp_path)
    calls: list[Path] = []

    class _FakeProc:
        returncode = 0

        def __init__(self) -> None:
            self._done = False

        def poll(self) -> int | None:
            self._done = True
            return 0 if self._done else None

        def communicate(self, timeout: float = 30.0) -> tuple[str, str]:
            return ('{"decision":"COMPLETE","validation_result":"PASS",'
                    '"evidence_id":"ffffffffffffffffffffffffffffffffffffffff'
                    'ffffffffffffffffffffffffffff"}'), ''

    def _spawn(r: Path) -> _FakeProc:
        calls.append(r)
        return _FakeProc()

    monkeypatch.setattr(watcher, 'spawn_authority', _spawn)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(keys + 'q\n'))
    code = watcher.run_watch(root, ui=False)
    out = capsys.readouterr().out
    assert code == 0
    assert len(calls) == 1
    assert calls[0] == root
    assert 'decision=COMPLETE' in out               # displayed as run output


# ------------------------------------------- 6. runtime authority path

def test_authority_command_is_the_public_cli_only() -> None:
    assert watcher.authority_command() == [
        sys.executable, '-m', 'asha', '--json']


def test_authority_child_env_guards_git_prompt_pager(
        monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def _spy(cmd, **kwargs):
        captured.update(kwargs.get('env', {}))
        class _R:
            returncode = 0
            stdout = '{"schema_version": 1}'
            stderr = ''
        return _R()

    monkeypatch.setattr(subprocess, 'run', _spy)
    code, _ = watcher.run_authority(Path('/x'))
    assert code == 0
    assert captured.get('GIT_TERMINAL_PROMPT') == '0'
    assert captured.get('GIT_PAGER') == 'cat'


def test_explicit_trigger_reaches_schema_v1_runtime_authority(
        tmp_path: Path) -> None:
    root = _repo(tmp_path, with_tests=True)
    (root / 'a.py').write_text('x = 3  # dirty\n', encoding='utf-8')
    code, payload = watcher.run_authority(root, timeout=180)
    assert payload is not None, 'authority did not emit a JSON document'
    assert payload.get('schema_version') == 1       # the EXISTING contract
    assert code in (0, 1, 2)
    if code == 0:                                   # success path ran
        assert payload.get('decision') in ('COMPLETE', 'SCOPED')


def test_watcher_contains_no_shadow_governance() -> None:
    source = (REPO_ROOT / 'asha' / 'watcher.py').read_text(encoding='utf-8')
    for banned in ('run_scoped(', 'assess_scoping', 'build_graph',
                   'check_runner', 'from .scoping', 'from .scheduler'):
        assert banned not in source, banned


# ------------------------------------------------------- watch UI (Step 5)

def test_watch_ui_separates_preview_from_sealed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / 'a.py').write_text('x = 9  # dirty\n', encoding='utf-8')
    from asha import git_context
    snapshot = git_context.snapshot(root)          # real 5.2.3 contract
    payload = watcher.watch_report_payload(
        root, 'main', snapshot,
        {'validation_mode': 'COMPLETE', 'evidence_sha256': 'abc123'})
    html_text = ui.render_report(payload)
    assert 'a.py' in html_text
    assert 'LIVE / PREVIEW' in html_text
    assert 'non-authoritative' in html_text
    assert 'LAST SEALED RUN' in html_text
    assert 'abc123' in html_text                    # recorded identity shown
    assert '<h2>Decision<' not in html_text         # no verdict section
    assert 'http-equiv="refresh"' in html_text      # offline auto-refresh
    assert '<script' not in html_text
    # determinism / purity still hold for the watch layout
    assert html_text == ui.render_report(payload)


def test_watch_json_combination_is_rejected(
        capsys: pytest.CaptureFixture) -> None:
    code = cli.main(['--watch', '--json'])
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR                   # exit 2
    assert 'INVALID_ARGUMENTS' in captured.err or 'INVALID_ARGUMENTS' in \
        captured.out
