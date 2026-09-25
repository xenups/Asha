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

Runnable as `python -m asha.mcp_server` (or the `asha-mcp` console
script). A literal `python -m .<dotted.path>` invocation is never
valid module syntax -- a module component cannot start with a dot.
"""
from __future__ import annotations

if __package__ in (None, ""):
    # Direct-script spawn: drop the package dir from sys.path BEFORE any
    # stdlib import -- otherwise the sibling types.py shadows stdlib
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
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from time import perf_counter_ns
from typing import Any, TextIO

from . import dep_index, evidence, graph_state, worker_graph
from .ast_indexer import ImportFact, ModuleIndex, index_module
from .classifier import classify_task, governance_profile
from .codegraph import build_graph, closure, sym_node
from .conflict import ConflictManager, scope_status
from .context_slicer import slice_context
from .router import RuntimeMode, route
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


# -- Phase 4.3: runtime configuration + operational telemetry --------------
# Fast Path comes ONLY from the process environment. No input schema
# carries it, no task/prompt/spec field can set or override it, and
# an unknown field that looks like it is rejected as unknown input.
FAST_PATH_ENV = "ASHA_FAST_PATH_ENABLED"
TELEMETRY_NAME = "mcp_live_telemetry.jsonl"


def _fast_path_enabled() -> bool:
    """Env-only runtime config: exactly "1" -> True, anything else or
    unset -> False (safe default). Operational metadata, never a
    client input."""
    return os.environ.get(FAST_PATH_ENV) == "1"


def _telemetry_repo(arguments: dict[str, Any]) -> Path:
    """Where this call's event goes: the call's own valid root when it
    names a real directory, else the server process cwd. A client can
    never point telemetry at an arbitrary path."""
    for key in ("root", "repo_path"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            candidate = Path(value).expanduser()
            if candidate.is_dir():
                return candidate.resolve()
    return Path.cwd()


_META_KEYS = ("classification", "reason_code", "runtime_mode",
              "routing_reason", "fast_path_enabled", "target",
              "dependencies_count", "unresolved_count",
              "full_source_bytes", "context_source_bytes",
              "reduction_ratio", "state", "task_id")


def _safe_metadata(payload: Any) -> dict[str, Any]:
    """Allowlisted operational fields only: never prompt, never cmd,
    never raw arguments, never error text, never environment values."""
    meta: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return meta
    for key in _META_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, (bool, int, float)):
            meta[key] = value
        elif value is None:
            meta[key] = None
        elif isinstance(value, str):
            meta[key] = value[:200]
    timings = payload.get("timings")
    if isinstance(timings, dict):
        meta["timings"] = {key: value for key, value in timings.items()
                           if isinstance(key, str)
                           and isinstance(value, (int, float))}
    return meta


def _emit_telemetry(repo: Path, tool: str, request_id: Any,
                    duration_ns: int, status: str,
                    metadata: dict[str, Any]) -> None:
    """Append ONE event to <repo>/.jspace/mcp_live_telemetry.jsonl.

    Operational observability ONLY -- not authoritative evidence, not
    an audit proof, not tamper-proof, not a commitment. Fail-safe by
    contract: every failure (bad disk, serialization, permissions) is
    swallowed, so telemetry failure can never change routing,
    authorization or execution semantics. Concurrency: one complete
    line per single os.write on an O_APPEND fd.
    """
    try:
        event = {
            "timestamp": datetime.now(UTC)
                         .isoformat(timespec="milliseconds"),
            "tool": str(tool)[:100],
            "request_id": str(request_id)[:200],
            "duration_ms": round(duration_ns / 1e6, 3),
            "status": status,
            "metadata": metadata,
        }
        line = (json.dumps(event, sort_keys=True, ensure_ascii=False)
                + "\n").encode("utf-8")
        jspace = repo / ".jspace"
        jspace.mkdir(parents=True, exist_ok=True)
        fd = os.open(jspace / TELEMETRY_NAME,
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        # Fail-safe by contract: telemetry is best-effort observability;
        # a write failure must never surface to or alter the call.
        return


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

        worker_candidates: Any = None
        if workers is not None:
            nodes = [str(worker["id"]) for worker in workers]
            # Section 22: the SAME projection the scheduler publishes
            # with -- declared deps UNION CodeGraph-derived edges via
            # worker_graph; no MCP-specific graph or scheduler exists.
            worker_candidates = worker_graph.derive(
                workers, file_edges, completed=frozenset())
            predecessors = {wid: sorted(preds) for wid, preds
                            in worker_candidates.edges.items()}
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
                if (worker_candidates is not None
                        and wid in worker_candidates.uncertain):
                    # Sections 6/14: ambiguous live ownership = UNKNOWN,
                    # never reported as safe (UNKNOWN != SAFE).
                    reasons.append(
                        "owner_ambiguous:"
                        + ",".join(worker_candidates.ambiguous_paths))
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


# -- Phase 4.3: asha_get_surgical_context (read-only Phase 4.1 pipeline) --
ASHA_GET_SURGICAL_CONTEXT_SPEC: dict[str, Any] = {
    "name": "asha_get_surgical_context",
    "description": ("Read-only surgical context extraction for one "
                    "symbol in one repo file (Phase 4.1 pipeline: AST "
                    "index -> code graph -> dependency closure -> "
                    "surgical slice). Returns the full dependency "
                    "closure including every UNRESOLVED boundary "
                    "nothing is silently dropped), with byte counts; "
                    "reduction_ratio is SOURCE BYTES only, never a "
                    "token claim."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target_file": {"type": "string",
                            "description": "Repo-relative file that "
                                           "contains the symbol."},
            "target_symbol": {"type": "string",
                              "description": "Symbol name as indexed "
                                             "(function/class/variable; "
                                             "'Class.method' for methods)."},
            "repo_path": {"type": "string", "default": ".",
                          "description": ROOT_HELP},
        },
        "required": ["target_file", "target_symbol"],
        "additionalProperties": False,
    },
}

ASHA_DISPATCH_TASK_SPEC: dict[str, Any] = {
    "name": "asha_dispatch_task",
    "description": ("Dispatch ONE governed worker through the real Asha "
                    "pipeline: Phase 4.0 classification -> Phase 4.2 "
                    "router (fail-closed) -> GovernedScheduler "
                    "(worktrees, conflict gating, scope verification, "
                    "sealed evidence). Fast Path is decided ONLY by "
                    "process runtime config ASHA_FAST_PATH_ENABLED=1 "
                    "plus a proven-disjoint classification; no input "
                    "field can set or override it. Result carries the "
                    "decision and timings; authorized_to_ship stays "
                    "false."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {"type": "string",
                   "description": "Unique worker id."},
            "declared_scope": {"type": "array",
                               "items": {"type": "string"},
                               "description": "Repo-relative paths this "
                                              "task may touch."},
            "reads": {"type": "array", "items": {"type": "string"}},
            "writes": {"type": "array", "items": {"type": "string"}},
            "deps": {"type": "array", "items": {"type": "string"}},
            "cmd": {"type": "array", "items": {"type": "string"}},
            "prompt": {"type": "string",
                       "description": "Task metadata only: never "
                                      "executed, never logged to "
                                      "telemetry."},
            "root": {"type": "string", "default": ".",
                     "description": ROOT_HELP},
        },
        "required": ["id", "declared_scope", "cmd"],
        "additionalProperties": False,
    },
}


def _module_name(rel: str) -> str | None:
    """'pkg/mod.py' -> 'pkg.mod'; 'pkg/__init__.py' -> 'pkg'.
    None for anything not indexable as a Python module."""
    if not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or any(not part for part in parts):
        return None
    return ".".join(parts)


def _import_candidates(root: Path, consumer: str,
                       fact: ImportFact) -> list[str]:
    """Repo-relative files a single import fact can reach. Resolution
    is lexical only: a target that does not exist as a file is simply
    NOT followed (it surfaces later as an EXTERNAL/UNRESOLVED graph
    boundary -- never guessed, never dropped)."""
    dotted_roots: list[str] = []
    if fact.level == 0:
        if fact.module:
            dotted_roots.append(fact.module)
            if fact.kind in ("from", "star"):
                for original, _alias in fact.names:
                    if original:
                        dotted_roots.append(f"{fact.module}.{original}")
        elif fact.kind in ("from", "star"):
            dotted_roots.extend(original for original, _ in fact.names
                                if original)
    else:
        parts = consumer.split(".")
        prefix = ".".join(parts[:max(len(parts) - fact.level, 0)])
        base = f"{prefix}.{fact.module}" if fact.module else prefix
        if base:
            dotted_roots.append(base)
        if fact.kind in ("from", "star"):
            for original, _alias in fact.names:
                if original:
                    dotted_roots.append(
                        f"{base}.{original}" if base else original)
    found: list[str] = []
    for dotted in dict.fromkeys(dotted_roots):
        rel = dotted.replace(".", "/")
        if set(Path(rel).parts) & PLAN_EXCLUDED_DIRS:
            continue
        for candidate in (f"{rel}.py", f"{rel}/__init__.py"):
            if (root / candidate).is_file():
                found.append(candidate)
                break
    return found


def _index_reachable(root: Path, start_rel: str
                     ) -> tuple[ModuleIndex, ...]:
    """Index the start file plus every repo module reachable through
    its import facts (lexically, BFS, visited-set, excluded dirs
    skipped). Bounded by the repo's own import graph."""
    visited: set[str] = set()
    ordered: list[ModuleIndex] = []
    queue: list[str] = [start_rel]
    while queue:
        rel = queue.pop(0)
        if rel in visited:
            continue
        visited.add(rel)
        module = _module_name(rel)
        if module is None:
            continue
        try:
            source = (root / rel).read_text(encoding="utf-8",
                                             errors="replace")
        except OSError:
            continue  # unreadable target -> boundary, not a crash
        index = index_module(module, source)
        ordered.append(index)
        for fact in index.imports:
            queue.extend(_import_candidates(root, module, fact))
    ordered.sort(key=lambda entry: entry.module)
    return tuple(ordered)


