"""G3 remediation: worker process timeout & fail-closed termination.

TDD: this file is written BEFORE the implementation exists -- the
timeout key and the `timeout_exceeded` reason are new contract, so the
first run must fail (demonstrated pre-implementation failure), then go
green after the worktree/scheduler changes land.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1] / ".hermes" / "tools"

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import orchestrator  # (TOOLS must be on sys.path before this import)

# -- fixtures (house style: real scratch git repo) --------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(
        ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n")
    (repo / "README.md").write_text("# g3 fixture\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _worker(**overrides: Any) -> dict[str, Any]:
    worker: dict[str, Any] = {
        "id": "A", "deps": [], "declared_scope": ["tests/"],
        "reads": [], "writes": [],
        "cmd": [sys.executable, "-c", "pass"],
    }
    worker.update(overrides)
    return worker


# -- 1. hanging worker dies at its timeout, fail-closed ----------------------

def test_worker_timeout_fails_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    worker = _worker(
        cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=1,
    )

    started = time.monotonic()
    report = orchestrator.GovernedScheduler(
        repo, [worker], task_id="g3-timeout").run()
    elapsed = time.monotonic() - started

    assert report["status"] == "failed"
    entry = report["states"]["A"]
    assert entry["state"] == "FAILED"
    assert entry["reason"] == "timeout_exceeded"  # explicit, not worker_exit
    assert report["completed"] == []
    assert report["evidence"] == {}  # no evidence for a killed worker
    # teardown: zero orphaned worktree directories
    assert not (repo.parent / (repo.name + ".worktrees")).exists()
    assert report["cleanup_errors"] == []
    # a healthy early kill: the 10s sleep must never run to completion
    assert elapsed < 8.0, f"worker was not killed early ({elapsed:.1f}s)"


# -- 2. timeout spec validation at the trust boundary -------------------------

def test_timeout_spec_validation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    for bad in (0, -1, True, "1"):
        with pytest.raises(orchestrator.OrchestratorError, match="timeout"):
            orchestrator.GovernedScheduler(
                repo, [_worker(id="W", timeout=bad)],
                task_id=f"bad-{bad!r}")
    # unspecified and None keep the existing no-override behavior
    orchestrator.GovernedScheduler(
        repo, [_worker(id="W")], task_id="absent")
    orchestrator.GovernedScheduler(
        repo, [_worker(id="W", timeout=None)], task_id="none")
