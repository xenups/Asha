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
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .execution import (
    EXECUTION_DEADLINE_EXCEEDED,
    EXECUTION_EXITED,
    EXECUTION_LAUNCH_FAILED,
    DeadlineExhausted,
    ExecutionLaunchFailed,
    ExecutionTimeout,
    spawn_capture,
)
from .types import OrchestratorError, RunnerResult

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
           timeout: float,
           env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """ONE process primitive for all runners, now ``execution.spawn_capture``.

    Nothing is fabricated: a child that could not be created raises
    ``ExecutionLaunchFailed`` (never a synthetic ``-1`` exit code), an
    exhausted budget raises ``ExecutionTimeout`` carrying the verified
    outcome, and a proven zero is a real zero.
    """
    result = spawn_capture(argv, cwd, timeout_s=timeout, env=env)
    if result.outcome == EXECUTION_LAUNCH_FAILED:
        raise ExecutionLaunchFailed(result)
    if result.outcome == EXECUTION_DEADLINE_EXCEEDED and not result.launched:
        raise DeadlineExhausted(result)
    if result.outcome == EXECUTION_EXITED:
        return (result.exit_code if result.exit_code is not None else 0,
                result.stdout, result.stderr)
    raise ExecutionTimeout(result)


def _acceptance_env(argv: list[str]) -> dict[str, str]:
    """Environment for the acceptance child.

    The acceptance command runs in the CANDIDATE worktree, so whatever it
    writes would be committed and read back as worker output (an
    undeclared write -> INVALID_EVIDENCE). Suppress the two artifacts a
    test run always makes: bytecode caches and pytest's cache dir. The
    command itself is untouched.
    """
    env = dict(os.environ)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    if any('pytest' in token for token in argv):
        add = '-p no:cacheprovider'
        current = env.get('PYTEST_ADDOPTS', '').strip()
        env['PYTEST_ADDOPTS'] = f'{current} {add}'.strip()
    return env


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
        verify_tail = ''
        if code == 0 and isinstance(verify, str) and verify.strip():
            verify_ran = True
            vargv = _split_verify(verify)
            verify_exit, vout, verr = _spawn(
                vargv, worktree_path, timeout, env=_acceptance_env(vargv))
            out, err = out + vout, err + verr
            verify_tail = (vout + verr)[-2000:]
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
                # evidence needs the verify output to tell "passed" from
                # "ran but collected nothing" (skip/deselect tails)
                'verify_output_tail': verify_tail,
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
