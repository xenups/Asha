"""Phase 5.2.2 -- stable developer CLI: a thin adapter over the
existing engine. Presentation, determinism, diagnostics, exit codes.
NO policy: every semantic field below is copied verbatim from
``scoping.assess_scoping_eligibility`` and the existing executors.

Commands and flags
------------------
    asha [--root PATH] [--paths PATH ...] [--json] [--no-execute]
    asha run --spec FILE [--apply] [--keep-worktrees]
        legacy scheduler CLI -- passes through to ``asha.scheduler.main``
        unchanged (first positional token ``run`` selects it).

Stream contract
---------------
    stdout  human result block, or EXACTLY ONE JSON document with
            ``--json`` (never progress logs).
    stderr  diagnostics, warnings, operational errors.

Exit codes (documented classes)
-------------------------------
    0    validation completed (SCOPED targeted or canonical COMPLETE),
         or NO_CHANGES, or evaluation-only (--no-execute) success.
    1    validation failed (a check reported failed).
    2    operational/engine error (not a git repo, engine failure,
         canonical execution refused on an uncommitted target, or
         evidence verification failure).
    130  interrupted (SIGINT) -- never PASS, never fabricated evidence.

JSON contract (schema_version 1): field names stable, enum values match
the engine, missing data is explicit ``null``, no absolute local paths,
deterministic for identical inputs apart from ``duration_ms``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import scope_resolver, scoping
from .classifier import classify_task, governance_profile
from .mcp_server import _dispatch_context, _fast_path_enabled
from .replay import parse_evidence, verify_bytes, verify_record
from .router import RuntimeMode, route
from .scheduler import GovernedScheduler, verify_worker_evidence
from .scheduler import main as scheduler_main

SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130

_SEP = '─' * 29


class CliError(Exception):
    """Operational/engine failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(['git', *args], cwd=root, capture_output=True,
                              text=True, encoding='utf-8', errors='replace',
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError('REPOSITORY_ERROR',
                       f'git {args[0]} failed: {exc}') from exc
    if proc.returncode != 0:
        raise CliError('REPOSITORY_ERROR',
                       f'git {" ".join(args)}: '
                       f'{(proc.stderr or proc.stdout).strip()[:200]}')
    return proc.stdout.strip()


def _repository(root: Path) -> str:
    """Repository identity without absolute local paths: the origin URL,
    else the directory name (both already part of identity contracts)."""
    try:
        proc = subprocess.run(['git', 'remote', 'get-url', 'origin'],
                              cwd=root, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=30)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return root.name


def _require_git_root(root: Path) -> Path:
    if not root.is_dir():
        raise CliError('REPOSITORY_ERROR', f'root is not a directory: {root}')
    toplevel = _git(root, 'rev-parse', '--show-toplevel')
    if not toplevel:
        raise CliError('REPOSITORY_ERROR', 'not a git repository')
    return Path(toplevel).resolve()


def _change_set(root: Path, resolved: dict[str, Any]) -> dict[str, Any]:
    base_ref = str(resolved.get('base') or '')
    base_sha: str | None = None
    if base_ref:
        try:
            base_sha = _git(root, 'rev-parse', base_ref)
        except CliError:
            base_sha = None     # unknown base is explicit null, not fatal
    head_sha = _git(root, 'rev-parse', 'HEAD')
    porcelain = _git(root, 'status', '--porcelain')
    uncommitted = len([line for line in porcelain.splitlines() if line.strip()])
    return {'base': base_ref or None, 'base_sha': base_sha,
            'target_sha': head_sha, 'uncommitted_files': uncommitted}


def evaluate(root: Path, paths: list[str] | None
             ) -> tuple[dict[str, Any], scoping.ScopingDecision | None,
                        Any]:
    """Decision ONLY: resolve -> classify (existing envelope) -> assess.
    Returns (resolved, decision|None, classification) -- the decision is
    the engine's, never reconstructed here."""
    resolved = scope_resolver.resolve(root, paths=paths or None)
    affected = list(resolved.get('affected_files') or [])
    if not affected:
        return resolved, None, None
    task = {'id': 'asha-cli', 'reads': affected, 'writes': affected,
            'declared_scope': affected, 'deps': []}
    envelope = _dispatch_context(root)
    classification = classify_task(task, envelope)
    # the engine consumes the independence CLASSIFICATION (enum value),
    # exactly as the scheduler derives it from the route decision
    independence = getattr(classification, 'classification', None)
    decision = scoping.assess_scoping_eligibility(
        root,
        affected,
        getattr(independence, 'value', str(independence)),
        str(resolved.get('scope') or ''),
        scope_status=str(resolved.get('status') or ''),
        envelope_valid=True,
    )
    if not isinstance(decision, scoping.ScopingDecision):
        raise CliError('ENGINE_ERROR',
                       f'unexpected decision type {type(decision).__name__}')
    return resolved, decision, classification


def _run_sealed(root: Path, affected: list[str], classification: Any
                ) -> dict[str, Any]:
    """Canonical execution of the validation path through the EXISTING
    orchestrator (worktree isolation, sealed evidence, authoritative
    record) -- identical to a client dispatch; no phase-specific or
    invocation-specific execution path exists anywhere in this
    module."""
    envelope = _dispatch_context(root)
    routed = route(governance_profile(classification),
                   fast_path_enabled=_fast_path_enabled())
    wid = 'cli-' + os.urandom(4).hex()
    worker = {'id': wid, 'deps': [], 'declared_scope': list(affected),
              'reads': list(affected), 'writes': [],
              'cmd': [sys.executable, '-c', 'pass']}
    scheduler = GovernedScheduler(
        root, [worker], task_id='cli-' + os.urandom(4).hex(),
        fast_path_enabled=routed.mode is RuntimeMode.FAST_PATH,
        classification_context=envelope)
    report = scheduler.run()
    state = (report.get('states', {}).get(wid) or {}).get('state')
    ev_path = (report.get('evidence') or {}).get(wid)
    if state != 'DONE' or not ev_path:
        raise CliError('EXECUTION_FAILED',
                       f'validation run ended as {state or "UNKNOWN"}')

    payload = json.loads(Path(ev_path).read_text(encoding='utf-8'))
    checks = list(payload.get('checks') or [])
    failed = [entry['name'] for entry in checks
              if entry.get('status') == 'failed']
    validation = 'FAIL' if failed else 'PASS'

    seal_ok = bool(verify_worker_evidence(Path(ev_path)))
    evidence_id = payload.get('evidence_sha256')
    auth = scheduler.authoritative.get(wid, b'')
    record_ok: bool | None = None
    bytes_ok: bool | None = None
    if auth:
        try:
            record_ok = bool(getattr(verify_record(parse_evidence(auth)),
                                     'verified', False))
            bytes_ok = bool(getattr(verify_bytes(auth), 'verified', False))
        except Exception:            # fail closed: never a PASS
            record_ok, bytes_ok = False, False
    if evidence_id and not (seal_ok and record_ok and bytes_ok):
        raise CliError('EVIDENCE_VERIFICATION_FAILED',
                       'sealed evidence failed verification')
    return {'validation_result': validation,
            'evidence_id': evidence_id if seal_ok else None,
            'evidence_verification': ('PASS' if seal_ok and record_ok
                                      else 'FAIL'),
            'replay_verification': 'PASS' if bytes_ok else 'FAIL',
            'error': None}


def execute(root: Path, resolved: dict[str, Any],
            decision: scoping.ScopingDecision, classification: Any, *,
            no_execute: bool) -> dict[str, Any]:
    """Run validation through the existing architecture only.

    Runtime authority (Spec 6.5.5): only the scheduler may take the
    SCOPED runtime path, so ALL execution happens as one synthesized
    validation worker through ``GovernedScheduler`` against the
    COMMITTED target -- identical to the MCP dispatch path: isolated
    worktree, sealed evidence, authoritative record, replay check.
    An uncommitted target is evaluated but never executed: fail closed
    with a documented operational error instead of creating a second
    runtime path.
    """
    if no_execute:
        return {'validation_result': None, 'evidence_id': None,
                'evidence_verification': None, 'replay_verification': None,
                'error': None}
    dirty = bool(_git(root, 'status', '--porcelain').strip())
    if dirty:
        raise CliError(
            'EXECUTION_REQUIRES_COMMITTED_TARGET',
            'validation runs in an isolated worktree of the committed '
            'target; commit the change or pass --no-execute '
            '(evaluation only)')
    affected = list(resolved.get('affected_files') or [])
    return _run_sealed(root, affected, classification)


def _payload_base(root: Path, resolved: dict[str, Any]) -> dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'repository': _repository(root),
        'change_set': _change_set(root, resolved),
        'changed_files': list(resolved.get('affected_files') or []),
        'decision': None,
        'eligible': None,
        'fallback_reason': None,
        'execution_mode': None,
        'validation_result': None,
        'duration_ms': 0,
        'evidence_id': None,
        'evidence_verification': None,
        'replay_verification': None,
        'error': None,
        'status': None,
    }


