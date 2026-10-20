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

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from time import monotonic, perf_counter_ns
from typing import Any, TextIO

from . import (
    audit_snapshot,
    check_runner,
    dep_index,
    evidence,
    graph_state,
    scope_resolver,
    validation,
    worker_graph,
)
from .ast_indexer import ImportFact, ModuleIndex, index_module
from .classifier import classify_task, governance_profile
from .codegraph import build_graph, closure, sym_node
from .common import paths as common_paths
from .conflict import ConflictManager, covered, scope_status
from .context_slicer import slice_context
from .contracts.validation import validate_workers
from .governance.dag import run_workers_dag
from .governance.worker_execution import run_worker_in_worktree
from .router import RuntimeMode, route
from .types import OrchestratorError
from .worktree import _git

SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18",
                               "2025-03-26", "2024-11-05"]
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
SERVER_INFO = {"name": "asha-orchestrator", "version": "0.5.0"}
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
    """Append ONE event to external state mcp_live_telemetry.jsonl.

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
        jspace = common_paths.get_state_dir(repo)
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
ASHA_VERIFY_TIP_SPEC: dict[str, Any] = {
    "name": "asha_verify_tip",
    "description": ("In-place, fail-closed verification of the CURRENT "
                    "working-tree tip -- built for stacked-PR / "
                    "multi-branch rebase workflows where the agent owns "
                    "branch checkouts and rebases. Refuses dirty, "
                    "conflicted or mid-rebase trees; binds the tip to a "
                    "cryptographic snapshot id; optionally runs the "
                    "configured deterministic checks IN-PLACE (no "
                    "competing git worktree is ever created); seals a "
                    "digest-bound evidence artifact per tip. Never "
                    "grants ship authority."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "root": {"type": "string", "default": ".",
                     "description": ROOT_HELP},
            "base_ref": {"type": "string",
                         "description": "Optional ref the tip will be "
                                        "compared/pushed against; must "
                                        "resolve to a commit "
                                        "(fail-closed otherwise)"},
            "run_checks": {"type": "boolean", "default": False,
                           "description": "Run the configured "
                                          "deterministic check set "
                                          "in-place for the resolved "
                                          "scope (default: false)"},
            "declared_scope": {"type": "array",
                               "items": {"type": "string"},
                               "description": "Repo-relative files this "
                                              "tip declares as its "
                                              "scope (default: the "
                                              "scope resolver's "
                                              "changed-files set)"},
        },
        "required": ["root"],
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
    lock_file = common_paths.get_state_dir(root) / "lock"
    if not lock_file.is_file():
        return {"exists": False, "pid": None}
    raw = lock_file.read_text(encoding="utf-8", errors="replace").strip()
    try:
        return {"exists": True, "pid": int(raw)}
    except ValueError:
        return {"exists": True, "pid": None}


def _status_response(head: str, status_lines: list[str],
                     active: list[str], orphans: list[str],
                     lock: dict[str, Any]) -> str:
    """Deterministic one-line summary alongside the structured payload.

    Derived only from git facts (no clock, no random): identical repo
    states always render byte-identical text, so CLI and agent callers
    can rely on a non-empty `response` in every state (clean, dirty,
    detached HEAD, mid-rebase, orphaned worktrees) -- the empty-response
    friction recorded in the Detailist stacked-PR run.
    """
    tree = ("clean" if not status_lines
            else f"dirty ({len(status_lines)} path(s))")
    if lock.get("exists"):
        lock_state = ("held" if lock.get("pid") is not None
                      else "held (unreadable pid)")
    else:
        lock_state = "free"
    return (f"tip {head[:12]}: {tree}; "
            f"worktrees {len(active)} active, {len(orphans)} orphaned; "
            f"control lock {lock_state}")


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
        lock = _lock_state(resolved)
        payload = {"root": str(resolved),
                   "head": head,
                   "clean": status == "",
                   "status": status.splitlines(),
                   "worktrees": {"active": active, "orphaned": orphans},
                   "lock": lock}
        payload["response"] = _status_response(
            head, status.splitlines(), active, orphans, lock)
        return _tool_ok(payload)
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


# -- Phase 9: asha_verify_tip (in-place stacked-PR tip verification) ------
# Conflict-state porcelain prefixes (merge/rebase/cherry-pick collisions).
_CONFLICT_PREFIXES = ("UU", "AA", "DD", "AU", "UA", "DU", "UD")
# An in-flight git operation that must complete before the tip is
# verifiable. A MERGE_HEAD with resolved clean files is still refused:
# the operation has not produced its final tip yet.
_IN_FLIGHT_MARKERS = ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                      "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")


def _inspect_tip(root: Path, allow_dirty: bool = False
                 ) -> tuple[str | None, Path | None, str, str]:
    """Shared in-place tip inspection (Phase 10 reuse): clean-tree facts
    for asha_verify_tip AND in-place atomic dispatch. Returns
    (refusal, git_dir, head, tree); refusal is None on a clean tip, and
    (refusal, None, "", "") whenever git inspection itself failed.
    Order: unmerged paths -> dirty tree -> `git diff --check` defects ->
    in-flight git operation. Pure read-only inspection: never mutates.

    Phase 11.1 `allow_dirty=True` (in_place_atomic only): an uncommitted
    WIP tree is NOT a refusal -- the caller snapshots it first and rolls
    back to that snapshot instead of HEAD. Unmerged index entries and
    in-flight git operations still fail closed, and `git diff --check`
    defects are only tolerated while the tree is dirty (they belong to
    the caller's WIP, and the run seals WIP-bound evidence that grants
    no push authority)."""
    try:
        git_dir = Path(_git(root, "rev-parse", "--git-dir").strip())
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        status = _git(root, "status", "--porcelain", strip=False)
        # `git diff --check` exits NON-ZERO when it reports defects
        # (1 or 2 depending on git version), so it must not go
        # through the raising _git helper: 0 = clean, 1/2 = defect
        # lines on stdout; anything else (128/129 fatal/usage)
        # fails closed as an inspection error.
        diff_check_proc = subprocess.run(
            ["git", "diff", "--check"], cwd=root,
            capture_output=True, text=True, timeout=60)
        if diff_check_proc.returncode not in (0, 1, 2):
            message = ("git inspection failed: git diff --check exited "
                       f"{diff_check_proc.returncode}: "
                       f"{diff_check_proc.stderr.strip()[:200]}")
            return message, None, "", ""
        diff_check = diff_check_proc.stdout
        head = _git(root, "rev-parse", "HEAD").strip()
        tree = _git(root, "rev-parse", "HEAD^{tree}").strip()
    except OrchestratorError as exc:
        return (f"git inspection failed: {exc}", None, "", "")
    dirty = [line for line in status.splitlines() if line.strip()]
    if dirty:
        conflicted = [line for line in dirty
                      if line[:2] in _CONFLICT_PREFIXES]
        if conflicted:
            return ("TIP_CONFLICTED: "
                    f"{len(conflicted)} unmerged path(s): "
                    + ", ".join(line[3:].strip() for line in conflicted[:10]),
                    None, "", "")
        if not allow_dirty:
            return ("TIP_DIRTY: "
                    f"{len(dirty)} uncommitted path(s): "
                    + ", ".join(line[3:].strip() for line in dirty[:10]), None, "",
                    "")
    if diff_check.strip() and not (allow_dirty and dirty):
        return ("TIP_DIFF_UNCLEAN: git diff --check reported "
                "whitespace/conflict-marker defects: "
                + "; ".join(diff_check.splitlines()[:10]), None, "", "")
    for marker in _IN_FLIGHT_MARKERS:
        if (git_dir / marker).exists():
            message = ("TIP_REBASE_IN_PROGRESS: "
                       f"{marker} present; verify the tip after the git "
                       "operation completes")
            return message, None, "", ""
    return None, git_dir, head, tree


def asha_verify_tip(arguments: dict[str, Any]) -> dict[str, Any]:
    """asha_verify_tip: in-place, fail-closed verification of the
    CURRENT working-tree tip for stacked-PR / multi-branch rebase
    workflows. Never raises -- refusals are `isError: true` content with
    a stable TIP_* code; never creates a git worktree; never grants
    ship authority (`authorized_to_ship` is hard-coded false in both
    payload and sealed artifact).

    Order of refusals (most specific first): unmerged paths ->
    dirty tree -> `git diff --check` defects -> in-flight git
    operation -> unknown base_ref -> missing declared_scope path.
    A clean tip is bound to `snapshot_id_of(root)` and sealed to
    <state>/verify-tip/<head12>.evidence.json (digest-bound, atomic).
    """
    try:
        root = arguments.get("root", ".")
        if not isinstance(root, str):
            return _tool_error("root must be a string")
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            return _tool_error(
                f"root does not exist or is not a directory: {root}")
        resolved_root = root_path.resolve()
        base_ref = arguments.get("base_ref")
        if base_ref is not None and not isinstance(base_ref, str):
            return _tool_error("base_ref must be a string")
        run_checks = arguments.get("run_checks", False)
        if not isinstance(run_checks, bool):
            return _tool_error("run_checks must be a boolean")
        declared_scope = arguments.get("declared_scope")
        if declared_scope is not None:
            if (not isinstance(declared_scope, list)
                    or not all(isinstance(item, str) and item
                               for item in declared_scope)):
                return _tool_error(
                    "declared_scope must be a list of non-empty strings")
            declared_scope = list(declared_scope)
        if declared_scope is not None:
            # Phase 10 auto-scope: "@diff" / "@diff:<base_ref>" expand
            # deterministically from git facts; protected governance
            # paths (.github/, .jspace/) never enter implicitly, and an
            # empty/unresolvable diff fails closed as SCOPE_AUTO_EMPTY.
            scope_res = _resolve_auto(resolved_root, declared_scope)
            resolved_scope, _auto_changed, auto_error = scope_res
            _auto_deleted = getattr(scope_res, "deleted", [])
            if auto_error is not None:
                return _tool_error(auto_error)
            declared_scope = resolved_scope
        refusal, _git_dir, head, tree = _inspect_tip(resolved_root)
        if refusal is not None:
            return _tool_error(refusal)
        base_sha = None
        if base_ref:
            try:
                base_sha = _git(resolved_root, "rev-parse", "--verify",
                                base_ref + "^{commit}").strip()
            except OrchestratorError:
                return _tool_error(
                    "TIP_BASE_UNKNOWN: "
                    f"base_ref {base_ref!r} does not resolve to a commit")
        if declared_scope is not None:
            missing = [rel for rel in declared_scope
                       if not (resolved_root / rel).exists()]
            if missing:
                # Check if any missing paths are actually deleted in the diff
                # (they were tracked deletions, so they're valid in-scope)
                if '_auto_deleted' in locals() and _auto_deleted:
                    # Filter out paths that were legitimately deleted in the diff
                    missing = [rel for rel in missing if rel not in _auto_deleted]
                if missing:
                    return _tool_error(
                        "TIP_SCOPE_MISSING: declared_scope path(s) not in "
                        "the tip tree: " + ", ".join(missing))
        try:
            snapshot_id = audit_snapshot.snapshot_id_of(resolved_root)
        except Exception as exc:
            return _tool_error(
                f"snapshot failed: {type(exc).__name__}: {exc}")
        checks: list[dict[str, Any]] | None = None
        scope_level = "unresolved"
        if run_checks:
            try:
                scope = scope_resolver.resolve(resolved_root, base_ref,
                                               paths=declared_scope)
            except Exception as exc:
                return _tool_error(
                    f"scope resolution failed: "
                    f"{type(exc).__name__}: {exc}")
            scope_level = str(scope.get("scope"))
            checks = check_runner.run(resolved_root, scope)
        validation_state, validation_rule = validation.validation_status(
            checks or [])
        validation_policy = (validation.VALIDATION_POLICY_SCOPE_MATRIX
                             if run_checks
                             else validation.VALIDATION_POLICY_NONE)
        checks_ok = validation_state == validation.VALIDATION_PASS
        sealed = evidence.seal({
            "schema": evidence.SCHEMA_VALIDATED,
            "stage": "verify_tip",
            "scope": scope_level,
            "commit": head,
            "tree_hash": tree,
            "observed_at": evidence.now_iso(),
            "checks": checks if checks is not None else [],
            "kind": ("verification" if checks_ok
                     else "execution_attestation"),
            "validation": {"status": validation_state,
                           "policy": validation_policy,
                           "rule": validation_rule},
            "validation_status": validation_state,
            # a tip verification never grants ship authority; the ship
            # gate re-derives authorization from its own evidence chain.
            "authorized_to_ship": False,
            "snapshot_id": snapshot_id,
            "base_ref": base_ref,
            "base_sha": base_sha,
            "declared_scope": declared_scope,
            "run_checks": run_checks,
            "ok": checks_ok,
            "ready_to_push": checks_ok,
            "reason": (None if checks_ok
                       else "VALIDATION_" + validation_state),
            # Phase 11.2: track deleted files in scope
            "deleted_in_scope": _auto_deleted if '_auto_deleted' in locals() else [],
        })
        artifact = (common_paths.get_state_dir(resolved_root)
                    / "verify-tip" / f"{head[:12]}.evidence.json")
        try:
            evidence.write_to(artifact, sealed)
        except OSError as exc:
            return _tool_error(f"EVIDENCE_WRITE_FAILED: {exc}")
        if run_checks and not checks_ok:
            prefix = ("TIP_VALIDATION_FAILED"
                      if validation_state == validation.VALIDATION_FAIL
                      else "TIP_VALIDATION_BLOCKED")
            detail = "; ".join(
                str(entry.get("name")) + ": " + str(entry.get("status"))
                + " exit=" + str(entry.get("exit_code"))
                + (" " + str(entry.get("note")) if entry.get("note") else "")
                for entry in (checks or []))
            return _tool_error(
                prefix + ": " + validation_rule + "; " + detail
                + "; evidence sealed at " + str(artifact))
        if checks is None:
            check_summary = "checks not run"
        else:
            passed = sum(1 for entry in checks
                         if entry.get("status") == "passed")
            check_summary = f"checks {passed}/{len(checks)} passed"
        payload = {"root": str(resolved_root),
                   "head": head,
                   "tree_hash": tree,
                   "snapshot_id": snapshot_id,
                   "base_ref": base_ref,
                   "base_sha": base_sha,
                   "declared_scope": declared_scope,
                   "run_checks": run_checks,
                   "checks": checks,
                   "validation_status": validation_state,
                   "ok": checks_ok,
                   "state": "DONE",
                   "ready_to_push": checks_ok,
                   "reason": (None if checks_ok
                              else "VALIDATION_" + validation_state),
                   "authorized_to_ship": False,
                   "evidence_sha256": sealed["evidence_sha256"],
                   "evidence_path": str(artifact),
                   # Phase 11.2: include deleted files in scope
                   "deleted_in_scope": _auto_deleted if '_auto_deleted' in locals() else [],
                   "response": (
                       f"tip {head[:12]} verified in place: clean tree; "
                       f"snapshot {snapshot_id[:12]}; {check_summary}; "
                       f"evidence {sealed['evidence_sha256'][:12]} "
                       "sealed")}
        return _tool_ok(payload)
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


# -- Phase 10: asha_verify_stack (ordered multi-branch lineage check) ------

ASHA_VERIFY_STACK_SPEC: dict[str, Any] = {
    "name": "asha_verify_stack",
    "description": ("Verify an ordered stack of branches (bottom-to-top) "
                    "against a base ref, in place, with zero worktrees: "
                    "per-branch linear ancestry (merge-base == parent) "
                    "plus re-verification of the sealed verify-tip "
                    "evidence for every branch head. A non-linear link "
                    "returns the exact deterministic recovery recipe "
                    "`git rebase --onto <parent_sha> <merge_base> <B>`; "
                    "missing/tampered evidence is reported per branch, "
                    "never ignored. `stack_healthy` is true only when "
                    "EVERY branch is linear AND sealed. Validation "
                    "refusals: STACK_EMPTY, BRANCH_NOT_FOUND, "
                    "BASE_REF_NOT_FOUND. Report codes: "
                    "STACK_ANCESTRY_DRIFT, STACK_EVIDENCE_MISSING, "
                    "STACK_EVIDENCE_TAMPERED. Never grants ship "
                    "authority."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "branches": {"type": "array",
                         "items": {"type": "string"},
                         "minItems": 1,
                         "description": "Branch names ordered "
                                        "bottom-to-top (stack[0] sits "
                                        "on base_ref)."},
            "base_ref": {"type": ["string", "null"],
                         "description": "Anchor for the bottom branch "
                                        "(e.g. origin/dev, origin/main). "
                                        "Required: an unanchored bottom "
                                        "is never assumed healthy."},
            "root": {"type": "string", "default": ".",
                     "description": ROOT_HELP},
        },
        "required": ["branches"],
        "additionalProperties": False,
    },
}


def _artifact_evidence_state(sealed: object, head_sha: str,
                            tree_sha: str) -> str:
    """Classify one sealed artifact for stack consumption.

    A valid digest proves integrity of the serialized payload only; truth of
    the claims inside it is established by the schema, the artifact kind, and
    the derived validation status -- never by the digest. Anything that is not
    unambiguous validation evidence is refused, and legacy schema-1 artifacts
    are named as legacy rather than silently re-interpreted.
    """
    if not isinstance(sealed, dict):
        return "tampered"
    if evidence.compute_digest(sealed) != sealed.get("evidence_sha256"):
        return "tampered"
    if sealed.get("schema") != evidence.SCHEMA_VALIDATED:
        return "legacy"
    if sealed.get("kind") != "verification":
        return "unvalidated"
    if sealed.get("validation_status") != validation.VALIDATION_PASS:
        return "unvalidated"
    if sealed.get("commit") != head_sha:
        return "mismatch"
    if sealed.get("tree_hash") != tree_sha:
        return "mismatch"
    if sealed.get("ready_to_push") is not True:
        return "unready"
    return "verified"


def _tip_evidence_state(state_dir: Path, head_sha: str,
                        tree_sha: str) -> str:
    """Canonical verify-tip evidence that fully validates this exact commit.

    Dispatch evidence is only consulted when the canonical tip artifact is
    absent, and it must satisfy the same rules (so an execution attestation
    can never be mistaken for validation).
    """
    canonical = state_dir / "verify-tip" / f"{head_sha[:12]}.evidence.json"
    if canonical.is_file():
        try:
            return _artifact_evidence_state(
                json.loads(canonical.read_text(encoding="utf-8")),
                head_sha, tree_sha)
        except (OSError, ValueError):
            return "tampered"
    fallback_dir = state_dir / "dispatch-in-place"
    states: list[str] = []
    if fallback_dir.is_dir():
        for path in sorted(fallback_dir.glob("*.evidence.json"),
                           key=lambda item: item.stat().st_mtime,
                           reverse=True):
            try:
                states.append(_artifact_evidence_state(
                    json.loads(path.read_text(encoding="utf-8")),
                    head_sha, tree_sha))
            except (OSError, ValueError):
                continue
    for candidate in ("verified", "tampered", "legacy", "unvalidated",
                      "mismatch", "unready"):
        if candidate in states:
            return candidate
    return "missing"


def asha_verify_stack(arguments: dict[str, Any]) -> dict[str, Any]:
    """asha_verify_stack: read-only, deterministic stack lineage report.
    Never raises; never mutates the repo; never spawns a worktree.
    Validation refusals (STACK_EMPTY / BRANCH_NOT_FOUND /
    BASE_REF_NOT_FOUND) are isError content; ancestry-drift and
    evidence findings live INSIDE the ok-report as per-branch status
    codes with stack_healthy=false."""
    try:
        unknown = sorted(set(arguments) - {"branches", "base_ref", "root"})
        if unknown:
            return _tool_error("unknown field(s): " + ", ".join(unknown))
        root = arguments.get("root", ".")
        if not isinstance(root, str):
            return _tool_error("root must be a string")
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            return _tool_error(
                f"root does not exist or is not a directory: {root}")
        resolved_root = root_path.resolve()
        branches = arguments.get("branches")
        if (not isinstance(branches, list) or not branches
                or not all(isinstance(item, str) and item
                           for item in branches)):
            return _tool_error(
                "STACK_EMPTY: branches must be a non-empty ordered list "
                "of branch names (bottom-to-top)")
        branches = list(branches)
        base_ref = arguments.get("base_ref")
        if base_ref is None:
            return _tool_error(
                "BASE_REF_NOT_FOUND: base_ref is required to anchor the "
                "stack bottom (e.g. origin/dev, origin/main); an "
                "unanchored stack is never assumed healthy")
        if not isinstance(base_ref, str) or not base_ref:
            return _tool_error(
                "base_ref must be a non-empty string (or omitted/null "
                "only to trigger the BASE_REF_NOT_FOUND refusal)")

        def _resolve(ref: str) -> str | None:
            try:
                return _git(resolved_root, "rev-parse", "--verify",
                            ref + "^{commit}").strip()
            except OrchestratorError:
                return None

        base_sha = _resolve(base_ref)
        if base_sha is None:
            return _tool_error(
                f"BASE_REF_NOT_FOUND: {base_ref!r} does not resolve to "
                "a commit")
        missing = [ref for ref in branches if _resolve(ref) is None]
        if missing:
            return _tool_error(
                "BRANCH_NOT_FOUND: ref(s) do not resolve to a commit: "
                + ", ".join(missing))

        state_dir = common_paths.get_state_dir(resolved_root)
        rows: list[dict[str, Any]] = []
        for index, branch in enumerate(branches):
            parent_ref = base_ref if index == 0 else branches[index - 1]
            head_sha = _resolve(branch)
            parent_sha = _resolve(parent_ref)
            assert head_sha is not None and parent_sha is not None
            try:
                merge_base = _git(resolved_root, "merge-base", branch,
                                  parent_ref).strip()
            except OrchestratorError:
                merge_base = None
            is_linear = merge_base == parent_sha
            recipe = None
            if not is_linear and merge_base is not None:
                recipe = (f"git rebase --onto {parent_sha} "
                          f"{merge_base} {branch}")
            head_tree = _git(resolved_root, "rev-parse",
                             head_sha + "^{tree}").strip()
            evidence_state = _tip_evidence_state(state_dir, head_sha,
                                                 head_tree)
            if not is_linear:
                status = "STACK_ANCESTRY_DRIFT"
            elif evidence_state == "missing":
                status = "STACK_EVIDENCE_MISSING"
            elif evidence_state == "tampered":
                status = "STACK_EVIDENCE_TAMPERED"
            elif evidence_state == "legacy":
                status = "STACK_EVIDENCE_LEGACY"
            elif evidence_state == "unvalidated":
                status = "STACK_EVIDENCE_UNVALIDATED"
            elif evidence_state == "mismatch":
                status = "STACK_EVIDENCE_MISMATCH"
            elif evidence_state == "unready":
                status = "STACK_EVIDENCE_UNREADY"
            else:
                status = None
            rows.append({
                "branch": branch,
                "parent_ref": parent_ref,
                "head_sha": head_sha,
                "parent_sha": parent_sha,
                "merge_base": merge_base,
                "is_linear": is_linear,
                "rebase_recipe": recipe,
                "evidence": evidence_state,
                "evidence_verified": evidence_state == "verified",
                "status": status,
            })
        linear_count = sum(1 for row in rows if row["is_linear"])
        sealed_count = sum(1 for row in rows
                           if row["evidence"] == "verified")
        stack_healthy = all(row["is_linear"]
                            and row["evidence"] == "verified"
                            for row in rows)
        total = len(rows)
        if stack_healthy:
            response = (f"stack {total} branch(es): {total}/{total} "
                        f"linear with sealed evidence -- healthy")
        else:
            codes = sorted({row["status"] for row in rows
                            if row["status"]})
            response = (f"stack {total} branch(es): {linear_count}/"
                        f"{total} linear, {sealed_count}/{total} sealed "
                        f"-- UNHEALTHY: {', '.join(codes)}")
        payload = {
            "root": str(resolved_root),
            "base_ref": base_ref,
            "base_sha": base_sha,
            "stack_healthy": stack_healthy,
            "branches": rows,
            "response": response,
        }
        return _tool_ok(payload)
    except ValueError as exc:
        return _tool_error(str(exc))
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
        try:
            report = run_workers_dag(Path(plan["root"]), workers,
                                     task_id=task_id)
        except Exception as exc:
            # Refused to start or died catastrophically: report the real
            # state, never a fabricated success (partial = partial).
            return _tool_error(
                f"orchestration aborted: {type(exc).__name__}: {exc}")

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
            "deferrals": list(report.get("deferral_events") or []),
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
            "targets": {"type": "array",
                        "items": {"type": "object",
                                  "properties": {
                                      "target_file": {"type": "string"},
                                      "target_symbol": {"type": "string"},
                                  },
                                  "required": ["target_file",
                                               "target_symbol"],
                                  "additionalProperties": False},
                        "description": "Phase 10 batch mode: slice many "
                                       "symbols with ONE AST index "
                                       "build. Either give these OR the "
                                       "single target_file/target_symbol "
                                       "(never both); single mode is "
                                       "byte-identical to before."},
        },
        # Either (target_file + target_symbol) or `targets`; the handler
        # enforces exactly one of the two forms (fail-closed on mixing).
        "required": [],
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
            "execution_mode": {"type": "string",
                               "enum": ["worktree", "in_place_atomic"],
                               "default": "worktree",
                               "description": "Phase 10: 'worktree' "
                                              "(default, existing "
                                              "behavior) isolates the "
                                              "worker in a detached "
                                              "git worktree; "
                                              "'in_place_atomic' runs "
                                              "cmd on the CURRENT branch "
                                              "tip inside the caller's "
                                              "tree. Phase 11.1: an "
                                              "uncommitted WIP tree is "
                                              "allowed -- its exact "
                                              "state is snapshotted and "
                                              "ANY failure (scope "
                                              "violation, cmd failure, "
                                              "timeout) restores that "
                                              "snapshot, never HEAD; a "
                                              "pass on a dirty tree "
                                              "returns ready_to_push: "
                                              "false + reason "
                                              "WIP_UNCOMMITTED_PASS. "
                                              "Unmerged/in-flight git "
                                              "states still refuse."},
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
    """Single-start convenience wrapper over _index_from."""
    return _index_from(root, (start_rel,))


def _index_from(root: Path, starts: tuple[str, ...],
                collect_errors: list[dict[str, Any]] | None = None
                ) -> tuple[ModuleIndex, ...]:
    """Index every start file plus every repo module reachable through
    their import facts (lexically, ONE shared BFS visited-set, excluded
    dirs skipped) -- the AST index is built exactly once per call even
    for batch surgical-context targets. Bounded by the repo's own
    import graph."""
    visited: set[str] = set()
    ordered: list[ModuleIndex] = []
    queue: list[str] = list(starts)
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
        try:
            index = index_module(module, source)
        except (ValueError, SyntaxError) as exc:
            line = getattr(exc, 'lineno', 0) or 0
            msg = getattr(exc, 'msg', str(exc)) or str(exc)
            if collect_errors is not None:
                collect_errors.append({
                    "file": rel,
                    "msg": msg,
                    "line": line,
                })
            continue
        ordered.append(index)
        for fact in index.imports:
            queue.extend(_import_candidates(root, module, fact))
    ordered.sort(key=lambda entry: entry.module)
    return tuple(ordered)


# Phase 10 -- git-diff auto-scope. Protected governance paths are never
# UNLOCKED implicitly by a "@diff" expansion: they enter scope only via
# an explicit declaration (which scope rules still evaluate).
_AUTO_PROTECTED = (".github/", ".jspace/")


def _is_auto(entry: str) -> bool:
    """True for the auto-scope sentinels "@diff" and "@diff:<base_ref>"."""
    return entry == "@diff" or entry.startswith("@diff:")


def _auto_diff_files(root: Path, token: str) -> tuple[list[str], list[str], str | None]:
    """Deterministic expansion of one auto-scope token to repo-relative
    files: `git diff --name-only <base>...HEAD`. Bare "@diff" anchors on
    the first tracked main/dev ref (origin/dev, origin/main, dev, main)
    via merge-base semantics, falling back to HEAD~1; "@diff:<ref>"
    requires that ref to resolve. Anything unresolvable or empty fails
    closed as SCOPE_AUTO_EMPTY -- never an empty scope that silently
    authorizes nothing-or-everything.

    Returns (files, deleted_files, error) where deleted_files are paths
    that were deleted in the diff (git diff --diff-filter=D)."""
    if token == "@diff":
        base = None
        for candidate in ("origin/dev", "origin/main", "dev", "main"):
            try:
                _git(root, "rev-parse", "--verify", candidate + "^{commit}")
                base = candidate
                break
            except OrchestratorError:
                continue
        if base is None:
            try:
                _git(root, "rev-parse", "--verify", "HEAD~1^{commit}")
                base = "HEAD~1"
            except OrchestratorError:
                return [], [], ("SCOPE_AUTO_EMPTY: no base ref resolves "
                            "(no tracked main/dev ref and HEAD has no "
                            "parent commit)")
    else:
        base = token.partition(":")[2].strip()
        if not base:
            return [], [], ("SCOPE_AUTO_EMPTY: empty base_ref in "
                        "@diff:<base_ref>")
        try:
            _git(root, "rev-parse", "--verify", base + "^{commit}")
        except OrchestratorError:
            return [], [], (f"SCOPE_AUTO_EMPTY: base_ref {base!r} does not "
                        "resolve to a commit")
    try:
        output = _git(root, "diff", "--name-only", f"{base}...HEAD")
    except OrchestratorError as exc:
        return [], [], f"SCOPE_AUTO_EMPTY: git diff failed: {exc}"
    files = sorted({line.strip().removeprefix("./")
                    for line in output.splitlines() if line.strip()})
    # Get deleted files from the diff (--diff-filter=D) and working tree
    deleted_list: list[str] = []
    try:
        deleted_output = _git(root, "diff", "--name-only", "--diff-filter=D", f"{base}...HEAD")
        deleted_list.extend(line.strip().removeprefix("./")
                            for line in deleted_output.splitlines() if line.strip())
    except OrchestratorError:
        pass
    try:
        st_out = _git(root, "status", "--porcelain", strip=False)
        for line in st_out.splitlines():
            if not line.strip():
                continue
            if "D" in line[:2]:
                p = line[3:].strip().removeprefix("./")
                if " -> " in p:
                    p = p.split(" -> ")[0]
                deleted_list.append(p)
    except OrchestratorError:
        pass
    deleted = sorted(set(deleted_list))
    # Phase 11.1: the auto-scope must describe the working TREE, not just
    # the committed branch diff -- an in-place run on a dirty tip (the
    # inner TDD loop) would otherwise report every WIP file as a scope
    # violation. Uncommitted/untracked paths join the expansion; protected
    # governance paths are still filtered out by _resolve_auto.
    try:
        dirty = _observed_paths(root)
    except Exception:
        dirty = []
    files = sorted(set(files) | set(dirty))
    if not files:
        return [], [], (f"SCOPE_AUTO_EMPTY: diff {base}...HEAD and the working "
                    "tree contain zero files")
    return files, deleted, None


class AutoScopeResult(tuple):
    """3-tuple (resolved, changed, error) with a .deleted attribute.
    Preserves backwards compatibility for Phase 10/11 callers unpacking 3 values
    while providing Phase 11.2 deleted file tracking."""
    deleted: list[str]

    def __new__(cls, resolved: list[str] | None, changed: list[str],
                error: str | None, deleted: list[str] | None = None):
        instance = super().__new__(cls, (resolved, changed, error))
        instance.deleted = list(deleted or ())
        return instance


def _resolve_auto(root: Path, entries: list[str] | None
                  ) -> AutoScopeResult:
    """(resolved_entries, all_changed_files, error) with .deleted attribute.

    Every "@diff" / "@diff:<base_ref>" entry expands to the changed
    files; explicitly listed entries pass through unchanged (only
    auto-expanded protected paths are filtered out). Zero or unresolvable
    expansion -> (None, [], SCOPE_AUTO_EMPTY...). Non-auto input lists
    return verbatim with no git call."""
    if entries is None:
        return AutoScopeResult(None, [], None, [])
    if not any(_is_auto(entry) for entry in entries):
        return AutoScopeResult(list(entries), [], None, [])
    changed: list[str] = []
    deleted: list[str] = []
    for token in (entry for entry in entries if _is_auto(entry)):
        files, deleted_files, error = _auto_diff_files(root, token)
        if error is not None:
            return AutoScopeResult(None, [], error, [])
        changed.extend(files)
        deleted.extend(deleted_files)
    others = [entry for entry in entries if not _is_auto(entry)]
    auto_kept = [rel for rel in sorted(set(changed))
                 if not rel.startswith(_AUTO_PROTECTED)]
    resolved = sorted(set(auto_kept) | set(others))
    if not resolved:
        return AutoScopeResult(
            None, sorted(set(changed)),
            ("SCOPE_AUTO_EMPTY: diff resolves to zero in-scope files "
             "(all changed paths are protected governance paths under "
             ".github/ or .jspace/)"),
            sorted(set(deleted)))
    return AutoScopeResult(resolved, sorted(set(changed)), None, sorted(set(deleted)))


def _module_rel(root: Path, module: str) -> str | None:
    """'pkg.base' -> repo-relative file ('pkg/base.py' or
    'pkg/base/__init__.py'); None when the module has no file in root."""
    parts = module.split(".")  # dotted module name: pkg.base -> pkg/base
    if not parts or any(not part for part in parts):
        return None
    for candidate in ("/".join(parts) + ".py",
                      "/".join(parts) + "/__init__.py"):
        if (root / candidate).is_file():
            return candidate
    return None


def _expand_reads(root: Path, changed: list[str],
                  base_reads: list[str] | None,
                  collect_errors: list[dict[str, Any]] | None = None) -> list[str]:
    """Read-set expansion for auto-scope: every changed file plus its
    AST dependency closure (the SAME _index_from BFS as the surgical
    pipeline) -- deterministic, no guessing, existing reads unioned in.

    Returns expanded_reads list. If collect_errors is provided, AST syntax
    errors are recorded into it."""
    closure: set[str] = set(changed)
    errs: list[dict[str, Any]] = collect_errors if collect_errors is not None else []
    for rel in changed:
        if not rel.endswith(".py") or not (root / rel).is_file():
            continue
        indices = _index_from(root, (rel,), collect_errors=errs)
        for index in indices:
            found = _module_rel(root, index.module)
            if found is not None:
                closure.add(found)
    return sorted(closure | set(base_reads or ()))


def asha_get_surgical_context(arguments: dict[str, Any]
                              ) -> dict[str, Any]:
    """Read-only Phase 4.1 pipeline. Never raises: every failure
    becomes structured error content (tool-level fail-closed)."""
    try:
        unknown = sorted(set(arguments) - {"target_file", "target_symbol",
                                           "repo_path", "targets"})
        if unknown:
            return _tool_error("unknown field(s): " + ", ".join(unknown))
        target_file = arguments.get("target_file")
        target_symbol = arguments.get("target_symbol")
        targets = arguments.get("targets")
        batch = targets is not None
        pairs: list[tuple[str, str]] = []
        if batch:
            # Phase 10 batch mode: mixing single + batch fails closed as
            # ambiguous; one call slices MANY symbols with ONE index.
            if target_file is not None or target_symbol is not None:
                return _tool_error(
                    "give either single target_file/target_symbol or "
                    "batch targets, not both")
            if not isinstance(targets, list) or not targets:
                return _tool_error(
                    "targets must be a non-empty list of "
                    "{target_file, target_symbol} objects")
            for position, item in enumerate(targets):
                if (not isinstance(item, dict)
                        or set(item) != {"target_file", "target_symbol"}
                        or not isinstance(item.get("target_file"), str)
                        or not item.get("target_file")
                        or not isinstance(item.get("target_symbol"), str)
                        or not item.get("target_symbol")):
                    return _tool_error(
                        f"targets[{position}] must be exactly "
                        "{target_file: str, target_symbol: str}")
                pairs.append((item["target_file"], item["target_symbol"]))
        else:
            if not isinstance(target_file, str) or not target_file:
                return _tool_error("target_file must be a non-empty string")
            if not isinstance(target_symbol, str) or not target_symbol:
                return _tool_error(
                    "target_symbol must be a non-empty string")
            pairs.append((target_file, target_symbol))
        repo_arg = arguments.get("repo_path", ".")
        if not isinstance(repo_arg, str):
            return _tool_error("repo_path must be a string")
        repo = Path(repo_arg).expanduser()
        if not repo.is_dir():
            return _tool_error(
                f"repo_path does not exist or is not a directory: {repo_arg}")
        root = repo.resolve()
        rels: list[str] = []
        for item_file, _item_symbol in pairs:
            rel = item_file.replace(chr(92), "/")
            if rel.startswith("/") or ".." in rel.split("/"):
                return _tool_error(
                    f"target_file must stay inside the repository: "
                    f"{item_file}")
            if not rel.endswith(".py"):
                return _tool_error("target_file must be a .py file")
            if not (root / rel).is_file():
                return _tool_error(f"target_file not found: {item_file}")
            rels.append(rel)

        # Phase 4.1 pipeline: ONE index build + ONE graph for every
        # target (shared BFS visited-set), then closure -> slice per
        # target. Single mode is the batch-of-one path, so the payload
        # shape stays byte-identical to the Phase 4.3 contract.
        syntax_errors: list[dict[str, Any]] = []
        indices = _index_from(root, tuple(dict.fromkeys(rels)),
                              collect_errors=syntax_errors)
        if syntax_errors:
            first_err = syntax_errors[0]
            err_msg = f"AST_SYNTAX_ERROR: {first_err['file']}: {first_err['msg']}"
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "state": "FAILED",
                        "ok": False,
                        "error": err_msg,
                        "refusal_code": "AST_SYNTAX_ERROR",
                        "response": err_msg,
                        "ast_syntax_errors": syntax_errors,
                    })
                }],
                "isError": True,
            }
        graph = build_graph(indices)
        results: list[dict[str, Any]] = []
        for (item_file, item_symbol), rel in zip(pairs, rels):
            module = _module_name(rel)
            if module is None:
                return _tool_error(
                    f"cannot derive module name: {item_file}")
            target_index = next((entry for entry in indices
                                 if entry.module == module), None)
            if (target_index is None
                    or target_index.symbol(item_symbol) is None):
                return _tool_error(
                    f"symbol not found in target file: {item_symbol}")
            source = (root / rel).read_text(encoding="utf-8",
                                            errors="replace")
            root_node = sym_node(module, item_symbol)
            reach = closure(graph, (root_node,), module)
            sliced = slice_context(source, target_name=item_symbol,
                                   target_module=module, graph=graph,
                                   indices=indices)
            context_text = (sliced.target_source
                            + "".join(stub.text for stub in sliced.stubs))
            full_bytes = sliced.full_bytes
            context_bytes = sliced.surgical_bytes
            ratio = (1.0 - context_bytes / full_bytes) if full_bytes else 0.0
            results.append({
                "target": f"{rel}:{item_symbol}",
                "context": context_text,
                # every closure member except the root, sorted;
                # UNRESOLVED boundaries stay in `unresolved`
                # (reported, never dropped)
                "dependencies": [node for node in reach.reachable
                                 if node != root_node],
                "unresolved": list(reach.unresolved),
                "full_source_bytes": full_bytes,
                "context_source_bytes": context_bytes,
                "reduction_ratio": ratio,
            })
        if batch:
            return _tool_ok({"count": len(results), "results": results})
        return _tool_ok(results[0])
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
    external orchestrator state -- real recorded surfaces, sorted by
    id, never caller-supplied. Absence of records stays UNKNOWN; a
    record that is unreadable, unsealed or tampered with enters as a
    None-surface peer so classification fails closed to UNKNOWN."""
    cache = common_paths.get_orchestrator_dir(root)
    peers: dict[str, dict[str, Any]] = {}
    if not cache.is_dir():
        return ()
    # records live one level deeper: cache/orchestrator/<task>/<wid>.json
    for path in sorted(cache.rglob("*.json")):
        if path.name.endswith(".baseline.json"):
            continue  # Ignore Phase 1 baseline artifacts
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