def asha_get_surgical_context(arguments: dict[str, Any]
                              ) -> dict[str, Any]:
    """Read-only Phase 4.1 pipeline. Never raises: every failure
    becomes structured error content (tool-level fail-closed)."""
    try:
        unknown = sorted(set(arguments) - {"target_file", "target_symbol",
                                           "repo_path"})
        if unknown:
            return _tool_error("unknown field(s): " + ", ".join(unknown))
        target_file = arguments.get("target_file")
        if not isinstance(target_file, str) or not target_file:
            return _tool_error("target_file must be a non-empty string")
        target_symbol = arguments.get("target_symbol")
        if not isinstance(target_symbol, str) or not target_symbol:
            return _tool_error("target_symbol must be a non-empty string")
        repo_arg = arguments.get("repo_path", ".")
        if not isinstance(repo_arg, str):
            return _tool_error("repo_path must be a string")
        repo = Path(repo_arg).expanduser()
        if not repo.is_dir():
            return _tool_error(
                f"repo_path does not exist or is not a directory: {repo_arg}")
        root = repo.resolve()
        rel = target_file.replace(chr(92), "/")
        if rel.startswith("/") or ".." in rel.split("/"):
            return _tool_error(
                f"target_file must stay inside the repository: {target_file}")
        if not rel.endswith(".py"):
            return _tool_error("target_file must be a .py file")
        source_file = root / rel
        if not source_file.is_file():
            return _tool_error(f"target_file not found: {target_file}")
        source = source_file.read_text(encoding="utf-8", errors="replace")
        module = _module_name(rel)
        if module is None:
            return _tool_error(f"cannot derive module name: {target_file}")

        # Phase 4.1 pipeline, in order: index -> graph -> closure -> slice
        indices = _index_reachable(root, rel)
        target_index = next((entry for entry in indices
                             if entry.module == module), None)
        if target_index is None or target_index.symbol(target_symbol) is None:
            return _tool_error(
                f"symbol not found in target file: {target_symbol}")
        graph = build_graph(indices)
        root_node = sym_node(module, target_symbol)
        reach = closure(graph, (root_node,), module)
        sliced = slice_context(source, target_name=target_symbol,
                               target_module=module, graph=graph,
                               indices=indices)
        context_text = (sliced.target_source
                        + "".join(stub.text for stub in sliced.stubs))
        full_bytes = sliced.full_bytes
        context_bytes = sliced.surgical_bytes
        ratio = (1.0 - context_bytes / full_bytes) if full_bytes else 0.0
        payload = {
            "target": f"{rel}:{target_symbol}",
            "context": context_text,
            # every closure member except the root, sorted; UNRESOLVED
            # boundaries stay in `unresolved` (reported, never dropped)
            "dependencies": [node for node in reach.reachable
                             if node != root_node],
            "unresolved": list(reach.unresolved),
            "full_source_bytes": full_bytes,
            "context_source_bytes": context_bytes,
            "reduction_ratio": ratio,
        }
        return _tool_ok(payload)
    except ValueError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


