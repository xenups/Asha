#!/usr/bin/env python3
"""Phase 2 -- governed Agent Runners: the dispatch seam between a
worker's spec and the subprocess that performs it. Stdlib only.

BaseAgentRunner.execute(worker_spec, worktree_path, timeout) ->
RunnerResult(exit_code, stdout, stderr, duration_s, audit_metadata).

Adapters: CommandRunner (default; raw argv `cmd`, byte-compatible with
the pre-Phase-2 default_execute primitive) and AntigravityRunner
(headless Google Antigravity: `--workspace <worktree>
--non-interactive --task <prompt>`; cwd locked to the worktree).

Hermetic boundary: `_spawn` is the ONE process primitive (Popen with
PIPEs, timeout budget, G3 `kill_process_tree` teardown raising
TimeoutExpired) -- shared by every runner, never reimplemented.
Scope is NOT enforced here: files outside `writes` are caught later at
evidence sealing (INVALID_EVIDENCE/scope_violation)."""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .types import OrchestratorError, RunnerResult
from .worktree import kill_process_tree

#: Worker values that select a non-default runner (spec field contract).
KNOWN_RUNNERS = frozenset({'command', 'antigravity'})

#: Default binary for the Antigravity adapter (resolved via PATH).
ANTIGRAVITY_BINARY = 'antigravity'


def runner_kind(worker: Mapping[str, Any]) -> str:
    """Which runner serves this worker: an explicit `runner.type` wins
    over `agent`; default 'command' keeps every pre-Phase-2 spec on
    CommandRunner unchanged."""
    runner_cfg = worker.get('runner')
    if isinstance(runner_cfg, Mapping):
        kind = runner_cfg.get('type')
        if isinstance(kind, str) and kind:
            return kind
    agent = worker.get('agent')
    if isinstance(agent, str) and agent:
        return agent
    return 'command'


def _spawn(argv: list[str], cwd: Path,
           timeout: float) -> tuple[int, str, str]:
    """ONE process primitive for all runners -- same contract
    default_execute had: spawn failure -> (-1, msg); timeout ->
    kill_process_tree, then TimeoutExpired propagates for _run_one to
    map to FAILED/timeout_exceeded."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=(os.name != 'nt'))
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, '', f'{type(exc).__name__}: {exc}'
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, '', f'{type(exc).__name__}: {exc}'
    return proc.returncode, out or '', err or ''


def _split_verify(verify: str) -> list[str]:
    """argv split for verify_command -- split once, NEVER a shell.
    POSIX shlex eats backslashes, which corrupts Windows paths written
    with them, so on nt split on whitespace with quotes kept literal,
    then strip ONE wrapping quote pair per token.
    ponytail: embedded/escaped quotes inside a quoted verify arg are not
    supported on nt; upgrade path = a list-valued verify field
    (cmd-style argv) if that ever appears."""
    if os.name != 'nt':
        return shlex.split(verify)
    parts = shlex.split(verify, posix=False)
    quotes = (chr(34), chr(39))  # double / single quote, no escapes
    return [part[1:-1] if (len(part) >= 2 and part[0] == part[-1]
                           and part[0] in quotes) else part
            for part in parts]


class BaseAgentRunner:
    """Runner protocol: subclasses build the primary argv; verification
    chaining is shared -- `verify_command` (split once via
    _split_verify, never executed through a shell) runs ONLY after a
    zero primary exit and its exit code becomes the final exit_code."""

    name = 'base'

    def primary_argv(self, worker: Mapping[str, Any],
                     worktree: Path) -> list[str]:
        raise NotImplementedError

    def execute(self, worker_spec: Mapping[str, Any],
                worktree_path: Path, timeout: float) -> RunnerResult:
        started = time.monotonic()
        argv = self.primary_argv(worker_spec, worktree_path)
        code, out, err = _spawn(argv, worktree_path, timeout)
        verify = worker_spec.get('verify_command')
        verify_ran = False
        verify_exit: int | None = None
        if code == 0 and isinstance(verify, str) and verify.strip():
            verify_ran = True
            verify_exit, vout, verr = _spawn(
                _split_verify(verify), worktree_path, timeout)
            out, err = out + vout, err + verr
            code = verify_exit
        return RunnerResult(
            exit_code=code, stdout=out, stderr=err,
            duration_s=round(time.monotonic() - started, 6),
            audit_metadata={
                'worker_id': worker_spec.get('id'),
                'runner': self.name,
                'cwd': str(worktree_path),
                'argv0': argv[0] if argv else None,
                'verify_command': verify,
                'verify_ran': verify_ran,
                'verify_exit': verify_exit,
            })


class CommandRunner(BaseAgentRunner):
    """Default runner: raw argv from `cmd` -- the backward-compatible
    path for every existing worker spec."""

    name = 'command'

    def primary_argv(self, worker: Mapping[str, Any],
                     worktree: Path) -> list[str]:
        return list(worker['cmd'])


def antigravity_argv(worker: Mapping[str, Any],
                     worktree: Path) -> list[str]:
    """Headless invocation + injected task context: prompt, target
    files (`writes`) and the acceptance command travel inside ONE
    --task element (one argv member, never a shell string)."""
    prompt = str(worker.get('prompt') or '').strip()
    parts = [prompt, '', 'Target files (only these may change):']
    parts += [str(item) for item in (worker.get('writes') or [])]
    verify = worker.get('verify_command')
    if isinstance(verify, str) and verify.strip():
        parts += ['', 'Acceptance test (must pass):', verify]
    return [ANTIGRAVITY_BINARY, '--workspace', str(worktree),
            '--non-interactive', '--task', '\n'.join(parts)]


class AntigravityRunner(BaseAgentRunner):
    """Google Antigravity adapter (triggers: `agent: "antigravity"` or
    `runner: {"type": "antigravity", ...}`). cwd is locked to the
    worktree by _spawn; verify_command chains after an agent exit of 0."""

    name = 'antigravity'

    def primary_argv(self, worker: Mapping[str, Any],
                     worktree: Path) -> list[str]:
        return antigravity_argv(worker, worktree)


def dispatch_runner(worker: Mapping[str, Any]) -> BaseAgentRunner:
    """Select the runner for a validated worker; unknown kinds fail
    closed (validate_workers rejects them before dispatch normally)."""
    kind = runner_kind(worker)
    if kind == 'command':
        return CommandRunner()
    if kind == 'antigravity':
        return AntigravityRunner()
    raise OrchestratorError(
        f'unknown agent/runner {kind!r} '
        f'(known: {sorted(KNOWN_RUNNERS)})')
