"""Phase 5.4.3 -- human pipeline stepper tests.

The stepper is presentation-only: it maps projector (state, meta) to
human labels + offline HTML and never infers, executes, or writes.
"""

from __future__ import annotations

from pathlib import Path

from asha import presentation
from asha.presentation import (
    hero_text,
    human_label,
    present,
    presentation_step,
    render_stepper_html,
)
from asha.telemetry import project

ALL_STATES = ('IDLE_CLEAN', 'MODIFIED_PREVIEW', 'SCOPE_ASSESSED',
              'EXECUTING', 'VALIDATING', 'FAILED', 'INTERRUPTED',
              'SEALED')


# ---------------------------------------------------------------- mapping

def test_state_mapping_every_state_maps_once() -> None:
    seen: set[tuple[int, str]] = set()
    for state in ALL_STATES:
        step, status = presentation_step(state, None)
        assert (step, status) not in seen           # no same tuple twice
        seen.add((step, status))
        assert 1 <= step <= 5
        assert status in ('current', 'done', 'failed', 'interrupted',
                          'not_reached')
    # the 5 pipeline steps are all represented
    assert {s for s, _ in seen} == {1, 2, 3, 4, 5}


def test_human_labels_deterministic_no_claims() -> None:
    for state in ALL_STATES:
        label = human_label(state)
        assert label == human_label(state)
        assert label.strip()
        # presentation translations: no governance claims
        for banned in ('safe', 'healthy', 'secure', 'correct',
                       'successful system state', 'سرویس', 'شما'):
            assert banned.lower() not in label.lower()
    assert human_label('NOT_A_STATE') == 'Not recorded'


def test_no_inference_render_deterministic() -> None:
    meta = {'last_event': 'sealed', 'run_id': 'r1',
            'integrity': {'incomplete': False, 'record_count': 6}}
    html1 = render_stepper_html('SEALED', meta)
    html2 = render_stepper_html('SEALED', dict(meta))
    assert html1 == html2


def test_missing_fields_render_not_recorded() -> None:
    html = render_stepper_html('EXECUTING', {})
    assert 'Not recorded' in html
    # never raises on empty/missing metadata
    render_stepper_html('FAILED', None)
    render_stepper_html('IDLE_CLEAN')


def test_failed_only_when_projector_failed() -> None:
    failed_html = render_stepper_html('FAILED',
                                      {'last_event': 'validation_failed'})
    assert 'class="node failed"' in failed_html
    non_failed = render_stepper_html('SEALED', {'last_event': 'sealed'})
    assert 'class="node failed"' not in non_failed


def test_interrupted_only_when_projector_interrupted() -> None:
    # INTERRUPTED comes from an explicit fact in the projector
    assert 'class="node interrupted"' in render_stepper_html(
        'INTERRUPTED', {'last_event': 'execution_started'})
    # a missing terminal event alone is NOT interrupted
    assert 'class="node interrupted"' not in render_stepper_html(
        'EXECUTING', {'last_event': 'execution_started'})


def test_sealed_only_when_projector_sealed() -> None:
    sealed = render_stepper_html('SEALED', {'last_event': 'sealed'})
    assert sealed.count('class="node done"') >= 5   # all five steps done
    assert 'class="node current"' not in sealed
    unsealed = render_stepper_html('EXECUTING',
                                   {'last_event': 'execution_started'})
    # steps 1-2 are recorded as done, step 3 current, 4-5 not reached
    assert unsealed.count('class="node done"') == 2
    assert unsealed.count('class="node current"') == 1
    assert 'class="node failed"' not in unsealed
    assert 'class="node interrupted"' not in unsealed


# ---------------------------------------------------------------- no fabrication

def test_no_service_fabrication() -> None:
    html = render_stepper_html('EXECUTING', {'last_event': 'execution_started'})
    for banned in ('RUNNING', 'Service', 'node_is_running', 'سرویس'):
        assert banned not in html