def _dispatch_context(root: Path) -> tuple[dict[str, Any], ...]:
    """Classifier context adapter (Phase 4.3 -- registered mismatch):
    classify_task demands a NON-EMPTY envelope of other executions
    (empty context is a missing signal, never a vacuous disjoint
    proof), but a lone MCP dispatch has no in-wave peers. The envelope
    therefore comes from THIS repo's sealed execution records under
    .jspace/cache/orchestrator -- real recorded surfaces, sorted by
    id, never caller-supplied. Absence of records stays UNKNOWN; a
    record that is unreadable, unsealed or tampered with enters as a
    None-surface peer so classification fails closed to UNKNOWN."""
    cache = root / ".jspace" / "cache" / "orchestrator"
    peers: dict[str, dict[str, Any]] = {}
    if not cache.is_dir():
        return ()
    # records live one level deeper: cache/orchestrator/<task>/<wid>.json
    for path in sorted(cache.rglob("*.json")):
        peer_id = f"record:{path.stem}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        sealed_ok = (
            isinstance(payload, dict)
            and isinstance(payload.get("evidence_sha256"), str)
            and evidence.compute_digest(payload)
            == payload.get("evidence_sha256"))
        if not sealed_ok:
            peers.setdefault(peer_id, {"id": peer_id, "reads": None,
                                       "writes": None})
            continue
        wid = str(payload.get("worker_id") or peer_id)
        read_set = payload.get("read_set")
        write_set = payload.get("write_set")
        declared = payload.get("declared_scope")
        peers.setdefault(wid, {
            "id": wid,
            "reads": list(read_set) if isinstance(read_set, list) else None,
            "writes": list(write_set) if isinstance(write_set, list) else None,
            "declared_scope": (list(declared)
                               if isinstance(declared, list) else []),
        })
    return tuple(peers[key] for key in sorted(peers))


