"""Regression tests for the 0.5.1 benchmark remediation.

Every test reproduces a demonstrated failure from
`/home/amir/asha-agent-eval/20261020T190156Z`:
  * a forged completion claim reported `status:"ok"` (C12/B3)
  * `asha` zero-config and `cmd` failed on a clean tree (C24/C26)
  * `asha_dispatch_task` reported `DONE` + `validation_result:"FAIL"` (C22)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import asha.mcp_server as server

PY = sys.executable


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / ".gitignore").write_text(
        ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n")
    (repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    (repo / "README.md").write_text("# fixture\n")
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "canary.py").write_text("CANARY = 'untouched'\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _call(name: str, arguments: dict) -> dict:
    frame = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})
    assert frame is not None
    return frame["result"]


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _spec(tmp_path: Path, workers: list[dict]) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"workers": workers}))
    return path


def _write_cmd(rel: str, body: str) -> list[str]:
    """argv that writes `body` to `rel` (no shell quoting involved)."""
    code = f"from pathlib import Path\nPath({rel!r}).write_text({body!r})\n"
    return [PY, "-c", code]


def _worker(cmd: list[str], writes: list[str],
            declared_scope: list[str] | None = None) -> dict:
    return {"id": "w1", "deps": [],
            "declared_scope": declared_scope or ["tests/"],
            "reads": [], "writes": writes, "cmd": cmd}


# ---------------------------------------------------------------- Phase 2

def test_forged_completion_is_not_reported_as_applied(tmp_path: Path) -> None:
    """A claim with no observed change must not read as an application."""
    repo = _make_repo(tmp_path)
    spec = _spec(tmp_path, [_worker([PY, "-c", "print('42 tests passed')"],
                                    ["tests/test_claimed.py"])])

    payload = _payload(_call("asha_run_spec", {
        "root": str(repo), "spec_path": str(spec), "apply": True}))

    state = payload["states"]["w1"]
    assert state["applied"] is False
    assert state["writes_observed"] == []
    assert state["comparison"]["reason"] == "BASE_EQUALS_TARGET"
    assert payload["authorized_to_ship"] is False


def test_worker_that_writes_its_declared_file_succeeds(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = _spec(tmp_path, [_worker(
        _write_cmd("tests/test_new.py", "def test_new():\n    assert True\n"),
        ["tests/test_new.py"])])

    payload = _payload(_call("asha_run_spec", {
        "root": str(repo), "spec_path": str(spec), "apply": True}))

    assert payload["status"] == "ok"
    assert payload["successful_workers"] == ["w1"]
    assert payload["states"]["w1"]["applied"] is True


def test_worker_without_declared_writes_is_not_punished(tmp_path: Path) -> None:
    """writes=[] stays a legal no-op (conflict-planning only)."""
    repo = _make_repo(tmp_path)
    spec = _spec(tmp_path, [_worker([PY, "-c", "pass"], [])])

    payload = _payload(_call("asha_run_spec", {
        "root": str(repo), "spec_path": str(spec), "apply": True}))

    assert payload["status"] == "ok"
    assert payload["successful_workers"] == ["w1"]


def test_scope_violation_still_refused(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = _spec(tmp_path, [_worker(
        _write_cmd("pkg/canary.py", "CANARY = 'pwned'\n"),
        ["tests/test_x.py"])])

    payload = _payload(_call("asha_run_spec", {
        "root": str(repo), "spec_path": str(spec), "apply": True}))

    assert payload["status"] == "failed"
    assert "INVALID_EVIDENCE" in str(payload["reason"])
    assert (repo / "pkg" / "canary.py").read_text() == "CANARY = 'untouched'\n"


# ---------------------------------------------------------------- Phase 3

def test_facade_zero_config_on_clean_repo(tmp_path: Path) -> None:
    """Clean tree with nothing since base: report it, do not fail closed."""
    repo = _make_repo(tmp_path)

    result = _call("asha", {"root": str(repo)})
    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ready_to_push"] is True
    assert "nothing to verify" in payload["response"]


def test_facade_cmd_on_clean_repo_is_actionable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    result = _call("asha", {"root": str(repo), "cmd": "true"})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "SCOPE_AUTO_EMPTY" in text
    assert "declared_scope" in text


# ---------------------------------------------------------------- Phase 4

def test_dispatch_does_not_report_contradictory_validation(tmp_path: Path) -> None:
    """DONE means execution finished; validation_result must say the truth."""
    repo = _make_repo(tmp_path)
    result = _call("asha_dispatch_task", {
        "root": str(repo), "id": "d1", "declared_scope": ["tests/"],
        "cmd": _write_cmd("tests/test_d.py",
                          "def test_d():\n    assert True\n")})

    payload = _payload(result)
    assert payload["state"] == "DONE"
    assert payload["validation_status"] == "NOT_CONFIGURED"
    assert payload["validation_result"] == "NOT_CONFIGURED"
    assert payload["ready_to_push"] is False
    assert payload["authorized_to_ship"] is False


def test_dispatch_rejects_validation_worktree_combination(tmp_path: Path) -> None:
    """Worktree mode never runs validation: refuse instead of pretending."""
    repo = _make_repo(tmp_path)
    result = _call("asha_dispatch_task", {
        "root": str(repo), "id": "d2", "declared_scope": ["tests/"],
        "cmd": _write_cmd("tests/test_d.py",
                          "def test_d():\n    assert True\n"),
        "validation": [f"{PY} -c 'import sys; sys.exit(3)'"]})

    assert result["isError"] is True
    assert "in_place_atomic" in result["content"][0]["text"]
    assert (repo / "tests" / "test_d.py").exists() is False
