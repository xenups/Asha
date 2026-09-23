#!/usr/bin/env python3
"""Asha native MCP server -- stdio JSON-RPC, protocol handshake phase.

Transport contract: stdout carries ONLY JSON-RPC response frames (one
compact JSON object per line); every warning, log and diagnostic goes
to stderr. A handler failure becomes a structured JSON-RPC error --
the live transport must never see a traceback or a dead process.

Protocol version negotiation: initialize echoes any SUPPORTED
requested version verbatim (no silent upgrade); unsupported/missing
versions fall back to DEFAULT_PROTOCOL_VERSION with a one-line stderr
warning -- the handshake is never failed by version mismatch.

Stdlib only: json/sys/typing. No `mcp`, `anyio` or `pydantic`.

Runnable as `python .hermes/tools/orchestrator/mcp_server.py` or
`python -m orchestrator.mcp_server` (tools dir on path). The literal
`python -m .hermes.tools.orchestrator.mcp_server` from the spec is not
valid module syntax -- a module component cannot start with a dot.
"""
from __future__ import annotations

if __package__ in (None, ""):
    # Direct-script spawn: drop the package dir from sys.path BEFORE any
    # stdlib import -- otherwise orchestrator/types.py shadows stdlib
    # `types` and `import json` dies in the enum chain (circular). Pure
    # string ops only: pathlib/json are unsafe while the shadow sits on
    # the path. Same proven guard as orchestrator/__main__.py.
    import sys as _script_sys

    def _norm(entry: str) -> str:
        return (entry or ".").replace(chr(92), "/").rstrip("/").lower()

    _pkg_dir = _norm(__file__).rsplit("/", 1)[0]
    _script_sys.path = [entry for entry in _script_sys.path
                        if _norm(entry) != _pkg_dir]
    # Put the tools dir on the path and adopt the package name so the
    # later relative imports (.types / .worktree) also work in script
    # mode (Phase 5.2: git-inspection reuse via worktree._git).
    _script_sys.path.insert(0, _pkg_dir.rsplit("/", 1)[0])
    __package__ = "orchestrator"

import json
import os
import sys
from collections.abc import Callable
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, TextIO

import dep_index
import graph_state

from .conflict import ConflictManager, scope_status
from .scheduler import GovernedScheduler, validate_workers
from .types import OrchestratorError
from .worktree import _git

SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18",
                               "2025-03-26", "2024-11-05"]
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
SERVER_INFO = {"name": "asha-orchestrator", "version": "0.1.0"}
CAPABILITIES: dict[str, Any] = {"tools": {}}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


ROOT_HELP = "Path to the git repository (default: current directory)."

ASHA_STATUS_SPEC: dict[str, Any] = {
    "name": "asha_status",
    "description": ("Inspect a git root without changing it: HEAD SHA, "
                    "porcelain status, active/orphaned worktrees and "
                    ".jspace control-lock state."),
    "inputSchema": {
        "type": "object",
        "properties": {"root": {"type": "string", "default": ".",
                                "description": ROOT_HELP}},
        "additionalProperties": False,
    },
}
ASHA_PLAN_DAG_SPEC: dict[str, Any] = {
    "name": "asha_plan_dag",
    "description": ("Static pre-flight DAG plan (never executes): in-scope "
                    "import dependency edges, topological generations over "
                    "declared deps (workers) or import edges (files), the "
                    "pairwise read/write conflict matrix, and per-node "
                    "safe/uncertain flags (UNKNOWN != SAFE)."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "root": {"type": "string",
                     "description": "Path to the repository to plan "
                                    "against (required)."},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Repo-relative files to index "
                                     "(default: the workers' declared "
                                     "reads/writes, else every .py file "
                                     "under root)"},
            "workers": {"description": "Candidate worker spec: a list of "
                                        "worker objects, an {id: worker} "
                                        "map, or one worker object"},
        },
        "required": ["root"],
        "additionalProperties": False,
    },
}
ASHA_RUN_SPEC_SPEC: dict[str, Any] = {
    "name": "asha_run_spec",
    "description": ("Run a run-spec through the governed Asha engine: "
                    "workers are normalized and validated exactly like "
                    "asha_plan_dag, then executed through "
                    "GovernedScheduler (worktrees, conflict gating, "
                    "scope verification, checks, sealed evidence). "
                    "apply=false (default) is a simulation with zero "
                    "side effects; apply=true executes for real but "
                    "never grants ship authority."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "spec_path": {"type": "string",
                          "description": "Path to a run-spec JSON file "
                                         "(provide exactly one of "
                                         "spec_path / spec_content)"},
            "spec_content": {"type": "object",
                             "description": "Inline run-spec JSON object "
                                            "(provide exactly one of "
                                            "spec_path / spec_content)"},
            "apply": {"type": "boolean", "default": False,
                      "description": "false = dry-run simulation "
                                     "(default); true = real governed "
                                     "execution (authorized_to_ship "
                                     "stays false)"},
            "root": {"type": "string", "default": ".",
                     "description": "Path to the repository (default: "
                                    "current directory)"},
        },
        "additionalProperties": False,
    },
}
TOOL_SPECS: list[dict[str, Any]] = [
    ASHA_PLAN_DAG_SPEC, ASHA_RUN_SPEC_SPEC, ASHA_STATUS_SPEC]


