"""Read side: run listing, event replay and the derived run snapshot.

Everything here is computed from what the engine actually emitted. A run
whose stream stops without a terminal event is reported as
``INTERRUPTED`` — never as success.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

RUN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')

BROKEN = 'BROKEN_STREAM'
TERMINAL = {'run.completed', 'run.failed', 'run.cancelled',
            'run.interrupted'}


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID.match(run_id or ''))


def runs_dir(root: Path) -> Path:
    from asha.observability import events as obs_events
    return obs_events.run_dir(Path(root)) / 'runs'


def run_path(root: Path, run_id: str) -> Path:
    if not valid_run_id(run_id):
        raise ValueError('invalid run id')
    path = runs_dir(root) / run_id
    if path.parent != runs_dir(root):  # defence in depth
        raise ValueError('invalid run id')
    return path


def read_meta(root: Path, run_id: str) -> dict[str, Any]:
    path = run_path(root, run_id) / 'meta.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise
    except Exception:
        return {'run_id': run_id, 'root': '', 'broken_meta': True}


def read_events(root: Path, run_id: str, *, after: int = 0,
                limit: int = 5000, types: Iterable[str] | None = None,
                worker: str | None = None,
                query: str | None = None) -> list[dict[str, Any]]:
    path = run_path(root, run_id) / 'events.jsonl'
    wanted = set(types) if types else None
    needle = (query or '').lower()
    out: list[dict[str, Any]] = []
    try:
        fh = path.open('r', encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return out
    with fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                seq = int(event['sequence'])
            except Exception:  # noqa: S112 -- torn record: skip, never crash
                continue
            if seq <= after:
                continue
            if wanted and event.get('event_type') not in wanted:
                continue
            if worker and event.get('worker_id') != worker:
                continue
            if needle and needle not in (
                    json.dumps(event, default=str).lower()):
                continue
            out.append(event)
            if len(out) >= limit:
                break
    return out


def _dur(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
        return int((b - a).total_seconds() * 1000)
    except Exception:
        return None


def _scrub(value: Any) -> Any:
    """Drops absolute host paths from list payloads shown in the UI."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value[:200]]
    return value


