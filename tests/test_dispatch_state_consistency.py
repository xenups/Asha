"""Regression tests: `asha_dispatch_task` public fields must not contradict.

Benchmark evidence: `/home/amir/asha-agent-eval/20261020T201750Z` reported
`state: DONE` next to `reason_code: missing_signal_surface`, i.e. a routing
classification code presented as if it were the run's failure verdict.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import asha.mcp_server as server

PY = sys.executable
CLASSIFICATION_CODES = {
    "missing_signal_surface",
    "missing_signal_other_surface",
    "missing_context",
    "unknown_dependency",
    "ambiguous_declared_scope",
}
PARTIAL = "open('tests/test_partial.py','w').write('x = 1\\n')\nraise SystemExit(4)\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / ".gitignore").write_text(".jspace/\n__pycache__/\n*.pyc\n")
    (repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    (repo / "README.md").write_text("# fixture\n")
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "core.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _dispatch(repo: Path, wid: str, **kwargs) -> dict:
    args = {"root": str(repo), "id": wid, "declared_scope": ["tests/"],
            "reads": [], "writes": ["tests/t_out.py"],
            "cmd": [PY, "-c", "pass"]}
    args.update(kwargs)
    frame = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "asha_dispatch_task", "arguments": args}})
    assert frame is not None
    return frame["result"]


def _assert_no_contradiction(payload: dict) -> None:
    """A classification code may never appear as the run's reason_code, and
    any non-null outcome code must deny push readiness."""
    assert payload["reason_code"] not in CLASSIFICATION_CODES, payload
    assert "classification_reason" in payload
    if payload["reason_code"]:
        assert payload["ready_to_push"] is False, payload


# ------------------------------------------------------- both missing-surface

def test_missing_signal_surface_never_reads_as_task_success(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = _payload(_dispatch(repo, "mss", reads=None, writes=None))

    # executed: the command ran; the classification could not prove surface
    assert payload["state"] == "DONE"
    assert payload["executed"] is True
    assert payload["classification_reason"] == "missing_signal_surface"
    # ...and nothing says the TASK is done
    assert payload["applied"] is False
    assert payload["ready_to_push"] is False
    assert payload["authorized_to_ship"] is False
    _assert_no_contradiction(payload)


def test_missing_signal_other_surface_is_classification_only(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = server.classify_task

    def fake(task, context):
        return dataclasses.replace(real(task, context),
                                   reason_code="missing_signal_other_surface")

    monkeypatch.setattr(server, "classify_task", fake)
    payload = _payload(_dispatch(_repo(tmp_path), "mso"))

    # peer surface unknown: blocks Fast Path, still executes this task
    assert payload["classification"] == "UNKNOWN"
    assert payload["classification_reason"] == "missing_signal_other_surface"
    assert payload["reason_code"] is None
    assert payload["state"] == "DONE"
    assert payload["executed"] is True
    assert payload["ready_to_push"] is False
    _assert_no_contradiction(payload)


# ------------------------------------------------------------- happy paths

def test_success_reports_outcome_and_verdicts_separately(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = _payload(_dispatch(
        repo, "ok",
        cmd=[PY, "-c", "open('tests/test_new.py','w').write('x = 1\\n')"]))

    assert payload["state"] == "DONE"
    assert payload["executed"] is True
    assert payload["reason_code"] is None
    assert payload["validation_status"] == "NOT_CONFIGURED"
    assert payload["validation_result"] == "NOT_CONFIGURED"
    assert payload["ready_to_push"] is False
    assert payload["authorized_to_ship"] is False
    _assert_no_contradiction(payload)


def test_legitimate_no_op_stays_done_and_not_applied(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = _payload(_dispatch(repo, "noop", reads=[],
                                 writes=["tests/test_noop.py"]))

    assert payload["state"] == "DONE"
    assert payload["executed"] is True
    assert payload["ready_to_push"] is False


# ------------------------------------------------- refusals and failures

def test_policy_refusal_before_execution(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)

    def boom(profile, fast_path_enabled=False):
        raise RuntimeError("router exploded")

    monkeypatch.setattr(server, "route", boom)
    result = _dispatch(repo, "rf")

    assert result.get("isError") is True
    assert "nothing executed" in result["content"][0]["text"]


def test_validation_failure_is_a_verdict_not_a_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = _payload(_dispatch(
        repo, "vf", execution_mode="in_place_atomic",
        cmd=[PY, "-c", "open('tests/test_v.py','w').write('x = 1\\n')"],
        validation=[f"{PY} -c 'import sys; sys.exit(3)'"]))

    assert payload["validation_result"] in ("FAIL", "BLOCKED")
    assert payload["ready_to_push"] is False
    assert payload["authorized_to_ship"] is False
    _assert_no_contradiction(payload)


def test_failure_after_side_effect_is_failed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = _payload(_dispatch(repo, "fs", execution_mode="in_place_atomic",
                                 cmd=[PY, "-c", PARTIAL]))

    assert payload["state"] == "FAILED"
    assert payload["executed"] is True
    assert payload["ready_to_push"] is False
    _assert_no_contradiction(payload)


def test_missing_evidence_fails_closed(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    import asha.evidence as evidence_mod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(evidence_mod, "write_to", boom, raising=False)
    payload = _payload(_dispatch(
        repo, "ev", execution_mode="in_place_atomic",
        cmd=[PY, "-c", "open('tests/test_e.py','w').write('x = 1\\n')"]))

    assert payload["state"] == "FAILED"
    assert payload["authorized_to_ship"] is False
    assert payload["ready_to_push"] is False


# ------------------------------------------------------- serialization path

def test_mcp_serialization_matches_in_process(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    proc = subprocess.Popen([PY, "-m", "asha.mcp_server"], cwd=REPO,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)

    def rpc(i: int, method: str, params: dict) -> dict:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i,
                                     "method": method,
                                     "params": params}) + "\n")
        proc.stdin.flush()
        assert proc.stdout is not None
        return json.loads(proc.stdout.readline())

    try:
        rpc(1, "initialize", {"protocolVersion": "2024-11-05",
                              "capabilities": {},
                              "clientInfo": {"name": "t", "version": "1"}})
        refused = rpc(2, "tools/call", {
            "name": "asha_dispatch_task",
            "arguments": {"root": str(repo), "id": "ser-mss",
                          "declared_scope": ["tests/"],
                          "cmd": [PY, "-c", "pass"]}})
        frame = rpc(3, "tools/call", {
            "name": "asha_dispatch_task",
            "arguments": {"root": str(repo), "id": "ser",
                          "declared_scope": ["tests/"], "reads": [],
                          "writes": ["tests/t_out.py"],
                          "cmd": [PY, "-c", "pass"]}})
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)

    ref_payload = json.loads(refused["result"]["content"][0]["text"])
    assert refused["result"]["isError"] is False
    assert ref_payload["classification_reason"] == "missing_signal_surface"
    assert ref_payload["ready_to_push"] is False

    payload = json.loads(frame["result"]["content"][0]["text"])
    assert frame["result"]["isError"] is False
    assert payload["state"] == "DONE"
    assert payload["reason_code"] is None
    assert payload["classification_reason"] in CLASSIFICATION_CODES
    assert payload["executed"] is True
    assert payload["authorized_to_ship"] is False
    _assert_no_contradiction(payload)