def _tool_ok(payload: dict[str, Any]) -> dict[str, Any]:
    """tools/call success: payload JSON-encoded as one text item."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False}


def _tool_error(message: str) -> dict[str, Any]:
    """tools/call failure: human-readable, never an unhandled raise."""
    return {"content": [{"type": "text", "text": message}],
            "isError": True}


def _lock_state(root: Path) -> dict[str, Any]:
    """.jspace control-lock state (exists + recorded pid).

    ponytail: holder liveness (stale-pid triage via `tasklist /FI`) is
    deliberately out of scope -- add when a caller needs it; never probe
    with os.kill(pid, 0), which on Windows terminates instead of tests.
    """
    lock_file = root / ".jspace" / "lock"
    if not lock_file.is_file():
        return {"exists": False, "pid": None}
    raw = lock_file.read_text(encoding="utf-8", errors="replace").strip()
    try:
        return {"exists": True, "pid": int(raw)}
    except ValueError:
        return {"exists": True, "pid": None}


def asha_status(arguments: dict[str, Any]) -> dict[str, Any]:
    """asha_status: read-only repo facts. Never raises -- every failure
    becomes `isError: true` content (tool-level fail-closed contract)."""
    try:
        root = arguments.get("root", ".")
        if not isinstance(root, str):
            return _tool_error("root must be a string")
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            return _tool_error(
                f"root does not exist or is not a directory: {root}")
        resolved = root_path.resolve()
        try:
            head = _git(resolved, "rev-parse", "HEAD").strip()
            status = _git(resolved, "status", "--porcelain")
            worktrees_raw = _git(resolved, "worktree", "list",
                                 "--porcelain")
        except OrchestratorError as exc:
            return _tool_error(f"git inspection failed: {exc}")
        active = [line.split(" ", 1)[1]
                  for line in worktrees_raw.splitlines()
                  if line.startswith("worktree ")]
        registered = {os.path.normcase(item) for item in active}
        worktree_root = resolved.parent / (resolved.name + ".worktrees")
        orphans: list[str] = []
        if worktree_root.is_dir():
            orphans = [str(item)
                       for item in sorted(worktree_root.iterdir())
                       if item.is_dir()
                       and os.path.normcase(str(item)) not in registered]
        payload = {"root": str(resolved),
                   "head": head,
                   "clean": status == "",
                   "status": status.splitlines(),
                   "worktrees": {"active": active, "orphaned": orphans},
                   "lock": _lock_state(resolved)}
        return _tool_ok(payload)
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


# -- Phase 5.4: asha_plan_dag (static planning; reuse, never re-invent) ----
# Default scans stay bounded and stdlib-fast: a plan must never walk a
# virtualenv or a node_modules tree.
# Any matching path COMPONENT is excluded: "venv" therefore also covers
# nested trees like .hermes/venv (reviewed in Phase 5.5).
PLAN_EXCLUDED_DIRS = frozenset({"node_modules", ".git", "__pycache__",
                                ".venv", "site-packages", "venv",
                                "dist", "build", ".mypy_cache",
                                ".pytest_cache", ".ruff_cache"})


def _plan_workers(raw: Any) -> list[dict[str, Any]] | None:
    """Normalize the candidate worker spec: a list, an {id: worker} map,
    or one worker object. Anything else fails closed (ValueError)."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw  # validate_workers() performs the structural gate
    if isinstance(raw, dict):
        if "id" in raw:
            return [raw]
        if not all(isinstance(value, dict) for value in raw.values()):
            raise ValueError("workers map values must be worker objects")
        return [dict(value, id=key) for key, value in raw.items()]
    raise ValueError("workers must be a list, a {id: worker} map, "
                     "or a single worker object")


