"""Phase 5.2.4 -- Visual Inspector regression suite (spec section 2D).

Covers payload immutability/determinism, decision invariance, missing
field robustness, offline integrity of the generated HTML, and strict
non-interference of `asha inspect` (zero git / governance calls).
"""

from __future__ import annotations

import copy
import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from asha import cli, codegraph, scope_resolver, scoping, ui

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_EVIDENCE = REPO_ROOT / '.jspace/evidence.json'


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        'schema_version': 1,
        'repository': 'https://example.invalid/org/repo.git',
        'change_set': {'base': 'origin/main', 'base_sha': 'a' * 40,
                       'target_sha': 'b' * 40, 'uncommitted_files': 2,
                       'states': {'pkg/mod.py': ['staged', 'unstaged'],
                                  'new.py': ['untracked']},
                       'untracked': ['new.py'], 'conflicts': []},
        'changed_files': ['pkg/mod.py', 'new.py'],
        'decision': 'COMPLETE',
        'eligible': False,
        'fallback_reason': 'PROVEN_SHARED',
        'execution_mode': 'canonical',
        'validation_result': 'PASS',
        'duration_ms': 1234,
        'evidence_id': 'e' * 64,
        'evidence_verification': 'PASS',
        'replay_verification': 'PASS',
        'error': None,
        'status': None,
    }
    base.update(overrides)
    return base


# ------------------------------------------------- immutability / purity

def test_render_is_side_effect_free_and_deterministic() -> None:
    payload = _payload()
    before = copy.deepcopy(payload)
    html_a = ui.render_report(payload)
    assert payload == before            # render(P) leaves P untouched
    html_b = ui.render_report(copy.deepcopy(payload))
    assert html_a == html_b             # render(P) == render(deepcopy(P))
    html_c = ui.render_report(payload)
    assert html_a == html_c             # byte-deterministic


# ------------------------------------------------- decision invariance

@pytest.mark.parametrize('value,cls', [
    ('SCOPED', 'b-scoped'),
    ('COMPLETE', 'b-complete'),
    ('UNKNOWN', 'b-unknown'),
])
def test_decision_badge_exact_mapping(value: str, cls: str) -> None:
    out = ui.render_report(_payload(decision=value))
    assert f'class="badge {cls}">{value}<' in out


def test_missing_decision_renders_unknown_without_inference() -> None:
    out = ui.render_report(_payload(decision=None, fallback_reason=None))
    assert 'badge b-unknown">Unknown<' in out
    assert 'no decision field in artifact' in out
    assert 'fallback reason' in out
    # unknown fallback reason label, not a computed substitute
    assert '<span class="note">Unknown</span>' in out


def test_validation_mode_field_shown_with_provenance() -> None:
    # worker evidence records validation_mode, not decision: mapping
    # keeps the source key visible (no silent renaming)
    artifact = {'schema': 1, 'stage': 'worker',
                'validation_mode': 'COMPLETE',
                'fallback_reason': 'NO_CHANGED_FILES'}
    out = ui.render_report(artifact)
    assert 'recorded as validation_mode' in out
    assert 'No changed files observed' in out    # fixed label mapping


def test_fallback_label_falls_back_to_raw_string() -> None:
    out = ui.render_report(_payload(fallback_reason='SOME_FUTURE_CODE'))
    assert 'SOME_FUTURE_CODE' in out
    mapped = ui.render_report(_payload(fallback_reason='PROVEN_DISJOINT'))
    assert 'Proven disjoint classification' in mapped


# ------------------------------------------------- missing field safety

def test_missing_fields_render_as_not_recorded() -> None:
    out = ui.render_report({'schema_version': 1})   # minimal artifact
    assert 'Not recorded' in out                    # repository/tree/etc
    assert 'Unknown' in out                         # decision/fallback
    assert 'No recorded file-set field in artifact' in out
    assert 'No recorded edge data in artifact' in out


def test_edge_topology_drawn_only_when_recorded() -> None:
    with_edges = _payload(edges=[{'source': 'a.py', 'target': 'b.py'}])
    out = ui.render_report(with_edges)
    assert 'Topology (recorded edges)' in out
    assert '<svg class="graph"' in out
    without = ui.render_report(_payload())
    assert 'impact-set view only' in without
    assert '<svg class="graph"' not in without      # nothing fabricated


def test_impact_sets_render_verbatim() -> None:
    out = ui.render_report(_payload())
    assert 'pkg/mod.py' in out                      # changed_files shown
    assert 'affected_files' in out or 'Changed files' in out
    assert 'Asha inspection report' in out


# ------------------------------------------------- offline integrity

