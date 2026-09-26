"""Phase 5.3 -- thin non-authoritative observation loop (``--watch``).

Observation only: filesystem events wake the loop, the displayed
snapshot is the EXISTING 5.2.3 ``git_context`` change-set contract
(one coherent ``git status`` read), and LAST SEALED RUN is a read-only
view of previously recorded evidence. This module never evaluates
eligibility, never predicts a scope level, never invents a fallback
reason, and never invokes the frozen runtime authority on its own --
only an explicit ``r`` / Enter key does, by spawning the public CLI
(``python -m asha --json``) itself. No third-party dependency, no
WebSocket/SSE: a session-scoped 500 ms fingerprint scan with a 400 ms
debounce window, single main thread, zero leaked workers on exit.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import git_context, telemetry

# --------------------------------------------------------------- constants

IGNORE_DIRS = frozenset({
    '.git', '.jspace', '__pycache__', '.hermes', '.venv', 'venv', 'env',
    'node_modules',
})
IGNORE_SUFFIXES = ('.swp', '~', '.tmp')
SCAN_INTERVAL_MS = 500      # session-scoped observation cadence
DEBOUNCE_MS = 400           # quiet window before a refresh (300-500 ms)

AUTHORITY_COMMAND = ('-m', 'asha', '--json')   # the PUBLIC CLI, nothing else


# ------------------------------------------------------------- observation

def _ignored_name(name: str) -> bool:
    return name.endswith(IGNORE_SUFFIXES)


def fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """Normalized relative POSIX path -> (mtime_ns, size); ignores pruned."""
    found: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for name in sorted(filenames):
            if _ignored_name(name):
                continue
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:                # vanished mid-walk: not our event
                continue
            rel = path.relative_to(root).as_posix()
            found[rel] = (stat.st_mtime_ns, stat.st_size)
    return found


class Debouncer:
    """Quiet-window state machine with an injectable clock (testable)."""

    def __init__(self, debounce_ms: int = DEBOUNCE_MS) -> None:
        self.debounce_ms = debounce_ms
        self._last_change: float | None = None

    def note_change(self, now: float) -> None:
        self._last_change = now

    def due(self, now: float) -> bool:
        return (self._last_change is not None
                and now - self._last_change >= self.debounce_ms / 1000.0)

    def consume(self) -> None:
        self._last_change = None


class WatchLoop:
    """Scan -> debounce -> refresh state. Observation responsibility only:
    refresh() re-reads the existing coherent ChangeSet and nothing else."""

    def __init__(self, root: Path, *, debounce_ms: int = DEBOUNCE_MS,
                 scan_ms: int = SCAN_INTERVAL_MS) -> None:
        self.root = root
        self.debounce = Debouncer(debounce_ms)
        self.scan_ms = scan_ms
        self._fp = fingerprint(root)
        self._last_scan = 0.0

    def tick(self, now: float) -> bool:
        """Advance timers; True when a debounced refresh is due."""
        if (now - self._last_scan) * 1000.0 >= self.scan_ms:
            self._last_scan = now
            current = fingerprint(self.root)
            if current != self._fp:
                self._fp = current
                self.debounce.note_change(now)
        if self.debounce.due(now):
            self.debounce.consume()
            return True
        return False

    def refresh(self) -> git_context.WorkingState:
        return git_context.snapshot(self.root)


# ------------------------------------------------- recorded sealed view

def read_last_sealed(root: Path) -> dict[str, Any]:
    """Read-only view of the most recent recorded evidence artifacts.
    Only fields that EXIST are returned; nothing is derived."""
    sealed: dict[str, Any] = {}
    gate = root / '.jspace' / 'evidence.json'
    try:
        data = json.loads(gate.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            for key in ('commit', 'tree_hash', 'scope', 'evidence_sha256',
                        'stage', 'observed_at'):
                if data.get(key) is not None:
                    sealed[key] = data[key]
            sealed['gate_source'] = '.jspace/evidence.json'
    except (OSError, ValueError):
        pass
    best: Path | None = None
    best_mtime = -1.0
    for candidate in root.glob('.jspace/cache/orchestrator/*/*.json'):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = candidate, mtime
    if best is not None:
        try:
            data = json.loads(best.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            for key in ('validation_mode', 'decision', 'fallback_reason',
                        'evidence_sha256', 'worker_id', 'task_id',
                        'base_commit', 'exit_status'):
                if data.get(key) is not None:
                    sealed[key] = data[key]
            sealed['run_source'] = best.relative_to(root).as_posix()
    return sealed


def branch_name(root: Path) -> str:
    """Observed fact: current branch (or short HEAD when detached)."""
    try:
        proc = subprocess.run(['git', 'branch', '--show-current'],
                              cwd=root, capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'
    name = (proc.stdout or '').strip()
    if proc.returncode == 0 and name:
        return name
    try:
        proc = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                              cwd=root, capture_output=True, text=True,
                              timeout=10)
        if proc.returncode == 0:
            return (proc.stdout or '').strip() or 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 'unknown'


# -------------------------------------------------- authoritative trigger

def authority_command() -> list[str]:
    """The ONE path to formal evaluation: the public CLI entry."""
    return [sys.executable, *AUTHORITY_COMMAND]


def _authority_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault('GIT_TERMINAL_PROMPT', '0')
    env.setdefault('GIT_PAGER', 'cat')
    env.setdefault('GIT_ASKPASS', 'echo')
    return env


def spawn_authority(root: Path) -> subprocess.Popen[str]:
    """Explicit-trigger spawn of the existing CLI as a tracked child.
    The watcher only polls the child and reads its one schema-v1 JSON
    document; it never interprets or drives execution."""
    return subprocess.Popen(
        authority_command(), cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=_authority_env())


def finish_authority(proc: subprocess.Popen[str],
                     timeout: float = 30.0
                     ) -> tuple[int, dict[str, Any] | None]:
    """Collect a finished authority child -> (rc, parsed payload)."""
    try:
        out, _err = proc.communicate(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        return 127, {'error': 'authority child timed out'}
    payload: dict[str, Any] | None = None
    try:
        parsed = json.loads(out)
        if isinstance(parsed, dict):
            payload = parsed
    except ValueError:
        payload = None
    return proc.returncode, payload


def run_authority(root: Path,
                  timeout: float = 1800.0) -> tuple[int, dict[str, Any] | None]:
    """Explicit user trigger only: spawn the existing CLI and hand back
    its (single, schema v1) document. Watch never interprets or edits it.

    The child CLI runs git under the hood; on hosts without a terminal
    the git prompt/pager plumbing can hang on our empty stdin, so the
    child gets prompt/pager-disabling environment entries (observed
    failure: a bare `git rev-parse` stuck 60s in this exact path)."""
    try:
        proc = subprocess.run(authority_command(), cwd=root,
                              capture_output=True, text=True,
                              timeout=timeout, env=_authority_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, {'error': f'{type(exc).__name__}: {exc}'}
    payload: dict[str, Any] | None = None
    try:
        parsed = json.loads(proc.stdout)
        if isinstance(parsed, dict):
            payload = parsed
    except ValueError:
        payload = None
    return proc.returncode, payload


# --------------------------------------------------- live stepper view

def latest_journal(root: Path) -> Path | None:
    """Newest recorded run journal (append-only; mtime order)."""
    jdir = root / '.jspace' / 'execution'
    try:
        files = list(jdir.glob('*.jsonl'))
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


def journal_terminal_event(journal: Path | None) -> bool:
    """Explicit terminal-event fact from the journal itself."""
    if journal is None:
        return False
    try:
        state, _ = telemetry.project_path(journal)
    except OSError:
        return False
    return state in ('SEALED', 'FAILED')


def watch_stepper(root: Path, interrupted: bool) -> str:
    """Project the latest recorded journal (explicit interruption fact
    only) into the human stepper HTML. Falls back to the clean state
    when no journal exists yet -- presentation of recorded facts only."""
    journal = latest_journal(root)
    if journal is None:
        from . import presentation
        return presentation.render_stepper_html('IDLE_CLEAN', {})
    state, meta = telemetry.project_path(journal, interrupted=interrupted)
    from . import presentation
    return presentation.render_stepper_html(state, meta)


_STEPPER_OPEN = '<!-- ASHA-STEPPER -->'
_STEPPER_CLOSE = '<!-- /ASHA-STEPPER -->'


def inject_stepper(html: str, stepper_html: str) -> str:
    """Place/replace the stepper block before </body> (offline,
    inline). A previously injected block is replaced, never duplicated
    -- the journal grows while a formal run is active and the watch
    report is rewritten repeatedly."""
    if _STEPPER_OPEN in html:
        start = html.index(_STEPPER_OPEN)
        end = html.index(_STEPPER_CLOSE, start) + len(_STEPPER_CLOSE)
        html = html[:start] + html[end:]
    block = _STEPPER_OPEN + stepper_html + _STEPPER_CLOSE
    marker = '</body>'
    if marker not in html:
        return html + block
    return html.replace(marker, block + marker, 1)


# ----------------------------------------------------------------- display

def state_label(snapshot: git_context.WorkingState) -> str:
    return 'IDLE (CLEAN)' if not snapshot.paths else 'MODIFIED (LIVE PREVIEW)'


def render_terminal(root: Path, branch: str,
                    snapshot: git_context.WorkingState,
                    sealed: Mapping[str, Any],
                    last_run: str | None) -> str:
    files = list(snapshot.paths)
    shown = ', '.join(files[:8]) + (f' (+{len(files) - 8} more)'
                                    if len(files) > 8 else '')
    if sealed:
        mode = str(sealed.get('validation_mode')
                   or sealed.get('decision') or 'Not recorded')
        decision = str(sealed.get('decision') or 'Not recorded')
        sha = str(sealed.get('evidence_sha256') or '')
        sealed_line = (f'mode={mode}  decision={decision}  '
                       f'evidence={sha[:12]}' if sha else
                       f'mode={mode}  decision={decision}')
    else:
        sealed_line = 'none recorded'
    lines = [
        'Asha Watch  (LIVE / PREVIEW - non-authoritative)',
        '------------------------------------------------',
        f'Repository   {root}',
        f'Branch       {branch}',
        f'State        {state_label(snapshot)}',
        f'Files        {shown or "-"}',
        f'LAST SEALED RUN   {sealed_line}',
    ]
    if last_run:
        lines.append(f'Last run     {last_run}')
    lines.append('Enter/r = run authoritative evaluation  ·  q = quit')
    return '\n'.join(lines) + '\n'


def watch_report_payload(root: Path, branch: str,
                         snapshot: git_context.WorkingState,
                         sealed: Mapping[str, Any]) -> dict[str, Any]:
    """Presentation mapping for ui.render_report: observation facts plus
    the recorded sealed view. Carries NO decision/eligibility keys of its
    own -- the watch layout renders no verdict section at all."""
    states = {path: list(tags) for path, tags in snapshot.states.items()}
    return {
        'schema_version': 1,
        'repository': str(root),
        'watch': {
            'branch': branch,
            'state': state_label(snapshot),
            'files': list(snapshot.paths),
            'states': states,
            'non_authoritative': True,
        },
        'last_sealed_run': dict(sealed),
        'duration_ms': None,
    }


# ------------------------------------------------------------------- input

_EOF = object()          # distinct from any key string


def _read_keys(stream: Any, out: queue.Queue[Any],
               stop: threading.Event) -> None:
    try:
        for line in stream:
            if stop.is_set():
                break
            out.put(line)
    except (ValueError, OSError):
        pass                                    # stream closed at exit
    finally:
        out.put(_EOF)


def _tty_key() -> str | None:
    """Single next key from an interactive console, non-blocking."""
    try:
        if os.name == 'nt':
            import msvcrt
            # getattr keeps mypy clean on non-Windows platforms where
            # the msvcrt stubs expose no attributes
            kbhit = getattr(msvcrt, 'kbhit', None)
            getwch = getattr(msvcrt, 'getwch', None)
            if kbhit is not None and getwch is not None and kbhit():
                return str(getwch())
        else:
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                return sys.stdin.read(1)
    except (OSError, ValueError):
        return None
    return None


def _write_watch_html(root: Path, report_path: Path,
                      snapshot: git_context.WorkingState,
                      sealed: Mapping[str, Any],
                      stepper_html: str | None) -> None:
    """Write watch.html with the live stepper component injected."""
    from . import ui as ui_module
    html = ui_module.render_report(
        watch_report_payload(root, branch_name(root), snapshot, sealed))
    if stepper_html is not None:
        html = inject_stepper(html, stepper_html)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(html, encoding='utf-8')
    except OSError:
        pass


def _inject_live_stepper(report_path: Path, root: Path,
                         interrupted: bool) -> None:
    """Refresh the stepper component inside the already-written report."""
    try:
        html = report_path.read_text(encoding='utf-8')
    except OSError:
        return
    stepper = watch_stepper(root, interrupted)
    html = inject_stepper(html, stepper)
    try:
        report_path.write_text(html, encoding='utf-8')
    except OSError:
        pass


def _handle_key(key: str) -> str | None:
    """Map one input token to an action: 'q' quit, 'r'/Enter trigger."""
    if not key:
        return None
    first = key[0].lower()
    if first == 'q':
        return 'quit'
    if first in ('r', '\r', '\n'):
        return 'trigger'
    return None


# -------------------------------------------------------------------- main

def run_watch(root: Path, *, ui: bool = False,
              ui_out: Path | None = None) -> int:
    """The ``asha --watch`` session. Clean exit on q / Ctrl+C (code 0);
    no watcher-side evaluation, no evidence writes, no leaked threads."""
    branch = branch_name(root)
    sealed = read_last_sealed(root)
    loop = WatchLoop(root)
    snapshot = loop.refresh()
    report_path = (ui_out if ui_out is not None else
                   root / '.jspace' / 'reports' / 'watch.html')
    keys: queue.Queue[Any] = queue.Queue()
    stop = threading.Event()
    reader: threading.Thread | None = None
    interactive = bool(sys.stdin.isatty())
    if not interactive:
        reader = threading.Thread(target=_read_keys,
                                  args=(sys.stdin, keys, stop), daemon=True)
        reader.start()
    opened_browser = False
    last_render = ''
    last_authority: str | None = None
    active_proc: subprocess.Popen[str] | None = None
    if ui:
        _write_watch_html(root, report_path, snapshot, sealed,
                          watch_stepper(root, False))
    try:
        while True:
            now = time.monotonic()
            if loop.tick(now):
                snapshot = loop.refresh()
                sealed = read_last_sealed(root)
            # lapse a finished authority child: explicit host fact --
            # a dead child with no terminal journal event is
            # INTERRUPTED (never FAILED); a terminal journal event wins.
            interrupted = False
            if active_proc is not None and active_proc.poll() is not None:
                journal = latest_journal(root)
                if not journal_terminal_event(journal):
                    interrupted = True
                    if ui:
                        _write_watch_html(root, report_path, snapshot,
                                          sealed,
                                          watch_stepper(root, True))
                    sys.stdout.write(
                        '\nAuthoritative run interrupted (no terminal '
                        'event recorded).\n')
                    sys.stdout.flush()
                code, payload = finish_authority(active_proc)
                if payload is not None:
                    summary = (
                        f"decision={payload.get('decision')!s} "
                        f"validation={payload.get('validation_result')!s} "
                        f"evidence="
                        f"{str(payload.get('evidence_id'))[:12]} "
                        f"exit={code}")
                else:
                    summary = f'exit={code} (non-JSON output)'
                last_authority = summary
                active_proc = None
                sealed = read_last_sealed(root)
                last_render = ''    # force re-render: the journal now
                                    # has terminal events (SEALED/FAILED)
            text = render_terminal(root, branch, snapshot, sealed,
                                   last_authority)
            if text != last_render:             # render on change only
                last_render = text
                sys.stdout.write('\n' + text)
                sys.stdout.flush()
                if ui:
                    _write_watch_html(
                        root, report_path, snapshot, sealed,
                        watch_stepper(root, interrupted))
                    if not opened_browser and sys.stdout.isatty():
                        opened_browser = True
                        try:
                            webbrowser.open(report_path.resolve().as_uri())
                        except OSError:
                            pass
            elif ui and active_proc is not None:
                # live stepper refresh while a formal run is active:
                # re-project the growing journal every tick
                _inject_live_stepper(report_path, root,
                                     interrupted or active_proc.poll()
                                     is not None)
            key: str | None = None
            if interactive:
                key = _tty_key()
            else:
                token: Any = None
                try:
                    token = keys.get_nowait()
                except queue.Empty:
                    token = None
                if token is _EOF:       # stream ended: clean stop
                    break
                key = token if isinstance(token, str) else None
            if key is not None:
                action = _handle_key(key)
                if action == 'quit':
                    break
                if action == 'trigger':
                    if active_proc is not None:
                        sys.stdout.write(
                            '\nAn authoritative run is already '
                            'in progress...\n')
                        sys.stdout.flush()
                    else:
                        sys.stdout.write(
                            '\nRunning authoritative evaluation '
                            '(existing runtime authority)...\n')
                        sys.stdout.flush()
                        active_proc = spawn_authority(root)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass                                    # Ctrl+C == clean exit
    finally:
        stop.set()
        if reader is not None:
            try:
                sys.stdin.close()               # unblock the reader line
            except (OSError, ValueError):
                pass
            reader.join(timeout=2.0)
    sys.stdout.write('\nAsha Watch stopped.\n')
    sys.stdout.flush()
    return 0
