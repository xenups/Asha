"""Phase 2 TDD: governed Agent Runners (asha/runner.py).

Contracts: CommandRunner = byte-compatible with the pre-Phase-2
default_execute spawn behavior; AntigravityRunner = headless argv with
cwd locked to the worktree; G3 timeout (kill_process_tree) shared by all
runners; verify_command chains only after a zero primary exit and its
code decides the final exit; runner/agent spec validation fails closed.
"""
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asha import (
    AntigravityRunner,
    BaseAgentRunner,
    CommandRunner,
    OrchestratorError,
    RunnerResult,
    default_execute,
    dispatch_runner,
    validate_workers,
)


def test_command_runner_preserves_raw_argv_behavior(tmp_path: Path) -> None:
    """Backward compat: raw `command` executes via CommandRunner with the
    same primitive (rc/stdout byte-equal to subprocess.run) and the
    scheduler hook keeps its (rc, tail) shape."""
    worker: dict[str, Any] = {
        "id": "raw", "deps": [], "writes": ["a.py"], "timeout": 30,
        "cmd": [sys.executable, "-c", "print('cmd-runner-ok')"],
    }
    result = CommandRunner().execute(worker, tmp_path, timeout=30)
    assert isinstance(result, RunnerResult)
    assert result.exit_code == 0
    assert "cmd-runner-ok" in result.stdout
    assert result.stderr == ""
    assert result.duration_s >= 0
    assert result.audit_metadata["runner"] == "command"
    assert result.audit_metadata["cwd"] == str(tmp_path)
    reference = subprocess.run(worker["cmd"], cwd=tmp_path,
                               capture_output=True, text=True)
    assert reference.returncode == result.exit_code
    assert reference.stdout == result.stdout
    # default_execute hook shape untouched (rc, tail-of-combined-output)
    rc, tail = default_execute(worker, tmp_path)
    assert rc == 0
    assert "cmd-runner-ok" in tail


def test_antigravity_argv_is_headless_workspace_scoped(
        tmp_path: Path) -> None:
    """CLI generation: headless flags, cwd/workspace bound to the
    worktree, task prompt carries the job, target files and acceptance
    command as ONE argv element (never a shell string)."""
    worker: dict[str, Any] = {
        "id": "ag", "prompt": "Fix the failing widget",
        "writes": ["src/widget.py"],
        "verify_command": f"{sys.executable} -m pytest tests/ -q",
    }
    argv = AntigravityRunner().primary_argv(worker, tmp_path)
    assert argv[0] == "antigravity"
    workspace_at = argv.index("--workspace")
    assert argv[workspace_at + 1] == str(tmp_path)
    assert "--non-interactive" in argv
    task_at = argv.index("--task")
    task = argv[task_at + 1]
    assert argv.count("--task") == 1
    assert "Fix the failing widget" in task
    assert "src/widget.py" in task          # injected target files (writes)
    assert "-m pytest tests/ -q" in task    # injected acceptance command
    assert all(isinstance(item, str) for item in argv)


def test_runner_timeout_kills_process_tree_fast(tmp_path: Path) -> None:
    """G3: a timed-out runner raises TimeoutExpired AFTER killing the
    child tree -- teardown is fast, not the 30s sleep."""
    worker: dict[str, Any] = {
        "id": "slow", "deps": [], "writes": ["x"], "timeout": 1,
        "cmd": [sys.executable, "-c", "import time; time.sleep(30)"],
    }
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        CommandRunner().execute(worker, tmp_path, timeout=1)
    assert time.monotonic() - started < 10


def test_verify_command_chains_and_decides_final_exit(
        tmp_path: Path) -> None:
    """Verification chaining: zero primary exit -> verify runs and its
    exit code becomes final; failing primary -> verify never runs
    (marker file proves non-execution)."""
    worker: dict[str, Any] = {
        "id": "v", "deps": [], "writes": ["w"], "timeout": 30,
        "cmd": [sys.executable, "-c", "print('primary')"],
        "verify_command": f'{sys.executable} -c "import sys; sys.exit(2)"',
    }
    result = CommandRunner().execute(worker, tmp_path, timeout=30)
    assert result.exit_code == 2               # verify decides final
    assert "primary" in result.stdout
    assert result.audit_metadata["verify_ran"] is True
    assert result.audit_metadata["verify_exit"] == 2
    failing: dict[str, Any] = {
        "id": "b", "deps": [], "writes": [], "timeout": 30,
        "cmd": [sys.executable, "-c", "import sys; sys.exit(7)"],
        "verify_command": (
            f'{sys.executable} -c "open(\'ran\',\'w\').write(\'x\')"'),
    }
    second = CommandRunner().execute(failing, tmp_path, timeout=30)
    assert second.exit_code == 7               # primary code, verify skipped
    assert second.audit_metadata["verify_ran"] is False
    assert not (tmp_path / "ran").exists()


def test_spec_validation_fails_closed_for_runner_fields() -> None:
    """Invalid runner type / bad agent / missing prompt / bad
    verify_command -> OrchestratorError before anything runs. Agent
    workers may omit cmd; command workers still must not (back-compat)."""
    base: dict[str, Any] = {"deps": [], "writes": ["x"], "timeout": 5,
                            "cmd": [sys.executable, "-c", "pass"]}
    with pytest.raises(OrchestratorError, match="unknown runner"):
        validate_workers([{**base, "id": "r1",
                           "runner": {"type": "teleporter"}}])
    with pytest.raises(OrchestratorError, match="unknown agent"):
        validate_workers([{**base, "id": "r2", "agent": "teleporter"}])
    with pytest.raises(OrchestratorError, match="requires prompt"):
        validate_workers([{"deps": [], "writes": ["y"], "timeout": 5,
                           "id": "r3", "agent": "antigravity"}])
    with pytest.raises(OrchestratorError, match="prompt must be"):
        validate_workers([{"deps": [], "writes": ["y"], "timeout": 5,
                           "id": "r4", "agent": "antigravity",
                           "prompt": "   "}])
    with pytest.raises(OrchestratorError, match="verify_command must be"):
        validate_workers([{**base, "id": "r5", "verify_command": 42}])
    # valid agent spec WITHOUT cmd passes the cmd gate
    ok = validate_workers([{"deps": [], "writes": ["y"], "timeout": 5,
                            "id": "ag", "agent": "antigravity",
                            "prompt": "do it"}])
    assert ok[0]["id"] == "ag"
    # dispatch picks the right runner for both trigger spellings
    assert isinstance(dispatch_runner(ok[0]), AntigravityRunner)
    assert isinstance(dispatch_runner({"cmd": ["x"]}), CommandRunner)
    assert isinstance(dispatch_runner(
        {"runner": {"type": "antigravity"}, "prompt": "p"}),
        AntigravityRunner)
    # protocol surface
    assert isinstance(CommandRunner(), BaseAgentRunner)
    # command kind still demands cmd (back-compat gate, exact message)
    with pytest.raises(OrchestratorError,
                       match="cmd must be a non-empty argv list"):
        validate_workers([{"deps": [], "writes": [], "timeout": 5,
                           "id": "nocmd"}])
