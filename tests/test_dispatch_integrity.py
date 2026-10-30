"""Dispatch truthfulness: what ran, what verified, what may be pushed."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from asha import check_runner, cli
from asha.governance import worker_execution


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "A")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _worker(cmd: str = "printf ok > out.txt") -> dict:
    return {"id": "w1", "deps": [], "declared_scope": ["out.txt"],
            "reads": [], "writes": ["out.txt"], "cmd": ["sh", "-c", cmd]}


def test_a_missing_tool_is_blocked_not_a_test_failure() -> None:
    output = "/venv/bin/python: No module named ruff\n"
    assert check_runner._tool_unavailable("ruff", output) == "ruff"
    # a test importing a missing module stays a real test failure
    assert check_runner._tool_unavailable(
        "ruff", "ModuleNotFoundError: No module named 'requests'") is None


def test_worker_runs_and_reports_what_verified(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.txt": "one\n",
                            "test_ok.py": "def test_ok():\n    assert True\n"})
    report = worker_execution.run_worker_in_worktree(
        repo, _worker(), task_id="t1")
    state = report["states"]["w1"]
    assert state["state"] == "DONE"
    assert state["exit_status"] == 0
    assert state["writes_observed"] == ["out.txt"]
    assert state["verification_status"] == "VERIFIED_NO_REGRESSION"
    # ruff/mypy are not installed here: nothing is verified, nothing is ready
    sealed = json.loads(Path(state["evidence"]).read_text(encoding="utf-8"))
    assert sealed["authorized_to_ship"] is False
    assert sealed["validation_status"] != "PASS"
    assert state["ready_to_push"] is False


def test_inconclusive_verification_is_not_a_worker_failure(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.txt": "one\n",
                            "test_broken.py": "def test(syntax error\n"})
    report = worker_execution.run_worker_in_worktree(
        repo, _worker("true"), task_id="t2")
    state = report["states"]["w1"]
    # not DONE (fail-closed), but the facts are separated: the command
    # ran (exit 0) while the verification stayed unattributable
    assert state["state"] == "FAILED"
    assert state["verification_status"] == "COMPARISON_INCONCLUSIVE"
    assert state["exit_status"] == 0
    assert state["evidence"] is None


def test_executed_reflects_a_real_process() -> None:
    from asha import mcp_server
    assert mcp_server._execution_ran({"exit_status": 0}, {}) is True
    assert mcp_server._execution_ran({"state": "FAILED"}, {}) is False
    assert mcp_server._execution_ran({}, {"state": "INCONCLUSIVE"}) is False


def test_evaluation_only_run_does_not_exit_zero() -> None:
    payload = {"decision": "COMPLETE", "change_set": {"base": "HEAD~1"},
               "validation_result": None, "evidence_id": None}
    result = {"content": [{"text": json.dumps(payload)}]}
    assert cli._emit_unified(result, as_json=True) == cli.EXIT_UNVALIDATED
    # a real status payload keeps its normal exit code
    ok = {"content": [{"text": json.dumps({"state": "DONE", "root": "/r"})}]}
    assert cli._emit_unified(ok, as_json=True) == cli.EXIT_OK
