"""Read-only visual inspector: pure payload -> self-contained HTML.

Strictly a passive presentation consumer. This module never calls git,
AST parsers, CodeGraph, scope_resolver, check_runner, scheduler, or any
governance module; it never recomputes closures, never re-derives
affected sets, and never fills a missing field with a calculated
substitute -- absent data is rendered literally as "Not recorded" /
"Unknown". Rendering is side-effect free: ``render_report(p)`` leaves
``p`` untouched and is byte-deterministic (``render(p) == render(
deepcopy(p))``). Output is air-gapped: inline styles and inline SVG
only, system font stacks, zero script tags, zero network references.
"""

from __future__ import annotations

import copy
import html
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# Fixed presentation labels for known fallback reasons. Unmapped strings
# fall back verbatim to the raw recorded value; missing -> Unknown.
FALLBACK_LABELS: dict[str, str] = {
    'PROVEN_SHARED': 'Proven shared classification',
    'PROVEN_DISJOINT': 'Proven disjoint classification',
    'UNKNOWN_CLASSIFICATION': 'Unknown classification (fail-closed)',
    'NO_CHANGED_FILES': 'No changed files observed',
    'FORBIDDEN_SCOPE_LEVEL': 'Scope level above ceiling',
    'INVALID_DECISION': 'Invalid decision (fail-closed)',
}

_NOT_RECORDED = 'Not recorded'
_UNKNOWN = 'Unknown'

# keys rendered as recorded impact sets (membership lists), in order;
# only keys actually present in the artifact produce a section
IMPACT_KEYS: tuple[tuple[str, str], ...] = (
    ('affected_files', 'Affected files'),
    ('changed_files', 'Changed files'),
    ('declared_scope', 'Declared scope'),
    ('observed_scope', 'Observed scope'),
    ('read_set', 'Read set'),
    ('write_set', 'Write set'),
)

VERIFY_KEYS: tuple[tuple[str, str], ...] = (
    ('verify_record', 'verify_record'),
    ('verify_bytes', 'verify_bytes'),
    ('replay_verification', 'replay_verification'),
    ('evidence_verification', 'evidence_verification'),
    ('authorized_to_ship', 'authorized_to_ship'),
    ('evidence_id', 'evidence_id'),
    ('evidence_sha256', 'evidence_sha256'),
)

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem; background: #0f1115; color: #e6e6e6;
  font-family: ui-sans-serif, system-ui, sans-serif; line-height: 1.5; }
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 2rem 0 .5rem; color: #9ecbff;
  text-transform: uppercase; letter-spacing: .06em; }
