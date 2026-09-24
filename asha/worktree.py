"""Git worktree lifecycle for physical worker isolation."""
from __future__ import annotations

import os
import re
import subprocess
from contextlib import suppress
from pathlib import Path

from .types import OrchestratorError

# ---------------------------------------------------------------------------
# 6.3 WorktreeDispatcher -- git worktree lifecycle for worker isolation.
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str, timeout: int = 60,
         strip: bool = True) -> str:
    try:
        proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OrchestratorError(
            f'git {args[0] if args else "?"} failed: {exc}') from exc
    if proc.returncode != 0:
        raise OrchestratorError(
            'git ' + ' '.join(args) + ': ' + proc.stderr.strip())
    out = proc.stdout
    return out.strip() if strip else out


def _commit_all(path: Path, message: str) -> None:
    """Commit the worker's working state so a git tree identity exists.
    Identity flags are explicit: no dependence on repo/user config."""
    _git(path, 'add', '-A')
    _git(path, '-c', 'user.name=Asha Orchestrator',
         '-c', 'user.email=orchestrator@asha.local',
         'commit', '-q', '-m', message)


def _safe_id(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]', '_', value)[:64]


class WorktreeDispatcher:
    """Owns the worktree lifecycle: create (isolated checkout of the base
    commit) -> expose path for execution -> remove + prune at run end
    (unless keep=True). Errors are accumulated, never silenced."""

    def __init__(self, repo: Path, *, keep: bool = False) -> None:
        self.repo = Path(repo).resolve()
        self.keep = keep
        self.base_commit = _git(self.repo, 'rev-parse', 'HEAD')
        self.base_tree = _git(self.repo, 'rev-parse', 'HEAD^{tree}')
        self.root = self.repo.parent / (self.repo.name + '.worktrees')
        self.paths: dict[str, Path] = {}
        self.cleanup_errors: list[str] = []

    def create(self, worker_id: str) -> Path:
        path = self.root / _safe_id(worker_id)
        if path.exists():
            raise OrchestratorError(f'worktree path exists: {path}')
        self.root.mkdir(parents=True, exist_ok=True)
        _git(self.repo, 'worktree', 'add', '--detach', '-q', str(path),
             self.base_commit)
        self.paths[worker_id] = path
        return path

    def remove(self, worker_id: str) -> None:
        path = self.paths.get(worker_id)
        if path is None:
            return
        try:
            _git(self.repo, 'worktree', 'remove', '--force', str(path))
        except OrchestratorError as exc:
            self.cleanup_errors.append(f'{worker_id}: {exc}')

    def prune(self) -> None:
        try:
            _git(self.repo, 'worktree', 'prune')
        except OrchestratorError as exc:
            self.cleanup_errors.append(f'prune: {exc}')

    def cleanup(self) -> None:
        """Explicit lifecycle: always prune; remove created worktrees and
        the root directory unless keep (debug) was requested."""
        if not self.keep:
            for worker_id in list(self.paths):
                self.remove(worker_id)
            self.prune()
            if self.root.is_dir() and not any(self.root.iterdir()):
                try:
                    self.root.rmdir()
                except OSError as exc:
                    self.cleanup_errors.append(f'rmdir: {exc}')
        else:
            self.prune()


def kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Fail-closed teardown for a timed-out worker: kill the whole child
    tree (Windows `taskkill /T /F`; POSIX SIGKILL to the session group
    the worker was started in), then reap the direct child. Every error
    is swallowed -- this runs during timeout teardown and must never
    mask the timeout that triggered it."""
    if os.name == 'nt':
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                           capture_output=True, timeout=30)
    else:
        with suppress(OSError):
            # 9 = SIGKILL; killpg exists only on POSIX and is absent
            # from the win32 typeshed -- this branch never runs on nt.
            os.killpg(proc.pid, 9)  # type: ignore[attr-defined]
    with suppress(OSError):
        proc.kill()
    with suppress(OSError, subprocess.SubprocessError):
        proc.wait(timeout=30)