# Phase 10 -- atomic in-place execution ------------------------------------
# ponytail: dispatch schema carries no per-task timeout input, so in-place
# cmd runs are bounded by this fixed ceiling; add a `timeout` schema field
# when callers genuinely need per-task limits.
_IN_PLACE_TIMEOUT_S = 600


def _observed_paths(root: Path) -> list[str]:
    """Working-tree paths modified/created/deleted since the last commit
    (porcelain v1 -z: byte-exact, no quoting). Untracked directories are
    expanded to their files; renames report BOTH endpoints."""
    proc = subprocess.run(["git", "status", "--porcelain=v1", "-z"],
                          cwd=root, capture_output=True, text=True,
                          timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git status failed: {proc.stderr.strip()[:200]}")
    tokens = [token for token in proc.stdout.split(chr(0)) if token]
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        rel = entry[3:]  # "XY path": two status chars, one space, path
        paths.append(rel)
        if "R" in entry[:2] or "C" in entry[:2]:
            if index + 1 < len(tokens):
                paths.append(tokens[index + 1])  # rename/copy counterpart
            index += 2
            continue
        index += 1
    expanded: list[str] = []
    for rel in paths:
        target = root / rel
        if rel.endswith("/") or (target.is_dir() and target.exists()):
            for child in sorted(target.rglob("*")):
                if child.is_file():
                    expanded.append(child.relative_to(root).as_posix())
        else:
            expanded.append(rel)
    return sorted(dict.fromkeys(expanded))


def _rollback(root: Path, pre_head: str) -> dict[str, Any]:
    """Fail-closed atomic rollback for a run that STARTED from a clean
    tree: restore the exact pre-execution state (the clean-tree
    precondition guarantees no pre-existing untracked files, so `git
    clean -fd` can only remove files the run created)."""
    reset = subprocess.run(["git", "reset", "--hard", pre_head],
                           cwd=root, capture_output=True, text=True,
                           timeout=120)
    clean = subprocess.run(["git", "clean", "-fd"], cwd=root,
                           capture_output=True, text=True, timeout=120)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=60)
    restored = (reset.returncode == 0 and clean.returncode == 0
                and not status.stdout.strip())
    return {"mode": "head_reset", "reset": reset.returncode == 0,
            "clean": clean.returncode == 0,
            "restored": restored}