def _plan_scope(root: Path, paths: list[str] | None,
                workers: list[dict[str, Any]] | None,
                ) -> tuple[list[str], list[str]]:
    """(files to index, declared-but-absent paths) as root-relative
    posix strings. Precedence: explicit `paths` > the workers' declared
    reads/writes > every .py file under root outside the excluded dirs."""
    if paths is None and workers is None:
        scanned: list[str] = []
        for file in root.rglob("*.py"):
            relative = file.relative_to(root)
            if set(relative.parts) & PLAN_EXCLUDED_DIRS:
                continue
            scanned.append(relative.as_posix())
        return sorted(set(scanned)), []
    declared: list[str] = []
    if paths is not None:
        declared = paths
    else:
        assert workers is not None
        for worker in workers:
            for key in ("writes", "reads"):
                value = worker.get(key)
                if isinstance(value, list):
                    declared.extend(item for item in value
                                    if isinstance(item, str))
    scope: set[str] = set()
    absent: set[str] = set()
    for entry in dict.fromkeys(declared):
        rel = entry.removeprefix("./")
        if rel.startswith("/") or ".." in rel.split("/"):
            raise ValueError(f"path must stay inside root: {entry!r}")
        if (root / rel).is_file():
            scope.add(rel)
        else:
            absent.add(rel)
    return sorted(scope), sorted(absent)


def _plan_generations(
    nodes: list[str], predecessors: dict[str, list[str]],
) -> tuple[list[list[str]], list[str]]:
    """Topological layers via graphlib's TopologicalSorter -- the same
    primitive graph_state.reconcile cycle-gates on; `add(node, *preds)`
    puts each node after its predecessors. A cycle is REPORTED (its
    members returned) and every node fuses into one layer -- nodes are
    never silently dropped, never silently ordered."""
    known = set(nodes)
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for node in sorted(known):
        sorter.add(node, *[pred for pred in predecessors.get(node, ())
                           if pred in known and pred != node])
    try:
        sorter.prepare()
    except CycleError as exc:
        members = (set(exc.args[1]) if len(exc.args) > 1
                   and isinstance(exc.args[1], list) else known)
        return [sorted(known)], sorted(members & known or known)
    layers: list[list[str]] = []
    while True:
        ready = sorter.get_ready()
        if not ready:
            break
        layers.append(sorted(ready))
        sorter.done(*ready)   # done(*nodes): unblocks this batch's successors
    return layers, []


