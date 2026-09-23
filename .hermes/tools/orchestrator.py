#!/usr/bin/env python3
"""Asha Orchestrator -- Phase 1 governed worker scheduling (stdlib only).

Dependency readiness and conflict safety are separate mechanisms:

    graphlib.TopologicalSorter -> "are declared dependencies complete?"
    ConflictManager            -> "is it safe to run concurrently?"

TopologicalSorter never answers the second question. Dispatch invariant
(fail-closed, UNKNOWN != SAFE):

    dependency-ready AND scope-safe (known declared scope)
    AND conflict-safe (known read/write sets proven disjoint vs running)
        -> dispatch
    unknown read/write set or unknown/invalid declared scope
        -> defer / block; never optimistically dispatched on
           post-execution evidence that cannot exist beforehand.

After execution a worker reaches done() only with evidence proving:

    exit status 0 AND observed scope within declared scope AND
    verification passed AND Evidence.target_tree_sha == the git tree
    actually verified (re-bound against live git before completion).

Reuses the existing Asha subsystems -- no second evidence system, no
second scope system, no ledger of its own:

    evidence.seal / compute_digest        worker evidence integrity (the
                                          canonical §7 digest machinery;
                                          §7 identity fields ride along
                                          and are covered by the digest)
    scope_resolver.resolve/changed_files  observed scope, S0-S4, checks
    check_runner.run                      isolated verification capture
    .jspace/control.py                    the only ship authorization
                                          (existing check --stage ship)

Merge law: PASS(A) + PASS(B) != PASS(A U B). Worker evidence always seals
authorized_to_ship=false; integration verification belongs to the existing
ship gate run against the integration tree. Full automatic merge
orchestration is an explicit Phase-1 non-goal -- the boundary is this
paragraph, not a pretend merge.

Worktree isolation: git worktrees share object storage, but each has its
own working directory and consumes disk (not zero-disk). Lifecycle rules:
create at dispatch -> execute -> commit working state so a tree identity
exists -> collect evidence -> remove unless --keep-worktrees; removal and
prune errors are reported in the run report, never silenced.

States (execution vocabulary, deliberately NOT ledger keys):
    PENDING, DEFERRED, RUNNING, DONE, FAILED, BLOCKED, INVALID_EVIDENCE

CLI:
    python .hermes/tools/orchestrator.py --root R run --spec spec.json
Exit 0 only when every worker is DONE; every other outcome exits 1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

import check_runner
import evidence
import scope_resolver

STATES = ('PENDING', 'DEFERRED', 'RUNNING', 'DONE', 'FAILED', 'BLOCKED',
          'INVALID_EVIDENCE')
WORKER_TIMEOUT_S = 600
TAIL_CHARS = 2000
# Worker-evidence identity fields required by the Phase-1 evidence model.
WORKER_EVIDENCE_FIELDS = (
    'task_id', 'worker_id', 'base_tree_sha', 'target_tree_sha',
    'declared_scope', 'observed_scope', 'read_set', 'write_set', 'diff',
    'checks', 'exit_status',
)
_SHA_RE = re.compile(r'[0-9a-f]{40}')
_PAIR_LABELS = {'ww': 'write_write', 'wr': 'write_read',
                'rw': 'read_write'}

ExecuteHook = Callable[[dict[str, Any], Path], Any]
"""Hook contract: (worker, worktree) -> exit code, or (exit code, tail)."""


class OrchestratorError(Exception):
    """Structural / governance violation -- fail-closed, never degraded."""


# ---------------------------------------------------------------------------
# Structural path primitives (file/module granularity only; no semantic
# precision is claimed beyond what these rules establish).
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace('\\', '/').rstrip('/')


def _has_wild(entry: str) -> bool:
    return '*' in entry or '?' in entry


def _glob_match(path: str, pattern: str) -> bool:
    """Strict structural glob: '*' and '?' never cross '/', '**' does."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '*':
            if pattern[i:i + 2] == '**':
                out.append('.*')
                i += 2
                continue
            out.append('[^/]*')
        elif ch == '?':
            out.append('[^/]')
        else:
            out.append(re.escape(ch))
        i += 1
    return re.fullmatch(''.join(out), path) is not None


