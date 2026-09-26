"""Phase 5.4.3 -- human pipeline stepper (presentation-only).

This module is the presentation layer over the factual
``PipelineStateProjector`` (asha.telemetry). It translates an already
projected pipeline state into human language and visual stepper HTML.

Hard boundaries (spec 5.4.3):
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

Supported projector states (exactly the 5.4.2 set):
IDLE_CLEAN / MODIFIED_PREVIEW / SCOPE_ASSESSED / EXECUTING /
VALIDATING / FAILED / INTERRUPTED / SEALED
"""

from __future__ import annotations

import html
from typing import Any

# ------------------------------------------------------------------
# state -> (human label, pipeline step index 1..5, step status)
# step status: 'current' | 'done' | 'not_reached' | 'failed' |
#              'interrupted'
# ------------------------------------------------------------------

STEPS: tuple[str, ...] = (
    'Change Detected',
    'Scope Assessed',
    'Execution',
    'Validation',
    'Sealed',
)

_HUMAN: dict[str, str] = {
    'IDLE_CLEAN': 'همه‌چیز مرتب و همگام است',
    'MODIFIED_PREVIEW': 'تغییرات شناسایی شده‌اند؛ هنوز اجرا نشده است',
    'SCOPE_ASSESSED': 'دامنهٔ تغییرات ارزیابی شده است',
    'EXECUTING': 'ارزیابی در حال اجراست',
    'VALIDATING': 'نتیجه در حال اعتبارسنجی است',
    'FAILED': 'اجرای ثبت‌شده با خطا پایان یافته است',
    'INTERRUPTED': 'اجرای ثبت‌شده کامل نشده است',
    'SEALED': 'شواهد اجرا مهروموم شده‌اند',
}