# Phase 11.1 -- snapshot-safe inner loop. Sentinel for a path that was
# ABSENT in the pre-execution snapshot (a `cmd` that created it must be
# rolled back by deleting it again).
_WIP_ABSENT = "-"


def _porcelain_lines(root: Path) -> list[str]:
    """Sorted non-empty `git status --porcelain` lines (the byte-level
    identity of a working-tree state)."""
    return sorted(line for line in _git(root, "status", "--porcelain")
                  .splitlines() if line.strip())


def _blob_of(root: Path, rel: str) -> str:
    """Write `rel`'s exact bytes into the object store and return the
    blob sha (no EOL translation, no index involvement)."""
    proc = subprocess.run(["git", "hash-object", "-w", "--", rel],
                          cwd=root, capture_output=True, text=True,
                          timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git hash-object failed for {rel}: "
                           f"{proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def _snapshot_pre_state(root: Path) -> dict[str, Any]:
    """Exact pre-execution working-tree snapshot: HEAD, the index tree,
    every tracked path that differs from HEAD (its blob, or the ABSENT
    sentinel for a WIP deletion), every untracked non-ignored file, and
    the porcelain status set. Deterministic and read-only worktree-wise
    -- `git hash-object -w` only appends objects, so a rollback can put
    the caller's WIP back byte-for-byte instead of resetting to HEAD."""
    head = _git(root, "rev-parse", "HEAD").strip()
    index_tree = _git(root, "write-tree").strip()
    status = _porcelain_lines(root)
    tracked: dict[str, str] = {}
    changed = _git(root, "diff", "--name-only", "--no-renames", "HEAD")
    for rel in sorted({line.strip() for line in changed.splitlines()
                       if line.strip()}):
        tracked[rel] = (_blob_of(root, rel) if (root / rel).is_file()
                        else _WIP_ABSENT)
    others = _git(root, "ls-files", "--others", "--exclude-standard")
    untracked = {rel: _blob_of(root, rel)
                 for rel in sorted({line.strip() for line in
                                    others.splitlines() if line.strip()})}
    digest = hashlib.sha256("\n".join(
        [f"head {head}", f"index {index_tree}"]
        + [f"tracked {rel} {blob}" for rel, blob in tracked.items()]
        + [f"untracked {rel} {blob}" for rel, blob in untracked.items()]
        + status).encode("utf-8")).hexdigest()
    return {"clean": not status, "head": head, "index_tree": index_tree,
            "tracked": tracked, "untracked": untracked, "status": status,
            "sha256": digest,
            "tree_sha": _worktree_tree_sha(root, index_tree)}


def _worktree_tree_sha(root: Path, index_tree: str) -> str:
    """Tree sha of the working tree's tracked content: `git stash create`
    builds the commit object without touching the worktree or the index.
    An untracked-only change yields no tracked delta, so the index tree
    is the honest fallback (the snapshot digest still covers it)."""
    proc = subprocess.run(["git", "stash", "create"], cwd=root,
                          capture_output=True, text=True, timeout=120)
    commit = proc.stdout.strip() if proc.returncode == 0 else ""
    if not commit:
        return index_tree
    tree = subprocess.run(["git", "rev-parse", f"{commit}^{{tree}}"],
                          cwd=root, capture_output=True, text=True,
                          timeout=60)
    return (tree.stdout.strip() if tree.returncode == 0
            and tree.stdout.strip() else index_tree)


def _prune_empty_parents(root: Path, rel: str) -> None:
    """Remove directories left empty by a rollback (never the root)."""
    parent = (root / rel).parent
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _restore_snapshot(root: Path, snap: dict[str, Any]) -> dict[str, Any]:
    """Roll a dirty-tree run back to the caller's WIP snapshot: delete
    only the paths `cmd` created, write the recorded blobs back (tracked
    edits -- including WIP deletions -- and untracked files), restore the
    index tree, then verify the porcelain status set matches the snapshot
    exactly. A mismatch is REPORTED (fail-closed), never assumed away."""
    failures: list[str] = []
    restore = {**snap["tracked"], **snap["untracked"]}
    try:
        observed = _observed_paths(root)
    except Exception as exc:            # inspection failure stays visible
        return {"mode": "snapshot", "restored": False,
                "detail": f"{type(exc).__name__}: {exc}", "failures": []}
    for rel in observed:
        if rel in restore:
            continue
        target = root / rel
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError as exc:
            failures.append(f"remove {rel}: {exc}")
        else:
            _prune_empty_parents(root, rel)
    for rel, blob in restore.items():
        target = root / rel
        try:
            if blob == _WIP_ABSENT:
                if target.exists():
                    target.unlink()
                continue
            content = subprocess.run(["git", "cat-file", "blob", blob],
                                     cwd=root, capture_output=True,
                                     timeout=120)
            if content.returncode != 0:
                failures.append(f"restore {rel}: blob unreadable")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(content.stdout)
        except OSError as exc:
            failures.append(f"restore {rel}: {exc}")
    read_tree = subprocess.run(["git", "read-tree", snap["index_tree"]],
                               cwd=root, capture_output=True, text=True,
                               timeout=120)
    if read_tree.returncode != 0:
        failures.append(f"index restore: {read_tree.stderr.strip()[:150]}")
    try:
        status_match = _porcelain_lines(root) == snap["status"]
    except Exception:
        status_match = False
    return {"mode": "snapshot", "restored": not failures and status_match,
            "status_match": status_match, "failures": failures[:5],
            "snapshot_sha256": snap["sha256"]}


def _run_in_place(root: Path, worker: dict[str, Any],
                  task_id: str) -> dict[str, Any]:
    """Phase 10 `in_place_atomic` execution: run cmd on the CURRENT
    branch tip, verify every observed change stays strictly inside
    declared_scope, and roll back to `pre_head` on ANY failure (cmd
    non-zero, timeout, scope violation) with zero unsealed residue.
    Success seals digest-bound evidence with authorized_to_ship false --
    execution evidence is never ship authority.

    Phase 11.1: a dirty WIP tree is allowed (the inner TDD loop must be
    able to call Asha without committing first). The exact pre-run
    working-tree state is snapshotted first -- tracked edits, the index
    tree and untracked files -- and a failing/out-of-scope run restores
    THAT snapshot instead of `git reset --hard HEAD`, so the caller's
    uncommitted work survives. A passing run on a dirty tree seals a
    WIP-bound snapshot record with ready_to_push false and reason
    WIP_UNCOMMITTED_PASS: tests passed, the tip is still uncommitted."""
    declared = [entry for entry in (worker.get("declared_scope") or [])
                if isinstance(entry, str)]
    if not declared:
        return {"refusal": "SCOPE_VIOLATION: in_place_atomic requires a "
                           "non-empty declared_scope"}
    refusal, _git_dir, pre_head, _pre_tree = _inspect_tip(root,
                                                          allow_dirty=True)
    if refusal is not None:
        # fail-closed preconditions (conflicts, in-flight git ops, an
        # unclean-but-clean-tree tip), re-checked right before execution
        # (closes the TOCTOU window between dispatch validation and run)
        return {"refusal": refusal}
    snapshot = _snapshot_pre_state(root)
    cmd = [str(entry) for entry in worker["cmd"]]
    declared_validation = [list(entry)
                           for entry in (worker.get("validation") or [])]
    exec_started_at = evidence.now_iso()
    exec_started = monotonic()
    proc: subprocess.CompletedProcess[str] | None = None
    cmd_failure: str | None = None
    exit_code: int | None = None
    termination = validation.EXECUTION_EXITED
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True,
                              text=True, timeout=_IN_PLACE_TIMEOUT_S)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        termination = validation.EXECUTION_TIMED_OUT
        cmd_failure = (f"CMD_TIMEOUT: exceeded "
                       f"{_IN_PLACE_TIMEOUT_S}s")
    except OSError as exc:
        termination = validation.EXECUTION_LAUNCH_FAILED
        cmd_failure = f"CMD_FAILED: {type(exc).__name__}: {exc}"
    # Bound evidence to what the process actually did. A zero exit here is
    # evidence about the command only -- never about validation.
    execution = validation.execution_record(
        cmd, ".",
        started_at=exec_started_at,
        duration_ms=int((monotonic() - exec_started) * 1000),
        exit_code=exit_code,
        termination=termination,
        stdout=(proc.stdout or "") if proc is not None else "",
        stderr=(proc.stderr or "") if proc is not None else "",
        known_policy_command=False)
    try:
        observed = _observed_paths(root)
    except Exception as exc:
        observed = []
        if cmd_failure is None:
            cmd_failure = (f"INSPECTION_FAILED: "
                           f"{type(exc).__name__}: {exc}")
    reason: str | None = None
    detail = ""
    if cmd_failure is not None:
        reason = ("CMD_TIMEOUT" if "CMD_TIMEOUT" in cmd_failure
                  else "CMD_FAILED")
        detail = cmd_failure
    elif exit_code != 0:
        reason = "CMD_FAILED"
        stderr_tail = (proc.stderr.strip()[-300:]
                       if proc is not None and proc.stderr else "")
        detail = (f"cmd exited {exit_code}"
                  + (f": {stderr_tail}" if stderr_tail else ""))
    else:
        uncovered = [rel for rel in observed
                     if not any(covered(rel, entry)
                                for entry in declared)]
        if uncovered:
            reason = "SCOPE_VIOLATION"
            detail = ("observed path(s) outside declared_scope: "
                      + ", ".join(uncovered[:10]))
    if reason is not None:
        rollback = (_rollback(root, pre_head) if snapshot["clean"]
                    else _restore_snapshot(root, snapshot))
        return {"state": "FAILED", "reason": reason, "detail": detail,
                "exit_code": exit_code, "observed": observed,
                "rollback": rollback, "authorized_to_ship": False,
                "execution": execution,
                "validation_status": validation.VALIDATION_BLOCKED,
                "validation_result": "FAIL", "ready_to_push": False,
                "wip": not snapshot["clean"], "evidence": {}}
    wip = not snapshot["clean"]
    if declared_validation:
        checks = validation.run_validation_commands(declared_validation, root)
        validation_policy = validation.VALIDATION_POLICY_EXPLICIT
    else:
        checks = []
        validation_policy = validation.VALIDATION_POLICY_NONE
    validation_state, validation_rule = validation.validation_status(checks)
    ready_to_push, ready_reason = validation.push_readiness(
        execution=execution, validation=validation_state,
        clean_tree=not wip, evidence_verified=True)
    head_after = _git(root, "rev-parse", "HEAD").strip()
    tree_after = (_worktree_tree_sha(root, snapshot["index_tree"]) if wip
                  else _git(root, "rev-parse", "HEAD^{tree}").strip())
    try:
        snapshot_id = audit_snapshot.snapshot_id_of(root)
    except Exception as exc:
        rollback = (_rollback(root, pre_head) if snapshot["clean"]
                    else _restore_snapshot(root, snapshot))
        return {"state": "FAILED",
                "reason": "SNAPSHOT_FAILED",
                "detail": f"{type(exc).__name__}: {exc}",
                "exit_code": exit_code, "observed": observed,
                "rollback": rollback, "authorized_to_ship": False,
                "validation_result": "FAIL", "ready_to_push": False,
                "wip": wip, "evidence": {}}
    try:
        in_place_scope = str(scope_resolver.resolve(
            root, None, paths=declared).get("scope"))
    except Exception:
        in_place_scope = "unresolved"  # fail-closed: never assume S0
    seal_started = monotonic()
    sealed = evidence.seal({
        "schema": evidence.SCHEMA_VALIDATED,
        "stage": "dispatch_in_place_atomic",
        "scope": in_place_scope,
        "commit": head_after,
        "tree_hash": tree_after,
        "observed_at": evidence.now_iso(),
        "kind": ("verification"
                 if validation_state == validation.VALIDATION_PASS
                 else "execution_attestation"),
        "execution": execution,
        "checks": checks,
        "validation": {"status": validation_state,
                       "policy": validation_policy,
                       "rule": validation_rule},
        "validation_status": validation_state,
        "ok": ready_to_push,
        "task_id": task_id,
        "worker_id": worker.get("id"),
        "declared_scope": declared,
        "observed": observed,
        "snapshot_id": snapshot_id,
        # Phase 11.1: the exact pre-run working-tree identity (HEAD,
        # index tree, WIP blobs, porcelain status) -- a WIP-pass seal is
        # bound to it and never grants push authority.
        "pre_state_sha256": snapshot["sha256"],
        "wip_uncommitted": wip,
        "ready_to_push": ready_to_push,
        "reason": ready_reason if not ready_to_push else None,
        "execution_mode": "in_place_atomic",
        # Phase 10 invariant: in-place dispatch never grants ship
        # authority; the ship gate re-derives it from its own chain.
        "authorized_to_ship": False,
    })
    artifact = (common_paths.get_state_dir(root)
                / "dispatch-in-place" / f"{task_id}.evidence.json")
    try:
        evidence.write_to(artifact, sealed)
        # Phase 11.2: canonical commit-bound tip evidence
        if not wip and head_after:
            tip_artifact = (common_paths.get_state_dir(root)
                            / "verify-tip" / f"{head_after[:12]}.evidence.json")
            try:
                tip_sealed = evidence.seal({
                    "schema": evidence.SCHEMA_VALIDATED,
                    "stage": "verify_tip",
                    "scope": in_place_scope,
                    "commit": head_after,
                    "tree_hash": tree_after,
                    "observed_at": evidence.now_iso(),
                    # An execution attestation describes how the command
                    # ran; it is never represented as a check result.
                    "kind": ("verification"
                             if validation_state == validation.VALIDATION_PASS
                             else "execution_attestation"),
                    "execution": execution,
                    "checks": checks,
                    "validation": {"status": validation_state,
                                   "policy": validation_policy,
                                   "rule": validation_rule},
                    "validation_status": validation_state,
                    "authorized_to_ship": False,
                    "snapshot_id": snapshot_id,
                    "declared_scope": declared,
                    "run_checks": bool(declared_validation),
                    "ok": validation_state == validation.VALIDATION_PASS,
                    "ready_to_push": (
                        validation_state == validation.VALIDATION_PASS),
                    "reason": (None if validation_state
                               == validation.VALIDATION_PASS
                               else "VALIDATION_" + validation_state),
                })
                evidence.write_to(tip_artifact, tip_sealed)
            except OSError:
                pass
    except OSError as exc:
        rollback = (_rollback(root, pre_head) if snapshot["clean"]
                    else _restore_snapshot(root, snapshot))
        return {"state": "FAILED", "reason": "EVIDENCE_WRITE_FAILED",
                "detail": str(exc), "exit_code": exit_code,
                "observed": observed, "rollback": rollback,
                "authorized_to_ship": False, "validation_result": "FAIL",
                "ready_to_push": False, "wip": wip, "evidence": {}}
    evidence_ms = int((monotonic() - seal_started) * 1000)
    return {"state": "DONE", "exit_code": 0, "observed": observed,
            "head": head_after, "tree_hash": tree_after,
            "snapshot_id": snapshot_id,
            "execution": execution,
            "checks": checks,
            "execution_ms": execution["duration_ms"],
            "evidence_ms": evidence_ms,
            "validation_status": validation_state,
            "validation_rule": validation_rule,
            "validation_result": (
                "PASS" if validation_state == validation.VALIDATION_PASS
                else "FAIL"),
            "ready_to_push": ready_to_push,
            "reason": ready_reason if not ready_to_push else None,
            "wip": wip,
            "evidence": {"path": str(artifact),
                         "evidence_sha256": sealed["evidence_sha256"]},
            "authorized_to_ship": False}


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
            "validation",
            "prompt", "execution_mode", "root"})
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
        # Explicit validation policy: each entry is a command that MUST run
        # and pass before the run can be push-ready. Omitting it leaves the
        # run unvalidated (NOT_CONFIGURED), never implicitly passing.
        validation_commands, error = _str_list(arguments, "validation")
        if error:
            return _tool_error(error)
        prompt = arguments.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            return _tool_error("prompt must be a string")
        # prompt is metadata only: never read again, never executed,
        # never telemetry'd.
        execution_mode = arguments.get("execution_mode", "worktree")
        if execution_mode not in ("worktree", "in_place_atomic"):
            return _tool_error(
                "execution_mode must be 'worktree' (default) or "
                "'in_place_atomic'")
        root_arg = arguments.get("root", ".")
        if not isinstance(root_arg, str):
            return _tool_error("root must be a string")
        root_path = Path(root_arg).expanduser()
        if not root_path.is_dir():
            return _tool_error(
                f"root does not exist or is not a directory: {root_arg}")
        root = root_path.resolve()

        # Phase 10 auto-scope: "@diff" / "@diff:<base_ref>" entries in
        # declared_scope/reads/writes expand deterministically before
        # classification, reads grow by the AST dependency closure of
        # the changed files, and an empty/unresolvable diff fails closed
        # as SCOPE_AUTO_EMPTY with NOTHING executed.
        # In-place atomic refuses a conflicted/in-flight tree IMMEDIATELY
        # -- before auto-scope expansion, classification or execution.
        # Phase 11.1: an uncommitted WIP tree is allowed (snapshot +
        # snapshot rollback inside _run_in_place); conflicts stay
        # fail-closed and must never be masked by a downstream parse
        # error on conflict-marker text.
        if execution_mode == "in_place_atomic":
            tip_refusal, _git_dir, _head, _tree = _inspect_tip(
                root, allow_dirty=True)
            if tip_refusal is not None:
                return _tool_error(tip_refusal)

        auto_engaged = (any(_is_auto(entry) for entry in declared)
                        or any(_is_auto(entry) for entry in (reads or ()))
                        or any(_is_auto(entry)
                               for entry in (writes or ())))
        all_changed: list[str] = []
        all_deleted: list[str] = []
        for key, value in (("declared_scope", declared),
                           ("reads", reads), ("writes", writes)):
            if value is None:
                continue
            scope_res = _resolve_auto(root, value)
            resolved_entries, changed, auto_error = scope_res
            deleted = getattr(scope_res, "deleted", [])
            if auto_error is not None:
                return _tool_error(auto_error)
            all_changed.extend(changed)
            all_deleted.extend(deleted)
            if resolved_entries is not None:
                if key == "declared_scope":
                    declared = resolved_entries
                elif key == "reads":
                    reads = resolved_entries
                else:
                    writes = resolved_entries
        if auto_engaged:
            _ast_syntax_errors: list[dict[str, Any]] = []
            reads = _expand_reads(root, sorted(set(all_changed)), reads,
                                  collect_errors=_ast_syntax_errors)
            # Phase 11.2: syntax errors from AST indexing are recorded but
            # do not fail the dispatch (allow in_place_atomic to proceed)
            if not declared:
                return _tool_error(
                    "SCOPE_AUTO_EMPTY: auto-scope resolved to an empty "
                    "declared_scope")

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
            "reads": reads, "writes": writes, "cmd": [str(x) for x in cmd],
            "validation": [shlex.split(entry) for entry in
                           (validation_commands or [])]}
        # single authorization point: the fast flag is the ROUTER's
        # own decision (env AND proven-disjoint-coherent), never raw input
        sched_fast = decision.mode is RuntimeMode.FAST_PATH
        task_id = "mcp-" + os.urandom(6).hex()
        run_started = perf_counter_ns()
        try:
            if execution_mode == "in_place_atomic":
                report = _run_in_place(root, worker, task_id)
            else:
                report = run_worker_in_worktree(
                        root, worker, task_id=task_id, fast_path=sched_fast,
                        fast_path_classification=decision.classification
                        or "UNKNOWN")
        except Exception as exc:
            return _tool_error(
                f"orchestration aborted: {type(exc).__name__}: {exc}")
        if report.get("refusal"):
            # clean-tree precondition re-failed at execution time:
            # immediate refusal, nothing executed, nothing written
            return _tool_error(str(report["refusal"]))
        scheduler_ms = (perf_counter_ns() - run_started) / 1e6
        states: dict[str, Any] = report.get("states") or {}
        worker_state = ((states.get(wid) or {}).get("state")
                        or report.get("state"))
        total_ms = (perf_counter_ns() - run_started) / 1e6
        payload = {
            "task_id": task_id,
            "state": worker_state,
            "execution_mode": execution_mode,
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
                # Measured on the in-place path; None (not 0.0) when the
                # execution mode does not instrument it, so a fake zero
                # can never be read as "instantaneous".
                "execution_ms": report.get("execution_ms"),
                "evidence_ms": report.get("evidence_ms"),
                "total_ms": round(total_ms, 3),
            },
            # Merge law preserved verbatim: dispatch grants no ship
            # authority of its own.
            "authorized_to_ship": False,
        }
        # Phase 11.1 evidence semantics: a passing run on a clean tip is
        # commit-bound and pushable; a passing run on a dirty (WIP) tip
        # seals a snapshot record with ready_to_push false +
        # reason WIP_UNCOMMITTED_PASS -- tests passed, the commit is
        # still missing.
        # A DONE worker means the command ran; it is never evidence that
        # validation passed. Default to FAIL unless the runner said PASS.
        payload["validation_result"] = report.get("validation_result") or "FAIL"
        payload["ready_to_push"] = bool(report.get("ready_to_push"))
        if isinstance(report.get("execution"), dict):
            payload["execution"] = report["execution"]
        payload["validation_status"] = (
            report.get("validation_status")
            or validation.VALIDATION_NOT_CONFIGURED)
        for key in ("head", "tree_hash", "snapshot_id", "wip", "checks"):
            if report.get(key) is not None:
                payload[key] = report[key]
        if report.get("state") == "FAILED":
            payload["detail"] = report.get("detail")
            payload["exit_code"] = report.get("exit_code")
            payload["observed"] = report.get("observed") or []
            payload["rollback"] = report.get("rollback")
        if report.get("reason"):
            payload["reason"] = report.get("reason")
        return _tool_ok(payload)
    except ValueError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


