#!/usr/bin/env python3
"""Isolated subprocess check runner for the ship gate.

Runs the mandatory check matrix resolved by scope_resolver.SCOPE_CHECKS in
the target repository root, capturing exact exit codes, stdout/stderr tails
and wall-clock duration. Execution logic lives here so control.py only
delegates. Every command is fail-closed: nonzero exit -> status "failed".
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

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
            if changed_py:
                commands.append((name, [py, '-m', 'mypy', *changed_py]))
            elif (root / '.hermes' / 'tools').is_dir():
                commands.append((name, [py, '-m', 'mypy', '.hermes/tools']))
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
