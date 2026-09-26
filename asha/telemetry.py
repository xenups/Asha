"""Phase 5.4.2 -- pipeline event journal + deterministic state projector.

Observation-only telemetry for the human-facing pipeline stepper.

Boundaries (spec 5.4.2):
- records pipeline-level lifecycle FACTS produced by the existing
  runtime; it never schedules, dispatches, classifies, or authorizes;
- the projector derives state only from recorded events and an
  EXPLICIT host/process-liveness fact; a missing event or a vanished
  process is an incomplete journal, never a synthesized FAILED;
- FAILED requires an explicit failure event (validation_failed /
  execution_failed);
- every event is exactly one JSON object + ``\\n``; critical lifecycle
  milestones are flushed AND fsynced (flush alone is not physical
  persistence);
- malformed/truncated trailing records are ignored safely, the journal
  is marked incomplete, and a partial record is a journal-integrity
  fact -- never a validation failure;
- no AST/CodeGraph/governance imports; no per-service events; no UI or
  network dependency.

Durability note: ``os.fsync`` is the explicit durability barrier;
whether the underlying filesystem honors it is platform semantics we
do not over-claim.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVENT_TYPES = frozenset({
    'change_detected',
    'scope_assessed',
    'execution_started',
    'validation_started',
    'validation_failed',
    'execution_failed',
    'sealing_started',
    'sealed',
})

# critical milestones get flush + fsync (durability barrier)
CRITICAL_EVENTS = frozenset({
    'execution_started',
    'validation_started',
    'validation_failed',
    'execution_failed',
    'sealing_started',
    'sealed',
})

REQUIRED_FIELDS = ('event_id', 'run_id', 'timestamp', 'event_type', 'payload')


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def serialize_event(event: dict[str, Any]) -> str:
    """Deterministic serialization: sorted keys, compact separators."""
    body = {key: event[key] for key in REQUIRED_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(',', ':')) + '\n'


def validate_event(event: dict[str, Any]) -> None:
    """Minimum structural contract; raises TypeError/ValueError when
    violated."""
    if not isinstance(event, dict):
        raise TypeError('event must be a dict')
    for field in REQUIRED_FIELDS:
        if field not in event:
            raise ValueError(f'event missing required field: {field}')
    if not isinstance(event['event_id'], str) or not event['event_id']:
        raise TypeError('event_id must be a non-empty string')
    if not isinstance(event['run_id'], str) or not event['run_id']:
        raise TypeError('run_id must be a non-empty string')
    if not isinstance(event['timestamp'], str) or not event['timestamp']:
        raise TypeError('timestamp must be a string')
    if event['event_type'] not in EVENT_TYPES:
        raise ValueError(f'unknown event_type: {event["event_type"]}')
    if not isinstance(event['payload'], dict):
        raise TypeError('payload must be a dict')


class EventJournalWriter:
    """Append-only JSONL writer under ``.jspace/execution/<run_id>.jsonl``.

    Events are immutable after append: there is no API to modify or
    delete a prior record. Critical milestones are flushed and fsynced
    before returning.
    """

    def __init__(self, root: Path, run_id: str) -> None:
        if not run_id:
            raise ValueError('run_id must be non-empty')
        self.run_id = run_id
        self.root = Path(root)
        self.path = self.root / '.jspace' / 'execution' / f'{run_id}.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open('ab')
        self._closed = False

    def _new_event(self, event_type: str,
                   payload: dict[str, Any] | None) -> dict[str, Any]:
        event: dict[str, Any] = {
            'event_id': secrets.token_hex(8),
            'run_id': self.run_id,
            'timestamp': _now_iso(),
            'event_type': event_type,
            'payload': payload if payload is not None else {},
        }
        validate_event(event)
        return event

    def append(self, event_type: str, payload: dict[str, Any] | None = None,
               *, critical: bool = False) -> dict[str, Any]:
        """Append one event record. ``critical`` forces flush+fsync."""
        if event_type not in EVENT_TYPES:
            raise ValueError(f'unknown event_type: {event_type}')
        if self._closed:
            raise ValueError('journal is closed')
        event = self._new_event(event_type, payload)
        self._handle.write(serialize_event(event).encode('utf-8'))
        self._handle.flush()
        if critical or event_type in CRITICAL_EVENTS:
            os.fsync(self._handle.fileno())
        return event

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._handle.flush()
        finally:
            self._handle.close()
            self._closed = True

    def __enter__(self) -> EventJournalWriter:  # noqa: PYI034 (runtime self)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ----------------------------------------------------------------------
# projector
# ----------------------------------------------------------------------

class JournalIntegrity:
    """Facts about the journal file itself (not pipeline semantics)."""

    __slots__ = ('incomplete', 'invalid_trailing', 'record_count')

    def __init__(self, *, incomplete: bool, invalid_trailing: bool,
                 record_count: int) -> None:
        self.incomplete = incomplete
        self.invalid_trailing = invalid_trailing
        self.record_count = record_count


def read_events(text: str) -> tuple[list[dict[str, Any]], JournalIntegrity]:
    """Parse a journal: valid records + integrity facts.

    A malformed/truncated/invalid trailing record is ignored, never
    raises, never invalidates earlier valid events, and marks the
    journal incomplete.
    """
    events: list[dict[str, Any]] = []
    invalid_trailing = False
    lines = text.splitlines()
    if not lines:
        return events, JournalIntegrity(incomplete=False,
                                        invalid_trailing=False,
                                        record_count=0)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
            validate_event(parsed)
            events.append(parsed)
        except (ValueError, json.JSONDecodeError):
            if index == len(lines) - 1:
                invalid_trailing = True
            # mid-file junk: ignore; the journal is still suspicious
    incomplete = invalid_trailing
    return events, JournalIntegrity(incomplete=incomplete,
                                    invalid_trailing=invalid_trailing,
                                    record_count=len(events))


_STARTING_TYPES = frozenset({'execution_started', 'validation_started',
                             'sealing_started'})


def _project_last(events: list[dict[str, Any]],
                  interrupted: bool | None) -> str:
    """Deterministic state from the LAST valid event + explicit fact."""
    if not events:
        return 'IDLE_CLEAN'
    last = events[-1]['event_type']
    if last in ('validation_failed', 'execution_failed'):
        return 'FAILED'
    if last == 'sealed':
        return 'SEALED'
    if last == 'scope_assessed':
        return 'SCOPE_ASSESSED'
    if last == 'change_detected':
        return 'MODIFIED_PREVIEW'
    if last in _STARTING_TYPES:
        if interrupted is True:
            return 'INTERRUPTED'
        return 'EXECUTING' if last == 'execution_started' else 'VALIDATING'
    return 'IDLE_CLEAN'


def project(text: str,
            *, interrupted: bool | None = None) -> tuple[str, dict[str, Any]]:
    """Project one journal text -> (state, metadata).

    ``interrupted`` is the ONLY acceptable host/process-liveness fact
    (explicit, runtime-owned). Absence of a termination event is NOT
    failure: without an explicit failure fact the strongest derived
    state stays as the last recorded milestone.
    """
    events, integrity = read_events(text)
    state = _project_last(events, interrupted)
    metadata: dict[str, Any] = {
        'integrity': {
            'incomplete': integrity.incomplete,
            'invalid_trailing': integrity.invalid_trailing,
            'record_count': integrity.record_count,
        },
        'last_event': events[-1]['event_type'] if events else None,
        'run_id': events[-1]['run_id'] if events else None,
        'interrupted_fact': interrupted,
    }
    return state, metadata


class PipelineStateProjector:
    """Spec-named facade over the pure projection functions."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def project_run(self, run_id: str, *,
                    interrupted: bool | None = None
                    ) -> tuple[str, dict[str, Any]]:
        path = self.root / '.jspace' / 'execution' / f'{run_id}.jsonl'
        return project_path(path, interrupted=interrupted)

    def project_text(self, text: str, *,
                     interrupted: bool | None = None
                     ) -> tuple[str, dict[str, Any]]:
        return project(text, interrupted=interrupted)


def project_path(path: Path,
                 *, interrupted: bool | None = None
                 ) -> tuple[str, dict[str, Any]]:
    """Project a journal file. Missing file == empty journal."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        return project('', interrupted=interrupted)
    return project(text, interrupted=interrupted)