def _str_list(arguments: dict[str, Any], key: str, *,
              default: list[str] | None = None
              ) -> tuple[list[str] | None, str | None]:
    if key not in arguments or arguments[key] is None:
        return ([] if default is not None else None), None
    value = arguments[key]
    if not isinstance(value, list) or not all(
            isinstance(entry, str) for entry in value):
        return None, f"{key} must be a list of strings"
    return list(value), None


def asha_dispatch_task(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validation -> classify -> route -> GovernedScheduler -> result.

    Fail-closed ladder: unknown input fields (including anything that
    looks like runtime config) are rejected; a classification or
    routing exception returns a structured error with NOTHING
    executed; Fast Path requires BOTH env runtime config AND a proven
    disjoint coherent profile. The router decides the mode only --
    execution stays owned by GovernedScheduler, evidence stays on the
    existing _collect contract."""
    try:
        unknown = sorted(set(arguments) - {
            "id", "declared_scope", "reads", "writes", "deps", "cmd",
            "prompt", "root"})
        if unknown:
            return _tool_error("unknown field(s): " + ", ".join(unknown)
                               + " (runtime configuration is env-only "
                                 "and cannot be supplied as input)")
        wid = arguments.get("id")
        if not isinstance(wid, str) or not wid.strip():
            return _tool_error("id must be a non-empty string")
        declared = arguments.get("declared_scope")
        if not isinstance(declared, list) or not all(
                isinstance(entry, str) for entry in declared):
            return _tool_error("declared_scope must be a list of strings")
        reads, error = _str_list(arguments, "reads")
        if error:
            return _tool_error(error)
        writes, error = _str_list(arguments, "writes")
        if error:
            return _tool_error(error)
        deps, error = _str_list(arguments, "deps", default=[])
        if error:
            return _tool_error(error)
        assert deps is not None
        cmd = arguments.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(
                isinstance(entry, str) for entry in cmd):
            return _tool_error("cmd must be a non-empty list of strings")
        prompt = arguments.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            return _tool_error("prompt must be a string")
        # prompt is metadata only: never read again, never executed,
        # never telemetry'd.
        root_arg = arguments.get("root", ".")
        if not isinstance(root_arg, str):
            return _tool_error("root must be a string")
        root_path = Path(root_arg).expanduser()
        if not root_path.is_dir():
            return _tool_error(
                f"root does not exist or is not a directory: {root_arg}")
        root = root_path.resolve()

        envelope = _dispatch_context(root)
        task_payload = {"id": wid, "reads": reads, "writes": writes,
                        "declared_scope": list(declared), "deps": deps}
        fast_env = _fast_path_enabled()
        try:
            classify_started = perf_counter_ns()
            classification = classify_task(task_payload, envelope)
            profile = governance_profile(classification)
            classification_ms = (perf_counter_ns() - classify_started
                                 ) / 1e6
            route_started = perf_counter_ns()
            decision = route(profile, fast_path_enabled=fast_env)
            routing_ms = (perf_counter_ns() - route_started) / 1e6
        except Exception as exc:
            return _tool_error(
                f"classification/routing failed (fail-closed, nothing "
                f"executed): {type(exc).__name__}: {exc}")

        worker: dict[str, Any] = {
            "id": wid, "deps": deps, "declared_scope": list(declared),
            "reads": reads, "writes": writes, "cmd": [str(x) for x in cmd]}
        # single authorization point: the scheduler flag is the ROUTER's
        # own decision (env AND proven-disjoint-coherent), never raw input
        sched_fast = decision.mode is RuntimeMode.FAST_PATH
        scheduler = GovernedScheduler(
            root, [worker], task_id="mcp-" + os.urandom(6).hex(),
            fast_path_enabled=sched_fast,
            classification_context=envelope)
        evidence_ns: list[int] = []
        execution_ns: list[int] = []
        real_collect = scheduler._collect
        real_execute = scheduler.execute

        def timed_collect(worker_arg: dict[str, Any], path: Path,
                          rc: int, tail: str,
                          **kwargs: Any) -> dict[str, Any]:
            started = perf_counter_ns()
            try:
                return real_collect(worker_arg, path, rc, tail, **kwargs)
            finally:
                evidence_ns.append(perf_counter_ns() - started)

        def timed_execute(worker_arg: dict[str, Any],
                          path: Path) -> Any:
            started = perf_counter_ns()
            try:
                return real_execute(worker_arg, path)
            finally:
                execution_ns.append(perf_counter_ns() - started)

        scheduler._collect = timed_collect  # type: ignore[assignment]
        scheduler.execute = timed_execute  # type: ignore[assignment]
        run_started = perf_counter_ns()
        try:
            report = scheduler.run()
        except Exception as exc:
            snapshot = json.dumps(
                {key: entry.get("state")
                 for key, entry in scheduler.states.items()},
                sort_keys=True)
            return _tool_error(
                f"orchestration aborted: {type(exc).__name__}: {exc}; "
                f"states={snapshot}")
        scheduler_ms = (perf_counter_ns() - run_started) / 1e6
        states: dict[str, Any] = report.get("states") or {}
        worker_state = (states.get(wid) or {}).get("state")
        total_ms = (perf_counter_ns() - run_started) / 1e6
        payload = {
            "task_id": scheduler.task_id,
            "state": worker_state,
            "classification": decision.classification,
            "reason_code": classification.reason_code,
            "runtime_mode": decision.mode.value,
            "routing_reason": decision.reason_code,
            "fast_path_enabled": fast_env,
            "routing": report.get("routing"),
            "evidence": report.get("evidence") or {},
            "worktrees": report.get("worktrees") or {},
            "timings": {
                "classification_ms": round(classification_ms, 3),
                "routing_ms": round(routing_ms, 3),
                "context_ms": 0.0,
                "scheduler_ms": round(scheduler_ms, 3),
                "execution_ms": round(sum(execution_ns) / 1e6, 3),
                "evidence_ms": round(sum(evidence_ns) / 1e6, 3),
                "total_ms": round(total_ms, 3),
            },
            # Merge law preserved verbatim: dispatch grants no ship
            # authority of its own.
            "authorized_to_ship": False,
        }
        return _tool_ok(payload)
    except ValueError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


TOOL_SPECS: list[dict[str, Any]] = [
    ASHA_PLAN_DAG_SPEC, ASHA_RUN_SPEC_SPEC, ASHA_STATUS_SPEC,
    ASHA_GET_SURGICAL_CONTEXT_SPEC, ASHA_DISPATCH_TASK_SPEC]


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "asha_plan_dag": asha_plan_dag,
    "asha_run_spec": asha_run_spec,
    "asha_status": asha_status,
    "asha_get_surgical_context": asha_get_surgical_context,
    "asha_dispatch_task": asha_dispatch_task,
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
            started = perf_counter_ns()
            if handler is None:
                # one operational event per invocation, failures too
                _emit_telemetry(_telemetry_repo(arguments), name,
                                request_id,
                                perf_counter_ns() - started,
                                "unknown_tool", {})
                return _error(request_id, INVALID_PARAMS,
                              f"unknown tool: {name}")
            result = handler(arguments)
            payload: Any = {}
            if not result.get("isError"):
                try:
                    payload = json.loads(result["content"][0]["text"])
                except (KeyError, IndexError, TypeError, ValueError):
                    payload = {}
            _emit_telemetry(
                _telemetry_repo(arguments), name, request_id,
                perf_counter_ns() - started,
                "error" if result.get("isError") else "ok",
                _safe_metadata(payload))
            return _result(request_id, result)
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
    """Entry point (script or `python -m asha.mcp_server`)."""
    return serve()


if __name__ == "__main__":
    sys.exit(main())