.meta { color: #9aa0a6; font-size: .85rem; }
.badge { display: inline-block; padding: .2rem .7rem; border-radius: .4rem;
  font-weight: 700; font-size: .95rem; }
.b-scoped { background: #10351f; color: #6fe3a1; border: 1px solid #2f7d4f; }
.b-complete { background: #3a2c0d; color: #f5c451; border: 1px solid #8a6a1d; }
.b-unknown { background: #2a2a2e; color: #b9b9c0; border: 1px solid #55555c; }
.b-pass { background: #10351f; color: #6fe3a1; border: 1px solid #2f7d4f; }
.b-fail { background: #3d1416; color: #ff8b8b; border: 1px solid #8c2f33; }
.b-none { background: #2a2a2e; color: #8a8a92; border: 1px solid #4a4a50;
  font-weight: 400; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: .35rem .55rem;
  border-bottom: 1px solid #262a31; vertical-align: top; }
th { color: #9aa0a6; font-weight: 600; }
code { font-family: ui-monospace, monospace; font-size: .85em; }
ul.files { list-style: none; margin: .25rem 0; padding: 0; }
ul.files li { font-family: ui-monospace, monospace; font-size: .85rem;
  padding: .18rem 0; border-bottom: 1px dotted #262a31; }
.note { color: #9aa0a6; font-style: italic; font-size: .85rem; }
pre.tail { background: #14171d; border: 1px solid #262a31; padding: .6rem;
  overflow-x: auto; font-size: .75rem; white-space: pre-wrap;
  word-break: break-all; }
.kv td:first-child { color: #9aa0a6; width: 16rem; }
svg.graph { background: #14171d; border: 1px solid #262a31;
  width: 100%; height: auto; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{_esc(text)}</span>'


def _decision_view(payload: Mapping[str, Any]) -> str:
    """Decision badge: only recorded fields; never inferred.

    Worker evidence records ``validation_mode`` instead of ``decision``;
    showing it under its OWN key name is field mapping, not synthesis.
    """
    for key in ('decision', 'validation_mode'):
        if key in payload and payload[key] is not None:
            value = str(payload[key])
            cls = ('b-scoped' if value == 'SCOPED'
                   else 'b-complete' if value == 'COMPLETE'
                   else 'b-unknown')
            suffix = (f' <span class="note">(recorded as {key})</span>'
                      if key != 'decision' else '')
            return _badge(value, cls) + suffix
    return _badge(_UNKNOWN, 'b-unknown') + (
        ' <span class="note">(no decision field in artifact)</span>')


def _verify_badge(payload: Mapping[str, Any], key: str) -> str:
    if key not in payload or payload[key] is None:
        return _badge(_NOT_RECORDED, 'b-none')
    value = payload[key]
    if isinstance(value, bool):
        return _badge('PASS' if value else 'FAIL',
                      'b-pass' if value else 'b-fail')
    text = str(value)
    cls = ('b-pass' if text.upper() == 'PASS'
           else 'b-fail' if text.upper() == 'FAIL' else 'b-none')
    return _badge(text, cls)


def _impact_section(payload: Mapping[str, Any]) -> str:
    parts: list[str] = ['<h2>Verified impact set</h2>']
    shown = False
    for key, label in IMPACT_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            continue
        shown = True
        items = ''.join(f'<li>{_esc(item)}</li>' for item in value)
        empty = '' if value else ' <span class="note">(empty)</span>'
        parts.append(f'<h3>{_esc(label)} '
                     f'<span class="note">({len(value)} recorded){empty}'
                     '</span></h3>')
        parts.append(f'<ul class="files">{items}</ul>'
                     if value else '')
    if not shown:
        parts.append(
            '<p class="note">No recorded file-set field in artifact '
            '(affected_files / changed_files / declared_scope / '
            'observed_scope / read_set / write_set)</p>')
    # topology: draw ONLY recorded edges; never invent connections
    edges = payload.get('edges')
    if isinstance(edges, Sequence) and not isinstance(edges, (str, bytes)) \
            and edges:
        parts.append(_render_edges(edges, payload))
    else:
        parts.append('<p class="note">No recorded edge data in artifact '
                     '-- impact-set view only (no dependency graph is '
                     'fabricated).</p>')
    return ''.join(parts)


def _render_edges(edges: Sequence[Any], payload: Mapping[str, Any]) -> str:
    """Render a graph strictly from recorded edge records."""
    pairs: list[tuple[str, str]] = []
    for edge in edges:
        if isinstance(edge, Mapping):
            src = edge.get('source', edge.get('from'))
            dst = edge.get('target', edge.get('to'))
            if src is None or dst is None:
                continue
            pairs.append((str(src), str(dst)))
        elif isinstance(edge, Sequence) and len(edge) == 2 \
                and not isinstance(edge, (str, bytes)):
            pairs.append((str(edge[0]), str(edge[1])))
    if not pairs:
        return ('<p class="note">Edge records present but none carried '
                'two renderable endpoints.</p>')
    order: list[str] = []
    for src, dst in pairs:
        for node in (src, dst):
            if node not in order:
                order.append(node)
    width = 760
    row_h = 46
    height = 40 + row_h * max(len(order), 1)
    pos = {node: (170 + i * 380, 30 + i * row_h)
           for i, node in enumerate(order)}
    lines = []
    for src, dst in pairs:
        x1, y1 = pos[src]
        x2, y2 = pos[dst]
        lines.append(
            f'<line x1="{x1 + 150}" y1="{y1 + 14}" x2="{x2}" y2="{y2 + 14}" '
            'stroke="#5b8fd9" stroke-width="1.5" marker-end="url(#arrow)"/>')
    boxes = []
    for node in order:
        x, y = pos[node]
        boxes.append(
            f'<rect x="{x}" y="{y}" width="150" height="28" rx="5" '
            'fill="#1b2430" stroke="#3d5a80"/>'
            f'<text x="{x + 8}" y="{y + 19}" fill="#d7e3f4" '
            'font-family="ui-monospace, monospace" font-size="12">'
            f'{_esc(node[:20])}</text>')
    return ('<h2>Topology (recorded edges)</h2>'
            f'<svg class="graph" viewBox="0 0 {width} {height}" '
            'xmlns="http://www.w3.org/2000/svg" role="img" '
            'aria-label="recorded edge topology">'
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" '
            'refY="5" markerWidth="6" markerHeight="6" orient="auto">'
            '<path d="M 0 0 L 10 5 L 0 10 z" fill="#5b8fd9"/></marker>'
            '</defs>' + ''.join(lines) + ''.join(boxes) + '</svg>')


def _checks_table(payload: Mapping[str, Any]) -> str:
    checks = payload.get('checks')
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)) \
            or not checks:
        return ''
    rows = []
    for entry in checks:
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get('status', _NOT_RECORDED))
        cls = ('b-pass' if status == 'passed'
               else 'b-fail' if status == 'failed' else 'b-none')
        rows.append(
            '<tr>'
            f'<td><code>{_esc(entry.get("name", _NOT_RECORDED))}</code></td>'
            f'<td>{_badge(status, cls)}</td>'
            f'<td>{_esc(entry.get("exit_code", _NOT_RECORDED))}</td>'
            f'<td>{_esc(entry.get("duration_ms", _NOT_RECORDED))} ms</td>'
            f'<td>{_esc(entry.get("scope", _NOT_RECORDED))}</td>'
            '</tr>')
    if not rows:
        return ''
    return ('<h2>Validation checks (recorded)</h2>'
            '<table><tr><th>Check</th><th>Status</th><th>Exit</th>'
            '<th>Duration</th><th>Scope</th></tr>'
            + ''.join(rows) + '</table>')


def _render_watch(data: Mapping[str, Any]) -> str:
    """LIVE / PREVIEW layout (watch mode): observation facts plus the
    recorded LAST SEALED RUN. Renders NO verdict section at all -- a
    preview must never look like a current governance result."""
    watch = data.get('watch')
    if not isinstance(watch, Mapping):
        watch = {}
    sealed = data.get('last_sealed_run')
    if not isinstance(sealed, Mapping):
        sealed = {}
    state = str(watch.get('state') or 'UNKNOWN')
    files = watch.get('files')
    file_items = ''
    if isinstance(files, Sequence) and not isinstance(files, (str, bytes)):
        file_items = ''.join(f'<li>{_esc(item)}</li>' for item in files)
    states = watch.get('states')
    state_rows = ''
    if isinstance(states, Mapping):
        state_rows = ''.join(
            f'<tr><td><code>{_esc(path)}</code></td>'
            f'<td><code>{_esc(", ".join(str(t) for t in tags))}</code></td>'
            '</tr>'
            for path, tags in sorted(states.items()))
    sealed_rows = ''.join(
        f'<tr><td><code>{_esc(key)}</code></td>'
        f'<td>{_esc(value)}</td></tr>'
        for key, value in sealed.items()) or (
        '<tr><td colspan="2" class="note">No sealed run recorded'
        '</td></tr>')
    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
        ('<meta http-equiv="refresh" content="1">'
         '<title>Asha watch (LIVE / PREVIEW)</title>'),
        f'<style>{_STYLE}</style></head><body><main>',
        '<h1>Asha Watch</h1>',
        ('<p class="meta">LIVE / PREVIEW &mdash; '
         '<strong>non-authoritative</strong> &middot; '
         'filesystem observation only; no governance evaluation has run. '
         'A run happens only on an explicit user trigger.</p>'),
        '<h2>Observation (LIVE)</h2><p>',
        _badge(state, 'b-none'),
        f' &nbsp; branch: <code>{_esc(watch.get("branch") or _NOT_RECORDED)}</code>',
        '</p>',
        '<h3>Observed files (preview)</h3>',
        f'<ul class="files">{file_items}</ul>' if file_items
        else '<p class="note">no observed changes</p>',
        '<h3>Per-path states (observed)</h3>' if state_rows else '',
        f'<table><tr><th>Path</th><th>States</th></tr>{state_rows}</table>'
        if state_rows else '',
        '<h2>LAST SEALED RUN (recorded, previous)</h2>',
        '<table class="kv">'
        '<tr><th>Recorded field</th><th>Value</th></tr>'
        + sealed_rows + '</table>',
        '</main></body></html>',
    ]
    return ''.join(part for part in parts if part)


def render_report(payload: Mapping[str, Any]) -> str:
    """Pure renderer: payload in, self-contained HTML out. No mutation."""
    if not isinstance(payload, Mapping):
        raise TypeError('render_report expects a mapping payload')
    data: Mapping[str, Any] = copy.deepcopy(payload)
    if 'watch' in data:
        return _render_watch(data)

    repository = data.get('repository')
    header = [
        '<h1>Asha inspection report</h1>',
        ('<p class="meta">'
         f'repository: <code>{_esc(repository if repository is not None else _NOT_RECORDED)}</code>'),
    ]
    change_set = data.get('change_set')
    tree_bits = []
    if isinstance(change_set, Mapping):
        for key in ('target_sha', 'base_tree_sha', 'tree_hash', 'base_sha'):
            if change_set.get(key):
                tree_bits.append(f'{key}={change_set[key]}')
    for key in ('tree_hash', 'target_tree_sha', 'base_tree_sha', 'commit'):
        if data.get(key):
            tree_bits.append(f'{key}={data[key]}')
    header.append(
        '<br>tree: <code>' + (
            _esc(', '.join(tree_bits)) if tree_bits
            else _esc(_NOT_RECORDED)) + '</code>')
    duration = data.get('duration_ms')
    header.append(
        '<br>duration: ' + (
            f'{_esc(duration)} ms' if duration is not None
            else f'<span class="note">{_NOT_RECORDED}</span>'))
    mode = data.get('execution_mode')
    if mode is None and data.get('status') is not None:
        mode = data.get('status')
    header.append(
        '<br>execution mode: ' + (
            _esc(mode) if mode is not None
            else f'<span class="note">{_NOT_RECORDED}</span>'))
    header.append('</p>')

    fallback = data.get('fallback_reason')
    if fallback is None:
        fallback_text = f'<span class="note">{_UNKNOWN}</span>'
    else:
        raw = str(fallback)
        fallback_text = _esc(FALLBACK_LABELS.get(raw, raw))
    eligible = data.get('eligible')
    eligible_text = ('true' if eligible is True else 'false'
                     if eligible is False else _NOT_RECORDED)

    verify_rows = ''.join(
        f'<tr><td><code>{_esc(label)}</code></td>'
        f'<td>{_verify_badge(data, key)}</td></tr>'
        for key, label in VERIFY_KEYS)

    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
        ('<meta name="viewport" content="width=device-width, '
         'initial-scale=1"><title>Asha inspection report</title>'),
        f'<style>{_STYLE}</style></head><body><main>',
        ''.join(header),
        '<h2>Decision</h2><p>' + _decision_view(data)
        + f' &nbsp; eligible: <code>{_esc(eligible_text)}</code></p>',
        '<p class="meta">fallback reason: ' + fallback_text + '</p>',
        _impact_section(data),
        '<h2>Verification &amp; replay</h2>'
        '<table class="kv">' + verify_rows + '</table>',
        _checks_table(data),
        '</main></body></html>',
    ]
    return ''.join(parts)


def write_report(payload: Mapping[str, Any],
                 out_path: Path | str | None = None) -> Path:
    """Render and write to the default (or given) path; creating parent
    directories. Presentation-only I/O -- no governance side effects."""
    target = Path(out_path) if out_path is not None else Path(
        '.jspace/reports/inspector.html')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(payload), encoding='utf-8')
    return target