def covered(path: str, entry: str) -> bool:
    """True when `entry` (literal, directory-style or strict glob) provably
    covers the concrete `path`. Directory entries cover children only at a
    path-component boundary (a file can never have children)."""
    path, entry = _norm(path), _norm(entry)
    if path == entry or path.startswith(entry + '/'):
        return True
    if _has_wild(entry):
        return _glob_match(path, entry)
    return False


def _literal_prefix(entry: str) -> str:
    return re.split(r'[*?]', entry, maxsplit=1)[0]


def overlap(left: str, right: str) -> bool:
    """Structural read/write overlap. Conservative: two wildcard entries
    are only declared disjoint when their literal prefixes separate them;
    anything unprovable counts as overlap (fail-closed, never silently
    downgraded to 'no overlap')."""
    lft, rgt = _norm(left), _norm(right)
    if covered(lft, rgt) or covered(rgt, lft):
        return True
    if _has_wild(lft) or _has_wild(rgt):
        lp, rp = _literal_prefix(lft), _literal_prefix(rgt)
        separable = bool(lp and rp and not lp.startswith(rp)
                         and not rp.startswith(lp))
        # literal prefixes separate them => proven disjoint; anything else
        # between two wildcard entries cannot be proven -> conflict.
        return not separable
    return False


# ---------------------------------------------------------------------------
# Pre-execution scope safety (known conditions only; no future prediction).
# ---------------------------------------------------------------------------

def scope_status(worker: dict[str, Any]) -> tuple[bool, str]:
    """Dispatch-time scope safety from the DECLARED scope alone. Unknown or
    structurally invalid declarations block the worker (UNKNOWN != SAFE);
    nothing here predicts what the worker will actually change."""
    declared = worker.get('declared_scope')
    if declared is None:
        return False, 'unknown_declared_scope'
    if not isinstance(declared, list) or not declared:
        return False, 'empty_declared_scope'
    for entry in declared:
        if not isinstance(entry, str) or not entry.strip():
            return False, 'invalid_declared_scope_entry:' + repr(entry)
        parts = _norm(entry).split('/')
        if Path(entry).is_absolute() or entry.startswith('/') or '..' in parts:
            return False, 'invalid_declared_scope_entry:' + repr(entry)
    return True, 'declared_scope_ok'


# ---------------------------------------------------------------------------
# 6.2 ConflictManager -- pure read/write comparison over running workers.
# Conflict state never mutates the dependency graph: a deferred node stays
# dependency-ready, it is simply not currently dispatchable.
# ---------------------------------------------------------------------------

def _intersection(label: str, left: Any, right: Any) -> str | None:
    """None = intersection proven empty. A known-empty side proves it;
    an unknown (None) side with a non-empty other side defers -- UNKNOWN
    is never silently treated as an empty set."""
    if left == [] or right == []:
        return None  # known-empty side => intersection provably empty
    if left is None or right is None:
        return f'{label}_unknown_set'
    for litem in left:
        for ritem in right:
            if overlap(str(litem), str(ritem)):
                return f'{label}_overlap: {litem} ~ {ritem}'
    return None


def _pair_conflict(cand: dict[str, Any],
                   other: dict[str, Any]) -> str | None:
    """Unsafe overlap: cand.write&other.write, cand.write&other.read,
    other.write&cand.read. Read/read never conflicts."""
    ww = _intersection(_PAIR_LABELS['ww'], cand['writes'], other['writes'])
    if ww:
        return ww
    wr = _intersection(_PAIR_LABELS['wr'], cand['writes'], other['reads'])
    if wr:
        return wr
    rw = _intersection(_PAIR_LABELS['rw'], other['writes'], cand['reads'])
    if rw:
        return rw
    return None


class ConflictManager:
    """Compares dependency-ready candidates against currently running
    workers using only KNOWN read/write information."""

    def __init__(self) -> None:
        self._running: dict[str, dict[str, Any]] = {}

    @property
    def running(self) -> tuple[str, ...]:
        return tuple(sorted(self._running))

    def start(self, worker_id: str, worker: dict[str, Any]) -> None:
        self._running[worker_id] = worker

    def finish(self, worker_id: str) -> None:
        self._running.pop(worker_id, None)

    def assess(self, worker: dict[str, Any]) -> tuple[bool, str]:
        for other_id in sorted(self._running):
            reason = _pair_conflict(worker, self._running[other_id])
            if reason:
                return False, f'{other_id}: {reason}'
        return True, 'proven_disjoint'


