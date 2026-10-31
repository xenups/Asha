"""`asha_review` / `asha_review_status` through the real JSON-RPC dispatch."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_support import (  # type: ignore[import-not-found]
    FakeProvider,
    make_repo,
    stub_gate,
)

from asha import mcp_server
from asha.ladder import orchestrator


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ASHA_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('ASHA_LLM_API_KEY', raising=False)


def call(name: str, **arguments: Any) -> dict[str, Any]:
    frame = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}
    response = mcp_server.handle_message(dict(frame))
    assert response is not None and "result" in response, response
    return response["result"]


def payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def test_tools_list_exposes_the_review_tools() -> None:
    response = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert {"asha_review", "asha_review_status"} <= names


def test_review_schema_takes_refs_not_commands() -> None:
    spec = {tool["name"]: tool for tool in mcp_server.TOOL_SPECS}
    schema = spec["asha_review"]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "root", "base", "head", "deadline_seconds", "policy"}
    assert "command" not in schema["properties"]


def test_review_returns_a_structured_decision(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    monkeypatch.setattr(orchestrator, "resolve_provider",
                        lambda: FakeProvider())

    body = payload(call("asha_review", root=str(repo.root), base=repo.base,
                        head=repo.head))

    assert body["decision"] == "APPROVED"
    assert body["run_id"].startswith("rv-")
    assert body["target"]["head_sha"] == repo.head
    assert [stage["stage"] for stage in body["stages"]] == [
        "CODE_REVIEW", "SENIOR_REVIEW", "SEMANTIC_REVIEW", "SHIP_GATE"]
    assert body["gate"]["authorized_to_ship"] is True


def test_review_without_provider_is_a_blocked_result(
        tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    body = payload(call("asha_review", root=str(repo.root), base=repo.base,
                        head=repo.head))

    assert body["decision"] == "BLOCKED"
    assert "ASHA_LLM_API_KEY" in body["blocking_reason"]


def test_status_reads_the_same_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    stub_gate(monkeypatch)
    monkeypatch.setattr(orchestrator, "resolve_provider",
                        lambda: FakeProvider())
    first = payload(call("asha_review", root=str(repo.root), base=repo.base,
                         head=repo.head))

    body = payload(call("asha_review_status", root=str(repo.root),
                        run_id=first["run_id"]))

    assert body["run_id"] == first["run_id"]
    assert body["decision"] == first["decision"]
    assert body["target"]["diff_sha256"] == first["target"]["diff_sha256"]


@pytest.mark.parametrize("arguments", [
    {"base": 5},
    {"base": ""},
    {"head": "-rf"},
    {"policy": 3},
    {"deadline_seconds": "soon"},
    {"deadline_seconds": 0},
    {"deadline_seconds": 86401},
])
def test_review_rejects_malformed_arguments(
        tmp_path: Path, arguments: dict[str, Any]) -> None:
    result = call("asha_review", root=str(tmp_path), **arguments)
    assert result["isError"] is True


def test_status_rejects_unknown_and_unsafe_run_ids(tmp_path: Path) -> None:
    assert call("asha_review_status", root=str(tmp_path),
                run_id="rv-missing")["isError"] is True
    assert call("asha_review_status", root=str(tmp_path),
                run_id="../escape")["isError"] is True
    assert call("asha_review_status", root=str(tmp_path),
                run_id="")["isError"] is True


def test_review_missing_required_status_argument(tmp_path: Path) -> None:
    result = call("asha_review_status", root=str(tmp_path))
    assert result["isError"] is True


def test_review_surfaces_a_git_error_as_tool_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    result = call("asha_review", root=str(repo.root), base="origin/nope",
                  head=repo.head)
    assert result["isError"] is True
    assert "cannot resolve ref" in result["content"][0]["text"]