def test_read_only_render_leaves_sources_untouched(tmp_path: Path) -> None:
    journal = tmp_path / '.jspace' / 'execution' / 'r91.jsonl'
    journal.parent.mkdir(parents=True)
    journal.write_text(
        '{"event_id":"a","run_id":"r91","timestamp":"t",'
        '"event_type":"change_detected","payload":{}}\n'
        '{"event_id":"b","run_id":"r91","timestamp":"t",'
        '"event_type":"sealed","payload":{}}\n',
        encoding='utf-8')
    before = journal.read_bytes()
    state, meta = project(journal.read_text(encoding='utf-8'))
    render_stepper_html(state, meta)
    assert journal.read_bytes() == before      # journal untouched
    assert not (tmp_path / '.jspace' / 'evidence.json').exists()  # no new evidence


def test_offline_no_network_references() -> None:
    html = render_stepper_html('SEALED', {'last_event': 'sealed'})
    for banned in ('<script', 'fetch(', 'XMLHttpRequest', 'WebSocket',
                   'EventSource', 'http://', 'https://', '@import',
                   '<link'):
        assert banned not in html


def test_governance_freeze(tmp_path: Path) -> None:
    """The presentation layer never touches governance surfaces."""
    source = Path(presentation.__file__).read_text(encoding='utf-8')
    for banned in ('import scoping', 'import scheduler',
                   'import check_runner', 'import codegraph',
                   'import ast_indexer', 'import git_context',
                   'run_scoped(', 'subprocess', 'os.system'):
        assert banned not in source, banned


def test_step_status_uses_recorded_event_fact() -> None:
    # FAILED refined by WHICH recorded event; no inference
    assert presentation_step('FAILED', 'validation_failed') == (4, 'failed')
    assert presentation_step('FAILED', 'execution_failed') == (3, 'failed')
    assert presentation_step('INTERRUPTED', 'execution_started') == (3, 'interrupted')
    # default when no event refinement exists
    assert presentation_step('FAILED', None) == (4, 'failed')


def test_present_alias_equals_render() -> None:
    meta = {'last_event': 'scope_assessed', 'run_id': 'x'}
    assert present('SCOPE_ASSESSED', meta) == render_stepper_html(
        'SCOPE_ASSESSED', meta)


def test_sealed_displays_recorded_evidence() -> None:
    text = ''.join(
        '{"event_id":"a","run_id":"r","timestamp":"t1",'
        '"event_type":"change_detected","payload":{}}\n'
        '{"event_id":"b","run_id":"r","timestamp":"t2",'
        '"event_type":"scope_assessed","payload":{"mode":"COMPLETE",'
        '"fallback_reason":"UNKNOWN_CLASSIFICATION"}}\n'
        '{"event_id":"c","run_id":"r","timestamp":"t3",'
        '"event_type":"sealed","payload":{"evidence_sha256":"abc123",'
        '"evidence_id":"ev-9"}}\n')
    state, meta = project(text)
    assert state == 'SEALED'
    html = render_stepper_html(state, meta)
    assert 'abc123' in html
    assert 'ev-9' in html


def test_scope_info_comes_from_scope_event_only() -> None:
    text = ''.join(
        '{"event_id":"a","run_id":"r","timestamp":"t1",'
        '"event_type":"change_detected","payload":{}}\n'
        '{"event_id":"b","run_id":"r","timestamp":"t2",'
        '"event_type":"scope_assessed","payload":{"mode":"SCOPED",'
        '"fallback_reason":"PROVEN_SHARED"}}\n'
        '{"event_id":"c","run_id":"r","timestamp":"t3",'
        '"event_type":"sealed","payload":{"evidence_sha256":"x"}}\n')
    state, meta = project(text)
    html = render_stepper_html(state, meta)
    assert 'SCOPED' in html                 # from scope_assessed, recorded
    assert 'PROVEN_SHARED' in html          # recorded fallback, mapped label
    # an unrelated event must NOT feed scope fields: build a journal
    # where only sealed carries payload; scope shows Not recorded
    text2 = ''.join(
        '{"event_id":"a","run_id":"r","timestamp":"t1",'
        '"event_type":"change_detected","payload":{}}\n'
        '{"event_id":"b","run_id":"r","timestamp":"t2",'
        '"event_type":"sealed","payload":{"evidence_sha256":"y"}}\n')
    state2, meta2 = project(text2)
    html2 = render_stepper_html(state2, meta2)
    assert 'Scope mode' in html2
    assert 'Not recorded' in html2