# -- Phase 11: unified `asha` auto-routing facade -------------------------
ASHA_SPEC: dict[str, Any] = {
    "name": "asha",
    "description": (
        "#1 recommended entrypoint: one tool, deterministic auto-routing "
        "over your inputs (strict precedence cmd > branches > target > "
        "zero-config, never guessing). `cmd` executes atomically in "
        "place with automatic \"@diff\" scope (committed diff + "
        "uncommitted WIP) and snapshot-safe rollback on failure -- the "
        "inner TDD loop keeps its uncommitted work; `branches` verifies "
        "an ordered branch stack; `target` "
        "(\"file.py:Symbol\" selectors, single/list/comma-separated) "
        "extracts surgical AST context; with only `root` it returns the "
        "deterministic status summary and -- when the tree is clean -- "
        "seals pre-push tip evidence automatically (a dirty tree "
        "reports ready_to_push: false (TIP_DIRTY) without failing). "
        "All seven specialized tools (asha_status, asha_verify_tip, "
        "asha_verify_stack, asha_dispatch_task, "
        "asha_get_surgical_context, asha_plan_dag, asha_run_spec) stay "
        "fully available for direct use."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "root": {"type": "string", "default": ".",
                     "description": "repository path ('.' = cwd)"},
            "cmd": {"anyOf": [{"type": "string"},
                              {"type": "array",
                               "items": {"type": "string"}}],
                    "description": "command to execute atomically in "
                                   "place (string or argv list) with "
                                   "automatic \"@diff\" scope"},
            "target": {"anyOf": [{"type": "string"},
                                 {"type": "array",
                                  "items": {"type": "string"}}],
                       "description": "'file.py:Symbol' selector(s): "
                                      "single string, list, or "
                                      "comma-separated"},
            "branches": {"type": "array", "items": {"type": "string"},
                         "description": "ordered (bottom-to-top) branch "
                                        "stack to verify"},
            "base_ref": {"type": "string",
                         "description": "optional base ref (e.g. "
                                        "origin/dev or origin/main)"},
            "run_checks": {"type": "boolean", "default": False,
                           "description": "run configured checks during "
                                          "zero-config tip verification"},
            "validation": {"type": "array", "items": {"type": "string"},
                           "description": "explicit validation commands; "
                                          "each must run and pass before the "
                                          "run is push-ready. Omitting them "
                                          "leaves the run NOT_CONFIGURED."},
        },
        "additionalProperties": False,
    },
}