# which step each state points at, and how that step is painted
_STEP_STATUS: dict[str, tuple[int, str]] = {
    'MODIFIED_PREVIEW': (1, 'current'),
    'SCOPE_ASSESSED': (2, 'current'),
    'EXECUTING': (3, 'current'),
    'VALIDATING': (4, 'current'),
    'SEALED': (5, 'done'),
    'FAILED': (4, 'failed'),        # refined by last_event below
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
    """Deterministic Persian label; unknown states are NOT invented."""
    return _HUMAN.get(state, 'Not recorded')


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


# ------------------------------------------------------------------
# stepper HTML (single, pure call)
# ------------------------------------------------------------------

def render_stepper_html(state: str, meta: dict[str, Any] | None = None
                        ) -> str:
    """Projector (state, meta) -> self-contained offline stepper HTML.

    ``meta`` is the projector metadata dict (last_event, run_id,
    integrity, ...). Any of it may be absent; nothing is invented.
    """
    meta = meta or {}
    integrity: dict[str, Any] = meta.get('integrity') or {}
    last_event: str | None = meta.get('last_event')
    run_id: str | None = meta.get('run_id')
    step_index, step_status = presentation_step(state, last_event)

    # status classes on the 5 steps
    step_spans: list[str] = []
    for i, name in enumerate(STEPS, start=1):
        if i < step_index and state != 'IDLE_CLEAN':
            cls = 'done'
        elif i == step_index:
            cls = step_status
        else:
            cls = 'not_reached'
        if state == 'SEALED':
            cls = 'done'
        marker = {
            'done': '✓',
            'current': '●',
            'failed': '✗',
            'interrupted': '⚠',
        }.get(cls, '○')
        step_spans.append(
            f'<div class="step {cls}"><span class="mark">{marker}</span>'
            f'<span class="name">{html.escape(name)}</span></div>')

    details_rows = [
        ('Run ID', _fmt(run_id)),
        ('State', _fmt(state)),
        ('Last event', _fmt(last_event)),
    ]
    # ----- progressive disclosure: explicit event-type -> fields map.
    # Each field is read ONLY from the event type that authoritatively
    # carries it; a missing event/field renders 'Not recorded'. No
    # 'last value wins' fold across arbitrary payloads.
    event_rows: list[tuple[str, str]] = []
    events: list[dict[str, Any]] = meta.get('events') or []
    for ev in events:
        etype = ev.get('event_type')
        if etype == 'scope_assessed':
            event_rows.append(('Scope mode',
                               _fmt(ev.get('payload', {})
                                    .get('mode'))))
            event_rows.append(('Fallback reason',
                               _fmt(ev.get('payload', {})
                                    .get('fallback_reason'))))
        elif etype == 'validation_failed':
            event_rows.append(('Validation failure',
                               'explicit failure fact recorded'))
        elif etype == 'execution_failed':
            event_rows.append(('Execution failure',
                               'explicit failure fact recorded'))
        elif etype == 'sealed':
            event_rows.append(('Evidence ID',
                               _fmt(ev.get('payload', {})
                                    .get('evidence_id'))))
            event_rows.append(('Evidence SHA',
                               _fmt(ev.get('payload', {})
                                    .get('evidence_sha256'))))
            event_rows.append(('Sealing timestamp',
                               _fmt(ev.get('timestamp'))))
    details_rows.extend(event_rows)
    # expected-field transparency: rows for the events a state implies;
    # their value comes ONLY from the authoritative event type, and a
    # missing expected event renders 'Not recorded' -- never a fold.
    has_scope = any(e.get('event_type') == 'scope_assessed'
                    for e in events)
    has_validation = any(e.get('event_type') in ('validation_started',
                                                 'validation_failed')
                         for e in events)
    has_sealed = any(e.get('event_type') == 'sealed' for e in events)
    if state in ('SCOPE_ASSESSED', 'EXECUTING', 'VALIDATING', 'FAILED',
                 'INTERRUPTED', 'SEALED') and not has_scope:
        details_rows.append(('Scope mode', _NOT_RECORDED))
    if state in ('VALIDATING', 'FAILED') and not has_validation:
        details_rows.append(('Validation', _NOT_RECORDED))
    if state == 'SEALED' and not has_sealed:
        details_rows.append(('Evidence', _NOT_RECORDED))
    details_rows.append(('Event ID', _fmt(
        events[-1].get('event_id') if events else None)))
    details_rows.append(('Timestamp', _fmt(
        events[-1].get('timestamp') if events else None)))
    if integrity:
        details_rows.append(
            ('Journal integrity',
             'incomplete' if integrity.get('incomplete')
             else 'complete'))
        details_rows.append(('Record count',
                             _fmt(integrity.get('record_count'))))

    rows_html = ''.join(
        f'<tr><th>{html.escape(k)}</th><td>{v}</td></tr>'
        for k, v in details_rows)

    headline = human_label(state)
    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"/>
<title>Asha pipeline stepper</title>
<style>
  body {{ font-family: system-ui, -apple-system, 'Segoe UI', Tahoma,
                sans-serif; margin: 24px; color: #222; background: #fff; }}
  .headline {{ font-size: 20px; font-weight: 600; margin-bottom: 20px; }}
  .pipeline {{ display: flex; flex-direction: row; gap: 8px;
              flex-wrap: wrap; align-items: stretch; }}
  .step {{ flex: 1 1 0; min-width: 150px; border: 2px solid #ddd;
           border-radius: 10px; padding: 14px 10px; text-align: center;
           background: #fafafa; }}
  .step.done {{ border-color: #2e7d32; background: #e8f5e9; color: #1b5e20; }}
  .step.current {{ border-color: #1565c0; background: #e3f2fd;
                  color: #0d47a1; }}
  .step.failed {{ border-color: #c62828; background: #ffebee; color: #b71c1c; }}
  .step.interrupted {{ border-color: #ef6c00; background: #fff3e0;
                      color: #e65100; }}
  .step.not_reached {{ opacity: .55; }}
  .mark {{ display: block; font-size: 22px; margin-bottom: 6px; }}
  .name {{ font-size: 14px; }}
  .details {{ margin-top: 24px; border-top: 1px solid #eee;
             padding-top: 12px; max-width: 640px; }}
  .details table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  .details th {{ text-align: right; padding: 4px 8px; color: #555;
                 font-weight: 600; width: 40%; }}
  .details td {{ padding: 4px 8px; font-family: ui-monospace, Consolas,
                 monospace; }}
  .note {{ margin-top: 8px; font-size: 12px; color: #888; }}
</style>
</head>
<body>
  <div class="headline">{html.escape(headline)}</div>
  <div class="pipeline">{''.join(step_spans)}</div>
  <div class="details">
    <table>{rows_html}</table>
  </div>
  <div class="note">Presentation only — derived from the recorded
    pipeline event journal; never re-evaluated by this view.</div>
</body>
</html>"""


# deterministic alias for tests
present = render_stepper_html