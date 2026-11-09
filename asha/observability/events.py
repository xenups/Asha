"""Structured execution events for the optional observability layer.

Design rules (non-negotiable):

* A telemetry failure NEVER changes an execution decision: every sink
  call is wrapped and failures are counted, never raised.
* Disabled by default: with no sink configured, ``emit`` is a no-op and
  the only cost is one function call.
* Events are written outside any worktree, so they can never appear in a
  diff, a scope violation or an evidence seal.
* stdout is never used here (the MCP stdio transport owns it).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 1
MAX_TEXT = 4000
MAX_EVENT_BYTES = 64_000  # one JSONL record; overflow is pruned, never sliced
_KEEP_STEPS = (200, 100, 50, 20, 10, 5, 1)

_SECRET = re.compile(
    r'(?i)\b(token|secret|password|passwd|api[_-]?key|access[_-]?key'
    r'|authorization|bearer)(\s*[:=]\s*|\s+)(\S+)')


_SECRET_KEY = re.compile(
    r'(?i)^(token|secret|password|passwd|api[_-]?key|access[_-]?key'
    r'|authorization|credentials?)$')


def redact(text: str) -> str:
    """Mask credential-shaped substrings; bounds the payload size."""
    if not text:
        return ''
    return _SECRET.sub(lambda m: f'{m.group(1)}{m.group(2)}[REDACTED]',
                       str(text))[:MAX_TEXT]


def _now() -> str:
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _scrub_obj(value: Any, depth: int = 0) -> Any:
    """Redact every string in the payload; bounds size and depth."""
    if depth > 6:
        return '[truncated]'
    if isinstance(value, dict):
        return {str(k)[:80]: ('[REDACTED]'
                              if _SECRET_KEY.match(str(k))
                              else _scrub_obj(v, depth + 1))
                for k, v in list(value.items())[:200]}
    if isinstance(value, (list, tuple)):
        return [_scrub_obj(v, depth + 1) for v in value[:200]]
    if isinstance(value, str):
        return redact(value)
    return value


def _dump(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, default=str)


def _shrink(value: Any, keep: int) -> Any:
    """Cap every list at `keep` items, deterministically from the front."""
    if isinstance(value, list):
        return [_shrink(item, keep) for item in value[:keep]]
    if isinstance(value, dict):
        return {key: _shrink(item, keep) for key, item in value.items()}
    return value


def _coherent(data: dict[str, Any]) -> dict[str, Any]:
    """Drop edges whose endpoints were pruned, so a cut graph stays honest."""
    nodes = data.get('nodes')
    if not isinstance(nodes, list):
        return data
    ids = {str(node.get('id')) for node in nodes
           if isinstance(node, dict) and node.get('id') is not None}
    if not ids:
        return data
    edges = data.get('edges')
    if isinstance(edges, dict):
        data = dict(data, edges={str(key): [str(dep) for dep in deps
                                           if str(dep) in ids]
                                 for key, deps in edges.items()
                                 if str(key) in ids})
    relations = data.get('relations')
    if isinstance(relations, list):
        data = dict(data, relations=[
            item for item in relations
            if isinstance(item, dict) and str(item.get('from')) in ids
            and str(item.get('to')) in ids])
    return data


def _bounded(event: dict[str, Any], limit: int = MAX_EVENT_BYTES) -> str:
    """Serialize one record that always parses.

    Oversized optional collections are pruned to a deterministic prefix, the
    loss is recorded in ``data.truncation``, and a pruned graph is never
    reported COMPLETE. The envelope and message are never cut.
    """
    line = _dump(event)
    if len(line.encode('utf-8')) <= limit:
        return line
    data = event.get('data')
    if isinstance(data, dict):
        totals = {k: len(v) for k, v in data.items() if isinstance(v, list)}
        for keep in _KEEP_STEPS:
            pruned = _coherent(_shrink(data, keep))
            kept = {k: len(v) for k, v in pruned.items() if isinstance(v, list)}
            omitted = {k: totals[k] - kept[k] for k in totals
                       if totals[k] > kept[k]}
            if not omitted:
                break
            pruned['truncation'] = {'truncated': True, 'kept': keep,
                                    'omitted': omitted}
            if pruned.get('state') == 'COMPLETE':
                pruned['state'] = 'INCOMPLETE'
            line = _dump(dict(event, data=pruned))
            if len(line.encode('utf-8')) <= limit:
                return line
    # the envelope alone is small; keep the event readable and say why
    return _dump(dict(event, data={
        'truncated': True,
        'reason': 'payload exceeded the event size limit'}))


class Sink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class NullSink:
    """Disabled default: records nothing, cannot fail."""

    def emit(self, event: dict[str, Any]) -> None:
        return None


class JsonlSink:
    """Append-only, one JSON object per line, one file per run.

    Thread-safe: workers emit concurrently from a thread pool. A single
    ``write`` on an O_APPEND descriptor is atomic enough for readers that
    tolerate (and skip) a torn trailing line.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._seq = 0
        self.failures = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # resuming a run (or a rerun with the same id): continue the
            # sequence instead of reusing numbers
            with self.path.open('r', encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    try:
                        self._seq = max(self._seq,
                                        int(json.loads(line)['sequence']))
                    except Exception:  # noqa: S112 -- skip a torn line
                        continue
        self._fh = self.path.open('a', encoding='utf-8')

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            event['sequence'] = self._seq
            self._fh.write(_bounded(event) + '\n')
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class _State:
    sink: Sink = NullSink()
    run_id: str | None = None
    directory: Path | None = None
    failures: int = 0


_STATE = _State()
_RUN_LOCK = threading.Lock()

# env switch: any truthy value enables recording; the directory is derived
# from the repository checkout, never from the worktree
ENV_ENABLE = 'ASHA_TELEMETRY'


def enabled() -> bool:
    return not isinstance(_STATE.sink, NullSink)


def current_run() -> str | None:
    return _STATE.run_id


def run_dir(root: Path) -> Path:
    """<state>/<checkout>/observability — outside every worktree."""
    from asha.common import paths as common_paths
    return common_paths.get_state_dir(Path(root)) / 'observability'


def configure(sink: Sink, run_id: str | None = None) -> None:
    _STATE.sink = sink
    _STATE.run_id = run_id


def reset() -> None:
    internal_close()
    _STATE.sink = NullSink()
    _STATE.run_id = None
    _STATE.directory = None
    _STATE.failures = 0


def internal_close() -> None:
    closer = getattr(_STATE.sink, 'close', None)
    if closer is not None:
        try:
            closer()
        except Exception:  # noqa: S110 -- close must never raise
            pass


def start_run(root: Path, run_id: str, meta: dict[str, Any]) -> str:
    """Create the run directory and bind the sink. Never raises."""
    if os.environ.get(ENV_ENABLE, '') in ('', '0', 'false', 'False'):
        return run_id
    try:
        directory = run_dir(root) / 'runs' / run_id
        directory.mkdir(parents=True, exist_ok=True)
        _STATE.directory = directory
        _STATE.sink = JsonlSink(directory / 'events.jsonl', run_id)
        _STATE.run_id = run_id
        _STATE.failures = 0
        record = {'schema_version': SCHEMA_VERSION, 'run_id': run_id,
                  'root': str(Path(root).resolve()), 'started_at': _now(),
                  'pid': os.getpid(), **meta}
        tmp = directory / 'meta.json.tmp'
        tmp.write_text(json.dumps(record, indent=1, default=str),
                       encoding='utf-8')
        os.replace(tmp, directory / 'meta.json')
    except Exception as exc:  # telemetry must never break a run
        _STATE.sink = NullSink()
        _fail(str(exc))
    return run_id


WORKER_EVENT = {
    'PENDING': 'worker.queued', 'DEFERRED': 'worker.queued',
    'READY': 'worker.ready', 'RUNNING': 'worker.started',
    'DONE': 'worker.completed', 'FAILED': 'worker.failed',
    'BLOCKED': 'worker.blocked', 'CANCELLED': 'worker.cancelled',
    'INVALID_EVIDENCE': 'worker.failed',
}


def worker_event(state: str) -> str:
    """Canonical event name for an engine worker state."""
    return WORKER_EVENT.get(state, 'worker.' + state.lower())


def new_run_id(prefix: str = 'run') -> str:
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    return f'{prefix}-{stamp}-{uuid.uuid4().hex[:8]}'


def _fail(message: str) -> None:
    _STATE.failures += 1
    if _STATE.failures <= 3:  # bounded: a broken sink must stay quiet
        print(f'asha: telemetry disabled: {message}', file=sys.stderr)


def emit(event_type: str, component: str, *,
         status: str | None = None,
         message: str = '',
         stage_id: str | None = None,
         worker_id: str | None = None,
         data: dict[str, Any] | None = None) -> None:
    """Record one event. No-op when disabled; never raises."""
    if isinstance(_STATE.sink, NullSink) or _STATE.run_id is None:
        return
    event: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'run_id': _STATE.run_id,
        'sequence': 0,
        'timestamp': _now(),
        'event_type': event_type,
        'component': component,
    }
    if stage_id:
        event['stage_id'] = stage_id
    if worker_id:
        event['worker_id'] = str(worker_id)
    if status:
        event['status'] = status
    if message:
        event['message'] = redact(message)
    if data:
        clean = _scrub_obj(data)
        if isinstance(clean, dict):
            clean = _coherent(clean)
            lost = {key: len(value) - len(clean.get(key, []))
                    for key, value in data.items()
                    if isinstance(value, (list, dict))
                    and isinstance(clean.get(key), (list, dict))
                    and len(value) > len(clean[key])}
            if lost:
                if clean.get('state') == 'COMPLETE':
                    clean['state'] = 'INCOMPLETE'
                clean['truncation'] = {'truncated': True, 'kept': 200,
                                       'omitted': lost,
                                       'note': 'collection cap applied at emit'}
        event['data'] = clean
    try:
        _STATE.sink.emit(event)
    except Exception as exc:
        _STATE.sink = NullSink()
        _fail(str(exc))


def finish_run(status: str, *, reason: str | None = None,
               data: dict[str, Any] | None = None) -> None:
    event = 'run.completed' if status in ('ok', 'COMPLETED_CLEAN',
                                          'APPROVED') else 'run.failed'
    emit(event, 'scheduler', status=status, message=reason or '',
         data=data or {})
    internal_close()
    _STATE.sink = NullSink()
    _STATE.run_id = None
