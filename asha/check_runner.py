"""Isolated subprocess check runner for the ship gate.

Runs the mandatory check matrix resolved by scope_resolver.SCOPE_CHECKS in
the target repository root, capturing exact exit codes, stdout/stderr tails
and wall-clock duration. Execution logic lives here so control.py only
delegates. Every command is fail-closed: nonzero exit -> status "failed".
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from asha.execution import (
    EXECUTION_LAUNCH_FAILED,
    ExecutionOutcome,
    spawn_capture,
)
from asha.governance.env_resolver import (
    EnvironmentResolver,
    ServiceEnvironment,
    resolve_env_for_file,
)
from asha.timeouts import DEFAULT_TIMEOUTS

TAIL_CHARS = 2000


def validation_digest(text: str) -> str:
    """SHA-256 of a captured stream (output identity, no retention)."""
    return hashlib.sha256((text or '').encode('utf-8', 'replace')).hexdigest()

# Single-check timeout budget. Measured evidence: the full suite takes
# 734s on the reference host (rehearsal-v2 reproduction in a retained
# worker worktree), so the former 600s budget rejected every
# full-scope worker with an unattributable UNKNOWN delta verdict.
# 900s = measured runtime + ~20% headroom. Timeout itself stays
# FAILED + verdict UNKNOWN -- never a pass, never PRE_EXISTING_ONLY.
CHECK_TIMEOUT_S = DEFAULT_TIMEOUTS.check
# Baseline passes run concurrently (one per worker thread, ThreadPoolExecutor)
# and each is a full-suite run: measured 874-895s solo, >900s under pair
# contention (rehearsal-v4 baseline artifacts: exit -1, zero identities).
# Baseline gets 2x solo margin; worker verification keeps CHECK_TIMEOUT_S.
# ponytail: if a concurrent pair still exceeds this, compute ONE shared
# baseline (requires proof of validity across worker worktrees) or serialize
# the baseline passes.
BASELINE_TIMEOUT_S = DEFAULT_TIMEOUTS.baseline

# How each check's `scope` field is reported in evidence.json.
CHECK_SCOPE = {
    'ruff': 'changed_files',
    'pytest': 'package',
    'mypy': 'dependency_graph',
}

# pytest's documented exit codes. Only 1 means the SUITE judged the change:
# 2/3/4 mean the RUNNER never produced a verdict (interrupted, internal
# error, usage/config error -- e.g. an unresolvable test path), 5 means
# nothing was collected. Every nonzero code stays `failed` (fail-closed; the
# exit code itself is the authority, see gate_states.check_runner contract) --
# the note only keeps "the tests failed" and "the runner never ran them"
# distinguishable in evidence.json.
_PYTEST_EXIT_NOTES = {
    1: 'test_failures',
    2: 'pytest_interrupted',
    3: 'pytest_internal_error',
    4: 'pytest_usage_or_config_error',
    5: 'no_tests_collected',
}


def _pytest_exit_note(exit_code: int) -> str:
    """Informational label for a nonzero pytest exit code."""
    return _PYTEST_EXIT_NOTES.get(exit_code, 'pytest_failed')


class CheckRunnerError(Exception):
    """A check could not be executed at all (fail-closed)."""


def _commands(root: Path, names: list[str], changed_py: list[str]) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    py = sys.executable
    for name in names:
        if name == 'ruff':
            target = changed_py if changed_py else ['.']
            # concise keeps _DIAG_RE's path:line:col identity stable
            # (ruff 0.16.x emits full/` --> path:line:col` layout by
            # default, which is intentionally NOT parsed -- zero
            # identities must stay fail-closed UNKNOWN, never a pass).
            commands.append((name, [py, '-m', 'ruff', 'check',
                                    '--output-format=concise', *target]))
        elif name == 'pytest':
            # Test discovery: a `tests/` directory stays the configured
            # contract when present (unchanged behavior). Without it, pass
            # NO path so pytest resolves collection from the invocation
            # root (cwd = repository root) through the project's own
            # configuration (rootdir/testpaths/configfile). The previous
            # hardcoded 'tests/' made every root-level suite exit 4
            # (usage error: "file or directory not found") and surface as
            # EXECUTION_FAILED on code whose tests passed.
            target = ['tests/'] if (root / 'tests').is_dir() else []
            commands.append((name, [py, '-m', 'pytest', *target, '-q']))
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




def _service_env(root: Path, changed_py: list[str],
                 deps_root: Path | None = None) -> ServiceEnvironment | None:
    """Resolve the service environment for the check run.

    ALL changed Python files must belong to the SAME service: a worker's
    check run executes every changed file inside ONE interpreter, so a
    scope that spans two services would check at least one file with the
    wrong environment. That case fails closed (never silently picks the
    first service). With no changed files the repository root is the
    service root. Returns None when the environment could not be resolved
    (fail-closed policy disabled fallback already raised; a missing .venv
    with fallback enabled yields a non-hermetic environment which is still
    returned).

    ``deps_root`` (the original checkout) is passed when ``root`` is an
    isolated worktree: dependencies are resolved there, while imports stay
    bound to ``root`` so the worktree's own source is what gets validated.
    """
    if not changed_py:
        return resolve_env_for_file(
            root, root, deps_root=deps_root,
            source_root=root if deps_root is not None else None)
    first_root = EnvironmentResolver().service_root_for(
        root, root / changed_py[0])
    for path in changed_py[1:]:
        other_root = EnvironmentResolver().service_root_for(
            root, root / path)
        if other_root != first_root:
            raise CheckRunnerError(
                "cross-service check run: changed files span "
                f"{first_root} and {other_root}; a worker may only "
                "execute checks within one service"
            )
    return resolve_env_for_file(
        root, root / changed_py[0], deps_root=deps_root,
        source_root=root if deps_root is not None else None)


def _env_argv(env: ServiceEnvironment | None, name: str, argv: list[str]) -> list[str]:
    """Swap the interpreter for the resolved environment.

    pytest uses the local pytest launcher when present (it carries the
    service's plugin context); every other check uses the service Python.
    The command shape (module invocation) is preserved.
    """
    if env is None:
        return argv
    if not argv:
        return argv
    if name == "pytest" and env.pytest_bin is not None:
        # pytest_bin is the launcher itself: `-m pytest` is a marker
        # filter, not a module flag -- drop argv[1:3] or pytest would
        # select only tests marked "pytest" (silently exit 5).
        rest = argv[3:] if len(argv) > 3 and argv[1] == "-m" and argv[2] == "pytest" else argv[1:]
        return [str(env.pytest_bin), *rest]
    if Path(env.python_bin) != Path(sys.executable):
        # Hermetic service interpreter, or a worktree-bound run whose
        # dependencies come from the original checkout's venv.
        return [str(env.python_bin), *argv[1:]]
    return argv


def _env_cwd_env(env: ServiceEnvironment | None, root: Path) -> tuple[Path, dict[str, str]]:
    """Execution cwd + environment for a resolved service.

    cwd = service_root when hermetic (the service's own layout), else the
    repository root (historical behavior). env = an isolated copy of the
    process environment with the resolved service vars merged over it --
    including a NON-hermetic bound run, whose PYTHONPATH must still point
    at the worktree (the process environment is never mutated)."""
    proc_env = os.environ.copy()
    if env is None:
        return root, proc_env
    proc_env.update(env.env_vars)
    if env.source_root is not None:
        # Bound run: execute IN the source root (the worktree). The
        # service root belongs to the dependency checkout and is only
        # used to locate the interpreter.
        return root, proc_env
    cwd = env.service_root if env.is_hermetic else root
    return cwd, proc_env

def _exec_path_args(env: ServiceEnvironment | None, root: Path,
                    repo_relative: list[str]) -> list[str]:
    """Translate repo-relative paths into the execution cwd's coordinate
    system (conversion happens only at the execution boundary; governance
    representations stay repo-relative).

    Hermetic service (cwd = service_root): a path under the service root
    becomes service-relative; a path outside it fails closed -- silently
    rewriting it would run a test with the wrong interpreter. Non-hermetic
    and root cwd: paths are passed through unchanged (historical behavior).
    """
    if (env is None or not env.is_hermetic or env.service_root == root
            or env.source_root is not None):
        # Bound runs execute at the source root, so repo-relative paths
        # are already in the execution cwd's coordinate system.
        return list(repo_relative)
    # Normalize to forward slashes: on Windows Path.relative_to() yields
    # backslash paths while governance repo-relative paths are always
    # forward-slash. Matching on as_posix() keeps the translation
    # platform-independent (see test_exec_path_args on win32).
    prefix = env.service_root.relative_to(root).as_posix() + '/'
    out: list[str] = []
    for rel in repo_relative:
        if rel.startswith(prefix):
            out.append(rel[len(prefix):])
        else:
            raise CheckRunnerError(
                'test path outside selected service: '
                f'{rel} not under {env.service_root.relative_to(root).as_posix()}/'
            )
    return out


_BIND_PROBE = (
    "import importlib.util as u, sys\n"
    "spec = u.find_spec(sys.argv[1])\n"
    "if spec is None:\n"
    "    print('')\n"
    "else:\n"
    "    locs = list(spec.submodule_search_locations or [])\n"
    "    print(locs[0] if locs else (spec.origin or ''))\n"
)


def _top_packages(root: Path, changed_py: list[str]) -> list[str]:
    """Top-level packages owned by the changed files (import identity
    targets). Empty when the change has no package context (single module,
    docs, tests only), in which case binding is still applied but there is
    no package origin to assert."""
    packages: set[str] = set()
    for rel in changed_py:
        parts = Path(rel).parts
        if parts and parts[0] == 'src':
            parts = parts[1:]
        if len(parts) < 2:
            continue
        name = parts[0]
        src_pkg = root / 'src' / name
        flat_pkg = root / name
        base = src_pkg if src_pkg.is_dir() else flat_pkg
        if (base / '__init__.py').is_file():
            packages.add(name)
    return sorted(packages)


def verify_source_binding(env: ServiceEnvironment | None, root: Path,
                          changed_py: list[str]) -> dict[str, str]:
    """Fail closed unless every changed-file package imports from ``root``.

    A worktree-bound run must validate the worktree's own source. Losing
    the venue -- a venv that only exists in the original checkout, an
    editable install, an inherited ``PYTHONPATH``, user site-packages --
    silently swaps in another copy and produces a PASS about the wrong
    bytes. This probe makes that outcome an explicit execution error.
    """
    if env is None:
        raise CheckRunnerError('source binding: no resolved environment')
    root = Path(root).resolve()
    cwd, proc_env = _env_cwd_env(env, root)
    origins: dict[str, str] = {}
    for pkg in _top_packages(root, changed_py):
        try:
            proc = subprocess.run(
                [str(env.python_bin), '-c', _BIND_PROBE, pkg],
                cwd=cwd, capture_output=True, text=True, timeout=60,
                env=proc_env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CheckRunnerError(
                f'source binding probe failed for {pkg}: {exc}') from exc
        origin = (proc.stdout or '').strip()
        if proc.returncode != 0 or not origin:
            raise CheckRunnerError(
                'source binding: cannot resolve package '
                f'{pkg} from {root} (rc={proc.returncode}): '
                f'{(proc.stderr or "").strip()[-400:]}')
        resolved = Path(origin).resolve()
        if resolved != root and root not in resolved.parents:
            raise CheckRunnerError(
                f'source binding: package {pkg} resolves to {resolved}, '
                f'outside {root} -- refusing to validate another checkout')
        origins[pkg] = str(resolved)
    return origins


def _outcome_tail(result: ExecutionOutcome) -> str:
    """Retained diagnostics for a check that did not exit on its own: the
    partial output produced before the kill plus one explicit line naming
    the outcome, the budget and what was verified about termination. Trimmed
    to TAIL_CHARS and passed through the secret redactor (this tail is
    retained in the FAILED worker outcome and reaches the run report).
    Without it a timeout surfaces as an empty diagnostic set and the delta
    verdict degrades to an unexplained UNKNOWN."""
    partial = result.stdout or ""
    if result.outcome == 'deadline_exceeded':
        reason = ('deadline exceeded after '
                  f'{result.timeout_s:g}s (task budget)')
    else:
        reason = f'timed out after {result.timeout_s:g}s'
    joined = (partial + ("\n" if partial else "")
              + reason
              + f"; process_termination={result.process_termination}"
              + f"; {result.termination_detail}")
    return _redact_env(joined[-TAIL_CHARS:])


def _service_groups(root: Path,
                    changed_py: list[str]) -> list[tuple[str, list[str]]]:
    """Changed paths grouped by owning service root, deterministically.

    A multi-service diff is not a refusal: each service runs the same matrix
    in its own environment. The grouping uses the resolver's own marker walk,
    so there is no second notion of "service" in this module.
    """
    if not changed_py:
        return [('.', [])]
    resolver = EnvironmentResolver()
    groups: dict[str, list[str]] = {}
    for rel in changed_py:
        service_root = resolver.service_root_for(root, root / rel)
        try:
            label = service_root.relative_to(root).as_posix() or '.'
        except ValueError:
            label = '.'
        groups.setdefault(label, []).append(rel)
    return sorted(groups.items())


def _blocked_group(reason: str, names: list[str], label: str) -> list[dict]:
    """Entries for a group whose environment never resolved.

    Recorded as failed + launched False so the aggregate reports BLOCKED:
    not a test failure, and never a silent omission.
    """
    return [{
        'name': name,
        'scope': CHECK_SCOPE[name],
        'status': 'failed',
        'launched': False,
        'exit_code': None,
        'note': 'service ' + label + ': ' + reason,
        'service': label,
    } for name in names]


def _tool_unavailable(name: str, output: str) -> str | None:
    """Name the check tool that was never installed, or None.

    Only the check's OWN tool counts: a test that imports a missing
    module is a real failure and must stay one.
    """
    text = output or ''
    for marker in (f"No module named '{name}'", f'No module named "{name}"',
                   f'{name}: command not found',
                   f'No module named {name}'):
        if marker in text:
            return name
    return None

def _run_group(root: Path, names: list[str], changed_py: list[str],
               label: str, timeout_s: float,
               deps_root: Path | None) -> list[dict]:
    """Run the matrix for one service group in that service's environment."""
    try:
        env = _service_env(root, changed_py, deps_root)
        if deps_root is not None:
            verify_source_binding(env, root, changed_py)
        cwd, proc_env = _env_cwd_env(env, root)
    except (CheckRunnerError, OSError) as exc:
        return _blocked_group(f'{type(exc).__name__}: {exc}', names, label)
    results: list[dict] = []
    for name, argv in _commands(root, names, changed_py):
        entry: dict = {
            'name': name,
            'scope': CHECK_SCOPE[name],
            'status': 'passed',
            'launched': True,
            'exit_code': 0,
            'service': label,
        }
        if not argv:
            entry['status'] = 'skipped'
            entry['launched'] = False
            entry['note'] = 'no applicable target'
            results.append(entry)
            continue
        argv = _env_argv(env, name, argv)
        result = spawn_capture(argv, cwd=cwd, timeout_s=timeout_s,
                               env=proc_env)
        entry['launched'] = result.launched
        entry['exit_code'] = result.exit_code
        entry['termination'] = result.outcome
        entry['process_termination'] = result.process_termination
        entry['timeout_s'] = result.timeout_s
        entry['duration_ms'] = result.duration_ms
        if result.outcome == EXECUTION_LAUNCH_FAILED:
            entry['status'] = 'failed'
            entry['note'] = 'launch failed: ' + result.termination_detail
            entry['output_tail'] = redact_secrets(result.stderr[-TAIL_CHARS:])
            results.append(entry)
            continue
        if result.timed_out:
            entry['status'] = 'failed'
            entry['note'] = ('deadline exceeded after '
                             f'{result.timeout_s:g}s'
                             if result.outcome == 'deadline_exceeded'
                             else f'timed out after {result.timeout_s:g}s')
            entry['output_tail'] = _outcome_tail(result)
            results.append(entry)
            continue
        combined = (result.stdout or '') + (result.stderr or '')
        missing = _tool_unavailable(name, combined)
        if missing:
            # the tool was never installed: an environment gap, not a
            # failure of the code under test, and never an unparseable
            # baseline that blames the worker. The command DID run, so
            # `launched` stays True: "skipped" is the verdict, not
            # "no process existed".
            entry['status'] = 'skipped'
            entry['note'] = 'tool unavailable: ' + missing
        else:
            entry['status'] = 'passed' if result.exit_code == 0 else 'failed'
            if name == 'pytest' and result.exit_code != 0:
                entry['note'] = _pytest_exit_note(int(result.exit_code or 0))
        entry['output_tail'] = redact_secrets(combined[-TAIL_CHARS:])
        entry['stdout_sha256'] = validation_digest(result.stdout or '')
        entry['stderr_sha256'] = validation_digest(result.stderr or '')
        results.append(entry)
    return results


def run(root: Path | str, resolved: dict,
        timeout_s: float = CHECK_TIMEOUT_S,
        *, deps_root: Path | None = None) -> list[dict]:
    """Execute the mandatory checks for a resolved scope.

    One group per service root: each runs the full matrix for its own files
    in its own environment, so a diff spanning services is validated instead
    of refused. A group whose environment cannot resolve is recorded as
    failed + launched False (aggregate BLOCKED); other groups still report.

    ``deps_root`` binds the run to the worktree's source while taking
    dependencies from the original checkout; it also enables the
    fail-closed source-identity probe.
    """
    root = Path(root).resolve()
    names = resolved['checks']
    changed_py = [f for f in resolved['affected_files']
                  if f.endswith(('.py', '.pyi')) and (root / f).is_file()]
    results: list[dict] = []
    for label, paths in _service_groups(root, changed_py):
        results.extend(_run_group(root, names, paths, label, timeout_s,
                                  deps_root))
    return results


_SCOPED_CHECK_SCOPE = {
    'ruff': 'changed_files',
    'pytest': 'targeted_tests',
    'mypy': 'dependency_graph',
}

_SECRET_NAME_RE = re.compile(r'SECRET|TOKEN|PASSWORD|PASSWD|CRED|API_?KEY|_KEY$', re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Mechanical privacy scrub (Phase 5.0 spec 6.8): remove values of
    secret-looking environment variables from captured output.

    Public because every artifact that can retain argv or process output
    (execution records, validation entries) passes through it before sealing.
    """
    for name, value in os.environ.items():
        if len(value) < 6 or not _SECRET_NAME_RE.search(name):
            continue
        if value in text:
            text = text.replace(value, '[REDACTED]')
    return text


_redact_env = redact_secrets


def run_scoped(
    root: Path,
    *,
    changed_files: list[str],
    targeted_tests: list[str],
    mypy_targets: list[str],
    timeout: float = CHECK_TIMEOUT_S,
    deps_root: Path | None = None,
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
    changed_py = [
        file for file in changed_files
        if file.endswith(('.py', '.pyi')) and (root / file).is_file()
    ]
    env = _service_env(root, changed_py, deps_root)
    if deps_root is not None:
        verify_source_binding(env, root, changed_py)
    py = str(env.python_bin) if env is not None else sys.executable
    cwd, proc_env = _env_cwd_env(env, root)
    scoped_mypy = [
        file for file in mypy_targets
        if file.endswith(('.py', '.pyi')) and (root / file).is_file()
        and not ((root / file).with_suffix('') / '__init__.py').is_file()
    ]
    commands: list[tuple[str, list[str]]] = [
        ('ruff', [py, '-m', 'ruff', 'check', '--output-format=concise',
                  *changed_py] if changed_py else []),
        ('pytest', [py, '-m', 'pytest', *_exec_path_args(env, root, targeted_tests), '-q', '--tb=line'] if targeted_tests else []),
        ('mypy', [py, '-m', 'mypy', *scoped_mypy] if scoped_mypy else []),
    ]
    results: list[dict[str, Any]] = []
    for name, command in commands:
        command = _env_argv(env, name, command)
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
        result = spawn_capture(command, cwd=cwd, timeout_s=timeout,
                               env=proc_env)
        entry['exit_code'] = result.exit_code
        entry['termination'] = result.outcome
        entry['process_termination'] = result.process_termination
        entry['timeout_s'] = result.timeout_s
        entry['launched'] = result.launched
        entry['duration_ms'] = result.duration_ms
        if result.outcome == EXECUTION_LAUNCH_FAILED:
            entry['status'] = 'failed'
            entry['note'] = 'launch failed: ' + result.termination_detail
            entry['output_tail'] = _redact_env(result.stderr[-TAIL_CHARS:])
            results.append(entry)
            continue
        if result.timed_out:
            entry['status'] = 'failed'
            entry['note'] = ('deadline exceeded after '
                             f'{result.timeout_s:g}s'
                             if result.outcome == 'deadline_exceeded'
                             else f'timed out after {result.timeout_s:g}s')
            entry['output_tail'] = _outcome_tail(result)
            results.append(entry)
            continue
        if name == 'pytest' and result.exit_code == 5:
            # pytest's documented exit code 5 = no tests collected. The
            # authorized target set contained nothing runnable (e.g. a
            # non-test module under tests/): record it as an explicit
            # skip with a machine-readable note -- never as a failure
            # (that would make a sound containment proof look broken)
            # and never silently (the entry must exist).
            entry['status'] = 'skipped'
            entry['note'] = 'no_tests_collected'
        else:
            entry['status'] = 'passed' if result.exit_code == 0 else 'failed'
        combined = _redact_env((result.stdout or '') + (result.stderr or ''))
        entry['output_tail'] = combined[-TAIL_CHARS:]
        results.append(entry)
    return results
