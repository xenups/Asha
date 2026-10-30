"""Explicit execution / validation / push-readiness contract (Phase 12).

Three separate questions, each answered from its own inputs:

    execution_status   did the requested command run, and how did it end?
    validation_status  did the REQUIRED check policy run, and did it pass?
    ready_to_push      execution + validation + clean tree + sealed evidence

`authorized_to_ship` is deliberately absent: ship authorization is derived by
`gate_states` from its own evidence chain and is never granted here.

Invariants encoded (and pinned by tests/test_evidence_truth.py):

* a zero exit code is evidence about the COMMAND, never about validation;
* an empty check inventory is NOT_CONFIGURED, never PASS;
* a skipped, blocked, timed-out or unlaunchable check is not a pass;
* validation success cannot be inferred from sealing, cleanliness, or hashes.

Individual check entries keep the runner's established vocabulary
(passed / failed / skipped) plus a ``launched`` flag, which answers exactly
one question: was a process created? A ``failed`` entry with ``launched``
False never started, and the aggregate reports that as BLOCKED. A ``failed``
entry with ``launched`` True DID run -- including one killed at its budget --
and the aggregate reports it as FAIL: "the check ran and did not pass" is not
the same claim as "the check could not be executed".

Phase 12 (schema 2) adds these fields to sealed evidence. Schema 1 artifacts
carry none of them and are therefore NOT validation evidence: consumers treat
them as legacy, never as verified.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from .check_runner import redact_secrets
from .execution import (
    EXECUTION_DEADLINE_EXCEEDED,
    EXECUTION_EXITED,
    EXECUTION_LAUNCH_FAILED,
    spawn_capture,
)
from .timeouts import DEFAULT_TIMEOUTS

VALIDATION_PASS = 'PASS'
VALIDATION_FAIL = 'FAIL'
VALIDATION_BLOCKED = 'BLOCKED'
VALIDATION_NOT_CONFIGURED = 'NOT_CONFIGURED'

VALIDATION_POLICY_NONE = 'none'
VALIDATION_POLICY_EXPLICIT = 'explicit'
VALIDATION_POLICY_SCOPE_MATRIX = 'scope_matrix'

TAIL_CHARS = 2000
#: Compatibility alias: the budget itself lives in asha.timeouts.
VALIDATION_TIMEOUT_S = DEFAULT_TIMEOUTS.validation


def _seconds(budget: float) -> str:
    """Budget as a short label: 900 -> '900', 0.5 -> '0.5'."""
    return f'{budget:g}'


def digest_text(text: str) -> str:
    """SHA-256 of a captured stream, so output can be compared without
    retaining it in the artifact."""
    return hashlib.sha256((text or '').encode('utf-8', 'replace')).hexdigest()


def new_execution_id() -> str:
    """Identity of ONE executed command: a retry is a new execution."""
    return 'exec-' + os.urandom(6).hex()


def execution_record(argv: list[str], cwd_label: str, *,
                     started_at: str, duration_ms: int,
                     exit_code: int | None, termination: str,
                     stdout: str, stderr: str,
                     known_policy_command: bool,
                     execution_id: str | None = None,
                     process_termination: str = 'not_required',
                     timeout_s: float | None = None) -> dict[str, object]:
    """One executed command, described by what actually happened.

    ``known_policy_command`` is False unless the caller declared this command
    as (part of) the validation policy; it never implies success by itself.
    """
    return {
        'execution_id': execution_id or new_execution_id(),
        'argv': [redact_secrets(arg) for arg in argv],
        'cwd': cwd_label,
        'started_at': started_at,
        'duration_ms': int(duration_ms),
        'exit_code': exit_code,
        'termination': termination,
        'process_termination': process_termination,
        'timeout_s': timeout_s,
        'stdout_sha256': digest_text(stdout),
        'stderr_sha256': digest_text(stderr),
        'stdout_tail': redact_secrets(stdout[-TAIL_CHARS:]),
        'stderr_tail': redact_secrets(stderr[-TAIL_CHARS:]),
        'known_policy_command': bool(known_policy_command),
    }


def validation_status(checks: list[dict]) -> tuple[str, str]:
    """(status, rule) for a required-check inventory.

    Fail-closed and total: an empty inventory is NOT_CONFIGURED; a check that
    never launched is BLOCKED; a launched check that failed is FAIL; a skip is
    BLOCKED; PASS needs at least one passed check and nothing else.
    """
    if not checks:
        return VALIDATION_NOT_CONFIGURED, 'no required checks recorded'
    unlaunched = [str(entry.get('name')) for entry in checks
                  if entry.get('status') == 'failed'
                  and entry.get('launched') is False]
    if unlaunched:
        return (VALIDATION_BLOCKED,
                'could not be executed: ' + ', '.join(unlaunched))
    failed = [str(entry.get('name')) for entry in checks
              if entry.get('status') == 'failed']
    if failed:
        return VALIDATION_FAIL, 'failed: ' + ', '.join(failed)
    not_run = [str(entry.get('name')) for entry in checks
               if entry.get('status') in ('blocked', 'skipped')]
    if not_run:
        return VALIDATION_BLOCKED, 'not executed: ' + ', '.join(not_run)
    unknown = [str(entry.get('name')) for entry in checks
               if entry.get('status') != 'passed']
    if unknown:
        return VALIDATION_BLOCKED, 'unrecognised status: ' + ', '.join(unknown)
    return VALIDATION_PASS, 'all required checks passed'


def execution_ok(execution: dict[str, object]) -> bool:
    return (execution.get('termination') == EXECUTION_EXITED
            and execution.get('exit_code') == 0)


def push_readiness(*, execution: dict[str, object], validation: str,
                   clean_tree: bool, evidence_verified: bool
                   ) -> tuple[bool, str]:
    """Push readiness from explicit inputs; the reason names the first
    unsatisfied one so a consumer never has to guess."""
    if not execution_ok(execution):
        return False, 'EXECUTION_FAILED'
    if validation != VALIDATION_PASS:
        return False, 'VALIDATION_' + str(validation)
    if not clean_tree:
        return False, 'WIP_UNCOMMITTED_PASS'
    if not evidence_verified:
        return False, 'EVIDENCE_UNVERIFIED'
    return True, 'READY'


def run_validation_commands(commands: list[list[str]], root: Path,
                            timeout_s: float = VALIDATION_TIMEOUT_S, *,
                            execution_id_factory: Callable[[], str]
                            | None = None
                            ) -> list[dict]:
    """Execute each explicitly configured validation command.

    Every entry records what ran (argv, exit code, duration, output digests,
    bounded tail) and how it ended: ``termination`` is the execution outcome
    (exited / timed_out / deadline_exceeded / launch_failed) and
    ``process_termination`` says what could be verified about ending the
    child. A launch failure or a timeout is failed -- never a pass -- and a
    timed-out check keeps ``launched`` True because a process did exist.

    Each entry is ONE invocation: ``execution_id`` is minted per check and
    attached only when a process actually started (success, non-zero exit or
    timeout), so an entry without an id is an attempt that never reached the
    OS.
    """
    results: list[dict] = []
    for index, argv in enumerate(commands):
        execution_id = (execution_id_factory or new_execution_id)()
        result = spawn_capture(argv, cwd=root, timeout_s=timeout_s)
        entry: dict = {
            'name': 'validation[' + str(index) + ']',
            'scope': 'explicit',
            'status': 'failed',
            'launched': result.launched,
            'exit_code': result.exit_code,
            'termination': result.outcome,
            'process_termination': result.process_termination,
            'timeout_s': result.timeout_s,
            'duration_ms': result.duration_ms,
            'argv': [redact_secrets(arg) for arg in argv],
        }
        if result.launched:
            # the process existed: this attempt has an execution identity
            entry['execution_id'] = execution_id
        if result.outcome == EXECUTION_LAUNCH_FAILED:
            entry['note'] = f'launch failed: {result.termination_detail}'
            entry['output_tail'] = redact_secrets(result.stderr[-TAIL_CHARS:])
        elif result.timed_out:
            entry['note'] = (f'deadline exceeded after {_seconds(timeout_s)}s'
                             if result.outcome == EXECUTION_DEADLINE_EXCEEDED
                             else f'timed out after {_seconds(timeout_s)}s')
            entry['output_tail'] = redact_secrets(
                (result.stdout + result.stderr)[-TAIL_CHARS:])
        else:
            entry['status'] = 'passed' if result.exit_code == 0 else 'failed'
            combined = redact_secrets(result.stdout + result.stderr)
            entry['output_tail'] = combined[-TAIL_CHARS:]
            entry['stdout_sha256'] = digest_text(result.stdout)
            entry['stderr_sha256'] = digest_text(result.stderr)
        results.append(entry)
    return results