def _payload_text(result: dict[str, Any]) -> dict[str, Any]:
    """Parse a tool result's JSON text content (fail-closed: a
    non-JSON body becomes an explicit raw marker, never an exception)."""
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    try:
        payload = json.loads(text)
    except ValueError:
        return {"raw": text}
    return payload if isinstance(payload, dict) else {"raw": text}


def _unified_diff_token(base_ref: str | None) -> str:
    """Auto-scope token for the unified facade: \"@diff\" anchored on
    the tracked base, or \"@diff:<base_ref>\" when one is given."""
    return "@diff" if not base_ref else f"@diff:{base_ref}"


def _parse_unified_target(raw: Any) -> list[dict[str, str]] | str:
    """'file.py:Symbol' selector grammar: single string, list of
    strings, or comma-separated string -> batch {target_file,
    target_symbol} objects. Anything not in `file:symbol` form fails
    closed as TARGET_FORMAT_INVALID (never a guess)."""
    if isinstance(raw, str):
        items = [piece for piece in raw.split(",")]
    elif isinstance(raw, list) and all(isinstance(item, str) and item
                                       for item in raw):
        items = [piece for item in raw for piece in item.split(",")]
    else:
        return ("TARGET_FORMAT_INVALID: target must be a string or a "
                "list of 'file.py:Symbol' strings")
    pairs: list[dict[str, str]] = []
    for item in items:
        piece = item.strip()
        if not piece:
            continue
        file_part, sep, symbol = piece.partition(":")
        if not sep or not file_part.strip() or not symbol.strip():
            return (f"TARGET_FORMAT_INVALID: {item!r} is not in "
                    "'file.py:Symbol' form")
        pairs.append({"target_file": file_part.strip(),
                      "target_symbol": symbol.strip()})
    if not pairs:
        return ("TARGET_FORMAT_INVALID: no target selectors after "
                "parsing")
    return pairs


