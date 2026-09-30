"""Minimal DAG sequencing policy (H.2.2-C).

DECISION B boundary: this module owns DAG POLICY ONLY.

It sequences multi-worker runs: generation-tracked readiness, cycle
fail-closed, conflict-gated dispatch, DispatchIntent validation,
completion/reconciliation sequencing, stale-intent invalidation,
terminal-state labeling, and final run-report aggregation.

It does NOT own (delegates to the established primitives):
  worker execution        -> worker_execution.default_execute
  evidence collection     -> worker_execution.collect_worker_evidence
  worktree lifecycle      -> WorktreeDispatcher
  graph projection/sorter -> worker_graph.derive/build_sorter
  graph state/reconcile   -> graph_state.reconcile / GraphState
  conflict state          -> ConflictManager
  integration             -> TreeIntegrator (via caller)
  routing/classification  -> classify_task + route (invoked as hook)

The thread-pool transport of the legacy scheduler is retained as a
bounded parallel executor (product contract: multi-worker runtime
executes concurrently); the legacy 0.5s wave-union completion window is
kept for identical reconcile-pass coalescing behavior.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress as _suppress
from graphlib import CycleError, TopologicalSorter
from typing import Any

from asha import dep_index, graph_state, worker_graph
from asha.classifier import classify_task, governance_profile
from asha.common import paths as common_paths
from asha.conflict import ConflictManager, covered, scope_status
from asha.contracts.validation import validate_workers
from asha.governance import worker_execution
from asha.router import RuntimeMode, route
from asha.types import STATES, OrchestratorError
from asha.worktree import WorktreeDispatcher, _git, _safe_id

_WAVE_OVERLAP_S = 0.5  # legacy completion-coalescing window (kept)


class DAGCoordinator:
    """DAG sequencing policy for a multi-worker governed run.

    Semantics ported VERBATIM from GovernedScheduler.run() (H.2.2-B
    responsibility matrix) minus execution/evidence/worktree/graph
    ownership. All state transitions, reason strings and report fields
    preserved for byte-identical external behavior where the legacy
    path is deterministic.
    """

    def __init__(
        self,
        repo: Any,
        workers: list[dict[str, Any]],
        *,
        task_id: str,
        keep_worktrees: bool = False,
        preserve_on_failure: bool = False,
        fast_path_enabled: bool = False,
        classification_context: tuple[Any, ...] = (),
    ) -> None:
        self.repo = repo
        self.workers = list(workers)
        self.task_id = task_id
        self.keep_worktrees = keep_worktrees
        self.preserve_on_failure = preserve_on_failure
        self.fast_path_enabled = fast_path_enabled
        self.classification_context = tuple(classification_context)
        self.by_id: dict[str, dict[str, Any]] = {
            str(worker["id"]): worker for worker in self.workers}
        self.dispatcher = WorktreeDispatcher(self.repo, keep=keep_worktrees)
        self.conflicts = ConflictManager()
        self.dep_index = dep_index.DependencyIndex()
        self.graph = graph_state.GraphState.empty()
        self.completed: list[str] = []
        self.worker_graph: dict[str, Any] = self._derive_worker_graph({}, 0)
        self.worker_cycle_members: frozenset[str] = frozenset()
        self.states: dict[str, dict[str, Any]] = {
            wid: {"state": "PENDING", "reason": None}
            for wid in self.by_id}
        self.deferral_events: list[dict[str, str]] = []
        self.evidence_paths: dict[str, str] = {}
        self.routes: dict[str, dict[str, Any]] = {}
        self.reconcile_log: list[dict[str, Any]] = []
        self.stale_intents_dropped = 0
        self.stale_intents: list[dict[str, Any]] = []
        self.orphaned_reads: set[str] = set()
        self._deferred_seen: set[tuple[str, str]] = set()
        self.evidence_dir = (common_paths.get_orchestrator_dir(self.repo)
                             / _safe_id(task_id))
        self._fast_lock = threading.Lock()
        self.execute = worker_execution.default_execute

    # -- small state helpers ------------------------------------------------

    def _set(self, worker_id: str, state: str, reason: str | None) -> None:
        assert state in STATES, state
        self.states[worker_id] = {"state": state, "reason": reason}

    def state_of(self, worker_id: str) -> str:
        return str(self.states[worker_id]["state"])

    def _graph_report(self) -> dict[str, Any]:
        return {
            "generation": self.graph.generation,
            "worker_generation": self.worker_graph["generation"],
            "derived": list(self.worker_graph["derived"]),
            "uncertain_owners": sorted(self.worker_graph["uncertain"]),
            "reconcile_passes": len(self.reconcile_log),
            "stale_intents_dropped": self.stale_intents_dropped,
            "failures": [entry["reason"] for entry in self.reconcile_log
                         if not entry["ok"]],
        }

    # -- classification / routing (governance hook, not owned) --------------

    def _task_payload(self, worker: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(worker.get("id")),
            "reads": worker.get("reads"),
            "writes": worker.get("writes"),
            "declared_scope": worker.get("declared_scope"),
            "deps": [str(dep) for dep in (worker.get("deps") or [])],
        }

    def _route_for(self, worker: dict[str, Any]) -> dict[str, Any]:
        """Classification -> profile -> fail-closed router (verbatim port).
        A classifier error never crashes dispatch, never enables Fast Path."""
        from time import perf_counter_ns

        wid = str(worker["id"])
        task = self._task_payload(worker)
        context = tuple(
            self._task_payload(other) for other in self.workers
            if str(other.get("id")) != wid) + self.classification_context
        started = perf_counter_ns()
        profile: object
        error: str | None = None
        try:
            profile = governance_profile(classify_task(task, context))
        except Exception as exc:  # fail-closed, not fail-crash
            profile = None
            error = f"{type(exc).__name__}: {exc}"
        classified_ns = perf_counter_ns() - started
        routed_started = perf_counter_ns()
        decision = route(profile, fast_path_enabled=True)
        routed_ns = perf_counter_ns() - routed_started
        record: dict[str, Any] = {
            "mode": decision.mode.value,
            "reason": decision.reason_code,
            "classification": decision.classification,
            "t_classify_ns": classified_ns,
            "t_route_ns": routed_ns,
        }
        if error is not None:
            record["classification_error"] = error
        self.routes[wid] = record
        return record

    # -- dispatch gates ------------------------------------------------------

    def _decide(self, worker: dict[str, Any]) -> tuple[str, str]:
        ok, why = scope_status(worker)
        if not ok:
            return "block", why
        wid = str(worker["id"])
        reads = worker.get("reads") or []
        orphans = sorted(
            node for node in self.orphaned_reads
            if any(covered(node, str(entry)) for entry in reads))
        if orphans:
            return "block", "orphaned_dependency:" + ",".join(orphans)
        if wid in self.worker_graph["uncertain"]:
            return "block", ("owner_ambiguous:"
                             + ",".join(self.worker_graph["ambiguous_paths"]))
        if wid in self.worker_cycle_members:
            return "block", "worker_cycle_member"
        safe, why = self.conflicts.assess(worker)
        if not safe:
            return "defer", why
        return "dispatch", "proven_safe"

    # -- execution primitives (delegated) -----------------------------------

    @staticmethod
    def _merge_verdict_fields(target: dict[str, Any],
                              outcome: dict[str, Any]) -> None:
        """Copy Delta Check verdict fields from a worker outcome into the
        DAG states entry (isolated helper: keeps flow analysis of the
        scheduling loop free of extra assignments)."""
        for extra in ("verdict", "baseline_failures",
                      "current_failures", "delta_failures"):
            if outcome.get(extra) is not None:
                target[extra] = outcome[extra]

    def _run_one(self, worker: dict[str, Any], path: Any, *,
                 base: str | None = None,
                 base_tree: str | None = None) -> dict[str, Any]:
        baseline_journal: dict | None = None
        base_c = base or self.dispatcher.base_commit
        base_t = base_tree or self.dispatcher.base_tree
        if base_c and base_t:
            try:
                baseline_journal = worker_execution.baseline_checks(
                    worker, path, base_c, base_t, self.repo, self.task_id)
            except Exception:
                baseline_journal = None
        # Strip verification caches the baseline pass left in the worktree
        # (__pycache__/.pytest_cache/.ruff_cache/.mypy_cache). Without this
        # the current pass can read a stale timestamped .pyc for a source
        # file the worker rewrote within the same wall-clock second, so the
        # current test run silently executes pre-change bytecode and the
        # regression is missed (K.5.3: stale-pyc mtime race). Mirrors
        # run_worker_in_worktree's post-baseline strip exactly.
        worker_execution._strip_check_caches(path)
        try:
            result = self.execute(worker, path)
        except subprocess.TimeoutExpired:
            return {"state": "FAILED", "reason": "timeout_exceeded",
                    "evidence": None}
        except Exception as exc:
            return {"state": "FAILED",
                    "reason": f"execution_error:{type(exc).__name__}: {exc}",
                    "evidence": None}
        if isinstance(result, tuple):
            rc, tail = int(result[0]), str(result[1])
        else:
            rc, tail = int(result), ""
        if base is None or base_tree is None:
            base = self.dispatcher.base_commit
            base_tree = self.dispatcher.base_tree
        return worker_execution.collect_worker_evidence(
            worker, path, rc, tail, self.task_id, self.evidence_dir,
            base=base, base_tree=base_tree,
            uncertain=set(self.worker_graph["uncertain"]),
            by_id=self.by_id, workers=self.workers,
            baseline_checks=(
                (baseline_journal or {}).get("checks")
                if baseline_journal else None))

    def _run_one_fast(self, worker: dict[str, Any]) -> dict[str, Any]:
        with self._fast_lock:
            base = _git(self.repo, "rev-parse", "HEAD").strip()
            base_tree = _git(self.repo, "rev-parse", "HEAD^{tree}").strip()
            return self._run_one(worker, self.repo, base=base,
                                 base_tree=base_tree)

    # -- graph projection ----------------------------------------------------

    def _derive_worker_graph(
            self, file_edges: Mapping[str, Iterable[str]],
            generation: int) -> dict[str, Any]:
        candidate = worker_graph.derive(
            self.workers, file_edges,
            completed=frozenset(self.completed))
        return {
            "edges": {wid: set(preds)
                      for wid, preds in candidate.edges.items()},
            "declared": {wid: list(preds)
                         for wid, preds in candidate.declared.items()},
            "derived": candidate.derived,
            "uncertain": candidate.uncertain,
            "ambiguous_paths": candidate.ambiguous_paths,
            "generation": generation,
        }

    # -- reconciliation sequencing ------------------------------------------

    def _reconcile_batch(
            self, items: list[tuple[str, str | None, str, str]]) -> bool:
        updates: dict[str, str | None] = {}
        providers: dict[str, tuple[str, str]] = {}
        ambiguous: set[str] = set()
        for path, content, wid, tree in items:
            if path in providers:
                ambiguous.add(path)
                continue
            providers[path] = (wid, tree)
            updates[path] = content
        if ambiguous:
            self.reconcile_log.append({
                "ok": False,
                "reason": "virtual_ambiguity:"
                          + ",".join(sorted(ambiguous)),
                "generation": self.graph.generation,
                "files": sorted(updates),
                "affected": (), "added": 0, "removed": 0,
                "derived": [], "uncertain": [],
                "virtual": worker_graph.virtual_fingerprint(
                    providers, updates),
            })
            return False
        return self._reconcile(updates, providers=providers)

    def _reconcile(self, updates: dict[str, str | None], *,
                   providers: dict[str, tuple[str, str]] | None = None
                   ) -> bool:
        if not updates:
            return False
        fingerprint = worker_graph.virtual_fingerprint(
            providers or {}, updates)
        outcome = graph_state.reconcile(self.graph, self.dep_index, updates)
        if not outcome.ok:
            self.reconcile_log.append({
                "ok": False, "reason": outcome.reason,
                "generation": outcome.generation,
                "files": list(outcome.files),
                "affected": list(outcome.affected),
                "added": outcome.added, "removed": outcome.removed,
                "derived": [], "uncertain": [], "virtual": fingerprint,
            })
            return False
        candidate = worker_graph.derive(
            self.workers, outcome.state.edges,
            completed=frozenset(self.completed))
        combined = {wid: set(preds)
                    for wid, preds in candidate.edges.items()}
        try:
            worker_graph.build_sorter(combined)
        except worker_graph.WorkerCycleError as exc:
            self.worker_cycle_members = frozenset(exc.members)
            self.reconcile_log.append({
                "ok": False,
                "reason": "worker_cycle:" + ",".join(exc.members),
                "generation": self.graph.generation,
                "files": list(outcome.files),
                "affected": list(outcome.affected),
                "added": outcome.added, "removed": outcome.removed,
                "derived": [], "uncertain": [], "virtual": fingerprint,
            })
            return False
        vanished = set(self.graph.nodes) - set(outcome.state.nodes)
        self.graph = outcome.state
        self.orphaned_reads = ((self.orphaned_reads | vanished)
                               - set(outcome.state.nodes))
        self.worker_graph = {
            "edges": {wid: set(preds)
                      for wid, preds in candidate.edges.items()},
            "declared": dict(candidate.declared),
            "derived": candidate.derived,
            "uncertain": candidate.uncertain,
            "ambiguous_paths": candidate.ambiguous_paths,
            "generation": outcome.state.generation,
        }
        self.worker_cycle_members = frozenset()
        self.reconcile_log.append({
            "ok": True, "reason": None,
            "generation": outcome.generation,
            "files": list(outcome.files),
            "affected": list(outcome.affected),
            "added": outcome.added, "removed": outcome.removed,
            "derived": list(candidate.derived),
            "uncertain": sorted(candidate.uncertain),
            "virtual": fingerprint,
        })
        return True

    # -- post-execution evidence side-records -------------------------------

    # -- the scheduling loop (legacy run() port) ----------------------------

    def run(self) -> dict[str, Any]:
        sorter: TopologicalSorter[str] = TopologicalSorter()
        for worker in self.workers:
            sorter.add(worker["id"],
                       *(self.worker_graph["edges"].get(worker["id"])
                         or ()))
        report: dict[str, Any] = {
            "task_id": self.task_id, "status": "failed", "reason": None,
            "base_commit": self.dispatcher.base_commit,
            "base_tree": self.dispatcher.base_tree,
            "states": self.states, "completed": self.completed,
            "deferral_events": self.deferral_events,
            "evidence": self.evidence_paths, "cleanup_errors": [],
            "worktrees": {},
        }
        if self.fast_path_enabled:
            report["routing"] = self.routes
        try:
            sorter.prepare()
        except CycleError as exc:
            cycle = (list(exc.args[1]) if len(exc.args) > 1
                     and isinstance(exc.args[1], list) else [])
            for wid in dict.fromkeys(cycle):
                if wid in self.states:
                    self._set(wid, "FAILED", "cycle")
            report["reason"] = "cycle"
            report["cycle"] = cycle
            report["graph"] = self._graph_report()
            self.dispatcher.cleanup()
            report["cleanup_errors"] = list(self.dispatcher.cleanup_errors)
            return report

        ready: list[str] = list(sorter.get_ready())
        futures: dict[Future[dict[str, Any]], str] = {}
        abort = False
        fail_reason: str | None = None
        with ThreadPoolExecutor(max_workers=max(1, len(self.workers))) as pool:
            while True:
                dispatched = 0
                stale_before = self.stale_intents_dropped
                if not abort:
                    for wid in list(ready):  # deferred nodes re-enter here
                        state = self.state_of(wid)
                        if state not in ("PENDING", "DEFERRED"):
                            continue
                        worker = self.by_id[wid]
                        decided_against = self.graph.generation
                        decision, reason = self._decide(worker)
                        if decision == "block":
                            self._set(wid, "BLOCKED", reason)
                            continue
                        if decision == "defer":
                            self._set(wid, "DEFERRED", reason)
                            key = (wid, reason)
                            if key not in self._deferred_seen:
                                self._deferred_seen.add(key)
                                self.deferral_events.append(
                                    {"worker": wid, "reason": reason})
                            continue
                        intent = graph_state.DispatchIntent(
                            wid, decided_against)
                        if not intent.matches(self.graph):
                            self.stale_intents_dropped += 1
                            self.stale_intents.append({
                                "worker": wid,
                                "reason": graph_state
                                          .STALE_GRAPH_GENERATION,
                                "intent_generation": intent.generation,
                                "current_generation":
                                    self.graph.generation})
                            continue
                        fast_routed = False
                        if self.fast_path_enabled:
                            fast_routed = (
                                self._route_for(worker)["mode"]
                                == RuntimeMode.FAST_PATH.value)
                        if fast_routed:
                            self._set(wid, "RUNNING", None)
                            self.conflicts.start(wid, worker)
                            futures[pool.submit(
                                self._run_one_fast, worker)] = wid
                            dispatched += 1
                            continue
                        try:
                            path = self.dispatcher.create(wid)
                        except OrchestratorError as exc:
                            self._set(wid, "FAILED", f"worktree: {exc}")
                            abort = True
                            fail_reason = fail_reason or f"{wid}:worktree"
                            continue
                        self._set(wid, "RUNNING", None)
                        self.conflicts.start(wid, worker)
                        futures[pool.submit(
                            self._run_one, worker, path)] = wid
                        dispatched += 1
                if dispatched:
                    continue
                if futures:
                    finished, _ = wait(futures,
                                       return_when=FIRST_COMPLETED)
                    batch: list[tuple[str, str | None, str, str]] = []
                    wave = set(finished)
                    handled: set[Future[Any]] = set()
                    while wave:
                        for fut in wave:
                            handled.add(fut)
                            wid = futures.pop(fut)
                            try:
                                outcome = fut.result()
                            except Exception as exc:
                                outcome = {
                                    "state": "INVALID_EVIDENCE",
                                    "reason": f"internal: {exc}",
                                    "evidence": None}
                            self.conflicts.finish(wid)
                            self._set(wid, str(outcome["state"]),
                                      outcome.get("reason"))
                            type(self)._merge_verdict_fields(
                                self.states[wid], outcome)
                            if outcome.get("evidence"):
                                self.evidence_paths[wid] = str(
                                    outcome["evidence"])
                            if outcome["state"] == "DONE":
                                sorter.done(wid)
                                self.completed.append(wid)
                                ready.extend(sorter.get_ready())
                                tree = str(outcome["target_tree"])
                                for item in outcome.get("observed") or ():
                                    batch.append(
                                        (item,
                                         self._read_blob(tree, item),
                                         wid, tree))
                            else:
                                abort = True
                                fail_reason = fail_reason or (
                                    f"{wid}:{outcome['state']}")
                        wave = {f for f in list(futures)
                                if f.done()} - handled
                        if not wave and futures:
                            extra, _ = wait(futures, timeout=_WAVE_OVERLAP_S)
                            wave = set(extra) - handled
                    if self._reconcile_batch(batch):
                        remaining = {
                            str(worker["id"]): set(
                                self.worker_graph["edges"].get(
                                    worker["id"]) or set())
                            for worker in self.workers
                            if worker["id"] not in self.completed}
                        sorter = worker_graph.build_sorter(remaining)
                        ready = list(dict.fromkeys(sorter.get_ready()))
                    continue
                if self.stale_intents_dropped != stale_before:
                    continue
                break  # nothing dispatched, nothing running: schedule ends

        failed_seen = any(entry["state"] in
                          ("FAILED", "INVALID_EVIDENCE", "BLOCKED")
                          for entry in self.states.values())
        for wid, entry in self.states.items():
            if entry["state"] not in ("PENDING", "DEFERRED"):
                continue
            deps = self.worker_graph["edges"].get(wid) or []
            unfinished = [dep for dep in deps
                          if self.states[dep]["state"] != "DONE"]
            if unfinished:
                entry.update(state="BLOCKED",
                             reason="dependency_not_finished:"
                             + ",".join(unfinished))
            elif failed_seen:
                entry.update(state="BLOCKED", reason="upstream_failure")
            else:
                entry.update(state="BLOCKED", reason="not_dispatchable")

        if self.preserve_on_failure:
            for wid, entry in self.states.items():
                if entry["state"] in ("FAILED", "INVALID_EVIDENCE"):
                    run_dir = (common_paths.get_orchestrator_dir(self.repo)
                               / _safe_id(wid))
                    worker = self.by_id.get(wid, {})
                    exec_path = self.dispatcher.paths.get(wid, self.repo)
                    with _suppress(Exception):
                        worker_execution.persist_failure_evidence(
                            worker, exec_path, entry, run_dir)
        self.dispatcher.cleanup()
        report["worktrees"] = {wid: str(path)
                               for wid, path in self.dispatcher.paths.items()}
        report["cleanup_errors"] = list(self.dispatcher.cleanup_errors)
        all_done = all(entry["state"] == "DONE"
                       for entry in self.states.values())
        report["status"] = "ok" if all_done else "failed"
        report["reason"] = None if all_done else (
            fail_reason or "incomplete")
        report["graph"] = self._graph_report()
        return report

    def _read_blob(self, tree: str, path: str) -> str | None:
        proc = subprocess.run(["git", "show", f"{tree}:{path}"],
                              cwd=self.repo, capture_output=True)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", errors="replace")


def run_workers_dag(
    repo: Any,
    workers: list[dict[str, Any]],
    *,
    task_id: str,
    keep_worktrees: bool = False,
    fast_path_enabled: bool = False,
    classification_context: tuple[Any, ...] = (),
    execute_hook=None,
    preserve_on_failure: bool = False,
) -> dict[str, Any]:
    """Facade: minimal DAG coordinator for multi-worker governed runs.

    Replaces GovernedScheduler(repo, workers, ...).run() for live
    production callers (MCP apply=true, CLI run --apply).
    """
    coordinator = DAGCoordinator(
        repo, validate_workers(workers), task_id=task_id, keep_worktrees=keep_worktrees,
        fast_path_enabled=fast_path_enabled,
        classification_context=classification_context,
        preserve_on_failure=preserve_on_failure)
    if execute_hook is not None:
        coordinator.execute = execute_hook  # type: ignore[attr-defined]
    return coordinator.run()
