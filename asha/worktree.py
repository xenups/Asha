"""Git worktree lifecycle for physical worker isolation."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

from .common import paths as common_paths
from .types import (
    TERMINATION_CONFIRMED,
    TERMINATION_UNCONFIRMED,
    OrchestratorError,
    ProcessTermination,
)

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
    _add_with_lock_guard(path)
    _git(path, '-c', 'user.name=Asha Orchestrator',
         '-c', 'user.email=orchestrator@asha.local',
         'commit', '-q', '-m', message)


#: How long `git add` waits for a transient index lock before refusing.
INDEX_LOCK_WAIT_S = 3.0
_INDEX_LOCK_POLL_S = 0.2


def _index_lock_path(path: Path) -> Path:
    """The lock file git would create for this worktree's index.

    Read from the filesystem only: a lock check must not add a git
    subprocess to every run (the evidence budget counts them).
    """
    dot_git = path / '.git'
    if dot_git.is_file():
        # worktree or submodule: `.git` is a file pointing at the gitdir
        for line in dot_git.read_text(encoding='utf-8').splitlines():
            if line.startswith('gitdir:'):
                raw = Path(line.split(':', 1)[1].strip())
                gitdir = raw if raw.is_absolute() else (path / raw).resolve()
                return gitdir / 'index.lock'
    return dot_git / 'index.lock'


def _locked_error(path: Path, lock: Path,
                  cause: BaseException | None = None) -> OrchestratorError:
    return OrchestratorError(
        f'git index is locked: {lock} (Asha never deletes a lock file, '
        f'because the process that created it may still hold it). If no git '
        f'process is running in {path}, the lock is stale from a killed '
        f'process: verify with `git -C "{path}" status`, then remove '
        f'"{lock}" and retry.')


def _add_with_lock_guard(path: Path) -> None:
    """`git add -A`, waiting out a transient lock and naming a stale one.

    A worker's own git command can hold the index for a moment, so a short
    bounded wait turns a spurious failure into success. Beyond that wait the
    lock has an owner Asha cannot prove is gone: it is reported with the
    exact file and a safe recovery instead of git's generic message.
    """
    deadline = time.monotonic() + INDEX_LOCK_WAIT_S
    while True:
        lock = _index_lock_path(path)
        if not lock.exists():
            try:
                _git(path, 'add', '-A')
                return
            except OrchestratorError as exc:
                if 'index.lock' not in str(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise _locked_error(path, lock) from exc
        elif time.monotonic() >= deadline:
            raise _locked_error(path, lock)
        time.sleep(_INDEX_LOCK_POLL_S)


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
            # unique scratch root per dispatcher instance (run-id semantics):
            # a repo-name-derived path collides across concurrent instances.
            self.root = common_paths.get_scratch_dir(
                f'{self.repo.name}-wt-{uuid.uuid4().hex[:12]}')
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


def kill_process_tree(proc: subprocess.Popen[str]) -> ProcessTermination:
    """Hard-kill the child's process tree and report what was VERIFIED.

    Windows: ``taskkill /T /F``; POSIX: SIGKILL to the session group the child
    was started in. The direct child is then reaped by this function, so
    ``confirmed`` means "observed to exit" -- not "a signal was accepted".

    Descendants are not enumerable portably (no process-tree walk for a
    reparented grandchild), so tree-wide termination is never claimed; the
    detail string says so. Errors are captured, not swallowed: a failed kill
    must not look like a successful one. The timeout that triggered the kill
    is reported by the caller regardless of what happens here.
    """
    errors: list[str] = []
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'taskkill: {type(exc).__name__}: {exc}')
    else:
        try:
            # killpg exists only on POSIX and is absent from the win32
            # typeshed -- this branch never runs on nt.
            os.killpg(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            errors.append(f'killpg: {type(exc).__name__}: {exc}')
    try:
        proc.kill()
    except OSError as exc:
        errors.append(f'kill: {type(exc).__name__}: {exc}')
    reaped = False
    try:
        proc.wait(timeout=30)
        reaped = proc.returncode is not None
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(f'wait: {type(exc).__name__}: {exc}')
    suffix = '' if not errors else '; errors: ' + '; '.join(errors)
    if reaped:
        return ProcessTermination(
            TERMINATION_CONFIRMED,
            f'direct child reaped (rc={proc.returncode}); '
            f'descendants not verified{suffix}')
    return ProcessTermination(
        TERMINATION_UNCONFIRMED,
        'termination requested, direct child not observed to exit'
        f'{suffix}')