# ---------------------------------------------------------------------------
# Evidence verification (reuses evidence.py digest; adds tree re-binding).
# ---------------------------------------------------------------------------

def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """IO plumbing only: atomic temp-sibling write (same pattern as
    evidence.write, different destination so the ship artifact at
    .jspace/evidence.json is never touched)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True,
                       ensure_ascii=False) + '\n').encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix='.worker-evidence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def verify_worker_evidence(path: Path,
                           worktree: Path | None = None) -> dict[str, Any]:
    """Fail-closed worker-evidence verification: canonical digest intact,
    Phase-1 identity fields present, worker evidence can never authorize a
    ship (merge law), and -- when the worktree still exists -- the sealed
    target_tree_sha re-binds to the live git tree. Raises on anything
    unproven; returns the payload."""
    raw = Path(path).read_text(encoding='utf-8')
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise OrchestratorError(f'worker evidence unreadable: {exc}') from exc
    if not isinstance(payload, dict):
        raise OrchestratorError('worker evidence must be a JSON object')
    recorded = payload.get('evidence_sha256')
    if not isinstance(recorded, str) or \
            evidence.compute_digest(payload) != recorded:
        raise OrchestratorError(
            'worker evidence digest mismatch (modified after sealing)')
    if payload.get('authorized_to_ship') is not False:
        raise OrchestratorError(
            'worker evidence must seal authorized_to_ship=false '
            '(PASS(A)+PASS(B) != PASS(A U B); ship stays with the gate)')
    missing = [field for field in WORKER_EVIDENCE_FIELDS
               if field not in payload]
    if missing:
        raise OrchestratorError(
            'worker evidence missing fields: ' + ', '.join(missing))
    target = payload.get('target_tree_sha')
    if not isinstance(target, str) or not _SHA_RE.fullmatch(target):
        raise OrchestratorError('missing/invalid tree identity '
                                f'(target_tree_sha={target!r})')
    if target != payload.get('tree_hash'):
        raise OrchestratorError('target_tree_sha/tree_hash mismatch')
    if worktree is not None:
        live = _git(Path(worktree), 'rev-parse', 'HEAD^{tree}')
        if live != target:
            raise OrchestratorError(
                'tree identity mismatch: evidence ' + target
                + ' != live ' + live)
    return payload


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


def default_execute(worker: dict[str, Any], worktree: Path
                    ) -> tuple[int, str]:
    """Production execution: run the worker's argv in its worktree."""
    try:
        proc = subprocess.run(worker['cmd'], cwd=worktree,
                              capture_output=True, text=True,
                              timeout=WORKER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, f'{type(exc).__name__}: {exc}'
    combined = (proc.stdout or '') + (proc.stderr or '')
    return proc.returncode, combined[-TAIL_CHARS:]


# ---------------------------------------------------------------------------
# 6.4 GovernedScheduler -- readiness -> conflict gate -> dispatch ->
# evidence -> done(). done(node) is called ONLY after the full lifecycle.
# ---------------------------------------------------------------------------

def validate_workers(workers: Any) -> list[dict[str, Any]]:
    """Graph/spec validation before anything is created or dispatched."""
    if not isinstance(workers, list) or not workers:
        raise OrchestratorError('spec.workers must be a non-empty list')
    ids: set[str] = set()
    for raw in workers:
        if not isinstance(raw, dict):
            raise OrchestratorError('each worker must be a JSON object')
        wid = raw.get('id')
        if not isinstance(wid, str) or not wid:
            raise OrchestratorError('worker id must be a non-empty string')
        if wid in ids:
            raise OrchestratorError(f'duplicate worker id: {wid!r}')
        ids.add(wid)
        deps = raw.get('deps', [])
        if not isinstance(deps, list) or \
                not all(isinstance(dep, str) for dep in deps):
            raise OrchestratorError(
                f'{wid}.deps must be a list of worker ids')
        for key in ('reads', 'writes'):
            value = raw.get(key)
            if value is not None and (
                    not isinstance(value, list)
                    or not all(isinstance(item, str) for item in value)):
                raise OrchestratorError(
                    f'{wid}.{key} must be a list of paths, or absent '
                    'to mean UNKNOWN (never silently an empty set)')
        cmd = raw.get('cmd')
        if not isinstance(cmd, list) or not cmd or \
                not all(isinstance(part, str) for part in cmd):
            raise OrchestratorError(
                f'{wid}.cmd must be a non-empty argv list')
    for raw in workers:
        for dep in raw.get('deps', []) or []:
            if dep not in ids:
                raise OrchestratorError(
                    f'{raw["id"]}: unknown dependency {dep!r}')
    return list(workers)


class GovernedScheduler:
    """TopologicalSorter wrapped in the dispatch/evidence invariants."""

    def __init__(self, repo: Path | str, workers: Any, *,
                 task_id: str = 'task', keep_worktrees: bool = False,
                 execute: ExecuteHook | None = None) -> None:
        # workers is Any at the boundary: validate_workers() is the gate.
        self.workers: list[dict[str, Any]] = validate_workers(workers)
        self.repo = Path(repo).resolve()
        self.by_id = {worker['id']: worker for worker in self.workers}
        self.task_id = task_id
        self.execute: ExecuteHook = execute or default_execute
        self.dispatcher = WorktreeDispatcher(self.repo, keep=keep_worktrees)
        self.conflicts = ConflictManager()
        self.states: dict[str, dict[str, Any]] = {
            wid: {'state': 'PENDING', 'reason': None} for wid in self.by_id}
        self.deferral_events: list[dict[str, str]] = []
        self.evidence_paths: dict[str, str] = {}
        self.completed: list[str] = []
        self._deferred_seen: set[tuple[str, str]] = set()
        self.evidence_dir = (self.repo / '.jspace' / 'cache' / 'orchestrator'
                             / _safe_id(task_id))

    # -- small state helpers ------------------------------------------------

    def _set(self, worker_id: str, state: str, reason: str | None) -> None:
        assert state in STATES, state
        self.states[worker_id] = {'state': state, 'reason': reason}

    def state_of(self, worker_id: str) -> str:
        return str(self.states[worker_id]['state'])

    def _decide(self, worker: dict[str, Any]) -> tuple[str, str]:
        ok, why = scope_status(worker)
        if not ok:
            return 'block', why
        safe, why = self.conflicts.assess(worker)
        if not safe:
            return 'defer', why
        return 'dispatch', 'proven_safe'

    # -- post-execution lifecycle (runs inside pool threads) ----------------

    def _collect(self, worker: dict[str, Any], path: Path, rc: int,
                 tail: str) -> dict[str, Any]:
        wid = worker['id']
        if rc != 0:
            return {'state': 'FAILED', 'reason': f'worker_exit_{rc}',
                    'evidence': None}
        try:
            dirty = [line for line in
                     _git(path, 'status', '--porcelain').splitlines()
                     if line.strip()]
            if dirty:
                # A tree identity only exists for committed state: commit
                # the worker's result (or fail closed if that is impossible).
                _commit_all(path, f'orchestrator: worker {wid}')
                dirty = [line for line in
                         _git(path, 'status', '--porcelain').splitlines()
                         if line.strip()]
            if dirty:
                return {'state': 'INVALID_EVIDENCE',
                        'reason': 'missing_tree_identity', 'evidence': None}
            target_commit = _git(path, 'rev-parse', 'HEAD')
            target_tree = _git(path, 'rev-parse', 'HEAD^{tree}')
            observed = scope_resolver.changed_files(
                path, base=self.dispatcher.base_commit)
            declared = worker.get('declared_scope') or []
            violations = [item for item in observed
                          if not any(covered(item, entry)
                                     for entry in declared)]
            if violations:
                return {'state': 'INVALID_EVIDENCE',
                        'reason': 'scope_violation:'
                        + ','.join(violations[:5]),
                        'evidence': None}
            resolved = scope_resolver.resolve(
                path, base=self.dispatcher.base_commit)
            checks = check_runner.run(path, resolved)
            failed = [entry['name'] for entry in checks
                      if entry['status'] == 'failed']
            if failed:
                return {'state': 'FAILED',
                        'reason': 'verification_failed:' + ','.join(failed),
                        'evidence': None}
            if checks and all(entry['status'] == 'skipped'
                              for entry in checks):
                return {'state': 'INVALID_EVIDENCE',
                        'reason': 'no_verification_ran', 'evidence': None}
            diff = _git(path, 'diff', self.dispatcher.base_commit,
                        target_commit, strip=False)
            payload: dict[str, Any] = {
                'schema': evidence.SCHEMA,
                'stage': 'worker',
                'scope': resolved['scope'],
                'commit': target_commit,
                'tree_hash': target_tree,
                'observed_at': evidence.now_iso(),
                'checks': checks,
                # Merge law: worker evidence never authorizes a ship.
                'authorized_to_ship': False,
                'task_id': self.task_id,
                'worker_id': wid,
                'base_commit': self.dispatcher.base_commit,
                'base_tree_sha': self.dispatcher.base_tree,
                'target_tree_sha': target_tree,
                'declared_scope': list(declared),
                'observed_scope': observed,
                'read_set': worker.get('reads'),
                'write_set': worker.get('writes'),
                'diff': diff,
                'exit_status': rc,
            }
            if tail:
                payload['output_tail'] = tail[-TAIL_CHARS:]
            sealed = evidence.seal(payload)
            evi_path = self.evidence_dir / (_safe_id(wid) + '.json')
            _atomic_json(evi_path, sealed)
            # Governed completion requires the sealed identity to re-bind
            # to the live tree -- digest alone is not proof.
            verify_worker_evidence(evi_path, worktree=path)
        except (OrchestratorError, scope_resolver.ScopeError,
                evidence.EvidenceError, OSError) as exc:
            return {'state': 'INVALID_EVIDENCE',
                    'reason': f'{type(exc).__name__}: {exc}',
                    'evidence': None}
        return {'state': 'DONE', 'reason': None, 'evidence': str(evi_path)}

    def _run_one(self, worker: dict[str, Any], path: Path
                 ) -> dict[str, Any]:
        try:
            result = self.execute(worker, path)
        except Exception as exc:  # worker hook must not kill the scheduler
            return {'state': 'FAILED',
                    'reason': f'execution_error:{type(exc).__name__}: {exc}',
                    'evidence': None}
        if isinstance(result, tuple):
            rc, tail = int(result[0]), str(result[1])
        else:
            rc, tail = int(result), ''
        return self._collect(worker, path, rc, tail)

    # -- the scheduling loop ------------------------------------------------

    def run(self) -> dict[str, Any]:
        sorter: TopologicalSorter[str] = TopologicalSorter()
        for worker in self.workers:
            sorter.add(worker['id'], *(worker.get('deps') or []))
        report: dict[str, Any] = {
            'task_id': self.task_id, 'status': 'failed', 'reason': None,
            'base_commit': self.dispatcher.base_commit,
            'base_tree': self.dispatcher.base_tree,
            'states': self.states, 'completed': self.completed,
            'deferral_events': self.deferral_events,
            'evidence': self.evidence_paths, 'cleanup_errors': [],
            'worktrees': {},
        }
        try:
            sorter.prepare()
        except CycleError as exc:
            # §9: detect -> fail closed -> report. Never invent order.
            cycle = (list(exc.args[1]) if len(exc.args) > 1
                     and isinstance(exc.args[1], list) else [])
            for wid in dict.fromkeys(cycle):
                if wid in self.states:
                    self._set(wid, 'FAILED', 'cycle')
            report['reason'] = 'cycle'
            report['cycle'] = cycle
            self.dispatcher.cleanup()
            report['cleanup_errors'] = list(self.dispatcher.cleanup_errors)
            return report

        ready: list[str] = list(sorter.get_ready())
        futures: dict[Future[dict[str, Any]], str] = {}
        abort = False
        fail_reason: str | None = None
        with ThreadPoolExecutor(max_workers=max(1, len(self.workers))) as pool:
            while True:
                dispatched = 0
                if not abort:
                    for wid in list(ready):  # deferred nodes re-enter here
                        state = self.state_of(wid)
                        if state not in ('PENDING', 'DEFERRED'):
                            continue
                        worker = self.by_id[wid]
                        decision, reason = self._decide(worker)
                        if decision == 'block':
                            self._set(wid, 'BLOCKED', reason)
                            continue
                        if decision == 'defer':
                            self._set(wid, 'DEFERRED', reason)
                            key = (wid, reason)
                            if key not in self._deferred_seen:
                                self._deferred_seen.add(key)
                                self.deferral_events.append(
                                    {'worker': wid, 'reason': reason})
                            continue
                        try:
                            path = self.dispatcher.create(wid)
                        except OrchestratorError as exc:
                            # Dispatch itself failed: fail closed, never
                            # pretend the worker may still run elsewhere.
                            self._set(wid, 'FAILED', f'worktree: {exc}')
                            abort = True
                            fail_reason = fail_reason or f'{wid}:worktree'
                            continue
                        self._set(wid, 'RUNNING', None)
                        self.conflicts.start(wid, worker)
                        futures[pool.submit(self._run_one, worker, path)] = wid
                        dispatched += 1
                if dispatched:
                    continue
                if futures:
                    finished, _ = wait(futures,
                                       return_when=FIRST_COMPLETED)
                    for fut in finished:
                        wid = futures.pop(fut)
                        try:
                            outcome = fut.result()
                        except Exception as exc:  # never lose a failure
                            outcome = {'state': 'INVALID_EVIDENCE',
                                       'reason': f'internal: {exc}',
                                       'evidence': None}
                        self.conflicts.finish(wid)
                        self._set(wid, str(outcome['state']),
                                  outcome.get('reason'))
                        if outcome.get('evidence'):
                            self.evidence_paths[wid] = str(
                                outcome['evidence'])
                        if outcome['state'] == 'DONE':
                            # done() = completed dependency execution, and
                            # only after evidence was sealed AND re-verified.
                            sorter.done(wid)
                            self.completed.append(wid)
                            ready.extend(sorter.get_ready())
                        else:
                            abort = True
                            fail_reason = fail_reason or (
                                f'{wid}:{outcome["state"]}')
                    continue
                break  # nothing dispatched, nothing running: schedule ends

        # Sweep nodes that never became ready or dispatchable: fail-closed
        # BLOCKED states, never silent PENDING leftovers.
        failed_seen = any(entry['state'] in
                          ('FAILED', 'INVALID_EVIDENCE', 'BLOCKED')
                          for entry in self.states.values())
        for wid, entry in self.states.items():
            if entry['state'] not in ('PENDING', 'DEFERRED'):
                continue
            deps = self.by_id[wid].get('deps') or []
            unfinished = [dep for dep in deps
                          if self.states[dep]['state'] != 'DONE']
            if unfinished:
                entry.update(state='BLOCKED',
                             reason='dependency_not_finished:'
                             + ','.join(unfinished))
            elif failed_seen:
                entry.update(state='BLOCKED', reason='upstream_failure')
            else:
                entry.update(state='BLOCKED', reason='not_dispatchable')

        self.dispatcher.cleanup()
        report['worktrees'] = {wid: str(path)
                               for wid, path in self.dispatcher.paths.items()}
        report['cleanup_errors'] = list(self.dispatcher.cleanup_errors)
        all_done = all(entry['state'] == 'DONE'
                       for entry in self.states.values())
        report['status'] = 'ok' if all_done else 'failed'
        report['reason'] = None if all_done else (
            fail_reason or 'incomplete')
        return report


# ---------------------------------------------------------------------------
# CLI (direct module usability; control.py delegates to this).
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Asha Orchestrator (Phase 1 governed scheduling)')
    parser.add_argument('--root', default='.',
                        help='repository root (default: cwd)')
    sub = parser.add_subparsers(dest='command', required=True)
    run_p = sub.add_parser('run', help='run one worker graph to completion')
    run_p.add_argument('--spec', required=True,
                       help="JSON {task_id?, workers:[{id,deps,"
                            "declared_scope,reads,writes,cmd}]}")
    run_p.add_argument('--keep-worktrees', action='store_true',
                       help='debug: skip worktree removal (disk cost stays '
                            'until removed manually; reported)')
    args = parser.parse_args(argv)
    try:
        spec_path = Path(args.spec)
        spec = json.loads(spec_path.read_text(encoding='utf-8'))
        if not isinstance(spec, dict):
            raise OrchestratorError('spec must be a JSON object')
        task_id = spec.get('task_id', 'task')
        if not isinstance(task_id, str) or not task_id:
            raise OrchestratorError('spec.task_id must be a non-empty string')
        scheduler = GovernedScheduler(
            Path(args.root), spec.get('workers'), task_id=task_id,
            keep_worktrees=args.keep_worktrees)
        report = scheduler.run()
    except OrchestratorError as exc:
        print(f'ORCHESTRATOR ERROR: {exc}', file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f'ORCHESTRATOR ERROR: invalid spec json: {exc}',
              file=sys.stderr)
        return 1
    except OSError as exc:
        print(f'ORCHESTRATOR ERROR: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed, never a traceback at the agent
        print(f'ORCHESTRATOR ERROR: {type(exc).__name__}: {exc}',
              file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report.get('status') == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