def asha_plan_dag(arguments: dict[str, Any]) -> dict[str, Any]:
    """Tool handler: static pre-flight plan over the shared engines
    (dep_index facts -> graph_state.reconcile edge building ->
    scheduler.validate_workers -> conflict.ConflictManager). Never
    executes a worker, never mutates the repo, never raises."""
    try:
        root = arguments.get("root")
        if not isinstance(root, str) or not root.strip():
            return _tool_error("root must be a non-empty string")
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            return _tool_error(
                f"root does not exist or is not a directory: {root}")
        resolved_root = root_path.resolve()

        paths = arguments.get("paths")
        if paths is not None and (
                not isinstance(paths, list)
                or not all(isinstance(item, str) for item in paths)):
            return _tool_error(
                "paths must be a list of repo-relative strings")
        workers = _plan_workers(arguments.get("workers"))
        if workers is not None:
            try:
                validate_workers(workers)  # the orchestrator's own gate
            except OrchestratorError as exc:
                return _tool_error(f"invalid worker spec: {exc}")
        scope_files, absent = _plan_scope(resolved_root, paths, workers)

        # Facts through the shared cache: one ast.parse per (path, sha).
        index = dep_index.DependencyIndex()
        contents: dict[str, str] = {}
        uncertain_facts: list[dict[str, str]] = []
        uncertain_files: set[str] = set()
        for rel in scope_files:
            text = (resolved_root / rel).read_text(encoding="utf-8",
                                                    errors="replace")
            contents[rel] = text
            for fact in index.analyze(rel, text):
                if fact.confidence == dep_index.UNCERTAIN:
                    uncertain_files.add(rel)
                    uncertain_facts.append({
                        "source": fact.source, "target": fact.target,
                        "relation": fact.relation,
                        "location": fact.location})

        # Edge building through graph_state.reconcile: the SAME resolver
        # and cycle gate the orchestrator publishes state with. Only
        # certain files enter the batch; uncertainty is reported as fact
        # rows (UNKNOWN never silently becomes EMPTY). The index cache
        # makes this second pass parse-free.
        certain = {rel: text for rel, text in contents.items()
                   if rel not in uncertain_files}
        file_edges: dict[str, Any] = {}
        cycle_reconcile: set[str] = set()
        reconcile_failure: str | None = None
        if certain:
            outcome = graph_state.reconcile(
                graph_state.GraphState.empty(), index, certain)
            if outcome.ok:
                file_edges = {src: set(targets)
                              for src, targets in outcome.state.edges.items()}
            else:
                reconcile_failure = outcome.reason
                if outcome.reason and outcome.reason.startswith("cycle:"):
                    cycle_reconcile = set(outcome.reason[6:].split(","))

        scope_set = set(scope_files)
        in_scope_edges = {src: {target for target in targets
                                if target in scope_set}
                          for src, targets in file_edges.items()
                          if src in scope_set}
        import_edges = sorted(
            ({"source": src, "target": target}
             for src, targets in in_scope_edges.items()
             for target in targets),
            key=lambda edge: (edge["source"], edge["target"]))

        if workers is not None:
            nodes = [str(worker["id"]) for worker in workers]
            predecessors = {str(worker["id"]): [
                str(dep) for dep in (worker.get("deps") or [])]
                for worker in workers}
        else:
            nodes = list(scope_files)
            predecessors = {src: sorted(targets)
                            for src, targets in in_scope_edges.items()}
        generations, cycle_graph = _plan_generations(nodes, predecessors)
        cycle = sorted(cycle_reconcile | set(cycle_graph))

        # Pairwise conflict matrix (workers mode): one assess per pair
        # with only the counterpart running -- the manager's own rule.
        matrix: dict[str, dict[str, str]] = {}
        by_id = ({str(worker["id"]): worker for worker in workers}
                 if workers is not None else {})
        ids = sorted(by_id)
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                manager = ConflictManager()
                manager.start(right, by_id[right])
                _, reason = manager.assess(by_id[left])
                manager.finish(right)
                matrix.setdefault(left, {})[right] = reason
                matrix.setdefault(right, {})[left] = reason

        # Per-node safety (UNKNOWN != SAFE): uncertain when (i) an
        # UNCERTAIN dep fact touches the node's file(s), (ii) declared
        # scope is invalid, (iii) any conflict cell is not
        # proven_disjoint, (iv) the node sits in an import cycle.
        safety: dict[str, str] = {}
        notes: dict[str, str] = {}
        cycle_set = set(cycle)
        if workers is not None:
            for worker in workers:
                wid = str(worker["id"])
                declared = {str(path) for key in ("writes", "reads")
                            for path in (worker.get(key) or [])}
                reasons: list[str] = []
                if declared & uncertain_files:
                    reasons.append(
                        "uncertain_dep_fact:"
                        + ",".join(sorted(declared & uncertain_files)))
                scope_ok, scope_why = scope_status(worker)
                if not scope_ok:
                    reasons.append(scope_why)
                cells = [matrix.get(wid, {}).get(other)
                         for other in ids if other != wid]
                clash = [cell for cell in cells
                         if cell and cell != "proven_disjoint"]
                if clash:
                    reasons.append(f"conflict:{clash[0]}")
                if wid in cycle_set:
                    reasons.append("import_cycle")
                elif declared & cycle_set:
                    reasons.append(
                        "import_cycle:"
                        + ",".join(sorted(declared & cycle_set)))
                safety[wid] = "uncertain" if reasons else "safe"
                if reasons:
                    notes[wid] = "; ".join(reasons)
        else:
            for rel in scope_files:
                reasons = []
                if rel in uncertain_files:
                    reasons.append("uncertain_dep_fact")
                if rel in cycle_set:
                    reasons.append("import_cycle")
                safety[rel] = "uncertain" if reasons else "safe"
                if reasons:
                    notes[rel] = "; ".join(reasons)

        payload: dict[str, Any] = {
            "root": str(resolved_root),
            "mode": "workers" if workers is not None else "files",
            "nodes": sorted(nodes),
            "import_edges": import_edges,
            "generations": generations,
            "conflict_matrix": matrix,
            "safety": {node: safety[node] for node in sorted(safety)},
            "safety_notes": {node: notes[node]
                             for node in sorted(notes)},
            "uncertain_facts": uncertain_facts,
        }
        if absent:
            payload["unanalyzed_paths"] = absent
        if cycle:
            payload["cycle"] = cycle
        if reconcile_failure and not cycle:
            payload["reconcile_failure"] = reconcile_failure
        return _tool_ok(payload)
    except ValueError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