def asha_asha(arguments: dict[str, Any]) -> dict[str, Any]:
    """Phase 11 unified facade: deterministic intent auto-routing
    (strict order cmd -> branches -> target -> zero-config) that
    delegates to the SAME specialized handlers -- one code path, no
    duplicated logic. Never raises; never guesses; `authorized_to_ship`
    stays false everywhere (delegation contract preserved)."""
    try:
        unknown = sorted(set(arguments) - {
            "root", "cmd", "target", "branches", "base_ref",
            "run_checks", "validation"})
        if unknown:
            return _tool_error("unknown field(s): " + ", ".join(unknown))
        root = arguments.get("root", ".")
        if not isinstance(root, str):
            return _tool_error("root must be a string")
        base_ref = arguments.get("base_ref")
        if base_ref is not None and not isinstance(base_ref, str):
            return _tool_error("base_ref must be a string")
        run_checks = arguments.get("run_checks", False)
        if not isinstance(run_checks, bool):
            return _tool_error("run_checks must be a boolean")
        validation_commands = arguments.get("validation")
        if validation_commands is not None and (
                not isinstance(validation_commands, list)
                or not all(isinstance(item, str) and item
                           for item in validation_commands)):
            return _tool_error("validation must be a list of commands")
        cmd = arguments.get("cmd")
        if cmd is not None:
            if isinstance(cmd, str):
                argv_list = [cmd] if cmd.strip() else []
            elif isinstance(cmd, list) and all(
                    isinstance(item, str) for item in cmd):
                argv_list = [item for item in cmd if item]
            else:
                return _tool_error(
                    "cmd must be a string or a list of strings")
            if not argv_list:
                return _tool_error(
                    "cmd must be a non-empty string or list of strings")
            return asha_dispatch_task({
                "root": root, "id": "asha-unified", "cmd": argv_list,
                "declared_scope": [_unified_diff_token(base_ref)],
                "validation": validation_commands or [],
                "execution_mode": "in_place_atomic"})
        branches = arguments.get("branches")
        if branches is not None:
            if (not isinstance(branches, list) or not branches
                    or not all(isinstance(item, str) and item
                               for item in branches)):
                return _tool_error(
                    "branches must be a non-empty list of strings")
            return asha_verify_stack({
                "root": root, "branches": list(branches),
                "base_ref": base_ref})
        target = arguments.get("target")
        if target is not None:
            pairs = _parse_unified_target(target)
            if isinstance(pairs, str):
                return _tool_error(pairs)
            return asha_get_surgical_context({"repo_path": root,
                                              "targets": pairs})
        # Zero-config default: ALWAYS the deterministic status summary;
        # a clean tree additionally seals pre-push tip evidence, a
        # dirty tree reports readiness without failing status reads.
        status_result = asha_status({"root": root})
        if status_result.get("isError"):
            return status_result
        status = _payload_text(status_result)
        if not status.get("clean"):
            changed = list(status.get("status") or [])
            status["ready_to_push"] = False
            status["refusal"] = "TIP_DIRTY"
            status["response"] = (
                status.get("response", "")
                + f"; ready_to_push=false (TIP_DIRTY): {len(changed)} "
                "changed path(s); commit or stash before tip sealing")
            return _tool_ok(status)
        tip = asha_verify_tip({
            "root": root, "base_ref": base_ref,
            "run_checks": run_checks,
            "declared_scope": [_unified_diff_token(base_ref)]})
        payload = _payload_text(tip)
        payload["status"] = {key: status.get(key)
                             for key in ("root", "head", "clean",
                                         "worktrees", "lock")}
        if tip.get("isError"):
            payload["ready_to_push"] = False
            return _tool_error(json.dumps(payload, sort_keys=True))
        # Readiness is decided by the tip evidence (validation status +
        # clean tree), never asserted by the facade.
        payload["ready_to_push"] = bool(payload.get("ready_to_push"))
        if payload["ready_to_push"]:
            payload["response"] = (
                f"ready to push: {payload.get('response', '')}")
        else:
            payload["response"] = (
                "not ready to push ("
                + str(payload.get("reason") or "VALIDATION_NOT_CONFIGURED")
                + "): " + str(payload.get("response", "")))
        return _tool_ok(payload)
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