def test_missing_payload_fields_not_recorded() -> None:
    text = ''.join(
        '{"event_id":"a","run_id":"r","timestamp":"t1",'
        '"event_type":"change_detected","payload":{}}\n'
        '{"event_id":"b","run_id":"r","timestamp":"t2",'
        '"event_type":"sealed","payload":{}}\n')
    state, meta = project(text)
    html = render_stepper_html(state, meta)
    assert 'Evidence SHA' in html
    assert 'Not recorded' in html            # no fabrication


def test_payload_key_order_does_not_change_semantics() -> None:
    text_a = ('{"event_id":"a","run_id":"r","timestamp":"t1",'
              '"event_type":"sealed","payload":{"evidence_sha256":"x",'
              '"evidence_id":"e1"}}\n')
    text_b = ('{"event_id":"a","run_id":"r","timestamp":"t1",'
              '"event_type":"sealed","payload":{"evidence_id":"e1",'
              '"evidence_sha256":"x"}}\n')
    state_a, meta_a = project(text_a)
    state_b, meta_b = project(text_b)
    assert render_stepper_html(state_a, meta_a) == \
        render_stepper_html(state_b, meta_b)


def test_identical_input_identical_html() -> None:
    text = ('{"event_id":"a","run_id":"r","timestamp":"t1",'
            '"event_type":"sealed","payload":{"evidence_sha256":"x"}}\n')
    state, meta = project(text)
    h1 = render_stepper_html(state, dict(meta))
    h2 = render_stepper_html(state, dict(meta))
    assert h1 == h2


def test_presentation_no_io_no_gov_imports() -> None:
    src = Path(presentation.__file__).read_text(encoding='utf-8')
    for banned in ('import subprocess', 'import os', 'open(',
                   'Path(', 'import scoping', 'import scheduler',
                   'import check_runner', 'import codegraph',
                   'import ast_indexer', 'import git_context',
                   'run_scoped('):
        assert banned not in src, banned


