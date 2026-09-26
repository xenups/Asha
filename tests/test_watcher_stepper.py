"""Phase 5.4.4 -- live stepper integration in Watch Mode.

These tests exercise the REAL projector/presentation path through the
watcher helpers; they never duplicate the state logic under test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from asha import telemetry, watcher
from asha.presentation import render_stepper_html


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'tests').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', '.'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@e.i'],
                   cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 't'],
                   cwd=root, check=True)
    (root / '.gitignore').write_text('.jspace/\n', encoding='utf-8')
    (root / 'tests' / 'test_a.py').write_text(
        'def test_ok():\n    assert True\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'init'], cwd=root, check=True)
    return root


def _journal(root: Path, events: list[str], run_id: str = 'r-live') -> None:
    with telemetry.EventJournalWriter(root, run_id) as j:
        for ev in events:
            payload = {}
            if ev == 'sealed':
                payload = {'evidence_sha256': 'deadbeef',
                           'evidence_id': 'ev-1'}
            elif ev == 'validation_failed':
                payload = {'reason': 'checks failed'}
            elif ev == 'scope_assessed':
                payload = {'mode': 'COMPLETE',
                           'fallback_reason': 'UNKNOWN_CLASSIFICATION'}
            j.append(ev, payload, critical=(ev not in (
                'change_detected', 'scope_assessed')))


# ------------------------------------------------------------ Test 1

def test_idle_shows_clean_no_spawn(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    journal = watcher.latest_journal(root)
    assert journal is None                    # no journal: clean state
    html = watcher.watch_stepper(root, False)
    assert 'IDLE_CLEAN' in html
    assert 'All systems synchronized' in html
    # modified repo: journal with change_detected only
    _journal(root, ['change_detected'])
    html2 = watcher.watch_stepper(root, False)
    assert 'MODIFIED_PREVIEW' in html2
    assert 'Change Detected' in html2


# ------------------------------------------------------------ Test 2

def test_active_progression_steps(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _journal(root, ['change_detected', 'scope_assessed',
                    'execution_started'])
    journal = watcher.latest_journal(root)
    assert journal is not None
    state, meta = telemetry.project_path(journal)
    assert state == 'EXECUTING'
    html = render_stepper_html(state, meta)
    # step 1+2 completed, step 3 active -- asserted via the stepper's
    # own deterministic classes (no independent state math here)
    assert html.count('class="node done"') == 2
    assert html.count('class="node current"') == 1
    assert 'Execution' in html


# ------------------------------------------------------------ Test 3

def test_process_interruption_explicit_fact(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _journal(root, ['change_detected', 'scope_assessed',
                    'execution_started'])
    journal = watcher.latest_journal(root)
    # no terminal event; process confirmed dead (poll() is not None
    # is the watcher's explicit host fact)
    assert journal is not None
    assert not watcher.journal_terminal_event(journal)
    state, meta = telemetry.project_path(journal, interrupted=True)
    assert state == 'INTERRUPTED'
    html = render_stepper_html(state, meta)
    assert 'INTERRUPTED' in html
    assert 'FAILED' not in html
    # without the fact: state stays EXECUTING
    state2, _ = telemetry.project_path(journal)
    assert state2 == 'EXECUTING'


# ------------------------------------------------------------ Test 4

def test_explicit_failure_renders(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _journal(root, ['change_detected', 'scope_assessed',
                    'execution_started', 'validation_started',
                    'validation_failed'])
    journal = watcher.latest_journal(root)
    assert journal is not None
    state, meta = telemetry.project_path(journal)
    assert state == 'FAILED'
    html = render_stepper_html(state, meta)
    assert 'FAILED' in html
    assert 'explicit failure fact recorded' in html
    # recorded failure row carries the fact (not inferred)
    # failure comes from the recorded event, not from process state
    assert 'INTERRUPTED' not in html


# ------------------------------------------------------------ Test 5

def test_terminal_sealing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _journal(root, ['change_detected', 'scope_assessed',
                    'execution_started', 'validation_started',
                    'sealed'])
    journal = watcher.latest_journal(root)
    assert journal is not None
    state, meta = telemetry.project_path(journal)
    assert state == 'SEALED'
    html = render_stepper_html(state, meta)
    assert html.count('class="node done"') == 5
    # recorded evidence field from the sealed event
    assert 'deadbeef' in html


# ------------------------------------------------------------ Test 6

def test_watch_html_strict_offline(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _journal(root, ['change_detected', 'scope_assessed',
                    'execution_started', 'validation_started', 'sealed'])
    html = watcher.watch_stepper(root, False)
    for banned in ('http://', 'https://', 'new WebSocket', 'EventSource',
                   '<script', '<link', '<iframe', 'fetch('):
        assert banned not in html, banned


# ------------------------------------------------------------ Test 7

def test_watch_html_integration_sealed_journal(tmp_path: Path) -> None:
    """The full watcher UI path: journal written by the real authority
    (via EventJournalWriter, same call the CLI hook makes) renders as a
    completed stepper in watch.html through the watcher helpers."""
    import json as _json
    root = _repo(tmp_path)
    _journal(root, ['change_detected', 'scope_assessed',
                    'execution_started', 'validation_started',
                    'sealing_started', 'sealed'], run_id='r-full')
    # authoritative sealed evidence (as the gate writes it): the drawer
    # must consume THIS, not the ephemeral journal metadata
    gate = root / '.jspace' / 'evidence.json'
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(_json.dumps({
        'commit': 'c' * 40, 'tree_hash': 'd' * 40,
        'evidence_sha256': 'e' * 64, 'scope': 'S3',
        'observed_at': '2026-09-26T11:00:00Z'}))
    from asha import git_context
    snap = git_context.snapshot(root)
    sealed = watcher.read_last_sealed(root)
    report = root / '.jspace' / 'reports' / 'watch.html'
    watcher._write_watch_html(root, report, snap, sealed,
                              watcher.watch_stepper(root, False))
    html = report.read_text(encoding='utf-8')
    assert html.count('class="node done"') == 5
    assert 'SEALED' in html
    assert 'Evidence sealed' in html
    # drawer shows the authoritative sealed evidence (hashes only)
    assert 'Evidence SHA-256' in html
    assert 'Tree Hash' in html
    assert ('c' * 10) in html        # commit, middle-truncated
    # no second stepper block (single injection)
    assert html.count('<!-- ASHA-STEPPER -->') == 1


def test_watch_stepper_current_vs_last_sealed(tmp_path: Path) -> None:
    """Current state stays the projector's state; a previous sealed
    run is never presented as current."""
    root = _repo(tmp_path)
    _journal(root, ['change_detected', 'scope_assessed', 'sealed'],
             run_id='r-old')
    # a NEW run starts (new journal, no terminal event yet)
    _journal(root, ['change_detected'], run_id='r-new')
    html = watcher.watch_stepper(root, False)
    # projector sees the newest run's change_detected -> MODIFIED_PREVIEW
    assert 'MODIFIED_PREVIEW' in html
    assert 'node not_reached' in html
    assert 'node done' not in html


def test_governance_trio_frozen(tmp_path: Path) -> None:
    import subprocess as sp
    cwd = Path(__file__).resolve().parents[1]
    out = sp.run(['git', 'diff', 'b30f731..HEAD', '--',
                  'asha/scoping.py', 'asha/check_runner.py',
                  'asha/scheduler.py'],
                 cwd=cwd, capture_output=True, text=True, check=True)
    assert out.stdout == ''


# ------------------------------------------------------------ watcher helpers

def test_journal_terminal_event_detects_sealed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert watcher.latest_journal(root) is None
    _journal(root, ['change_detected'])
    assert not watcher.journal_terminal_event(
        watcher.latest_journal(root))
    _journal(root, ['change_detected', 'scope_assessed',
                    'execution_started', 'validation_started', 'sealed'])
    assert watcher.journal_terminal_event(
        watcher.latest_journal(root))


def test_inject_stepper_places_before_body() -> None:
    html = '<html><body><p>hi</p></body></html>'
    out = watcher.inject_stepper(html, '<div id="stepper">S</div>')
    assert out.index('<div id="stepper">') < out.index('</body>')
    assert out.count('<div id="stepper">') == 1
    # repeated injection REPLACES the previous block (growing journal)
    out2 = watcher.inject_stepper(out, '<div id="stepper">S2</div>')
    assert out2.count('<div id="stepper">') == 1
    assert 'S2' in out2 and '>S<' not in out2
    # no </body> -> append at end
    out3 = watcher.inject_stepper('<html><body>',
                                  '<div id="stepper">S</div>')
    assert out3 == '<html><body><!-- ASHA-STEPPER -->' \
        '<div id="stepper">S</div><!-- /ASHA-STEPPER -->'