TOOL_SPECS: list[dict[str, Any]] = [
    ASHA_SPEC, ASHA_PLAN_DAG_SPEC, ASHA_RUN_SPEC_SPEC, ASHA_STATUS_SPEC,
    ASHA_GET_SURGICAL_CONTEXT_SPEC, ASHA_DISPATCH_TASK_SPEC,
    ASHA_VERIFY_TIP_SPEC, ASHA_VERIFY_STACK_SPEC]


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "asha": asha_asha,
    "asha_plan_dag": asha_plan_dag,
    "asha_run_spec": asha_run_spec,
    "asha_status": asha_status,
    "asha_get_surgical_context": asha_get_surgical_context,
    "asha_dispatch_task": asha_dispatch_task,
    "asha_verify_tip": asha_verify_tip,
    "asha_verify_stack": asha_verify_stack,
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


_CLI_USAGE = (
    "usage: asha-mcp                 serve stdio JSON-RPC (default)\n"
    "       asha-mcp TOOL [k=v ...] [PATH]\n"
    "                               one-shot tool call; a bare PATH maps\n"
    "                               to root=PATH when root is not given\n"
    "       asha-mcp PATH           bare existing directory routes to the\n"
    "                               unified 'asha' zero-config tool\n"
    "exit:  0 tool ok / 1 tool refused (isError) / 2 usage error")