def test_cli_stepper_reads_journal(tmp_path: Path) -> None:
    """--stepper <run_id> renders the recorded journal; read-only."""
    import subprocess

    from asha import cli, telemetry
    root = tmp_path / 'repo'
    (root / 'tests').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', '.'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@e.i'],
                   cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 't'],
                   cwd=root, check=True)
    (root / '.gitignore').write_text('.jspace/\n', encoding='utf-8')
    (root / 'tests' / 'test_a.py').write_text(
        'def test_ok():\\n    assert True\\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'init'], cwd=root, check=True)
    # record a journal directly
    (root / 'a.py').write_text('x = 1\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    with telemetry.EventJournalWriter(root, 'run-stepper') as j:
        j.append('change_detected', {'target_files': ['a.py']})
        j.append('scope_assessed', {'mode': 'COMPLETE'}, critical=False)
    code = cli.main(['--root', str(root), '--stepper', 'run-stepper'])
    assert code == cli.EXIT_OK
    # no source/evidence mutation from the presentation path
    assert not (root / '.jspace' / 'evidence.json').exists()


# ------------------------------------------------- 5.4.4.1 visual contract

def test_visual_structure_five_steps_ltr_english() -> None:
    html = render_stepper_html('SEALED', {'last_event': 'sealed'})
    # exactly five pipeline nodes, in LTR order, one coherent rail
    assert html.count('<li class="node ') == 5
    assert html.count('<ol class="rail"') == 1
    order = [html.index(f'>{name}<') for name in
             ('Change Detected', 'Scope Assessed', 'Execution',
              'Validation', 'Sealed')]
    assert order == sorted(order)          # 1 -> 5 left-to-right
    assert '<html lang="en" dir="ltr">' in html
    # English-only UI: no Persian strings anywhere in the document
    for banned in ('سرویس', 'شما', 'همه', 'اجرا', 'مهروموم'):
        assert banned not in html


def test_every_state_has_hero_and_step_class() -> None:
    for state in ALL_STATES:
        html = render_stepper_html(state, {'last_event': None})
        title, _ = hero_text(state)
        assert title in html
        # state class present on exactly the pointed step
        _, status = presentation_step(state, None)
        if status == 'done':
            assert html.count('class="node done"') == 5
        elif status == 'current':
            assert html.count('class="node current"') == 1
        elif status == 'failed':
            assert html.count('class="node failed"') == 1
        elif status == 'interrupted':
            assert html.count('class="node interrupted"') == 1
        else:
            assert html.count('class="node not_reached"') == 5


def test_sealed_evidence_fields_only_when_present() -> None:
    text = ''.join(
        '{"event_id":"a","run_id":"r","timestamp":"t1",'
        '"event_type":"change_detected","payload":{}}\n'
        '{"event_id":"b","run_id":"r","timestamp":"t2",'
        '"event_type":"scope_assessed","payload":{"mode":"COMPLETE"}}\n'
        '{"event_id":"c","run_id":"r","timestamp":"t3",'
        '"event_type":"sealed","payload":{"evidence_sha256":"abc123"}}\n')
    state, meta = project(text)
    html = render_stepper_html(state, meta)
    assert 'abc123' in html
    # missing fallback_reason (no scope payload detail) -> Not recorded
    assert 'Not recorded' in html
    assert 'fallback_reason' not in html       # never fabricated
    # the run summary shows the recorded validation result
    assert 'COMPLETE' in html


def test_offline_document_no_network() -> None:
    html = render_stepper_html('VALIDATING', {'last_event':
                                              'validation_started'})
    for banned in ('http://', 'https://', 'WebSocket', 'EventSource',
                   'fetch(', 'XMLHttpRequest', '<script', '<link',
                   '<iframe', '@import'):
        assert banned not in html, banned


def test_determinism_same_input_same_bytes() -> None:
    meta = {'last_event': 'validation_failed', 'run_id': 'r9',
            'integrity': {'incomplete': False, 'record_count': 5},
            'events': [
                {'event_id': 'v', 'event_type': 'validation_failed',
                 'timestamp': 't', 'payload': {'reason': 'checks'}}]}
    h1 = render_stepper_html('FAILED', dict(meta))
    h2 = render_stepper_html('FAILED', dict(meta))
    assert h1 == h2
    # dict key order must not matter
    flipped = dict(reversed(list(meta.items())))
    assert render_stepper_html('FAILED', flipped) == h1


def test_watch_progression_event_facts() -> None:
    """MODIFIED_PREVIEW -> EXECUTING -> VALIDATING -> SEALED from the
    same recorded journal, and process death without a terminal event
    yields INTERRUPTED (never FAILED)."""
    runs = {
        'MODIFIED_PREVIEW': ['change_detected'],
        'EXECUTING': ['change_detected', 'scope_assessed',
                      'execution_started'],
        'VALIDATING': ['change_detected', 'scope_assessed',
                       'execution_started', 'validation_started'],
        'SEALED': ['change_detected', 'scope_assessed',
                   'execution_started', 'validation_started',
                   'sealing_started', 'sealed'],
    }
    for state, events in runs.items():
        text = ''.join(
            f'{{"event_id":"e{i}","run_id":"r","timestamp":"t",'
            f'"event_type":"{ev}","payload":{{}}}}\n'
            for i, ev in enumerate(events))
        st, meta = project(text)
        assert st == state
        html = render_stepper_html(st, meta)
        assert st in html
        if state == 'SEALED':
            assert html.count('class="node done"') == 5
        else:
            assert html.count('class="node current"') == 1
    # process death WITHOUT terminal event: interrupted=True fact
    text = ''.join(
        '{"event_id":"e0","run_id":"r","timestamp":"t",'
        '"event_type":"change_detected","payload":{}}\n'
        '{"event_id":"e1","run_id":"r","timestamp":"t",'
        '"event_type":"scope_assessed","payload":{}}\n'
        '{"event_id":"e2","run_id":"r","timestamp":"t",'
        '"event_type":"execution_started","payload":{}}\n')
    st2, meta2 = project(text, interrupted=True)
    assert st2 == 'INTERRUPTED'
    html2 = render_stepper_html(st2, meta2)
    assert 'class="node interrupted"' in html2
    assert 'FAILED' not in html2
    # without the fact: still EXECUTING, never FAILED
    st3, _ = project(text)
    assert st3 == 'EXECUTING'
