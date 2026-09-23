"""Phase 5.1 TDD: MCP stdio handshake + protocol version negotiation.

Red first: `orchestrator.mcp_server` does not exist yet, so collection
dies with ModuleNotFoundError (the pre-implementation failure proof).

Contract under test:
- stdout carries JSON-RPC frames only; warnings go to stderr
- initialize echoes a SUPPORTED requested version verbatim (any of
  them, newest or oldest -- no silent upgrade), falls back to
  DEFAULT_PROTOCOL_VERSION on garbage/missing input with a one-line
  stderr warning, and never fails the handshake
- notifications produce no reply frame; ping -> {}; tools/list -> []
- bad frames become structured JSON-RPC errors; the loop survives
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".hermes" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import orchestrator.mcp_server as server  # RED: ModuleNotFoundError pre-impl


def test_initialize_echoes_supported_version() -> None:
    stderr = io.StringIO()
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}}, stderr)
    assert response is not None
    result: dict[str, Any] = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"] == {"name": "asha-orchestrator",
                                    "version": "0.1.0"}
    assert result["capabilities"] == {"tools": {}}
    assert response["id"] == 1
    assert stderr.getvalue() == ""  # supported -> silent, no warning
    # Phase-5.6 audit decision: the official MCP SDK's current version is
    # in the list (newest first) and echoes like any supported version.
    response_new = server.handle_message(
        {"jsonrpc": "2.0", "id": 6, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25"}}, stderr)
    assert response_new is not None
    assert response_new["result"]["protocolVersion"] == "2025-11-25"
    assert server.SUPPORTED_PROTOCOL_VERSIONS[0] == "2025-11-25"
    assert stderr.getvalue() == ""


def test_initialize_echoes_older_supported_version() -> None:
    stderr = io.StringIO()
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}}, stderr)
    assert response is not None
    # exact echo, NOT silently upgraded to the newest version
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert stderr.getvalue() == ""


def test_initialize_falls_back_on_unsupported_version() -> None:
    stderr = io.StringIO()
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize",
         "params": {"protocolVersion": "banana-9.9"}}, stderr)
    assert response is not None  # handshake NOT failed
    assert response["result"]["protocolVersion"] == \
        server.DEFAULT_PROTOCOL_VERSION
    assert response["result"]["protocolVersion"] == "2025-11-25"
    # one-line warning naming the requested version, on stderr only
    warning = stderr.getvalue()
    assert warning.count("\n") == 1
    assert "banana-9.9" in warning


def test_ping_returns_empty_object() -> None:
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert response == {"jsonrpc": "2.0", "id": 4, "result": {}}
    # notification semantics: no id -> acknowledged, zero frames out
    assert server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_returns_tool_array() -> None:
    """tools/list contract: an array of self-describing tool entries.

    Phase 5.1 asserted the array was empty; Phase 5.2's spec puts
    asha_status in it, so emptiness is superseded by structure.
    """
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    assert response is not None
    tools: list[dict[str, Any]] = response["result"]["tools"]
    assert isinstance(tools, list)
    assert all({"name", "description", "inputSchema"} <= set(entry)
               for entry in tools)


def test_malformed_json_rpc_fails_closed() -> None:
    stdin = io.StringIO(
        'THIS IS NOT JSON\n'
        '\n'
        '{"jsonrpc": "2.0", "id": 7}\n'
        '{"jsonrpc": "2.0", "id": 8, "method": "ping"}\n')
    stdout, stderr = io.StringIO(), io.StringIO()
    code = server.serve(stdin, stdout, stderr)
    assert code == 0  # EOF, not a crash

    text = stdout.getvalue()
    frames = [json.loads(line) for line in text.splitlines()]
    assert len(frames) == 3
    # parse error on bad JSON (id null), loop kept going
    assert frames[0]["error"]["code"] == server.PARSE_ERROR
    assert frames[0]["id"] is None
    # request missing required fields -> structured invalid request
    assert frames[1]["error"]["code"] == server.INVALID_REQUEST
    assert frames[1]["id"] == 7
    # still alive after garbage: the next request is answered normally
    assert frames[2]["result"] == {}
    # stdout purity: every line is one JSON frame, never a traceback
    assert "Traceback" not in text
    assert all(line.startswith("{") for line in text.splitlines())


# --------------------------------------------------------------------------
# Phase 5.2: asha_status tool
# --------------------------------------------------------------------------


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    def git(*args: str) -> None:
        assert subprocess.run(["git", *args], cwd=path,
                              capture_output=True).returncode == 0
    git("init")
    git("config", "user.email", "asha@test.local")
    git("config", "user.name", "Asha Test")
    (path / "file.txt").write_text("hello\\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "base")
    return path


def _call_tool(name: str, arguments: dict[str, Any],
               request_id: int = 1) -> dict[str, Any]:
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}})
    assert response is not None
    return response


def test_tools_list_includes_asha_status() -> None:
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
    assert response is not None
    tools: list[dict[str, Any]] = response["result"]["tools"]
    assert "asha_status" in [tool["name"] for tool in tools]
    spec = next(tool for tool in tools if tool["name"] == "asha_status")
    schema = spec["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["root"] == {"type": "string",
                                            "default": ".",
                                            "description": server.ROOT_HELP}
    # `root` is optional: never listed as required
    assert "root" not in schema.get("required", [])


def test_asha_status_clean_tree(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "clean-repo")
    response = _call_tool("asha_status", {"root": str(repo)})
    assert response["result"]["isError"] is False
    payload: dict[str, Any] = json.loads(
        response["result"]["content"][0]["text"])
    assert payload["clean"] is True
    assert payload["status"] == []
    assert re.fullmatch(r"[0-9a-f]{40}", payload["head"]) is not None
    active_paths = [Path(item) for item in payload["worktrees"]["active"]]
    assert repo in active_paths  # Path equality tolerates / vs \ forms
    assert payload["worktrees"]["orphaned"] == []
    assert payload["lock"]["exists"] is False


def test_asha_status_dirty_tree(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "dirty-repo")
    (repo / "pending.txt").write_text("uncommitted\\n", encoding="utf-8")
    response = _call_tool("asha_status", {"root": str(repo)})
    assert response["result"]["isError"] is False
    payload: dict[str, Any] = json.loads(
        response["result"]["content"][0]["text"])
    assert payload["clean"] is False
    assert any("pending.txt" in line for line in payload["status"])


def test_asha_status_invalid_root_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-dir"
    response = _call_tool("asha_status", {"root": str(missing)})
    assert response["result"]["isError"] is True
    assert "no-such-dir" in response["result"]["content"][0]["text"]
    assert response["result"]["content"][0]["type"] == "text"

    # a path that exists but is not a git repo: same fail-closed contract
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    response = _call_tool("asha_status", {"root": str(plain)})
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["type"] == "text"


# --------------------------------------------------------------------------
# Phase 5.4: asha_plan_dag tool (static pre-flight planning, no execution)
# --------------------------------------------------------------------------


def _plan(root: Path, *, paths: list[str] | None = None,
          workers: list[dict[str, Any]] | dict[str, Any] | None = None,
          request_id: int = 20) -> dict[str, Any]:
    arguments: dict[str, Any] = {"root": str(root)}
    if paths is not None:
        arguments["paths"] = paths
    if workers is not None:
        arguments["workers"] = workers
    response = _call_tool("asha_plan_dag", arguments, request_id)
    assert "result" in response, response  # RED: unknown tool -> error frame
    result: dict[str, Any] = response["result"]
    if result["isError"]:
        return {"error": result["content"][0]["text"]}
    return json.loads(result["content"][0]["text"])


def test_asha_plan_dag_basic_topology(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("import b\n", encoding="utf-8")
    workers = [
        {"id": "w1", "deps": [], "declared_scope": ["."],
         "reads": [], "writes": ["a.py"], "cmd": ["python", "-c", "pass"]},
        {"id": "w2", "deps": ["w1"], "declared_scope": ["."],
         "reads": [], "writes": ["b.py"], "cmd": ["python", "-c", "pass"]},
        {"id": "w3", "deps": ["w2"], "declared_scope": ["."],
         "reads": [], "writes": ["c.py"], "cmd": ["python", "-c", "pass"]},
    ]
    plan = _plan(tmp_path, workers=workers)
    assert plan["mode"] == "workers"
    assert plan["generations"] == [["w1"], ["w2"], ["w3"]]
    assert {"source": "b.py", "target": "a.py"} in plan["import_edges"]
    assert {"source": "c.py", "target": "b.py"} in plan["import_edges"]
    assert all(flag == "safe" for flag in plan["safety"].values())
    assert plan["conflict_matrix"]["w1"]["w2"] == "proven_disjoint"


def test_asha_plan_dag_conflict_detection(tmp_path: Path) -> None:
    # conflict.py is file-granularity by design ("no semantic precision is
    # claimed"): two writers on the SAME file is the structural form of
    # "two paths that touch the same symbol".
    (tmp_path / "shared.py").write_text("SYMBOL = 1\n", encoding="utf-8")
    workers = [
        {"id": "alpha", "deps": [], "declared_scope": ["."],
         "reads": [], "writes": ["shared.py"],
         "cmd": ["python", "-c", "pass"]},
        {"id": "beta", "deps": [], "declared_scope": ["."],
         "reads": [], "writes": ["shared.py"],
         "cmd": ["python", "-c", "pass"]},
    ]
    plan = _plan(tmp_path, workers=workers)
    cell = plan["conflict_matrix"]["alpha"]["beta"]
    assert "write_write_overlap" in cell
    assert plan["safety"]["alpha"] == "uncertain"
    assert plan["safety"]["beta"] == "uncertain"
    assert plan["generations"] == [["alpha", "beta"]]


def test_asha_plan_dag_respects_paths_filter(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("import y\n", encoding="utf-8")
    (tmp_path / "y.py").write_text("SYMBOL = 2\n", encoding="utf-8")
    (tmp_path / "z.py").write_text("import x\n", encoding="utf-8")
    plan = _plan(tmp_path, paths=["x.py", "y.py"])
    assert plan["mode"] == "files"
    # layering inside the filtered set: y before x (x imports y); z absent
    assert plan["generations"] == [["y.py"], ["x.py"]]
    assert plan["import_edges"] == [{"source": "x.py", "target": "y.py"}]
    assert "z.py" not in plan["safety"]


def test_asha_plan_dag_invalid_root_fails_closed(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "no-such-plan-root")
    assert "error" in plan
    assert "no-such-plan-root" in plan["error"]


# --------------------------------------------------------------------------
# Phase 5.5: asha_run_spec tool (DRY-RUN only; apply=true rejected here)
# --------------------------------------------------------------------------


def _run_spec(arguments: dict[str, Any],
              request_id: int) -> dict[str, Any]:
    response = _call_tool("asha_run_spec", arguments, request_id)
    assert "result" in response, response  # RED: unknown tool -> error frame
    result: dict[str, Any] = response["result"]
    if result["isError"]:
        return {"error": result["content"][0]["text"]}
    return json.loads(result["content"][0]["text"])


def test_asha_run_spec_dry_run_from_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")
    marker = tmp_path / "MARKER_RAN"
    spec = {"workers": [
        {"id": "w1", "deps": [], "declared_scope": ["."], "reads": [],
         "writes": ["a.py"],
         "cmd": ["python", "-c",
                 f"open({str(marker)!r}, 'w').write('ran')"]},
        {"id": "w2", "deps": ["w1"], "declared_scope": ["."],
         "reads": ["a.py"], "writes": ["b.py"],
         "cmd": ["python", "-c", "pass"]},
    ]}
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")

    payload = _run_spec({"spec_path": str(spec_file),
                         "root": str(tmp_path)}, 30)
    assert payload["dry_run"] is True
    assert payload["generation_count"] == 2
    by_id = {entry["worker_id"]: entry for entry in payload["completed"]}
    assert set(by_id) == {"w1", "w2"}
    assert by_id["w1"]["generation"] == 0
    assert by_id["w2"]["generation"] == 1
    assert all(entry["state"] == "SIMULATED_DONE"
               for entry in payload["completed"])
    # dry-run contract: NOTHING ran (w1's cmd would create the marker)
    assert not marker.exists()


def test_asha_run_spec_dry_run_inline_spec(tmp_path: Path) -> None:
    spec = {"workers": [
        {"id": "solo", "deps": [], "declared_scope": ["."], "reads": [],
         "writes": ["only.py"], "cmd": ["python", "-c", "pass"]},
    ]}
    payload = _run_spec({"spec_content": spec, "root": str(tmp_path)}, 31)
    assert payload["dry_run"] is True
    assert payload["generation_count"] == 1
    assert payload["completed"] == [
        {"worker_id": "solo", "generation": 0, "state": "SIMULATED_DONE"}]


def test_asha_run_spec_apply_type_gates_and_enablement(
        tmp_path: Path) -> None:
    spec = {"workers": [
        {"id": "solo", "deps": [], "declared_scope": ["."], "reads": [],
         "writes": ["x.py"], "cmd": ["python", "-c", "pass"]},
    ]}
    # the apply TYPE gate survives Phase 5.6 (fail-closed, pre-side-effect)
    payload = _run_spec({"spec_content": spec, "root": str(tmp_path),
                         "apply": "yes"}, 32)
    assert payload.get("error") == "apply must be a boolean"
    # Phase 5.6 flipped the capability: apply=true is no longer answered
    # with the 5.5 "intentionally disabled" rejection (its real-execution
    # semantics are proven in tests/test_mcp_apply.py).
    raw = _call_tool("asha_run_spec",
                     {"spec_content": spec, "root": str(tmp_path),
                      "apply": True}, 35)
    assert "result" in raw, raw
    assert "intentionally disabled" not in json.dumps(raw["result"])


def test_asha_run_spec_malformed_spec_fails_closed(tmp_path: Path) -> None:
    payload = _run_spec({"spec_content": {"task": "nope"},
                         "root": str(tmp_path)}, 33)
    assert "error" in payload
    assert "workers" in payload["error"]
    # structurally invalid worker: the SAME validate_workers gate as
    # asha_plan_dag / a real run (missing cmd cannot pass here either)
    payload = _run_spec({"spec_content": {"workers": [
        {"id": "bad", "deps": [], "writes": ["x.py"]}]},
        "root": str(tmp_path)}, 34)
    assert "error" in payload
    assert "cmd" in payload["error"]


def test_asha_run_spec_workers_shape_matches_plan_dag(
        tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")
    base = {"deps": [], "declared_scope": ["."], "reads": [],
            "cmd": ["python", "-c", "pass"]}
    w1_body = {**base, "writes": ["a.py"]}
    w2_body = {**base, "writes": ["b.py"], "deps": ["w1"]}

    def run(content: dict[str, Any]) -> dict[str, Any]:
        payload = _run_spec({"spec_content": content,
                             "root": str(tmp_path)}, 40)
        assert "error" not in payload, payload
        return payload

    from_list = run({"workers": [dict(w1_body, id="w1"),
                                 dict(w2_body, id="w2")]})
    from_map = run({"workers": {"w1": dict(w1_body),
                                "w2": dict(w2_body)}})
    from_single = run({"workers": dict(w1_body, id="w1")})
    # same normalizer: list and {id: worker} map give identical DAGs
    assert from_list["generations"] == from_map["generations"]
    assert from_list["generations"] == [["w1"], ["w2"]]
    assert from_list["completed"] == from_map["completed"]
    assert from_single["generation_count"] == 1
    assert [entry["worker_id"]
            for entry in from_single["completed"]] == ["w1"]
    # the DAG is literally asha_plan_dag's, never a second builder
    plan = _plan(tmp_path,
                 workers=[dict(w1_body, id="w1"), dict(w2_body, id="w2")])
    assert plan["generations"] == from_list["generations"]
