"""D-1: execution identity for the worktree / DAG worker command path."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha.governance import dag, worker_execution

PY = sys.executable
EXEC_ID_RE = re.compile(r"^exec-[0-9a-f]{12}$")
FILES = {"a.txt": "one\n", "test_ok.py": "def test_ok():\n    assert True\n"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, name: str = "repo",
          files: dict[str, str] | None = None) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "A")
    for rel, text in (files or FILES).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _worker(cmd: str = "printf ok > out.txt", wid: str = "w1",
            **extra: object) -> dict:
    worker: dict[str, Any] = {"id": wid, "deps": [], "declared_scope": ["out.txt"],
              "reads": [], "writes": ["out.txt"], "cmd": ["sh", "-c", cmd]}
    worker.update(extra)
    return worker


def test_worker_run_carries_one_execution_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = worker_execution.run_worker_in_worktree(
        repo, _worker(), task_id="t1")
    state = report["states"]["w1"]
    assert state["state"] == "DONE"
    assert state["exit_status"] == 0
    assert EXEC_ID_RE.match(state["execution_id"])


def test_two_runs_never_share_an_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = worker_execution.run_worker_in_worktree(
        repo, _worker(), task_id="t1")["states"]["w1"]
    second = worker_execution.run_worker_in_worktree(
        repo, _worker(), task_id="t2")["states"]["w1"]
    assert first["execution_id"] != second["execution_id"]


def test_failed_command_keeps_its_execution_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = worker_execution.run_worker_in_worktree(
        repo, _worker("printf bad > out.txt; exit 3"), task_id="t3")
    state = report["states"]["w1"]
    assert state["state"] == "FAILED"
    assert state["exit_status"] == 3
    assert EXEC_ID_RE.match(state["execution_id"])


def test_supplied_execution_id_is_not_replaced(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = worker_execution.run_worker_in_worktree(
        repo, _worker(), task_id="t4", execution_id="exec-owned-by-caller")
    assert report["states"]["w1"]["execution_id"] == "exec-owned-by-caller"


def test_factory_is_the_only_source_when_injected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = worker_execution.run_worker_in_worktree(
        repo, _worker(), task_id="t5",
        execution_id_factory=lambda: "exec-from-factory")
    assert report["states"]["w1"]["execution_id"] == "exec-from-factory"


def test_timeout_still_records_the_launched_execution(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = worker_execution.run_worker_in_worktree(
        repo, _worker("sleep 5", timeout=1), task_id="t6")
    state = report["states"]["w1"]
    assert state["state"] == "FAILED"
    assert state["reason"] == "timeout_exceeded"
    assert EXEC_ID_RE.match(state["execution_id"])


def test_concurrent_workers_get_distinct_ids(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workers = [
        {"id": "wa", "deps": [], "declared_scope": ["a.txt"],
         "reads": [], "writes": ["a.txt"],
         "cmd": ["sh", "-c", "printf x >> a.txt"]},
        {"id": "wb", "deps": [], "declared_scope": ["b.txt"],
         "reads": [], "writes": ["b.txt"],
         "cmd": ["sh", "-c", "printf y > b.txt"]},
    ]
    report = dag.run_workers_dag(repo, workers, task_id="t7")
    states = report["states"]
    assert {wid: entry["state"] for wid, entry in states.items()} == {
        "wa": "DONE", "wb": "DONE"}
    ids = [states[wid]["execution_id"] for wid in ("wa", "wb")]
    assert all(EXEC_ID_RE.match(value) for value in ids)
    assert len(set(ids)) == 2


def test_worker_blocked_before_launch_has_no_execution_id(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workers = [
        {"id": "wa", "deps": [], "declared_scope": ["a.txt"],
         "reads": [], "writes": ["a.txt"],
         "cmd": ["sh", "-c", "exit 5"]},
        {"id": "wb", "deps": ["wa"], "declared_scope": ["b.txt"],
         "reads": [], "writes": ["b.txt"],
         "cmd": ["sh", "-c", "printf y > b.txt"]},
    ]
    report = dag.run_workers_dag(repo, workers, task_id="t8")
    states = report["states"]
    assert states["wa"]["state"] == "FAILED"
    assert EXEC_ID_RE.match(states["wa"]["execution_id"])
    assert states["wb"]["state"] == "BLOCKED"
    assert "execution_id" not in states["wb"]


def test_existing_state_fields_are_unchanged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state = worker_execution.run_worker_in_worktree(
        repo, _worker(), task_id="t9")["states"]["w1"]
    for key in ("state", "reason", "exit_status", "writes_observed",
                "verification_status", "ready_to_push", "evidence"):
        assert key in state
