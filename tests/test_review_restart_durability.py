"""Phase 9 -- Journal and Review artifacts must survive a real process restart.

Two separate ``python -m asha.mcp_server`` processes, one disposable state
directory (``ASHA_STATE_DIR``), real JSON-RPC over stdio, real Ladder lifecycle
and the real host-driven Ship Gate. Nothing here is mocked: process B is a
different OS process that can only see what process A persisted.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha.telemetry import read_events
from tests.ladder_support import make_repo

PY = sys.executable
CLEAN_SOURCE = ('def add(a, b):\n    return a + b\n\n\n'
                'def sub(a, b):\n    return a - b\n')


class Server:
    """One real MCP server process speaking stdio JSON-RPC."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        env = dict(os.environ)
        env["ASHA_STATE_DIR"] = str(state_dir)
        self.proc = subprocess.Popen(
            [PY, "-m", "asha.mcp_server"], cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self._next_id = 0
        self.pid = self.proc.pid
        self.initialize()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        frame = {"jsonrpc": "2.0", "id": self._next_id,
                 "method": method, "params": params}
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise AssertionError(f"server {self.pid} produced no response: "
                                 f"{stderr[-2000:]}")
        return json.loads(line)

    def initialize(self) -> dict[str, Any]:
        return self._request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "p9-restart", "version": "1"}})

    def tools(self) -> list[str]:
        return [tool["name"] for tool in
                self._request("tools/list", {})["result"]["tools"]]

    def tool(self, name: str, **arguments: Any) -> dict[str, Any]:
        response = self._request("tools/call",
                                 {"name": name, "arguments": arguments})
        result = response["result"]
        assert result["isError"] is False, result["content"][0]["text"]
        return json.loads(result["content"][0]["text"])

    def tool_error(self, name: str, **arguments: Any) -> str:
        response = self._request("tools/call",
                                 {"name": name, "arguments": arguments})
        result = response["result"]
        assert result["isError"] is True, result["content"][0]["text"]
        return result["content"][0]["text"]

    def stop(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)


def _run_review(server: Server, repo: Any) -> tuple[str, dict[str, Any]]:
    """Create a run and validate all three stages, the way a host would."""
    started = server.tool("asha_review", root=str(repo.root),
                          base=repo.base, head=repo.head)
    run_id = started["run"]["run_id"]
    packet = started["packet"]
    for _ in range(3):
        server.tool("asha_review_submit_stage", root=str(repo.root),
                    review_run_id=run_id, stage=packet["stage"],
                    stage_id=packet["stage_id"], verdict="APPROVE",
                    summary="read the diff; nothing blocking",
                    findings=[],
                    reviewer_metadata={"host_client": "p9-restart",
                                       "isolation": "SAME_HOST_SESSION",
                                       "attestation_source": "fresh context"})
        packet = server.tool("asha_review_next_stage", root=str(repo.root),
                             review_run_id=run_id)
    finalized = server.tool("asha_review_finalize", root=str(repo.root),
                            review_run_id=run_id)
    return run_id, finalized


def _journal_text(state_dir: Path, run_id: str) -> str:
    path = next(state_dir.glob(f"*/journal/{run_id}.jsonl"))
    return path.read_text(encoding="utf-8")


