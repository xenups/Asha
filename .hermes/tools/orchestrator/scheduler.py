#!/usr/bin/env python3
"""Governed scheduling loop: dependency readiness -> dispatch-safety
gates -> worktree dispatch -> evidence -> generational reconciliation
(Phase 2). The CLI entrypoint lives here and is re-exported by the
`orchestrator` package facade."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

import check_runner
import dep_index
import evidence
import graph_state
import scope_resolver

from .conflict import ConflictManager, covered, scope_status
from .types import (
    STATES,
    TAIL_CHARS,
    WORKER_EVIDENCE_FIELDS,
    WORKER_TIMEOUT_S,
    ExecuteHook,
    OrchestratorError,
)
from .worktree import WorktreeDispatcher, _commit_all, _git, _safe_id

_SHA_RE = re.compile(r'[0-9a-f]{40}')


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
        # Phase 2: in-memory dependency facts + versioned graph state.
        self.dep_index = dep_index.DependencyIndex()
        self.graph = graph_state.GraphState.empty()
        self.reconcile_log: list[dict[str, Any]] = []
        self.stale_intents_dropped = 0
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

    def _graph_report(self) -> dict[str, Any]:
        """Phase-2 graph summary for the run report (§2.2 / §2.4)."""
        return {
            'generation': self.graph.generation,
            'reconcile_passes': len(self.reconcile_log),
            'stale_intents_dropped': self.stale_intents_dropped,
            'failures': [entry['reason'] for entry in self.reconcile_log
                         if not entry['ok']],
        }

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
        return {'state': 'DONE', 'reason': None, 'evidence': str(evi_path),
                # Phase-2 reconciliation input: git-derived paths bound to
                # the same target_tree identity the sealed evidence carries.
                'observed': list(observed), 'target_tree': target_tree}

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

    # -- Phase-2 online reconciliation --------------------------------------

    def _read_blob(self, tree: str, path: str) -> str | None:
        """Read one path from a worker result tree (read-only git).

        Undecodable bytes decode with replacement so a broken file fails
        the AST parser as UNCERTAIN (fail-closed) instead of vanishing;
        a missing path returns None = deletion side of the delta.
        """
        proc = subprocess.run(['git', 'show', f'{tree}:{path}'],
                              cwd=self.repo, capture_output=True)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode('utf-8', errors='replace')

    def _reconcile(self, updates: dict[str, str | None]) -> None:
        """One reconciliation pass per coalesced batch (§2.3 / §2.4).

        Failure keeps the previous GraphState by reference (fail-closed);
        every pass is appended to self.reconcile_log for the run report.
        """
        if not updates:
            return
        outcome = graph_state.reconcile(self.graph, self.dep_index, updates)
        self.graph = outcome.state  # atomic swap (same ref on failure)
        self.reconcile_log.append({
            'ok': outcome.ok,
            'reason': outcome.reason,
            'generation': outcome.generation,
            'files': list(outcome.files),
            'affected': list(outcome.affected),
            'added': outcome.added,
            'removed': outcome.removed,
        })

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
            report['graph'] = self._graph_report()
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
                        decided_against = self.graph.generation
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
                        # §2.5: a dispatch intent is valid only for the
                        # graph generation its decision was made against;
                        # on mismatch discard it and re-evaluate the node.
                        intent = graph_state.DispatchIntent(
                            wid, decided_against)
                        if not intent.matches(self.graph):
                            self.stale_intents_dropped += 1
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
                    # §2.4: coalesce every completion drained in this
                    # batch into ONE reconciliation pass (+1 generation).
                    pending: dict[str, str | None] = {}
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
                            # Phase-2 input: read each observed path from
                            # the tree identity this outcome just verified.
                            for item in outcome.get('observed') or ():
                                pending[item] = self._read_blob(
                                    str(outcome['target_tree']), item)
                        else:
                            abort = True
                            fail_reason = fail_reason or (
                                f'{wid}:{outcome["state"]}')
                    self._reconcile(pending)
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
        report['graph'] = self._graph_report()
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
