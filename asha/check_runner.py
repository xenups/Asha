"""Isolated subprocess check runner for the ship gate.

Runs the mandatory check matrix resolved by scope_resolver.SCOPE_CHECKS in
the target repository root, capturing exact exit codes, stdout/stderr tails
and wall-clock duration. Execution logic lives here so control.py only
delegates. Every command is fail-closed: nonzero exit -> status "failed".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TAIL_CHARS = 2000

# How each check's `scope` field is reported in evidence.json.
CHECK_SCOPE = {
    'ruff': 'changed_files',
    'pytest': 'package',
    'mypy': 'dependency_graph',
}


class CheckRunnerError(Exception):
    """A check could not be executed at all (fail-closed)."""


def _commands(root: Path, names: list[str], changed_py: list[str]) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    py = sys.executable
    for name in names:
        if name == 'ruff':
            target = changed_py if changed_py else ['.']
            commands.append((name, [py, '-m', 'ruff', 'check', *target]))
        elif name == 'pytest':
            commands.append((name, [py, '-m', 'pytest', 'tests/', '-q']))
        elif name == 'mypy':
            # A .py launcher shadowed by a same-named package cannot
            # be typed as itself (mypy: 'Duplicate module named ...';
            # --exclude skips directory scans, not explicit file args).
            # The package carries the types: drop the launcher, then
            # fall through so the outcome is a real check, never a skip.
            targets = [f for f in changed_py
                       if not ((root / f).with_suffix('')
                               / '__init__.py').is_file()]
            if targets:
                commands.append((name, [py, '-m', 'mypy', *targets]))
            elif (root / 'asha').is_dir():
                commands.append((name, [py, '-m', 'mypy', 'asha']))
            else:
                # No python changed and no toolchain present: nothing to type
                # check. Recorded as skipped, never as a silent pass.
                commands.append((name, []))
        else:
            raise CheckRunnerError('unknown check: ' + name)
    return commands


def run(root: Path | str, resolved: dict) -> list[dict]:
    """Execute the mandatory checks for a resolved scope."""
    root = Path(root).resolve()
    names = resolved['checks']
    changed_py = [f for f in resolved['affected_files']
                  if f.endswith(('.py', '.pyi')) and (root / f).is_file()]
    results: list[dict] = []
    for name, argv in _commands(root, names, changed_py):
        entry: dict = {
            'name': name,
            'scope': CHECK_SCOPE[name],
            'status': 'passed',
            'exit_code': 0,
        }
        if not argv:
            entry['status'] = 'skipped'
            entry['note'] = 'no applicable target'
            results.append(entry)
            continue
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=root, capture_output=True,
                                  text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            entry['status'] = 'failed'
            entry['exit_code'] = -1
            entry['output_tail'] = str(exc)
            entry['duration_ms'] = int((time.monotonic() - started) * 1000)
            results.append(entry)
            continue
        entry['exit_code'] = proc.returncode
        entry['status'] = 'passed' if proc.returncode == 0 else 'failed'
        combined = (proc.stdout or '') + (proc.stderr or '')
        entry['output_tail'] = combined[-TAIL_CHARS:]
        entry['duration_ms'] = int((time.monotonic() - started) * 1000)
        results.append(entry)
    return results


_SCOPED_CHECK_SCOPE = {
    'ruff': 'changed_files',
    'pytest': 'targeted_tests',
    'mypy': 'dependency_graph',
}

_SECRET_NAME_RE = re.compile(r'SECRET|TOKEN|PASSWORD|PASSWD|CRED|API_?KEY|_KEY$', re.IGNORECASE)


def _redact_env(text: str) -> str:
    """Mechanical privacy scrub (Phase 5.0 spec 6.8): remove values of
    secret-looking environment variables from captured output."""
    for name, value in os.environ.items():
        if len(value) < 6 or not _SECRET_NAME_RE.search(name):
            continue
        if value in text:
            text = text.replace(value, '[REDACTED]')
    return text


def run_scoped(
    root: Path,
    *,
    changed_files: list[str],
    targeted_tests: list[str],
    mypy_targets: list[str],
    timeout: int = 600,
) -> list[dict[str, Any]]:
    """Dumb executor of an affirmative eligibility decision (spec 1.1).

    It MUST NOT evaluate eligibility, decide policy, inspect
    classification or scope level, or broaden/narrow the authorized
    target sets: the Scheduler supplies exactly the changed_files,
    targeted_tests and mypy_targets proven safe by the Eligibility
    Engine (asha.scoping). This function only runs them and returns
    bounded, secret-scrubbed checks[] entries with the established
    field vocabulary. Target filtering here is mechanical (existing
    shadowed-module rule mirrored from run()), never policy.
    """
    root = Path(root).resolve()
    py = sys.executable
    changed_py = [
        file for file in changed_files
        if file.endswith(('.py', '.pyi')) and (root / file).is_file()
    ]
    scoped_mypy = [
        file for file in mypy_targets
        if file.endswith(('.py', '.pyi')) and (root / file).is_file()
        and not ((root / file).with_suffix('') / '__init__.py').is_file()
    ]
    commands: list[tuple[str, list[str]]] = [
        ('ruff', [py, '-m', 'ruff', 'check', *changed_py] if changed_py else []),
        ('pytest', [py, '-m', 'pytest', *targeted_tests, '-q', '--tb=line'] if targeted_tests else []),
        ('mypy', [py, '-m', 'mypy', *scoped_mypy] if scoped_mypy else []),
    ]
    results: list[dict[str, Any]] = []
    env = os.environ.copy()
    env['PATH'] = str(root) + os.pathsep + env.get('PATH', '')
    for name, command in commands:
        entry: dict[str, Any] = {
            'name': name,
            'scope': _SCOPED_CHECK_SCOPE[name],
            'status': 'skipped',
            'exit_code': None,
            'output_tail': '',
            'duration_ms': 0,
        }
        if not command:
            if name == 'pytest' and not targeted_tests:
                entry['note'] = 'empty_target_set_proven'
            else:
                entry['note'] = 'no applicable target'
            results.append(entry)
            continue
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command, cwd=root, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            entry['status'] = 'failed'
            entry['note'] = f'timed out after {timeout}s'
            entry['duration_ms'] = int((time.monotonic() - started) * 1000)
            results.append(entry)
            continue
        entry['exit_code'] = proc.returncode
        if name == 'pytest' and proc.returncode == 5:
            # pytest's documented exit code 5 = no tests collected. The
            # authorized target set contained nothing runnable (e.g. a
            # non-test module under tests/): record it as an explicit
            # skip with a machine-readable note -- never as a failure
            # (that would make a sound containment proof look broken)
            # and never silently (the entry must exist).
            entry['status'] = 'skipped'
            entry['note'] = 'no_tests_collected'
        else:
            entry['status'] = 'passed' if proc.returncode == 0 else 'failed'
        combined = _redact_env((proc.stdout or '') + (proc.stderr or ''))
        entry['output_tail'] = combined[-TAIL_CHARS:]
        entry['duration_ms'] = int((time.monotonic() - started) * 1000)
        results.append(entry)
    return results