def render_human(payload: dict[str, Any]) -> str:
    if payload.get('status') == 'NO_CHANGES':
        return 'Asha\nNo changes detected.\n'
    lines = ['Asha', _SEP,
             f"Changes       {len(payload['changed_files'])} files",
             f"Decision      {payload['decision']}"]
    if payload.get('fallback_reason'):
        lines.append(f"Reason        {payload['fallback_reason']}")
    lines.append(f"Execution     {payload['execution_mode']}")
    if payload.get('validation_result'):
        lines.append(f"Validation    {payload['validation_result']}")
    if payload.get('evidence_id'):
        lines.append(f"Evidence      {payload['evidence_id'][:12]}")
    lines.append(f"Duration      {payload['duration_ms'] / 1000.0:.2f}s")
    return '\n'.join(lines) + '\n'


def _emit(payload: dict[str, Any], as_json: bool,
          stream: Any = None) -> None:
    # resolve the stream at call time so capture layers see the output
    stream = sys.stdout if stream is None else stream
    if as_json:
        stream.write(json.dumps(payload, sort_keys=True,
                                ensure_ascii=False) + '\n')
    else:
        stream.write(render_human(payload))
    stream.flush()


def _legacy_run(argv: list[str]) -> bool:
    """Detect `asha [--root X] run ...` for scheduler passthrough. The
    first positional token decides; `--paths` switches to our mode so a
    file literally named `run` stays a path."""
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token.startswith('-'):
            if token == '--root':
                skip = True
            elif token == '--paths':
                return False
            continue
        return token == 'run'
    return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if _legacy_run(argv):
        return scheduler_main(argv)

    parser = argparse.ArgumentParser(
        prog='asha',
        description='Asha governance CLI (stable contract, schema v1).',
        epilog=('exit codes: 0 validation completed / NO_CHANGES / '
                'evaluation-only, 1 validation failed, 2 operational or '
                'engine error, 130 interrupted. '
                '--json writes exactly one JSON document to stdout; '
                'diagnostics go to stderr. '
                '`asha run --spec FILE [--apply]` is the legacy '
                'scheduler CLI and passes through unchanged.'))
    parser.add_argument('--root', default='.',
                        help='repository root (default: cwd)')
    parser.add_argument('--paths', nargs='+', metavar='PATH',
                        help='explicit target files (scope-resolver '
                             'path mechanism)')
    parser.add_argument('--json', action='store_true', dest='as_json',
                        help='emit exactly one JSON document on stdout')
    parser.add_argument('--no-execute', action='store_true',
                        help='evaluate governance only; run no checks')
    args = parser.parse_args(argv)

    started = time.monotonic()
    as_json = bool(args.as_json)
    root = Path(args.root).expanduser()
    payload: dict[str, Any] | None = None
    exit_code = EXIT_OK
    try:
        root = _require_git_root(root)
        resolved, decision, classification = evaluate(root, args.paths)
        payload = _payload_base(root, resolved)
        if decision is None:
            payload['status'] = 'NO_CHANGES'
        else:
            payload['decision'] = decision.mode
            payload['eligible'] = bool(decision.eligible)
            payload['fallback_reason'] = decision.fallback_reason or None
            payload['execution_mode'] = ('targeted' if decision.eligible
                                         else 'canonical')
            outcome = execute(root, resolved, decision, classification,
                              no_execute=bool(args.no_execute))
            payload.update(outcome)
            if payload['validation_result'] == 'FAIL':
                exit_code = EXIT_VALIDATION_FAILED
    except CliError as exc:
        exit_code = EXIT_ERROR
        if payload is None:
            payload = {
                'schema_version': SCHEMA_VERSION,
                'repository': None, 'change_set': None,
                'changed_files': [], 'decision': None, 'eligible': None,
                'fallback_reason': None, 'execution_mode': None,
                'validation_result': None, 'duration_ms': 0,
                'evidence_id': None, 'evidence_verification': None,
                'replay_verification': None, 'error': exc.code,
                'status': None,
            }
        else:
            payload['error'] = exc.code
        print(f'Asha: {exc.message}', file=sys.stderr)
    except KeyboardInterrupt:
        print('Asha: interrupted', file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:        # fail closed, never a traceback
        exit_code = EXIT_ERROR
        code = 'ENGINE_ERROR'
        if payload is not None:
            payload['error'] = code
            payload['duration_ms'] = int((time.monotonic() - started) * 1000)
            _emit(payload, as_json)
        print(f'Asha: {type(exc).__name__}: {exc}', file=sys.stderr)
        return exit_code

    assert payload is not None
    payload['duration_ms'] = int((time.monotonic() - started) * 1000)
    if payload.get('error') and not as_json:
        # human contract: errors go to stderr only (already printed),
        # never a result block on stdout
        return exit_code
    _emit(payload, as_json)
    if payload.get('error') == 'EVIDENCE_VERIFICATION_FAILED':
        return EXIT_ERROR
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