def snapshot(root: Path, run_id: str) -> dict[str, Any]:
    meta = read_meta(root, run_id)
    events = read_events(root, run_id, limit=200000)
    workers: dict[str, dict[str, Any]] = {}
    phases: dict[str, dict[str, Any]] = {}
    dag: dict[str, Any] | None = None
    codegraph: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    scope: dict[str, Any] | None = None
    status = 'RUNNING'
    reason = None
    started = meta.get('started_at')
    ended = None

    for event in events:
        etype = event.get('event_type', '')
        wid = event.get('worker_id')
        data = event.get('data') or {}
        if wid:
            node = workers.setdefault(wid, {'id': wid, 'state': 'PENDING',
                                            'evidence': None, 'checks': [],
                                            'task': data.get('task', ''),
                                            'reads': data.get('reads', []),
                                            'writes': data.get('writes', []),
                                            'declared_scope':
                                                data.get('declared_scope', []),
                                            'deps': data.get('deps', [])})
            if etype.startswith('worker.'):
                node['state'] = event.get('status') or etype.split('.')[1]
                node['reason'] = event.get('message') or None
                node['last_event'] = etype
                node['last_message'] = event.get('message', '')
                if etype.endswith('.started') or etype == 'worker.started':
                    node['started_at'] = event['timestamp']
                if etype in ('worker.completed', 'worker.failed',
                             'worker.blocked', 'worker.cancelled'):
                    node['ended_at'] = event['timestamp']
                    node['duration_ms'] = _dur(node.get('started_at'),
                                               event['timestamp'])
            for key in ('reads', 'writes', 'declared_scope', 'deps', 'task'):
                if data.get(key):
                    node[key] = _scrub(data[key])
        if event.get('stage_id'):
            phase = phases.setdefault(event['stage_id'], {
                'id': event['stage_id'], 'status': 'running',
                'started_at': event['timestamp'], 'workers': []})
            phase['status'] = event.get('status') or phase['status']
            phase['last_message'] = event.get('message', '')
            if wid and wid not in phase['workers']:
                phase['workers'].append(wid)
            if etype.endswith(('.started', '.completed', '.failed')):
                phase['ended_at'] = event['timestamp']
            phase['duration_ms'] = _dur(phase.get('started_at'),
                                        event['timestamp'])

        if etype == 'dag.created':
            dag = {'available': True,
                   'nodes': _scrub(data.get('nodes', [])),
                   'edges': _scrub(data.get('edges', {})),
                   'generation': data.get('generation'),
                   'truncation': data.get('truncation')}
        elif etype in ('codegraph.build.completed', 'codegraph.load.completed'):
            codegraph = {'available': True,
                         'state': data.get('state', 'COMPLETE'),
                         'modules': data.get('modules', 0),
                         'edges': data.get('edges', 0),
                         'nodes': _scrub(data.get('nodes', [])),
                         'relations': _scrub(data.get('relations', [])),
                         'source': etype.split('.')[1],
                         'reason': data.get('reason'),
                         'truncation': data.get('truncation')}
        elif etype.startswith(('candidate_check', 'baseline')):
            checks.append({'type': etype, 'name': data.get('name'),
                           'status': data.get('status', event.get('status')),
                           'exit_code': data.get('exit_code'),
                           'worker_id': wid,
                           'at': event['timestamp']})
        elif etype.startswith('evidence.'):
            evidence = {'event': etype, 'status': event.get('status'),
                        'sha256': data.get('sha256'),
                        'path': data.get('path'),
                        'reason': event.get('message') or data.get('reason')}
        elif etype == 'verdict.computed':
            verdict = {'status': data.get('status', event.get('status')),
                       'reason': event.get('message') or data.get('reason'),
                       'new_failures': data.get('new_failures', []),
                       'pre_existing': data.get('pre_existing', []),
                       'unparsed': data.get('unparsed', [])}
        elif etype.startswith('scope_validation'):
            scope = {'status': data.get('status', event.get('status')),
                     'violations': data.get('violations', []),
                     'files': data.get('files', [])}
        elif etype in TERMINAL:
            status = data.get('status') or event.get('status') or (
                'COMPLETED' if etype == 'run.completed' else 'FAILED')
            reason = event.get('message') or data.get('reason')
            ended = event['timestamp']

    if ended is None and meta.get('started_at'):
        status = 'RUNNING' if _alive(meta) else 'INTERRUPTED'
    return {
        'run_id': run_id,
        'meta': meta,
        'status': status,
        'reason': reason,
        'started_at': started,
        'ended_at': ended,
        'duration_ms': _dur(started, ended or started),
        'sequence': events[-1]['sequence'] if events else 0,
        'events': len(events),
        'truncated': len(events) >= 200000,
        'workers': workers,
        'phases': list(phases.values()),
        'dag': dag or {'nodes': [], 'edges': {}, 'available': False},
        'codegraph': codegraph or {'available': False,
                                   'state': 'UNKNOWN',
                                   'reason': 'no codegraph event in this run'},
        'checks': checks,
        'scope': scope,
        'evidence': evidence,
        'verdict': verdict,
    }


def _alive(meta: dict[str, Any]) -> bool:
    """A run is 'running' only if its process still exists."""
    import os
    pid = meta.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        return True  # ponytail: no portable probe; Windows runs are rare
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def list_runs(root: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    base = runs_dir(root)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(base.iterdir(), reverse=True):
        if not entry.is_dir() or not valid_run_id(entry.name):
            continue
        try:
            meta = json.loads((entry / 'meta.json').read_text('utf-8'))
        except Exception:  # noqa: S112 -- a directory without valid meta is not a run
            continue
        rows = read_events(root, entry.name, limit=100000)
        terminal = [e for e in rows if e.get('event_type') in TERMINAL]
        status = 'RUNNING'
        verdict = None
        for event in rows:
            if event.get('event_type') == 'verdict.computed':
                verdict = (event.get('data') or {}).get('status')
            if event.get('event_type') in TERMINAL:
                status = ((event.get('data') or {}).get('status')
                          or event.get('status')
                          or ('COMPLETED'
                              if event['event_type'] == 'run.completed'
                              else 'FAILED'))
        if not terminal:
            status = 'RUNNING' if _alive(meta) else 'INTERRUPTED'
        out.append({'run_id': entry.name,
                    'root': meta.get('root'),
                    'command': meta.get('command'),
                    'task_id': meta.get('task_id'),
                    'started_at': meta.get('started_at'),
                    'ended_at': terminal[-1]['timestamp'] if terminal else None,
                    'status': status,
                    'verdict': verdict,
                    'workers': len({e.get('worker_id') for e in rows
                                    if e.get('worker_id')}),
                    'events': rows[-1]['sequence'] if rows else 0})
        if len(out) >= limit:
            break
    return out


def prune(root: Path, *, keep: int = 50) -> int:
    """Bounded retention: delete the oldest runs beyond ``keep``."""
    base = runs_dir(root)
    if not base.is_dir():
        return 0
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), reverse=True)
    removed = 0
    for entry in dirs[keep:]:
        if not valid_run_id(entry.name):
            continue
        for child in sorted(entry.rglob('*'), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        entry.rmdir()
        removed += 1
    return removed