def asha_run_spec(arguments: dict[str, Any]) -> dict[str, Any]:
    """Tool handler: DRY-RUN only (Phase 5.5). `apply=true` is
    intentionally rejected -- enabling real writes needs its own review.
    Workers reuse the Phase-5.4 normalizer, the validate_workers gate,
    and asha_plan_dag's DAG verbatim: one pre-flight, never two."""
    try:
        apply_flag = arguments.get("apply", False)
        if not isinstance(apply_flag, bool):
            return _tool_error("apply must be a boolean")
        root = arguments.get("root", ".")
        if not isinstance(root, str) or not root.strip():
            return _tool_error("root must be a non-empty string")
        spec_path = arguments.get("spec_path")
        spec_content = arguments.get("spec_content")
        if (spec_path is None) == (spec_content is None):
            return _tool_error(
                "provide exactly one of spec_path or spec_content")
        if spec_path is not None:
            if not isinstance(spec_path, str):
                return _tool_error("spec_path must be a string")
            path = Path(spec_path).expanduser()
            if not path.is_file():
                return _tool_error(f"spec file not found: {spec_path}")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return _tool_error(f"cannot read spec file: {exc}")
        else:
            raw = spec_content
        if not isinstance(raw, dict):
            return _tool_error("spec must be a JSON object with workers")
        if "workers" not in raw or raw["workers"] is None:
            return _tool_error("spec must contain a 'workers' entry")
        workers = _plan_workers(raw["workers"])
        if workers is None:
            return _tool_error("spec.workers must not be null")
        try:
            validate_workers(workers)
        except OrchestratorError as exc:
            return _tool_error(f"invalid worker spec: {exc}")

        # Reuse the Phase-5.4 DAG verbatim: same normalizer, same
        # validator, same generations -- no second builder exists here.
        plan_result = asha_plan_dag({"root": root, "workers": workers})
        if plan_result["isError"]:
            return plan_result
        plan = json.loads(plan_result["content"][0]["text"])
        generations: list[list[str]] = plan["generations"]

        if not apply_flag:
            completed = [
                {"worker_id": worker_id, "generation": index,
                 "state": "SIMULATED_DONE"}
                for index, layer in enumerate(generations)
                for worker_id in layer]
            return _tool_ok({
                "dry_run": True,
                "root": plan["root"],
                "generation_count": len(generations),
                "generations": generations,
                "completed": completed,
                "safety": plan["safety"],
            })

        # Phase 5.6 -- apply=true: a thin ADAPTER over the same engine
        # control.py delegates to. Every validation above already passed,
        # so nothing has executed yet; side effects begin inside run().
        # This handler adds no execution logic of its own: worktrees,
        # conflict gating, scope verification, checks and sealed
        # evidence are all the existing orchestrator's own.
        task_id = "mcp-" + os.urandom(6).hex()
        scheduler: GovernedScheduler | None = None
        try:
            scheduler = GovernedScheduler(Path(plan["root"]), workers,
                                          task_id=task_id)
            report = scheduler.run()
        except Exception as exc:
            # Refused to start or died catastrophically: report the real
            # state, never a fabricated success (partial = partial).
            snapshot = (json.dumps(
                {wid: entry.get("state")
                 for wid, entry in scheduler.states.items()},
                sort_keys=True) if scheduler is not None else "{}")
            return _tool_error(
                f"orchestration aborted: {type(exc).__name__}: {exc}; "
                f"states={snapshot}")

        states: dict[str, Any] = report.get("states") or {}

        def bucket(state: str) -> list[str]:
            return [wid for wid in sorted(states)
                    if states[wid].get("state") == state]

        return _tool_ok({
            "dry_run": False,
            "root": plan["root"],
            "task_id": task_id,
            "status": report.get("status"),
            "reason": report.get("reason"),
            "generation_count": len(generations),
            "generations": generations,
            "states": states,
            "successful_workers": bucket("DONE"),
            "failed_workers": bucket("FAILED"),
            "deferred_workers": bucket("DEFERRED"),
            "blocked_workers": bucket("BLOCKED"),
            "invalid_evidence_workers": bucket("INVALID_EVIDENCE"),
            "evidence": report.get("evidence") or {},
            "deferrals": list(scheduler.deferral_events),
            "graph": report.get("graph") or {},
            "worktrees": report.get("worktrees") or {},
            "cleanup_errors": report.get("cleanup_errors") or [],
            # Merge law (scheduler._collect): worker evidence never
            # authorizes a ship -- the MCP layer is not one either.
            "authorized_to_ship": False,
        })
    except ValueError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "asha_plan_dag": asha_plan_dag,
    "asha_run_spec": asha_run_spec,
    "asha_status": asha_status,
}