def _artifacts(state_dir: Path, run_id: str) -> dict[str, str]:
    run_dir = next(state_dir.glob(f"*/reviews/{run_id}"))
    return {
        str(path.relative_to(run_dir)): hashlib.sha256(
            path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*")) if path.is_file()
    }


@pytest.fixture(scope="module")
def restart_world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    tmp = tmp_path_factory.mktemp("p9-restart")
    # a genuinely correct change: the real Ship Gate must be able to authorize it
    repo = make_repo(tmp, changed=CLEAN_SOURCE)
    state = tmp / "state"
    state.mkdir()

    first = Server(state)
    try:
        tools = first.tools()
        assert "asha_review_status" in tools and "asha_review_finalize" in tools
        run_id, finalized = _run_review(first, repo)
        status_before = first.tool("asha_review_status", root=str(repo.root),
                                   run_id=run_id)
        pid_a = first.pid
    finally:
        first.stop()

    journal_before = _journal_text(state, run_id)
    artifacts_before = _artifacts(state, run_id)

    second = Server(state)
    try:
        pid_b = second.pid
        status_after = second.tool("asha_review_status", root=str(repo.root),
                                   run_id=run_id)
    finally:
        second.stop()

    journal_after = _journal_text(state, run_id)
    return {
        "repo": repo, "state": state, "run_id": run_id,
        "finalized": finalized, "status_before": status_before,
        "status_after": status_after, "journal_before": journal_before,
        "journal_after": journal_after,
        "artifacts_before": artifacts_before, "pid_a": pid_a, "pid_b": pid_b,
    }


def test_review_status_is_identical_after_a_real_restart(
        restart_world: dict[str, Any]) -> None:
    assert restart_world["pid_a"] != restart_world["pid_b"]
    before = restart_world["status_before"]
    after = restart_world["status_after"]

    # same run, same target, same decisions: compared structurally, not as text
    assert before["run_id"] == after["run_id"] == restart_world["run_id"]
    for key in ("schema", "artifact_schema", "state", "decision", "approved",
                "execution_mode", "reasons", "independence", "target",
                "findings", "gate", "artifacts", "created_at", "expires_at",
                "finalized_at"):
        assert before[key] == after[key], key
    assert [(s["stage"], s["stage_id"]) for s in before["stages"]] == \
        [(s["stage"], s["stage_id"]) for s in after["stages"]]
    for pair in zip(before["stages"], after["stages"], strict=True):
        for key in ("status", "verdict", "summary", "submission_sha256",
                    "artifact", "validated_at", "error"):
            assert pair[0][key] == pair[1][key], key
    # every mandated stage is validated, and the decision is the gate's
    assert all(stage["status"] == "VALIDATED" for stage in after["stages"])
    assert after["state"] == "FINALIZED"
    assert after["decision"] == "APPROVED" and after["approved"] is True
    assert after["gate"]["authorized_to_ship"] is True
    assert restart_world["finalized"]["decision"] == after["decision"]


def test_journal_and_artifacts_are_byte_identical_after_the_restart(
        restart_world: dict[str, Any]) -> None:
    before = restart_world["journal_before"]
    after = restart_world["journal_after"]
    assert before == after, "a fresh server rewrote the journal it did not own"

    events, integrity = read_events(after)
    assert events, "no journal events were persisted"
    assert integrity.incomplete is False and integrity.invalid_trailing is False
    assert [event["run_id"] for event in events] == \
        [restart_world["run_id"]] * len(events)
    assert len({event["event_id"] for event in events}) == len(events)
    assert all(event["event_id"] for event in events)
    # the lifecycle milestones really happened and stayed in order
    kinds = [event["event_type"] for event in events]
    assert kinds[0] == "review_started"
    assert kinds[-1] == "review_finalized"
    assert kinds.count("review_stage_validated") == 3
    assert kinds.count("ship_gate_evaluated") == 1
    assert kinds.index("review_stage_submitted") < \
        kinds.index("review_stage_validated") < \
        kinds.index("review_finalized")

    artifacts_after = _artifacts(restart_world["state"], restart_world["run_id"])
    assert artifacts_after == restart_world["artifacts_before"]
    assert "run.json" in artifacts_after
    assert any(name.startswith("stages/") for name in artifacts_after)


def test_a_corrupted_run_artifact_fails_closed_on_restart(
        restart_world: dict[str, Any], tmp_path: Path) -> None:
    state = tmp_path / "corrupt-state"
    shutil.copytree(restart_world["state"], state)
    run_dir = next(state.glob(f"*/reviews/{restart_world['run_id']}"))
    (run_dir / "run.json").write_text('{"schema": 2, "state": "COMPLET',
                                      encoding="utf-8")   # truncated

    server = Server(state)
    try:
        error = server.tool_error(
            "asha_review_status", root=str(restart_world["repo"].root),
            run_id=restart_world["run_id"])
    finally:
        server.stop()

    assert "COMPLET" not in error            # never reconstructed as approval
    # the corrupted copy is untouched by the reader (no silent repair)
    assert (run_dir / "run.json").read_text(encoding="utf-8").endswith("COMPLET")


def test_a_run_artifact_is_not_accepted_under_another_run_id(
        restart_world: dict[str, Any], tmp_path: Path) -> None:
    """Cross-run substitution: rename the directory, keep the sealed record."""
    state = tmp_path / "renamed-state"
    shutil.copytree(restart_world["state"], state)
    run_dir = next(state.glob(f"*/reviews/{restart_world['run_id']}"))
    renamed = run_dir.with_name("rv-20990101T000000Z-deadbeef")
    run_dir.rename(renamed)

    server = Server(state)
    try:
        error = server.tool_error(
            "asha_review_status", root=str(restart_world["repo"].root),
            run_id=renamed.name)
    finally:
        server.stop()

    assert "declares run" in error
    assert restart_world["run_id"] in error


def test_a_missing_review_artifact_does_not_reconstruct_approval(
        restart_world: dict[str, Any], tmp_path: Path) -> None:
    state = tmp_path / "missing-state"
    shutil.copytree(restart_world["state"], state)
    run_dir = next(state.glob(f"*/reviews/{restart_world['run_id']}"))
    shutil.rmtree(run_dir)

    server = Server(state)
    try:
        error = server.tool_error(
            "asha_review_status", root=str(restart_world["repo"].root),
            run_id=restart_world["run_id"])
    finally:
        server.stop()

    assert "not found" in error.lower() or "missing" in error.lower()
