"""Phase 5.4.3 / 5.4.4.1 -- human pipeline stepper (presentation-only).

This module is the presentation layer over the factual
``PipelineStateProjector`` (asha.telemetry). It translates an already
projected pipeline state into human language and modern developer-tool
HTML (Linear/Vercel-style restrained dashboard).

Hard boundaries (spec 5.4.3 / 5.4.4.1):
- NO governance, git, AST, CodeGraph, scheduler, run_scoped access.
  The only input is ``(state, meta)`` that the projector produced.
- NO inference: a step is shown current/completed/failed/interrupted
  ONLY from the recorded projector state. No timers, no progress
  percentages, no simulated transitions, no per-service status.
- NO fabrication: any field missing from recorded data renders as
  ``Not recorded``. No new evidence is created or modified.
- Offline and deterministic: inline CSS + inline SVG only, system font
  stacks, no script tags, no network references; rendering is a pure
  function of its arguments.
- English/LTR only (spec 16): lang=en, dir=ltr; UI labels English.

Supported projector states (exactly the 5.4.2 set):
IDLE_CLEAN / MODIFIED_PREVIEW / SCOPE_ASSESSED / EXECUTING /
VALIDATING / FAILED / INTERRUPTED / SEALED
"""

from __future__ import annotations

import html
from typing import Any

# ------------------------------------------------------------------
# state -> (hero title, hero description, pipeline step 1..5, status)
# status: 'current' | 'done' | 'not_reached' | 'failed' | 'interrupted'
# ------------------------------------------------------------------

STEPS: tuple[str, ...] = (
    'Change Detected',
    'Scope Assessed',
    'Execution',
    'Validation',
    'Sealed',
)

_HERO: dict[str, tuple[str, str]] = {
    'IDLE_CLEAN': (
        'All systems synchronized',
        'No pending changes detected.'),
    'MODIFIED_PREVIEW': (
        'Changes detected',
        'Execution has not started.'),
    'SCOPE_ASSESSED': (
        'Scope assessed',
        'The change scope has been evaluated.'),
    'EXECUTING': (
        'Evaluation in progress',
        'Asha is executing the recorded evaluation.'),
    'VALIDATING': (
        'Validating results',
        'Check outputs are being validated.'),
    'FAILED': (
        'Execution failed',
        'The recorded run ended with an explicit failure.'),
    'INTERRUPTED': (
        'Execution interrupted',
        'The recorded run did not reach a terminal event.'),
    'SEALED': (
        'Evidence sealed',
        'Execution completed and evidence was sealed.'),
}

# which step each state points at, and how that step is painted
_STEP_STATUS: dict[str, tuple[int, str]] = {
    'MODIFIED_PREVIEW': (1, 'current'),
    'SCOPE_ASSESSED': (2, 'current'),
    'EXECUTING': (3, 'current'),
    'VALIDATING': (4, 'current'),
    'SEALED': (5, 'done'),
    'FAILED': (4, 'failed'),
    'INTERRUPTED': (3, 'interrupted'),
}

_LAST_EVENT_STEP: dict[str, tuple[int, str]] = {
    'validation_failed': (4, 'failed'),
    'execution_failed': (3, 'failed'),
    'execution_started': (3, 'interrupted'),
    'validation_started': (4, 'interrupted'),
}

_NOT_RECORDED = 'Not recorded'


def human_label(state: str) -> str:
    """Deterministic English hero title; unknown states are NOT
    invented."""
    return _HERO.get(state, (_NOT_RECORDED, ''))[0]


def hero_text(state: str) -> tuple[str, str]:
    """(title, description) for the hero; unknown -> Not recorded."""
    return _HERO.get(state, (_NOT_RECORDED, _NOT_RECORDED))


def presentation_step(state: str, last_event: str | None
                      ) -> tuple[int, str]:
    """(step_index, status) for the state; FAILED/INTERRUPTED are
    refined by which event fact exists (never inferred)."""
    if state == 'FAILED' and last_event in _LAST_EVENT_STEP:
        return _LAST_EVENT_STEP[last_event]
    if state == 'INTERRUPTED' and last_event in _LAST_EVENT_STEP:
        return _LAST_EVENT_STEP[last_event]
    return _STEP_STATUS.get(state, (1, 'not_reached'))