def _coerce_scalar(value: str) -> Any:
    """One-shot CLI value coercion: only unambiguous booleans convert;
    everything else stays a string (schemas carry no numeric args)."""
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def _array_keys(tool_name: str | None) -> set[str]:
    """Schema array-typed fields for a tool: in one-shot CLI form those
    arrive comma-separated (`branches=s1,s2,s3` -> ["s1","s2","s3"]),
    since `key=value` strings are the only value grammar."""
    if not tool_name:
        return set()
    for spec in TOOL_SPECS:
        if spec.get("name") == tool_name:
            properties = spec.get("inputSchema", {}).get("properties", {})
            return {key for key, value in properties.items()
                    if isinstance(value, dict)
                    and value.get("type") == "array"}
    return set()


def _parse_tool_argv(tokens: list[str],
                     tool_name: str | None = None) -> dict[str, Any]:
    """One-shot argument grammar: `key=value` pairs plus ONE optional
    bare positional path mapped to `root` (the exact friction point
    logged in the Detailist run: a bare positional raised ValueError in
    every ad-hoc driver). Ambiguity fails closed with a usage error
    instead of guessing. Array-typed fields split on commas."""
    arrays = _array_keys(tool_name)
    arguments: dict[str, Any] = {}
    positionals: list[str] = []
    for token in tokens:
        if "=" in token:
            key, _, value = token.partition("=")
            if not key:
                raise ValueError(f"invalid argument {token!r}: empty key")
            if key in arguments:
                raise ValueError(f"duplicate argument: {key}")
            if key in arrays:
                arguments[key] = ([] if not value
                                  else value.split(","))
            else:
                arguments[key] = _coerce_scalar(value)
        else:
            positionals.append(token)
    if positionals:
        if "root" in arguments:
            raise ValueError(
                "ambiguous repository: bare positional path combined "
                "with root=...; give exactly one")
        if len(positionals) > 1:
            raise ValueError("unexpected extra positional token(s): "
                             + " ".join(positionals[1:]))
        arguments["root"] = positionals[0]
    return arguments


def main(argv: list[str] | None = None) -> int:
    """Entry point (script or `python -m asha.mcp_server`).

    No arguments: stdio JSON-RPC server (unchanged transport contract).
    `TOOL [k=v ...] [PATH]`: one-shot tool call -- the tool's text
    content prints on stdout as ONE line; exit 0 (ok), 1 (tool refused,
    isError), 2 (usage error: unknown tool, malformed/ambiguous
    arguments)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return serve()
    if argv[0] in ("-h", "--help"):
        print(_CLI_USAGE)
        return 0
    name = argv[0]
    bare_path_root: str | None = None
    if name not in TOOL_HANDLERS:
        # Phase 11: a bare existing directory routes to the unified
        # `asha` tool (zero-config status + tip sealing). A directory
        # is a deterministic filesystem fact, never a guess; any other
        # unknown token keeps the exact usage-error contract.
        if os.path.isdir(os.path.expanduser(name)):
            name = "asha"
            bare_path_root = argv[0]
        else:
            print(f"mcp: unknown tool: {name}", file=sys.stderr)
            print(_CLI_USAGE, file=sys.stderr)
            return 2
    try:
        arguments = _parse_tool_argv(argv[1:], name)
        if bare_path_root is not None:
            if "root" in arguments:
                raise ValueError(
                    "ambiguous repository: bare positional path "
                    "combined with root=...; give exactly one")
            arguments["root"] = bare_path_root
    except ValueError as exc:
        print(f"mcp: {exc}", file=sys.stderr)
        print(_CLI_USAGE, file=sys.stderr)
        return 2
    result = TOOL_HANDLERS[name](arguments)
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    print(text)
    return 1 if result.get("isError") else 0


if __name__ == "__main__":
    sys.exit(main())