class _AssetScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        mapping = dict(attrs)
        if tag == 'script':
            self.problems.append('script tag present')
        if tag == 'link':
            self.problems.append('link tag present: ' + str(mapping))
        if tag in ('img', 'iframe', 'source'):
            source = mapping.get('src') or ''
            if source.startswith(('http://', 'https://', '//')):
                self.problems.append(f'external {tag} src: {source}')
        if tag == 'a':
            href = mapping.get('href') or ''
            if href.startswith(('http://', 'https://')):
                self.problems.append(f'external anchor: {href}')


def test_offline_integrity_zero_external_assets() -> None:
    html_text = ui.render_report(_payload())
    scanner = _AssetScanner()
    scanner.feed(html_text)
    assert scanner.problems == []
    lowered = html_text.lower()
    # raw-text sweep: network APIS and asset-fetching CSS constructs
    # only (URLs appearing as recorded TEXT data are content, not
    # references; active asset tags are covered by _AssetScanner)
    for banned in ('fetch(', 'xmlhttprequest', 'websocket', 'eventsource',
                   '@import', '@font-face', 'url(http', 'url(//',
                   'url(https', '<script', '<link', '<iframe'):
        # xmlns namespace URIs in inline SVG are identifiers, not
        # fetched assets; strip them before the network sweep
        sweep = lowered.replace('http://www.w3.org/2000/svg', '')
        assert banned not in sweep, banned
    assert 'font-family' in lowered
    assert 'ui-sans-serif, system-ui, sans-serif' in lowered


# ------------------------------------------------- CLI integration

def test_ui_flag_keeps_json_stdout_pure_and_reports_path_on_stderr(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', '.'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@example.invalid'],
                   cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'test'],
                   cwd=root, check=True)
    (root / 'a.py').write_text('x = 1\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'init'], cwd=root, check=True)
    (root / 'a.py').write_text('x = 2\n', encoding='utf-8')
    out_path = tmp_path / 'report.html'

    code = cli.main(['--root', str(root), '--json', '--no-execute',
                     '--ui', '--ui-out', str(out_path)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)      # stdout: pure schema v1 JSON
    assert payload['schema_version'] == 1
    assert payload['decision'] is not None  # pipeline unchanged by --ui
    assert 'report written' in captured.err  # path only on stderr
    assert out_path.is_file()
    assert '<script' not in out_path.read_text(encoding='utf-8')
    assert code == cli.EXIT_OK


def test_inspect_writes_default_path_offline(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture
                                             ) -> None:
    artifact = tmp_path / 'ev.json'
    artifact.write_text(json.dumps(_payload()), encoding='utf-8')
    monkeypatch.chdir(tmp_path)             # default .jspace/reports/...
    code = cli.main(['inspect', str(artifact)])
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    written = Path(captured.out.strip())
    expected = tmp_path / '.jspace' / 'reports' / 'inspector.html'
    assert written.is_file()            # relative to the chdir'd cwd
    assert expected.is_file()           # default location honored
    text = expected.read_text(encoding='utf-8')
    assert 'Asha inspection report' in text
    assert '<script' not in text


def test_inspect_non_interference_zero_git_and_governance_calls(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """`asha inspect` must not touch git or any governance module."""
    artifact = tmp_path / 'ev.json'
    artifact.write_text(json.dumps(_payload()), encoding='utf-8')

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError('non-interference violated')

    monkeypatch.setattr(subprocess, 'run', _boom)
    monkeypatch.setattr(scoping, 'assess_scoping_eligibility', _boom)
    monkeypatch.setattr(scope_resolver, 'resolve', _boom)
    monkeypatch.setattr(scope_resolver, 'changed_files', _boom)
    monkeypatch.setattr(codegraph, 'build_graph', _boom)
    monkeypatch.setattr(cli, '_git', _boom)

    code = cli.main(['inspect', str(artifact),
                     '--ui-out', str(tmp_path / 'out.html')])
    assert code == cli.EXIT_OK
    assert (tmp_path / 'out.html').is_file()
    assert 'COMPLETE' in (tmp_path / 'out.html').read_text(encoding='utf-8')


def test_inspect_bad_artifact_fails_closed(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture
                                           ) -> None:
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json', encoding='utf-8')
    code = cli.main(['inspect', str(bad)])
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert captured.out == ''
    assert 'inspect failed' in captured.err


@pytest.mark.skipif(not GATE_EVIDENCE.is_file(),
                    reason='gate evidence artifact not present')
def test_real_gate_artifact_renders_passively(
        tmp_path: Path) -> None:
    artifact = json.loads(GATE_EVIDENCE.read_text(encoding='utf-8'))
    before = copy.deepcopy(artifact)
    out = ui.render_report(artifact)
    assert artifact == before
    assert str(artifact['commit']) in out
    assert 'authorized_to_ship' in out
    assert 'ruff' in out and 'pytest' in out and 'mypy' in out
    assert 'No recorded edge data in artifact' in out
