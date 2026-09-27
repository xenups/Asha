"""Worker-spec validation (H.2.1 extraction).

Natural home for structural worker validation: the contracts package
(validates the transport/spec shape, never executes).

Extracted verbatim from asha/scheduler.py validate_workers. The scheduler
re-exports it for backward compatibility; CLI/MCP consume it here.
"""

from __future__ import annotations

from typing import Any

from asha.runner import KNOWN_RUNNERS, runner_kind
from asha.types import OrchestratorError


def validate_workers(workers: Any) -> list[dict[str, Any]]:
    """Graph/spec validation before anything is created or dispatched."""
    if not isinstance(workers, list) or not workers:
        raise OrchestratorError("spec.workers must be a non-empty list")
    ids: set[str] = set()
    for raw in workers:
        if not isinstance(raw, dict):
            raise OrchestratorError("each worker must be a JSON object")
        wid = raw.get("id")
        if not isinstance(wid, str) or not wid:
            raise OrchestratorError("worker id must be a non-empty string")
        if wid in ids:
            raise OrchestratorError(f"duplicate worker id: {wid!r}")
        ids.add(wid)
        deps = raw.get("deps", [])
        if not isinstance(deps, list) or \
                not all(isinstance(dep, str) for dep in deps):
            raise OrchestratorError(
                f"{wid}.deps must be a list of worker ids")
        for key in ("reads", "writes"):
            value = raw.get(key)
            if value is not None and (
                    not isinstance(value, list)
                    or not all(isinstance(item, str) for item in value)):
                raise OrchestratorError(
                    f"{wid}.{key} must be a list of paths, or absent "
                    "to mean UNKNOWN (never silently an empty set)")
        kind = runner_kind(raw)
        cmd = raw.get("cmd")
        if cmd is not None and (not isinstance(cmd, list)
                                or not all(isinstance(part, str)
                                           for part in cmd)):
            raise OrchestratorError(
                f"{wid}.cmd must be a non-empty argv list")
        if kind == "command" and (not isinstance(cmd, list) or not cmd):
            raise OrchestratorError(
                f"{wid}.cmd must be a non-empty argv list")
        timeout = raw.get("timeout")
        if timeout is not None and (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or timeout <= 0):
            raise OrchestratorError(
                f"{wid}.timeout must be a positive number of seconds "
                "(int/float), or absent/None for the default budget")
        runner_cfg = raw.get("runner")
        if runner_cfg is not None:
            if not isinstance(runner_cfg, dict):
                raise OrchestratorError(
                    f"{wid}.runner must be an object with a type")
            rtype = runner_cfg.get("type")
            if not isinstance(rtype, str) or rtype not in KNOWN_RUNNERS:
                raise OrchestratorError(
                    f"{wid}: unknown runner type {rtype!r} "
                    f"(known: {sorted(KNOWN_RUNNERS)})")
        agent = raw.get("agent")
        if agent is not None and (not isinstance(agent, str)
                                  or not agent.strip()):
            raise OrchestratorError(
                f"{wid}.agent must be a non-empty string "
                f"(known: {sorted(KNOWN_RUNNERS)})")
        if kind not in KNOWN_RUNNERS:
            raise OrchestratorError(
                f"{wid}: unknown agent/runner {kind!r} "
                f"(known: {sorted(KNOWN_RUNNERS)})")
        prompt = raw.get("prompt")
        if prompt is not None and (not isinstance(prompt, str)
                                   or not prompt.strip()):
            raise OrchestratorError(
                f"{wid}.prompt must be a non-empty string when set")
        if kind == "antigravity" and prompt is None:
            raise OrchestratorError(
                f"{wid}: agent antigravity requires prompt")
        verify_command = raw.get("verify_command")
        if verify_command is not None and (
                not isinstance(verify_command, str)
                or not verify_command.strip()):
            raise OrchestratorError(
                f"{wid}.verify_command must be a non-empty string "
                "when set")
    for raw in workers:
        for dep in raw.get("deps", []) or []:
            if dep not in ids:
                raise OrchestratorError(
                    f'{raw["id"]}: unknown dependency {dep!r}')
    return list(workers)