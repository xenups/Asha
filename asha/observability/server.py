"""Loopback-only read-only HTTP server + SSE stream for the dashboard.

No framework, no CDN, no arbitrary file serving, no command endpoints.
Every route is a constant: user input only ever selects a run id that is
validated against a strict pattern.
"""

from __future__ import annotations

import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from asha.observability import store

WEB = Path(__file__).resolve().parent / 'web'
STATIC = {'/': 'index.html', '/app.js': 'app.js', '/styles.css': 'styles.css',
          '/favicon.svg': 'favicon.svg'}
HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': ("default-src 'self'; script-src 'self'; "
                                "style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'"),
}
MAX_BODY = 1024


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = 'asha-dashboard/1'
    root: Path = Path('.')

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        return

    # -- helpers ---------------------------------------------------------
    def _json(self, payload: Any,
              status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        for key, value in HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({'error': message}, status)

    def _static(self, name: str) -> None:
        path = WEB / name
        try:
            body = path.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, 'not found')
            return
        ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', f'{ctype}; charset=utf-8'
                         if ctype.startswith('text/') or ctype.endswith(
                             'javascript') else ctype)
        self.send_header('Content-Length', str(len(body)))
        for key, value in HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if not getattr(self, '_head_only', False):
            self.wfile.write(body)

    # -- routing ---------------------------------------------------------
    def do_HEAD(self) -> None:
        """Same headers as GET, no body: health probes and caches."""
        self._head_only = True
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        try:
            if route in STATIC:
                self._static(STATIC[route])
                return
            if route == '/api/health':
                self._json({'ok': True, 'runs': str(store.runs_dir(self.root)),
                            'version': 1})
                return
            if route == '/api/runs':
                self._json({'runs': store.list_runs(self.root)})
                return
            if route == '/api/stream':
                self._stream(params)
                return
            if route.startswith('/api/runs/'):
                self._run_route(route, params)
                return
            self._error(HTTPStatus.NOT_FOUND, 'not found')
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except BrokenPipeError:
            return

    def _run_route(self, route: str, params: dict[str, list[str]]) -> None:
        parts = route.strip('/').split('/')  # api runs <id> [sub]
        if len(parts) < 3:
            self._error(HTTPStatus.NOT_FOUND, 'not found')
            return
        run_id = parts[2]
        if not store.valid_run_id(run_id):
            self._error(HTTPStatus.BAD_REQUEST, 'invalid run id')
            return
        try:
            store.read_meta(self.root, run_id)
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, 'unknown run')
            return
        sub = parts[3] if len(parts) > 3 else ''
        if sub == '':
            self._json(store.snapshot(self.root, run_id))
        elif sub == 'graph':
            snap = store.snapshot(self.root, run_id)
            codegraph_view = snap['codegraph']
            if not codegraph_view.get('available'):
                meta = store.read_meta(self.root, run_id)
                codegraph_view = _project_graph(str(meta.get('root') or ''))
            self._json({'dag': snap['dag'], 'codegraph': codegraph_view})
        elif sub == 'events':
            after = _int(params, 'after', 0)
            limit = min(_int(params, 'limit', 2000), 5000)
            types = [t for t in (params.get('type') or []) if t]
            self._json({'events': store.read_events(
                self.root, run_id, after=after, limit=limit,
                types=types or None,
                worker=_opt(params, 'worker'),
                query=_opt(params, 'q'))})
        else:
            self._error(HTTPStatus.NOT_FOUND, 'not found')

    # -- SSE -------------------------------------------------------------
    def _stream(self, params: dict[str, list[str]]) -> None:
        run_id = (params.get('run_id') or [''])[0]
        if not store.valid_run_id(run_id):
            self._error(HTTPStatus.BAD_REQUEST, 'invalid run id')
            return
        try:
            store.read_meta(self.root, run_id)
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, 'unknown run')
            return
        cursor = _int(params, 'after', 0)
        last_id = self.headers.get('Last-Event-ID')
        if last_id and last_id.isdigit():
            cursor = max(cursor, int(last_id))
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        for key, value in HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        heartbeat = time.monotonic()
        try:
            while True:
                events = store.read_events(self.root, run_id, after=cursor,
                                           limit=500)
                for event in events:
                    frame = (f'id: {event["sequence"]}\n'
                             'data: ' + json.dumps(event, default=str)
                             + '\n\n')
                    self.wfile.write(frame.encode('utf-8'))
                    cursor = int(event['sequence'])
                if events:
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat > 15:
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                snap_status = _terminal_status(self.root, run_id, cursor)
                if snap_status is not None and not events:
                    self.wfile.write(
                        f'event: end\ndata: {json.dumps({"status": snap_status})}'
                        '\n\n'.encode())
                    self.wfile.flush()
                    return
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return


def _opt(params: dict[str, list[str]], key: str) -> str | None:
    value = (params.get(key) or [''])[0]
    return value or None


_GRAPH_MEMO: dict[str, tuple[float, dict[str, Any]]] = {}
GRAPH_TTL_S = 60.0


def _project_graph(root: str) -> dict[str, Any]:
    """The checkout's real CodeGraph, via Asha's own index cache.

    Nothing is synthesised: if the graph cannot be built the answer is
    UNKNOWN with a reason, never an empty-but-plausible graph.
    """
    now = time.monotonic()
    memo = _GRAPH_MEMO.get(root)
    if memo and now - memo[0] < GRAPH_TTL_S:
        return memo[1]
    try:
        from asha import codegraph, graph_cache
        from asha.common import paths

        root_path = Path(root)
        result = graph_cache.load(root_path, paths.get_cache_dir(root_path)
                                  / 'codegraph')
        graph = result.graph
        unresolved = {node for node, boundary in graph.boundaries
                      if boundary == codegraph.BOUNDARY_UNRESOLVED}
        payload: dict[str, Any] = {
            'available': True,
            'state': 'INCOMPLETE' if unresolved else 'COMPLETE',
            'source': 'dashboard:' + result.mode,
            'modules': len({node.split('.')[0] for node in graph.nodes}),
            'edges': len(graph.edges),
            'unresolved': len(unresolved),
            'nodes': [{'id': node, 'kind': 'module',
                       'module': node.split('.')[0],
                       'confidence': 'UNKNOWN' if node in unresolved
                       else 'COMPLETE'}
                      for node in sorted(graph.nodes)[:4000]],
            'relations': [{'from': edge.source, 'to': edge.target,
                           'rel': edge.kind}
                          for edge in list(graph.edges)[:8000]],
        }
    except Exception as exc:  # a graph is optional; never break the page
        payload = {'available': False, 'state': 'UNKNOWN',
                   'reason': f'graph unavailable: {type(exc).__name__}: {exc}'
                             [:200]}
    _GRAPH_MEMO[root] = (now, payload)
    return payload


def _int(params: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return max(0, int((params.get(key) or [str(default)])[0]))
    except (TypeError, ValueError):
        return default


def _terminal_status(root: Path, run_id: str, cursor: int) -> str | None:
    events = store.read_events(root, run_id, after=cursor - 1, limit=1)
    if events and events[-1].get('event_type') in store.TERMINAL:
        data = events[-1].get('data') or {}
        return str(data.get('status') or events[-1].get('status') or 'DONE')
    return None


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(root: Path, host: str = '127.0.0.1',
                port: int = 7331) -> DashboardServer:
    root = Path(root).resolve()
    handler = type('BoundDashboardHandler', (DashboardHandler,),
                   {'root': root})
    return DashboardServer((host, port), handler)


def serve(root: Path, host: str = '127.0.0.1', port: int = 7331) -> None:
    server = make_server(root, host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