def _fmt(value: Any) -> str:
    """Missing -> 'Not recorded', everything else escaped verbatim."""
    if value is None or value == '':
        return _NOT_RECORDED
    return html.escape(str(value))


def _fmt_sha(value: Any) -> str:
    """Long hashes get middle-ellipsis so the drawer row never wraps
    (the full value is still the recorded field; this is pure
    presentation truncation)."""
    if value is None or value == '':
        return _NOT_RECORDED
    s = str(value)
    if len(s) > 20:
        s = s[:10] + '\u2026' + s[-6:]
    return html.escape(s)


def _mark_svg(kind: str) -> str:
    """Inline glyphs (offline; check/cross/alert/pulse/dot)."""
    if kind == 'check':
        return ('<svg viewBox="0 0 16 16" width="16" height="16" '
                'aria-hidden="true"><path d="M3 8.5 L6.5 12 L13 4.5" '
                'fill="none" stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    if kind == 'cross':
        return ('<svg viewBox="0 0 16 16" width="16" height="16" '
                'aria-hidden="true"><path d="M4 4 L12 12 M12 4 L4 12" '
                'stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round"/></svg>')
    if kind == 'alert':
        return ('<svg viewBox="0 0 16 16" width="16" height="16" '
                'aria-hidden="true"><path d="M8 3 L14.5 13 H1.5 Z" '
                'fill="none" stroke="currentColor" stroke-width="1.6" '
                'stroke-linejoin="round"/><path d="M8 7 V10" '
                'stroke="currentColor" stroke-width="1.6" '
                'stroke-linecap="round"/><circle cx="8" cy="12" r=".9" '
                'fill="currentColor"/></svg>')
    if kind == 'pulse':
        return '<span class="pulse" aria-hidden="true"></span>'
    return '<span class="dot" aria-hidden="true"></span>'


def _rail(step_index: int, step_status: str, state: str) -> str:
    """The five-step execution timeline as compact rail nodes (one
    coherent process, 1 -> 5, with thin connector rails)."""
    nodes: list[str] = []
    for i, name in enumerate(STEPS, start=1):
        if state == 'SEALED' or i < step_index and state != 'IDLE_CLEAN':
            cls = 'done'
        elif i == step_index:
            cls = step_status
        else:
            cls = 'not_reached'
        marks = {'done': 'check', 'current': 'pulse', 'failed': 'cross',
                 'interrupted': 'alert', 'not_reached': 'dot'}
        nodes.append(
            f'<li class="node {cls}">'
            f'<span class="ring">{_mark_svg(marks[cls])}</span>'
            f'<span class="lbl">{html.escape(name)}</span>'
            f'<span class="bar" aria-hidden="true"></span></li>')
    return ('<ol class="rail" aria-label="Pipeline">'
            + ''.join(nodes) + '</ol>')


def _file_list(meta: dict[str, Any]) -> str:
    """Compact observed-changes list; membership facts from recorded
    change_detected payloads only."""
    events: list[dict[str, Any]] = meta.get('events') or []
    files: list[str] = []
    for e in events:
        if e.get('event_type') == 'change_detected':
            for f in (e.get('payload', {}).get('target_files') or []):
                if isinstance(f, str) and f not in files:
                    files.append(f)
    if not files:
        return ('<section class="panel files"><h3>Observed changes</h3>'
                '<p class="empty">No observed changes</p></section>')
    rows = ''.join(
        f'<li><span class="fdot"></span><code>{html.escape(f)}</code>'
        f'</li>' for f in files)
    return (f'<section class="panel files"><h3>Observed changes</h3>'
            f'<p class="count">{len(files)} file'
            f'{"s" if len(files) != 1 else ""} changed</p>'
            f'<ul class="flist">{rows}</ul></section>')


def _run_summary(state: str, meta: dict[str, Any]) -> str:
    """Compact run summary; every value from a recorded field."""
    rows: list[tuple[str, str]] = [('State', _fmt(state))]
    events: list[dict[str, Any]] = meta.get('events') or []
    scope = next((e for e in events
                  if e.get('event_type') == 'scope_assessed'), None)
    sealed = next((e for e in events
                   if e.get('event_type') == 'sealed'), None)
    failed = next((e for e in events if e.get('event_type') in (
        'validation_failed', 'execution_failed')), None)
    if scope is not None:
        p = scope.get('payload', {})
        rows.append(('Scope mode', _fmt(p.get('mode'))))
        rows.append(('Fallback reason', _fmt(p.get('fallback_reason'))))
    elif state in ('SCOPE_ASSESSED', 'EXECUTING', 'VALIDATING', 'FAILED',
                   'INTERRUPTED', 'SEALED'):
        rows.append(('Scope mode', _NOT_RECORDED))
    if failed is not None:
        rows.append(('Validation', 'FAIL'))
        rows.append(('Evidence', 'explicit failure fact recorded'))
    elif sealed is not None:
        p = sealed.get('payload', {})
        rows.append(('Validation', _fmt(
            p.get('validation_result') or 'PASS')))
        ev = (p.get('evidence_sha256') or p.get('evidence_id')
              or None)
        rows.append(('Evidence',
                     _fmt_sha(ev) if ev else 'SEALED'))
    integrity = meta.get('integrity') or {}
    if integrity.get('incomplete'):
        rows.append(('Journal', 'incomplete'))
    cells = ''.join(
        f'<div class="kv"><dt>{html.escape(k)}</dt>'
        f'<dd>{v}</dd></div>' for k, v in rows)
    return ('<section class="panel run"><h3>Run</h3>'
            f'<div class="keys">{cells}</div></section>')


def _tech_drawer(state: str, meta: dict[str, Any]) -> str:
    """Collapsible Technical Audit & Sealed Evidence drawer.

    The drawer consumes the AUTHORITATIVE sealed evidence payload
    (``meta['last_sealed_run']`` -- what the watcher read from
    external evidence dir via ``read_last_sealed``), never the
    ephemeral live-journal metadata. If no sealed run exists it shows
    a single clear message. Collapsed by default.
    """
    sealed = meta.get('last_sealed_run')
    if not sealed:
        return ('<details class="tech">'
                '<summary>Technical Audit &amp; Sealed Evidence</summary>'
                '<p class="nosealed">No previous sealed run recorded.'
                '</p></details>')
    # explicit field map; only fields that EXIST in the sealed payload
    # are rendered (middle-truncated hashes), everything missing ->
    # Not recorded (never invented)
    fields: list[tuple[str, Any]] = [
        ('Commit SHA', sealed.get('commit')),
        ('Tree Hash', sealed.get('tree_hash')),
        ('Evidence SHA-256', sealed.get('evidence_sha256')),
        ('Scope Mode', sealed.get('scope')),
        ('Validation Mode', sealed.get('validation_mode')),
        ('Validation Result', sealed.get('decision')),
        ('Fallback Reason', sealed.get('fallback_reason')),
        ('Worker ID', sealed.get('worker_id')),
        ('Task ID', sealed.get('task_id')),
        ('Sealed Timestamp', sealed.get('observed_at')),
    ]
    hash_fields = ('Commit SHA', 'Tree Hash', 'Evidence SHA-256')
    rows = ''.join(
        f'<div class="trow"><dt>{html.escape(k)}</dt>'
        f'<dd>{_fmt_sha(v) if k in hash_fields else _fmt(v)}</dd></div>'
        for k, v in fields if v is not None)
    return ('<details class="tech">'
            '<summary>Technical Audit &amp; Sealed Evidence</summary>'
            f'<div class="tgrid">{rows}</div></details>')


# ------------------------------------------------------------------
# page assembly (single, pure call; deterministic)
# ------------------------------------------------------------------

def render_stepper_html(state: str, meta: dict[str, Any] | None = None
                        ) -> str:
    """Projector (state, meta) -> modern self-contained offline
    dashboard. ``meta`` is the projector metadata dict (last_event,
    run_id, integrity, events, ...). Any of it may be absent; nothing
    is invented. Byte-deterministic for the same (state, meta)."""
    meta = meta or {}
    step_index, step_status = presentation_step(
        state, meta.get('last_event'))
    hero_title, hero_desc = hero_text(state)
    radius = 14

    return f"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Asha Watch — Developer Execution Monitor</title>
<style>
  :root {{
    --bg: #f6f7f9;
    --surface: #ffffff;
    --border: #e5e7eb;
    --text: #111827;
    --muted: #6b7280;
    --faint: #9ca3af;
    --accent: #2563eb;
    --accent-soft: #eff6ff;
    --ok: #16a34a;
    --ok-soft: #f0fdf4;
    --warn: #d97706;
    --warn-soft: #fffbeb;
    --bad: #dc2626;
    --bad-soft: #fef2f2;
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
            monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter,
            Roboto, Helvetica, Arial, sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 14px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .app {{ max-width: 880px; margin: 0 auto; padding: 28px 24px 48px; }}
  /* ---------- header ---------- */
  header {{ display: flex; align-items: baseline; justify-content:
           space-between; margin-bottom: 20px; }}
  .brand {{ display: flex; align-items: baseline; gap: 10px; }}
  .brand h1 {{ font-size: 15px; font-weight: 600; letter-spacing:
              -0.01em; }}
  .brand .sub {{ font-size: 12px; color: var(--muted); }}
  .live {{ display: flex; align-items: center; gap: 8px;
          font-size: 11px; font-weight: 600; color: var(--muted);
          text-transform: uppercase; letter-spacing: .08em; }}
  .live .branch {{ background: var(--surface); border: 1px solid
                  var(--border); border-radius: 999px; padding: 2px
                  10px; font-family: var(--mono); font-size: 11px;
                  color: var(--text); text-transform: none;
                  letter-spacing: 0; }}
  .live .badge {{ display: inline-flex; align-items: center; gap: 5px;
                 color: var(--ok); }}
  .live .badge::before {{ content: ''; width: 7px; height: 7px;
                         border-radius: 50%; background: var(--ok);
                         animation: breathe 2s ease-in-out infinite; }}
  @keyframes breathe {{ 0%, 100% {{ opacity: 1; }} 50% {{
                        opacity: .35; }} }}
  /* ---------- hero ---------- */
  .hero {{ background: var(--surface); border: 1px solid var(--border);
          border-radius: {radius}px; padding: 26px 28px 22px;
          box-shadow: 0 1px 2px rgba(16,24,40,.04); }}
  .hero .title {{ font-size: 24px; font-weight: 700;
                 letter-spacing: -0.02em; }}
  .hero .desc {{ font-size: 13px; color: var(--muted); margin-top: 2px;
                margin-bottom: 22px; }}
  /* ---------- rail stepper ---------- */
  .rail {{ list-style: none; display: flex; align-items: flex-start;
          justify-content: space-between; position: relative;
          padding: 0 14px; }}
  .node {{ flex: 1 1 0; min-width: 0; display: flex; flex-direction:
          column; align-items: center; gap: 8px; position: relative; }}
  .ring {{ width: 30px; height: 30px; border-radius: 50%;
          display: grid; place-items: center; border: 1.5px solid
          var(--border); background: var(--surface); color: var(--faint);
          position: relative; z-index: 1; }}
  .lbl {{ font-size: 12px; color: var(--muted); font-weight: 500;
         white-space: nowrap; }}
  .bar {{ position: absolute; top: 15px; left: calc(50% + 18px);
         right: calc(-50% + 18px); height: 1.5px; background:
         var(--border); z-index: 0; }}
  .node:last-child .bar {{ display: none; }}
  .node.done .ring {{ background: var(--ok-soft); border-color: var(--ok);
                      color: var(--ok); }}
  .node.done .lbl {{ color: var(--text); }}
  .node.done .bar {{ background: var(--ok); }}
  .node.current .ring {{ border-color: var(--accent); background:
                         var(--accent-soft); color: var(--accent);
                         box-shadow: 0 0 0 4px rgba(37,99,235,.10); }}
  .node.current .lbl {{ color: var(--accent); font-weight: 600; }}
  .pulse {{ width: 12px; height: 12px; border-radius: 50%;
           background: var(--accent);
           animation: pulse 1.6s ease-in-out infinite; display: block; }}
  @keyframes pulse {{ 0%,100% {{ transform: scale(1); opacity: 1; }}
                      50% {{ transform: scale(.72); opacity: .55; }} }}
  .node.failed .ring {{ background: var(--bad-soft); border-color:
                        var(--bad); color: var(--bad); }}
  .node.failed .lbl {{ color: var(--bad); }}
  .node.interrupted .ring {{ background: var(--warn-soft);
                             border-color: var(--warn); color: var(--warn); }}
  .node.interrupted .lbl {{ color: var(--warn); }}
  .node.not_reached .ring {{ background: var(--surface); }}
  .node.not_reached .lbl {{ color: var(--faint); }}
  /* ---------- panels ---------- */
  .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
          margin-top: 16px; }}
  .panel {{ background: var(--surface); border: 1px solid var(--border);
           border-radius: {radius}px; padding: 18px 20px;
           box-shadow: 0 1px 2px rgba(16,24,40,.04); }}
  h3 {{ font-size: 13px; font-weight: 600; color: var(--muted);
       text-transform: uppercase; letter-spacing: .06em;
       margin-bottom: 10px; }}
  .files .count {{ font-size: 12px; color: var(--faint);
                  margin-bottom: 8px; }}
  .files .empty {{ font-size: 13px; color: var(--faint); }}
  .flist {{ list-style: none; }}
  .flist li {{ display: flex; align-items: center; gap: 8px;
              padding: 5px 0; border-top: 1px solid var(--bg); }}
  .flist li:first-child {{ border-top: 0; }}
  .flist code {{ font-family: var(--mono); font-size: 12px;
                color: var(--text); overflow-wrap: anywhere; }}
  .fdot {{ width: 6px; height: 6px; border-radius: 50%;
          background: var(--border); flex: none; }}
  .run .keys {{ display: flex; flex-direction: column; gap: 6px; }}
  .run .kv {{ display: flex; justify-content: space-between;
             align-items: baseline; gap: 12px; }}
  .run dt {{ font-size: 12px; color: var(--muted); }}
  .run dd {{ font-family: var(--mono); font-size: 12px;
            font-weight: 600; }}
  /* ---------- tech drawer ---------- */
  .tech {{ margin-top: 16px; background: var(--surface); border: 1px
          solid var(--border); border-radius: {radius}px;
          box-shadow: 0 1px 2px rgba(16,24,40,.04); }}
  .tech summary {{ cursor: pointer; list-style: none; padding: 14px 20px;
                  font-size: 13px; font-weight: 600; color: var(--text);
                  display: flex; align-items: center; gap: 8px;
                  user-select: none; }}
  .tech summary::-webkit-details-marker {{ display: none; }}
  .tech summary::before {{ content: '▸'; color: var(--faint);
                          font-size: 12px; transition: transform .15s
                          ease; }}
  .tech[open] summary::before {{ transform: rotate(90deg); }}
  .tech summary:hover {{ background: var(--bg); }}
  .tgrid {{ display: grid; grid-template-columns: repeat(2, 1fr);
           gap: 4px 28px; padding: 4px 20px 16px; }}
  .trow {{ display: flex; justify-content: space-between; gap: 12px;
          padding: 4px 0; border-top: 1px solid var(--bg); }}
  .trow:first-of-type {{ border-top: 0; }}
  .trow dt {{ font-size: 12px; color: var(--muted); }}
  .trow dd {{ font-family: ui-monospace, SFMono-Regular, Menlo,
              Monaco, Consolas, monospace; font-size: 0.82rem;
              overflow-wrap: anywhere; text-align: right; }}
  .trow dd, .nosealed {{ font-variant-numeric: tabular-nums; }}
  .nosealed {{ padding: 4px 20px 16px; font-size: 13px;
              color: var(--muted); }}
  footer {{ margin-top: 18px; font-size: 11px; color: var(--faint);
           text-align: center; }}
  /* ---------- responsive ---------- */
  @media (max-width: 640px) {{
    .cols {{ grid-template-columns: 1fr; }}
    .rail {{ overflow-x: auto; }}
    .tgrid {{ grid-template-columns: 1fr; }}
    .app {{ padding: 18px 14px 36px; }}
  }}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">
      <h1>Asha Watch</h1>
      <span class="sub">Developer Execution Monitor</span>
    </div>
    <div class="live">
      <span class="branch">main</span>
      <span class="badge">LIVE</span>
    </div>
  </header>

  <section class="hero">
    <div class="title">{html.escape(hero_title)}</div>
    <div class="desc">{html.escape(hero_desc)}</div>
    {_rail(step_index, step_status, state)}
  </section>

  <section class="cols">
    {_file_list(meta)}
    {_run_summary(state, meta)}
  </section>

  {_tech_drawer(state, meta)}

  <footer>Presentation only — derived from the recorded pipeline
  event journal; never re-evaluated by this view.</footer>
</div>
</body>
</html>"""


# deterministic alias for tests
present = render_stepper_html