def handle_message(message: dict[str, Any],
                   stderr: TextIO | None = None) -> dict[str, Any] | None:
    """Dispatch one parsed request -> response frame, or None.

    None means "no frame": JSON-RPC notifications (no `id`) are never
    answered, not even when they error. Any internal exception becomes
    a structured INTERNAL_ERROR response instead of a traceback.
    """
    stream = sys.stderr if stderr is None else stderr
    try:
        method = message.get("method")
        request_id = message.get("id")
        has_id = "id" in message
        params = message.get("params")
        if not isinstance(method, str) or not method:
            if not has_id:
                return None
            return _error(request_id, INVALID_REQUEST,
                          "missing or invalid method")
        if params is not None and not isinstance(params, dict):
            if not has_id:
                return None
            return _error(request_id, INVALID_PARAMS,
                          "params must be an object")
        params = params or {}

        if method == "initialize":
            requested = params.get("protocolVersion")
            if requested in SUPPORTED_PROTOCOL_VERSIONS:
                chosen = requested
            else:
                chosen = DEFAULT_PROTOCOL_VERSION
                print(f"mcp: unsupported protocolVersion {requested!r}; "
                      f"answering {DEFAULT_PROTOCOL_VERSION}",
                      file=stream, flush=True)
            if not has_id:
                return None
            return _result(request_id, {
                "protocolVersion": chosen,
                "capabilities": CAPABILITIES,
                "serverInfo": SERVER_INFO,
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _result(request_id, {}) if has_id else None
        if method == "tools/list":
            return _result(request_id, {"tools": TOOL_SPECS}) if has_id else None
        if method == "tools/call":
            if not has_id:
                return None
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or not name:
                return _error(request_id, INVALID_PARAMS,
                              "tools/call requires params.name")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return _error(request_id, INVALID_PARAMS,
                              "params.arguments must be an object")
            handler = TOOL_HANDLERS.get(name)
            if handler is None:
                return _error(request_id, INVALID_PARAMS,
                              f"unknown tool: {name}")
            return _result(request_id, handler(arguments))
        if not has_id:
            return None
        return _error(request_id, METHOD_NOT_FOUND,
                      f"method not found: {method}")
    except Exception as exc:  # belt: structured error, never a traceback
        recoverable_id = (message.get("id")
                          if isinstance(message, dict) else None)
        return _error(recoverable_id, INTERNAL_ERROR,
                      f"{type(exc).__name__}: {exc}")


def _emit(stdout: TextIO, payload: dict[str, Any]) -> None:
    """stdout is reserved for JSON-RPC frames: exactly one line each."""
    stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stdout.flush()


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None,
          stderr: TextIO | None = None) -> int:
    """Line-framed stdio loop: survives bad frames, returns 0 at EOF."""
    in_stream = sys.stdin if stdin is None else stdin
    out_stream = sys.stdout if stdout is None else stdout
    err_stream = sys.stderr if stderr is None else stderr
    for raw in in_stream:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit(out_stream, _error(None, PARSE_ERROR,
                                     f"parse error: {exc}"))
            continue
        if not isinstance(message, dict):
            _emit(out_stream, _error(None, INVALID_REQUEST,
                                     "message must be a JSON-RPC object"))
            continue
        response = handle_message(message, err_stream)
        if response is not None:
            _emit(out_stream, response)
    return 0


def main() -> int:
    """Entry point (script or `python -m orchestrator.mcp_server`)."""
    return serve()


if __name__ == "__main__":
    sys.exit(main())
